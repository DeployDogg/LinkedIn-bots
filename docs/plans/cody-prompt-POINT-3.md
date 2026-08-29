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
