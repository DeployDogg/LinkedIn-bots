#!/usr/bin/env python3
"""Run Lily repair jobs for zero-output LinkedIn container alerts.

This script is intentionally separate from the no-LLM reporter/watchdog so the
cron job can spawn it in the background and return quickly. It calls Hermes Lily
with --yolo, then posts Lily's final summary to Telegram Hermes Stats.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/Users/deploydog-ai/LinkedIn')
CONFIG_PATH = ROOT / '.hermes_stats_bot.env'
LOG_DIR = ROOT / 'shared/logs/HermesStats/repairs'
BA = ZoneInfo('America/Argentina/Buenos_Aires')

SERVICE_HINTS = {
    'jobseeker': {
        'title': 'JobSeeker',
        'metric': 'applications/app == 0',
        'paths': [
            '/Users/deploydog-ai/LinkedIn/services/JobSeeker',
            '/Users/deploydog-ai/LinkedIn/shared/legacy_state/jobseeker/li_apply_platform_full_progress.json',
            '/Users/deploydog-ai/LinkedIn/shared/logs/JobSeeker/',
        ],
        'goal': 'Понять, почему JobSeeker за отчётный период дал app==0: это реальная причина пайплайна, поломка выполнения контейнера или ошибка подсчёта отчёта. Исправить автономно и проверить.',
    },
    'sendman': {
        'title': 'SendMan',
        'metric': 'messages/msg == 0',
        'paths': [
            '/Users/deploydog-ai/LinkedIn/services/SendMan',
            '/Users/deploydog-ai/LinkedIn/shared/message_state/linkedin_message_outreach_status.json',
            '/Users/deploydog-ai/LinkedIn/shared/message_state/logs/',
            '/Users/deploydog-ai/LinkedIn/shared/logs/SendMan/',
        ],
        'goal': 'Понять, почему SendMan за отчётный период дал msg==0: контейнер не отправлял, LinkedIn блок/лимит, UI/selector проблема или ошибка подсчёта отчёта. Исправить автономно и проверить.',
    },
    'postliker': {
        'title': 'PostLiker',
        'metric': 'likes verified == 0',
        'paths': [
            '/Users/deploydog-ai/LinkedIn/services/PostLiker',
            '/Users/deploydog-ai/LinkedIn/shared/legacy_state/liked_posts/linkedin_liked_posts_status.json',
            '/Users/deploydog-ai/LinkedIn/shared/legacy_state/liked_posts/',
            '/Users/deploydog-ai/LinkedIn/shared/logs/PostLiker/',
        ],
        'goal': 'Понять, почему PostLiker за отчётный период дал verified==0: лайки не выполнялись, не верифицировались или сломан подсчёт отчёта. Исправить автономно и проверить.',
    },
    'connectman': {
        'title': 'ConnectMan',
        'metric': 'weekly connects sent == 0',
        'paths': [
            '/Users/deploydog-ai/LinkedIn/services/ConnectMan',
            '/Users/deploydog-ai/LinkedIn/shared/legacy_state/outreach_run_status.json',
            '/Users/deploydog-ai/LinkedIn/shared/logs/ConnectMan/',
        ],
        'goal': 'Понять, почему ConnectMan за недельный отчёт дал sent==0: не запускался cron, weekly limit/LinkedIn блок, нет кандидатов, auth/selector проблема или ошибка подсчёта отчёта. Исправить автономно и проверить.',
    },
}


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


def send_telegram(text: str) -> None:
    cfg = load_env(CONFIG_PATH)
    token = cfg.get('TELEGRAM_BOT_TOKEN') or os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = cfg.get('TELEGRAM_CHAT_ID') or os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        raise RuntimeError('TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing')
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = urllib.parse.urlencode({'chat_id': chat_id, 'text': text[:3900], 'disable_web_page_preview': 'true'}).encode('utf-8')
    with urllib.request.urlopen(urllib.request.Request(url, data=data, method='POST'), timeout=30) as resp:
        json.loads(resp.read().decode('utf-8'))


def compact_final(raw: str, service_title: str) -> str:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    # Prefer a line Lily explicitly formatted for the channel.
    for ln in reversed(lines):
        if 'Lily fix' in ln or 'Лили fix' in ln or ln.startswith('🛠'):
            return ln[:3500]
    tail = ' '.join(lines[-10:]) if lines else 'Hermes finished without visible output.'
    tail = ' '.join(tail.split())[:1200]
    return f'🛠 Lily fix · {service_title}: {tail}'


def build_prompt(service: str, period: str, trigger: str, report: str) -> str:
    info = SERVICE_HINTS[service]
    paths = '\n'.join(f'- {p}' for p in info['paths'])
    return textwrap.dedent(f'''
    Ты Лили, профиль lily. Нужно автономно диагностировать и исправить zero-output alarm LinkedIn контейнера.

    Контейнер: {info['title']}
    Отчётный период: {period}
    Триггер: {trigger}
    Метрика тревоги: {info['metric']}

    Задача:
    {info['goal']}

    Рабочий root: /Users/deploydog-ai/LinkedIn
    Важные пути:
    {paths}

    Правила:
    - Работай автономно с --yolo; Андрей разрешил чинить всё автономно для этого watchdog.
    - Сначала установи root cause: контейнер не выполняет функцию, LinkedIn блок/лимит, cron/auth/browser/selector проблема или ошибка подсчёта stats script.
    - Если проблема в подсчёте отчёта — исправь /Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py или related watchdog scripts и проверь dry-run.
    - Если проблема в контейнере — исправь сервис/cron/config/code, проверь docker compose ps, logs, и минимальный безопасный canary/verification.
    - На captcha/security/rate-limit/safeguard/login докладывай честно; не обходи security challenge.
    - Не сбрасывай LinkedIn browser profile/session без крайней необходимости; если нужно, сначала докажи почему.
    - В конце дай ОДНУ короткую строку для Telegram в формате:
      🛠 Lily fix · {info['title']}: root cause <...> · fixed <...> · verified <...>

    Последний отчёт:
    {report}
    ''').strip()


def run_hermes(service: str, period: str, trigger: str, report: str) -> tuple[int, str]:
    prompt = build_prompt(service, period, trigger, report)
    cmd = [
        'hermes', '-p', 'lily', 'chat', '--yolo', '-Q',
        '-s', 'pochemuchka,hermes-agent,linkedin-outreach-automation',
        '--source', 'hermes-stats-watchdog',
        '--max-turns', '80',
        '-q', prompt,
    ]
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60 * 60 * 2,
    )
    return proc.returncode, proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--period', required=True, choices=['daily', 'weekly'])
    ap.add_argument('--services', required=True, help='Comma-separated service ids')
    ap.add_argument('--trigger-file', required=True)
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    trigger_path = Path(args.trigger_file)
    trigger_data = json.loads(trigger_path.read_text(encoding='utf-8'))
    report = trigger_data.get('report', '')
    triggers = trigger_data.get('triggers', {})

    for service in [s.strip() for s in args.services.split(',') if s.strip()]:
        info = SERVICE_HINTS.get(service)
        if not info:
            continue
        ts = datetime.now(BA).strftime('%Y%m%d_%H%M%S')
        log_path = LOG_DIR / f'{ts}_{args.period}_{service}.log'
        trigger = triggers.get(service, 'zero-output alarm')
        try:
            rc, out = run_hermes(service, args.period, trigger, report)
            log_path.write_text(out, encoding='utf-8')
            final = compact_final(out, info['title'])
            if rc != 0:
                final = f'🛠 Lily fix · {info["title"]}: Hermes exited {rc} · details {log_path}'
            send_telegram(final)
        except Exception as e:
            err = f'🛠 Lily fix · {info["title"]}: repair runner failed · {type(e).__name__}: {e}'
            try:
                send_telegram(err)
            finally:
                (LOG_DIR / f'{ts}_{args.period}_{service}_runner_error.log').write_text(err, encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
