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
