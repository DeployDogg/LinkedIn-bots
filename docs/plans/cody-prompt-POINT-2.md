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
