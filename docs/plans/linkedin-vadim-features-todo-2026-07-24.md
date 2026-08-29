# LinkedIn Vadim Features Integration TODO

Дата составления: 2026-07-24 13:12 -03
Рабочая папка: `/Users/deploydog-ai/LinkedIn`
Источник идей: `/Users/deploydog-ai/vadim/`
Исполнитель кода: Cody через `hermes -p cody .`
Проверяющая: Лили

Важно:
- `/Users/deploydog-ai/LinkedIn` сейчас не git repo: `git status` вернул `fatal: not a git repository`.
- Тестов `test_*.py` / `*_test.py` в проекте не найдено.
- Поэтому Definition of Done опирается на: `python3 -m py_compile`, `bash -n`, `docker compose config`, rebuild/recreate нужного контейнера, safe-mode/dry-run/canary run, проверка логов/status JSON/exit code/health.
- Нельзя продолжать реальный LinkedIn action-run, если появился captcha/security/checkpoint/safeguard/rate-limit/daily-limit. Нужно остановиться, собрать фактуру и вернуть задачу Cody.

---

## Общий цикл для каждого пункта

### 0. Preflight перед запуском Cody

Команды Лили:

```bash
cd /Users/deploydog-ai/LinkedIn
pwd
docker compose ps
find /Users/deploydog-ai/vadim -maxdepth 1 -type f -name '*.md' -print
```

Зафиксировать текущие baseline-файлы:

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile \
  services/JobSeeker/scripts/linkedin_auth.py \
  services/JobSeeker/scripts/linkedin_extractor.py \
  services/JobSeeker/scripts/linkedin_worker.py \
  services/ConnectMan/scripts/linkedin_outreach.py \
  services/SendMan/scripts/linkedin_message_outreach.py \
  services/PostLiker/scripts/linkedin_like_posts.py
bash -n services/*/scripts/run.sh services/*/entrypoint.sh services/*/scripts/healthcheck.sh
docker compose config >/tmp/linkedin-compose-config.txt
```

### 1. Запуск Cody на конкретный пункт

Запускать из корня LinkedIn:

```bash
cd /Users/deploydog-ai/LinkedIn
hermes -p cody .
```

В интерактивный Cody вставлять prompt конкретного пункта из разделов ниже.

Для автоматического запуска из Лили допустимый эквивалент, всё равно через профиль Cody:

```bash
cd /Users/deploydog-ai/LinkedIn
hermes -p cody chat -q "$(cat docs/plans/cody-prompt-POINT-N.md)" --max-turns 90
```

Если интерактивный `hermes -p cody .` не стартует из-за CLI-синтаксиса, использовать именно `hermes -p cody chat -q ...`: это тот же профиль `cody`, но без ручной вставки prompt.

Правила для Cody в каждом prompt:
- Делать только один пункт за раз.
- Не запускать массовые реальные LinkedIn actions.
- Сначала self-tests / safe-mode / dry-run.
- Сохранять совместимость Docker Compose.
- Не удалять и не сбрасывать Chrome/session/profile/state.
- Не трогать credentials.
- Все новые режимы должны быть выключены по умолчанию или безопасны по умолчанию.
- Все operational knobs — через `.env` / env vars, с дефолтами в коде и `.env.example`, если файл есть.
- Логи/status JSON должны быть понятными для Лили.

### 2. Что Cody должен вернуть

Cody обязан отчитаться:
- какие файлы изменил/создал;
- какие команды запускал;
- какие exit codes получил;
- где status/log/report артефакты;
- какой exact command Лили должна выполнить для одноразовой проверки;
- что является expected success.

### 3. Проверка Лили после отчёта Cody

Минимальный общий набор:

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile services/*/scripts/*.py
bash -n services/*/scripts/*.sh services/*/entrypoint.sh
docker compose config >/tmp/linkedin-compose-config.txt
docker compose build <service>
docker compose up -d <service>
docker compose ps <service>
```

После одноразового запуска нужного бота:

```bash
docker compose ps <service>
docker compose logs --tail=120 <service>
tail -n 120 shared/logs/<Service>/run.log
```

Проверить:
- container `running` и `healthy`;
- exit code команды проверки 0;
- нет traceback/exception/crash loop;
- нет LinkedIn blocker: captcha/security/checkpoint/safeguard/rate-limit/daily-limit;
- status JSON обновился и валидно парсится;
- отчёт/артефакт создан там, где обещал Cody.

### 4. Если есть ошибка или blocker

Собрать фактуру и вернуть Cody тот же пункт:

```text
Cody, пункт <N> не принят. Фактура:
- команда проверки:
- exit code:
- docker compose ps:
- log tail:
- status JSON excerpt:
- screenshots/block path, если есть:
- ожидаемое поведение:
- фактическое поведение:
Исправь только этот пункт, не расширяй scope. После фикса снова дай команды проверки.
```

Повторять цикл до чистого результата.

### 5. Финальный отчёт Лили Андрею

По каждому пункту:
- implemented: да/нет;
- files changed;
- verification command;
- result counts/status;
- container health;
- blockers: нет/есть;
- что осталось выключено/требует ручного approve.

---

# Пункт 1. CommentResponder dry-run/approval

## Цель

Встроить новую безопасную функцию из `vadim/linkedin-comment-responder.md`: находить LinkedIn notifications/comments, где нужен ответ, но по умолчанию НЕ отправлять публичные ответы. Первый этап — dry-run/approval workflow.

## Архитектура

Рекомендуемо: новый сервис `CommentResponder`, отдельный контейнер рядом с `PostLiker`/`SendMan`.

Новые/изменяемые пути:
- Create: `services/CommentResponder/Dockerfile`
- Create: `services/CommentResponder/entrypoint.sh`
- Create: `services/CommentResponder/crontab`
- Create: `services/CommentResponder/crontab.test`
- Create: `services/CommentResponder/scripts/run.sh`
- Create: `services/CommentResponder/scripts/healthcheck.sh`
- Create: `services/CommentResponder/scripts/logging.sh`
- Create: `services/CommentResponder/scripts/linkedin_comment_responder.py`
- Modify: `docker-compose.yml`
- Modify if exists: `.env.example`
- State/report paths:
  - `/Users/deploydog-ai/LinkedIn/shared/comment_state/linkedin_comment_responder_status.json`
  - `/Users/deploydog-ai/LinkedIn/shared/comment_state/linkedin_comment_responder_drafts.md`
  - `/Users/deploydog-ai/LinkedIn/shared/comment_state/logs/`
  - `/Users/deploydog-ai/LinkedIn/shared/screenshots/` for blockers/screenshots

## Required behavior

1. Default mode must be dry-run.
   - Env: `LINKEDIN_COMMENT_DRY_RUN=1` default.
   - In dry-run: collect candidates and draft replies, but do not click Reply/Submit.

2. Scan these notification pages:
   - `https://www.linkedin.com/notifications/?filter=my_posts_all`
   - `https://www.linkedin.com/notifications/?filter=mentions`
   - optionally all notifications: `https://www.linkedin.com/notifications/?filter=all`

3. Time window:
   - default last 3 days.
   - Env: `LINKEDIN_COMMENT_LOOKBACK_DAYS=3`.

4. Candidate classes:
   - comments on Andrew's own posts without Andrew reply;
   - replies to Andrew's comments without Andrew follow-up.

5. Skip rules:
   - skip already replied threads;
   - skip older than lookback;
   - skip short confirmations: `agree`, `cool`, `nice`, `true`, `exactly`, `+1`, `this`, `100%`, similar;
   - skip if page has captcha/checkpoint/security/safeguard/rate-limit/login blocker.

6. Draft style:
   - Russian or English should match original comment language.
   - 1–3 sentences.
   - Friendly, peer tone.
   - Specific to the comment.
   - No emojis unless commenter used emojis.
   - No job/referral/opening asks.

7. Approval workflow:
   - Dry-run creates Markdown draft report with candidate rows and suggested reply.
   - Real send requires explicit env/CLI opt-in:
     - `LINKEDIN_COMMENT_DRY_RUN=0`
     - and `LINKEDIN_COMMENT_APPROVED_DRAFTS_PATH=<path>` or `--approved-drafts <path>`.
   - If approved mode is not implemented fully now, acceptable MVP: dry-run only plus clear status `approval_required`.

8. Stop handling:
   - Stop immediately on captcha/security/checkpoint/safeguard/rate-limit/daily-limit.
   - Save screenshot.
   - status JSON `stop_reason` and `block_screenshot`.
   - exit 12.

9. Docker:
   - Add service `commentresponder` to `docker-compose.yml`.
   - Mount common state/logs/screenshots/resumes if needed; add `./shared/comment_state` volume.
   - healthcheck same supercronic liveness pattern.
   - safe mode: `SAFE_MODE=1` runs py_compile only and no LinkedIn actions.

## Prompt to Cody for пункт 1

```text
Ты Cody. Рабочая папка: /Users/deploydog-ai/LinkedIn.

Сделай только пункт 1: CommentResponder dry-run/approval, встроив идею из /Users/deploydog-ai/vadim/linkedin-comment-responder.md в нашу Docker Compose архитектуру LinkedIn.

Контекст текущей архитектуры:
- docker-compose.yml уже содержит services: jobseeker, connectman, sendman, postliker.
- Общие логи: /shared/logs/<Service>/run.log + all-time/daily через scripts/logging.sh.
- Общий session storage: /Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_session.json.
- Нельзя удалять/сбрасывать profiles/session/state/credentials.
- Нужно stop immediately on captcha/security/checkpoint/safeguard/rate-limit/daily-limit with screenshot + exit 12.

Требования:
1. Создай новый service CommentResponder:
   - services/CommentResponder/Dockerfile
   - services/CommentResponder/entrypoint.sh
   - services/CommentResponder/crontab
   - services/CommentResponder/crontab.test
   - services/CommentResponder/scripts/run.sh
   - services/CommentResponder/scripts/healthcheck.sh
   - services/CommentResponder/scripts/logging.sh
   - services/CommentResponder/scripts/linkedin_comment_responder.py
2. Добавь service `commentresponder` в docker-compose.yml.
3. По умолчанию только dry-run: LINKEDIN_COMMENT_DRY_RUN=1.
4. Скрипт должен сканировать LinkedIn notifications за последние 3 дня:
   - /notifications/?filter=my_posts_all
   - /notifications/?filter=mentions
   - /notifications/?filter=all, если нужно
5. Скрипт должен находить candidates where reply is useful, skip short confirmations, skip already replied/old items.
6. Скрипт должен сохранять:
   - shared/comment_state/linkedin_comment_responder_status.json
   - shared/comment_state/linkedin_comment_responder_drafts.md
7. Никаких публичных Reply/Submit в dry-run.
8. SAFE_MODE=1 должен делать только py_compile и exit 0.
9. Добавь CLI args: --dry-run, --max-items, --lookback-days, --no-delay.
10. Если реальный approved send не успеваешь сделать безопасно — оставь только dry-run и status stop_reason=approval_required для send-mode, это ок.
11. В конце запусти проверки:
    - python3 -m py_compile services/CommentResponder/scripts/linkedin_comment_responder.py
    - bash -n services/CommentResponder/scripts/run.sh services/CommentResponder/entrypoint.sh services/CommentResponder/scripts/healthcheck.sh
    - docker compose config
    - docker compose build commentresponder
    - CRON_TEST_MODE=1 SAFE_MODE=1 docker compose up -d commentresponder && docker compose ps commentresponder
    - One dry-run canary with max 3 items, no send.
12. Верни отчёт: files changed, commands run, exit codes, status/report paths, exact command for Lily verification.

Не трогай пункты 2 и 3.
```

## Проверка Лили для пункта 1

После отчёта Cody:

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile services/CommentResponder/scripts/linkedin_comment_responder.py
bash -n services/CommentResponder/scripts/run.sh services/CommentResponder/entrypoint.sh services/CommentResponder/scripts/healthcheck.sh
docker compose config >/tmp/linkedin-compose-config.txt
docker compose build commentresponder
CRON_TEST_MODE=1 SAFE_MODE=1 docker compose up -d commentresponder
sleep 5
docker compose ps commentresponder
docker compose logs --tail=120 commentresponder
```

Dry-run one-shot, без отправки:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_COMMENT_DRY_RUN=1 \
  -e LINKEDIN_COMMENT_MAX_ITEMS=3 \
  -e LINKEDIN_COMMENT_NO_DELAY=1 \
  commentresponder
```

Accept criteria:
- exit 0 or controlled exit 11 if session/login missing; 12 only if real LinkedIn blocker found;
- no traceback;
- container healthy after recreate;
- status JSON parses;
- drafts Markdown exists;
- no Reply/Submit happened in dry-run;
- if blocker appeared, screenshot path exists and task returns to Cody.

---

# Пункт 2. Varied reaction + promoted skip в PostLiker

## Цель

Улучшить существующий `PostLiker`: брать из `vadim/linkedin-ssi-boost.md` и `vadim/weekly-linkedin-ssi-boost.md` varied reactions и skip promoted/sponsored, сохранив контейнерную безопасность.

## Текущие файлы

- Modify: `services/PostLiker/scripts/linkedin_like_posts.py`
- Modify if needed: `services/PostLiker/scripts/run.sh`
- Modify if needed: `.env.example`
- Existing status:
  - `/Users/deploydog-ai/LinkedIn/shared/legacy_state/liked_posts/linkedin_liked_posts_status.json`

## Required behavior

1. Promoted/sponsored skip:
   - If post card text has `Promoted`, `Sponsored`, localized obvious markers — skip with reason `promoted_or_sponsored`.

2. Already reacted skip:
   - Preserve existing skip if already reacted.

3. Varied reactions:
   - Supported reaction candidates: Like, Celebrate, Support, Love, Insightful, Funny.
   - Env: `LINKEDIN_LIKE_REACTION_MODE`:
     - `like_only` default initially for conservative safety, OR `varied` if Андрей explicitly wants production varied.
   - Env: `LINKEDIN_LIKE_ALLOWED_REACTIONS_JSON`, default list all above.
   - Selection heuristic:
     - achievements/certification/new role -> Celebrate.
     - struggles/job seeking/help/support -> Support.
     - technical insight/deep professional content -> Insightful.
     - genuinely funny/humor/meme -> Funny.
     - warm personal/professional milestone -> Love or Celebrate.
     - fallback -> Like.
   - If reaction picker fails, fallback to Like and record `fallback_to_like`.

4. No comments in this пункт.

5. Status JSON must include for each liked post:
   - selected_reaction;
   - reason/category;
   - fallback flag if any;
   - promoted skip count.

6. Verification screenshots stay.

7. Stop handling unchanged.

8. Canaries:
   - `--max-likes 1 --no-verify` with `LINKEDIN_LIKE_REACTION_MODE=like_only`.
   - optional dry/canary for varied mode with max 1, but if this does a real reaction, keep tiny and stop on any blocker.

## Prompt to Cody for пункт 2

```text
Ты Cody. Рабочая папка: /Users/deploydog-ai/LinkedIn.

Сделай только пункт 2: улучшить PostLiker varied reactions + promoted/sponsored skip, по мотивам /Users/deploydog-ai/vadim/linkedin-ssi-boost.md и /Users/deploydog-ai/vadim/weekly-linkedin-ssi-boost.md.

Текущий файл: services/PostLiker/scripts/linkedin_like_posts.py.
Текущий контейнер уже есть: postliker.
Не трогай CommentResponder и ConnectMan.

Требования:
1. Добавь skip promoted/sponsored posts: reason `promoted_or_sponsored`.
2. Добавь configurable reaction mode:
   - LINKEDIN_LIKE_REACTION_MODE=like_only|varied
   - безопасный default: like_only
   - LINKEDIN_LIKE_ALLOWED_REACTIONS_JSON default ["Like","Celebrate","Support","Love","Insightful","Funny"]
3. Для varied mode реализуй простую explainable heuristic по тексту поста:
   - achievement/certification/new role -> Celebrate
   - struggle/job seeking/support/help -> Support
   - technical/deep/professional insight -> Insightful
   - funny/humor/meme -> Funny
   - fallback -> Like
4. Реакция должна быть реальным UI action через Playwright. Если picker/non-Like не сработал — fallback to Like, записать fallback flag.
5. Не добавляй комментарии и не отправляй публичные тексты.
6. Status JSON должен содержать selected_reaction, reaction_reason, fallback_to_like, skip promoted count.
7. Сохрани stop-on captcha/security/checkpoint/safeguard/rate-limit with screenshot + exit 12.
8. Добавь/обнови self-test helpers if useful, но не усложняй.
9. Запусти проверки:
   - python3 -m py_compile services/PostLiker/scripts/linkedin_like_posts.py
   - bash -n services/PostLiker/scripts/run.sh services/PostLiker/entrypoint.sh services/PostLiker/scripts/healthcheck.sh
   - docker compose config
   - docker compose build postliker
   - SAFE_MODE=1 docker compose run --rm --entrypoint /app/scripts/run.sh postliker
   - One canary: max 1 like, no verify, preferably like_only unless varied mode can be safely tested with max 1.
10. Верни отчёт: files changed, commands run, exit codes, status path, exact command for Lily verification.

Не трогай пункты 1 и 3.
```

## Проверка Лили для пункта 2

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile services/PostLiker/scripts/linkedin_like_posts.py
bash -n services/PostLiker/scripts/run.sh services/PostLiker/entrypoint.sh services/PostLiker/scripts/healthcheck.sh
docker compose config >/tmp/linkedin-compose-config.txt
docker compose build postliker
SAFE_MODE=1 docker compose run --rm --entrypoint /app/scripts/run.sh postliker
```

One-shot canary, минимальный action:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_LIKE_MAX=1 \
  -e LINKEDIN_LIKE_MAX_ATTEMPTS=1 \
  -e LINKEDIN_LIKE_REACTION_MODE=like_only \
  -e LINKEDIN_LIKE_MAX_SCROLLS=15 \
  postliker
```

Если Cody просит varied canary:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_LIKE_MAX=1 \
  -e LINKEDIN_LIKE_MAX_ATTEMPTS=1 \
  -e LINKEDIN_LIKE_REACTION_MODE=varied \
  -e LINKEDIN_LIKE_MAX_SCROLLS=15 \
  postliker
```

После:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/Users/deploydog-ai/LinkedIn/shared/legacy_state/liked_posts/linkedin_liked_posts_status.json')
data=json.loads(p.read_text())
print('stop_reason=', data.get('stop_reason'))
print('liked=', len(data.get('liked', [])))
print('skipped=', len(data.get('skipped', [])))
print('errors=', len(data.get('errors', [])))
print('sample_liked=', data.get('liked', [])[:1])
print('promoted_skips=', sum(1 for x in data.get('skipped', []) if x.get('reason')=='promoted_or_sponsored'))
PY
docker compose ps postliker
docker compose logs --tail=120 postliker
```

Accept criteria:
- no traceback/container crash;
- status JSON parses;
- no blocker;
- promoted/sponsored posts are skipped when encountered;
- like_only remains safe default;
- varied mode records selected_reaction when used;
- no comments posted.

---

# Пункт 3. Soft recruiter profile-view/connect mode в ConnectMan

## Цель

Взять из `vadim/daily-ssi-find-right-people.md` мягкий recruiter/hiring-manager discovery режим: profile views + малый connect cap, без просьб о работе/referrals/openings, сохранив наши geo/role/LinkedIn safety rules.

## Текущие файлы

- Modify: `services/ConnectMan/scripts/linkedin_outreach.py`
- Modify if needed: `services/ConnectMan/scripts/run.sh`
- Modify if needed: `.env.example`
- Existing status:
  - `/Users/deploydog-ai/LinkedIn/shared/legacy_state/outreach_run_status.json`

## Required behavior

1. Add mode env/CLI:
   - `LINKEDIN_CONNECT_MODE=standard|soft_recruiter`
   - default: `standard` to preserve current behavior.
   - CLI optional: `--mode soft_recruiter`.

2. In `soft_recruiter` mode:
   - Search recruiter/hiring manager ICP, not generic DevOps peers.
   - Default daily cap:
     - profile views: 10–15 max, env `LINKEDIN_SOFT_PROFILE_VIEW_MAX=15`.
     - connects: 3–5 max, env `LINKEDIN_SOFT_CONNECT_MAX=5`.
   - No notes/messages by default.
   - Do not ask for jobs/referrals/openings anywhere.
   - Keep existing connect geo allowlist Spain/PT/UK unless Андрей explicitly changes it.
   - Unknown/missing location => skip for connect. Profile view can be allowed if not blocked, but record location confidence.

3. ICP keywords:
   - Technical Recruiter AI ML
   - Talent Acquisition Engineering Tech AI
   - Engineering Manager AI ML
   - Head of AI / Head of Machine Learning
   - Tech Recruiter / IT Recruiter
   - People Partner / Head of People startup AI
   - Env configurable: `LINKEDIN_SOFT_RECRUITER_SEARCHES_JSON`.

4. Candidate scoring:
   - Recruiter/TA at tech/product/AI company high priority.
   - Hiring manager / Head of AI / CTO acceptable.
   - Off-ICP recruiter (e.g. healthcare/non-tech) skip.
   - Skip blocked names/locations.

5. Profile views:
   - Open profile page, wait human delay, record viewed profile URL/name/headline/company/location.
   - Stop on blocker.

6. Connects:
   - Send no-note connect only for best candidates passing geo allowlist and ICP score.
   - Verify button flips to Pending or request sent.
   - Record name/title/company/location/why_selected/status.

7. Status JSON additions:
   - mode;
   - profiles_viewed list;
   - soft_connects_sent list;
   - skipped with reasons;
   - inbound_opportunities placeholder/list if any role/opening text is encountered, but do not reply.

8. Safe mode/dry-run:
   - `SAFE_MODE=1` py_compile only.
   - `LINKEDIN_CONNECT_DRY_RUN=1` should collect candidates and not send connects. Profile views are still page opens; if Cody can add `no_action_dry_run` that only parses search pages, better.

## Prompt to Cody for пункт 3

```text
Ты Cody. Рабочая папка: /Users/deploydog-ai/LinkedIn.

Сделай только пункт 3: soft recruiter profile-view/connect mode в ConnectMan, по мотивам /Users/deploydog-ai/vadim/daily-ssi-find-right-people.md.

Текущий файл: services/ConnectMan/scripts/linkedin_outreach.py.
Текущий контейнер: connectman.
Не трогай CommentResponder, PostLiker, JobSeeker, SendMan.

Требования:
1. Добавь режим LINKEDIN_CONNECT_MODE=standard|soft_recruiter, default standard без изменения текущего поведения.
2. В soft_recruiter режиме:
   - view 10–15 recruiter/hiring-manager profiles max, env LINKEDIN_SOFT_PROFILE_VIEW_MAX default 15;
   - send 3–5 no-note connects max, env LINKEDIN_SOFT_CONNECT_MAX default 5;
   - никаких сообщений/follow-ups/notes/job asks/referral asks/opening asks;
   - сохраняй нашу geo allowlist Spain/PT/UK для connects;
   - unknown/missing location skip for connect;
   - profile views можно делать шире, если нет blocker/blocked filters, но всё логировать.
3. Search ICP configurable через LINKEDIN_SOFT_RECRUITER_SEARCHES_JSON, defaults по recruiter/TA/AI/hiring manager keywords.
4. Добавь candidate scoring/explainable reason.
5. Статус JSON должен включать:
   - mode
   - profiles_viewed
   - soft_connects_sent
   - skipped reasons
   - inbound_opportunities list if detected, but no replies
6. Добавь LINKEDIN_CONNECT_DRY_RUN=1:
   - не отправлять connect;
   - если можно, не открывать профили в no-action dry-run, только собрать candidates. Если нельзя — явно задокументируй.
7. Сохрани stop-on captcha/security/checkpoint/safeguard/rate-limit/limit with screenshot + exit 12.
8. Проверки:
   - python3 -m py_compile services/ConnectMan/scripts/linkedin_outreach.py
   - bash -n services/ConnectMan/scripts/run.sh services/ConnectMan/entrypoint.sh services/ConnectMan/scripts/healthcheck.sh
   - docker compose config
   - docker compose build connectman
   - SAFE_MODE=1 docker compose run --rm --entrypoint /app/scripts/run.sh connectman
   - dry-run/no-send soft recruiter canary max pages 1, profile views max 2, connects max 0.
9. Верни отчёт: files changed, commands run, exit codes, status path, exact command for Lily verification.

Не трогай пункты 1 и 2.
```

## Проверка Лили для пункта 3

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile services/ConnectMan/scripts/linkedin_outreach.py
bash -n services/ConnectMan/scripts/run.sh services/ConnectMan/entrypoint.sh services/ConnectMan/scripts/healthcheck.sh
docker compose config >/tmp/linkedin-compose-config.txt
docker compose build connectman
SAFE_MODE=1 docker compose run --rm --entrypoint /app/scripts/run.sh connectman
```

Dry-run/no-send canary:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_CONNECT_MODE=soft_recruiter \
  -e LINKEDIN_CONNECT_DRY_RUN=1 \
  -e LINKEDIN_MAX_PAGES=1 \
  -e LINKEDIN_SOFT_PROFILE_VIEW_MAX=2 \
  -e LINKEDIN_SOFT_CONNECT_MAX=0 \
  connectman
```

Optional one real profile-view canary, без connect:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_CONNECT_MODE=soft_recruiter \
  -e LINKEDIN_CONNECT_DRY_RUN=0 \
  -e LINKEDIN_MAX_PAGES=1 \
  -e LINKEDIN_SOFT_PROFILE_VIEW_MAX=1 \
  -e LINKEDIN_SOFT_CONNECT_MAX=0 \
  connectman
```

Real connect canary only if no blocker and Андрей wants real connect test:

```bash
cd /Users/deploydog-ai/LinkedIn
docker compose run --rm --entrypoint /app/scripts/run.sh \
  -e SAFE_MODE=0 \
  -e LINKEDIN_CONNECT_MODE=soft_recruiter \
  -e LINKEDIN_CONNECT_DRY_RUN=0 \
  -e LINKEDIN_MAX_PAGES=1 \
  -e LINKEDIN_SOFT_PROFILE_VIEW_MAX=2 \
  -e LINKEDIN_SOFT_CONNECT_MAX=1 \
  connectman
```

After:

```bash
python3 - <<'PY'
import json
from pathlib import Path
p=Path('/Users/deploydog-ai/LinkedIn/shared/legacy_state/outreach_run_status.json')
data=json.loads(p.read_text())
print('mode=', data.get('mode'))
print('stop_reason=', data.get('stop_reason'))
print('profiles_viewed=', len(data.get('profiles_viewed', [])))
print('soft_connects_sent=', len(data.get('soft_connects_sent', [])))
print('sent=', len(data.get('sent', [])))
print('skipped=', len(data.get('skipped', [])))
print('errors=', len(data.get('errors', [])))
print('sample_view=', (data.get('profiles_viewed') or [])[:1])
PY
docker compose ps connectman
docker compose logs --tail=120 connectman
```

Accept criteria:
- default standard behavior preserved;
- soft_recruiter dry-run sends 0 connects;
- profile views/connects have separate counters;
- geo allowlist enforced for connects;
- no notes/messages/follow-ups;
- no blocker/crash;
- status JSON parseable.

---

# Финальная последовательность выполнения

1. Выполнить пункт 1 через Cody.
2. Лили проверяет пункт 1.
3. Если ошибка/blocker — вернуть Cody фактуру, повторить пункт 1.
4. Только после clean пункт 1 перейти к пункт 2.
5. Выполнить пункт 2 через Cody.
6. Лили проверяет пункт 2.
7. Если ошибка/blocker — вернуть Cody фактуру, повторить пункт 2.
8. Только после clean пункт 2 перейти к пункт 3.
9. Выполнить пункт 3 через Cody.
10. Лили проверяет пункт 3.
11. Если ошибка/blocker — вернуть Cody фактуру, повторить пункт 3.
12. Финальная общая проверка:

```bash
cd /Users/deploydog-ai/LinkedIn
python3 -m py_compile services/*/scripts/*.py
bash -n services/*/scripts/*.sh services/*/entrypoint.sh
docker compose config >/tmp/linkedin-compose-config-final.txt
docker compose ps
for s in jobseeker connectman sendman postliker commentresponder; do
  echo "--- $s ---"
  docker compose ps "$s" || true
  docker compose logs --tail=40 "$s" || true
done
```

13. Финальный отчёт Андрею без воды:

```text
Готово: <N>/3 пункта внедрены.

1. CommentResponder:
- files:
- verification:
- status:
- blocker: нет/есть

2. PostLiker varied/promoted skip:
- files:
- verification:
- status:
- blocker: нет/есть

3. ConnectMan soft recruiter:
- files:
- verification:
- status:
- blocker: нет/есть

Общий Docker статус:
- jobseeker:
- connectman:
- sendman:
- postliker:
- commentresponder:

Осталось/ручные approvals:
- публичные comments/replies требуют approve;
- real soft recruiter connect canary требует явного разрешения, если не запускался;
- stale invitation cleanup не входит в эти 3 пункта.
```
