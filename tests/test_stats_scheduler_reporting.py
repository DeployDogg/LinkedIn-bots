from __future__ import annotations

import importlib.util
import fcntl
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
BOT_PATH = ROOT / "scripts" / "hermes_container_status_bot.py"
BA = ZoneInfo("America/Argentina/Buenos_Aires")


def load_bot():
    spec = importlib.util.spec_from_file_location("hermes_container_status_bot", BOT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def test_central_mode_schedule_labels_use_scheduler_source_of_truth_without_container_crontab(monkeypatch):
    bot = load_bot()
    calls = []

    def fake_run(cmd, cwd=ROOT, timeout=25):
        calls.append(cmd)
        assert cmd[:4] == ["docker", "compose", "exec", "-T"]
        service = cmd[4]
        return 0, f"mode=0 safe=0 central=1\n# stale internal crontab must be ignored\n0,20,40 * * * * /app/scripts/run.sh\nservice={service}"

    monkeypatch.setattr(bot, "run", fake_run)
    expected = {
        "jobseeker": "every 4h until DOD/block, then block until 00:00 BA",
        "postliker": "daily 06:00 max once/day",
        "sendman": "daily09:00 max once/day",
        "connectman": "Mon11 + Wed11 fallback max once/day",
    }

    for service, label in expected.items():
        mode, safe, cron = bot.container_cron_and_mode(service, {service: {"State": "running"}})
        assert (mode, safe, cron) == ("central", "0", label)
        assert bot.human_cron(cron) == label

    assert len(calls) == len(expected)


def test_scheduler_status_reports_real_host_flock_and_is_read_only(tmp_path, monkeypatch):
    bot = load_bot()
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    state_path = tmp_path / "shared" / "state" / "LinkedInScheduler" / "state.json"
    lock_path = tmp_path / "shared" / "state" / "LinkedInScheduler" / "scheduler.lock"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"tasks": {"sendman": {"last_attempt": {"status": "running"}}}}, sort_keys=True))
    before = state_path.read_bytes()

    with lock_path.open("a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        status = bot.scheduler_status()
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    assert status["lock"]["locked"] is True
    assert state_path.read_bytes() == before
    assert status["tasks"]["sendman"]["last_attempt"]["status"] == "running"


def test_scheduler_lock_skip_is_classified_separately_from_success_and_err0(tmp_path, monkeypatch):
    bot = load_bot()
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    start = datetime.combine(datetime(2026, 7, 28).date(), time.min, BA)
    end = datetime.combine(datetime(2026, 7, 28).date(), time.max, BA)
    log_dir = tmp_path / "shared" / "logs" / "JobSeeker" / "daily"
    log_dir.mkdir(parents=True)
    (log_dir / "2026-07-28.log").write_text("busy lock=/shared/runtime/linkedin-workers.lock; child not started; exiting 0\n")

    stats = bot.jobseeker_stats(start, end, {"jobseeker": {"State": "running", "Health": "healthy"}})

    assert stats["scheduler_lock_skips"] == 1
    assert stats["successful_runs"] == 0
    assert stats["errors"] == 0


def test_format_message_uses_explicit_runtime_counts_modes_and_scheduler_status(monkeypatch):
    bot = load_bot()
    scheduler_status = {
        "timezone": "America/Argentina/Buenos_Aires",
        "lock": {"locked": False},
        "tasks": {
            "postliker": {"last_attempt": {"status": "completed", "finished_at": "2026-07-28T06:01:00-03:00", "exit_code": 0}},
            "jobseeker": {"last_attempt": {"status": "skipped", "reason": "scheduler_lock_skip", "finished_at": "2026-07-28T10:20:00-03:00", "exit_code": 0}},
        },
    }
    monkeypatch.setattr(bot, "docker_ps", lambda: {
        "jobseeker": {"State": "running", "Health": "healthy"},
        "connectman": {"State": "running", "Health": "healthy"},
        "sendman": {"State": "exited"},
        "postliker": {"State": "running", "Health": "unhealthy"},
    })
    monkeypatch.setattr(bot, "read_dotenv", lambda: {"SAFE_MODE": "0", "CRON_TEST_MODE": "0"})
    monkeypatch.setattr(bot, "container_cron_and_mode", lambda svc, ps: ("central", "0", bot.scheduler_schedule_label(svc)))
    monkeypatch.setattr(bot, "jobseeker_stats", lambda start, end, ps: {"health": "healthy", "applications": 0, "blocked_by_question": 0, "skipped": 0, "errors": 0, "successful_runs": 0, "scheduler_lock_skips": 1, "blockers": "none"})
    monkeypatch.setattr(bot, "connectman_stats", lambda start, end, ps: {"health": "healthy", "sent": 0, "skipped": 0, "errors": 0, "limit": "unknown", "blockers": "none"})
    monkeypatch.setattr(bot, "sendman_stats", lambda start, end, ps: {"health": "exited", "sent": 0, "skipped": 0, "errors": 0, "safeguard": "no", "rate_limit": "no", "inmail": "ok/unknown", "blockers": "none"})
    monkeypatch.setattr(bot, "postliker_stats", lambda start, end, ps: {"health": "unhealthy", "liked": 0, "verified": 0, "skipped": 0, "errors": 0, "blockers": "none"})
    monkeypatch.setattr(bot, "scheduler_status", lambda: scheduler_status)

    msg = bot.format_message("daily")

    assert "containers=0/0" not in msg
    assert "runtime healthy 2/4" in msg
    assert "mode central" in msg
    assert "Scheduler · lock free" in msg
    assert "postliker completed exit0" in msg
    assert "jobseeker skipped scheduler_lock_skip" in msg
    assert "no useful actions" in msg
    assert "JobSeeker · daily 11:00 max once/day" in msg
    assert "ConnectMan · Mon11 + Wed11 fallback max once/day" in msg
    assert "SendMan · daily09:00 max once/day" in msg
    assert "PostLiker · daily 06:00 max once/day" in msg
    assert "Commentator" not in msg


def test_daily_persist_writes_hermesstats_daily_ledger(tmp_path, monkeypatch):
    bot = load_bot()
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "docker_ps", lambda: {})
    monkeypatch.setattr(bot, "container_cron_and_mode", lambda svc, ps: ("central", "0", bot.scheduler_schedule_label(svc)))
    monkeypatch.setattr(bot, "read_dotenv", lambda: {"SAFE_MODE": "0", "CRON_TEST_MODE": "0"})
    monkeypatch.setattr(bot, "scheduler_status", lambda: {"lock": {"locked": False}, "tasks": {}})
    monkeypatch.setattr(bot, "jobseeker_stats", lambda start, end, ps: {"health": "healthy", "applications": 2, "blocked_by_question": 1, "skipped": 3, "errors": 0, "successful_runs": 1, "scheduler_lock_skips": 0, "blockers": "questions×1"})
    monkeypatch.setattr(bot, "connectman_stats", lambda start, end, ps: {"health": "healthy", "sent": 4, "skipped": 5, "errors": 0, "limit": "no", "blockers": "none"})
    monkeypatch.setattr(bot, "sendman_stats", lambda start, end, ps: {"health": "healthy", "sent": 6, "skipped": 7, "errors": 0, "safeguard": "no", "rate_limit": "no", "inmail": "ok/unknown", "blockers": "none"})
    monkeypatch.setattr(bot, "postliker_stats", lambda start, end, ps: {"health": "healthy", "liked": 8, "verified": 8, "skipped": 9, "errors": 0, "blockers": "none"})

    msg = bot.format_message("daily", persist=True)

    ledger = tmp_path / "shared" / "logs" / "HermesStats" / "daily"
    files = list(ledger.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert data["kind"] == "daily"
    assert data["services"]["jobseeker"]["applications"] == 2
    assert "app 2" in msg


def test_weekly_report_sums_daily_ledger_files(tmp_path, monkeypatch):
    bot = load_bot()
    monkeypatch.setattr(bot, "ROOT", tmp_path)
    monkeypatch.setattr(bot, "docker_ps", lambda: {})
    base = tmp_path / "shared" / "logs" / "HermesStats" / "daily"
    base.mkdir(parents=True)
    for day, app, sent, msg_count, likes in [
        ("2026-07-27", 1, 2, 3, 4),
        ("2026-07-28", 10, 20, 30, 40),
    ]:
        payload = {
            "kind": "daily",
            "report_date": day,
            "label": day,
            "window": {"start": f"{day}T00:00:00-03:00", "end": f"{day}T23:59:59-03:00"},
            "env": {"SAFE_MODE": "0", "CRON_TEST_MODE": "0"},
            "modes": {svc: ["central", "0"] for svc in bot.SERVICES},
            "crons": {svc: bot.scheduler_schedule_label(svc) for svc in bot.SERVICES},
            "scheduler": {"lock": {"locked": False}, "tasks": {}},
            "services": {
                "jobseeker": {"health": "healthy", "applications": app, "blocked_by_question": 0, "skipped": 0, "errors": 0, "successful_runs": 1, "scheduler_lock_skips": 0, "blockers": "none"},
                "connectman": {"health": "healthy", "sent": sent, "skipped": 0, "errors": 0, "limit": "no", "blockers": "none"},
                "sendman": {"health": "healthy", "sent": msg_count, "skipped": 0, "errors": 0, "safeguard": "no", "rate_limit": "no", "inmail": "ok/unknown", "blockers": "none"},
                "postliker": {"health": "healthy", "liked": likes, "verified": likes, "skipped": 0, "errors": 0, "blockers": "none"},
            },
        }
        (base / f"{day}.json").write_text(json.dumps(payload))

    now = datetime(2026, 8, 3, 9, 30, tzinfo=BA)
    summary = bot.build_weekly_summary(now=now, materialize_missing=False)
    msg = bot.format_summary(summary)

    assert summary["included_daily_logs"] == ["2026-07-27", "2026-07-28"]
    assert "source: daily logs 2/7" in msg
    assert "app 11" in msg
    assert "sent 22" in msg
    assert "msg 33" in msg
    assert "verified 44/44" in msg
