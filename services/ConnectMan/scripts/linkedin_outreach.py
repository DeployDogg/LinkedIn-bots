from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

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
FILTERS_PATH = Path(env_str("LINKEDIN_OUTREACH_FILTERS_PATH", str(BASE / "outreach_filters.json")))
FILTERS = json.loads(FILTERS_PATH.read_text()) if FILTERS_PATH.exists() else {}
ALLOWED_LOCATIONS = [
    str(loc).strip().lower()
    for loc in env_json("LINKEDIN_CONNECT_ALLOWED_LOCATIONS_JSON", FILTERS.get("allowed_locations", ["spain", "portugal", "united kingdom", "uk"]))
    if str(loc).strip()
]
LOG_PATH = Path(env_str("LINKEDIN_CONNECT_STATUS_PATH", str(BASE / "outreach_run_status.json")))
SCREENSHOT_DIR = Path(env_str("LINKEDIN_CONNECT_SCREENSHOT_DIR", "/Users/deploydog-ai/LinkedIn/BlockScreenshots"))
SEARCH_URL = env_str("LINKEDIN_OUTREACH_URL", "https://www.linkedin.com/search/results/people/?keywords=DevOps%20Engineer&network=%5B%22S%22%5D")
CONNECT_MODE = env_str("LINKEDIN_CONNECT_MODE", "standard").strip().lower()
DRY_RUN = env_str("LINKEDIN_CONNECT_DRY_RUN", "0") == "1"
MAX_CONNECTS = int(env_str("LINKEDIN_MAX_CONNECTS", "10"))
MAX_PAGES = int(env_str("LINKEDIN_MAX_PAGES", "8"))
SOFT_PROFILE_VIEW_MAX = int(env_str("LINKEDIN_SOFT_PROFILE_VIEW_MAX", "15"))
SOFT_CONNECT_MAX = int(env_str("LINKEDIN_SOFT_CONNECT_MAX", "5"))
CONNECT_DELAY_MIN = float(env_str("LINKEDIN_CONNECT_DELAY_MIN", "1.2"))
CONNECT_DELAY_MAX = float(env_str("LINKEDIN_CONNECT_DELAY_MAX", "3.2"))
STOP_PATTERNS = env_json("LINKEDIN_STOP_PATTERNS_JSON", [
    "captcha",
    "authwall",
    "sign in",
    "log in",
    "login",
    "security verification",
    "verify your identity",
    "unusual activity",
    "temporarily restricted",
    "account has been restricted",
    "weekly invitation limit",
    "invitation limit",
    "you’ve reached the limit",
    "you've reached the limit",
    "you have reached the limit",
    "daily limit",
    "weekly limit",
    "rate limit",
    "try again later",
    "safeguard",
])
DEFAULT_SOFT_RECRUITER_SEARCHES = [
    "Technical Recruiter AI ML",
    "Talent Acquisition Engineering Tech AI",
    "Engineering Manager AI ML",
    "Head of AI Head of Machine Learning",
    "Tech Recruiter IT Recruiter",
    "People Partner Head of People startup AI",
]
SOFT_RECRUITER_SEARCHES = env_json("LINKEDIN_SOFT_RECRUITER_SEARCHES_JSON", DEFAULT_SOFT_RECRUITER_SEARCHES)
INBOUND_PATTERNS = [
    "role", "opening", "opportunity", "hiring", "vacancy", "position",
    "are you looking", "looking for work", "interested in", "job"
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def delay(lo: float | None = None, hi: float | None = None) -> None:
    lo = CONNECT_DELAY_MIN if lo is None else lo
    hi = CONNECT_DELAY_MAX if hi is None else hi
    time.sleep(random.uniform(lo, hi))


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def detect_stop(page) -> str | None:
    txt = page_text(page).lower()
    for pattern in STOP_PATTERNS:
        if pattern in txt:
            return pattern
    url = page.url.lower()
    if any(x in url for x in ["checkpoint", "challenge", "captcha"]):
        return f"url:{page.url}"
    return None


def save_stop_screenshot(page, reason: str) -> str | None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-z0-9_-]+", "_", reason.lower()).strip("_") or "linkedin_stop"
    path = SCREENSHOT_DIR / f"connectman_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        print(f"[WARN] failed to save stop screenshot: {exc}", flush=True)
        return None


def mark_stop(status: dict[str, Any], page, reason: str) -> dict[str, Any]:
    status["stop_reason"] = reason
    screenshot = save_stop_screenshot(page, reason)
    if screenshot:
        status["stop_screenshot"] = screenshot
    save_status(status)
    return status


def people_search_url(keywords: str) -> str:
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(keywords)}&network=%5B%22S%22%5D"


def normalize_soft_searches(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, dict):
        raw = [{"label": str(k), "url": str(v)} for k, v in raw.items()]
    searches: list[dict[str, str]] = []
    for idx, item in enumerate(raw if isinstance(raw, list) else DEFAULT_SOFT_RECRUITER_SEARCHES):
        if isinstance(item, str):
            searches.append({"label": item, "keywords": item, "url": people_search_url(item)})
        elif isinstance(item, dict):
            label = str(item.get("label") or item.get("name") or item.get("keywords") or f"soft_search_{idx + 1}")
            keywords = str(item.get("keywords") or label)
            url = str(item.get("url") or people_search_url(keywords))
            searches.append({"label": label, "keywords": keywords, "url": url})
    return searches


def add_inbound_opportunities(status: dict[str, Any], text: str, source: str, url: str | None = None, name: str | None = None) -> None:
    low = (text or "").lower()
    matched = [pat for pat in INBOUND_PATTERNS if pat in low]
    if not matched:
        return
    status.setdefault("inbound_opportunities", []).append({
        "source": source,
        "name": name,
        "url": url,
        "matched_terms": matched[:5],
        "excerpt": re.sub(r"\s+", " ", text or "")[:500],
        "detected_at": now_iso(),
        "action": "detected_only_no_reply",
    })


def clean_name(raw: str) -> str:
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    for line in lines[:8]:
        if "connect" in line.lower() or "message" in line.lower() or "follow" in line.lower():
            continue
        line = re.sub(r"\s+•\s+.*$", "", line).strip()
        if 2 <= len(line) <= 80:
            return line
    return lines[0][:80] if lines else "unknown"


def contains_location_term(text: str, term: str) -> bool:
    """Match location/filter terms without substring false positives like oman in Roman."""
    low = re.sub(r"\s+", " ", (text or "").lower())
    term = re.sub(r"\s+", " ", (term or "").strip().lower())
    if not term:
        return False
    return re.search(rf"(^|[^a-z]){re.escape(term)}([^a-z]|$)", low) is not None


def location_match(card_text: str) -> str | None:
    """Return matched allowlisted location or None.

    Connect requests are allowed only when the visible candidate card explicitly
    contains Spain, Portugal, or United Kingdom/UK. Unknown/missing location is
    skipped safely.
    """
    low = re.sub(r"\s+", " ", (card_text or "").lower())
    for loc in ALLOWED_LOCATIONS:
        if loc == "uk":
            if re.search(r"(^|[^a-z])uk([^a-z]|$)", low):
                return "uk"
            continue
        if contains_location_term(low, loc):
            return loc
    return None


def blocked_by_filters(name: str, card_text: str) -> str | None:
    first = re.sub(r"[^a-z]", "", name.lower().split()[0]) if name.split() else ""
    if first and first in FILTERS.get("blocked_names", []):
        return f"blocked_name:{first}"
    low = card_text.lower()
    for loc in FILTERS.get("blocked_locations", []):
        if contains_location_term(low, loc):
            return f"blocked_location:{loc}"
    match = location_match(card_text)
    if not match:
        return "outside_allowed_locations:spain|portugal|united_kingdom"
    return None


def get_connect_candidates(page) -> list[dict[str, Any]]:
    """Collect visible people-search Connect controls and infer candidate names/card text."""
    return page.evaluate(
        r"""
        () => Array.from(document.querySelectorAll('a[aria-label*="connect"], a[aria-label*="Connect"], button'))
          .filter(el => el.offsetParent !== null)
          .filter(el => {
            const label = (el.getAttribute('aria-label') || '').toLowerCase();
            const text = (el.innerText || '').trim().toLowerCase();
            return label.includes('invite') && label.includes('connect') || text === 'connect';
          })
          .map((el, idx) => {
            let node = el;
            let cardText = '';
            for (let i = 0; i < 10 && node; i++) {
              const txt = (node.innerText || '').trim();
              if (txt.includes('Connect') && txt.length > 20 && txt.length < 1600) {
                cardText = txt;
                break;
              }
              node = node.parentElement;
            }
            const label = el.getAttribute('aria-label') || '';
            const inferredName = label.replace(/^Invite\s+/i, '').replace(/\s+to connect.*$/i, '').trim();
            const r = el.getBoundingClientRect();
            return {
              idx,
              text: cardText || inferredName || (el.innerText || '').trim(),
              name: inferredName,
              x: r.x + r.width / 2,
              y: r.y + r.height / 2
            };
          })
        """
    )


def get_people_candidates(page) -> list[dict[str, Any]]:
    """Collect visible people-search profile cards, profile URLs, and optional Connect coordinates."""
    return page.evaluate(
        """
        () => {
          const cards = [];
          const seen = new Set();
          const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]')).filter(a => a.offsetParent !== null);
          for (const a of anchors) {
            const href = a.href.split('?')[0];
            if (!href || seen.has(href) || href.includes('/search/')) continue;
            let node = a;
            let card = null;
            for (let i = 0; i < 12 && node; i++) {
              const txt = (node.innerText || '').trim();
              if (txt.length > 40 && txt.length < 2200 && /connect|message|follow|followers|degree|location/i.test(txt)) {
                card = node;
                break;
              }
              node = node.parentElement;
            }
            const text = ((card || a).innerText || '').trim();
            const name = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0] || '';
            let connect = null;
            if (card) {
              const buttons = Array.from(card.querySelectorAll('button,a')).filter(el => el.offsetParent !== null);
              const btn = buttons.find(el => {
                const label = (el.getAttribute('aria-label') || '').toLowerCase();
                const txt = (el.innerText || '').trim().toLowerCase();
                return (label.includes('invite') && label.includes('connect')) || txt === 'connect';
              });
              if (btn) {
                const r = btn.getBoundingClientRect();
                connect = {x: r.x + r.width / 2, y: r.y + r.height / 2};
              }
            }
            seen.add(href);
            cards.push({name, text, profile_url: href, connect});
          }
          return cards;
        }
        """
    )


def candidate_blocked_for_view(name: str, card_text: str) -> str | None:
    first = re.sub(r"[^a-z]", "", name.lower().split()[0]) if name.split() else ""
    if first and first in FILTERS.get("blocked_names", []):
        return f"blocked_name:{first}"
    low = card_text.lower()
    for loc in FILTERS.get("blocked_locations", []):
        if contains_location_term(low, loc):
            return f"blocked_location:{loc}"
    return None


def score_recruiter_candidate(card_text: str) -> dict[str, Any]:
    low = re.sub(r"\s+", " ", (card_text or "").lower())
    score = 0
    reasons: list[str] = []
    icp_group = "unknown"

    recruiter_terms = ["technical recruiter", "tech recruiter", "recruiter", "talent acquisition", "talent partner", "sourcer"]
    hiring_terms = ["hiring manager", "engineering manager", "head of ai", "head of machine learning", "head of ml", "cto", "vp engineering"]
    ai_terms = [" ai", "artificial intelligence", "machine learning", " ml", "llm", "genai", "data science", "engineering", "devops", "platform"]
    company_terms = ["startup", "product", "software", "technology", "tech", "saas", "cloud"]
    off_icp_terms = ["nurse", "nursing", "healthcare recruiter", "medical recruiter", "pharma recruiter", "sales recruiter", "retail", "hospitality"]

    if any(term in low for term in off_icp_terms):
        return {"score": -20, "icp_group": "off_icp", "reasons": ["off_icp_non_tech_recruiting"], "blocked": True}
    if any(term in low for term in recruiter_terms):
        score += 50
        icp_group = "recruiter_ta"
        reasons.append("recruiter_or_talent_acquisition")
    if any(term in low for term in hiring_terms):
        score += 40
        icp_group = "hiring_manager" if icp_group == "unknown" else icp_group
        reasons.append("hiring_manager_or_ai_leader")
    if any(term in low for term in ai_terms):
        score += 30
        reasons.append("ai_ml_engineering_keywords")
    if any(term in low for term in company_terms):
        score += 10
        reasons.append("tech_product_company_signal")
    if "2nd" in low or "mutual" in low:
        score += 5
        reasons.append("network_proximity_signal")
    if not reasons:
        reasons.append("weak_or_missing_recruiter_hiring_signal")
    return {"score": score, "icp_group": icp_group, "reasons": reasons, "blocked": False}


def extract_profile_snapshot(page) -> dict[str, str]:
    title = ""
    location = ""
    company = ""
    text = page_text(page)
    try:
        title = page.locator("main h1").first.inner_text(timeout=2500).strip()
    except Exception:
        pass
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in lines[:80]:
        low = line.lower()
        if not location and any(loc in low for loc in ["spain", "portugal", "united kingdom", " uk", "london", "madrid", "barcelona", "lisbon", "porto"]):
            location = line[:160]
        if not company and any(term in low for term in ["company", "recruit", "talent", "engineering", "ai", "machine learning"]):
            company = line[:160]
    return {"name_from_profile": title, "location_from_profile": location, "company_or_headline": company, "text_excerpt": re.sub(r"\s+", " ", text)[:800]}


def click_send_without_note(page) -> bool:
    """Send connection request without adding a note/message."""
    for selector in [
        "button[aria-label='Send now']",
        "button:has-text('Send now')",
        "button:has-text('Send')",
    ]:
        loc = page.locator(selector)
        if loc.count() and loc.first.is_visible(timeout=1000):
            disabled = loc.first.get_attribute("disabled", timeout=1000)
            aria_disabled = loc.first.get_attribute("aria-disabled", timeout=1000)
            if disabled is not None or aria_disabled == "true":
                return False
            loc.first.click(timeout=3000)
            return True
    return False


def click_next_page(page) -> bool:
    """Click LinkedIn people-search pagination Next button.

    LinkedIn currently exposes it as:
    button[data-testid='pagination-controls-next-button-visible']
    and may not have a stable aria-label.
    """
    page.mouse.wheel(0, 2200)
    delay(0.8, 1.4)
    selectors = [
        "button[data-testid='pagination-controls-next-button-visible']",
        "button:has-text('Next')",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        if loc.count() and loc.first.is_visible(timeout=1500):
            disabled = loc.first.get_attribute("disabled")
            aria_disabled = loc.first.get_attribute("aria-disabled")
            if disabled is not None or aria_disabled == "true":
                return False
            before_url = page.url
            loc.first.click()
            delay(4, 6)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8000)
            except Exception:
                pass
            # URL may or may not change immediately; visible page content refresh is enough.
            print(f"[ACTION] Next page clicked ({selector}); before_url={before_url}; after_url={page.url}", flush=True)
            return True
    return False


def save_status(status: dict[str, Any]) -> None:
    LOG_PATH.write_text(json.dumps(status, ensure_ascii=False, indent=2))


DEFAULT_CDP_ENDPOINT = "http://linkedin-browser:9222"


def cdp_endpoint() -> str:
    value = os.environ.get("LINKEDIN_CDP_ENDPOINT")
    return str(value).strip() if value else DEFAULT_CDP_ENDPOINT


def connect_browser(p):
    browser = p.chromium.connect_over_cdp(cdp_endpoint())
    contexts = list(getattr(browser, "contexts", []) or [])
    if len(contexts) != 1:
        return browser, None, None, f"expected_exactly_one_persistent_context:{len(contexts)}"
    context = contexts[0]
    page = context.new_page()
    return browser, context, page, None


def close_connectman_page(page) -> None:
    if page is None:
        return
    try:
        page.close()
    except Exception:
        pass


def run_standard_outreach() -> dict[str, Any]:
    status: dict[str, Any] = {
        "started_at": now_iso(),
        "mode": "standard",
        "dry_run": DRY_RUN,
        "search_url": SEARCH_URL,
        "max_connects": MAX_CONNECTS,
        "max_pages": MAX_PAGES,
        "allowed_locations": ALLOWED_LOCATIONS,
        "sent": [],
        "profiles_viewed": [],
        "soft_connects_sent": [],
        "inbound_opportunities": [],
        "skipped": [],
        "errors": [],
        "pages_visited": 0,
        "stop_reason": None,
    }
    save_status(status)

    with sync_playwright() as p:
        browser, context, page, err = connect_browser(p)
        if err:
            status["errors"].append({"error": err})
            status["stop_reason"] = err
            save_status(status)
            return status
        try:
            page.goto(SEARCH_URL, wait_until="domcontentloaded")
            delay(3, 5)

            seen_names: set[str] = set()
            while len(status["sent"]) < MAX_CONNECTS and status["pages_visited"] < MAX_PAGES:
                status["pages_visited"] += 1
                stop = detect_stop(page)
                if stop:
                    mark_stop(status, page, stop)
                    break

                candidates = get_connect_candidates(page)
                status["visible_connect_buttons"] = len(candidates)
                print(
                    f"[PAGE] {status['pages_visited']}/{MAX_PAGES}: {len(candidates)} connect candidates; sent={len(status['sent'])}/{MAX_CONNECTS}",
                    flush=True,
                )
                save_status(status)

                for candidate in candidates:
                    if len(status["sent"]) >= MAX_CONNECTS:
                        break

                    card_text = candidate.get("text") or ""
                    name = candidate.get("name") or clean_name(card_text)
                    if not name or name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())

                    matched_location = location_match(card_text)
                    reason = blocked_by_filters(name, card_text)
                    if reason:
                        status["skipped"].append({"name": name, "reason": reason, "page": status["pages_visited"]})
                        save_status(status)
                        continue

                    if DRY_RUN:
                        status["skipped"].append({"name": name, "reason": "dry_run_no_connect_sent", "page": status["pages_visited"], "matched_location": matched_location})
                        save_status(status)
                        continue

                    try:
                        page.mouse.click(float(candidate["x"]), float(candidate["y"]))
                        delay(1.0, 2.0)

                        stop = detect_stop(page)
                        if stop:
                            mark_stop(status, page, stop)
                            print(json.dumps(status, ensure_ascii=False, indent=2))
                            return status

                        if click_send_without_note(page):
                            delay(1.8, 3.6)
                            stop = detect_stop(page)
                            if stop:
                                mark_stop(status, page, stop)
                                print(json.dumps(status, ensure_ascii=False, indent=2))
                                return status
                            status["sent"].append({
                                "name": name,
                                "matched_location": matched_location,
                                "at": now_iso(),
                                "page": status["pages_visited"],
                            })
                            print(f"[SENT] {name} [{matched_location}] ({len(status['sent'])}/{MAX_CONNECTS})", flush=True)
                            save_status(status)
                        else:
                            status["skipped"].append({"name": name, "reason": "send_button_not_found", "page": status["pages_visited"]})
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            save_status(status)
                    except Exception as exc:
                        status["errors"].append({"name": name, "error": str(exc)[:300], "page": status["pages_visited"]})
                        try:
                            page.keyboard.press("Escape")
                        except Exception:
                            pass
                        save_status(status)
                    delay(2.5, 5.5)

                if len(status["sent"]) >= MAX_CONNECTS:
                    break

                stop = detect_stop(page)
                if stop:
                    mark_stop(status, page, stop)
                    break
                if not click_next_page(page):
                    status["stop_reason"] = "no_next_page"
                    print("[DONE] No visible/enabled Next button.", flush=True)
                    break

            status["finished_at"] = now_iso()
            save_status(status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return status
        finally:
            close_connectman_page(page)


def run_soft_recruiter() -> dict[str, Any]:
    searches = normalize_soft_searches(SOFT_RECRUITER_SEARCHES)
    max_connects = min(SOFT_CONNECT_MAX, 5)
    max_views = min(SOFT_PROFILE_VIEW_MAX, 15)
    status: dict[str, Any] = {
        "started_at": now_iso(),
        "mode": "soft_recruiter",
        "dry_run": DRY_RUN,
        "dry_run_policy": "no-action: does not send connects; does not open profiles; only parses search-result candidates" if DRY_RUN else "profile views allowed; connects are no-note only",
        "searches": searches,
        "max_pages": MAX_PAGES,
        "profile_view_max": max_views,
        "soft_connect_max": max_connects,
        "allowed_locations": ALLOWED_LOCATIONS,
        "sent": [],
        "profiles_viewed": [],
        "soft_connects_sent": [],
        "candidate_pool": [],
        "inbound_opportunities": [],
        "skipped": [],
        "errors": [],
        "pages_visited": 0,
        "stop_reason": None,
    }
    save_status(status)

    with sync_playwright() as p:
        browser, context, page, err = connect_browser(p)
        if err:
            status["errors"].append({"error": err})
            status["stop_reason"] = err
            save_status(status)
            return status
        try:

            seen_profiles: set[str] = set()
            connected_profiles: set[str] = set()
            dry_profile_candidates = 0
            for search in searches:
                if status["pages_visited"] >= MAX_PAGES or len(status["profiles_viewed"]) >= max_views or (DRY_RUN and dry_profile_candidates >= max_views):
                    break
                page.goto(search["url"], wait_until="domcontentloaded")
                delay(3, 5)
                search_pages = 0
                while status["pages_visited"] < MAX_PAGES and len(status["profiles_viewed"]) < max_views and (not DRY_RUN or dry_profile_candidates < max_views):
                    search_pages += 1
                    status["pages_visited"] += 1
                    stop = detect_stop(page)
                    if stop:
                        mark_stop(status, page, stop)
                        print(json.dumps(status, ensure_ascii=False, indent=2))
                        return status

                    candidates = get_people_candidates(page)
                    print(
                        f"[SOFT] {search['label']} page={search_pages} global_page={status['pages_visited']}/{MAX_PAGES}: candidates={len(candidates)} viewed={len(status['profiles_viewed'])}/{max_views} connects={len(status['soft_connects_sent'])}/{max_connects}",
                        flush=True,
                    )

                    ranked: list[dict[str, Any]] = []
                    for candidate in candidates:
                        profile_url = candidate.get("profile_url") or ""
                        card_text = candidate.get("text") or ""
                        name = candidate.get("name") or clean_name(card_text)
                        if not profile_url or profile_url in seen_profiles:
                            continue
                        view_block = candidate_blocked_for_view(name, card_text)
                        score = score_recruiter_candidate(card_text)
                        matched_location = location_match(card_text)
                        record = {
                            "name": name,
                            "profile_url": profile_url,
                            "search_label": search["label"],
                            "page": status["pages_visited"],
                            "score": score["score"],
                            "icp_group": score["icp_group"],
                            "reason": "; ".join(score["reasons"]),
                            "matched_location": matched_location,
                            "location_allowed_for_connect": bool(matched_location),
                            "connect_available": bool(candidate.get("connect")),
                        }
                        status["candidate_pool"].append(record)
                        add_inbound_opportunities(status, card_text, "search_card", profile_url, name)
                        if view_block:
                            status["skipped"].append({**record, "reason": view_block, "stage": "profile_view"})
                            continue
                        if score.get("blocked"):
                            status["skipped"].append({**record, "reason": "; ".join(score["reasons"]), "stage": "profile_view"})
                            continue
                        ranked.append({**candidate, **record, "score_detail": score})

                    ranked.sort(key=lambda c: c.get("score", 0), reverse=True)
                    save_status(status)

                    for candidate in ranked:
                        if len(status["profiles_viewed"]) >= max_views or (DRY_RUN and dry_profile_candidates >= max_views):
                            break
                        profile_url = candidate["profile_url"]
                        if profile_url in seen_profiles:
                            continue
                        seen_profiles.add(profile_url)

                        view_record = {
                            "name": candidate.get("name"),
                            "profile_url": profile_url,
                            "search_label": candidate.get("search_label"),
                            "page": candidate.get("page"),
                            "score": candidate.get("score"),
                            "icp_group": candidate.get("icp_group"),
                            "reason": candidate.get("reason"),
                            "matched_location": candidate.get("matched_location"),
                            "location_allowed_for_connect": candidate.get("location_allowed_for_connect"),
                            "viewed_at": None,
                        }

                        if DRY_RUN:
                            dry_profile_candidates += 1
                            status["skipped"].append({**view_record, "reason": "dry_run_no_profile_open_no_connect", "stage": "profile_view"})
                            save_status(status)
                            continue

                        search_page_url = page.url
                        profile_page = page
                        profile_stop = False
                        try:
                            profile_page.goto(profile_url, wait_until="domcontentloaded")
                            delay(2.5, 5.0)
                            stop = detect_stop(profile_page)
                            if stop:
                                profile_stop = True
                                mark_stop(status, profile_page, stop)
                                print(json.dumps(status, ensure_ascii=False, indent=2))
                                return status
                            snapshot = extract_profile_snapshot(profile_page)
                            add_inbound_opportunities(status, snapshot.get("text_excerpt", ""), "profile_view", profile_url, candidate.get("name"))
                            profile_location_match = location_match(" ".join(str(v) for v in snapshot.values()))
                            if profile_location_match and not view_record.get("matched_location"):
                                view_record["matched_location"] = profile_location_match
                                view_record["location_allowed_for_connect"] = True
                                candidate["matched_location"] = profile_location_match
                            view_record.update(snapshot)
                            view_record["viewed_at"] = now_iso()
                            status["profiles_viewed"].append(view_record)
                            print(f"[VIEWED] {candidate.get('name')} score={candidate.get('score')} url={profile_url}", flush=True)
                            save_status(status)
                        except Exception as exc:
                            status["errors"].append({"name": candidate.get("name"), "profile_url": profile_url, "error": str(exc)[:300], "stage": "profile_view"})
                            save_status(status)
                        finally:
                            if not profile_stop and page.url != search_page_url:
                                try:
                                    page.goto(search_page_url, wait_until="domcontentloaded")
                                    delay(1.0, 2.0)
                                except Exception as exc:
                                    status["errors"].append({"name": candidate.get("name"), "profile_url": profile_url, "error": str(exc)[:300], "stage": "return_to_search"})
                                    save_status(status)

                        if len(status["soft_connects_sent"]) >= max_connects or max_connects <= 0:
                            continue
                        if profile_url in connected_profiles:
                            continue
                        if not candidate.get("matched_location"):
                            status["skipped"].append({**view_record, "reason": "missing_or_unknown_location_skip_connect", "stage": "connect"})
                            save_status(status)
                            continue
                        connect_block = blocked_by_filters(candidate.get("name") or "", candidate.get("text") or "")
                        if connect_block:
                            status["skipped"].append({**view_record, "reason": connect_block, "stage": "connect"})
                            save_status(status)
                            continue
                        if candidate.get("score", 0) < 50:
                            status["skipped"].append({**view_record, "reason": "score_below_soft_connect_threshold", "stage": "connect"})
                            save_status(status)
                            continue
                        if not candidate.get("connect"):
                            status["skipped"].append({**view_record, "reason": "connect_button_not_available_on_search_card", "stage": "connect"})
                            save_status(status)
                            continue

                        try:
                            page.mouse.click(float(candidate["connect"]["x"]), float(candidate["connect"]["y"]))
                            delay(1.0, 2.0)
                            stop = detect_stop(page)
                            if stop:
                                mark_stop(status, page, stop)
                                print(json.dumps(status, ensure_ascii=False, indent=2))
                                return status
                            if click_send_without_note(page):
                                delay(1.8, 3.6)
                                stop = detect_stop(page)
                                if stop:
                                    mark_stop(status, page, stop)
                                    print(json.dumps(status, ensure_ascii=False, indent=2))
                                    return status
                                record = {
                                    "name": candidate.get("name"),
                                    "profile_url": profile_url,
                                    "matched_location": candidate.get("matched_location"),
                                    "score": candidate.get("score"),
                                    "reason": candidate.get("reason"),
                                    "status": "sent_no_note",
                                    "at": now_iso(),
                                }
                                status["soft_connects_sent"].append(record)
                                status["sent"].append(record)
                                connected_profiles.add(profile_url)
                                print(f"[SOFT_CONNECT_SENT] {candidate.get('name')} [{candidate.get('matched_location')}] ({len(status['soft_connects_sent'])}/{max_connects})", flush=True)
                                save_status(status)
                            else:
                                status["skipped"].append({**view_record, "reason": "send_button_not_found", "stage": "connect"})
                                try:
                                    page.keyboard.press("Escape")
                                except Exception:
                                    pass
                                save_status(status)
                        except Exception as exc:
                            status["errors"].append({"name": candidate.get("name"), "profile_url": profile_url, "error": str(exc)[:300], "stage": "connect"})
                            try:
                                page.keyboard.press("Escape")
                            except Exception:
                                pass
                            save_status(status)
                        delay(2.5, 5.5)

                    if len(status["profiles_viewed"]) >= max_views or (DRY_RUN and dry_profile_candidates >= max_views) or status["pages_visited"] >= MAX_PAGES:
                        break
                    stop = detect_stop(page)
                    if stop:
                        mark_stop(status, page, stop)
                        print(json.dumps(status, ensure_ascii=False, indent=2))
                        return status
                    if not click_next_page(page):
                        print(f"[SOFT] No next page for search {search['label']}", flush=True)
                        break

            status["finished_at"] = now_iso()
            if not status["stop_reason"]:
                status["stop_reason"] = "completed"
            save_status(status)
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return status
        finally:
            close_connectman_page(page)


def run_outreach() -> dict[str, Any]:
    if CONNECT_MODE not in {"standard", "soft_recruiter"}:
        status = {
            "started_at": now_iso(),
            "mode": CONNECT_MODE,
            "sent": [],
            "profiles_viewed": [],
            "soft_connects_sent": [],
            "skipped": [],
            "errors": [{"error": "invalid LINKEDIN_CONNECT_MODE", "allowed": ["standard", "soft_recruiter"]}],
            "inbound_opportunities": [],
            "stop_reason": "invalid_mode",
            "finished_at": now_iso(),
        }
        save_status(status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return status
    if CONNECT_MODE == "soft_recruiter":
        return run_soft_recruiter()
    return run_standard_outreach()


if __name__ == "__main__":
    result = run_outreach()
    if result.get("stop_reason") in {"missing_session", "invalid_mode"}:
        sys.exit(11)
    if result.get("stop_reason") not in (None, "completed", "no_next_page"):
        sys.exit(12)


