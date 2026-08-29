# Hermes Stats launchd audit/design (mini-loop 1/6)

Дата аудита: 2026-07-27
Root: `/Users/deploydog-ai/LinkedIn`
Режим: audit/design only. Мутации: только этот markdown-файл.

## Цель

Заменить ненадёжные Hermes cron jobs для LinkedIn/Hermes Stats на прямой macOS `launchd`-вариант, который:

- каждый день в 09:30 America/Argentina/Buenos_Aires отправляет daily статус контейнеров;
- по понедельникам в 09:30 America/Argentina/Buenos_Aires отправляет weekly статус;
- не зависит от Hermes gateway, Hermes cron и LLM;
- не трогает Telegram во время аудита;
- не вызывает `launchctl` и не меняет существующие plist во время аудита.

## Обнаруженные файлы и текущие команды

### Основные Hermes Stats scripts

| Файл | Статус | Mode | Size | Mtime | Что делает |
|---|---:|---:|---:|---:|---|
| `/Users/deploydog-ai/LinkedIn/scripts/hermes_stats_daily.sh` | exists | `700` | 246 | `2026-07-24T13:44:15` | Запускает Telegram send daily, затем zero-watchdog daily |
| `/Users/deploydog-ai/LinkedIn/scripts/hermes_stats_weekly.sh` | exists | `700` | 248 | `2026-07-24T13:44:15` | Запускает Telegram send weekly, затем zero-watchdog weekly |
| `/Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py` | exists | `700` | 30242 | `2026-07-24T14:01:28` | No-LLM сбор статуса Docker/LinkedIn state, форматирование сообщения, optional Telegram send |
| `/Users/deploydog-ai/LinkedIn/scripts/hermes_zero_watchdog.py` | exists | `700` | 5604 | `2026-07-24T13:44:56` | No-LLM detector нулевого output, но может запускать repair runner |
| `/Users/deploydog-ai/LinkedIn/.hermes_stats_bot.env` | exists | `600` | 98 | `2026-07-24T13:21:16` | Telegram bot config: ключи `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` присутствуют; token не раскрывать |
| `/Users/deploydog-ai/LinkedIn/.env` | exists | `644` | 9414 | `2026-07-24T11:55:37` | Общий runtime env; safe checked keys ниже |

Текущее содержимое shell wrappers:

```bash
# hermes_stats_daily.sh
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py --period daily --send
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_zero_watchdog.py --period daily || true
```

```bash
# hermes_stats_weekly.sh
#!/usr/bin/env bash
set -euo pipefail
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py --period weekly --send
/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_zero_watchdog.py --period weekly || true
```

### Важное наблюдение по wrappers

`hermes_stats_daily.sh` и `hermes_stats_weekly.sh` сейчас не являются чисто status-only.
После отправки статуса они запускают `hermes_zero_watchdog.py`.

`hermes_zero_watchdog.py`:

- пишет state/trigger JSON в `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/watchdog`;
- без `--dry-run` может вызвать `/Users/deploydog-ai/LinkedIn/scripts/hermes_zero_repair_runner.py`;
- по смыслу это уже repair/automation loop, а не просто direct status send;
- потенциально возвращает зависимость на Hermes/LLM repair flow, если repair runner использует агента.

Вывод: для требования “status без Hermes gateway/cron/LLM” launchd job должен запускать либо:

1. напрямую `/usr/bin/python3 /Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py --period <daily|weekly> --send`; либо
2. отдельные status-only wrappers, которые не вызывают `hermes_zero_watchdog.py`.

Использовать текущие `hermes_stats_daily.sh` / `hermes_stats_weekly.sh` как ProgramArguments без изменения — рискованно и не соответствует no-LLM/no-Hermes-repair intent.

## Telegram config audit

Файл: `/Users/deploydog-ai/LinkedIn/.hermes_stats_bot.env`

Найденные ключи:

- `TELEGRAM_BOT_TOKEN`: присутствует, значение не раскрывать;
- `TELEGRAM_CHAT_ID`: присутствует, chat id задан.

Права файла: `600` — хорошо для локального secret/env файла.

`hermes_container_status_bot.py` читает config из:

```python
ROOT = Path('/Users/deploydog-ai/LinkedIn')
CONFIG_PATH = ROOT / '.hermes_stats_bot.env'
```

При `--send` он использует `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` из `.hermes_stats_bot.env` или process env.

Во время аудита Telegram send не запускался.

## Docker/LinkedIn runtime audit

`docker-compose.yml` содержит сервисы:

- `jobseeker`, container_name `linkedin-jobseeker`;
- `connectman`, container_name `linkedin-connectman`;
- `sendman`, container_name `linkedin-sendman`;
- `postliker`, container_name `linkedin-postliker`;
- `commentresponder`, container_name `linkedin-commentresponder`.

Safe `.env` keys:

```text
TZ=America/Argentina/Buenos_Aires
CRON_TEST_MODE=0
SAFE_MODE=0
```

Текущие service crontab внутри repo:

```text
JobSeeker:        0,20,40 * * * * /app/scripts/run.sh >> /shared/logs/JobSeeker/cron.log 2>&1
ConnectMan:       0 11 * * 1 /app/scripts/run.sh >> /shared/logs/ConnectMan/cron.log 2>&1
SendMan:          0 9 * * * /app/scripts/run.sh >> /shared/logs/SendMan/cron.log 2>&1
PostLiker:        0 6,18 * * * /app/scripts/run.sh >> /shared/logs/PostLiker/cron.log 2>&1
CommentResponder: 17 9,15,21 * * * /app/scripts/run.sh >> /shared/logs/CommentResponder/cron.log 2>&1
```

`docker compose ps --format json` из `/Users/deploydog-ai/LinkedIn` вернул пустой stdout с exit 0.
`docker compose ls --format json` показал running projects только:

- `defidash-grafana`;
- `honcho`;
- `local-hm-stack`.

Проект LinkedIn Compose сейчас не числится running. Поэтому dry-run статусов ниже показывает ошибки чтения cron из контейнеров `service "jobseeker"` и т.п. Это не ошибка скрипта launchd, а текущая среда: контейнеры LinkedIn, вероятно, не подняты.

## Проверка status script без Telegram

Команды, которые были выполнены безопасно, без `--send`:

```bash
cd /Users/deploydog-ai/LinkedIn
/usr/bin/python3 scripts/hermes_container_status_bot.py --period daily --dry-run
/usr/bin/python3 scripts/hermes_container_status_bot.py --period weekly --dry-run
python3 -m py_compile scripts/hermes_container_status_bot.py scripts/hermes_zero_watchdog.py
```

Результат `py_compile`: `py_compile_ok`.

Daily dry-run output:

```text
📊 Hermes Stats · daily
2026-07-27 10:54 BA · 🔥 prod env=0/0, containers=?/?

🔥 JobSeeker · cron read failed: service "jobseeker"
   app 0 · questions 0 · skip 130 · err 0
   blocks: none

🔥 ConnectMan · cron read failed: service "connectman"
   sent 0 · skip 0 · err 0 · limit no
   blocks: none

🔥 SendMan · cron read failed: service "sendman"
   msg 0 · skip 0 · err 0 · safeguard no · rate no · InMail ok/unknown
   blocks: none

🔥 PostLiker · cron read failed: service "postliker"
   verified 0/0 · skip 0 · err 0
   blocks: login×1
```

Weekly dry-run output:

```text
📊 Hermes Stats · weekly
2026-07-20..2026-07-26 · 🔥 prod env=0/0, containers=?/?

🔥 JobSeeker · cron read failed: service "jobseeker"
   app 4 · questions 82 · skip 2549 · err 212
   blocks: automation×45, questions×37

🔥 ConnectMan · cron read failed: service "connectman"
   sent 64 · skip 63 · err 0 · limit yes
   blocks: none

🔥 SendMan · cron read failed: service "sendman"
   msg 4 · skip 1850 · err 35 · safeguard no · rate no · InMail ok/unknown
   blocks: safeguard×13, login×2

🔥 PostLiker · cron read failed: service "postliker"
   verified 8/8 · skip 10 · err 0
   blocks: login×17
```

## Existing launchd audit

Repo-local plist files found only under legacy backup:

- `/Users/deploydog-ai/LinkedIn/shared/legacy_launchd_backup/20260717-154804/ai.hermes.lily.linkedin-message-outreach.plist`
- `/Users/deploydog-ai/LinkedIn/shared/legacy_launchd_backup/20260717-154804/ai.hermes.lily.linkedin-connects.plist`
- `/Users/deploydog-ai/LinkedIn/shared/legacy_launchd_backup/20260717-154804/ai.hermes.lily.easyapply.plist`

User LaunchAgents found:

- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.gateway-natasha.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.gateway-misha.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.gateway-miri.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.gateway-vitya.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.dashboard.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.gateway-lily.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.misha-watchdog.plist`
- Google updater plists

No Hermes Stats-specific active plist was found by filename scan.

Per instruction, `launchctl` was not called and no plist was modified.

## Hermes cron audit

`cronjob(action='list')` in current active Hermes profile returned:

```text
count=0
jobs=[]
```

Ограничение: это read-only audit только текущего активного Hermes profile/session visibility. Я не читал/не менял cron state других Hermes profiles.

## Git/repo audit

`/Users/deploydog-ai/LinkedIn` currently is not a Git repository:

```text
fatal: not a git repository (or any of the parent directories): .git
```

Следствие: для этого mini-loop нет git diff/status verification. Изменён только этот markdown-файл.

## Recommended launchd design

### Labels

Использовать два независимых LaunchAgent job, чтобы daily и weekly имели независимые логи и exit statuses:

- `ai.hermes.linkedin.hermes-stats-daily`
- `ai.hermes.linkedin.hermes-stats-weekly`

### Schedules

Daily:

```xml
<key>StartCalendarInterval</key>
<dict>
  <key>Hour</key><integer>9</integer>
  <key>Minute</key><integer>30</integer>
</dict>
```

Weekly Monday:

```xml
<key>StartCalendarInterval</key>
<dict>
  <key>Weekday</key><integer>1</integer>
  <key>Hour</key><integer>9</integer>
  <key>Minute</key><integer>30</integer>
</dict>
```

macOS `launchd` weekday convention: Sunday=0 or 7, Monday=1. Existing backup plist for Monday ConnectMan also uses `Weekday=1`, so this matches repo precedent.

### ProgramArguments

Recommended direct status-only command, no wrapper:

Daily:

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py</string>
  <string>--period</string>
  <string>daily</string>
  <string>--send</string>
</array>
```

Weekly:

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/bin/python3</string>
  <string>/Users/deploydog-ai/LinkedIn/scripts/hermes_container_status_bot.py</string>
  <string>--period</string>
  <string>weekly</string>
  <string>--send</string>
</array>
```

Reason: this avoids `hermes_zero_watchdog.py` and therefore avoids repair-loop/LLM side effects.

### WorkingDirectory

```xml
<key>WorkingDirectory</key>
<string>/Users/deploydog-ai/LinkedIn</string>
```

### EnvironmentVariables

Minimum reliable env:

```xml
<key>EnvironmentVariables</key>
<dict>
  <key>TZ</key>
  <string>America/Argentina/Buenos_Aires</string>
  <key>PATH</key>
  <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
</dict>
```

Reason:

- script calls `/usr/bin/python3` directly;
- Docker CLI may live in `/usr/local/bin` or `/opt/homebrew/bin` depending on install;
- Bot config is loaded from absolute `.hermes_stats_bot.env`, so Telegram token/chat do not need to be embedded in plist.

### Logs

Recommended log dir:

- `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/`

Recommended log files:

- daily stdout: `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/daily.out.log`
- daily stderr: `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/daily.err.log`
- weekly stdout: `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/weekly.out.log`
- weekly stderr: `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/weekly.err.log`

Note: launchd will not create missing parent directories reliably. Directory creation should be a separate implementation artifact/step before loading jobs.

### Plist target paths

Recommended active plist paths:

- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-daily.plist`
- `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-weekly.plist`

Optional repo copies/templates for review:

- `/Users/deploydog-ai/LinkedIn/ops/launchd/ai.hermes.linkedin.hermes-stats-daily.plist`
- `/Users/deploydog-ai/LinkedIn/ops/launchd/ai.hermes.linkedin.hermes-stats-weekly.plist`

## Exhaustive Definition of Done

### Scope/behavior DoD

- [ ] There are exactly two direct macOS LaunchAgent jobs for Hermes Stats: daily and weekly.
- [ ] Daily job runs every calendar day at 09:30 local Buenos Aires time.
- [ ] Weekly job runs only on Mondays at 09:30 local Buenos Aires time.
- [ ] Jobs do not depend on Hermes gateway.
- [ ] Jobs do not depend on Hermes cron.
- [ ] Jobs do not call an LLM.
- [ ] Jobs do not call Hermes agent CLI, Hermes gateway API, `cronjob`, or any profile-specific Hermes repair script.
- [ ] Jobs send Telegram messages through direct Telegram Bot API call implemented by `hermes_container_status_bot.py`.
- [ ] Jobs use only public/local operational state and Telegram bot credentials already in `.hermes_stats_bot.env`.
- [ ] Jobs do not embed Telegram token in plist.
- [ ] Jobs work after reboot/user login without opening a terminal.

### File/artifact DoD

- [ ] Directory exists: `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd`.
- [ ] Active plist exists: `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-daily.plist`.
- [ ] Active plist exists: `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-weekly.plist`.
- [ ] Optional repo template/copy exists under `/Users/deploydog-ai/LinkedIn/ops/launchd/` for both jobs.
- [ ] Plists have valid XML/plist syntax (`plutil -lint`).
- [ ] Plists have `Label` matching filename/job name.
- [ ] Plists have absolute `ProgramArguments`; no shell expansion needed.
- [ ] Plists set `WorkingDirectory=/Users/deploydog-ai/LinkedIn`.
- [ ] Plists set `TZ=America/Argentina/Buenos_Aires`.
- [ ] Plists set PATH containing Docker CLI locations and system dirs.
- [ ] Plists write stdout/stderr to stable files under `shared/logs/HermesStats/launchd`.
- [ ] Existing unrelated LaunchAgents are untouched.
- [ ] Existing legacy backup plists are untouched.

### Script compatibility DoD

- [ ] `python3 -m py_compile scripts/hermes_container_status_bot.py` passes.
- [ ] Direct dry-run daily works locally without Telegram send:
  `cd /Users/deploydog-ai/LinkedIn && /usr/bin/python3 scripts/hermes_container_status_bot.py --period daily --dry-run`.
- [ ] Direct dry-run weekly works locally without Telegram send:
  `cd /Users/deploydog-ai/LinkedIn && /usr/bin/python3 scripts/hermes_container_status_bot.py --period weekly --dry-run`.
- [ ] `--send` path is verified once only when explicitly allowed, to avoid accidental Telegram spam.
- [ ] If Docker Compose project is not running, output clearly reports container read failures instead of crashing.
- [ ] If Telegram config is missing, script exits non-zero and prints actionable error.
- [ ] If Telegram API returns error, launchd stderr captures it.

### Launchd verification DoD

Implementation loop should verify with read-only/status commands after creating/loading jobs:

- [ ] `plutil -lint <daily.plist> <weekly.plist>` returns OK.
- [ ] `launchctl print gui/$(id -u)/ai.hermes.linkedin.hermes-stats-daily` shows loaded job.
- [ ] `launchctl print gui/$(id -u)/ai.hermes.linkedin.hermes-stats-weekly` shows loaded job.
- [ ] `launchctl print` shows `program arguments` matching direct Python status command.
- [ ] `launchctl print` shows `calendar interval` matching 09:30 daily / Monday 09:30 weekly.
- [ ] Manual one-shot test via `launchctl kickstart -k gui/$(id -u)/<label>` is done only after explicit approval because it sends Telegram.
- [ ] After approved kickstart, daily/weekly stdout log receives `sent ok: message_id=... period=...`.
- [ ] After approved kickstart, stderr log is empty or contains no fatal error.
- [ ] Telegram message appears in expected chat.
- [ ] Jobs survive unload/load or reboot/login.

### Reliability DoD

- [ ] No dependency on interactive shell rc files.
- [ ] No dependency on current terminal cwd.
- [ ] No dependency on active Hermes session/profile.
- [ ] No dependency on Hermes process uptime.
- [ ] Docker CLI path is available to launchd environment.
- [ ] Parent log directory exists before load.
- [ ] Failure modes are visible in stderr log.
- [ ] Job labels are unique and not colliding with existing LaunchAgents.
- [ ] Weekly and daily are separate jobs, so one failing does not block the other.
- [ ] There is a documented rollback: unload/remove the two new LaunchAgents only.

### Safety DoD

- [ ] No Telegram send during audit/design loop.
- [ ] No `launchctl` during audit/design loop.
- [ ] No plist mutation during audit/design loop.
- [ ] No secret values copied into docs, plist, logs, or final response.
- [ ] No changes to LinkedIn service crontabs in this loop.
- [ ] No changes to Docker Compose in this loop.
- [ ] No repair runner / zero-watchdog execution in this loop.

## Artifact list for next implementation loops

Required artifacts:

1. `/Users/deploydog-ai/LinkedIn/shared/logs/HermesStats/launchd/`
   - directory for launchd stdout/stderr logs.

2. `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-daily.plist`
   - active daily LaunchAgent.

3. `/Users/deploydog-ai/Library/LaunchAgents/ai.hermes.linkedin.hermes-stats-weekly.plist`
   - active weekly LaunchAgent.

4. Optional repo templates:
   - `/Users/deploydog-ai/LinkedIn/ops/launchd/ai.hermes.linkedin.hermes-stats-daily.plist`
   - `/Users/deploydog-ai/LinkedIn/ops/launchd/ai.hermes.linkedin.hermes-stats-weekly.plist`

5. Optional operational doc:
   - `/Users/deploydog-ai/LinkedIn/docs/hermes-stats-launchd-runbook.md`
   - contents: install/load/unload/test/rollback commands, expected logs, failure troubleshooting.

## Blockers / risks before implementation

1. Current wrappers include `hermes_zero_watchdog.py`; do not use them as-is for no-LLM/no-Hermes status-only launchd jobs.
2. LinkedIn Docker Compose project is not currently running, so live container health in dry-run is `?/?` with cron read failures.
3. `/Users/deploydog-ai/LinkedIn` is not a Git repo, so changes cannot be verified with git diff/status unless this root is intentionally non-git.
4. Any `launchctl kickstart` after implementation will send Telegram if job uses `--send`; requires explicit approval.
5. launchd environment is minimal; PATH must include Docker CLI location or script will report Docker command failures.

## Recommended next safe step

Implementation loop 2 should only create local artifacts, not load them yet:

- create log directory;
- create repo plist templates under `/Users/deploydog-ai/LinkedIn/ops/launchd/`;
- optionally copy to `~/Library/LaunchAgents/` only if approved for plist mutation;
- validate with `plutil -lint`;
- do not `launchctl bootstrap/load/kickstart` until a later approved loop.
