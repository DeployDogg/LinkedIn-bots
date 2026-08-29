from __future__ import annotations

import importlib.util
import fcntl
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEDULER_PATH = ROOT / "scripts" / "linkedin_scheduler.py"
BA = "America/Argentina/Buenos_Aires"


def load_scheduler():
    spec = importlib.util.spec_from_file_location("linkedin_scheduler", SCHEDULER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def dt(value: str):
    module = load_scheduler()
    return datetime.fromisoformat(value).replace(tzinfo=module.BA_TZ)


class RecordingRunner:
    def __init__(self, codes=None):
        self.codes = list(codes or [])
        self.calls = []

    def __call__(self, task, command, env, cwd):
        self.calls.append((task.name, list(command), dict(env or {}), str(cwd)))
        code = self.codes.pop(0) if self.codes else 0
        return code, f"out-{task.name}", f"err-{task.name}"


def test_plan_due_starts_with_active_tasks_when_all_tasks_are_due(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    now = dt("2026-07-29T11:20:00")  # Wednesday

    due = module.plan_due(now, state.load(), include_all=False)

    assert [item.name for item in due] == ["postliker", "connectman", "sendman", "jobseeker"]


def test_plan_due_keeps_previous_order_after_postliker_completed_today(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    now = dt("2026-07-29T11:20:00")  # Wednesday

    state.write(module.mark_attempt_completed(state.load(), "postliker", now, exit_code=0, command=["x"]))
    due = module.plan_due(now, state.load(), include_all=False)

    assert [item.name for item in due] == ["connectman", "sendman", "jobseeker"]

    state.write(module.mark_attempt_completed(state.load(), "connectman", now, exit_code=0, command=["x"]))
    assert "connectman" not in [item.name for item in module.plan_due(now, state.load(), include_all=False)]


def test_jobseeker_retries_every_4_hours_until_dod(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    failed = dt("2026-07-28T11:07:00")
    state.write(module.finish_attempt(state.load(), "jobseeker", failed, "failed", 70, ["x"]))

    assert "jobseeker" not in [item.name for item in module.plan_due(dt("2026-07-28T15:06:00"), state.load(), include_all=False)]
    assert "jobseeker" in [item.name for item in module.plan_due(dt("2026-07-28T15:07:00"), state.load(), include_all=False)]
    assert "jobseeker" in [item.name for item in module.plan_due(dt("2026-07-28T23:59:00"), state.load(), include_all=True)]


def test_jobseeker_dod_or_safety_stop_blocks_until_next_ba_midnight(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    dod = dt("2026-07-28T11:07:00")
    data = module.finish_attempt(
        state.load(),
        "jobseeker",
        dod,
        "completed",
        0,
        ["x"],
        stdout=json.dumps({"event": "quota_done", "target": 60, "submitted_today_total": 60, "dod_reached": True}),
        dod_reached=True,
        blocked_until="2026-07-29T00:00:00-03:00",
    )
    state.write(data)

    assert "jobseeker" not in [item.name for item in module.plan_due(dt("2026-07-28T23:59:00"), state.load(), include_all=False)]
    assert "jobseeker" in [item.name for item in module.plan_due(dt("2026-07-29T00:00:00"), state.load(), include_all=False)]

    stopped = module.SchedulerState(tmp_path / "state-stop.json")
    data = module.finish_attempt(
        stopped.load(),
        "jobseeker",
        dt("2026-07-28T12:00:00"),
        "failed",
        12,
        ["x"],
        stop="exit_12",
        blocked_until="2026-07-29T00:00:00-03:00",
    )
    stopped.write(data)
    assert "jobseeker" not in [item.name for item in module.plan_due(dt("2026-07-28T20:00:00"), stopped.load(), include_all=False)]


def test_run_queue_marks_jobseeker_dod_from_stdout_and_blocks_until_midnight(tmp_path, monkeypatch):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    monkeypatch.setattr(module, "next_ba_midnight", lambda finished: dt("2026-07-29T00:00:00"))

    def runner(task, command, env, cwd):
        assert task.name == "jobseeker"
        return module.TaskRunResult(
            0,
            stdout=json.dumps({
                "event": "quota_done",
                "target": 60,
                "new_submissions": 4,
                "submitted_today_total": 60,
                "dod_reached": True,
                "exit_code": 0,
            }),
        )

    result = module.run_queue(
        action="run-due",
        now=dt("2026-07-28T01:00:00"),
        state=state,
        log_dir=tmp_path / "logs",
        runner=runner,
        root=ROOT,
    )

    assert result.exit_code == 0
    attempt = state.load()["tasks"]["jobseeker"]["last_attempt"]
    assert attempt["status"] == "completed"
    assert attempt["dod_reached"] is True
    assert attempt["submitted_today_total"] == 60
    assert attempt["blocked_until"] == "2026-07-29T00:00:00-03:00"


def test_run_queue_marks_jobseeker_safety_stop_block_until_midnight(tmp_path, monkeypatch):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    monkeypatch.setattr(module, "next_ba_midnight", lambda finished: dt("2026-07-29T00:00:00"))

    result = module.run_queue(
        action="run-due",
        now=dt("2026-07-28T01:00:00"),
        state=state,
        log_dir=tmp_path / "logs",
        runner=lambda task, command, env, cwd: module.TaskRunResult(12, stdout='{"event":"quota_stopped"}'),
        root=ROOT,
    )

    assert result.exit_code == 12
    attempt = state.load()["tasks"]["jobseeker"]["last_attempt"]
    assert attempt["status"] == "failed"
    assert attempt["stop"] == "exit_12"
    assert attempt["blocked_until"] == "2026-07-29T00:00:00-03:00"


def test_run_all_order_commands_env_and_safety_stop(tmp_path):
    module = load_scheduler()
    runner = RecordingRunner(codes=[0, 11, 0, 0])
    state_path = tmp_path / "state" / "state.json"
    log_dir = tmp_path / "logs"
    result = module.run_queue(
        action="run-all",
        now=dt("2026-07-28T12:00:00"),
        state=module.SchedulerState(state_path),
        log_dir=log_dir,
        runner=runner,
        root=ROOT,
    )

    assert result.exit_code == 11
    assert [call[0] for call in runner.calls] == ["postliker", "connectman"]
    assert runner.calls[0][1] == ["docker", "compose", "exec", "-T", "postliker", "/app/scripts/run.sh"]
    assert runner.calls[1][1] == ["docker", "compose", "exec", "-T", "connectman", "/app/scripts/run.sh"]
    assert all("LINKEDIN_WORKER_MAX_JOBS" not in call[2] for call in runner.calls)
    data = json.loads(state_path.read_text())
    assert data["tasks"]["connectman"]["last_attempt"]["status"] == "failed"
    assert data["tasks"]["connectman"]["last_attempt"]["stop"] == "exit_11"
    assert "sendman" not in data["tasks"]
    assert "out-connectman" in (log_dir / "daily" / "2026-07-28.log").read_text()


def test_postliker_timeout_is_persisted_and_does_not_block_following_tasks(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    calls = []

    def runner(task, command, env, cwd):
        calls.append(task.name)
        if task.name == "postliker":
            return module.TaskRunResult(
                exit_code=124,
                stderr="task_timeout",
                reason="task_timeout",
                timeout_seconds=task.timeout_seconds(),
            )
        return module.TaskRunResult(exit_code=0, stdout=f"out-{task.name}")

    result = module.run_queue(
        action="run-all",
        now=dt("2026-07-28T18:20:00"),
        state=state,
        log_dir=tmp_path / "logs",
        runner=runner,
        root=ROOT,
    )

    assert result.exit_code == 124
    assert result.stopped is False
    assert calls == ["postliker", "connectman", "sendman", "jobseeker"]
    timeout_attempt = state.load()["tasks"]["postliker"]["last_attempt"]
    assert timeout_attempt["status"] == "failed"
    assert timeout_attempt["reason"] == "task_timeout"
    assert timeout_attempt["timeout_seconds"] == module.TASKS["postliker"].timeout_seconds()
    assert state.load()["tasks"]["connectman"]["last_attempt"]["status"] == "completed"


def test_postliker_safety_exit_stops_queue(tmp_path):
    module = load_scheduler()

    for safety_exit in (11, 12):
        runner = RecordingRunner(codes=[safety_exit, 0, 0, 0])
        state = module.SchedulerState(tmp_path / f"state-{safety_exit}.json")
        result = module.run_queue(
            action="run-all",
            now=dt("2026-07-28T18:20:00"),
            state=state,
            log_dir=tmp_path / f"logs-{safety_exit}",
            runner=runner,
            root=ROOT,
        )

        assert result.exit_code == safety_exit
        assert result.stopped is True
        assert [call[0] for call in runner.calls] == ["postliker"]
        attempt = state.load()["tasks"]["postliker"]["last_attempt"]
        assert attempt["status"] == "failed"
        assert attempt["stop"] == f"exit_{safety_exit}"


def test_task_timeouts_have_documented_defaults_and_env_overrides(monkeypatch):
    module = load_scheduler()

    assert module.TASKS["postliker"].timeout_seconds() == 900
    monkeypatch.setenv("LINKEDIN_SCHEDULER_POSTLIKER_TIMEOUT_SECONDS", "321")
    assert module.TASKS["postliker"].timeout_seconds() == 321
    assert module.TASKS["jobseeker"].timeout_seconds() == 7200


def test_timeout_env_loader_imports_only_scheduler_timeout_keys(tmp_path, monkeypatch):
    module = load_scheduler()
    env_path = tmp_path / ".env"
    env_path.write_text(
        "LINKEDIN_SCHEDULER_POSTLIKER_TIMEOUT_SECONDS=123\n"
        "LINKEDIN_PASSWORD=must-not-enter-scheduler-env\n"
    )
    monkeypatch.delenv("LINKEDIN_SCHEDULER_POSTLIKER_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LINKEDIN_PASSWORD", raising=False)

    module.load_scheduler_timeout_env(env_path)

    assert module.TASKS["postliker"].timeout_seconds() == 123
    assert "LINKEDIN_PASSWORD" not in os.environ


def test_default_runner_timeout_kills_process_group_without_leaking_child_output(monkeypatch):
    module = load_scheduler()
    events = []

    class FakeProcess:
        pid = 4321

        def __init__(self):
            self.communicate_calls = 0

        def communicate(self, timeout=None):
            self.communicate_calls += 1
            events.append(("communicate", timeout))
            if self.communicate_calls < 3:
                raise subprocess.TimeoutExpired(["docker", "compose"], timeout, output="possible-secret")
            return "possible-secret", "possible-secret"

    def fake_popen(*args, **kwargs):
        events.append(("popen", args, kwargs))
        assert kwargs["start_new_session"] is True
        return FakeProcess()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: events.append(("killpg", pid, sig)))
    monkeypatch.setattr(module, "cleanup_timed_out_container_task", lambda task, cwd: "")
    monkeypatch.setenv("LINKEDIN_SCHEDULER_POSTLIKER_TIMEOUT_SECONDS", "1")

    result = module.default_runner(module.TASKS["postliker"], ["docker", "compose"], {}, ROOT)

    assert result == module.TaskRunResult(
        exit_code=124,
        stderr="task_timeout",
        reason="task_timeout",
        timeout_seconds=1,
    )
    assert ("killpg", 4321, signal.SIGTERM) in events
    assert ("killpg", 4321, signal.SIGKILL) in events
    assert "possible-secret" not in result.stdout
    assert "possible-secret" not in result.stderr


def test_run_due_jobseeker_injects_max_jobs_and_logs_lock_skip_as_skipped(tmp_path):
    module = load_scheduler()
    runner = RecordingRunner(codes=[0])
    state = module.SchedulerState(tmp_path / "state.json")
    module.run_queue(
        action="run-all",
        now=dt("2026-07-28T00:20:00"),
        state=state,
        log_dir=tmp_path / "logs",
        runner=lambda task, command, env, cwd: (
            (0, "busy lock=/shared/runtime/linkedin-workers.lock; child not started; exiting 0", "")
            if task.name == "jobseeker"
            else (0, f"out-{task.name}", "")
        ),
        root=ROOT,
    )
    data = state.load()
    assert data["tasks"]["jobseeker"]["last_attempt"]["status"] == "skipped"
    assert data["tasks"]["jobseeker"]["last_attempt"]["reason"] == "scheduler_lock_skip"

    module.run_queue(
        action="run-due",
        now=dt("2026-07-28T11:40:00"),
        state=module.SchedulerState(tmp_path / "state2.json"),
        log_dir=tmp_path / "logs2",
        runner=runner,
        root=ROOT,
    )
    jobseeker_call = [call for call in runner.calls if call[0] == "jobseeker"][0]
    assert jobseeker_call[1][4:9] == ["-e", "LINKEDIN_WORKER_MAX_JOBS=60", "-e", "LINKEDIN_JOBSEEKER_QUEUE_MODE=1", "jobseeker"]
    assert "LINKEDIN_WORKER_MAX_JOBS" not in jobseeker_call[2]


def test_jobseeker_cap_is_passed_inside_docker_exec_command_before_service():
    module = load_scheduler()

    command = module.TASKS["jobseeker"].command()

    assert command == [
        "docker",
        "compose",
        "exec",
        "-T",
        "-e",
        "LINKEDIN_WORKER_MAX_JOBS=60",
        "-e",
        "LINKEDIN_JOBSEEKER_QUEUE_MODE=1",
        "jobseeker",
        "/app/scripts/run.sh",
    ]


def test_status_does_not_recover_running_attempt_while_host_lock_is_held(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    lock_path = tmp_path / "scheduler.lock"
    raw = state.load()
    raw["tasks"] = {
        "sendman": {
            "last_attempt": {
                "status": "running",
                "started_at": "2026-07-28T09:00:00-03:00",
                "command": ["docker", "compose"],
            }
        }
    }
    state.write(raw)

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        payload = module.status_payload(state, lock_path)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    assert payload["lock"]["locked"] is True
    assert state.load()["tasks"]["sendman"]["last_attempt"]["status"] == "running"


def test_run_due_recovers_running_attempt_only_after_host_lock_is_proven_free(tmp_path):
    module = load_scheduler()
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "scheduler.lock"
    state = module.SchedulerState(state_path)
    raw = state.load()
    raw["tasks"] = {
        "sendman": {
            "last_attempt": {
                "status": "running",
                "started_at": "2026-07-28T09:00:00-03:00",
                "command": ["docker", "compose"],
            }
        }
    }
    state.write(raw)

    with module.HostLock(lock_path):
        locked_data = module.recover_stale_running(state.load(), dt("2026-07-28T09:01:00"))
        state.write(locked_data)
        result = module.run_queue(
            action="run-due",
            now=dt("2026-07-28T09:01:00"),
            state=state,
            log_dir=tmp_path / "logs",
            runner=RecordingRunner(),
            root=ROOT,
        )

    assert result.exit_code == 0
    data = state.load()
    assert data["tasks"]["sendman"]["history"][0]["status"] == "interrupted"


def test_lock_skip_returns_exit75_stops_queue_and_remains_due(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    calls = []

    def runner(task, command, env, cwd):
        calls.append(task.name)
        if task.name == "postliker":
            return 0, "busy lock=/shared/runtime/linkedin-workers.lock; child not started; exiting 0", ""
        return 0, f"out-{task.name}", ""

    result = module.run_queue(
        action="run-due",
        now=dt("2026-07-28T18:20:00"),
        state=state,
        log_dir=tmp_path / "logs",
        runner=runner,
        root=ROOT,
    )

    assert result.exit_code == 75
    assert result.stopped is True
    assert calls == ["postliker"]
    data = state.load()
    assert data["tasks"]["postliker"]["last_attempt"]["status"] == "skipped"
    assert data["tasks"]["postliker"]["last_attempt"]["reason"] == "scheduler_lock_skip"
    assert "postliker" in [item.name for item in module.plan_due(dt("2026-07-28T18:21:00"), data, include_all=False)]


def test_run_due_planning_counts_failed_but_not_interrupted_or_lock_skip_attempts(tmp_path):
    module = load_scheduler()
    now = dt("2026-07-28T18:20:00")
    state = module.SchedulerState(tmp_path / "state.json")
    data = state.load()
    data = module.finish_attempt(data, "postliker", now, "skipped", 0, ["x"], "busy lock child not started", "", reason="scheduler_lock_skip")
    data = module.finish_attempt(data, "sendman", dt("2026-07-28T09:01:00"), "interrupted", None, ["x"], reason="stale_running_recovered")
    data = module.finish_attempt(data, "commentator", dt("2026-07-28T18:17:30"), "failed", 2, ["x"])
    data = module.finish_attempt(data, "jobseeker", dt("2026-07-28T18:05:00"), "skipped", 0, ["x"], "busy lock child not started", "", reason="scheduler_lock_skip")
    state.write(data)

    due = [item.name for item in module.plan_due(now, state.load(), include_all=False)]

    assert "postliker" in due
    assert "sendman" in due
    assert "commentator" not in due
    assert "jobseeker" in due


def test_cli_plan_is_read_only_and_does_not_recover_running_or_touch_lock_file(tmp_path, capsys):
    module = load_scheduler()
    state_path = tmp_path / "state.json"
    lock_path = tmp_path / "scheduler.lock"
    state = module.SchedulerState(state_path)
    raw = state.load()
    raw["tasks"] = {
        "sendman": {
            "last_attempt": {
                "status": "running",
                "started_at": "2026-07-28T09:00:00-03:00",
                "command": ["docker", "compose"],
            }
        }
    }
    state.write(raw)
    before_state = state_path.read_bytes()

    rc = module.main([
        "plan",
        "--state",
        str(state_path),
        "--logs",
        str(tmp_path / "logs"),
        "--lock",
        str(lock_path),
        "--now",
        "2026-07-28T11:01:00-03:00",
    ])

    assert rc == 0
    due = json.loads(capsys.readouterr().out)["due"]
    assert "sendman" in due
    assert "jobseeker" in due
    assert state_path.read_bytes() == before_state
    assert state.load()["tasks"]["sendman"]["last_attempt"]["status"] == "running"
    assert not lock_path.exists()


def test_stale_running_is_recovered_as_interrupted_and_eligible(tmp_path):
    module = load_scheduler()
    state = module.SchedulerState(tmp_path / "state.json")
    raw = state.load()
    raw["tasks"] = {
        "sendman": {
            "last_attempt": {
                "status": "running",
                "started_at": "2026-07-27T09:00:00-03:00",
                "command": ["docker", "compose"],
            }
        }
    }
    state.write(raw)

    recovered = module.recover_stale_running(state.load(), now=dt("2026-07-28T09:01:00"))

    assert recovered["tasks"]["sendman"]["last_attempt"]["status"] == "interrupted"
    assert "sendman" in [item.name for item in module.plan_due(dt("2026-07-28T09:01:00"), recovered, include_all=False)]


def test_cli_status_returns_json_without_running_workers(tmp_path, capsys):
    module = load_scheduler()
    rc = module.main(["status", "--state", str(tmp_path / "state.json"), "--logs", str(tmp_path / "logs")])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["timezone"] == BA
    assert out["tasks"]["jobseeker"]["kind"] == "interval"
