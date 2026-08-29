#!/usr/bin/env python3
"""No-LLM LinkedIn container status reporter for Telegram.

Collects Docker/container state + LinkedIn pipeline state and optionally sends a
plain-text message to Telegram Bot API.
"""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Users/deploydog-ai/LinkedIn')
CONFIG_PATH = ROOT / '.hermes_stats_bot.env'
SERVICES = {
    'jobseeker': 'JobSeeker',
    'connectman': 'ConnectMan',
    'sendman': 'SendMan',
    'postliker': 'PostLiker',
}
BLOCKER_KEYWORDS = ['captcha', 'security', 'rate-limit', 'rate limit', 'safeguard', 'login', 'blocked_by_question', 'checkpoint']
BA = ZoneInfo('America/Argentina/Buenos_Aires')


def load_scheduler_module():
    path = ROOT / 'scripts' / 'linkedin_scheduler.py'
    spec = importlib.util.spec_from_file_location('linkedin_scheduler_for_stats', path)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception:
        return None
    return module


def scheduler_schedule_label(service: str) -> str:
    module = load_scheduler_module()
    if module and hasattr(module, 'schedule_label'):
        return str(module.schedule_label(service))
    fallback = {
        'jobseeker': 'daily 11:00 max once/day',
        'postliker': 'daily 06:00 max once/day',
        'sendman': 'daily09:00 max once/day',
        'connectman': 'Mon11 + Wed11 fallback max once/day',
    }
    return fallback.get(service, 'manual')


def host_lock_status(path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+') as fh:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            return {'locked': False, 'path': str(path)}
        except BlockingIOError:
            return {'locked': True, 'path': str(path)}


def load_env(path: Path) -> dict[str, str]:
    data = {}
    if path.exists():
        for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def run(cmd: list[str], cwd: Path = ROOT, timeout: int = 25) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 999, f'{type(e).__name__}: {e}'


def parse_dt(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(BA)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    # Python's fromisoformat accepts -03:00 but not the log format -0300.
    if re.search(r'[+-]\d{4}$', s):
        s = s[:-5] + s[-5:-2] + ':' + s[-2:]
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BA)
        return dt.astimezone(BA)
    except Exception:
        return None


def period_range(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    today = now.date()
    if period == 'weekly':
        # Previous full Monday..Sunday when executed on Monday 09:30 BA.
        this_monday = today - timedelta(days=today.weekday())
        start_d = this_monday - timedelta(days=7)
        end_d = this_monday - timedelta(days=1)
        start = datetime.combine(start_d, time.min, BA)
        end = datetime.combine(end_d, time.max, BA)
        label = f'weekly {start_d.isoformat()}..{end_d.isoformat()}'
        return start, end, label
    # Андрей reads the morning "daily" Telegram report as "за сутки".  The
    # report is sent at 09:30 BA, while JobSeeker normally runs at 11:00 BA, so a
    # calendar-day window (00:00..23:59) makes real applications from yesterday
    # late morning/afternoon disappear as `app 0`.  Use a rolling 24h window for
    # daily stats and show the range explicitly in the message.
    end = now
    start = now - timedelta(hours=24)
    return start, end, f'daily last24 {start.strftime("%Y-%m-%d %H:%M")}..{end.strftime("%Y-%m-%d %H:%M")} BA'


def in_period(value, start: datetime, end: datetime) -> bool:
    dt = parse_dt(value)
    return bool(dt and start <= dt <= end)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8', errors='ignore'))
    except Exception:
        return default


def read_dotenv() -> dict[str, str]:
    return load_env(ROOT / '.env')


def report_env(env: dict[str, str]) -> dict[str, str]:
    """Only persist non-secret report mode flags in HermesStats ledgers."""
    return {
        'CRON_TEST_MODE': str(env.get('CRON_TEST_MODE', '?')),
        'SAFE_MODE': str(env.get('SAFE_MODE', '?')),
    }


def docker_ps() -> dict[str, dict]:
    # Use `ps -a`, not only running containers. During maintenance/rebuild work
    # the LinkedIn compose project can be intentionally stopped by another
    # session. If we only look at running containers, Docker reports services as
    # missing and later `compose exec` errors become the misleading
    # `cron read failed: service ...` message. With `-a` we can report the real
    # state: exited/created/restarting/running.
    rc, out = run(['docker', 'compose', 'ps', '-a', '--format', 'json'], timeout=35)
    result = {}
    if rc != 0 or not out:
        return {'_error': {'error': out or f'exit {rc}'}}
    try:
        data = json.loads(out)
        rows = data if isinstance(data, list) else [data]
    except Exception:
        rows = []
        for line in out.splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    for row in rows:
        keys = [row.get('Service'), row.get('Name'), row.get('Names'), row.get('ID')]
        for name in keys:
            if name:
                result[str(name)] = row
    return result


def health_for(service: str, ps: dict[str, dict]) -> str:
    row = ps.get(service) or ps.get(f'linkedin-{service}') or {}
    if not row:
        return 'unknown'
    state = str(row.get('State') or row.get('Status') or '').lower()
    health = str(row.get('Health') or '').lower()
    if 'unhealthy' in health or 'unhealthy' in state:
        return 'unhealthy'
    if 'healthy' in health or 'healthy' in state:
        return 'healthy'
    if 'running' in state or row.get('Running') is True:
        return 'running/no-health'
    if 'exited' in state or 'exited' in str(row.get('Status') or '').lower():
        return 'exited'
    return state or 'unknown'


def container_cron_and_mode(service: str, ps: dict[str, dict] | None = None) -> tuple[str, str, str]:
    if ps is not None:
        row = ps.get(service) or ps.get(f'linkedin-{service}') or {}
        state = str(row.get('State') or row.get('Status') or '').lower()
        if row and 'running' not in state:
            status = str(row.get('Status') or row.get('State') or 'not running')
            return '?', '?', f'container {status}'
    cmd = "echo mode=${CRON_TEST_MODE:-?} safe=${SAFE_MODE:-?} central=${CENTRAL_SCHEDULER_MODE:-0}; ps -eo args | grep supercronic | grep -v grep || true; if [ -f /app/crontab ]; then cat /app/crontab; fi"
    rc, out = run(['docker', 'compose', 'exec', '-T', service, 'sh', '-lc', cmd], timeout=25)
    if rc != 0:
        return '?', '?', f'cron read failed: {out[:160]}'
    mode = '?'; safe = '?'; central = '0'; cron_lines = []
    for line in out.splitlines():
        if line.startswith('mode='):
            m = re.search(r'mode=([^ ]+) safe=([^ ]+)(?: central=([^ ]+))?', line)
            if m:
                mode, safe = m.group(1), m.group(2)
                central = m.group(3) or '0'
        elif line.strip() and not line.startswith('/usr/local/bin/supercronic'):
            cron_lines.append(line.strip())
    if central == '1':
        return 'central', safe, scheduler_schedule_label(service)
    return mode, safe, ' | '.join(cron_lines) if cron_lines else 'not found'


def latest_daily_logs(service_title: str, start: datetime, end: datetime) -> list[Path]:
    base = ROOT / 'shared' / 'logs' / service_title / 'daily'
    if not base.exists():
        return []
    paths = []
    d = start.date()
    while d <= end.date():
        p = base / f'{d.isoformat()}.log'
        if p.exists():
            paths.append(p)
        d += timedelta(days=1)
    return paths


def blocker_summary(service_title: str, start: datetime, end: datetime, extra_texts: list[str] | None = None) -> str:
    """Return compact blocker counters only; no raw JSON/log excerpts."""
    counts = Counter()
    texts = []
    for p in latest_daily_logs(service_title, start, end):
        try:
            texts.extend(p.read_text(encoding='utf-8', errors='ignore').splitlines()[-800:])
        except Exception:
            pass
    if extra_texts:
        texts.extend(extra_texts)
    labels = {
        'blocked_by_question': 'questions',
        'rate-limit': 'rate-limit',
        'rate limit': 'rate-limit',
        'safeguard': 'safeguard',
        'checkpoint': 'checkpoint',
        'security': 'security',
        'captcha': 'captcha',
        'login': 'login',
    }
    for line in texts:
        low = line.lower()
        for key in BLOCKER_KEYWORDS:
            if key in low:
                counts[labels.get(key, key)] += 1
    if not counts:
        return 'none'
    return ', '.join(f'{k}×{v}' for k, v in counts.most_common(4))


def merge_blockers(*values: str) -> str:
    counts: Counter = Counter()
    for value in values:
        if not value or value == 'none':
            continue
        for part in str(value).split(','):
            part = part.strip()
            if not part:
                continue
            if '×' in part:
                name, raw_count = part.rsplit('×', 1)
                try:
                    counts[name.strip()] += int(raw_count)
                except Exception:
                    counts[name.strip()] += 1
            else:
                counts[part] += 1
    if not counts:
        return 'none'
    return ', '.join(f'{k}×{v}' for k, v in counts.most_common(4))


def classify_attempt_blocker(attempt: dict) -> str:
    """Classify failed scheduler attempts from their durable stdout/stderr tail.

    The daily report can run after a worker failed but before that worker wrote a
    rich status JSON.  In that case the old report showed `err 1` and
    `blocks: none`, hiding the real reason Андрей needed: browser/CDP/login
    failure vs LinkedIn/product limit.  Treat scheduler attempt tails as first-
    class evidence for the period.
    """
    if not attempt:
        return 'none'
    text = json.dumps({
        'reason': attempt.get('reason'),
        'stop': attempt.get('stop'),
        'stdout_tail': attempt.get('stdout_tail'),
        'stderr_tail': attempt.get('stderr_tail'),
        'exit_code': attempt.get('exit_code'),
    }, ensure_ascii=False).lower()
    exit_code = attempt.get('exit_code')
    counts = Counter()
    if 'connect_over_cdp' in text or 'browser' in text and 'timeout' in text:
        counts['cdp/browser'] += 1
    if 'login_required' in text or exit_code == 11:
        counts['login/session'] += 1
    if 'captcha' in text:
        counts['captcha'] += 1
    if 'checkpoint' in text or 'security verification' in text or 'verify your identity' in text:
        counts['security'] += 1
    if 'rate limit' in text or 'rate-limit' in text:
        counts['rate-limit'] += 1
    if 'safeguard' in text:
        counts['safeguard'] += 1
    if not counts and exit_code not in (None, 0):
        counts[f'exit{exit_code}'] += 1
    if not counts:
        return 'none'
    return ', '.join(f'{k}×{v}' for k, v in counts.most_common(4))


def attempt_in_period(attempt: dict, start: datetime, end: datetime) -> bool:
    return bool(attempt and in_period(attempt.get('finished_at') or attempt.get('started_at'), start, end))


def enrich_with_scheduler_attempts(stats_by_service: dict[str, dict], sched: dict, start: datetime, end: datetime) -> None:
    tasks = sched.get('tasks') or {}
    for service, stats in stats_by_service.items():
        task = tasks.get(service) or {}
        attempts: list[dict] = []
        # Prefer the full scheduler history when present. `last_attempt` can be
        # overwritten by a later run outside a simulated/report window, which is
        # exactly how yesterday's JobSeeker exit11 disappeared from the 09:30
        # audit after today's 11:00 attempt ran.
        for attempt in task.get('history') or []:
            if attempt_in_period(attempt, start, end):
                attempts.append(attempt)
        last_attempt = task.get('last_attempt') or {}
        if attempt_in_period(last_attempt, start, end):
            key = json.dumps({k: last_attempt.get(k) for k in ('started_at', 'finished_at', 'exit_code', 'status')}, sort_keys=True)
            existing = {json.dumps({k: a.get(k) for k in ('started_at', 'finished_at', 'exit_code', 'status')}, sort_keys=True) for a in attempts}
            if key not in existing:
                attempts.append(last_attempt)
        failed = 0
        blockers = str(stats.get('blockers') or 'none')
        for attempt in attempts:
            exit_code = attempt.get('exit_code')
            if attempt.get('status') == 'failed' or (exit_code not in (None, 0)):
                failed += 1
                blockers = merge_blockers(blockers, classify_attempt_blocker(attempt))
        if failed:
            stats['errors'] = max(int(stats.get('errors') or 0), failed)
            stats['blockers'] = blockers


def iter_connectman_log_statuses(start: datetime, end: datetime) -> list[dict]:
    """Recover ConnectMan run summaries from durable logs.

    ConnectMan keeps only the latest JSON status in
    `outreach_run_status.json`. Manual canaries, soft-recruiter dry-runs, or
    later diagnostic runs can overwrite that file, so weekly reports must not
    depend on the latest snapshot alone. Parse all-time.log for historical
    standard-mode summaries in the requested period, then let callers merge the
    current latest file as a fallback.
    """
    p = ROOT / 'shared/logs/ConnectMan/all-time.log'
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding='utf-8', errors='ignore').splitlines()
    except Exception:
        return []

    statuses: list[dict] = []
    buf: list[str] = []
    depth = 0

    def feed(obj: dict) -> None:
        if not isinstance(obj, dict) or 'started_at' not in obj:
            return
        # Standard ConnectMan summaries have search_url/max_connects; soft
        # recruiter summaries have mode=soft_recruiter and separate counters.
        if obj.get('mode') == 'soft_recruiter' or 'search_url' not in obj:
            return
        if in_period(obj.get('started_at'), start, end):
            statuses.append(obj)

    for raw in lines:
        line = raw.strip()
        if not buf and not line.startswith('{'):
            continue
        if not buf:
            buf = [line]
            depth = line.count('{') - line.count('}')
        else:
            buf.append(line)
            depth += line.count('{') - line.count('}')
        if depth > 0:
            continue
        text = '\n'.join(buf)
        buf = []
        depth = 0
        try:
            feed(json.loads(text))
        except Exception:
            continue
    return statuses


def health_icon(health: str) -> str:
    return '🍀' if str(health).lower() == 'healthy' else '🔥'


def compact_mode(env: dict[str, str], modes: dict[str, tuple[str, str]], ps: dict[str, dict] | None = None) -> str:
    env_mode = env.get('CRON_TEST_MODE', '?')
    env_safe = env.get('SAFE_MODE', '?')
    mode_values = {m for m, _ in modes.values() if m not in ('?', '')}
    mode_label = 'central' if 'central' in mode_values else ('test' if '1' in mode_values else 'cron')
    if ps:
        states = [health_for(svc, ps) for svc in SERVICES]
        counts = Counter(states)
        healthy = counts.get('healthy', 0)
        order = ['healthy', 'running/no-health', 'exited', 'unhealthy', 'unknown']
        parts = [f'{name}×{counts[name]}' for name in order if counts.get(name)]
        parts.extend(f'{name}×{count}' for name, count in sorted(counts.items()) if name not in order)
        runtime_text = f'runtime healthy {healthy}/{len(states)} ({", ".join(parts)})'
    else:
        runtime_text = 'runtime health unknown'
    prod = env_mode == '0' and env_safe == '0'
    icon = '🍀' if prod else '🔥'
    return f'{icon} prod env={env_mode}/{env_safe}, mode {mode_label}, {runtime_text}'


def human_cron(cron: str) -> str:
    """Convert the first 5 cron fields to a short human schedule."""
    known_labels = set(getattr(load_scheduler_module(), 'SCHEDULE_LABELS', {}).values()) if load_scheduler_module() else {
        'daily 11:00 max once/day', 'daily 06:00 max once/day', 'daily09:00 max once/day', 'Mon11 + Wed11 fallback max once/day', 'daily 10:17 max once/day'
    }
    if str(cron) in known_labels:
        return str(cron)
    if str(cron).startswith(('container ', 'cron read failed:')):
        return str(cron)
    parts = str(cron).split()
    if len(parts) < 5:
        return 'cron?'
    minute, hour, dom, month, dow = parts[:5]
    dow_names = {'0': 'Sun', '1': 'Mon', '2': 'Tue', '3': 'Wed', '4': 'Thu', '5': 'Fri', '6': 'Sat', '7': 'Sun'}

    def fmt_time(h: str, m: str) -> str:
        try:
            return f'{int(h):02d}:{int(m):02d}'
        except Exception:
            return f'{h}:{m}'

    if minute == '0,20,40' and hour == '*' and dom == '*' and month == '*' and dow == '*':
        return 'every 20m'
    if dom == '*' and month == '*' and dow == '*':
        if ',' in hour and minute.isdigit():
            return 'daily ' + ','.join(fmt_time(h, minute) for h in hour.split(','))
        if hour.isdigit() and minute.isdigit():
            return f'daily {fmt_time(hour, minute)}'
    if dom == '*' and month == '*' and dow != '*' and hour.isdigit() and minute.isdigit():
        days = ','.join(dow_names.get(x, x) for x in dow.split(','))
        return f'{days} {fmt_time(hour, minute)}'
    return ' '.join(parts[:5])


def jobseeker_log_events(start: datetime, end: datetime) -> tuple[list[dict], int, Counter, int, int]:
    """Structured JobSeeker outcomes and run failures for the period.

    JobSeeker progress is a pruned/latest snapshot, and daily logs are retained
    only briefly. Use all-time.log when available so weekly reports can still see
    historical runs and container crashes such as extractor import failures.
    """
    outcomes: list[dict] = []
    error_runs: set[str] = set()
    blocker_counts: Counter = Counter()
    scheduler_lock_skips = 0
    successful_runs = 0
    base = ROOT / 'shared/logs/JobSeeker'
    paths = [base / 'all-time.log'] if (base / 'all-time.log').exists() else latest_daily_logs('JobSeeker', start, end)
    run_dt: datetime | None = None
    run_re = re.compile(r'--- JobSeeker run (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}) ---')
    bracket_re = re.compile(r'^\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\]')
    time_re = re.compile(r'time="(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})"')

    def line_dt(line: str) -> datetime | None:
        m = bracket_re.search(line) or time_re.search(line)
        return parse_dt(m.group(1)) if m else None

    def in_current_period(line: str) -> bool:
        dt = line_dt(line) or run_dt or path_dt
        return bool(dt and start <= dt <= end)

    for p in paths:
        try:
            lines = p.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception:
            continue
        try:
            path_dt = datetime.combine(datetime.strptime(p.stem, '%Y-%m-%d').date(), time.min, BA)
        except Exception:
            path_dt = None
        for raw in lines:
            line = raw.strip()
            m_run = run_re.search(line)
            if m_run:
                run_dt = parse_dt(m_run.group(1))
                continue
            if not in_current_period(line):
                continue
            low = line.lower()
            if 'busy lock' in low and 'child not started' in low:
                scheduler_lock_skips += 1
                continue
            if 'task=jobseeker' in low and ' exit=0' in low:
                successful_runs += 1
            if line.startswith('{'):
                try:
                    item = json.loads(line)
                except Exception:
                    item = {}
                if item.get('event') == 'outcome':
                    outcomes.append(item)
                    text = json.dumps(item, ensure_ascii=False).lower()
                    if 'blocked_by_question' in text:
                        notes = str(item.get('notes') or '').lower()
                        if 'unknown required linkedin question' in notes:
                            blocker_counts['questions'] += 1
                        elif 'page not found' in text or 'could not find next/review/submit action' in notes or item.get('code') == 14:
                            blocker_counts['automation'] += 1
                        else:
                            blocker_counts['questions'] += 1
                    if 'captcha' in text:
                        blocker_counts['captcha'] += 1
                    if 'checkpoint' in text or 'verify your identity' in text or 'security verification' in text:
                        blocker_counts['security'] += 1
                    if 'rate limit' in text or 'rate-limit' in text:
                        blocker_counts['rate-limit'] += 1
                    if 'safeguard' in text:
                        blocker_counts['safeguard'] += 1
                continue
            if (
                'traceback (most recent call last)' in low
                or 'nameerror:' in low
                or 'modulenotfounderror:' in low
            ):
                key_dt = run_dt or line_dt(line)
                error_runs.add(key_dt.isoformat() if key_dt else line[:120])
                continue
            m_exit = re.search(r'(?:error running command: exit status|worker exit code:)\s*(\d+)', low)
            if m_exit:
                code = int(m_exit.group(1))
                # JobSeeker uses 10/13/14 for expected per-job blockers/questions;
                # those belong in questions/blockers, not container/script errors.
                if code not in (0, 10, 13, 14):
                    key_dt = run_dt or line_dt(line)
                    error_runs.add(key_dt.isoformat() if key_dt else line[:120])
    return outcomes, len(error_runs), blocker_counts, scheduler_lock_skips, successful_runs


def jobseeker_progress_stats(start: datetime, end: datetime) -> dict:
    """Recover JobSeeker action counts from every active progress snapshot.

    The quota runner writes role-specific progress files such as
    `li_apply_devops_quota_progress.json`; the older reporter only used
    `li_apply_platform_full_progress.json` as a fallback.  Also, all-time logs do
    not timestamp every outcome line, so a long run that started before the
    rolling window can have in-window submissions undercounted.  Progress records
    carry per-job `submitted_at`/`updated_at`, which is the most precise source
    for rolling daily counts.
    """
    base = ROOT / 'shared/legacy_state/jobseeker'
    latest_by_url: dict[str, tuple[datetime, dict]] = {}
    if not base.exists():
        return {'applications': 0, 'blocked_by_question': 0, 'skipped': 0, 'errors': 0}
    for p in base.glob('*progress.json'):
        d = read_json(p, {})
        for record in d.get('records') or []:
            at = record.get('submitted_at') or record.get('updated_at') or d.get('updated_at') or d.get('created_at')
            dt = parse_dt(at)
            if not dt or not (start <= dt <= end):
                continue
            url = str(record.get('url') or f'{p}:{len(latest_by_url)}')
            old = latest_by_url.get(url)
            if old is None or dt >= old[0]:
                latest_by_url[url] = (dt, record)

    applications = blocked = skipped = errors = 0
    for _, record in latest_by_url.values():
        status = str(record.get('status') or '').lower()
        notes = str(record.get('notes') or '').lower()
        if ('applied' in status or 'submitted' in status) and (
            'submitted successfully' in notes or 'confirmed submission' in notes
        ):
            applications += 1
        elif status == 'blocked_by_question':
            blocked += 1
        elif 'not_applicable' in status or 'expired' in status or 'skip' in status or 'already shows application submitted' in notes or 'already applied' in notes:
            skipped += 1
        elif 'error' in status or 'failed' in status:
            errors += 1
    return {
        'applications': applications,
        'blocked_by_question': blocked,
        'skipped': skipped,
        'errors': errors,
    }


def jobseeker_stats(start: datetime, end: datetime, ps: dict[str, dict]) -> dict:
    p = ROOT / 'shared/legacy_state/jobseeker/li_apply_platform_full_progress.json'
    d = read_json(p, {})
    outcomes, log_errors, blocker_counts, scheduler_lock_skips, successful_runs = jobseeker_log_events(start, end)
    progress = jobseeker_progress_stats(start, end)

    if outcomes or log_errors:
        applications = 0
        blocked = 0
        skipped = 0
        errors = log_errors
        for item in outcomes:
            status = str(item.get('status') or '').lower()
            notes = str(item.get('notes') or '').lower()
            code = item.get('code')

            # Count only applications actually submitted during this period.
            # "Already shows application submitted/applied" is evidence that the
            # worker can read LinkedIn state, but it is not a new application sent.
            if ('applied' in status or 'submitted' in status) and (
                'submitted successfully' in notes or 'confirmed submission' in notes
            ):
                applications += 1
            elif 'not_applicable' in status or 'expired' in status or 'skip' in status or 'already shows application submitted' in notes or 'already applied' in notes:
                skipped += 1

            if status == 'blocked_by_question':
                blocked += 1
            elif code not in (None, 0, 10, 14):
                errors += 1
        # Merge with per-job progress snapshots.  This prevents the daily
        # rolling-24h report from showing 0 when the action run started before
        # the window or when the quota runner wrote to role-specific progress
        # files instead of the legacy platform_full file.
        applications = max(applications, progress['applications'])
        blocked = max(blocked, progress['blocked_by_question'])
        skipped = max(skipped, progress['skipped'])
        errors = max(errors, progress['errors'])
        if progress['blocked_by_question']:
            blocker_counts['questions'] = max(blocker_counts.get('questions', 0), progress['blocked_by_question'])
        blockers = 'none' if not blocker_counts else ', '.join(f'{k}×{v}' for k, v in blocker_counts.most_common(4))
    else:
        # Fallback for periods without durable logs.
        records = d.get('records') or []
        period_records = [r for r in records if in_period(r.get('updated_at') or d.get('updated_at') or d.get('created_at'), start, end)]
        c = Counter(r.get('status') or 'unknown' for r in period_records)
        applications = sum(v for k, v in c.items() if 'applied' in k or 'submitted' in k)
        blocked = c.get('blocked_by_question', 0)
        skipped = sum(v for k, v in c.items() if 'not_applicable' in k or 'skip' in k or 'skipped' in k)
        errors = sum(v for k, v in c.items() if 'error' in k or 'failed' in k)
        extra = [json.dumps({'status_counter': dict(c), 'updated_at': d.get('updated_at')}, ensure_ascii=False)]
        blockers = blocker_summary('JobSeeker', start, end, extra)
        applications = max(applications, progress['applications'])
        blocked = max(blocked, progress['blocked_by_question'])
        skipped = max(skipped, progress['skipped'])
        errors = max(errors, progress['errors'])
        if progress['blocked_by_question'] and blockers == 'none':
            blockers = f'questions×{progress["blocked_by_question"]}'

    return {
        'health': health_for('jobseeker', ps),
        'applications': applications,
        'blocked_by_question': blocked,
        'skipped': skipped,
        'errors': errors,
        'successful_runs': successful_runs,
        'scheduler_lock_skips': scheduler_lock_skips,
        'blockers': blockers,
    }


def connectman_stats(start: datetime, end: datetime, ps: dict[str, dict]) -> dict:
    d = read_json(ROOT / 'shared/legacy_state/outreach_run_status.json', {})
    statuses = iter_connectman_log_statuses(start, end)
    seen_starts = {str(s.get('started_at')) for s in statuses if s.get('started_at')}
    if d.get('mode') != 'soft_recruiter' and in_period(d.get('started_at'), start, end) and str(d.get('started_at')) not in seen_starts:
        statuses.append(d)

    sent_count = skipped_count = errors_count = 0
    limit_seen = False
    extra = []
    for status in statuses:
        started_at = status.get('started_at')
        sent_count += len([x for x in status.get('sent') or [] if in_period(x.get('at') or started_at, start, end)])
        skipped_raw = status.get('skipped') or []
        skipped_count += len([x for x in skipped_raw if in_period(x.get('at') or started_at, start, end)]) if skipped_raw else 0
        errors_raw = status.get('errors') or []
        errors_count += len([x for x in errors_raw if in_period(x.get('at') or started_at, start, end)]) if errors_raw else 0
        max_connects = int(status.get('max_connects') or 0)
        stop = status.get('stop_reason')
        if (max_connects and len(status.get('sent') or []) >= max_connects) or (stop and 'limit' in str(stop).lower()):
            limit_seen = True
        extra.append(json.dumps({
            'started_at': started_at,
            'stop_reason': stop,
            'visible_connect_buttons': status.get('visible_connect_buttons'),
            'sent': len(status.get('sent') or []),
            'errors': status.get('errors'),
        }, ensure_ascii=False))
    if not extra:
        extra = [json.dumps({'latest_started_at': d.get('started_at'), 'latest_mode': d.get('mode'), 'latest_stop_reason': d.get('stop_reason')}, ensure_ascii=False)]
    return {
        'health': health_for('connectman', ps),
        'sent': sent_count,
        'skipped': skipped_count,
        'errors': errors_count,
        'limit': 'yes' if limit_seen else 'no',
        'blockers': blocker_summary('ConnectMan', start, end, extra),
    }


def sendman_stats(start: datetime, end: datetime, ps: dict[str, dict]) -> dict:
    d = read_json(ROOT / 'shared/message_state/linkedin_message_outreach_status.json', {})
    sent = 0; skipped = 0; errors = 0; blockers_extra = []
    logs_dir = ROOT / 'shared/message_state/logs'
    for p in sorted(logs_dir.glob('linkedin_message_outreach_actions_*.jsonl')) if logs_dir.exists() else []:
        try:
            lines = p.read_text(encoding='utf-8', errors='ignore').splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            at = item.get('at') or item.get('ts') or item.get('time')
            if not in_period(at, start, end):
                continue
            ev = str(item.get('event') or item.get('status') or '').lower()
            if ev == 'sent':
                sent += 1
            elif 'skip' in ev:
                skipped += 1
            elif 'error' in ev or 'failed' in ev:
                errors += 1
            if any(k in json.dumps(item, ensure_ascii=False).lower() for k in BLOCKER_KEYWORDS):
                blockers_extra.append(json.dumps(item, ensure_ascii=False)[:220])
    if sent == 0 and in_period(d.get('started_at'), start, end):
        sent = int(d.get('sent_count') or 0)
        skipped = max(skipped, int(d.get('skipped_count') or 0))
        errors = max(errors, len(d.get('errors') or []))
    stop = str(d.get('stop_reason') or '')
    lower = json.dumps(d, ensure_ascii=False).lower()
    safeguard = 'yes' if 'safeguard' in lower else 'no'
    rate_limit = 'yes' if 'rate-limit' in lower or 'rate limit' in lower else 'no'
    inmail = 'exhausted' if 'inmail' in lower and ('exhaust' in lower or 'credit' in lower) else 'ok/unknown'
    blockers_extra.append(json.dumps({'stop_reason': stop, 'block_screenshot': d.get('block_screenshot'), 'errors': d.get('errors')}, ensure_ascii=False))
    return {
        'health': health_for('sendman', ps),
        'sent': sent,
        'skipped': skipped,
        'errors': errors,
        'safeguard': safeguard,
        'rate_limit': rate_limit,
        'inmail': inmail,
        'blockers': blocker_summary('SendMan', start, end, blockers_extra),
    }


def postliker_verified_count(status: dict) -> int:
    """Count durable verification for PostLiker status snapshots.

    Older/diagnostic runs sometimes confirmed the reaction directly on the feed
    (`confirmed_on_feed` / `Reaction button state: Like`) but did not populate
    the separate `verification` array, especially when a stable post URL could
    not be recovered. Treat those feed-confirmed likes as verified so reports do
    not undercount successful safe canaries.
    """
    verification = status.get('verification') or []
    verified = sum(1 for v in verification if v.get('verified'))
    if verification:
        return verified
    count = 0
    for item in status.get('liked') or []:
        if item.get('confirmed_on_feed') is True:
            count += 1
            continue
        aria = str(item.get('confirmed_aria_after_click') or '').strip().lower()
        if aria.startswith('reaction button state:') and 'no reaction' not in aria:
            count += 1
    return count


def iter_postliker_log_statuses(start: datetime, end: datetime) -> list[dict]:
    """Recover PostLiker run summaries from all-time.log.

    Some historical container runs wrote the JSON summary only to logs and the
    shared latest file, while the per-run `status.json` was missing. The weekly
    reporter used to scan only per-run status files, which made those periods
    appear as verified 0/0, skip 0, err 0, blocks none. Parse the durable
    all-time log as a fallback so old runs and blockers are visible.
    """
    p = ROOT / 'shared/logs/PostLiker/all-time.log'
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding='utf-8', errors='ignore').splitlines()
    except Exception:
        return []

    statuses: list[dict] = []
    buf: list[str] = []
    depth = 0

    def feed(obj: dict) -> None:
        if not isinstance(obj, dict) or 'feed_url' not in obj or 'started_at' not in obj:
            return
        if in_period(obj.get('started_at'), start, end):
            statuses.append(obj)

    for raw in lines:
        line = raw.strip()
        if not buf and not line.startswith('{'):
            continue
        if not buf:
            buf = [line]
            depth = line.count('{') - line.count('}')
        else:
            buf.append(line)
            depth += line.count('{') - line.count('}')
        if depth > 0:
            continue
        text = '\n'.join(buf)
        buf = []
        depth = 0
        try:
            feed(json.loads(text))
        except Exception:
            continue
    return statuses


def postliker_stats(start: datetime, end: datetime, ps: dict[str, dict]) -> dict:
    base = ROOT / 'shared/legacy_state/liked_posts'
    liked = skipped = errors = verified = 0
    blockers_extra = []
    seen_runs: set[str] = set()
    if base.exists():
        for run_dir in sorted(base.iterdir()):
            if not run_dir.is_dir() or not re.match(r'\d{8}_', run_dir.name):
                continue
            try:
                run_date = datetime.strptime(run_dir.name[:8], '%Y%m%d').date()
            except Exception:
                continue
            if not (start.date() <= run_date <= end.date()):
                continue
            status = read_json(run_dir / 'status.json', {})
            if not status:
                continue
            run_key = str(status.get('run_dir') or run_dir)
            seen_runs.add(run_key)
            liked += len(status.get('liked') or [])
            skipped += len(status.get('skipped') or [])
            errors += len(status.get('errors') or [])
            verified += postliker_verified_count(status)
            blockers_extra.append(json.dumps({'run': run_dir.name, 'stop_reason': status.get('stop_reason'), 'errors': status.get('errors')}, ensure_ascii=False))
    for status in iter_postliker_log_statuses(start, end):
        run_key = str(status.get('run_dir') or status.get('started_at'))
        if run_key in seen_runs:
            continue
        seen_runs.add(run_key)
        liked += len(status.get('liked') or [])
        skipped += len(status.get('skipped') or [])
        errors += len(status.get('errors') or [])
        verified += postliker_verified_count(status)
        blockers_extra.append(json.dumps({'run': Path(run_key).name, 'stop_reason': status.get('stop_reason'), 'errors': status.get('errors')}, ensure_ascii=False))
    latest = read_json(base / 'linkedin_liked_posts_status.json', {}) if base.exists() else {}
    if liked == verified == skipped == errors == 0 and in_period(latest.get('started_at'), start, end):
        liked = len(latest.get('liked') or [])
        skipped = len(latest.get('skipped') or [])
        errors = len(latest.get('errors') or [])
        verified = postliker_verified_count(latest)
    blockers_extra.append(json.dumps({'latest_stop_reason': latest.get('stop_reason')}, ensure_ascii=False))
    return {
        'health': health_for('postliker', ps),
        'liked': liked,
        'verified': verified,
        'skipped': skipped,
        'errors': errors,
        'blockers': blocker_summary('PostLiker', start, end, blockers_extra),
    }


def scheduler_status() -> dict:
    path = ROOT / 'shared/state/LinkedInScheduler/state.json'
    data = read_json(path, {})
    data.setdefault('tasks', {})
    data['lock'] = host_lock_status(ROOT / 'shared/state/LinkedInScheduler/scheduler.lock')
    return data


def scheduler_summary(data: dict) -> list[str]:
    lock = data.get('lock') or {}
    lock_text = 'locked' if lock.get('locked') else 'free'
    parts = [f'Scheduler · lock {lock_text}']
    tasks = data.get('tasks') or {}
    for name in ['postliker', 'connectman', 'sendman', 'jobseeker']:
        attempt = (tasks.get(name) or {}).get('last_attempt') or {}
        status = attempt.get('status')
        if not status:
            continue
        exit_code = attempt.get('exit_code')
        suffix = f' exit{exit_code}' if exit_code is not None else ''
        reason = f' {attempt.get("reason")}' if attempt.get('reason') else ''
        parts.append(f'{name} {status}{reason}{suffix}')
    return parts


def stats_payload_for_window(start: datetime, end: datetime) -> dict:
    ps = docker_ps()
    env = read_dotenv()
    modes: dict[str, tuple[str, str]] = {}
    crons: dict[str, str] = {}
    for svc in SERVICES:
        mode, safe, cron = container_cron_and_mode(svc, ps)
        modes[svc] = (mode, safe)
        crons[svc] = cron

    js = jobseeker_stats(start, end, ps)
    cm = connectman_stats(start, end, ps)
    sm = sendman_stats(start, end, ps)
    pl = postliker_stats(start, end, ps)
    sched = scheduler_status()
    enrich_with_scheduler_attempts({'jobseeker': js, 'connectman': cm, 'sendman': sm, 'postliker': pl}, sched, start, end)
    return {
        'generated_at': datetime.now(BA).isoformat(),
        'timezone': 'America/Argentina/Buenos_Aires',
        'window': {'start': start.isoformat(), 'end': end.isoformat()},
        'env': report_env(env),
        'modes': modes,
        'crons': crons,
        'scheduler': sched,
        'services': {
            'jobseeker': js,
            'connectman': cm,
            'sendman': sm,
            'postliker': pl,
        },
    }


def daily_ledger_dir() -> Path:
    return ROOT / 'shared/logs/HermesStats/daily'


def weekly_ledger_dir() -> Path:
    return ROOT / 'shared/logs/HermesStats/weekly'


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f'.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    tmp.replace(path)


def daily_ledger_path(end: datetime) -> Path:
    return daily_ledger_dir() / f'{end.date().isoformat()}.json'


def weekly_ledger_path(start: datetime, end: datetime) -> Path:
    return weekly_ledger_dir() / f'{start.date().isoformat()}..{end.date().isoformat()}.json'


def build_daily_summary(now: datetime | None = None) -> dict:
    now = now or datetime.now(BA)
    start, end, label = period_range('daily', now)
    payload = stats_payload_for_window(start, end)
    payload.update({
        'kind': 'daily',
        'report_date': end.date().isoformat(),
        'label': label.replace('daily ', ''),
        'source_contract': 'daily ledger: one rolling-24h action summary; weekly reports sum these daily ledgers',
    })
    return payload


def write_daily_summary(summary: dict) -> Path:
    end = parse_dt((summary.get('window') or {}).get('end')) or datetime.now(BA)
    path = daily_ledger_path(end)
    atomic_write_json(path, summary)
    return path


def read_daily_summary_for_date(day: date) -> dict | None:
    path = daily_ledger_dir() / f'{day.isoformat()}.json'
    if not path.exists():
        return None
    data = read_json(path, {})
    if isinstance(data, dict) and data.get('kind') == 'daily':
        return data
    return None


def parse_blocker_counts(value: str) -> Counter:
    counts: Counter = Counter()
    if not value or value == 'none':
        return counts
    for part in str(value).split(','):
        part = part.strip()
        if not part:
            continue
        if '×' in part:
            name, raw = part.rsplit('×', 1)
            try:
                counts[name.strip()] += int(raw)
            except Exception:
                counts[name.strip()] += 1
        else:
            counts[part] += 1
    return counts


def format_blocker_counts(counts: Counter) -> str:
    if not counts:
        return 'none'
    return ', '.join(f'{k}×{v}' for k, v in counts.most_common(4))


def sum_daily_summaries(summaries: list[dict], start: datetime, end: datetime) -> dict:
    services = {
        'jobseeker': {'health': 'unknown', 'applications': 0, 'blocked_by_question': 0, 'skipped': 0, 'errors': 0, 'successful_runs': 0, 'scheduler_lock_skips': 0, 'blockers': 'none'},
        'connectman': {'health': 'unknown', 'sent': 0, 'skipped': 0, 'errors': 0, 'limit': 'no', 'blockers': 'none'},
        'sendman': {'health': 'unknown', 'sent': 0, 'skipped': 0, 'errors': 0, 'safeguard': 'no', 'rate_limit': 'no', 'inmail': 'ok/unknown', 'blockers': 'none'},
        'postliker': {'health': 'unknown', 'liked': 0, 'verified': 0, 'skipped': 0, 'errors': 0, 'blockers': 'none'},
    }
    blocker_totals = {svc: Counter() for svc in services}
    crons: dict[str, str] = {svc: scheduler_schedule_label(svc) for svc in SERVICES}
    modes: dict[str, tuple[str, str]] = {svc: ('central', '?') for svc in SERVICES}
    env = read_dotenv()
    scheduler = scheduler_status()
    included: list[str] = []
    missing: list[str] = []

    for summary in summaries:
        report_date = summary.get('report_date') or ((summary.get('window') or {}).get('end') or '')[:10]
        if report_date:
            included.append(report_date)
        for svc, stats in (summary.get('services') or {}).items():
            if svc not in services or not isinstance(stats, dict):
                continue
            target = services[svc]
            if stats.get('health'):
                target['health'] = stats.get('health')
            for key in ['applications', 'blocked_by_question', 'skipped', 'errors', 'successful_runs', 'scheduler_lock_skips', 'sent', 'liked', 'verified']:
                if key in target:
                    try:
                        target[key] += int(stats.get(key) or 0)
                    except Exception:
                        pass
            if svc == 'connectman' and stats.get('limit') == 'yes':
                target['limit'] = 'yes'
            if svc == 'sendman':
                if stats.get('safeguard') == 'yes':
                    target['safeguard'] = 'yes'
                if stats.get('rate_limit') == 'yes':
                    target['rate_limit'] = 'yes'
                if stats.get('inmail') == 'exhausted':
                    target['inmail'] = 'exhausted'
            blocker_totals[svc].update(parse_blocker_counts(str(stats.get('blockers') or 'none')))
        if isinstance(summary.get('crons'), dict):
            crons.update({k: str(v) for k, v in summary['crons'].items() if k in SERVICES and v})
        if isinstance(summary.get('modes'), dict):
            for k, v in summary['modes'].items():
                if k in SERVICES and isinstance(v, (list, tuple)) and len(v) >= 2:
                    modes[k] = (str(v[0]), str(v[1]))
        if isinstance(summary.get('env'), dict):
            env.update({str(k): str(v) for k, v in summary['env'].items()})
        if isinstance(summary.get('scheduler'), dict):
            scheduler = summary['scheduler']

    for svc, counts in blocker_totals.items():
        services[svc]['blockers'] = format_blocker_counts(counts)

    return {
        'kind': 'weekly',
        'generated_at': datetime.now(BA).isoformat(),
        'timezone': 'America/Argentina/Buenos_Aires',
        'window': {'start': start.isoformat(), 'end': end.isoformat()},
        'report_date': end.date().isoformat(),
        'label': f'{start.date().isoformat()}..{end.date().isoformat()}',
        'source_contract': 'weekly ledger: sum of daily HermesStats daily ledger files',
        'included_daily_logs': sorted(set(included)),
        'missing_daily_logs': missing,
        'env': report_env(env),
        'modes': modes,
        'crons': crons,
        'scheduler': scheduler,
        'services': services,
    }


def build_weekly_summary(now: datetime | None = None, materialize_missing: bool = False) -> dict:
    now = now or datetime.now(BA)
    start, end, _ = period_range('weekly', now)
    summaries: list[dict] = []
    missing: list[str] = []
    d = start.date()
    while d <= end.date():
        summary = read_daily_summary_for_date(d)
        if summary is None:
            missing.append(d.isoformat())
            if materialize_missing:
                # Backfill the missing daily ledger from durable action artifacts
                # for that calendar day. Future daily launchd runs write this
                # ledger directly before sending Telegram, so weekly can simply
                # sum the files.
                day_start = datetime.combine(d, time.min, BA)
                day_end = datetime.combine(d, time.max, BA)
                summary = stats_payload_for_window(day_start, day_end)
                summary.update({
                    'kind': 'daily',
                    'report_date': d.isoformat(),
                    'label': f'{d.isoformat()} 00:00..23:59 BA',
                    'source_contract': 'backfilled daily ledger from durable service logs/artifacts',
                    'backfilled': True,
                })
                atomic_write_json(daily_ledger_dir() / f'{d.isoformat()}.json', summary)
        if summary is not None:
            summaries.append(summary)
        d += timedelta(days=1)
    weekly = sum_daily_summaries(summaries, start, end)
    weekly['missing_daily_logs'] = [x for x in missing if x not in set(weekly.get('included_daily_logs') or [])]
    return weekly


def write_weekly_summary(summary: dict) -> Path:
    start = parse_dt((summary.get('window') or {}).get('start')) or datetime.now(BA)
    end = parse_dt((summary.get('window') or {}).get('end')) or datetime.now(BA)
    path = weekly_ledger_path(start, end)
    atomic_write_json(path, summary)
    return path


def report_summary(period: str, persist: bool = False) -> dict:
    if period == 'weekly':
        summary = build_weekly_summary(materialize_missing=persist)
        if persist:
            summary['ledger_path'] = str(write_weekly_summary(summary))
        return summary
    summary = build_daily_summary()
    if persist:
        summary['ledger_path'] = str(write_daily_summary(summary))
    return summary


def format_summary(summary: dict) -> str:
    kind = 'weekly' if summary.get('kind') == 'weekly' else 'daily'
    services = summary.get('services') or {}
    js = services.get('jobseeker') or {}
    cm = services.get('connectman') or {}
    sm = services.get('sendman') or {}
    pl = services.get('postliker') or {}
    crons = summary.get('crons') or {svc: scheduler_schedule_label(svc) for svc in SERVICES}
    sched = summary.get('scheduler') or {'lock': {'locked': False}, 'tasks': {}}
    modes = summary.get('modes') or {svc: ('central', '?') for svc in SERVICES}
    env = summary.get('env') or read_dotenv()
    no_useful = int(js.get('applications') or 0) == int(cm.get('sent') or 0) == int(sm.get('sent') or 0) == int(pl.get('verified') or 0) == 0
    mode_line = compact_mode(env, modes, docker_ps())
    sched_lines = scheduler_summary(sched)
    date_line = str(summary.get('label') or '')
    if kind == 'daily' and not date_line.startswith('last24'):
        date_line = date_line or 'daily?'
    missing = summary.get('missing_daily_logs') or []
    source_line = ''
    if kind == 'weekly':
        included = len(summary.get('included_daily_logs') or [])
        source_line = f'   source: daily logs {included}/7' + (f' · missing {len(missing)}' if missing else '')

    lines = [
        f'📊 Hermes Stats · {kind}',
        f'{date_line} · {mode_line}',
        f'   {sched_lines[0]}',
        f'   last: ' + ' · '.join(sched_lines[1:4]) if len(sched_lines) > 1 else '   last: none',
        f'   no useful actions: {"yes" if no_useful else "no"}',
    ]
    if source_line:
        lines.append(source_line)
    lines.extend([
        '',
        f'{health_icon(str(js.get("health", "unknown")))} JobSeeker · {human_cron(str(crons.get("jobseeker", scheduler_schedule_label("jobseeker"))))}',
        f'   app {int(js.get("applications") or 0)} · questions {int(js.get("blocked_by_question") or 0)} · skip {int(js.get("skipped") or 0)} · lock-skip {int(js.get("scheduler_lock_skips") or 0)} · err {int(js.get("errors") or 0)}',
        f'   blocks: {js.get("blockers") or "none"}',
        '',
        f'{health_icon(str(cm.get("health", "unknown")))} ConnectMan · {human_cron(str(crons.get("connectman", scheduler_schedule_label("connectman"))))}',
        f'   sent {int(cm.get("sent") or 0)} · skip {int(cm.get("skipped") or 0)} · err {int(cm.get("errors") or 0)} · limit {cm.get("limit") or "no"}',
        f'   blocks: {cm.get("blockers") or "none"}',
        '',
        f'{health_icon(str(sm.get("health", "unknown")))} SendMan · {human_cron(str(crons.get("sendman", scheduler_schedule_label("sendman"))))}',
        f'   msg {int(sm.get("sent") or 0)} · skip {int(sm.get("skipped") or 0)} · err {int(sm.get("errors") or 0)} · safeguard {sm.get("safeguard") or "no"} · rate {sm.get("rate_limit") or "no"} · InMail {sm.get("inmail") or "ok/unknown"}',
        f'   blocks: {sm.get("blockers") or "none"}',
        '',
        f'{health_icon(str(pl.get("health", "unknown")))} PostLiker · {human_cron(str(crons.get("postliker", scheduler_schedule_label("postliker"))))}',
        f'   verified {int(pl.get("verified") or 0)}/{int(pl.get("liked") or 0)} · skip {int(pl.get("skipped") or 0)} · err {int(pl.get("errors") or 0)}',
        f'   blocks: {pl.get("blockers") or "none"}',
    ])
    return '\n'.join(lines)


def format_message(period: str, persist: bool = False) -> str:
    return format_summary(report_summary(period, persist=persist))


def send_telegram(text: str, token: str, chat_id: str) -> dict:
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({
        'chat_id': chat_id,
        'text': text,
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    req = urllib.request.Request(url, data=data, method='POST')
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode('utf-8'))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', choices=['daily', 'weekly'], required=True)
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    msg = format_message(args.period, persist=not args.dry_run)
    if args.send:
        cfg = load_env(CONFIG_PATH)
        token = cfg.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
        chat_id = cfg.get('TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID')
        if not token or not chat_id:
            print('ERROR: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing', file=sys.stderr)
            print(msg)
            return 2
        try:
            res = send_telegram(msg, token, chat_id)
            print(f'sent ok: message_id={res.get("result", {}).get("message_id")} period={args.period}')
        except Exception as e:
            print(f'Telegram send failed: {type(e).__name__}: {e}', file=sys.stderr)
            print(msg)
            return 3
    else:
        print(msg)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
