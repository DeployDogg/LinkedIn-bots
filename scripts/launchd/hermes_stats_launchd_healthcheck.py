#!/usr/bin/env python3
"""No-LLM launchd healthcheck for Hermes Stats daily/weekly jobs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path("/Users/deploydog-ai/LinkedIn")
STATE_DIR = ROOT / "shared" / "state" / "HermesStats"
LOG_DIR = ROOT / "shared" / "logs" / "HermesStats" / "launchd"
HEALTH_LOG = LOG_DIR / "healthcheck.log"
LATEST_JSON = STATE_DIR / "healthcheck_last.json"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
TZ_NAME = "America/Argentina/Buenos_Aires"
TZ = ZoneInfo(TZ_NAME)
DOMAIN = f"gui/{os.getuid()}"

JOBS = {
    "daily": {
        "label": "ai.linkedin.hermes-stats.daily",
        "state_file": STATE_DIR / "last_daily.json",
        "max_age": timedelta(hours=36),
    },
    "weekly": {
        "label": "ai.linkedin.hermes-stats.weekly",
        "state_file": STATE_DIR / "last_weekly.json",
        "max_age": timedelta(days=8),
    },
}


@dataclass
class CheckResult:
    name: str
    ok: bool = True
    details: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)


def parse_ba_timestamp(raw: Any, field_name: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field_name} missing or not a string")
    value = raw.strip()
    # Wrapper writes YYYY-mm-ddTHH:MM:SS-0300. fromisoformat also accepts +HH:MM.
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError as exc:
            raise ValueError(f"{field_name} invalid ISO timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise ValueError(f"{field_name} has no timezone: {value!r}")
    ba_dt = dt.astimezone(TZ)
    expected_offset = ba_dt.utcoffset()
    if dt.utcoffset() != expected_offset:
        raise ValueError(
            f"{field_name} timezone offset {dt.utcoffset()} does not match {TZ_NAME} offset {expected_offset}"
        )
    return ba_dt


def read_status(period: str, cfg: dict[str, Any], now: datetime) -> CheckResult:
    result = CheckResult(period)
    path: Path = cfg["state_file"]
    result.details["state_file"] = str(path)

    if not path.exists():
        result.fail(f"missing state file: {path}")
        return result

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - report exact validation problem
        result.fail(f"invalid JSON in {path}: {exc}")
        return result

    result.details["period"] = data.get("period")
    result.details["status"] = data.get("status")
    result.details["exit_code"] = data.get("exit_code")

    if data.get("period") != period:
        result.fail(f"period mismatch: expected {period!r}, got {data.get('period')!r}")
    if data.get("status") != "ok":
        result.fail(f"status is not ok: {data.get('status')!r}")
    if data.get("exit_code") != 0:
        result.fail(f"exit_code is not 0: {data.get('exit_code')!r}")

    try:
        started_at = parse_ba_timestamp(data.get("started_at"), "started_at")
        finished_at = parse_ba_timestamp(data.get("finished_at"), "finished_at")
    except ValueError as exc:
        result.fail(str(exc))
        return result

    result.details["started_at"] = started_at.isoformat()
    result.details["finished_at"] = finished_at.isoformat()

    if finished_at < started_at:
        result.fail("finished_at is before started_at")

    age = now - finished_at
    max_age: timedelta = cfg["max_age"]
    result.details["age_seconds"] = int(age.total_seconds())
    result.details["max_age_seconds"] = int(max_age.total_seconds())
    if age < timedelta(seconds=-60):
        result.fail(f"finished_at is in the future by {int((-age).total_seconds())}s")
    elif age > max_age:
        result.fail(f"stale: age {format_timedelta(age)} > max {format_timedelta(max_age)}")

    return result


def check_launchd(period: str, cfg: dict[str, Any]) -> CheckResult:
    label = cfg["label"]
    result = CheckResult(f"{period}-launchd")
    target = LAUNCH_AGENTS_DIR / f"{label}.plist"
    result.details.update({"label": label, "domain": DOMAIN, "target_plist": str(target)})

    if not target.exists():
        result.fail(f"missing target plist: {target}")

    launchctl = Path("/bin/launchctl")
    if not launchctl.exists():
        result.warn("/bin/launchctl not available; skipped launchctl print")
        result.details["launchctl_print"] = "skipped"
        return result

    proc = subprocess.run(
        [str(launchctl), "print", f"{DOMAIN}/{label}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    result.details["launchctl_print_rc"] = proc.returncode
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip().splitlines()
        suffix = f": {stderr[-1]}" if stderr else ""
        result.fail(f"launchctl print failed for {DOMAIN}/{label}{suffix}")

    return result


def format_timedelta(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    chunks: list[str] = []
    if days:
        chunks.append(f"{days}d")
    if hours or chunks:
        chunks.append(f"{hours}h")
    if minutes or chunks:
        chunks.append(f"{minutes}m")
    chunks.append(f"{seconds}s")
    return " ".join(chunks)


def render_human(now: datetime, results: list[CheckResult], overall_ok: bool) -> str:
    lines = [
        f"Hermes Stats launchd healthcheck: {'OK' if overall_ok else 'FAIL'}",
        f"time: {now.isoformat()} ({TZ_NAME})",
        f"domain: {DOMAIN}",
    ]
    for result in results:
        marker = "OK" if result.ok else "FAIL"
        if result.name in JOBS:
            finished = result.details.get("finished_at", "?")
            age = result.details.get("age_seconds")
            max_age = result.details.get("max_age_seconds")
            age_text = format_timedelta(timedelta(seconds=age)) if isinstance(age, int) else "?"
            max_text = format_timedelta(timedelta(seconds=max_age)) if isinstance(max_age, int) else "?"
            lines.append(
                f"{marker} {result.name}: status={result.details.get('status')!r} "
                f"exit_code={result.details.get('exit_code')!r} finished_at={finished} age={age_text}/{max_text}"
            )
        else:
            label = result.details.get("label", result.name)
            rc = result.details.get("launchctl_print_rc", result.details.get("launchctl_print", "?"))
            lines.append(f"{marker} {result.name}: label={label} plist={result.details.get('target_plist')} launchctl_print={rc}")
        for warning in result.warnings:
            lines.append(f"  WARN: {warning}")
        for error in result.errors:
            lines.append(f"  ERROR: {error}")
    return "\n".join(lines)


def main() -> int:
    now = datetime.now(TZ)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    results: list[CheckResult] = []
    for period, cfg in JOBS.items():
        results.append(read_status(period, cfg, now))
    for period, cfg in JOBS.items():
        results.append(check_launchd(period, cfg))

    overall_ok = all(result.ok for result in results)
    report = render_human(now, results, overall_ok)

    payload = {
        "checked_at": now.isoformat(),
        "timezone": TZ_NAME,
        "ok": overall_ok,
        "domain": DOMAIN,
        "results": [
            {
                "name": result.name,
                "ok": result.ok,
                "details": result.details,
                "warnings": result.warnings,
                "errors": result.errors,
            }
            for result in results
        ],
    }
    LATEST_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with HEALTH_LOG.open("a", encoding="utf-8") as log_file:
        log_file.write(report + "\n")

    print(report)
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
