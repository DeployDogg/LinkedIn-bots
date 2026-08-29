#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Sequence
from zoneinfo import ZoneInfo

ROOT = Path("/Users/deploydog-ai/LinkedIn")
BA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
DEFAULT_STATE = ROOT / "shared" / "state" / "LinkedInScheduler" / "state.json"
DEFAULT_LOG_DIR = ROOT / "shared" / "logs" / "LinkedInScheduler"
DEFAULT_LOCK = ROOT / "shared" / "state" / "LinkedInScheduler" / "scheduler.lock"
SAFETY_STOP_EXIT_CODES = {11, 12}
TASK_TIMEOUT_EXIT_CODE = 124
PROCESS_GROUP_TERM_GRACE_SECONDS = 5
LOCK_SKIP_REASONS = ("busy lock", "child not started")
JOBSEEKER_RETRY_INTERVAL = timedelta(hours=4)


@dataclass(frozen=True)
class Task:
    name: str
    service: str
    kind: str
    priority: int
    default_timeout_seconds: int

    def command(self) -> list[str]:
        command = ["docker", "compose", "exec", "-T"]
        if self.name == "jobseeker":
            command.extend(["-e", "LINKEDIN_WORKER_MAX_JOBS=60", "-e", "LINKEDIN_JOBSEEKER_QUEUE_MODE=1"])
        command.extend([self.service, "/app/scripts/run.sh"])
        return command

    def env(self) -> dict[str, str]:
        return {}

    def timeout_seconds(self) -> int:
        env_name = f"LINKEDIN_SCHEDULER_{self.name.upper()}_TIMEOUT_SECONDS"
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            return self.default_timeout_seconds
        try:
            value = int(raw)
        except ValueError:
            return self.default_timeout_seconds
        return value if value > 0 else self.default_timeout_seconds


TASKS = {
    # Commentator disabled: service is intentionally commented out in docker-compose.yml.
    "postliker": Task("postliker", "postliker", "fixed", 10, 900),
    "connectman": Task("connectman", "connectman", "fixed", 20, 900),
    "sendman": Task("sendman", "sendman", "fixed", 30, 1200),
    "jobseeker": Task("jobseeker", "jobseeker", "interval", 100, 7200),
}
RUN_ALL_ORDER = ["postliker", "connectman", "sendman", "jobseeker"]
TASK_TIMEOUT_ENV_NAMES = {
    f"LINKEDIN_SCHEDULER_{name.upper()}_TIMEOUT_SECONDS" for name in TASKS
}
SCHEDULE_LABELS = {
    "jobseeker": "every 4h until DOD/block, then block until 00:00 BA",
    "postliker": "daily 06:00 max once/day",
    "sendman": "daily09:00 max once/day",
    "connectman": "Mon11 + Wed11 fallback max once/day",
}


def schedule_label(name: str) -> str:
    return SCHEDULE_LABELS.get(name, "manual")


def load_scheduler_timeout_env(path: Path) -> None:
    """Load only scheduler timeout knobs from .env; never import worker secrets."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key not in TASK_TIMEOUT_ENV_NAMES or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


@dataclass
class RunResult:
    exit_code: int
    attempted: list[str]
    stopped: bool = False


@dataclass(frozen=True)
class TaskRunResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    reason: str | None = None
    timeout_seconds: int | None = None


class SchedulerState:
    def __init__(self, path: Path = DEFAULT_STATE):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "timezone": "America/Argentina/Buenos_Aires", "tasks": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            data = {"version": 1, "timezone": "America/Argentina/Buenos_Aires", "tasks": {}}
        data.setdefault("version", 1)
        data.setdefault("timezone", "America/Argentina/Buenos_Aires")
        data.setdefault("tasks", {})
        return data

    def write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BA_TZ)
    return dt.astimezone(BA_TZ).isoformat(timespec="seconds")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(BA_TZ)
    except Exception:
        return None


def today_at(now: datetime, hour: int, minute: int) -> datetime:
    return datetime.combine(now.astimezone(BA_TZ).date(), dtime(hour, minute), BA_TZ)


def last_attempt(data: dict, name: str) -> dict:
    return ((data.get("tasks") or {}).get(name) or {}).get("last_attempt") or {}


def has_completed_since(data: dict, name: str, since: datetime) -> bool:
    hist = ((data.get("tasks") or {}).get(name) or {}).get("history") or []
    attempts = hist + [last_attempt(data, name)]
    for attempt in attempts:
        if attempt.get("status") == "completed":
            finished = parse_dt(attempt.get("finished_at")) or parse_dt(attempt.get("started_at"))
            if finished and finished >= since:
                return True
    return False


EXECUTED_TERMINAL_STATUSES = {"completed", "failed"}


def has_attempt_since(data: dict, name: str, since: datetime, status: str | None = None) -> bool:
    hist = ((data.get("tasks") or {}).get(name) or {}).get("history") or []
    attempts = hist + [last_attempt(data, name)]
    for attempt in attempts:
        when = parse_dt(attempt.get("finished_at")) or parse_dt(attempt.get("started_at"))
        if when and when >= since and (status is None or attempt.get("status") == status):
            if status is None and attempt.get("status") not in EXECUTED_TERMINAL_STATUSES:
                continue
            return True
    return False


def last_executed_attempt(data: dict, name: str) -> dict:
    hist = ((data.get("tasks") or {}).get(name) or {}).get("history") or []
    attempts = hist + [last_attempt(data, name)]
    executed = [a for a in attempts if a.get("status") in EXECUTED_TERMINAL_STATUSES]
    if not executed:
        return {}
    return max(executed, key=lambda a: parse_dt(a.get("finished_at")) or parse_dt(a.get("started_at")) or datetime.min.replace(tzinfo=BA_TZ))


def has_daily_terminal_attempt(data: dict, name: str, now: datetime) -> bool:
    """Return True once non-JobSeeker tasks already executed today in BA time.

    JobSeeker is special: it retries every 4 hours until DOD or a safety stop,
    then remains blocked only until the next BA midnight.
    """
    if name == "jobseeker":
        return is_jobseeker_blocked_until_midnight(data, now)
    local = now.astimezone(BA_TZ)
    day_start = datetime.combine(local.date(), dtime.min, BA_TZ)
    return has_attempt_since(data, name, day_start)


def next_ba_midnight(now: datetime) -> datetime:
    local = now.astimezone(BA_TZ)
    return datetime.combine(local.date() + timedelta(days=1), dtime.min, BA_TZ)


def is_jobseeker_blocked_until_midnight(data: dict, now: datetime) -> bool:
    attempt = last_executed_attempt(data, "jobseeker")
    blocked_until = parse_dt(attempt.get("blocked_until"))
    return bool(blocked_until and now.astimezone(BA_TZ) < blocked_until)


def parse_jobseeker_stdout(stdout: str) -> dict:
    """Extract DOD metadata from JobSeeker JSONL stdout."""
    summary: dict = {}
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("event") in {"quota_done", "quota_stopped"}:
            summary.update({k: event[k] for k in ("target", "new_submissions", "submitted_today_total", "dod_reached") if k in event})
    return summary


def jobseeker_attempt_extra(rc: int, stdout: str, finished: datetime) -> dict:
    meta = parse_jobseeker_stdout(stdout)
    extra: dict = {}
    if "submitted_today_total" in meta:
        extra["submitted_today_total"] = meta.get("submitted_today_total")
    if "target" in meta:
        extra["target"] = meta.get("target")
    dod_reached = bool(meta.get("dod_reached"))
    if not dod_reached:
        try:
            dod_reached = int(meta.get("submitted_today_total", -1)) >= int(meta.get("target", 10**9))
        except Exception:
            dod_reached = False
    if dod_reached:
        extra["dod_reached"] = True
        extra["blocked_until"] = iso(next_ba_midnight(finished))
    if rc in SAFETY_STOP_EXIT_CODES:
        extra["blocked_until"] = iso(next_ba_midnight(finished))
    return extra


def is_jobseeker_due(now: datetime, data: dict) -> bool:
    local = now.astimezone(BA_TZ)
    if is_jobseeker_blocked_until_midnight(data, local):
        return False
    attempt = last_executed_attempt(data, "jobseeker")
    if not attempt:
        return True
    finished = parse_dt(attempt.get("finished_at")) or parse_dt(attempt.get("started_at"))
    if not finished:
        return True
    return local >= finished + JOBSEEKER_RETRY_INTERVAL


def start_of_iso_week(now: datetime) -> datetime:
    local = now.astimezone(BA_TZ)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime.combine(monday, dtime.min, BA_TZ)


def is_due(name: str, now: datetime, data: dict) -> bool:
    local = now.astimezone(BA_TZ)
    if has_daily_terminal_attempt(data, name, local):
        return False
    if name == "postliker":
        due_at = today_at(local, 6, 0)
        return local >= due_at
    if name == "sendman":
        due_at = today_at(local, 9, 0)
        return local >= due_at
    if name == "connectman":
        week_start = start_of_iso_week(local)
        if has_completed_since(data, name, week_start):
            return False
        monday = week_start + timedelta(hours=11)
        wednesday = week_start + timedelta(days=2, hours=11)
        if local.weekday() == 0:
            return local >= monday and not has_attempt_since(data, name, week_start)
        if local.weekday() >= 2:
            return local >= wednesday
        return False
    if name == "jobseeker":
        return is_jobseeker_due(local, data)
    return False


def plan_due(now: datetime, data: dict, include_all: bool = False) -> list[Task]:
    if include_all:
        return [TASKS[n] for n in RUN_ALL_ORDER if not has_daily_terminal_attempt(data, n, now)]
    fixed = [TASKS[n] for n in ("postliker", "connectman", "sendman") if is_due(n, now, data)]
    interval = [TASKS["jobseeker"]] if is_due("jobseeker", now, data) else []
    return sorted(fixed, key=lambda t: t.priority) + interval


def append_history(task_state: dict, attempt: dict) -> None:
    if attempt:
        hist = task_state.setdefault("history", [])
        hist.append(attempt)
        del hist[:-50]


def mark_attempt_started(data: dict, name: str, now: datetime, command: Sequence[str]) -> dict:
    tasks = data.setdefault("tasks", {})
    task_state = tasks.setdefault(name, {})
    previous = task_state.get("last_attempt")
    if previous and previous.get("status") != "running":
        append_history(task_state, previous)
    task_state["last_attempt"] = {"status": "running", "started_at": iso(now), "command": list(command)}
    task_state["updated_at"] = iso(now)
    return data


def finish_attempt(data: dict, name: str, now: datetime, status: str, exit_code: int | None, command: Sequence[str], stdout: str = "", stderr: str = "", **extra) -> dict:
    task_state = data.setdefault("tasks", {}).setdefault(name, {})
    attempt = task_state.get("last_attempt") or {}
    if attempt.get("status") != "running":
        append_history(task_state, attempt)
        attempt = {"started_at": iso(now), "command": list(command)}
    attempt.update({
        "status": status,
        "finished_at": iso(now),
        "exit_code": exit_code,
        "command": list(command),
        "stdout_tail": stdout[-4000:],
        "stderr_tail": stderr[-4000:],
    })
    attempt.update(extra)
    task_state["last_attempt"] = attempt
    task_state["updated_at"] = iso(now)
    return data


def mark_attempt_completed(data: dict, name: str, now: datetime, exit_code: int, command: Sequence[str]) -> dict:
    return finish_attempt(data, name, now, "completed", exit_code, command)


def recover_stale_running(data: dict, now: datetime) -> dict:
    changed = False
    for name, task_state in (data.get("tasks") or {}).items():
        attempt = task_state.get("last_attempt") or {}
        if attempt.get("status") == "running":
            attempt = dict(attempt)
            attempt.update({"status": "interrupted", "interrupted_at": iso(now), "finished_at": iso(now), "exit_code": None, "reason": "stale_running_recovered"})
            task_state["last_attempt"] = attempt
            task_state["updated_at"] = iso(now)
            changed = True
    if changed:
        data["updated_at"] = iso(now)
    return data


def _signal_process_group(proc: subprocess.Popen, sig: signal.Signals) -> None:
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except OSError:
        action = proc.terminate if sig == signal.SIGTERM else proc.kill
        try:
            action()
        except ProcessLookupError:
            pass


def _terminate_process_group(proc: subprocess.Popen) -> None:
    _signal_process_group(proc, signal.SIGTERM)
    try:
        proc.communicate(timeout=PROCESS_GROUP_TERM_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGKILL)
        proc.communicate()


def cleanup_timed_out_container_task(task: Task, cwd: Path) -> str:
    """Best-effort cleanup for docker compose exec children left after host timeout.

    Killing the host-side docker compose exec process group is not enough: the
    in-container Python/Playwright child can survive, keep the shared LinkedIn
    runtime lock, and starve later services such as JobSeeker. Keep patterns
    limited to worker scripts and Playwright driver, never the browser owner.
    """
    patterns = (
        "[/]app/scripts/run_unlocked.sh",
        "[l]inkedin_runtime_lock.py",
        "[l]inkedin_message_outreach.py",
        "[l]inkedin_worker.py",
        "[l]inkedin_post_liker.py",
        "[l]inkedin_outreach.py",
        "[l]inkedin_connect",
        "[p]laywright/driver",
    )
    pattern = "|".join(patterns)
    shell = f"pkill -TERM -f '{pattern}' 2>/dev/null || true; sleep 3; pkill -KILL -f '{pattern}' 2>/dev/null || true"
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", task.service, "sh", "-lc", shell],
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        details = (result.stdout or "") + (result.stderr or "")
        return details.strip()
    except Exception as exc:
        return f"cleanup_failed:{exc!r}"


def default_runner(task: Task, command: Sequence[str], env: dict[str, str], cwd: Path) -> TaskRunResult:
    child_env = os.environ.copy()
    child_env.update(env)
    proc = subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=child_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timeout_seconds = task.timeout_seconds()
    try:
        stdout, stderr = proc.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        cleanup_details = cleanup_timed_out_container_task(task, cwd)
        stderr = "task_timeout"
        if cleanup_details:
            stderr += f"\ncontainer_cleanup: {cleanup_details}"
        return TaskRunResult(
            exit_code=TASK_TIMEOUT_EXIT_CODE,
            stderr=stderr,
            reason="task_timeout",
            timeout_seconds=timeout_seconds,
        )
    return TaskRunResult(proc.returncode, stdout or "", stderr or "")


def normalize_runner_result(result: TaskRunResult | tuple[int, str, str]) -> TaskRunResult:
    if isinstance(result, TaskRunResult):
        return result
    rc, stdout, stderr = result
    return TaskRunResult(rc, stdout, stderr)


def log_attempt(log_dir: Path, now: datetime, task: Task, command: Sequence[str], rc: int, stdout: str, stderr: str) -> None:
    daily = Path(log_dir) / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    path = daily / f"{now.astimezone(BA_TZ).date().isoformat()}.log"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"[{iso(now)}] task={task.name} command={json.dumps(list(command))} exit={rc}\n")
        if stdout:
            fh.write(stdout.rstrip() + "\n")
        if stderr:
            fh.write(stderr.rstrip() + "\n")


def is_lock_skip(stdout: str, stderr: str) -> bool:
    text = f"{stdout}\n{stderr}".lower()
    return all(part in text for part in LOCK_SKIP_REASONS)


def run_queue(action: str, now: datetime, state: SchedulerState, log_dir: Path, runner: Callable = default_runner, root: Path = ROOT) -> RunResult:
    data = state.load()
    include_all = action == "run-all"
    queue = plan_due(now, data, include_all=include_all)
    attempted: list[str] = []
    max_exit = 0
    for task in queue:
        command = task.command()
        data = mark_attempt_started(data, task.name, now, command)
        state.write(data)
        task_result = normalize_runner_result(runner(task, command, task.env(), root))
        rc, stdout, stderr = task_result.exit_code, task_result.stdout, task_result.stderr
        finished = datetime.now(BA_TZ)
        log_attempt(log_dir, now, task, command, rc, stdout, stderr)
        attempted.append(task.name)
        if rc == 0 and is_lock_skip(stdout, stderr):
            data = finish_attempt(data, task.name, finished, "skipped", 0, command, stdout, stderr, reason="scheduler_lock_skip")
            state.write(data)
            return RunResult(exit_code=75, attempted=attempted, stopped=True)
        elif rc == 0:
            extra = jobseeker_attempt_extra(rc, stdout, finished) if task.name == "jobseeker" else {}
            data = finish_attempt(data, task.name, finished, "completed", 0, command, stdout, stderr, **extra)
        else:
            extra = {"stop": f"exit_{rc}"} if rc in SAFETY_STOP_EXIT_CODES else {}
            if task.name == "jobseeker":
                extra.update(jobseeker_attempt_extra(rc, stdout, finished))
            if task_result.reason == "task_timeout":
                extra.update(reason="task_timeout", timeout_seconds=task_result.timeout_seconds)
            data = finish_attempt(data, task.name, finished, "failed", rc, command, stdout, stderr, **extra)
            max_exit = rc
        state.write(data)
        if rc in SAFETY_STOP_EXIT_CODES:
            return RunResult(exit_code=rc, attempted=attempted, stopped=True)
        if rc != 0 and max_exit == 0:
            max_exit = rc
    return RunResult(exit_code=max_exit, attempted=attempted)


class HostLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = self.path.open("a+")
        fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.fh.seek(0)
        self.fh.truncate()
        self.fh.write(json.dumps({"pid": os.getpid(), "locked_at": iso(datetime.now(BA_TZ))}))
        self.fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.fh:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


def lock_status(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return {"locked": False, "path": str(path)}
        except BlockingIOError:
            return {"locked": True, "path": str(path)}


def status_payload(state: SchedulerState, lock_path: Path) -> dict:
    data = state.load()
    tasks = {}
    for name, task in TASKS.items():
        tasks[name] = {"service": task.service, "kind": task.kind, "schedule": schedule_label(name), "last_attempt": last_attempt(data, name)}
    return {"timezone": "America/Argentina/Buenos_Aires", "lock": lock_status(lock_path), "state_path": str(state.path), "tasks": tasks}


def main(argv: list[str] | None = None) -> int:
    load_scheduler_timeout_env(ROOT / ".env")
    ap = argparse.ArgumentParser(description="Central host-side LinkedIn scheduler")
    ap.add_argument("action", choices=["plan", "run-due", "run-all", "status"])
    ap.add_argument("--state", default=str(DEFAULT_STATE))
    ap.add_argument("--logs", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--lock", default=str(DEFAULT_LOCK))
    ap.add_argument("--now", default="")
    args = ap.parse_args(argv)

    state = SchedulerState(Path(args.state))
    lock_path = Path(args.lock)
    now = parse_dt(args.now) if args.now else datetime.now(BA_TZ)
    assert now is not None

    if args.action == "status":
        print(json.dumps(status_payload(state, lock_path), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.action == "plan":
        data = state.load()
        due = plan_due(now, data, include_all=False)
        print(json.dumps({"now": iso(now), "due": [t.name for t in due]}, ensure_ascii=False, indent=2))
        return 0

    try:
        with HostLock(lock_path):
            data = state.load()
            if args.action in {"run-due", "run-all"}:
                data = recover_stale_running(data, now)
                state.write(data)
            result = run_queue(args.action, now, state, Path(args.logs), root=ROOT)
            print(json.dumps({"attempted": result.attempted, "exit_code": result.exit_code, "stopped": result.stopped}, ensure_ascii=False))
            return result.exit_code
    except BlockingIOError:
        print("scheduler host lock is busy; queue not started", file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
