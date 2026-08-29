#!/usr/bin/env python3
"""LinkedIn feed liker.

Connects to the shared LinkedIn Chromium over CDP and uses its existing persistent context.
Likes up to N posts from the feed with pattern: like 1st, skip 2nd/3rd, like 4th, ...
Writes exact liked post URLs and verification screenshots.

Exit codes:
  0 success / partial success without platform block
  2 auth/session problem
  12 LinkedIn captcha/security/rate-limit/checkpoint detected
  14 unexpected automation error
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    value = str(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return json.loads(raw)


BASE = Path(env_str("LINKEDIN_LEGACY_STATE_DIR", "/Users/deploydog-ai/LinkedIn/shared/legacy_state"))
OUT_DIR = Path(env_str("LINKEDIN_LIKE_OUT_DIR", str(BASE / "liked_posts")))
STATUS_PATH = Path(env_str("LINKEDIN_LIKE_STATUS_PATH", str(OUT_DIR / "linkedin_liked_posts_status.json")))
LATEST_PATH = Path(env_str("LINKEDIN_LIKE_LATEST_PATH", str(OUT_DIR / "linkedin_liked_posts_latest.json")))
DEFAULT_FEED_URL = env_str("LINKEDIN_FEED_URL", "https://www.linkedin.com/feed/")
LIKE_DELAY_MIN = float(env_str("LINKEDIN_LIKE_DELAY_MIN", "1.0"))
LIKE_DELAY_MAX = float(env_str("LINKEDIN_LIKE_DELAY_MAX", "32.0"))
REACTION_MODE = env_str("LINKEDIN_LIKE_REACTION_MODE", "like_only").strip().lower()
DEFAULT_ALLOWED_REACTIONS = ["Like", "Celebrate", "Support", "Love", "Insightful", "Funny"]
ALLOWED_REACTIONS = env_json("LINKEDIN_LIKE_ALLOWED_REACTIONS_JSON", DEFAULT_ALLOWED_REACTIONS)

DEFAULT_STOP_PATTERNS = [
    "sign in",
    "authwall",
    "captcha",
    "security verification",
    "verify your identity",
    "checkpoint",
    "challenge",
    "unusual activity",
    "temporarily restricted",
    "try again later",
    "rate limit",
    "rate-limit",
    "safeguard",
    "daily limit",
    "daily-limit",
]
STOP_PATTERNS = list(dict.fromkeys(env_json("LINKEDIN_STOP_PATTERNS_JSON", DEFAULT_STOP_PATTERNS) + DEFAULT_STOP_PATTERNS))
AMBIGUOUS_BODY_STOP_PATTERNS = {"challenge", "checkpoint", "security", "sign in"}
BODY_STOP_PATTERNS = [pattern for pattern in STOP_PATTERNS if pattern.strip().lower() not in AMBIGUOUS_BODY_STOP_PATTERNS]
URL_STOP_TOKENS = (
    "/login",
    "authwall",
    "checkpoint",
    "challenge",
    "captcha",
    "security",
    "rate-limit",
    "safeguard",
    "daily-limit",
)
LINKEDIN_CDP_ENDPOINT_DEFAULT = "http://linkedin-browser:9222"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def delay(lo: float | None = None, hi: float | None = None) -> None:
    """Human-like random pause configurable from .env."""
    lo = LIKE_DELAY_MIN if lo is None else max(LIKE_DELAY_MIN, float(lo))
    hi = LIKE_DELAY_MAX if hi is None else max(LIKE_DELAY_MAX, float(hi), lo)
    time.sleep(random.uniform(lo, hi))


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def safe_name(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", text).strip("_")[:80] or "item"


def body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def has_visible_login_form(page, text: str) -> bool:
    """Fail closed on a visible LinkedIn login form without matching generic feed text."""
    for selector in ('input[type="password"]', 'input[name="session_password"]', '#password'):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            pass
    compact = re.sub(r"\s+", " ", text).lower()
    has_password = re.search(r"\bpassword\b", compact) is not None
    has_login_identity = re.search(r"\b(email|phone|username)\b", compact) is not None
    has_signin = re.search(r"\bsign\s+in\b", compact) is not None
    return bool(has_password and (has_login_identity or has_signin))


def detect_stop(page) -> str | None:
    url = page.url.lower()
    if any(token in url for token in URL_STOP_TOKENS):
        return f"url:{page.url}"
    text = body_text(page).lower()
    if has_visible_login_form(page, text):
        return "login_form"
    for pattern in BODY_STOP_PATTERNS:
        if pattern in text:
            return pattern
    return None


def connect_browser(playwright) -> tuple[Any, Any | None, Any | None, str | None]:
    endpoint = env_str("LINKEDIN_CDP_ENDPOINT", LINKEDIN_CDP_ENDPOINT_DEFAULT)
    browser = playwright.chromium.connect_over_cdp(endpoint)
    contexts = list(getattr(browser, "contexts", []) or [])
    if len(contexts) != 1:
        return browser, None, None, f"expected_exactly_one_persistent_context:{len(contexts)}"
    context = contexts[0]
    page = context.new_page()
    return browser, context, page, None


def close_postliker_page(page) -> None:
    if page is None:
        return
    try:
        page.close()
    except Exception as exc:
        emit("page_close_failed", error=repr(exc))


def normalized_allowed_reactions() -> set[str]:
    if not isinstance(ALLOWED_REACTIONS, list):
        return set(DEFAULT_ALLOWED_REACTIONS)
    return {str(item).strip().title() for item in ALLOWED_REACTIONS if str(item).strip()}


def is_promoted_or_sponsored(text: str) -> bool:
    markers = {
        "promoted",
        "sponsored",
        "promocionado",
        "patrocinado",
        "publicidad",
        "anuncio",
        "reklama",
    }
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("•·: ").lower()
        if line in markers:
            return True
        if re.fullmatch(r"(promoted|sponsored)\s*[•·].*", line):
            return True
    return False


def select_reaction(text: str) -> tuple[str, str]:
    allowed = normalized_allowed_reactions()
    mode = REACTION_MODE if REACTION_MODE in {"like_only", "varied"} else "like_only"
    if mode == "like_only":
        return "Like", "mode_like_only"

    lower = text.lower()
    rules: list[tuple[str, str, list[str]]] = [
        (
            "Celebrate",
            "achievement_certification_new_role",
            [
                "achievement", "achieved", "certification", "certified", "certificate",
                "new role", "new position", "promoted to", "promotion", "milestone",
                "anniversary", "award", "graduated", "i’m happy to share", "i'm happy to share",
                "excited to share", "proud to", "joined", "joining",
            ],
        ),
        (
            "Support",
            "struggle_job_seeking_support_help",
            [
                "job seeking", "open to work", "looking for a job", "looking for work",
                "laid off", "layoff", "struggle", "struggling", "tough time", "hard time",
                "need help", "please help", "support", "help me", "career transition",
                "seeking", "available for", "mental health",
            ],
        ),
        (
            "Insightful",
            "technical_deep_professional_insight",
            [
                "technical", "architecture", "engineering", "software", "developer",
                "deep dive", "case study", "lesson learned", "lessons learned", "insight",
                "analysis", "framework", "strategy", "ai", "data", "api", "database",
                "cloud", "security", "product", "design", "system", "python", "javascript",
                "typescript", "react", "kubernetes", "docker", "postgres", "redis",
            ],
        ),
        (
            "Funny",
            "funny_humor_meme",
            ["funny", "humor", "humour", "meme", "lol", "😂", "🤣", "joke", "comedy"],
        ),
    ]
    for reaction, reason, keywords in rules:
        if reaction in allowed and any(keyword in lower for keyword in keywords):
            return reaction, reason
    return "Like", "fallback_like_unclear_or_not_allowed"


def reaction_confirmed(aria: str, reaction: str) -> bool:
    return aria.strip().lower() == f"reaction button state: {reaction}".lower()


def reaction_from_aria(aria: str) -> str | None:
    m = re.fullmatch(r"\s*Reaction button state:\s*([A-Za-z]+)\s*", aria or "", flags=re.I)
    if not m:
        return None
    reaction = m.group(1).title()
    if reaction == "No":
        return None
    return reaction


def normalize_post_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.split("?")[0]
    if raw.startswith("/"):
        raw = "https://www.linkedin.com" + raw
    m = re.search(r"https://www\.linkedin\.com/feed/update/(urn:li:[^/?#]+)/?", raw)
    if m:
        return f"https://www.linkedin.com/feed/update/{m.group(1)}/"
    m = re.search(r"https://www\.linkedin\.com/posts/[^?#]+", raw)
    if m:
        return m.group(0).rstrip("/") + "/"
    return None


def collect_feed_posts(page) -> list[dict[str, Any]]:
    """Collect visible post cards with reaction/menu coordinates.

    Current LinkedIn feed often does not expose the public post URL directly in
    the card. The stable URL is recovered later from the post control menu
    (`Report post`/`Embed this post` href includes updateUrn/targetUrn).
    """
    return page.evaluate(
        r"""
        () => {
          function visible(el) {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            // Feed cards can be taller than the viewport; the first actionable
            // reaction button may sit just below the fold. Collect it anyway —
            // click_reaction_for_post scrolls the owning post into view.
            return !!(el.offsetParent !== null && r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none');
          }
          function findPostRoot(el) {
            const listItem = el.closest('[role="listitem"]');
            if (listItem && listItem.querySelector('button[aria-label^="Open control menu for post"]')) return listItem;
            let node = el;
            for (let i = 0; i < 16 && node; i++) {
              const txt = (node.innerText || '').trim();
              const menu = node.querySelector('button[aria-label^="Open control menu for post"]');
              if (menu && txt.length > 20) return node;
              node = node.parentElement;
            }
            return el.closest('article') || listItem || el.parentElement;
          }
          function getUrl(root) {
            if (!root) return null;
            const anchors = Array.from(root.querySelectorAll('a[href*="/feed/update/"], a[href*="/posts/"]'));
            for (const a of anchors) {
              const href = a.href || a.getAttribute('href') || '';
              if (href.includes('/feed/update/') || href.includes('/posts/')) return href;
            }
            return null;
          }
          const buttons = Array.from(document.querySelectorAll('button[aria-label^="Reaction button state:"]'))
            .filter(visible);
          const seen = new Set();
          const posts = [];
          for (const btn of buttons) {
            const label = btn.getAttribute('aria-label') || '';
            const root = findPostRoot(btn);
            const href = getUrl(root);
            const menu = root ? root.querySelector('button[aria-label^="Open control menu for post"]') : null;
            const key = href || (root ? ((menu && menu.getAttribute('aria-label')) || root.innerText.slice(0,160)) : label);
            if (seen.has(key)) continue;
            seen.add(key);
            const r = btn.getBoundingClientRect();
            const mr = menu ? menu.getBoundingClientRect() : null;
            posts.push({
              aria: label,
              liked: /state:\s*Like/i.test(label),
              noReaction: /state:\s*no reaction/i.test(label),
              href,
              text: root ? (root.innerText || '').trim().slice(0, 500) : '',
              x: r.x + r.width / 2,
              y: r.y + r.height / 2,
              menuX: mr ? mr.x + mr.width / 2 : null,
              menuY: mr ? mr.y + mr.height / 2 : null,
              menuLabel: menu ? menu.getAttribute('aria-label') : null,
              viewportY: r.y,
              key
            });
          }
          posts.sort((a,b) => a.viewportY - b.viewportY);
          return posts;
        }
        """
    )


def extract_url_from_open_menu(page) -> str | None:
    """Extract public post URL from the currently open LinkedIn post menu."""
    hrefs = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href]'))
          .filter(el => el.offsetParent !== null)
          .map(a => a.href || a.getAttribute('href') || '')
          .filter(h => h.includes('updateUrn=') || h.includes('targetUrn='))
        """
    )
    # Prefer updateUrn (activity URL). targetUrn=share can render as
    # "This post cannot be displayed" when used directly as /feed/update/.
    for wanted_key in ("updateUrn=", "targetUrn="):
        for href in hrefs:
            if wanted_key not in href:
                continue
            m = re.search(wanted_key + r"(urn%3Ali%3A(?:activity|share)%3A\d+|urn:li:(?:activity|share):\d+)", href)
            if not m:
                continue
            urn = m.group(1).replace("%3A", ":")
            return f"https://www.linkedin.com/feed/update/{urn}/"
    return None


def recover_post_url(page, post: dict[str, Any]) -> str | None:
    direct = normalize_post_url(post.get("href"))
    if direct:
        return direct
    if post.get("menuX") is None or post.get("menuY") is None:
        return None
    page.mouse.click(float(post["menuX"]), float(post["menuY"]))
    delay(0.8, 1.5)
    url = extract_url_from_open_menu(page)
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    delay(0.2, 0.5)
    return url


def reaction_button_box(page, post: dict[str, Any]) -> dict[str, Any]:
    """Find the same post reaction button and return a clickable bounding box."""
    menu_label = post.get("menuLabel") or ""
    key = post.get("key") or ""
    return page.evaluate(
        """
        ([menuLabel, key]) => {
          function findPostRoot(el) {
            const listItem = el.closest('[role="listitem"]');
            if (listItem && listItem.querySelector('button[aria-label^="Open control menu for post"]')) return listItem;
            let node = el;
            for (let i = 0; i < 16 && node; i++) {
              const txt = (node.innerText || '').trim();
              const menu = node.querySelector('button[aria-label^="Open control menu for post"]');
              if (menu && txt.length > 20) return node;
              node = node.parentElement;
            }
            return el.closest('article') || listItem || el.parentElement;
          }
          const buttons = Array.from(document.querySelectorAll('button[aria-label^="Reaction button state:"]'));
          for (const candidateBtn of buttons) {
            const root = findPostRoot(candidateBtn);
            const menu = root ? root.querySelector('button[aria-label^="Open control menu for post"]') : null;
            const label = menu ? (menu.getAttribute('aria-label') || '') : '';
            const txt = root ? (root.innerText || '').slice(0, 500) : '';
            if ((menuLabel && label === menuLabel) || (key && ((label && key.includes(label)) || key === txt.slice(0,160)))) {
              candidateBtn.scrollIntoView({block:'center', inline:'center'});
              const r = candidateBtn.getBoundingClientRect();
              return {
                found: true,
                aria: candidateBtn.getAttribute('aria-label') || '',
                x: r.x + r.width / 2,
                y: r.y + r.height / 2,
              };
            }
          }
          return {found:false, aria:'', reason:'reaction_not_found'};
        }
        """,
        [menu_label, key],
    )


def reaction_aria_for_post(page, post: dict[str, Any]) -> str:
    box = reaction_button_box(page, post)
    if not box.get("found"):
        return f"reaction_not_found_after:{box.get('reason')}"
    return str(box.get("aria") or "")


def click_like_button(page, post: dict[str, Any]) -> str:
    box = reaction_button_box(page, post)
    if not box.get("found"):
        return f"click_failed:{box.get('reason')}"
    page.mouse.click(float(box["x"]), float(box["y"]))
    delay(1.2, 2.3)
    return reaction_aria_for_post(page, post)


def click_reaction_for_post(page, post: dict[str, Any], selected_reaction: str) -> dict[str, Any]:
    """Perform a real Playwright UI reaction action, with non-Like fallback to Like."""
    result: dict[str, Any] = {
        "selected_reaction": selected_reaction,
        "attempted_reaction": selected_reaction,
        "fallback_to_like": False,
        "confirmed_aria_after_click": "",
        "fallback_reason": None,
    }
    if selected_reaction == "Like":
        result["confirmed_aria_after_click"] = click_like_button(page, post)
        return result

    box = reaction_button_box(page, post)
    if not box.get("found"):
        result["fallback_to_like"] = True
        result["selected_reaction"] = "Like"
        result["fallback_reason"] = f"reaction_button_not_found:{box.get('reason')}"
        result["confirmed_aria_after_click"] = click_like_button(page, post)
        return result

    try:
        page.mouse.move(float(box["x"]), float(box["y"]))
        page.wait_for_timeout(2500)
        picker = page.locator(
            f'button[aria-label="React {selected_reaction}"], '
            f'button[aria-label*="React {selected_reaction}"]'
        ).first
        picker.click(timeout=5000)
        delay(1.2, 2.3)
        after = reaction_aria_for_post(page, post)
        result["confirmed_aria_after_click"] = after
        if reaction_confirmed(after, selected_reaction):
            return result
        result["fallback_reason"] = f"non_like_not_confirmed:{after}"
    except Exception as exc:
        result["fallback_reason"] = f"non_like_picker_failed:{repr(exc)}"

    result["fallback_to_like"] = True
    observed_reaction = reaction_from_aria(str(result.get("confirmed_aria_after_click") or ""))
    if observed_reaction:
        result["selected_reaction"] = observed_reaction
        return result
    result["selected_reaction"] = "Like"
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    delay(0.2, 0.5)
    result["confirmed_aria_after_click"] = click_like_button(page, post)
    return result


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def verify_post(page, url: str, idx: int, run_dir: Path, expected_reaction: str = "Like") -> dict[str, Any]:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    delay(2.0, 3.5)
    stop = detect_stop(page)
    if stop:
        shot = run_dir / f"verify_{idx:02d}_blocked.png"
        try:
            page.screenshot(path=str(shot), full_page=True)
        except Exception:
            pass
        return {"url": url, "verified": False, "expected_reaction": expected_reaction, "reason": f"blocked:{stop}", "screenshot": str(shot)}
    reacted = False
    no_reaction = False
    # Verify post state after a pause to let LinkedIn UI sync.
    delay(5.0, 7.0)
    
    controls = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('button,a'))
          .filter(el => el.offsetParent !== null)
          .map(el => ({aria: el.getAttribute('aria-label') || '', text: (el.innerText || '').trim()}))
        """
    )
    joined = "\n".join((c.get("aria", "") + " " + c.get("text", "")) for c in controls)
    expected_upper = expected_reaction.upper()
    if f"Unreact {expected_reaction}" in joined or ("View Andrew Anashkin" in joined and f"reacted with {expected_upper}" in joined):
        reacted = True
    elif "Reaction button state: no reaction" in joined or "React Like to" in joined:
        no_reaction = True
    shot = run_dir / f"verify_{idx:02d}_{'reacted' if reacted else 'not_reacted'}.png"
    try:
        page.screenshot(path=str(shot), full_page=True)
    except Exception:
        pass
    return {"url": url, "verified": bool(reacted), "expected_reaction": expected_reaction, "has_no_reaction_button": bool(no_reaction), "screenshot": str(shot)}


def run(max_likes: int, feed_url: str, headless: bool, verify: bool) -> dict[str, Any]:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    status: dict[str, Any] = {
        "started_at": now_iso(),
        "feed_url": feed_url,
        "max_likes": max_likes,
        "pattern": "like feed positions 1,4,7,... (skip two posts between likes)",
        "reaction_mode": REACTION_MODE if REACTION_MODE in {"like_only", "varied"} else "like_only",
        "allowed_reactions": sorted(normalized_allowed_reactions()),
        "liked": [],
        "skipped": [],
        "skip_counts": {"promoted_or_sponsored": 0},
        "promoted_or_sponsored_skip_count": 0,
        "errors": [],
        "verification": [],
        "stop_reason": None,
        "run_dir": str(run_dir),
    }
    save_json(STATUS_PATH, status)

    page = None
    with sync_playwright() as p:
        _browser, _context, page, err = connect_browser(p)
        if err:
            raise RuntimeError(err)
        assert page is not None
        try:
            page.goto(feed_url, wait_until="domcontentloaded", timeout=60000)
            delay(3.0, 5.0)

            stop = detect_stop(page)
            if stop:
                status["stop_reason"] = stop
                shot = run_dir / "blocked_initial.png"
                page.screenshot(path=str(shot), full_page=True)
                status["blocked_screenshot"] = str(shot)
                save_json(STATUS_PATH, status)
                save_json(LATEST_PATH, status)
                return status

            feed_position = 0
            seen_urls: set[str] = set()
            seen_post_keys: set[str] = set()
            scroll_round = 0
            max_scroll_rounds = int(os.environ.get("LINKEDIN_LIKE_MAX_SCROLLS", "80"))

            while len(status["liked"]) < max_likes and scroll_round < max_scroll_rounds:
                scroll_round += 1
                posts = collect_feed_posts(page)
                emit("scan", scroll_round=scroll_round, visible_posts=len(posts), liked=len(status["liked"]))

                for post in posts:
                    post_key = str(post.get("key") or post.get("menuLabel") or post.get("text", "")[:120])
                    if post_key in seen_post_keys:
                        continue
                    seen_post_keys.add(post_key)
                    feed_position += 1

                    if is_promoted_or_sponsored(str(post.get("text", ""))):
                        status["skip_counts"]["promoted_or_sponsored"] = int(status["skip_counts"].get("promoted_or_sponsored", 0)) + 1
                        status["promoted_or_sponsored_skip_count"] = status["skip_counts"]["promoted_or_sponsored"]
                        status["skipped"].append({"feed_position": feed_position, "reason": "promoted_or_sponsored", "menuLabel": post.get("menuLabel"), "text_excerpt": post.get("text", "")[:160]})
                        continue

                    should_like = (feed_position - 1) % 3 == 0
                    if not should_like:
                        status["skipped"].append({"feed_position": feed_position, "reason": "pattern_skip", "menuLabel": post.get("menuLabel"), "text_excerpt": post.get("text", "")[:160]})
                        continue

                    url = recover_post_url(page, post)
                    if url and url in seen_urls:
                        continue
                    if url:
                        seen_urls.add(url)

                    if post.get("liked"):
                        status["skipped"].append({"feed_position": feed_position, "url": url, "reason": "already_liked"})
                        continue
                    if not post.get("noReaction"):
                        status["skipped"].append({"feed_position": feed_position, "url": url, "reason": f"unknown_reaction_state:{post.get('aria')}"})
                        continue

                    before = run_dir / f"like_{len(status['liked'])+1:02d}_before.png"
                    after = run_dir / f"like_{len(status['liked'])+1:02d}_after.png"
                    selected_reaction, reaction_reason = select_reaction(str(post.get("text", "")))
                    try:
                        page.screenshot(path=str(before), full_page=False)
                        reaction_result = click_reaction_for_post(page, post, selected_reaction)
                        confirmed = str(reaction_result.get("confirmed_aria_after_click") or "")
                        final_reaction = str(reaction_result.get("selected_reaction") or selected_reaction)
                        stop = detect_stop(page)
                        if stop:
                            status["stop_reason"] = stop
                            page.screenshot(path=str(after), full_page=True)
                            save_json(STATUS_PATH, status)
                            save_json(LATEST_PATH, status)
                            return status
                        page.screenshot(path=str(after), full_page=False)
                        if not reaction_confirmed(confirmed, final_reaction):
                            status["errors"].append({"feed_position": feed_position, "url": url, "stage": "click_not_confirmed", "selected_reaction": final_reaction, "reaction_reason": reaction_reason, "fallback_to_like": bool(reaction_result.get("fallback_to_like")), "fallback_reason": reaction_result.get("fallback_reason"), "aria_after": confirmed, "before_screenshot": str(before), "after_screenshot": str(after)})
                            save_json(STATUS_PATH, status)
                            continue
                        entry = {
                            "feed_position": feed_position,
                            "url": url or f"unrecovered://feed-position-{feed_position}",
                            "url_recovered": bool(url),
                            "at": now_iso(),
                            "selected_reaction": final_reaction,
                            "attempted_reaction": reaction_result.get("attempted_reaction", selected_reaction),
                            "reaction_reason": reaction_reason,
                            "fallback_to_like": bool(reaction_result.get("fallback_to_like")),
                            "fallback_reason": reaction_result.get("fallback_reason"),
                            "confirmed_on_feed": reaction_confirmed(confirmed, final_reaction),
                            "confirmed_aria_after_click": confirmed,
                            "before_screenshot": str(before),
                            "after_screenshot": str(after),
                            "text_excerpt": post.get("text", "")[:300],
                        }
                        status["liked"].append(entry)
                        emit("liked", count=len(status["liked"]), feed_position=feed_position, url=url, selected_reaction=final_reaction, reaction_reason=reaction_reason, fallback_to_like=entry["fallback_to_like"], confirmed_on_feed=confirmed)
                        save_json(STATUS_PATH, status)
                        if len(status["liked"]) >= max_likes:
                            break
                    except Exception as exc:
                        status["errors"].append({"feed_position": feed_position, "url": url, "error": repr(exc)})
                        save_json(STATUS_PATH, status)

                if len(status["liked"]) >= max_likes:
                    break
                page.mouse.wheel(0, random.randint(900, 1500))
                delay(1.7, 3.5)
                stop = detect_stop(page)
                if stop:
                    status["stop_reason"] = stop
                    shot = run_dir / f"blocked_scroll_{scroll_round}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    status["blocked_screenshot"] = str(shot)
                    break

            if verify and status["liked"]:
                for i, item in enumerate(status["liked"], start=1):
                    if not str(item.get("url", "")).startswith("http"):
                        result = {"url": item.get("url"), "verified": bool(item.get("confirmed_on_feed")), "expected_reaction": item.get("selected_reaction", "Like"), "reason": "feed_confirmed_url_not_recovered", "screenshot": item.get("after_screenshot")}
                    else:
                        result = verify_post(page, item["url"], i, run_dir, expected_reaction=str(item.get("selected_reaction") or "Like"))
                    status["verification"].append(result)
                    emit("verify", index=i, url=item["url"], verified=result.get("verified"), screenshot=result.get("screenshot"))
                    save_json(STATUS_PATH, status)
                    if result.get("reason", "").startswith("blocked:"):
                        status["stop_reason"] = result["reason"]
                        break
                    delay(1.2, 2.6)

            status["finished_at"] = now_iso()
            save_json(run_dir / "status.json", status)
            save_json(STATUS_PATH, status)
            save_json(LATEST_PATH, status)
            return status
        finally:
            close_postliker_page(page)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-likes", type=int, default=int(os.environ.get("LINKEDIN_LIKE_MAX", "15")))
    parser.add_argument("--feed-url", default=os.environ.get("LINKEDIN_FEED_URL", DEFAULT_FEED_URL))
    parser.add_argument("--headless", action="store_true", default=os.environ.get("LINKEDIN_LIKE_HEADLESS", "0") == "1")
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    try:
        status = run(args.max_likes, args.feed_url, args.headless, verify=not args.no_verify)
        print(json.dumps(status, ensure_ascii=False, indent=2), flush=True)
        if status.get("stop_reason"):
            return 12 if any(x in str(status["stop_reason"]).lower() for x in STOP_PATTERNS + ["url:", "blocked:"]) else 14
        return 0
    except Exception as exc:
        emit("fatal", error=repr(exc))
        return 14 if "session" not in str(exc).lower() else 2


if __name__ == "__main__":
    sys.exit(main())
