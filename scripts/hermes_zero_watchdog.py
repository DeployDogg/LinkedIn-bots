#!/usr/bin/env python3
"""No-LLM watchdog that triggers Lily repair when report useful-output is zero."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Users/deploydog-ai/LinkedIn')
SCRIPT_DIR = ROOT / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))

import hermes_container_status_bot as stats  # noqa: E402

BA = ZoneInfo('America/Argentina/Buenos_Aires')
STATE_DIR = ROOT / 'shared/logs/HermesStats/watchdog'
REPAIR_RUNNER = SCRIPT_DIR / 'hermes_zero_repair_runner.py'


def collect(period: str) -> tuple[dict, str, str]:
    now = datetime.now(BA)
    start, end, label = stats.period_range(period, now)
    ps = stats.docker_ps()
    data = {
        'period': period,
        'label': label,
        'start': start.isoformat(),
        'end': end.isoformat(),
        'jobseeker': stats.jobseeker_stats(start, end, ps),
        'connectman': stats.connectman_stats(start, end, ps),
        'sendman': stats.sendman_stats(start, end, ps),
        'postliker': stats.postliker_stats(start, end, ps),
    }
    return data, stats.format_message(period), label


def find_triggers(data: dict) -> dict[str, str]:
    period = data['period']
    triggers: dict[str, str] = {}
    if int(data['jobseeker'].get('applications') or 0) == 0:
        triggers['jobseeker'] = 'JobSeeker app == 0'
    if int(data['sendman'].get('sent') or 0) == 0:
        triggers['sendman'] = 'SendMan msg == 0'
    if int(data['postliker'].get('verified') or 0) == 0:
        triggers['postliker'] = 'PostLiker verified == 0'
    if period == 'weekly' and int(data['connectman'].get('sent') or 0) == 0:
        triggers['connectman'] = 'ConnectMan weekly sent == 0'
    # Андрей asked to exclude ConnectMan from daily checks.
    if period != 'weekly':
        triggers.pop('connectman', None)
    return triggers


def state_key(period: str, label: str, service: str) -> str:
    safe = ''.join(ch if ch.isalnum() else '_' for ch in f'{period}_{label}_{service}')
    return safe[:180]


def recently_launched(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
        launched_at = datetime.fromisoformat(payload['launched_at']).astimezone(BA)
    except Exception:
        return False
    return datetime.now(BA) - launched_at < timedelta(hours=ttl_hours)


def spawn_runner(period: str, services: list[str], trigger_file: Path) -> int:
    log_dir = ROOT / 'shared/logs/HermesStats/watchdog'
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(BA).strftime('%Y%m%d_%H%M%S')
    out_path = log_dir / f'{ts}_{period}_spawn.log'
    cmd = [
        '/usr/bin/python3', str(REPAIR_RUNNER),
        '--period', period,
        '--services', ','.join(services),
        '--trigger-file', str(trigger_file),
    ]
    with out_path.open('ab') as out:
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    return proc.pid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', required=True, choices=['daily', 'weekly'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='Ignore recent launch state')
    args = ap.parse_args()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    data, report, label = collect(args.period)
    triggers = find_triggers(data)
    if not triggers:
        print(f'watchdog: no zero-output triggers for {args.period}')
        return 0

    ttl = 24 if args.period == 'weekly' else 12
    selected = []
    skipped_recent = []
    pending_markers: list[tuple[Path, dict]] = []
    for service, reason in triggers.items():
        marker = STATE_DIR / f'{state_key(args.period, label, service)}.json'
        if not args.force and recently_launched(marker, ttl):
            skipped_recent.append(service)
            continue
        selected.append(service)
        pending_markers.append((marker, {
            'period': args.period,
            'label': label,
            'service': service,
            'reason': reason,
            'launched_at': datetime.now(BA).isoformat(),
        }))

    trigger_file = STATE_DIR / f'{datetime.now(BA).strftime("%Y%m%d_%H%M%S")}_{args.period}_triggers.json'
    trigger_file.write_text(json.dumps({
        'period': args.period,
        'label': label,
        'triggers': triggers,
        'selected': selected,
        'skipped_recent': skipped_recent,
        'stats': data,
        'report': report,
    }, ensure_ascii=False, indent=2), encoding='utf-8')

    if args.dry_run:
        print(json.dumps({'period': args.period, 'triggers': triggers, 'selected': selected, 'skipped_recent': skipped_recent, 'trigger_file': str(trigger_file)}, ensure_ascii=False, indent=2))
        return 0

    if not selected:
        print(f'watchdog: triggers exist but recently launched: {skipped_recent}')
        return 0
    for marker, payload in pending_markers:
        marker.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    pid = spawn_runner(args.period, selected, trigger_file)
    print(f'watchdog: spawned repair runner pid={pid} services={selected} trigger_file={trigger_file}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
