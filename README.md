# LinkedIn Docker Services

Services:
- JobSeeker: LinkedIn Easy Apply pipeline.
- ConnectMan: LinkedIn connection requests.
- SendMan: LinkedIn message outreach.
- PostLiker: LinkedIn feed likes.
- Commentator: scans new comments under Andrew's posts, drafts Andrew-style replies, sends Telegram approvals, and can publish approved replies.

Common shared volumes live under ./shared.
Production worker cadence is owned by the central host-side scheduler; service
crontabs are no-action placeholders while `CENTRAL_SCHEDULER_MODE=1`.
Set CRON_TEST_MODE=1 and SAFE_MODE=1 in .env to verify cron firing without LinkedIn actions.

## Configuration in .env

Operator-only configuration lives in `/Users/deploydog-ai/LinkedIn/.env`. Worker containers do not receive this file directly.

- Common runtime: `TZ`, `CRON_TEST_MODE`, `SAFE_MODE`, `LINKEDIN_*_DIR`, browser args, viewport, stop patterns.
- JobSeeker: search URL, extractor pagination/delay, progress/source paths, contact fields, resume matrix, role/location regexes.
- ConnectMan: people-search URL, allowed countries, filters path, caps, pacing.
- SendMan: people-search URLs, job labels, subject/body templates, Calendly URL, blocked names/location regex, caps, pacing.
- PostLiker: feed URL, max likes, output paths, pacing.
- Commentator: hourly comment scan, Telegram approval toggles, approve→publish toggles, max candidates.

### Central scheduler task timeouts

The host scheduler reads only the five timeout keys below from `.env`; it does
not import worker credentials or other secrets into its environment. Existing
process environment values take precedence. Missing, invalid, and non-positive
values use these defaults:

| Task | Environment variable | Default |
| --- | --- | ---: |
| PostLiker | `LINKEDIN_SCHEDULER_POSTLIKER_TIMEOUT_SECONDS` | 900 (15m) |
| ConnectMan | `LINKEDIN_SCHEDULER_CONNECTMAN_TIMEOUT_SECONDS` | 900 (15m) |
| SendMan | `LINKEDIN_SCHEDULER_SENDMAN_TIMEOUT_SECONDS` | 1200 (20m) |
| Commentator | `LINKEDIN_SCHEDULER_COMMENTATOR_TIMEOUT_SECONDS` | 900 (15m) |
| JobSeeker | `LINKEDIN_SCHEDULER_JOBSEEKER_TIMEOUT_SECONDS` | 2700 (45m) |

Each worker is started in its own process group. At the hard timeout the host
scheduler sends `SIGTERM` to the whole group, waits 5 seconds, then sends
`SIGKILL` if needed. State records `status=failed`, `exit_code=124`,
`reason=task_timeout`, and `timeout_seconds`; the queue then attempts the next
task. LinkedIn safety blocker exits `11`/`12` still stop the queue immediately.
A worker-level shared-lock skip still records `scheduler_lock_skip`, exits 75,
and stops the current queue so the skipped task remains due.

The schedule itself is unchanged, including Commentator hourly at minute `:17`.
Commentator is the first-priority fixed task (and runs first in manual `run-all`)
to protect its hourly cadence from avoidable delay behind other fixed tasks. The
queue remains serial and protected by the existing host/shared worker locks.

## Commentator production rollout

Keep these defaults until Андрей explicitly authorizes external actions:

```dotenv
LINKEDIN_COMMENTATOR_DRY_RUN=1
LINKEDIN_COMMENTATOR_SEND_TELEGRAM=0
LINKEDIN_COMMENTATOR_PUBLISH_APPROVED=0
LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING=1
```

Local readiness check (never opens CDP, invokes Codex, calls Telegram, or publishes):

```bash
docker compose exec -T commentator python3 /app/scripts/linkedin_commentator.py --preflight --dry-run
```

Staged rollout after separate explicit authorization:

1. Stage A — full scan in dry-run: `DRY_RUN=1`, `SEND_TELEGRAM=0`, `PUBLISH_APPROVED=0`, publishing paused. Verify clean drafts/status and `telegram_sent=0`, `published=0`.
2. Stage B — Telegram approvals only: `DRY_RUN=0`, `SEND_TELEGRAM=1`, `PUBLISH_APPROVED=0`, publishing paused. Verify one approval round-trip; LinkedIn publish remains impossible.
3. Stage C — one canary: set a small limit, remove publishing pause, enable `PUBLISH_APPROVED=1`, and publish exactly one explicitly approved item. Verify the LinkedIn DOM post-publish check and persisted `published` state before continuing.
4. Stage D — hourly production at minute 17 via the central scheduler: restore normal limits and verify two scheduler ticks, fresh status, no duplicate approval/publish, and no alert artifact.

Emergency controls:

- Stop everything before browser/Telegram/publish: set `LINKEDIN_COMMENTATOR_PAUSE_ALL=1` or create `/shared/comment_state/PAUSE_ALL`.
- Stop publishing while retaining scan/drafts/approvals: set `LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING=1` or create `/shared/comment_state/PAUSE_PUBLISHING`.
- Rollback: `touch shared/comment_state/PAUSE_PUBLISHING && docker compose restart commentator`. This does not reset state.
- Local alerts are written atomically to `/shared/comment_state/linkedin_commentator_alert.json`; no external alert transport is enabled.

State is never reset by rollout. Writes use fsync + atomic replace and retain `linkedin_commentator_state.json.bak`. Corrupt production state fails closed and creates a local alert instead of silently starting from empty state.

Workers use generated `/Users/deploydog-ai/LinkedIn/.env.workers`, which excludes LinkedIn credentials and legacy session/profile/login/CDP overrides. Regenerate it after changing `.env`:

```bash
cd /Users/deploydog-ai/LinkedIn
python3 scripts/generate_worker_env.py
docker compose config
docker compose build
docker compose up -d --force-recreate
```

`docker-compose.yml` sets worker `LINKEDIN_CDP_ENDPOINT` centrally to `http://linkedin-browser:9222`; do not put worker CDP, login URL, session path, or Chromium profile path overrides in `.env`/`.env.example`.

For scheduler-only validation without LinkedIn actions set `CRON_TEST_MODE=1` and `SAFE_MODE=1`, recreate containers, wait for cron probe stamps, then switch both back to `0`.

## LinkedIn auth owner: linkedin-browser

Login is manual only. Workers must not auto-login, read credentials, or write Playwright storage state. The persistent Chromium profile mounted at `/profile` in `linkedin-browser` is the primary session state. The JSON snapshot at `/session-backup/linkedin_session.json` is a secondary backup owned only by the browser service.

Manual flow:

```bash
cd /Users/deploydog-ai/LinkedIn
# Open localhost-only noVNC and complete LinkedIn login in the persistent Chromium:
open http://127.0.0.1:6080/vnc.html

# After the feed is visible, verify and export the browser-owned backup snapshot:
docker compose exec linkedin-browser python3 /app/scripts/central_auth.py
```

The browser entrypoint opens `https://www.linkedin.com/feed/` by default. Override with `LINKEDIN_BROWSER_START_URL` only if you need a different start page. `central_auth.py` connects to local CDP at `127.0.0.1:9222`, fails closed with exit `11` for login/authwall and exit `12` for captcha/checkpoint/challenge/security/rate-limit/safeguard/limit blockers.


### Logging layout
- Per service cumulative log: `/Users/deploydog-ai/LinkedIn/shared/logs/<Service>/all-time.log`
- Per service daily logs: `/Users/deploydog-ai/LinkedIn/shared/logs/<Service>/daily/YYYY-MM-DD.log`
- Retention: keep the last 3 days, older daily logs are pruned automatically on service start
