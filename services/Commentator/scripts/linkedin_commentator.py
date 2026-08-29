#!/usr/bin/env python3
"""LinkedIn commentator: scan comments, generate Андрей-style drafts, send Telegram approvals, publish approved replies.

Safe defaults:
- Telegram sending is controlled by LINKEDIN_COMMENTATOR_SEND_TELEGRAM (default 0).
- LinkedIn publishing is controlled by LINKEDIN_COMMENTATOR_PUBLISH_APPROVED (default 0).
- Dry-run never sends Telegram messages and never publishes LinkedIn replies.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


def env_str(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    value = str(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def env_bool(name: str, default: bool = False) -> bool:
    raw = env_str(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(env_str(name, str(default)))
    except Exception:
        return default


def env_json(name: str, default: Any) -> Any:
    raw = env_str(name, "")
    if not raw:
        return default
    return json.loads(raw)


ROOT = Path(env_str("LINKEDIN_ROOT_DIR", "/Users/deploydog-ai/LinkedIn"))
STATE_DIR = Path(env_str("LINKEDIN_COMMENTATOR_STATE_DIR", str(ROOT / "shared" / "comment_state")))
STATUS_PATH = Path(env_str("LINKEDIN_COMMENTATOR_STATUS_PATH", str(STATE_DIR / "linkedin_commentator_status.json")))
STATE_PATH = Path(env_str("LINKEDIN_COMMENTATOR_DB_PATH", str(STATE_DIR / "linkedin_commentator_state.json")))
DRAFTS_PATH = Path(env_str("LINKEDIN_COMMENTATOR_DRAFTS_PATH", str(STATE_DIR / "linkedin_commentator_drafts.md")))
ALERT_PATH = Path(env_str("LINKEDIN_COMMENTATOR_ALERT_PATH", str(STATE_DIR / "linkedin_commentator_alert.json")))
CONTROL_DIR = Path(env_str("LINKEDIN_COMMENTATOR_CONTROL_DIR", "/shared/comment_state"))
TG_ENV_PATH = Path(env_str("LINKEDIN_COMMENTATOR_TELEGRAM_ENV", str(ROOT / ".hermes_stats_bot.env")))
SCREENSHOT_DIR = Path(env_str("LINKEDIN_COMMENTATOR_SCREENSHOT_DIR", str(ROOT / "BlockScreenshots")))
BA = ZoneInfo(env_str("TZ", "America/Argentina/Buenos_Aires"))
OWNER_NAME = env_str("LINKEDIN_COMMENT_OWNER_NAME", env_str("LINKEDIN_CONTACT_FULL_NAME", "Andrew Anashkin"))
CDP_ENDPOINT_DEFAULT = "http://linkedin-browser:9222"

NOTIFICATION_URLS = env_json("LINKEDIN_COMMENTATOR_NOTIFICATION_URLS_JSON", [
    "https://www.linkedin.com/notifications/?filter=my_posts_all",
    "https://www.linkedin.com/notifications/?filter=mentions",
])
ACTIVITY_URLS = env_json("LINKEDIN_COMMENTATOR_ACTIVITY_URLS_JSON", [
    "https://www.linkedin.com/in/andrew-anashkin/recent-activity/comments/",
])
POST_ACTIVITY_URLS = env_json("LINKEDIN_COMMENTATOR_POST_ACTIVITY_URLS_JSON", [
    "https://www.linkedin.com/in/andrew-anashkin/recent-activity/all/",
    "https://www.linkedin.com/in/andrew-anashkin/recent-activity/shares/",
])
STOP_PATTERNS = env_json("LINKEDIN_STOP_PATTERNS_JSON", [
    "captcha", "authwall", "security verification", "verify your identity", "checkpoint", "challenge",
    "unusual activity", "temporarily restricted", "account has been restricted", "you’ve reached the limit",
    "you have reached the limit", "daily limit", "weekly limit", "try again later", "safeguard",
])
SHORT_CONFIRMATIONS = {"+1", "100%", "agree", "agreed", "согласен", "согласна", "да", "точно", "верно", "класс", "спасибо", "thanks", "true", "yes", "nice"}

# Production-safety state machine for one canonical comment_key/item id.
# Safe retry design:
# - published/rejected are terminal and never return to approved from Telegram callbacks.
# - approved -> publishing is acquired as a local item-level lock and persisted before
#   the external LinkedIn publish action.
# - failed publishing attempts return to approved while attempts remain; after
#   LINKEDIN_COMMENTATOR_MAX_PUBLISH_ATTEMPTS the item becomes publish_failed with
#   requires_manual_review=True and is not retried automatically.
# - stale publishing locks can be recovered after
#   LINKEDIN_COMMENTATOR_PUBLISHING_STALE_MINUTES and are accounted as failed attempts.
DECISION_PROPOSED = "proposed"
DECISION_SENT_TO_TELEGRAM = "sent_to_telegram"
DECISION_APPROVED = "approved"
DECISION_REJECTED = "rejected"
DECISION_PUBLISHING = "publishing"
DECISION_PUBLISHED = "published"
DECISION_PUBLISH_FAILED = "publish_failed"
TERMINAL_DECISIONS = {DECISION_REJECTED, DECISION_PUBLISHED}
NON_RETRY_PUBLISH_REASONS = {"published_unverified"}


def publishing_stale_minutes() -> int:
    return env_int("LINKEDIN_COMMENTATOR_PUBLISHING_STALE_MINUTES", 60)


def max_publish_attempts() -> int:
    return env_int("LINKEDIN_COMMENTATOR_MAX_PUBLISH_ATTEMPTS", 3)


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BA)
        return parsed
    except Exception:
        return None


def state_event(item: dict[str, Any], event: str, reason: str, **extra: Any) -> None:
    payload = {"at": now_iso(), "event": event, "reason": reason}
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    item.setdefault("state_events", []).append(payload)


def item_decision(item: dict[str, Any]) -> str:
    return str(item.get("decision") or DECISION_PROPOSED)


def apply_callback_decision(item: dict[str, Any], action: str, callback_id: str = "") -> tuple[bool, str]:
    """Idempotently apply one Telegram approval callback to an item.

    Allowed callback transitions:
    - proposed/sent_to_telegram -> approved/rejected
    - approved -> rejected, while repeated approve is a no-op
    - rejected/publishing/published are protected from callback changes
    Unknown/unsupported current decisions are ignored safely.
    """
    if callback_id:
        seen_callbacks = item.setdefault("telegram_callback_ids", [])
        if isinstance(seen_callbacks, list) and callback_id in seen_callbacks:
            return False, "duplicate_callback_ignored"
        if isinstance(seen_callbacks, list):
            seen_callbacks.append(callback_id)
    current = item_decision(item)
    if action == "approve":
        if current in {DECISION_PROPOSED, DECISION_SENT_TO_TELEGRAM}:
            item["decision"] = DECISION_APPROVED
            item["decided_at"] = now_iso()
            state_event(item, "telegram_callback", "approved", callback_id=callback_id)
            return True, "approved"
        reason = "noop_already_approved" if current == DECISION_APPROVED else f"noop_approve_ignored_from_{current}"
        state_event(item, "telegram_callback_ignored", reason, callback_id=callback_id)
        return False, reason
    if action == "reject":
        if current in {DECISION_PROPOSED, DECISION_SENT_TO_TELEGRAM, DECISION_APPROVED}:
            item["decision"] = DECISION_REJECTED
            item["decided_at"] = now_iso()
            state_event(item, "telegram_callback", "rejected", callback_id=callback_id)
            return True, "rejected"
        reason = f"noop_reject_ignored_from_{current}"
        state_event(item, "telegram_callback_ignored", reason, callback_id=callback_id)
        return False, reason
    state_event(item, "telegram_callback_ignored", "unknown_action", callback_id=callback_id, action=action)
    return False, "unknown_action"


def should_send_telegram_approval(item: dict[str, Any]) -> bool:
    """Return True only for a never-sent proposed item.

    This prevents duplicate Telegram approvals after restart/race. Dry-run callers
    should not mark sent_to_telegram; the transition happens only after sendMessage
    returns successfully.
    """
    if item_decision(item) != DECISION_PROPOSED:
        return False
    return not (item.get("telegram_message_id") or item.get("sent_to_telegram_at"))


def mark_telegram_sent(item: dict[str, Any], message_id: str | None) -> None:
    item["telegram_status"] = "sent"
    item["telegram_message_id"] = message_id
    if item_decision(item) == DECISION_PROPOSED:
        item["decision"] = DECISION_SENT_TO_TELEGRAM
        item["sent_to_telegram_at"] = now_iso()
        state_event(item, "telegram_sent", "sent_to_telegram", telegram_message_id=message_id)


def publish_attempt_count(item: dict[str, Any]) -> int:
    attempts = item.get("publish_attempts")
    return len(attempts) if isinstance(attempts, list) else 0


def mark_publish_failed_manual(item: dict[str, Any], reason: str) -> None:
    item["decision"] = DECISION_PUBLISH_FAILED
    item["requires_manual_review"] = True
    item["last_publish_error"] = reason
    item.pop("publishing_started_at", None)
    item.pop("publishing_run_id", None)
    state_event(item, "publish_failed", reason)


def _finish_open_publish_attempt(item: dict[str, Any], run_id: str, ok: bool, reason: str, when: str) -> None:
    attempts = item.setdefault("publish_attempts", [])
    for attempt in reversed(attempts):
        if attempt.get("run_id") == run_id and attempt.get("ok") is None:
            attempt.update({"finished_at": when, "ok": ok, "reason": reason})
            return
    attempts.append({"run_id": run_id, "started_at": item.get("publishing_started_at") or when, "finished_at": when, "ok": ok, "reason": reason})


def acquire_publish_lock(
    item: dict[str, Any],
    run_id: str,
    *,
    now: str | None = None,
    stale_minutes: int | None = None,
    max_attempts: int | None = None,
) -> tuple[bool, str]:
    """Atomically prepare an approved item for publish; caller must save before action."""
    current = item_decision(item)
    now_value = now or now_iso()
    stale_limit = publishing_stale_minutes() if stale_minutes is None else stale_minutes
    max_allowed = max_publish_attempts() if max_attempts is None else max_attempts

    if item.get("published_at") or current == DECISION_PUBLISHED:
        return False, "already_published"
    if current == DECISION_REJECTED:
        return False, "rejected_terminal"
    if current == DECISION_PUBLISH_FAILED:
        return False, "publish_failed_manual_review"

    if current == DECISION_PUBLISHING:
        started = parse_iso_datetime(item.get("publishing_started_at"))
        now_dt = parse_iso_datetime(now_value) or datetime.now(BA)
        age_seconds = (now_dt - started).total_seconds() if started else 0
        if started and age_seconds < stale_limit * 60:
            return False, "publishing_lock_fresh"
        stale_run_id = str(item.get("publishing_run_id") or "")
        _finish_open_publish_attempt(item, stale_run_id, False, "stale_publishing_recovered", now_value)
        item["decision"] = DECISION_APPROVED
        item["last_publish_error"] = "stale_publishing_recovered"
        state_event(item, "publishing_lock_recovered", "stale_publishing_recovered", previous_run_id=stale_run_id)
        current = DECISION_APPROVED

    if current != DECISION_APPROVED:
        return False, f"not_approved:{current}"
    if publish_attempt_count(item) >= max_allowed:
        mark_publish_failed_manual(item, "max_publish_attempts_reached")
        return False, "max_publish_attempts_reached"

    item["decision"] = DECISION_PUBLISHING
    item["publishing_started_at"] = now_value
    item["publishing_run_id"] = run_id
    item.setdefault("publish_attempts", []).append({"run_id": run_id, "started_at": now_value, "ok": None, "reason": "publishing_started"})
    state_event(item, "publishing_lock_acquired", "approved_to_publishing", run_id=run_id)
    return True, "publishing_lock_acquired"


def complete_publish_attempt(item: dict[str, Any], run_id: str, ok: bool, reason: str, *, now: str | None = None, max_attempts: int | None = None) -> None:
    when = now or now_iso()
    _finish_open_publish_attempt(item, run_id, ok, reason, when)
    item.pop("publishing_started_at", None)
    item.pop("publishing_run_id", None)
    if ok:
        item["decision"] = DECISION_PUBLISHED
        item["published_at"] = when
        item["publish_reason"] = reason
        item.pop("last_publish_error", None)
        item.pop("requires_manual_review", None)
        state_event(item, "published", reason, run_id=run_id)
        return
    item["last_publish_error"] = reason
    if reason in NON_RETRY_PUBLISH_REASONS:
        mark_publish_failed_manual(item, reason)
        return
    max_allowed = max_publish_attempts() if max_attempts is None else max_attempts
    if publish_attempt_count(item) >= max_allowed:
        mark_publish_failed_manual(item, "max_publish_attempts_reached")
    else:
        item["decision"] = DECISION_APPROVED
        state_event(item, "publish_attempt_failed", reason, run_id=run_id)


STYLE_PROMPT = """Ты пишешь ответ в LinkedIn от лица Andrew Anashkin.
Стиль Андрея:
- живой русский/английский, без корпоративной полировки;
- прямой инженерный тон, анти-bullshit, можно чуть иронии;
- комментарий обычно короткий: 1–4 предложения;
- если человек задал вопрос — ответь по делу;
- если человек спорит — спокойно уточни границу роли/ответственности;
- не выдумывай факты, цифры, опыт и обещания;
- не токсичить, не хамить, не уходить в длинный пост;
- можно обращаться к автору по имени, если оно есть.
Выводи только готовый комментарий без кавычек и пояснений.
"""


def now_iso() -> str:
    return datetime.now(BA).isoformat(timespec="seconds")


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class StateCorruptionError(RuntimeError):
    """Production state is unreadable or has an unsafe top-level schema."""


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        if path == STATE_PATH:
            raise StateCorruptionError(f"state_json_parse_failed:{exc.__class__.__name__}") from exc
    return default


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # Some filesystems do not support directory fsync. File fsync + replace
        # still prevents a reader from observing partial JSON.
        pass


def save_json(path: Path, data: Any, *, backup: bool | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    keep_backup = (path == STATE_PATH) if backup is None else backup
    if keep_backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        backup_tmp = backup_path.with_suffix(backup_path.suffix + ".tmp")
        with path.open("rb") as source, backup_tmp.open("wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(backup_tmp, backup_path)
    os.replace(tmp, path)
    _fsync_directory(path.parent)


def validate_state_schema(state: Any) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateCorruptionError("state_root_must_be_object")
    if "items" not in state:
        raise StateCorruptionError("state_items_missing")
    items = state.get("items")
    if not isinstance(items, dict):
        raise StateCorruptionError("state_items_must_be_object")
    for key, item in items.items():
        if not isinstance(key, str) or not isinstance(item, dict):
            raise StateCorruptionError("state_items_entries_must_be_objects")
    return state


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"schema_version": 2, "items": {}, "telegram_update_offset": 0}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StateCorruptionError(f"state_json_parse_failed:{exc.__class__.__name__}") from exc
    return validate_state_schema(state)


def normalize_state(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return a normalized copy; preserve keys and all historical fields."""
    validate_state_schema(state)
    normalized = copy.deepcopy(state)
    changed = False
    if normalized.get("schema_version") != 2:
        normalized["schema_version"] = 2
        changed = True
    normalized.setdefault("telegram_update_offset", 0)
    for storage_key, item in normalized["items"].items():
        original = copy.deepcopy(item)
        item_id = str(item.get("id") or storage_key)
        item.setdefault("id", item_id)
        if not item.get("comment_key"):
            item["comment_key"] = canonical_comment_key(
                str(item.get("post_url") or ""),
                str(item.get("author") or "unknown"),
                str(item.get("comment_text") or ""),
                str(item.get("author_profile") or ""),
            ) or item_id
        sources = item.get("source_seen")
        if isinstance(sources, str):
            item["source_seen"] = [sources] if sources else []
        elif not isinstance(sources, list):
            item["source_seen"] = []
        if not item.get("decision"):
            if item.get("published") or item.get("published_at"):
                item["decision"] = DECISION_PUBLISHED
            elif item.get("rejected") or item.get("rejected_at"):
                item["decision"] = DECISION_REJECTED
            elif item.get("approved") or item.get("approved_at"):
                item["decision"] = DECISION_APPROVED
            elif item.get("sent_to_telegram") or item.get("sent_to_telegram_at") or item.get("telegram_message_id") or item.get("telegram_status") == "sent":
                item["decision"] = DECISION_SENT_TO_TELEGRAM
            else:
                item["decision"] = DECISION_PROPOSED
        changed = changed or item != original
    return normalized, changed


def pause_status() -> dict[str, Any]:
    pause_all_env = env_bool("LINKEDIN_COMMENTATOR_PAUSE_ALL", False)
    pause_publish_env = env_bool("LINKEDIN_COMMENTATOR_PAUSE_PUBLISHING", False)
    pause_all_file = any((directory / "PAUSE_ALL").exists() for directory in {STATE_DIR, CONTROL_DIR})
    pause_publish_file = any((directory / "PAUSE_PUBLISHING").exists() for directory in {STATE_DIR, CONTROL_DIR})
    return {
        "pause_all": pause_all_env or pause_all_file,
        "pause_all_env": pause_all_env,
        "pause_all_file": pause_all_file,
        "pause_publishing": pause_publish_env or pause_publish_file,
        "pause_publishing_env": pause_publish_env,
        "pause_publishing_file": pause_publish_file,
    }


def publishing_pause_reason() -> str | None:
    pauses = pause_status()
    if pauses["pause_publishing_env"]:
        return "publishing_paused_env"
    if pauses["pause_publishing_file"]:
        return "publishing_paused_file"
    return None


def write_alert(category: str, reason: str, **details: Any) -> None:
    payload = {"created_at": now_iso(), "category": category, "reason": reason}
    payload.update({key: value for key, value in details.items() if value is not None})
    save_json(ALERT_PATH, payload, backup=False)


def _path_writable(path: Path) -> bool:
    target = path if path.exists() and path.is_dir() else path.parent
    while not target.exists() and target != target.parent:
        target = target.parent
    return target.exists() and os.access(target, os.W_OK | os.X_OK)


def _command_executable(command: str) -> bool:
    try:
        first = shlex.split(command)[0]
    except Exception:
        return False
    resolved = Path(first) if "/" in first else Path(shutil.which(first) or "")
    return bool(str(resolved)) and resolved.is_file() and os.access(resolved, os.X_OK)


def production_preflight(args: argparse.Namespace) -> dict[str, Any]:
    pauses = pause_status()
    token, chat_id = telegram_config()
    command = env_str("LINKEDIN_COMMENTATOR_CODEX_COMMAND", "")
    cdp_configured = bool(env_str("LINKEDIN_CDP_ENDPOINT", CDP_ENDPOINT_DEFAULT).strip())
    cron_path = Path(env_str("LINKEDIN_COMMENTATOR_CRONTAB_PATH", "/app/crontab"))
    if not cron_path.exists():
        host_cron = ROOT / "services" / "Commentator" / "crontab"
        if host_cron.exists():
            cron_path = host_cron
    checks: dict[str, bool] = {
        "state_dir_writable": _path_writable(STATE_DIR),
        "status_dir_writable": _path_writable(STATUS_PATH),
        "drafts_dir_writable": _path_writable(DRAFTS_PATH),
        "screenshot_dir_writable": _path_writable(SCREENSHOT_DIR),
        "state_valid": True,
        "codex_command_executable": bool(command) and _command_executable(command),
        "cdp_endpoint_configured": cdp_configured,
        "cron_daily_10_17_present": False,
    }
    state_error = None
    try:
        validate_state_schema(load_state())
    except StateCorruptionError as exc:
        checks["state_valid"] = False
        state_error = str(exc)
    if cron_path.is_file():
        try:
            checks["cron_daily_10_17_present"] = any(
                re.match(r"^17\s+10\s+\*\s+\*\s+\*\s+", line.strip())
                for line in cron_path.read_text(encoding="utf-8").splitlines()
            )
        except OSError:
            pass
    telegram_requested = bool(getattr(args, "send_telegram", False))
    hard_failures = [name for name, ok in checks.items() if not ok]
    if telegram_requested and (not token or not chat_id):
        hard_failures.append("telegram_credentials_missing_when_requested")
    return {
        "ready": not hard_failures,
        "hard_failures": hard_failures,
        "checks": checks,
        "state_error": state_error,
        "telegram": {"requested": telegram_requested, "token_present": bool(token), "chat_id_present": bool(chat_id)},
        "safety": {
            "dry_run": bool(getattr(args, "dry_run", False)),
            "send_telegram": telegram_requested,
            "publish_approved": bool(getattr(args, "publish_approved", False)),
            **pauses,
        },
    }


def exit_with_persisted_status(status: dict[str, Any], code: int) -> None:
    """Persist final non-secret status fields before an intentional worker exit."""
    status["finished_at"] = now_iso()
    save_json(STATUS_PATH, status)
    raise SystemExit(code)


def normalize_spaces(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_key(text: str | None) -> str:
    return normalize_spaces(re.sub(r"[^\w\s+%?!.-]", " ", str(text or ""))).casefold()


def truncate_on_word(text: str, limit: int) -> str:
    s = normalize_spaces(text)
    if len(s) <= limit:
        return s
    cut = s.rfind(" ", 0, limit)
    if cut < max(80, limit // 2):
        cut = limit
    return s[:cut].rstrip(" ,.;:—-")


def owner_name_matches(author: str | None) -> bool:
    author_key = normalize_key(author)
    owner_key = normalize_key(OWNER_NAME)
    if not author_key or author_key == "unknown" or not owner_key:
        return author_key == "unknown"
    owner_parts = [part for part in owner_key.split() if len(part) >= 3]
    return author_key == owner_key or owner_key in author_key or (owner_parts and all(part in author_key for part in owner_parts))


def _remove_repeated_owner_headline_lines(lines: list[str]) -> list[str]:
    owner_parts = [part for part in normalize_key(OWNER_NAME).split() if len(part) >= 3]
    cleaned: list[str] = []
    for line in lines:
        key = normalize_key(line)
        if not key:
            continue
        if owner_parts and all(part in key for part in owner_parts):
            continue
        cleaned.append(line)
    return cleaned


def clean_post_excerpt(raw_text: str | None, *, limit: int = 1600) -> str:
    """Return a clean human post excerpt without LinkedIn feed/analytics/comment chrome."""
    raw = str(raw_text or "").replace("\r", "\n")
    if not normalize_spaces(raw):
        return ""
    # Preserve line boundaries while collapsing duplicated whitespace inside each row;
    # LinkedIn chrome is usually line-like, even when Playwright innerText is noisy.
    lines = [normalize_spaces(line) for line in re.split(r"\n+", raw) if normalize_spaces(line)]
    if len(lines) <= 1:
        # Some collectors already collapsed newlines. Reinsert boundaries before common chrome markers.
        s = normalize_spaces(raw)
        markers = [
            "Feed detail update", "Feed post", "View my services", "Visible to anyone", "Show translation",
            "Activate to view", "View analytics", "Reactions Like Comment Repost Send", "Add a comment",
            "Open Emoji Keyboard", "Current selected sort order", "Most relevant", "comments section",
        ]
        for marker in markers:
            s = re.sub(r"\s+(" + re.escape(marker) + r")\b", r"\n\1", s, flags=re.I)
            s = re.sub(r"\b(" + re.escape(marker) + r")\s+", r"\1\n", s, flags=re.I)
        lines = [normalize_spaces(line) for line in s.splitlines() if normalize_spaces(line)]

    drop_exact = {
        "feed detail update", "feed post", "view my services", "show translation", "add a comment",
        "add a comment…", "open emoji keyboard", "most relevant", "comments section",
        "reactions like comment repost send", "like comment repost send",
    }
    drop_patterns = [
        r"^visible to anyone(?: on or off linkedin)?$",
        r"^activate to view\b.*$",
        r"^view analytics$",
        r"^current selected sort order\b.*$",
        r"^most relevant\b.*$",
        r"^\d[\d,\.\s]*\s+(?:reactions?|impressions?|views?|comments?|reposts?)(?:\b.*)?$",
        r"^(?:reactions?\s+)?like\s+comment\s+repost\s+send$",
        r"^comments?\s+section\b.*$",
        r"^sort order\b.*$",
    ]
    meaningful: list[str] = []
    for line in _remove_repeated_owner_headline_lines(lines):
        key = normalize_key(line)
        if key in drop_exact:
            continue
        if any(re.match(pattern, line, flags=re.I) for pattern in drop_patterns):
            continue
        meaningful.append(line)
    joined = normalize_spaces("\n".join(meaningful))
    joined = re.sub(r"^(?:Feed detail update|Feed post)\b\s*", "", joined, flags=re.I)
    joined = re.sub(
        r"^\d+\s*(?:s|m|h|d|w|mo|y)\s*•\s*"
        r"\d+\s+(?:seconds?|minutes?|hours?|days?|weeks?|months?|years?)\s+ago\s*•\s*"
        r"(?:(?:Visible to anyone\s+)?on or off LinkedIn|Visible to anyone(?:\s+on or off LinkedIn)?)\s*",
        "",
        joined,
        count=1,
        flags=re.I,
    )
    joined = re.sub(r"^.*?\bVisible to anyone(?: on or off LinkedIn)?\b\s*", "", joined, flags=re.I)
    # Notification cards can prepend the commenter text before the original post.
    post_starts = [
        "А можно пожалуйста кто-то мне уже скинет оффер?",
        "Я уже полтора месяца откликаюсь",
        "За полтора месяца поиска работы",
        "Exactly one month ago",
        "A few years ago",
        "🌴 Работа на пляже",
    ]
    marker_positions = [joined.find(marker) for marker in post_starts if joined.find(marker) > 0]
    if marker_positions:
        joined = joined[min(marker_positions):].strip()
    # Cut everything from the first comment composer/sort marker if it survived in a collapsed row.
    joined = re.split(
        r"\b(?:Add a comment|Open Emoji Keyboard|Current selected sort order|Most relevant|comments section|Reactions\s+Like\s+Comment\s+Repost\s+Send)\b",
        joined,
        maxsplit=1,
        flags=re.I,
    )[0]
    joined = re.sub(r"\b(?:Show translation|Activate to view(?: larger image)?|View analytics)\b", " ", joined, flags=re.I)
    joined = re.sub(r"(?:\s*,?\s*larger image\b)+", " ", joined, flags=re.I)
    joined = re.sub(r"\s*,?\s*\d[\d,.]*\s+You and \d[\d,.]* others\b.*$", "", joined, flags=re.I)
    joined = re.sub(r"\b\d[\d,\.\s]*\s+(?:reactions?|impressions?|views?|comments?|reposts?)\b.*$", "", joined, flags=re.I)
    return truncate_on_word(joined, limit)


def clean_comment_text(raw_text: str | None, *, author: str = "", post_excerpt: str = "", limit: int = 900) -> str:
    """Clean one LinkedIn comment/reply body, removing card chrome and embedded post text."""
    s = normalize_spaces(raw_text)
    if not s:
        return ""
    if author and author != "unknown":
        s = re.sub(r"^" + re.escape(author) + r"\b", "", s, flags=re.I).strip()
    # Drop profile degree/headline/timestamp prelude before the actual comment.
    s = re.sub(r"^[•·\s]*(?:1st|2nd|3rd|Author|Follow|Following|Подписаться|Связаться)\b\s*", "", s, flags=re.I)
    s = re.sub(r"^.{0,140}?\b\d+\s*(?:s|m|h|d|w|mo|y|min|hr|дн|ч|мин|мес)\b\s+", "", s, count=1, flags=re.I)
    # Some collapsed comment cards omit the timestamp. In that form, repeated
    # pipe-delimited headline segments are followed by optional ellipsis and
    # compact technology acronyms before the actual comment body.
    s = re.sub(
        r"^(?:[^|]{1,160}\|\s*)+(?:(?:\.{3}|…)\s*(?:[A-Z][A-Z0-9+#.-]{1,15}(?:\s+|\s*[|,/]+\s*))*)?",
        "",
        s,
        count=1,
    )
    s = re.sub(r"\s*(?:…|\.\.\.)\s*(?:more|see more|ещё|еще)\b", "", s, flags=re.I)
    # Remove duplicated original post/card content if LinkedIn appended it to the comment.
    post_clean = clean_post_excerpt(post_excerpt, limit=500)
    if post_clean:
        post_key = normalize_key(post_clean)
        post_words = post_key.split()
        markers = []
        if len(post_words) >= 8:
            markers.append(" ".join(post_words[:8]))
        if len(post_words) >= 14:
            markers.append(" ".join(post_words[:14]))
        s_key = normalize_key(s)
        for marker in markers:
            pos = s_key.find(marker)
            if pos > 0:
                # Approximate in the original string by looking for the first 3 words.
                first_words = marker.split()[:3]
                regex = r"\b" + r"\s+".join(re.escape(w) for w in first_words) + r"\b"
                m = re.search(regex, s, flags=re.I)
                if m and m.start() > 0:
                    s = s[:m.start()].strip()
                    break
    s = re.split(
        r"\b(?:Like|Reply|Translate|Report|Reactions?|See translation|Show translation|Edited|Follow|Following|Нравится|Ответить|Пожаловаться|Показать перевод)\b",
        s,
        maxsplit=1,
        flags=re.I,
    )[0]
    s = re.sub(r"\b\d+\s*(?:s|m|h|d|w|mo|y|min|hr|дн|ч|мин|мес)\b", " ", s, flags=re.I)
    s = re.sub(r"\b\d+\s+(?:reactions?|comments?|replies?|likes?)\b.*$", "", s, flags=re.I)
    return truncate_on_word(s, limit)


def normalize_reply_text_for_match(text: str | None) -> str:
    """Normalize a proposed/DOM reply for duplicate-reply matching.

    Keep only word-like content and collapse punctuation/whitespace so LinkedIn
    rendering differences (dashes, smart punctuation, line breaks) do not hide a
    duplicate reply.
    """
    normalized = str(text or "").casefold()
    normalized = re.sub(r"https?://\S+", " ", normalized)
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    return normalize_spaces(normalized)


def text_similarity(left: str | None, right: str | None) -> float:
    left_norm = normalize_reply_text_for_match(left)
    right_norm = normalize_reply_text_for_match(right)
    if not left_norm or not right_norm:
        return 0.0
    return difflib.SequenceMatcher(None, left_norm, right_norm).ratio()


def reply_text_matches(dom_text: str | None, proposed_reply: str | None, *, threshold: float = 0.82) -> bool:
    dom_norm = normalize_reply_text_for_match(dom_text)
    proposed_norm = normalize_reply_text_for_match(proposed_reply)
    if not dom_norm or not proposed_norm:
        return False
    shortest = min(len(dom_norm), len(proposed_norm))
    if shortest >= 24 and (dom_norm in proposed_norm or proposed_norm in dom_norm):
        return True
    return text_similarity(dom_norm, proposed_norm) >= threshold


def stable_id(*parts: str) -> str:
    return hashlib.sha256("\n".join(parts).encode("utf-8", errors="ignore")).hexdigest()[:16]


def _decode_linkedin_urn(value: str | None) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = urllib.parse.unquote(text)
        if decoded == text:
            break
        text = decoded
    return text


def canonical_linkedin_url(url: str) -> str:
    url = str(url or "")
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    try:
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.rstrip("/") + ("/" if parsed.path else "")
        m = re.search(r"/analytics/post-summary/(urn:li:activity:\d+)/?", path)
        if m:
            path = f"/feed/update/{m.group(1)}/"
        return urllib.parse.urlunsplit((parsed.scheme or "https", parsed.netloc or "www.linkedin.com", path, "", ""))
    except Exception:
        return url.split("?", 1)[0]


def _parse_comment_urn(value: str | None) -> tuple[str | None, str | None]:
    text = _decode_linkedin_urn(value)
    m = re.search(r"urn:li:comment:\(activity:(\d+),([^()&,?#\s]+)\)", text)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"urn:li:fsd_comment:\(([^,()?#\s]+),\s*urn:li:activity:(\d+)\)", text)
    if m:
        return m.group(2), m.group(1)
    return None, None


def extract_linkedin_comment_identifiers(value: str | None) -> dict[str, str | None]:
    text = _decode_linkedin_urn(value)
    ids: dict[str, str | None] = {"post_id": None, "comment_id": None, "reply_id": None}
    m = re.search(r"urn:li:activity:(\d+)", text)
    if m:
        ids["post_id"] = m.group(1)
    try:
        parsed = urllib.parse.urlsplit(text)
        query_values = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    except Exception:
        query_values = {}
    for key, values in query_values.items():
        low = key.lower()
        for raw in values:
            post_id, comment_id = _parse_comment_urn(raw)
            if post_id and not ids["post_id"]:
                ids["post_id"] = post_id
            if low in {"commenturn", "dashcommenturn"} and comment_id:
                ids["comment_id"] = comment_id
            elif low in {"replyurn", "dashreplyurn"} and comment_id:
                ids["reply_id"] = comment_id
    if not ids["comment_id"] and not query_values:
        post_id, comment_id = _parse_comment_urn(text)
        if post_id:
            ids["post_id"] = ids["post_id"] or post_id
        if comment_id:
            ids["comment_id"] = comment_id
    return ids


def canonical_author_profile(author_profile: str | None) -> str:
    return canonical_linkedin_url(author_profile or "").rstrip("/").casefold()


def canonical_comment_key(post_url: str, author: str, comment_text: str, author_profile: str = "") -> str:
    ids = extract_linkedin_comment_identifiers(post_url)
    post_id = ids.get("post_id")
    comment_id = ids.get("comment_id")
    if post_id and comment_id:
        return f"{post_id}:{comment_id}"
    post_part = post_id or canonical_linkedin_url(post_url).rstrip("/").casefold()
    author_part = canonical_author_profile(author_profile) or normalize_key(author)
    text_hash = hashlib.sha256(normalize_key(comment_text).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{post_part}:{author_part}:{text_hash}"


def linkedin_identifier_url(post_url: str, *values: str) -> str:
    for value in values:
        post_id, comment_id = _parse_comment_urn(value)
        if post_id and comment_id:
            return f"{canonical_linkedin_url(post_url)}?commentUrn={urllib.parse.quote(_decode_linkedin_urn(value), safe=':,()')}"
    return post_url


def merge_source_seen(target: dict[str, Any], source: str) -> None:
    seen = target.setdefault("source_seen", [])
    if isinstance(seen, str):
        seen = [seen]
        target["source_seen"] = seen
    if source and source not in seen:
        seen.append(source)


def refresh_missing_metadata(target: dict[str, Any], metadata: dict[str, Any]) -> None:
    for key, value in metadata.items():
        if value in (None, "", []):
            continue
        if not target.get(key):
            target[key] = value


def load_dotenv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            data[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return data


def telegram_config() -> tuple[str, str]:
    cfg = load_dotenv(TG_ENV_PATH)
    token = env_str("TELEGRAM_BOT_TOKEN", cfg.get("TELEGRAM_BOT_TOKEN", ""))
    chat_id = env_str("TELEGRAM_CHAT_ID", cfg.get("TELEGRAM_CHAT_ID", ""))
    return token, chat_id


def telegram_api(token: str, method: str, payload: dict[str, Any], timeout: int = 25) -> dict[str, Any]:
    data = urllib.parse.urlencode({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v) for k, v in payload.items()}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def split_message(text: str, limit: int = 3600) -> list[str]:
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5000)
    except Exception:
        return ""


def detect_stop(page) -> str | None:
    url = (getattr(page, "url", "") or "").lower()
    if any(token in url for token in ("/login", "authwall", "checkpoint", "challenge", "captcha", "security", "rate-limit", "safeguard")):
        return f"url:{page.url}"
    low = page_text(page).lower()
    for pattern in ["daily-limit", "rate limit", "sign in", "log in", "login", "authwall", *STOP_PATTERNS]:
        if pattern in low:
            return pattern
    return None


def save_stop_screenshot(page, reason: str) -> str:
    ensure_dirs()
    safe = re.sub(r"[^a-z0-9_-]+", "_", reason.lower()).strip("_") or "linkedin_stop"
    path = SCREENSHOT_DIR / f"{datetime.now(BA).strftime('%Y%m%d_%H%M%S')}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        return f"screenshot_failed:{exc!r}"


LINKEDIN_COMMENT_THREAD_EVALUATE = r"""
(target) => {
  function norm(s){ return String(s||'').replace(/\s+/g,' ').trim(); }
  function normKey(s){ return norm(s).toLowerCase().replace(/[^\p{L}\p{N}\s]/gu,' ').replace(/\s+/g,' ').trim(); }
  function visible(el){
    const r=el.getBoundingClientRect();
    const st=getComputedStyle(el);
    return !!(el.offsetParent!==null && r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none');
  }
  function textOf(el){ return norm(el.innerText || el.textContent || ''); }
  function hrefsOf(el){ return [...el.querySelectorAll('a[href]')].map(a => String(a.href || a.getAttribute('href') || '')); }
  function hasCommentId(el){
    const id = String(target.comment_id || '');
    if (!id) return false;
    const hay = [String(el.getAttribute('data-id')||''), String(el.id||''), ...hrefsOf(el)].join(' ');
    return hay.includes(id);
  }
  function hasAuthorText(el){
    const author = normKey(target.author || '');
    const comment = normKey(target.comment_text || '');
    const text = normKey(textOf(el));
    if (!comment) return false;
    const commentNeedle = comment.slice(0, Math.min(comment.length, 90));
    const authorOk = !author || text.includes(author.slice(0, Math.min(author.length, 60)));
    return authorOk && text.includes(commentNeedle);
  }
  function authorOf(el){
    const candidates = [
      el.querySelector('a[href*="/in/"]'),
      el.querySelector('.comments-post-meta__name-text'),
      el.querySelector('.comments-comment-meta__description-title'),
      el.querySelector('.feed-shared-actor__name')
    ].filter(Boolean);
    for (const node of candidates) {
      const value = norm(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
      if (value) return value;
    }
    return '';
  }
  const nodes=[...document.querySelectorAll('article li, div.comments-comment-item, div[data-id*=comment], li')].filter(visible);
  let targetNode=null;
  let matchedBy='';
  if (target.comment_id) {
    targetNode = nodes.find(hasCommentId) || null;
    if (targetNode) matchedBy='comment_id';
  }
  if (!targetNode) {
    targetNode = nodes.find(hasAuthorText) || null;
    if (targetNode) matchedBy='author_text';
  }
  if (!targetNode && target.comment_text) {
    const needle = normKey(target.comment_text).slice(0, 90);
    targetNode = nodes.find(el => normKey(textOf(el)).includes(needle)) || null;
    if (targetNode) matchedBy='text';
  }
  if (!targetNode) return {found:false, matched_by:'', owner_replies:[], all_replies:[]};
  const replyNodes = [...targetNode.querySelectorAll('li, div.comments-comment-item, div[data-id*=comment]')]
    .filter(el => el !== targetNode && visible(el));
  const ownerAliases = [target.owner_name || '', ...(target.owner_aliases || [])].map(normKey).filter(Boolean);
  const replyRows = replyNodes.map(el => ({author: authorOf(el), text: textOf(el)})).filter(r => r.text);
  const ownerReplies = replyRows.filter(r => {
    const author = normKey(r.author);
    return ownerAliases.some(owner => author.includes(owner) || owner.includes(author));
  });
  return {
    found:true,
    matched_by: matchedBy,
    owner_replies: ownerReplies,
    all_replies: replyRows,
    target_text: textOf(targetNode)
  };
}
"""


LINKEDIN_CLICK_REPLY_EVALUATE = r"""
(target) => {
  function norm(s){ return String(s||'').replace(/\s+/g,' ').trim(); }
  function normKey(s){ return norm(s).toLowerCase().replace(/[^\p{L}\p{N}\s]/gu,' ').replace(/\s+/g,' ').trim(); }
  function visible(el){ const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return !!(el.offsetParent!==null && r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'); }
  function textOf(el){ return norm(el.innerText || el.textContent || ''); }
  function hrefsOf(el){ return [...el.querySelectorAll('a[href]')].map(a => String(a.href || a.getAttribute('href') || '')); }
  function hasCommentId(el){ const id = String(target.comment_id || ''); if (!id) return false; return [String(el.getAttribute('data-id')||''), String(el.id||''), ...hrefsOf(el)].join(' ').includes(id); }
  function hasAuthorText(el){
    const author = normKey(target.author || '');
    const comment = normKey(target.comment_text || '');
    const text = normKey(textOf(el));
    if (!comment) return false;
    const authorOk = !author || text.includes(author.slice(0, Math.min(author.length, 60)));
    return authorOk && text.includes(comment.slice(0, Math.min(comment.length, 90)));
  }
  const nodes=[...document.querySelectorAll('article li, div.comments-comment-item, div[data-id*=comment], li')].filter(visible);
  let targetNode = target.comment_id ? (nodes.find(hasCommentId) || null) : null;
  if (!targetNode) targetNode = nodes.find(hasAuthorText) || null;
  if (!targetNode && target.comment_text) {
    const needle = normKey(target.comment_text).slice(0, 90);
    targetNode = nodes.find(el => normKey(textOf(el)).includes(needle)) || null;
  }
  if (!targetNode) return false;
  const buttons=[...targetNode.querySelectorAll('button, span, a')].filter(x=>/reply|ответить/i.test((x.innerText||x.getAttribute('aria-label')||'')));
  if (!buttons.length) return false;
  buttons[0].click();
  return true;
}
"""


def comment_thread_payload(item: dict[str, Any], *, proposed_reply: str = "") -> dict[str, Any]:
    ids = extract_linkedin_comment_identifiers(item.get("post_url") or "")
    owner_parts = [part for part in normalize_spaces(OWNER_NAME).split(" ") if len(part) >= 3]
    return {
        "comment_id": item.get("comment_id") or ids.get("comment_id") or "",
        "comment_key": item.get("comment_key") or item.get("id") or "",
        "author": item.get("author") or "",
        "comment_text": item.get("comment_text") or "",
        "owner_name": OWNER_NAME,
        "owner_aliases": owner_parts,
        "proposed_reply": proposed_reply,
    }


def find_linkedin_comment_thread(page, item: dict[str, Any]) -> dict[str, Any]:
    try:
        result = page.evaluate(LINKEDIN_COMMENT_THREAD_EVALUATE, comment_thread_payload(item))
    except Exception as exc:
        return {"found": False, "matched_by": "", "owner_replies": [], "all_replies": [], "error": repr(exc)}
    if not isinstance(result, dict):
        return {"found": False, "matched_by": "", "owner_replies": [], "all_replies": []}
    result.setdefault("owner_replies", [])
    result.setdefault("all_replies", [])
    return result


def owner_reply_exists(thread: dict[str, Any], proposed_reply: str) -> tuple[bool, str]:
    owner_replies = thread.get("owner_replies") if isinstance(thread, dict) else []
    if isinstance(owner_replies, list) and owner_replies:
        return True, "already_replied_on_linkedin"
    all_replies = thread.get("all_replies") if isinstance(thread, dict) else []
    if not isinstance(all_replies, list):
        return False, ""
    for row in all_replies:
        text = row.get("text") if isinstance(row, dict) else str(row)
        if reply_text_matches(text, proposed_reply):
            return True, "same_reply_exists_on_linkedin"
    return False, ""


def click_reply_for_comment_thread(page, item: dict[str, Any]) -> bool:
    try:
        return bool(page.evaluate(LINKEDIN_CLICK_REPLY_EVALUATE, comment_thread_payload(item)))
    except Exception:
        return False


@dataclass
class Candidate:
    id: str
    comment_key: str
    post_id: str | None
    comment_id: str | None
    reply_id: str | None
    source_seen: list[str]
    post_url: str
    author: str
    author_profile: str
    comment_text: str
    post_excerpt: str
    reply: str
    reason: str


def candidate_from_metadata(
    *,
    source: str,
    post_url: str,
    author: str,
    comment_text: str,
    post_excerpt: str,
    reply: str,
    reason: str,
    author_profile: str = "",
    identifier_url: str = "",
) -> Candidate:
    key_url = identifier_url or post_url
    ids = extract_linkedin_comment_identifiers(key_url)
    clean_comment = clean_comment_text(comment_text, author=author, post_excerpt=post_excerpt)
    clean_post = clean_post_excerpt(post_excerpt)
    comment_key = canonical_comment_key(key_url, author, clean_comment, author_profile)
    return Candidate(
        id=comment_key,
        comment_key=comment_key,
        post_id=ids.get("post_id"),
        comment_id=ids.get("comment_id"),
        reply_id=ids.get("reply_id"),
        source_seen=[source] if source else [],
        post_url=canonical_linkedin_url(post_url),
        author=author,
        author_profile=canonical_author_profile(author_profile),
        comment_text=clean_comment,
        post_excerpt=clean_post,
        reply=reply,
        reason=reason,
    )


def merge_candidate_metadata(target: Candidate, source: str, metadata: dict[str, Any]) -> None:
    if source and source not in target.source_seen:
        target.source_seen.append(source)
    for key, value in metadata.items():
        if value in (None, "", []):
            continue
        if getattr(target, key, None) in (None, "", []):
            setattr(target, key, value)


def strip_embedded_post_text(text: str) -> str:
    """LinkedIn notification cards often append the whole original post after the comment excerpt."""
    s = normalize_spaces(text)
    post_starts = [
        "А можно пожалуйста кто-то мне уже скинет оффер?",
        "Я уже полтора месяца откликаюсь",
        "За полтора месяца поиска работы",
        "Exactly one month ago",
        "A few years ago",
        "🌴 Работа на пляже",
    ]
    cut_positions = [s.find(marker) for marker in post_starts if s.find(marker) > 0]
    if cut_positions:
        s = s[:min(cut_positions)].strip()
    s = re.sub(r"\b\d+\s+(?:reactions?|comments?|reposts?|impressions?)\b.*$", "", s, flags=re.I).strip()
    return s


def extract_author_from_notification(text: str) -> str:
    s = normalize_spaces(text)
    s = re.sub(r"^Status is online\s+", "", s, flags=re.I)
    s = re.sub(r"^.*?Unread notification\.\s*", "", s, flags=re.I)
    m = re.match(r"(.{2,80}?)(?: and \d+ others)? (?:commented on your post|replied to your comment|mentioned you in a comment|commented on this)", s, re.I)
    if m:
        return normalize_spaces(m.group(1))
    return "unknown"


def extract_comment_from_notification(text: str) -> str:
    s = normalize_spaces(text)
    s = re.sub(r"^Status is online\s+", "", s, flags=re.I)
    s = re.sub(r"^.*?Unread notification\.\s*", "", s, flags=re.I)
    s = re.sub(r"^.{1,120}? (?:and \d+ others )?(?:commented on your post|replied to your comment|mentioned you in a comment|commented on this)\.\s*", "", s, flags=re.I)
    return clean_comment_text(strip_embedded_post_text(s), limit=900)


def should_reply(author: str, comment: str) -> tuple[bool, str]:
    key = normalize_key(comment)
    words = [w for w in re.split(r"\s+", key) if w]
    if owner_name_matches(author):
        return False, "own_or_unknown_author"
    if not comment or not key:
        return False, "low_value_emoji_or_reaction"
    letters_digits = re.sub(r"[^\w]", "", key, flags=re.UNICODE)
    if not letters_digits:
        return False, "low_value_emoji_or_reaction"
    if len(comment) < 8:
        return False, "empty_or_too_short"
    confirmation_phrases = {
        *SHORT_CONFIRMATIONS,
        "agree thanks", "thanks agree", "thank you", "спасибо согласен", "полностью согласен", "согласен спасибо",
    }
    if key in confirmation_phrases or (len(words) <= 3 and "?" not in key and not any(len(w) > 10 for w in words)):
        return False, "short_confirmation"
    if any(bad in key for bad in ["fuck", "идиот", "дебил", "тупой", "хуй", "пизд"]):
        return False, "toxic_or_low_value"
    if "?" in comment:
        return True, "question"
    if any(t in key for t in ["devops", "sre", "startup", "стартап", "рекрут", "ваканс", "job", "работ", "инженер", "код", "инфраструкт", "kubernetes", "terraform", "ci/cd"]):
        return True, "substantive_topic"
    if len(words) >= 12:
        return True, "substantive_comment"
    return False, "low_value"


def fallback_reply(author: str, comment: str, reason: str) -> str:
    key = normalize_key(comment)
    name = author.split()[0] if author and author != "unknown" else ""
    prefix = f"{name} " if name else ""
    if "?" in comment:
        return prefix + "хороший вопрос. Я бы тут разделял инфраструктурный код/автоматизацию и продуктовую разработку: первое — нормальная часть DevOps, второе уже совсем другая роль и другие ожидания."
    if any(t in key for t in ["соглас", "поддерж", "100%", "верно"]):
        return prefix + "да, примерно об этом и речь. Проблема не в том, что иногда нужно помочь шире своей роли, а в том, когда из этого делают постоянную модель найма."
    if any(t in key for t in ["стартап", "startup"]):
        return prefix + "у стартапа действительно больше хаоса и многорукости, но это не отменяет границы ответственности. Одно дело помочь с автоматизацией, другое — закрывать продуктовую разработку потому что команду собрали криво."
    if any(t in key for t in ["devops", "код", "инфраструкт"]):
        return prefix + "тут важная граница: инфраструктурный код, автоматизация и внутренние инструменты — да, это нормально. Продуктовые фичи как замена backend-разработчика — уже совсем другая история."
    return prefix + "да, понимаю эту мысль. Мне кажется, ключевой момент здесь — честно проговаривать ожидания по роли до найма, а не расширять их уже по ходу дела."


def codex_timeout_seconds() -> int:
    return max(1, env_int("LINKEDIN_COMMENTATOR_CODEX_TIMEOUT_SECONDS", 120))


def sanitize_codex_reply(text: str) -> str:
    """Return only the final LinkedIn reply from Codex output."""
    value = str(text or "").replace("\r", "\n").strip()
    value = re.sub(r"^```(?:\w+)?\s*", "", value).strip()
    value = re.sub(r"\s*```$", "", value).strip()
    lines = [normalize_spaces(line) for line in value.splitlines() if normalize_spaces(line)]
    if not lines:
        return ""
    if len(lines) > 1:
        # Codex sometimes prints a short explanation then the actual comment.
        value = lines[-1]
    else:
        value = lines[0]
    quoted = re.search(r"[\"“«](.{2,1200}?)[\"”»]$", value)
    if quoted:
        value = quoted.group(1)
    value = re.sub(
        r"^(?:конечно,?\s*)?(?:вот|держи)?\s*(?:короткий\s*)?(?:готовый\s*)?(?:комментарий|ответ)(?:\s+для\s+linkedin)?\s*[:—-]\s*",
        "",
        value,
        flags=re.I,
    ).strip()
    value = value.strip('"“”«»` ')
    return normalize_spaces(value)


def codex_reply(author: str, comment: str, post_excerpt: str, reason: str) -> tuple[str, str]:
    command = env_str("LINKEDIN_COMMENTATOR_CODEX_COMMAND", "")
    prompt = f"{STYLE_PROMPT}\n\nАвтор комментария: {author}\nПричина ответа: {reason}\nПост Андрея/контекст: {post_excerpt[:2400]}\nКомментарий под постом: {comment[:1800]}\n\nСгенерируй ответ Андрея:"
    if not command:
        return fallback_reply(author, comment, reason), "fallback_no_codex_command"
    try:
        proc = subprocess.run(command, input=prompt, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=codex_timeout_seconds())
        text = sanitize_codex_reply(proc.stdout)
        if proc.returncode != 0:
            return fallback_reply(author, comment, reason), f"fallback_codex_failed:exit_{proc.returncode}"
        if not text:
            return fallback_reply(author, comment, reason), "fallback_codex_empty"
        if len(text) > 1200:
            return fallback_reply(author, comment, reason), "fallback_codex_too_long"
        if len(text) >= 2:
            return text, "codex"
        return fallback_reply(author, comment, reason), "fallback_codex_too_short"
    except subprocess.TimeoutExpired:
        return fallback_reply(author, comment, reason), "fallback_codex_timeout"
    except Exception as exc:
        return fallback_reply(author, comment, reason), f"fallback_codex_exception:{exc.__class__.__name__}"


def connect_browser(playwright):
    endpoint = env_str("LINKEDIN_CDP_ENDPOINT", CDP_ENDPOINT_DEFAULT)
    try:
        browser = playwright.chromium.connect_over_cdp(endpoint, timeout=15000)
    except Exception as exc:
        return None, None, None, f"cdp_connect_failed:{exc!r}"
    contexts = list(getattr(browser, "contexts", []) or [])
    if len(contexts) != 1:
        return browser, None, None, f"expected_exactly_one_persistent_context:{len(contexts)}"
    context = contexts[0]
    try:
        page = context.new_page()
    except Exception as exc:
        return browser, context, None, f"page_create_failed:{exc!r}"
    return browser, context, page, None


def close_commentator_page(page) -> None:
    if page is None:
        return
    try:
        page.close()
    except Exception:
        pass


def page_wait(page, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except AttributeError:
        pass


def collect_notifications(page, max_items: int) -> list[dict[str, str]]:
    raw = page.evaluate("""
    () => {
      function visible(el) { const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return !!(el.offsetParent!==null && r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'); }
      const roots = Array.from(document.querySelectorAll('li, article, div[role="listitem"]')).filter(visible);
      const out=[]; const seen=new Set();
      for (const root of roots) {
        const text=(root.innerText||'').replace(/\\s+/g,' ').trim();
        if (!text || text.length<25 || text.length>2200) continue;
        if (!/(commented on your post|replied to your comment|mentioned you in a comment|commented on)/i.test(text)) continue;
        const a=root.querySelector('a[href*="/feed/update/"], a[href*="/posts/"], a[href*="/notifications/"], a[href*="/analytics/post-summary/"]');
        const profile=root.querySelector('a[href*="/in/"]');
        const href=a ? (a.href || a.getAttribute('href') || '') : '';
        const authorHref=profile ? (profile.href || profile.getAttribute('href') || '') : '';
        const key=(href||'')+'\\n'+text.slice(0,400);
        if (seen.has(key)) continue; seen.add(key);
        out.push({text, href, authorHref});
      }
      return out.slice(0, 80);
    }
    """)
    out: list[dict[str, str]] = []
    for item in raw or []:
        href = str(item.get("href") or "")
        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        out.append({"text": normalize_spaces(item.get("text")), "href": href, "authorHref": canonical_linkedin_url(item.get("authorHref") or "")})
        if len(out) >= max_items:
            break
    return out


def scan_activity_comments(page, max_items: int) -> list[dict[str, str]]:
    text = page_text(page)
    chunks = re.split(r"Feed post number \d+", text)
    out: list[dict[str, str]] = []
    for chunk in chunks[1:]:
        s = normalize_spaces(chunk)
        if not s or "Andrew Anashkin" not in s:
            continue
        if re.search(r"Andrew Anashkin (?:commented on this|replied to .+? comment)", s, re.I):
            # This is Андрей's own historic comment; use as style context only, not a candidate to answer.
            continue
        if len(s) > 80:
            out.append({"text": s[:1000], "href": getattr(page, "url", "")})
        if len(out) >= max_items:
            break
    return out




def collect_my_post_urls(page, max_posts: int, no_delay: bool) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    stable_rounds = 0
    max_rounds = max(6, min(80, max_posts // 2 + 8))
    for _ in range(max_rounds):
        raw = page.evaluate("""
        () => {
          const anchors = Array.from(document.querySelectorAll('a[href*="/feed/update/"], a[href*="/posts/"], a[href*="/analytics/post-summary/urn:li:activity:"]'));
          return anchors.map(a => a.href || a.getAttribute('href') || '').filter(Boolean);
        }
        """)
        before = len(seen)
        for href in raw or []:
            href = canonical_linkedin_url(href)
            m = re.search(r"/analytics/post-summary/(urn:li:activity:\d+)/?", href)
            if m:
                href = f"https://www.linkedin.com/feed/update/{m.group(1)}/"
            low = href.lower()
            if ('/feed/update/' not in low and '/posts/' not in low) or '/comments/' in low:
                continue
            if href not in seen:
                seen.add(href)
                urls.append(href)
                if len(urls) >= max_posts:
                    return urls
        if len(seen) == before:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 3:
            break
        try:
            page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight * 0.85))")
        except Exception:
            pass
        page_wait(page, 500 if no_delay else random.randint(1200, 2600))
    return urls[:max_posts]


def expand_post_comments(page, no_delay: bool) -> None:
    patterns = [
        r"Load more comments", r"Show previous comments", r"View .* comments", r"See more comments",
        r"Load more replies", r"Show previous replies", r"View .* replies", r"See more replies",
        r"Показать.*коммент", r"Загрузить.*коммент", r"Показать.*ответ", r"Ещё.*ответ",
    ]
    for _round in range(6):
        clicked = 0
        for pattern in patterns:
            try:
                loc = page.get_by_text(re.compile(pattern, re.I))
                count = min(loc.count(), 8)
                for _ in range(count):
                    try:
                        loc.first.click(timeout=1800)
                        clicked += 1
                        page_wait(page, 250 if no_delay else random.randint(700, 1500))
                    except Exception:
                        break
            except Exception:
                continue
        try:
            page.evaluate("window.scrollBy(0, Math.max(500, window.innerHeight * 0.6))")
        except Exception:
            pass
        page_wait(page, 300 if no_delay else random.randint(800, 1800))
        if clicked == 0 and _round >= 2:
            break


def collect_visible_post_comments(page) -> list[dict[str, str]]:
    raw = page.evaluate("""
    () => {
      function visible(el) { const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return !!(el.offsetParent!==null && r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none'); }
      function clean(s) { return (s || '').replace(/\\s+/g, ' ').trim(); }
      const rootSelectors = [
        'section.comments-comments-list', '.comments-comments-list',
        '.comments-comments-list__container', '.comments-comment-list',
        '.comments-comment-list__container', '[data-view-name*="comments"]'
      ];
      const entitySelectors = [
        'article.comments-comment-entity', '.comments-comment-entity',
        '[data-id*="comment"]', '[data-urn*="comment"]',
        '[data-comment-urn]', '[data-test-comment-urn]'
      ];
      const roots = Array.from(document.querySelectorAll(rootSelectors.join(',')));
      const directEntitySelectors = [
        'article.comments-comment-entity', '.comments-comment-entity',
        '[data-comment-urn]', '[data-test-comment-urn]',
        '[data-id*="urn:li:comment"]', '[data-urn*="urn:li:comment"]'
      ];
      const nodes = Array.from(document.querySelectorAll(directEntitySelectors.join(',')));
      for (const root of roots) {
        if (root.matches(entitySelectors.join(','))) nodes.push(root);
        for (const el of Array.from(root.querySelectorAll(entitySelectors.join(',')))) nodes.push(el);
      }
      const out=[]; const seen=new Set();
      for (const el of nodes) {
        if (!visible(el)) continue;
        const text=clean(el.innerText);
        if (!text || text.length < 12 || text.length > 2600) continue;
        if (/sent the following message|reply to conversation with|scroll quick replies/i.test(text)) continue;
        const isMessagingCard = !!el.closest('[class*="msg-overlay"], [class*="messaging"], [data-view-name*="messaging"]');
        if (isMessagingCard && /view profile/i.test(text)) continue;
        if (!/(Reply|Like|Author|Ответить|Нравится|comment|коммент)/i.test(text)) continue;
        const profile = el.querySelector('a[href*="/in/"]');
        const authorHref = profile ? (profile.href || profile.getAttribute('href') || '') : '';
        const anchors = Array.from(el.querySelectorAll('a[href]')).map(a => a.href || a.getAttribute('href') || '').filter(Boolean);
        const attrs = ['data-id', 'data-urn', 'data-comment-urn', 'data-test-comment-urn', 'data-finite-scroll-hotkey-item'];
        const attrValues = attrs.map(name => el.getAttribute(name) || '').filter(Boolean);
        const identifierText = [...anchors, ...attrValues].join(' ');
        const key=(authorHref || '')+'\\n'+identifierText+'\\n'+text.slice(0,500);
        if (seen.has(key)) continue; seen.add(key);
        out.push({text, authorHref, identifierText, isMessagingCard});
      }
      return out.slice(0, 160);
    }
    """)
    out=[]
    for item in raw or []:
        text = normalize_spaces(item.get('text'))
        messaging_marker = re.search(
            r"sent the following message|reply to conversation with|scroll quick replies",
            text,
            flags=re.I,
        )
        messaging_profile_row = item.get('isMessagingCard') and re.search(r"\bview profile\b", text, flags=re.I)
        if text and not messaging_marker and not messaging_profile_row:
            out.append({
                'text': text,
                'authorHref': canonical_linkedin_url(item.get('authorHref') or ''),
                'identifierText': _decode_linkedin_urn(item.get('identifierText') or ''),
            })
    return out


def extract_author_from_comment_node(text: str) -> str:
    s = normalize_spaces(text)
    s = re.sub(r"^Status is (?:online|reachable)\s+", "", s, flags=re.I)
    if " • " in s:
        first = normalize_spaces(s.split(" • ", 1)[0])
        if 2 <= len(first) <= 80 and re.search(r"[A-Za-zА-Яа-я]", first):
            return first
    # LinkedIn often starts comment cards with author name, then degree/status/time.
    m = re.match(r"([A-ZА-ЯЁ][^·•\n]{1,70}?)(?:\s+(?:1st|2nd|3rd|Author|Follow|Подписаться|Связаться|\d+[smhdwмо]|\d+\s*(?:min|h|d|mo|мес|дн)))\b", s)
    if m:
        return normalize_spaces(m.group(1))
    # Fallback: before typical UI words.
    m = re.match(r"(.{2,70}?)(?:\s+(?:Like|Reply|Author|Edited|See translation|Нравится|Ответить))\b", s, re.I)
    if m:
        return normalize_spaces(m.group(1))
    return "unknown"


def extract_body_from_comment_node(text: str, author: str, post_excerpt: str = "") -> str:
    s = normalize_spaces(text)
    if author and author != 'unknown':
        s = re.sub(r"^" + re.escape(author) + r"\b", "", s).strip()
    return clean_comment_text(s, author="", post_excerpt=post_excerpt, limit=900)


def comment_node_has_owner_reply(text: str, author: str) -> bool:
    """Best-effort skip for expanded threads where the owner's reply is embedded in the node text."""
    if owner_name_matches(author):
        return True
    s = normalize_spaces(text)
    owner = normalize_spaces(OWNER_NAME)
    if not owner or owner not in s:
        return False
    # If owner appears after reply/thread controls, this node likely includes an already answered branch.
    return bool(re.search(r"\b(?:Reply|Replies|Show previous replies|Load more replies|Ответить|ответ(?:а|ов)?)\b.*" + re.escape(owner), s, flags=re.I))


def collect_post_excerpt(page) -> str:
    try:
        raw = page.evaluate("""
        () => {
          const roots = Array.from(document.querySelectorAll('article, div.feed-shared-update-v2, main'));
          for (const root of roots) {
            const txt=(root.innerText||'').replace(/\\s+/g,' ').trim();
            if (txt.length > 80) return txt.slice(0, 1800);
          }
          return (document.body.innerText||'').replace(/\\s+/g,' ').trim().slice(0, 1800);
        }
        """)
        return clean_post_excerpt(raw)
    except Exception:
        return ""


def merge_existing_state_item(item: dict[str, Any] | Candidate, source: str, metadata: dict[str, Any]) -> None:
    if isinstance(item, Candidate):
        merge_candidate_metadata(item, source, metadata)
        return
    merge_source_seen(item, source)
    refresh_missing_metadata(item, metadata)


def candidate_metadata(
    *,
    key_url: str,
    post_url: str,
    author: str,
    author_profile: str,
    comment_text: str,
    post_excerpt: str = "",
) -> dict[str, Any]:
    ids = extract_linkedin_comment_identifiers(key_url)
    clean_comment = clean_comment_text(comment_text, author=author, post_excerpt=post_excerpt)
    clean_post = clean_post_excerpt(post_excerpt)
    comment_key = canonical_comment_key(key_url, author, clean_comment, author_profile)
    return {
        "id": comment_key,
        "comment_key": comment_key,
        "post_id": ids.get("post_id"),
        "comment_id": ids.get("comment_id"),
        "reply_id": ids.get("reply_id"),
        "post_url": canonical_linkedin_url(post_url),
        "author": author,
        "author_profile": canonical_author_profile(author_profile),
        "comment_text": clean_comment,
        "post_excerpt": clean_post,
    }


def candidate_dedupe_signature(*, post_url: str, author: str, comment_text: str) -> str:
    ids = extract_linkedin_comment_identifiers(post_url)
    post_part = ids.get("post_id") or canonical_linkedin_url(post_url).rstrip("/").casefold()
    text_hash = hashlib.sha256(normalize_key(comment_text).encode("utf-8", errors="ignore")).hexdigest()[:16]
    return f"{post_part}:{normalize_key(author)}:{text_hash}"


def candidate_dedupe_signature_dict(data: dict[str, Any]) -> str:
    return candidate_dedupe_signature(
        post_url=str(data.get("post_url") or ""),
        author=str(data.get("author") or ""),
        comment_text=str(data.get("comment_text") or ""),
    )


def build_candidate_signature_index(items: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for cid, item in items.items():
        data = asdict(item) if isinstance(item, Candidate) else item
        if not isinstance(data, dict):
            continue
        sig = candidate_dedupe_signature_dict(data)
        if sig and sig not in index:
            index[sig] = cid
    return index


def find_duplicate_candidate_id(items: dict[str, Any], metadata: dict[str, Any], signature_index: dict[str, str] | None = None) -> str | None:
    cid = str(metadata.get("id") or "")
    if cid and cid in items:
        return cid
    # For candidates without comment_id, LinkedIn may expose author profile in post scan but not notification.
    # Same normalized author+text under same post is one approval item.
    if metadata.get("comment_id"):
        return None
    index = signature_index if signature_index is not None else build_candidate_signature_index(items)
    return index.get(candidate_dedupe_signature_dict(metadata))


def add_candidate_signature(index: dict[str, str], candidate_id: str, data: dict[str, Any]) -> None:
    sig = candidate_dedupe_signature_dict(data)
    if sig:
        index.setdefault(sig, candidate_id)


def scan_my_posts(args: argparse.Namespace, page, status: dict[str, Any], known: dict[str, Any], remaining: int) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    known_signature_index = build_candidate_signature_index(known)
    candidate_signature_index: dict[str, str] = {}
    post_urls: list[str] = []
    for activity_url in POST_ACTIVITY_URLS:
        if len(post_urls) >= getattr(args, "max_posts", 100):
            break
        page.goto(activity_url, wait_until="domcontentloaded", timeout=60000)
        page_wait(page, 3000 if args.no_delay else random.randint(3500, 6500))
        stop = detect_stop(page)
        if stop:
            status["stop_reason"] = stop
            status["block_screenshot"] = save_stop_screenshot(page, stop)
            raise SystemExit(12)
        found = collect_my_post_urls(page, getattr(args, "max_posts", 100) - len(post_urls), args.no_delay)
        for u in found:
            if u not in post_urls:
                post_urls.append(u)
        status.setdefault("post_activity_visited", []).append({"url": activity_url, "post_urls_found": len(found), "total_unique_posts": len(post_urls)})
    for post_url in post_urls:
        if len(candidates) >= remaining:
            break
        page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
        page_wait(page, 900 if args.no_delay else random.randint(2200, 4800))
        stop = detect_stop(page)
        if stop:
            status["stop_reason"] = stop
            status["block_screenshot"] = save_stop_screenshot(page, stop)
            raise SystemExit(12)
        expand_post_comments(page, args.no_delay)
        post_excerpt = collect_post_excerpt(page)
        comments = collect_visible_post_comments(page)
        status.setdefault("posts_visited", []).append({"url": post_url, "comments_collected": len(comments), "post_excerpt": post_excerpt[:220]})
        for cm in comments:
            author = extract_author_from_comment_node(cm['text'])
            if comment_node_has_owner_reply(cm['text'], author):
                status.setdefault("skipped", []).append({"id": "", "author": author, "reason": "own_or_already_replied_branch", "excerpt": normalize_spaces(cm['text'])[:220], "post_url": post_url})
                continue
            body = extract_body_from_comment_node(cm['text'], author, post_excerpt)
            author_profile = cm.get('authorHref', '')
            identifier_url = linkedin_identifier_url(post_url, cm.get('identifierText', ''))
            metadata = candidate_metadata(
                key_url=identifier_url,
                post_url=post_url,
                author=author,
                author_profile=author_profile,
                comment_text=body,
                post_excerpt=post_excerpt,
            )
            cid = metadata["id"]
            existing_known = find_duplicate_candidate_id(known, metadata, known_signature_index)
            if existing_known:
                merge_existing_state_item(known[existing_known], "my_posts", metadata)
                continue
            existing_candidate = find_duplicate_candidate_id(candidates, metadata, candidate_signature_index)
            if existing_candidate:
                merge_candidate_metadata(candidates[existing_candidate], "my_posts", metadata)
                add_candidate_signature(candidate_signature_index, existing_candidate, asdict(candidates[existing_candidate]))
                continue
            ok, reason = should_reply(author, metadata["comment_text"])
            if not ok:
                status.setdefault("skipped", []).append({"id": cid, "author": author, "reason": reason, "excerpt": metadata["comment_text"][:220], "post_url": post_url})
                continue
            reply, source = codex_reply(author, metadata["comment_text"], metadata["post_excerpt"], reason)
            candidates[cid] = candidate_from_metadata(
                source="my_posts",
                post_url=post_url,
                author=author,
                author_profile=author_profile,
                comment_text=metadata["comment_text"],
                post_excerpt=metadata["post_excerpt"],
                reply=reply,
                reason=f"{reason};generator={source}",
                identifier_url=identifier_url,
            )
            add_candidate_signature(candidate_signature_index, cid, asdict(candidates[cid]))
            if len(candidates) >= remaining:
                break
    status["posts_discovered"] = len(post_urls)
    return list(candidates.values())


def scan_candidates(args: argparse.Namespace, status: dict[str, Any], known: dict[str, Any]) -> list[Candidate]:
    candidates: dict[str, Candidate] = {}
    known_signature_index = build_candidate_signature_index(known)
    candidate_signature_index: dict[str, str] = {}
    with sync_playwright() as p:
        browser, context, page, err = connect_browser(p)
        if err:
            status["stop_reason"] = err
            exit_with_persisted_status(status, 11)
        try:
            for url in NOTIFICATION_URLS:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page_wait(page, 1500 if args.no_delay else random.randint(2500, 5200))
                stop = detect_stop(page)
                if stop:
                    status["stop_reason"] = stop
                    status["block_screenshot"] = save_stop_screenshot(page, stop)
                    raise SystemExit(12)
                items = collect_notifications(page, args.max_items * 3)
                status.setdefault("visited", []).append({"url": url, "collected": len(items)})
                for item in items:
                    author = extract_author_from_notification(item["text"])
                    comment = extract_comment_from_notification(item["text"])
                    href = item.get("href", "")
                    author_profile = item.get("authorHref", "")
                    metadata = candidate_metadata(
                        key_url=href,
                        post_url=href,
                        author=author,
                        author_profile=author_profile,
                        comment_text=comment[:900],
                        post_excerpt=item["text"],
                    )
                    cid = metadata["id"]
                    existing_known = find_duplicate_candidate_id(known, metadata, known_signature_index)
                    if existing_known:
                        merge_existing_state_item(known[existing_known], "notifications", metadata)
                        continue
                    existing_candidate = find_duplicate_candidate_id(candidates, metadata, candidate_signature_index)
                    if existing_candidate:
                        merge_candidate_metadata(candidates[existing_candidate], "notifications", metadata)
                        add_candidate_signature(candidate_signature_index, existing_candidate, asdict(candidates[existing_candidate]))
                        continue
                    ok, reason = should_reply(author, metadata["comment_text"])
                    if not ok:
                        status.setdefault("skipped", []).append({"id": cid, "author": author, "reason": reason, "excerpt": metadata["comment_text"][:220]})
                        continue
                    reply, source = codex_reply(author, metadata["comment_text"], metadata["post_excerpt"], reason)
                    candidates[cid] = candidate_from_metadata(
                        source="notifications",
                        post_url=href,
                        author=author,
                        author_profile=author_profile,
                        comment_text=metadata["comment_text"],
                        post_excerpt=metadata["post_excerpt"],
                        reply=reply,
                        reason=f"{reason};generator={source}",
                        identifier_url=href,
                    )
                    add_candidate_signature(candidate_signature_index, cid, asdict(candidates[cid]))
                    if len(candidates) >= args.max_items:
                        break
                if len(candidates) >= args.max_items:
                    break
            needs_post_enrichment = any(not clean_post_excerpt(c.post_excerpt) for c in candidates.values())
            if getattr(args, "scan_posts", True) and (len(candidates) < args.max_items or needs_post_enrichment):
                remaining_slots = args.max_items - len(candidates)
                post_candidates = scan_my_posts(args, page, status, {**known, **candidates}, max(remaining_slots, 1 if needs_post_enrichment else 0))
                for c in post_candidates:
                    cdata = asdict(c)
                    existing_id = find_duplicate_candidate_id(candidates, cdata, candidate_signature_index)
                    if existing_id:
                        merge_candidate_metadata(candidates[existing_id], "my_posts", cdata)
                        add_candidate_signature(candidate_signature_index, existing_id, asdict(candidates[existing_id]))
                    elif len(candidates) < args.max_items:
                        candidates[c.id] = c
                        add_candidate_signature(candidate_signature_index, c.id, cdata)
            # Activity comments page is retained as a future fallback/evidence source.
            for url in ACTIVITY_URLS:
                if len(candidates) >= args.max_items:
                    break
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page_wait(page, 1000)
                status.setdefault("activity_probe", []).append({"url": url, "items": len(scan_activity_comments(page, 5))})
        finally:
            try:
                page.close()
            except Exception:
                pass
    return list(candidates.values())


def format_approval_message(c: Candidate) -> str:
    return (
        "🐧 LinkedIn commentator · нужен ответ?\n\n"
        f"Автор: {c.author}\n"
        f"Причина: {c.reason}\n"
        f"Пост: {c.post_url or 'unknown'}\n\n"
        f"Комментарий:\n{c.comment_text}\n\n"
        f"Предложенный ответ:\n{c.reply}\n\n"
        f"id: {c.id}"
    )


def telegram_callback_token_for_id(item_id: str) -> str:
    """Deterministic short opaque token for Telegram callback_data.

    Telegram limits callback_data to 64 bytes. LinkedIn-derived ids can be
    long and colon-heavy, so callbacks carry only this token and state stores
    the exact token->item mapping. The full item id is never truncated.
    """
    digest = hashlib.sha256(f"linkedin_commentator_callback:{item_id}".encode("utf-8")).hexdigest()
    return f"ct_{digest[:24]}"


def ensure_telegram_callback_token(item: dict[str, Any]) -> str:
    token = str(item.get("telegram_callback_token") or "").strip()
    if token:
        return token
    item_id = str(item.get("id") or item.get("comment_key") or "").strip()
    token = telegram_callback_token_for_id(item_id)
    item["telegram_callback_token"] = token
    return token


def telegram_callback_data(action: str, token: str) -> str:
    data = f"li_comment:{action}:{token}"
    if len(data.encode("utf-8")) > 64:
        raise ValueError("telegram_callback_data_too_long")
    return data


def resolve_callback_item(state: dict[str, Any], key: str) -> tuple[str, dict[str, Any] | None]:
    items = state.setdefault("items", {})
    if not isinstance(items, dict):
        return key, None
    for item_key, item in items.items():
        if isinstance(item, dict) and str(item.get("telegram_callback_token") or "") == key:
            return str(item.get("id") or item_key), item
    item = items.get(key)
    return key, item if isinstance(item, dict) else None


def telegram_send_error_status(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"telegram_send_failed_http_{int(exc.code)}"
    text = str(exc)
    m = re.search(r"HTTP Error\s+(\d{3})", text, re.I)
    if m:
        return f"telegram_send_failed_http_{m.group(1)}"
    if isinstance(exc, urllib.error.URLError):
        return "telegram_send_failed_network"
    return "telegram_send_failed_exception"


def is_retryable_telegram_status(status: str) -> bool:
    return status in RETRYABLE_TELEGRAM_STATUSES or status.startswith("telegram_send_failed_")


def send_approval(c: Candidate, dry_run: bool, send_enabled: bool, callback_token: str | None = None) -> tuple[str, str | None]:
    if dry_run or not send_enabled:
        return "not_sent_dry_run_or_disabled", None
    token, chat_id = telegram_config()
    if not token or not chat_id:
        return "telegram_config_missing", None
    callback_token = callback_token or telegram_callback_token_for_id(c.id)
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": telegram_callback_data("approve", callback_token)},
        {"text": "❌ Reject", "callback_data": telegram_callback_data("reject", callback_token)},
    ]]}
    last_message_id = None
    try:
        for i, chunk in enumerate(split_message(format_approval_message(c))):
            payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
            if i == 0:
                payload["reply_markup"] = keyboard
            res = telegram_api(token, "sendMessage", payload)
            if res.get("ok") is False:
                return "telegram_send_failed_api", None
            last_message_id = str(((res.get("result") or {}).get("message_id")) or "")
    except Exception as exc:
        if last_message_id:
            return "sent", last_message_id
        return telegram_send_error_status(exc), None
    return "sent", last_message_id


RETRYABLE_TELEGRAM_STATUSES = {"telegram_config_missing", "not_sent_dry_run_or_disabled"}


def candidate_from_state_item(item: dict[str, Any]) -> Candidate | None:
    """Convert a persisted proposed state item back to a Candidate without rescanning LinkedIn."""
    try:
        cid = str(item.get("id") or item.get("comment_key") or "").strip()
        if not cid:
            return None
        return Candidate(
            id=cid,
            comment_key=str(item.get("comment_key") or cid),
            post_id=item.get("post_id") if item.get("post_id") in (None, "") else str(item.get("post_id")),
            comment_id=item.get("comment_id") if item.get("comment_id") in (None, "") else str(item.get("comment_id")),
            reply_id=item.get("reply_id") if item.get("reply_id") in (None, "") else str(item.get("reply_id")),
            source_seen=list(item.get("source_seen") or []),
            post_url=str(item.get("post_url") or ""),
            author=str(item.get("author") or "unknown"),
            author_profile=str(item.get("author_profile") or ""),
            comment_text=str(item.get("comment_text") or ""),
            post_excerpt=str(item.get("post_excerpt") or ""),
            reply=str(item.get("reply") or ""),
            reason=str(item.get("reason") or ""),
        )
    except Exception:
        return None


def existing_pending_telegram_retry_candidates(state: dict[str, Any], limit: int) -> list[tuple[str, dict[str, Any], Candidate]]:
    items = state.get("items") or {}
    if not isinstance(items, dict) or limit <= 0:
        return []
    seen_ids: set[str] = set()
    ranked: list[tuple[tuple[int, str, int, str], str, dict[str, Any], Candidate]] = []
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        if item_decision(item) != DECISION_PROPOSED:
            continue
        if item.get("telegram_message_id"):
            continue
        tg_status = str(item.get("telegram_status") or "")
        if not is_retryable_telegram_status(tg_status):
            continue
        candidate = candidate_from_state_item(item)
        if candidate is None or candidate.id in seen_ids:
            continue
        seen_ids.add(candidate.id)
        status_rank = 0 if tg_status == "telegram_config_missing" else 1
        created_at = str(item.get("created_at") or "")
        created_dt = parse_iso_datetime(created_at)
        created_rank = -created_dt.timestamp() if created_dt else 0
        generator_rank = 0 if "generator=codex" in str(item.get("reason") or "").lower() else 1
        ranked.append(((status_rank, created_rank, generator_rank, candidate.id), str(key), item, candidate))
    ranked.sort(key=lambda row: row[0])
    return [(key, item, candidate) for _rank, key, item, candidate in ranked[:limit]]


def retry_existing_pending_telegram_approvals(args: argparse.Namespace, state: dict[str, Any], status: dict[str, Any], send_telegram: bool) -> None:
    if args.dry_run or not send_telegram:
        return
    retried = 0
    failed = 0
    for _key, item, candidate in existing_pending_telegram_retry_candidates(state, int(getattr(args, "max_items", 0) or 0)):
        callback_token = ensure_telegram_callback_token(item)
        save_json(STATE_PATH, state)
        tg_status, msg_id = send_approval(candidate, args.dry_run, send_telegram, callback_token=callback_token)
        if tg_status == "sent":
            mark_telegram_sent(item, msg_id)
            status["telegram_sent"] += 1
            retried += 1
        else:
            item["telegram_status"] = tg_status
            item["telegram_message_id"] = msg_id
            failed += 1
    if retried or failed:
        status["existing_telegram_retried"] = retried
        status["existing_telegram_retry_failed"] = failed
        save_json(STATE_PATH, state)
        save_json(STATUS_PATH, status)



def poll_approvals(state: dict[str, Any], dry_run: bool) -> list[dict[str, str]]:
    if dry_run:
        return []
    token, _chat_id = telegram_config()
    if not token:
        return []
    offset = int(state.get("telegram_update_offset") or 0)
    events: list[dict[str, str]] = []
    try:
        res = telegram_api(token, "getUpdates", {"offset": offset, "timeout": 1, "allowed_updates": json.dumps(["callback_query"])}, timeout=8)
    except Exception as exc:
        state.setdefault("warnings", []).append(f"telegram_getUpdates_failed:{exc!r}")
        return []
    for upd in res.get("result", []) or []:
        offset = max(offset, int(upd.get("update_id", 0)) + 1)
        cq = upd.get("callback_query") or {}
        data = str(cq.get("data") or "")
        m = re.match(r"li_comment:(approve|reject):(.+)$", data)
        if not m:
            continue
        action, callback_key = m.group(1), m.group(2)
        callback_id = str(cq.get("id") or "")
        resolved_id, item = resolve_callback_item(state, callback_key)
        if item:
            changed, reason = apply_callback_decision(item, action, callback_id)
        else:
            changed, reason = False, "unknown_item_ignored"
            state.setdefault("state_events", []).append({"at": now_iso(), "event": "telegram_callback_ignored", "reason": reason, "id": callback_key, "action": action})
        events.append({"id": resolved_id, "action": action, "changed": str(bool(changed)).lower(), "reason": reason})
        try:
            telegram_api(token, "answerCallbackQuery", {"callback_query_id": callback_id, "text": "Принято"})
        except Exception:
            pass
    state["telegram_update_offset"] = offset
    return events


def publish_reply_for_item(item: dict[str, Any], args: argparse.Namespace) -> tuple[bool, str]:
    pause_reason = publishing_pause_reason()
    if pause_reason:
        return False, pause_reason
    if args.dry_run or not args.publish_approved:
        return False, "publish_disabled_or_dry_run"
    post_url = item.get("post_url") or ""
    reply = item.get("reply") or ""
    comment = item.get("comment_text") or ""
    if not post_url or not reply:
        return False, "missing_post_url_or_reply"
    with sync_playwright() as p:
        browser, context, page, err = connect_browser(p)
        if err:
            return False, err
        try:
            page.goto(post_url, wait_until="domcontentloaded", timeout=60000)
            page_wait(page, 3500)
            stop = detect_stop(page)
            if stop:
                return False, f"linkedin_stop:{stop}"
            # Expand visible comments when LinkedIn hides them.
            for pattern in [r"Load more comments", r"Show previous comments", r"View .* comments", r"Load more replies"]:
                loc = page.get_by_text(re.compile(pattern, re.I))
                for _ in range(min(loc.count(), 3)):
                    try:
                        loc.first.click(timeout=2500)
                        page_wait(page, 1200)
                    except Exception:
                        break
            before_thread = find_linkedin_comment_thread(page, item)
            if not before_thread.get("found"):
                return False, "target_comment_not_found"
            exists, exists_reason = owner_reply_exists(before_thread, reply)
            if exists:
                return True, exists_reason

            clicked = click_reply_for_comment_thread(page, item)
            if not clicked:
                return False, "reply_button_not_found"
            page_wait(page, 1500)
            editors = page.locator('[contenteditable="true"]')
            if editors.count() < 1:
                return False, "reply_editor_not_found"
            editors.last.fill(reply, timeout=8000)
            page_wait(page, 800)
            # Click visible Reply/Post button near the editor. This is attempted at
            # most once per run; a later verification miss becomes manual review,
            # never a blind second submit.
            btn = page.get_by_role("button", name=re.compile(r"^(Reply|Post|Отправить|Ответить)$", re.I))
            if btn.count() < 1:
                return False, "submit_button_not_found"
            btn.last.click(timeout=8000)
            page_wait(page, 2500)
            after_thread = find_linkedin_comment_thread(page, item)
            verified, verified_reason = owner_reply_exists(after_thread, reply)
            if verified:
                return True, "published_verified" if verified_reason == "already_replied_on_linkedin" else verified_reason
            return False, "published_unverified"
        finally:
            try:
                page.close()
            except Exception:
                pass


def write_drafts(status: dict[str, Any], state: dict[str, Any]) -> None:
    lines = ["# LinkedIn commentator drafts", "", f"Generated: {status.get('finished_at') or status.get('started_at')}", ""]
    report_seen_signatures: set[str] = set()
    for item in state.get("items", {}).values():
        if item.get("decision") in {"rejected", "published"}:
            continue
        author = str(item.get("author") or "")
        post_url = str(item.get("post_url") or "")
        post_excerpt = clean_post_excerpt(item.get("post_excerpt", ""))
        comment_text = clean_comment_text(item.get("comment_text", ""), author=author, post_excerpt=post_excerpt)
        if not post_excerpt:
            status.setdefault("drafts_skipped", []).append({"id": item.get("id"), "author": author, "reason": "missing_clean_post_excerpt"})
            continue
        ok, skip_reason = should_reply(author, comment_text)
        if not ok:
            status.setdefault("drafts_skipped", []).append({"id": item.get("id"), "author": author, "reason": skip_reason})
            continue
        signature = candidate_dedupe_signature(post_url=post_url, author=author, comment_text=comment_text)
        if signature in report_seen_signatures:
            status.setdefault("drafts_skipped", []).append({"id": item.get("id"), "author": author, "reason": "duplicate_report_signature"})
            continue
        report_seen_signatures.add(signature)
        source_seen = item.get("source_seen") or []
        if isinstance(source_seen, list):
            source_seen_text = ",".join(str(x) for x in source_seen if x)
        else:
            source_seen_text = str(source_seen or "")
        lines += [
            f"## {author} · {item.get('id')}",
            f"- status: {item.get('decision')}",
            f"- post: {post_url}",
            f"- reason: {item.get('reason')}",
            *([f"- source_seen: {source_seen_text}"] if source_seen_text else []),
            "",
            "Post:", post_excerpt, "",
            "Comment:", comment_text, "",
            "Reply:", item.get("reply", ""), "",
        ]
    DRAFTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DRAFTS_PATH.write_text("\n".join(lines), encoding="utf-8")


def build_health_summary(
    state: dict[str, Any],
    status: dict[str, Any],
    *,
    previous_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = state.get("items") if isinstance(state, dict) else {}
    items = items if isinstance(items, dict) else {}
    counts: dict[str, int] = {}
    manual_review = 0
    for item in items.values():
        if not isinstance(item, dict):
            continue
        decision = item_decision(item)
        counts[decision] = counts.get(decision, 0) + 1
        if item.get("requires_manual_review") or decision == DECISION_PUBLISH_FAILED:
            manual_review += 1
    duplicate_skips = sum(
        1 for row in status.get("skipped", [])
        if isinstance(row, dict) and "duplicate" in str(row.get("reason") or "")
    ) + sum(
        1 for row in status.get("drafts_skipped", [])
        if isinstance(row, dict) and "duplicate" in str(row.get("reason") or "")
    )
    previous = previous_status if isinstance(previous_status, dict) else {}
    successful = not status.get("stop_reason")
    last_success_at = status.get("finished_at") if successful else previous.get("last_success_at")
    return {
        "last_success_at": last_success_at,
        "generator_counts": dict(status.get("generator_counts") or {"codex": 0, "fallback": 0}),
        "codex_failures": int(status.get("codex_failures") or 0),
        "candidates_new": int(status.get("candidates_new") or 0),
        "telegram_sent": int(status.get("telegram_sent") or 0),
        "published": int(status.get("published") or 0),
        "stop_reason": status.get("stop_reason"),
        "state_machine_counts": counts,
        "duplicate_skips": duplicate_skips,
        "pending_approvals": counts.get(DECISION_SENT_TO_TELEGRAM, 0) + counts.get(DECISION_APPROVED, 0),
        "manual_review": manual_review,
    }


def update_watchdog(status: dict[str, Any], previous_status: dict[str, Any]) -> None:
    zero_posts = int(previous_status.get("consecutive_zero_posts") or 0) + 1 if int(status.get("posts_discovered") or 0) == 0 else 0
    status["consecutive_zero_posts"] = zero_posts
    zero_threshold = max(1, env_int("LINKEDIN_COMMENTATOR_ZERO_POSTS_ALERT_RUNS", 3))
    if zero_posts >= zero_threshold:
        write_alert("zero_posts", "zero_posts_threshold_reached", consecutive_runs=zero_posts, threshold=zero_threshold)
    failures = int(status.get("codex_failures") or 0)
    previous_failures = int(previous_status.get("consecutive_codex_failure_runs") or 0)
    status["consecutive_codex_failure_runs"] = previous_failures + 1 if failures else 0
    codex_threshold = max(1, env_int("LINKEDIN_COMMENTATOR_CODEX_FAILURE_ALERT_RUNS", 3))
    if status["consecutive_codex_failure_runs"] >= codex_threshold:
        write_alert(
            "codex_failures",
            "codex_failure_threshold_reached",
            consecutive_runs=status["consecutive_codex_failure_runs"],
            threshold=codex_threshold,
        )


def _initial_status(args: argparse.Namespace) -> dict[str, Any]:
    pauses = pause_status()
    return {
        "run_id": datetime.now(BA).strftime("%Y%m%d_%H%M%S"),
        "started_at": now_iso(),
        "dry_run": bool(args.dry_run),
        "send_telegram": bool(getattr(args, "send_telegram", False)),
        "publish_approved": bool(getattr(args, "publish_approved", False)),
        "max_items": args.max_items,
        "max_posts": getattr(args, "max_posts", 100),
        "scan_posts": getattr(args, "scan_posts", True),
        "candidates_new": 0,
        "telegram_sent": 0,
        "published": 0,
        "rejected": 0,
        "skipped": [],
        "generator_counts": {"codex": 0, "fallback": 0},
        "codex_failures": 0,
        "stop_reason": None,
        "state_path": str(STATE_PATH),
        "status_path": str(STATUS_PATH),
        "drafts_path": str(DRAFTS_PATH),
        "alert_path": str(ALERT_PATH),
        **pauses,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    ensure_dirs()
    previous_status = load_json(STATUS_PATH, {})
    send_telegram = bool(getattr(args, "send_telegram", False))
    publish_approved = bool(getattr(args, "publish_approved", False))
    status = _initial_status(args)
    save_json(STATUS_PATH, status)

    if status["pause_all"]:
        status["stop_reason"] = "paused_all" if status["pause_all_env"] else "paused_all_file"
        status["finished_at"] = now_iso()
        status.update(build_health_summary({"items": {}}, status, previous_status=previous_status))
        status["health"] = build_health_summary({"items": {}}, status, previous_status=previous_status)
        save_json(STATUS_PATH, status)
        print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
        return status

    try:
        loaded_state = load_state()
        state, state_normalized = normalize_state(loaded_state)
    except StateCorruptionError as exc:
        status["stop_reason"] = "state_corruption"
        status["state_error"] = str(exc)
        status["finished_at"] = now_iso()
        write_alert("state_corruption", str(exc))
        status.update(build_health_summary({"items": {}}, status, previous_status=previous_status))
        save_json(STATUS_PATH, status)
        raise SystemExit(13)
    status["state_normalized"] = state_normalized

    decisions = poll_approvals(state, args.dry_run)
    status["approval_events"] = decisions

    for cid, item in list(state.get("items", {}).items()):
        if not publish_approved or args.dry_run or status["pause_publishing"]:
            if item.get("decision") == DECISION_REJECTED:
                status["rejected"] += 1
            if publish_approved and status["pause_publishing"] and item_decision(item) == DECISION_APPROVED:
                status.setdefault("publish_skipped", []).append({"id": cid, "reason": publishing_pause_reason() or "publishing_paused"})
            continue
        acquired, lock_reason = acquire_publish_lock(item, status["run_id"])
        if acquired:
            # Persist publishing lock before the external LinkedIn action. This is
            # the item-level restart/race guard: overlapping runs see publishing.
            save_json(STATE_PATH, state)
            ok, reason = publish_reply_for_item(item, argparse.Namespace(**{**vars(args), "publish_approved": publish_approved}))
            complete_publish_attempt(item, status["run_id"], ok, reason)
            if ok:
                status["published"] += 1
            elif reason.startswith("linkedin_stop:"):
                status["stop_reason"] = reason
                write_alert("linkedin_blocker", reason)
                save_json(STATE_PATH, state)
                save_json(STATUS_PATH, status)
                raise SystemExit(12)
        elif lock_reason in {"publishing_lock_fresh", "max_publish_attempts_reached", "publish_failed_manual_review"} or lock_reason.startswith("not_approved:"):
            status.setdefault("publish_skipped", []).append({"id": cid, "reason": lock_reason})
        if item.get("decision") == DECISION_REJECTED:
            status["rejected"] += 1

    known = state.setdefault("items", {})
    retry_existing_pending_telegram_approvals(args, state, status, send_telegram)
    try:
        candidates = scan_candidates(args, status, known)
    except SystemExit:
        reason = str(status.get("stop_reason") or "linkedin_blocker")
        if any(token in reason.lower() for token in ("captcha", "security", "login", "authwall", "checkpoint", "challenge", "rate", "limit", "safeguard", "restricted")):
            write_alert("linkedin_blocker", reason)
        status["finished_at"] = now_iso()
        status.update(build_health_summary(state, status, previous_status=previous_status))
        save_json(STATE_PATH, state)
        save_json(STATUS_PATH, status)
        raise
    for c in candidates:
        item = asdict(c)
        item.update({"decision": DECISION_PROPOSED, "created_at": now_iso(), "telegram_status": None, "telegram_message_id": None})
        known[c.id] = item
        callback_token = ensure_telegram_callback_token(item)
        save_json(STATE_PATH, state)
        if should_send_telegram_approval(item):
            tg_status, msg_id = send_approval(c, args.dry_run, send_telegram, callback_token=callback_token)
            if tg_status == "sent":
                mark_telegram_sent(item, msg_id)
                status["telegram_sent"] += 1
            else:
                item["telegram_status"] = tg_status
                item["telegram_message_id"] = msg_id
        else:
            item["telegram_status"] = "not_sent_already_sent_or_terminal"
    status["candidates_new"] = len(candidates)
    for candidate in candidates:
        generator = str(candidate.reason).split("generator=", 1)[-1] if "generator=" in str(candidate.reason) else "unknown"
        if generator == "codex":
            status["generator_counts"]["codex"] += 1
        else:
            status["generator_counts"]["fallback"] += 1
            if generator.startswith("fallback_codex"):
                status["codex_failures"] += 1
    status["known_items"] = len(known)
    status["finished_at"] = now_iso()
    write_drafts(status, state)
    update_watchdog(status, previous_status if isinstance(previous_status, dict) else {})
    summary = build_health_summary(state, status, previous_status=previous_status)
    status.update(summary)
    status["health"] = summary
    save_json(STATE_PATH, state)
    save_json(STATUS_PATH, status)
    print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", default=env_bool("LINKEDIN_COMMENTATOR_DRY_RUN", env_bool("LINKEDIN_COMMENT_DRY_RUN", True)))
    parser.add_argument("--send-telegram", action="store_true", default=env_bool("LINKEDIN_COMMENTATOR_SEND_TELEGRAM", False))
    parser.add_argument("--publish-approved", action="store_true", default=env_bool("LINKEDIN_COMMENTATOR_PUBLISH_APPROVED", False))
    parser.add_argument("--max-items", type=int, default=env_int("LINKEDIN_COMMENTATOR_MAX_ITEMS", env_int("LINKEDIN_COMMENT_MAX_ITEMS", 20)))
    parser.add_argument("--max-posts", type=int, default=env_int("LINKEDIN_COMMENTATOR_MAX_POSTS", 100))
    parser.add_argument("--scan-posts", action="store_true", default=env_bool("LINKEDIN_COMMENTATOR_SCAN_POSTS", True))
    parser.add_argument("--no-delay", action="store_true", default=env_bool("LINKEDIN_COMMENTATOR_NO_DELAY", False))
    parser.add_argument("--preflight", action="store_true", help="run local readiness checks only; no browser/network actions")
    args = parser.parse_args()
    if args.preflight:
        report = production_preflight(args)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), flush=True)
        return 0 if report["ready"] else 1
    try:
        run(args)
        return 0
    except SystemExit as exc:
        return int(exc.code or 0)
    except PlaywrightTimeoutError as exc:
        print(json.dumps({"event": "fatal", "error": repr(exc)}, ensure_ascii=False), flush=True)
        return 14
    except Exception as exc:
        print(json.dumps({"event": "fatal", "error": repr(exc)}, ensure_ascii=False), flush=True)
        return 14 if "session" not in str(exc).lower() else 11


if __name__ == "__main__":
    raise SystemExit(main())
