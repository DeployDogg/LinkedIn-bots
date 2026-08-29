#!/usr/bin/env python3
"""LinkedIn daily message outreach for DevOps/SRE/Platform hiring people.

Contract:
  - Uses an existing authenticated Chromium/Chrome CDP session at http://localhost:9222.
  - Scans LinkedIn Messaging first and stores full dialog names as a stop-list.
  - Visits configured people-search URLs, opens profiles, sends at most one message per person.
  - Stops immediately on captcha/security/rate-limit/safeguard/daily-limit signals.
  - Human delay before LinkedIn actions: 10 + random(0, 3) seconds by default.

Exit codes:
  0  finished normally
  11 browser/session/login problem
  12 LinkedIn block/security/rate-limit detected
  14 unexpected automation error
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

ROOT = Path("/Users/deploydog-ai/LinkedIn")
STATE_DIR = ROOT / "data" / "state" / "linkedin_company_jobs"
SCREENSHOT_DIR = ROOT / "BlockScreenshots"
STATUS_PATH = STATE_DIR / "linkedin_message_outreach_status.json"
STATE_PATH = STATE_DIR / "linkedin_message_outreach_state.json"
DIALOG_STOPLIST_PATH = STATE_DIR / "linkedin_dialog_names_stoplist.json"
LOG_DIR = STATE_DIR / "logs"
TIMEZONE = ZoneInfo("America/Argentina/Buenos_Aires")
CDP_ENDPOINT = "http://localhost:9222"
SESSION_PATH = Path("/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_session.json")
USER_DATA_DIR = Path("/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_chromium_profile")

SEARCHES = {
    "DevOps": "https://www.linkedin.com/search/results/people/?origin=FACETED_SEARCH&network=%5B%22O%22%5D&activelyHiringForJobTitles=%5B%2225764%22%2C%2230152%22%5D",
    "SRE": "https://www.linkedin.com/search/results/people/?origin=FACETED_SEARCH&network=%5B%22O%22%5D&activelyHiringForJobTitles=%5B%2222848%22%2C%2226262%22%5D",
    "Platform": "https://www.linkedin.com/search/results/people/?origin=FACETED_SEARCH&network=%5B%22O%22%5D&activelyHiringForJobTitles=%5B%226483%22%5D",
}

JOB_LABELS = {
    "DevOps": "DevOps",
    "SRE": "Site Reliability",
    "Platform": "Platform",
}

BASE_BLOCKED_FIRST_NAMES = {
    "amir", "arman", "darius", "farhad", "kian", "navid", "omid", "reza", "shahin", "yasmin", "leila",
    "neda", "shirin", "parisa", "roya", "ari", "david", "eitan", "eli", "isaac", "jacob", "jonathan",
    "noah", "samuel", "sarah", "miriam", "rachel", "leah", "esther", "tamar", "aarav", "arjun", "dev",
    "ishaan", "kabir", "krishna", "rahul", "rohan", "vikram", "ananya", "diya", "isha", "kavya", "meera",
    "priya", "saanvi", "adeel", "ahmed", "ali", "bilal", "danish", "fahad", "hamza", "hassan", "imran",
    "saad", "zain", "ayesha", "hira", "mahira", "sana", "zara", "abdullah", "adnan", "faisal", "khalid",
    "mohammed", "mustafa", "omar", "rashid", "sami", "tariq", "youssef", "amal", "fatima", "hana", "layla",
    "mariam", "nour", "rania", "salma", "yasmina",
}

# Expanded transliteration/spelling variants requested by Андрей.
BLOCKED_FIRST_NAMES = BASE_BLOCKED_FIRST_NAMES | {
    "mohammad", "muhammad", "muhammed", "mohamed", "mohamad", "mohd", "mohammed",
    "mohammmad", "mohammadreza", "mohammad-reza",
    "sara", "sarah", "laila", "layla", "leila", "leyla", "laela",
    "yousef", "yusuf", "yousuf", "youssef", "yasin", "yasine", "yasmeen", "yasmin", "yasmina",
    "ahmad", "ahmed", "husein", "hussein", "hossein", "hasan", "hassan",
    "fatimah", "fatemeh", "maryam", "mariam", "nora", "noor", "nour",
    "samir", "sameer", "samy", "sammy", "tarek", "tareq", "shahram", "shahrin",
}

BLOCKED_COUNTRIES_RE = re.compile(
    r"\b("
    r"india|pakistan|pakistani|indian|"
    r"karachi|lahore|islamabad|rawalpindi|faisalabad|multan|peshawar|quetta|hyderabad|gujranwala|sialkot|bahawalpur|"
    r"mumbai|delhi|bengaluru|bangalore|chennai|kolkata|ahmedabad|pune|jaipur|surat|lucknow|kanpur|nagpur|indore|bhopal|chandigarh|kochi|goa|agra|varanasi"
    r")\b",
    re.I,
)
UNKNOWN_LOCATION_RE = re.compile(r"\b(location|followers|connections|contact info|message|connect|follow)\b", re.I)
STOP_PATTERNS = [
    "captcha",
    "security verification",
    "verify your identity",
    "unusual activity",
    "temporarily restricted",
    "account has been restricted",
    "you’ve reached the limit",
    "you've reached the limit",
    "daily limit",
    "weekly limit",
    "try again later",
    "safeguard",
]

PROFILE_URL_RE = re.compile(r"https://www\.linkedin\.com/in/[^/?#\s]+")


@dataclass
class Candidate:
    name: str
    profile_url: str
    card_text: str = ""


def now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def next_9am_iso() -> str:
    now = datetime.now(TIMEZONE)
    nxt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return nxt.isoformat(timespec="seconds")


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def normalize_spaces(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_name_key(name: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s'-]", " ", text, flags=re.UNICODE)
    return normalize_spaces(text).casefold()


def first_name_key(full_name: str) -> str:
    key = normalize_name_key(full_name)
    first = key.split()[0] if key.split() else ""
    return re.sub(r"[^a-z-]", "", first).strip("-")


def is_asciiish_name(full_name: str) -> bool:
    if any(unicodedata.category(ch).startswith("S") for ch in str(full_name or "")):
        return False
    key = normalize_name_key(full_name)
    if not key or len(key.split()) < 1:
        return False
    tokens = [re.sub(r"[^a-zA-Z]", "", part) for part in key.split()]
    if any(len(token) <= 1 for token in tokens if token is not None):
        return False
    letters = re.sub(r"[^a-zA-Z]", "", key)
    return len(letters) >= 2 and bool(re.fullmatch(r"[a-zA-Z\s'\-\.]+", key))


def is_blocked_first_name(first: str) -> bool:
    if not first:
        return True
    if first in BLOCKED_FIRST_NAMES:
        return True
    # Catch compound/transliterated names like AmirAbbas or Mohammadreza without
    # blocking very short entries such as Dev inside unrelated names.
    return any(len(blocked) >= 4 and first.startswith(blocked) for blocked in BLOCKED_FIRST_NAMES)


def clean_profile_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    m = PROFILE_URL_RE.search(url)
    if m:
        return m.group(0).rstrip("/") + "/"
    parts = urlsplit(url)
    return urlunsplit((parts.scheme or "https", parts.netloc or "www.linkedin.com", parts.path.rstrip("/") + "/", "", ""))


def weekly_log_path(now: datetime | None = None) -> Path:
    dt = now or datetime.now(TIMEZONE)
    iso_year, iso_week, _ = dt.isocalendar()
    return LOG_DIR / f"linkedin_message_outreach_actions_{iso_year}-W{iso_week:02d}.jsonl"


def append_action(event: str, **payload: Any) -> None:
    ensure_dirs()
    rec = {"at": now_iso(), "event": event, **payload}
    with weekly_log_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def save_json(path: Path, data: Any) -> None:
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def default_state() -> dict[str, Any]:
    return {
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "sent_profiles": {},
        "sent_full_names": {},
        "skipped_profiles": {},
    }


def load_state() -> dict[str, Any]:
    state = load_json(STATE_PATH, default_state())
    state.setdefault("sent_profiles", {})
    state.setdefault("sent_full_names", {})
    state.setdefault("skipped_profiles", {})
    return state


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    save_json(STATE_PATH, state)


def load_dialog_names() -> set[str]:
    data = load_json(DIALOG_STOPLIST_PATH, {"names": []})
    return {normalize_name_key(x) for x in data.get("names", []) if normalize_name_key(x)}


def save_dialog_names(names: set[str], raw_names: list[str] | None = None) -> None:
    existing_raw = load_json(DIALOG_STOPLIST_PATH, {"names": []}).get("names", [])
    by_key = {normalize_name_key(x): x for x in existing_raw if normalize_name_key(x)}
    if raw_names:
        for name in raw_names:
            key = normalize_name_key(name)
            if key:
                by_key[key] = normalize_spaces(name)
    for key in names:
        by_key.setdefault(key, key)
    save_json(DIALOG_STOPLIST_PATH, {"updated_at": now_iso(), "names": sorted(by_key.values(), key=str.casefold)})


def write_dialog_names_exact(raw_names: list[str]) -> None:
    by_key = {}
    for name in raw_names:
        key = normalize_name_key(name)
        if key:
            by_key[key] = normalize_spaces(name)
    save_json(DIALOG_STOPLIST_PATH, {"updated_at": now_iso(), "names": sorted(by_key.values(), key=str.casefold)})


def human_delay(args: argparse.Namespace, label: str = "linkedin_action") -> None:
    if args.no_delay:
        return
    seconds = args.delay_base + random.uniform(0, args.delay_jitter)
    print(f"[DELAY] {label}: {seconds:.1f}s", flush=True)
    time.sleep(seconds)


def page_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def save_block_screenshot(page, reason: str) -> str:
    ensure_dirs()
    safe = re.sub(r"[^a-z0-9_-]+", "_", reason.lower()).strip("_") or "linkedin_block"
    path = SCREENSHOT_DIR / f"{datetime.now(TIMEZONE).strftime('%Y%m%d_%H%M%S')}_{safe}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        return f"screenshot_failed:{exc!r}"


def detect_stop(page) -> str | None:
    url = (getattr(page, "url", "") or "").lower()
    if any(x in url for x in ["checkpoint", "challenge", "captcha"]):
        return f"url:{page.url}"
    txt = page_text(page).lower()
    for pattern in STOP_PATTERNS:
        if pattern in txt:
            return pattern
    return None


def assert_no_stop(page, status: dict[str, Any]) -> None:
    stop = detect_stop(page)
    if stop:
        screenshot = save_block_screenshot(page, stop)
        status["stop_reason"] = stop
        status["block_screenshot"] = screenshot
        save_json(STATUS_PATH, status)
        append_action("blocked", reason=stop, screenshot=screenshot, url=getattr(page, "url", ""))
        raise SystemExit(12)


def open_linkedin_context(playwright, status: dict[str, Any]):
    """Return (browser_or_context, context, page, mode).

    Prefer the already-running authenticated Chrome over CDP. If that local CDP
    endpoint is wedged, fall back to a fresh Chromium context using the existing
    Easy Apply storage_state. This does not delete or reset any Chrome profile.
    """
    try:
        browser = playwright.chromium.connect_over_cdp(CDP_ENDPOINT, timeout=45000)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        append_action("browser_connected", mode="cdp", endpoint=CDP_ENDPOINT)
        return browser, context, page, "cdp"
    except Exception as exc:
        append_action("browser_connect_failed", mode="cdp", endpoint=CDP_ENDPOINT, error=repr(exc)[:600])
        if not SESSION_PATH.exists():
            status["stop_reason"] = f"cdp_connect_failed_and_no_session:{exc!r}"
            save_json(STATUS_PATH, jsonable_status(status))
            raise SystemExit(11)
        try:
            browser = playwright.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(storage_state=str(SESSION_PATH), viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            append_action("browser_connected", mode="storage_state", session_path=str(SESSION_PATH))
            return browser, context, page, "storage_state"
        except Exception as storage_exc:
            append_action("browser_connect_failed", mode="storage_state", session_path=str(SESSION_PATH), error=repr(storage_exc)[:600])
            context = playwright.chromium.launch_persistent_context(
                str(USER_DATA_DIR),
                headless=False,
                viewport={"width": 1440, "height": 1000},
                accept_downloads=True,
            )
            page = context.pages[0] if context.pages else context.new_page()
            append_action("browser_connected", mode="persistent_context", user_data_dir=str(USER_DATA_DIR))
            return context, context, page, "persistent_context"


def is_authenticated(page) -> bool:
    url = (page.url or "").lower()
    text = page_text(page).lower()
    if "/login" in url or "sign in" in text[:3000] or "join linkedin" in text[:3000]:
        return False
    return "linkedin" in url or "linkedin" in text[:5000]


def extract_dialog_names_from_dom(page) -> list[str]:
    names = page.evaluate(
        """
        () => {
          const selectors = [
            '.msg-conversation-listitem',
            'li.msg-conversations-container__convo-item',
            'a[href*="/messaging/thread"]'
          ];
          const bad = /^(you|linkedin|sponsored|message|messages|compose|search|inmail|learn more|draft|ad choices|business services|get the linkedin app|help center|load more conversations|my network|privacy & terms|status is online|status is reachable|view .* profile|new .* notifications?)$/i;
          const out = [];
          const seen = new Set();
          for (const sel of selectors) {
            for (const el of Array.from(document.querySelectorAll(sel))) {
              const rect = el.getBoundingClientRect();
              if (rect.width <= 0 || rect.height <= 0) continue;
              const txt = (el.innerText || '').replace(String.fromCharCode(13), '').split(String.fromCharCode(10)).map(s => s.trim()).filter(Boolean);
              for (const line of txt.slice(0, 4)) {
                const clean = line.replace(/\s+•\s+.*$/, '').replace(/^\d+\s+/, '').trim();
                if (!clean || clean.length < 3 || clean.length > 90) continue;
                if (bad.test(clean)) continue;
                if (/\b(choices|services|notifications|privacy|terms|linkedin app|help center|load more|status is|my network)\b/i.test(clean)) continue;
                if (/^(mon|tue|wed|thu|fri|sat|sun|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b/i.test(clean)) continue;
                if (!/[A-Za-z]/.test(clean)) continue;
                const words = clean.split(/\s+/).filter(Boolean);
                if (words.length < 2 || words.length > 5) continue;
                const key = clean.toLowerCase();
                if (!seen.has(key)) { seen.add(key); out.push(clean); }
                break;
              }
            }
          }
          return out;
        }
        """
    )
    cleaned = []
    seen = set()
    for raw in names or []:
        name = normalize_spaces(raw)
        key = normalize_name_key(name)
        if key and key not in seen and is_asciiish_name(name):
            seen.add(key)
            cleaned.append(name)
    return cleaned


def scroll_best_messaging_container(page) -> bool:
    return bool(page.evaluate(
        """
        () => {
          const candidates = Array.from(document.querySelectorAll('*'))
            .filter(el => el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 160)
            .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
          const el = candidates.find(e => /msg|conversation|thread|scaffold/i.test(e.className || '')) || candidates[0];
          if (!el) { window.scrollBy(0, 1200); return true; }
          const before = el.scrollTop;
          el.scrollTop = Math.min(el.scrollTop + Math.max(500, el.clientHeight * 0.85), el.scrollHeight);
          return el.scrollTop !== before;
        }
        """
    ))


def scan_dialog_stoplist(page, args: argparse.Namespace, status: dict[str, Any]) -> set[str]:
    if args.skip_dialog_scan:
        names = load_dialog_names()
        status["dialog_stoplist_count"] = len(names)
        return names

    print("[1/4] Scanning LinkedIn Messaging dialog names...", flush=True)
    human_delay(args, "open_messaging")
    page.goto("https://www.linkedin.com/messaging/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    assert_no_stop(page, status)
    if not is_authenticated(page):
        status["stop_reason"] = "not_authenticated"
        save_json(STATUS_PATH, status)
        raise SystemExit(11)

    raw_names: list[str] = []
    seen_keys: set[str] = set()
    stagnant = 0
    max_scrolls = max(1, args.dialog_scrolls)
    for idx in range(max_scrolls):
        batch = extract_dialog_names_from_dom(page)
        added = 0
        for name in batch:
            key = normalize_name_key(name)
            if key and key not in seen_keys:
                seen_keys.add(key)
                raw_names.append(name)
                added += 1
        print(f"[DIALOGS] scroll={idx + 1}/{max_scrolls} names={len(seen_keys)} added={added}", flush=True)
        write_dialog_names_exact(raw_names)
        if added == 0:
            stagnant += 1
        else:
            stagnant = 0
        if stagnant >= 4:
            break
        human_delay(args, "scroll_messaging")
        moved = scroll_best_messaging_container(page)
        if not moved:
            stagnant += 1
        page.wait_for_timeout(1200)
        assert_no_stop(page, status)

    write_dialog_names_exact(raw_names)
    status["dialog_stoplist_count"] = len(seen_keys)
    append_action("dialog_scan", names=len(seen_keys), raw_names=raw_names[-20:])
    return seen_keys


def collect_search_candidates(page) -> list[Candidate]:
    rows = page.evaluate(
        """
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'))
            .filter(a => a.offsetParent !== null);
          const out = [];
          const seenCards = new Set();
          for (const a of anchors) {
            let node = a;
            let card = null;
            for (let i = 0; i < 10 && node; i++, node = node.parentElement) {
              const txt = norm(node.innerText || node.textContent || '');
              if (txt.length > 40 && txt.length < 2500 && /(?:2nd|3rd\+)/.test(txt) && /(?:Connect|Message|Follow|Pending)/i.test(txt)) {
                card = node;
                break;
              }
            }
            if (!card) continue;
            const cardText = norm(card.innerText || card.textContent || '');
            if (!/3rd\+/.test(cardText)) continue;
            const direct = norm(a.innerText || a.getAttribute('aria-label') || '');
            if (!/3rd\+/.test(direct)) continue;
            // Later /in/ links are mutual connections; their own direct text does
            // not contain the result relation marker (for example "3rd+").
            const key = a.href.split('?')[0];
            if (seenCards.has(key)) continue;
            seenCards.add(key);
            out.push({href: a.href, text: direct, cardText});
          }
          return out;
        }
        """
    )
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in rows or []:
        url = clean_profile_url(row.get("href", ""))
        if not url or url in seen:
            continue
        text = normalize_spaces(row.get("text") or "")
        if "•" in text:
            text = normalize_spaces(text.split("•", 1)[0])
        if not text or text.lower() in {"view profile", "profile"} or len(text) > 90:
            # Try the first useful card line.
            for line in str(row.get("cardText") or "").splitlines():
                line = normalize_spaces(line)
                if 3 <= len(line) <= 90 and re.search(r"[A-Za-z]", line) and not re.search(r"connect|message|follow|view", line, re.I):
                    text = line
                    break
        if not text:
            text = "unknown"
        seen.add(url)
        out.append(Candidate(name=text, profile_url=url, card_text=normalize_spaces(row.get("cardText") or "")))
    return out


def click_next_page(page, args: argparse.Namespace) -> bool:
    human_delay(args, "search_scroll_to_next")
    page.mouse.wheel(0, 2600)
    page.wait_for_timeout(1000)
    selectors = [
        "button[data-testid='pagination-controls-next-button-visible']",
        "button[aria-label='Next']",
        "button:has-text('Next')",
        "a[aria-label='Next']",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        try:
            if loc.count() and loc.first.is_visible(timeout=1200):
                disabled = loc.first.get_attribute("disabled")
                aria_disabled = loc.first.get_attribute("aria-disabled")
                if disabled is not None or aria_disabled == "true":
                    return False
                human_delay(args, "click_next_page")
                before = page.url
                loc.first.click(timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=12000)
                page.wait_for_timeout(1800)
                print(f"[NEXT] before={before} after={page.url}", flush=True)
                return True
        except Exception:
            continue
    return False


def extract_profile(page, candidate: Candidate) -> dict[str, str]:
    name = ""
    for selector in ["main h1", "h1"]:
        loc = page.locator(selector)
        try:
            if loc.count():
                txt = normalize_spaces(loc.first.inner_text(timeout=2500))
                if txt and 2 <= len(txt) <= 100:
                    name = txt
                    break
        except Exception:
            pass
    if not name or name.lower() == "unknown":
        name = candidate.name

    location = ""
    location_selectors = [
        "main span.text-body-small.inline.t-black--light.break-words",
        "main .pv-text-details__left-panel span.text-body-small",
        "main .ph5 span.text-body-small",
    ]
    for selector in location_selectors:
        loc = page.locator(selector)
        try:
            for i in range(min(loc.count(), 6)):
                txt = normalize_spaces(loc.nth(i).inner_text(timeout=1000))
                if txt and 3 <= len(txt) <= 100 and not UNKNOWN_LOCATION_RE.search(txt):
                    location = txt
                    break
            if location:
                break
        except Exception:
            pass

    headline = ""
    try:
        h = page.locator("main .text-body-medium").first
        if h.count():
            headline = normalize_spaces(h.inner_text(timeout=1500))
    except Exception:
        pass

    return {"full_name": normalize_spaces(name), "location": normalize_spaces(location), "headline": normalize_spaces(headline)}


def has_message_button(page) -> bool:
    selectors = [
        "main button[aria-label*='Message']",
        "main a[aria-label*='Message']",
        "main button:has-text('Message')",
        "main a:has-text('Message')",
        "button[aria-label*='Message']",
        "button:has-text('Message')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    return False


def open_message_box(page, args: argparse.Namespace) -> bool:
    # Prefer the current profile's top Message action. LinkedIn also renders
    # recommendation/sidebar Message links; clicking those opens the wrong person.
    try:
        clicked = page.evaluate(r'''
        () => {
          const norm = s => (s || '').replace(/\s+/g, ' ').trim();
          const visible = el => {
            const r = el.getBoundingClientRect();
            const st = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
          };
          const els = Array.from(document.querySelectorAll('a[href*="/messaging/compose"], button, a[role="button"]'));
          const candidates = els.map(el => ({el, r: el.getBoundingClientRect(), txt: norm(el.innerText), aria: norm(el.getAttribute('aria-label')), href: el.href || ''}))
            .filter(x => visible(x.el))
            .filter(x => /message/i.test([x.txt, x.aria, x.href].join(' ')))
            .filter(x => x.r.left < 650 && x.r.top > 250 && x.r.top < 750);
          candidates.sort((a,b) => (a.r.top - b.r.top) || (a.r.left - b.r.left));
          if (!candidates.length) return false;
          candidates[0].el.click();
          return true;
        }
        ''')
        if clicked:
            human_delay(args, "click_message_button")
            page.wait_for_timeout(3500)
            return True
    except Exception:
        pass
    selectors = [
        "main button[aria-label*='Message']",
        "main a[aria-label*='Message']",
        "main button:has-text('Message')",
        "main a:has-text('Message')",
        "button[aria-label*='Message']",
        "button:has-text('Message')",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() and loc.first.is_visible(timeout=1500):
                human_delay(args, "click_message_button")
                loc.first.click(timeout=7000)
                page.wait_for_timeout(3500)
                return True
        except Exception:
            continue
    return False


def _locator_scopes(page):
    return [page, *page.frames]


def _first_visible_locator(page, selectors: list[str], timeout: int = 1000):
    for scope in _locator_scopes(page):
        for selector in selectors:
            try:
                loc = scope.locator(selector)
                if loc.count() and loc.last.is_visible(timeout=timeout):
                    return loc.last
            except Exception:
                continue
    return None


def find_message_editor(page):
    selectors = [
        "div.msg-form__contenteditable[contenteditable='true']",
        "div[role='textbox'][contenteditable='true']",
        "div[contenteditable='true'][aria-label*='Write']",
        "div[contenteditable='true']",
    ]
    return _first_visible_locator(page, selectors, timeout=1500)


def find_subject_input(page):
    selectors = [
        "input[name='subject']",
        "input[placeholder*='Subject']",
        "input[aria-label*='Subject']",
        "textarea[placeholder*='Subject']",
        "textarea[aria-label*='Subject']",
    ]
    return _first_visible_locator(page, selectors, timeout=1500)


def compose_subject(job: str) -> str:
    label = JOB_LABELS[job]
    return f"{label} Engineer opportunity"


def _message_still_open_with_body(page) -> bool:
    try:
        leave_dialog = page.locator("text=/Are you sure you want to discard this message|Leave\\?/i")
        if leave_dialog.count() and leave_dialog.first.is_visible(timeout=800):
            return True
    except Exception:
        pass
    selectors = ["div[contenteditable='true']", "div[role='textbox'][contenteditable='true']"]
    for scope in _locator_scopes(page):
        for selector in selectors:
            try:
                loc = scope.locator(selector)
                if loc.count() and loc.last.is_visible(timeout=800):
                    txt = normalize_spaces(loc.last.inner_text(timeout=800))
                    if "Would it be okay if I sent you my CV" in txt:
                        return True
            except Exception:
                continue
    return False


def dismiss_open_message_draft(page) -> None:
    """Discard an unsent draft after a failed send attempt and continue."""
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    except Exception:
        pass
    discard_selectors = [
        "button:has-text('Discard')",
        "button:has-text('Leave')",
        "button:has-text('Yes')",
        "button[aria-label*='Discard']",
        "button[aria-label*='Leave']",
    ]
    for scope in _locator_scopes(page):
        for selector in discard_selectors:
            try:
                loc = scope.locator(selector)
                if loc.count() and loc.last.is_visible(timeout=800):
                    loc.last.click(timeout=3000)
                    page.wait_for_timeout(800)
                    return
            except Exception:
                continue


def click_send_message(page, args: argparse.Namespace) -> bool:
    selectors = [
        "button.msg-form__send-button",
        "button[aria-label='Send']",
        "button[aria-label*='Send']",
        "button[type='submit']",
        "button:has-text('Send')",
        "button:has(svg[data-test-icon*='send'])",
    ]
    for scope in _locator_scopes(page):
        for selector in selectors:
            loc = scope.locator(selector)
            try:
                if loc.count() and loc.last.is_visible(timeout=1500):
                    disabled = loc.last.get_attribute("disabled")
                    aria_disabled = loc.last.get_attribute("aria-disabled")
                    if disabled is not None or aria_disabled == "true":
                        continue
                    human_delay(args, "click_send_message")
                    loc.last.click(timeout=7000)
                    page.wait_for_timeout(3000)
                    return not _message_still_open_with_body(page)
            except Exception:
                continue

    # Last-resort DOM click inside the messaging frame: LinkedIn sometimes
    # renders the Send control as an icon-only button with unstable classes.
    for frame in page.frames:
        try:
            clicked = frame.evaluate(r'''
            () => {
              const norm = s => (s || '').replace(/\s+/g, ' ').trim();
              const visible = el => {
                const r = el.getBoundingClientRect();
                const st = getComputedStyle(el);
                return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
              };
              const els = Array.from(document.querySelectorAll('button'));
              const candidates = els.map(el => ({el, r: el.getBoundingClientRect(), txt: norm(el.innerText), aria: norm(el.getAttribute('aria-label')), cls: String(el.className)}))
                .filter(x => visible(x.el))
                .filter(x => !x.el.disabled && x.el.getAttribute('aria-disabled') !== 'true')
                .filter(x => /send/i.test([x.txt, x.aria, x.cls, x.el.outerHTML].join(' ')));
              candidates.sort((a,b) => (b.r.top - a.r.top) || (b.r.left - a.r.left));
              if (!candidates.length) return false;
              candidates[0].el.click();
              return true;
            }
            ''')
            if clicked:
                human_delay(args, "click_send_message")
                page.wait_for_timeout(3000)
                return not _message_still_open_with_body(page)
        except Exception:
            continue
    return False


def compose_message(job: str) -> str:
    label = JOB_LABELS[job]
    return (
        "Hi! How are you?\n\n"
        f"I saw that you’re currently looking for a {label} Engineer. "
        "I’m actively exploring new opportunities and would be very interested in learning more about the role. "
        "Would it be okay if I sent you my CV? "
        "You can also schedule a quick meeting with me here: https://calendly.com/aay9898/30min"
    )


def record_skip(state: dict[str, Any], candidate: Candidate, reason: str, details: dict[str, Any] | None = None) -> None:
    if candidate.profile_url:
        rec = state["skipped_profiles"].setdefault(candidate.profile_url, {})
        rec.update({"reason": reason, "updated_at": now_iso(), **(details or {})})


def already_processed(state: dict[str, Any], candidate: Candidate) -> str | None:
    if candidate.profile_url in state.get("sent_profiles", {}):
        return "already_sent_profile"
    key = normalize_name_key(candidate.name)
    if key and key in state.get("sent_full_names", {}):
        return "already_sent_full_name"
    skipped = state.get("skipped_profiles", {}).get(candidate.profile_url)
    if skipped and skipped.get("reason") in {
        "blocked_first_name", "non_ascii_or_invalid_name", "blocked_country", "blocked_location", "existing_dialog",
        "no_message_button", "no_profile_name", "already_sent_profile", "already_sent_full_name",
        "no_message_compose_opened", "message_editor_missing", "subject_fill_failed", "message_send_failed_or_editor_missing",
        "message_editor_missing_after_subject_fill", "message_send_not_confirmed_after_body_fill",
    }:
        return f"cached_skip:{skipped.get('reason')}"
    return None


def evaluate_candidate(profile: dict[str, str], dialog_names: set[str]) -> str | None:
    full_name = profile.get("full_name", "")
    if not full_name or full_name.lower() == "unknown":
        return "no_profile_name"
    if not is_asciiish_name(full_name):
        return "non_ascii_or_invalid_name"
    first = first_name_key(full_name)
    if is_blocked_first_name(first):
        return f"blocked_first_name:{first or 'unknown'}"
    if normalize_name_key(full_name) in dialog_names:
        return "existing_dialog"
    location = profile.get("location", "")
    combined = " ".join([location, profile.get("headline", "")])
    if BLOCKED_COUNTRIES_RE.search(combined):
        return "blocked_location:india_pakistan_or_blocked_city"
    return None


def send_message_to_candidate(page, args: argparse.Namespace, candidate: Candidate, job: str, profile: dict[str, str]) -> tuple[bool, str | None, bool]:
    """Return (sent_ok, failure_reason, fatal).

    If this profile does not actually send, the caller records a skip,
    discards any unsent draft, and continues toward the daily sent target.
    True security/captcha/rate-limit signals are handled separately by assert_no_stop().
    """
    if not open_message_box(page, args):
        return False, "no_message_compose_opened", False
    subject = compose_subject(job)
    draft_started = False
    subject_input = find_subject_input(page)
    if subject_input is not None:
        human_delay(args, "fill_subject")
        subject_input.click(timeout=5000)
        try:
            subject_input.fill(subject, timeout=5000)
        except Exception:
            page.keyboard.press("Meta+A")
            page.keyboard.insert_text(subject)
        page.wait_for_timeout(500)
        try:
            value = normalize_spaces(subject_input.input_value(timeout=1000))
        except Exception:
            value = ""
        if value != subject:
            return False, "subject_fill_failed", False
        draft_started = True
    editor = find_message_editor(page)
    if editor is None:
        return False, "message_editor_missing_after_subject_fill" if draft_started else "message_editor_missing", False
    msg = compose_message(job)
    human_delay(args, "fill_message")
    editor.click(timeout=5000)
    page.keyboard.insert_text(msg)
    page.wait_for_timeout(800)
    if click_send_message(page, args):
        return True, None, False
    return False, "message_send_not_confirmed_after_body_fill", False


def process_search(page, args: argparse.Namespace, status: dict[str, Any], state: dict[str, Any], dialog_names: set[str], job: str) -> None:
    search_url = SEARCHES[job]
    sent_for_job = 0
    page_no = 0
    max_pages = args.max_pages if args.max_pages and args.max_pages > 0 else 10_000

    def active_total() -> int:
        return status["would_send_count"] if args.dry_run else status["sent_count"]

    def active_for_job() -> int:
        return status["jobs"][job]["would_send"] if args.dry_run else sent_for_job

    print(f"[2/4] Search {job}: {search_url}", flush=True)
    human_delay(args, f"open_search_{job}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    assert_no_stop(page, status)

    while active_for_job() < args.max_per_job and active_total() < args.max_messages and page_no < max_pages:
        page_no += 1
        status["jobs"][job]["pages_visited"] = page_no
        assert_no_stop(page, status)
        current_search_page_url = page.url
        candidates = collect_search_candidates(page)
        print(f"[PAGE] {job} page={page_no} candidates={len(candidates)} sent_job={sent_for_job}/{args.max_per_job} sent_total={status['sent_count']}/{args.max_messages}", flush=True)
        append_action("search_page", job=job, page=page_no, candidates=len(candidates), url=page.url)
        save_json(STATUS_PATH, status)

        seen_on_page: set[str] = set()
        for candidate in candidates:
            if active_for_job() >= args.max_per_job or active_total() >= args.max_messages:
                break
            if candidate.profile_url in seen_on_page:
                continue
            seen_on_page.add(candidate.profile_url)
            status["last_profile"] = candidate.profile_url

            cached = already_processed(state, candidate)
            if cached:
                status["skipped_count"] += 1
                status["skipped_by_reason"][cached] += 1
                append_action("skip", job=job, name=candidate.name, profile_url=candidate.profile_url, reason=cached)
                continue

            try:
                human_delay(args, "open_profile")
                page.goto(candidate.profile_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2200)
                assert_no_stop(page, status)
                profile = extract_profile(page, candidate)
                candidate.name = profile.get("full_name") or candidate.name
                reason = evaluate_candidate(profile, dialog_names)
                if reason:
                    status["skipped_count"] += 1
                    status["skipped_by_reason"][reason] += 1
                    record_skip(state, candidate, reason, {"job": job, "profile": profile})
                    save_state(state)
                    append_action("skip", job=job, name=candidate.name, profile_url=candidate.profile_url, reason=reason, profile=profile)
                    continue

                if not has_message_button(page):
                    reason = "no_message_button"
                    status["skipped_count"] += 1
                    status["skipped_by_reason"][reason] += 1
                    record_skip(state, candidate, reason, {"job": job, "profile": profile})
                    save_state(state)
                    append_action("skip", job=job, name=candidate.name, profile_url=candidate.profile_url, reason=reason, profile=profile)
                    continue

                if args.dry_run:
                    status["would_send_count"] += 1
                    status["jobs"][job]["would_send"] += 1
                    append_action("would_send", job=job, name=candidate.name, profile_url=candidate.profile_url, profile=profile)
                    print(f"[WOULD_SEND] {job}: {candidate.name} — {profile.get('location')} — {candidate.profile_url}", flush=True)
                    continue

                ok, send_reason, fatal_send_failure = send_message_to_candidate(page, args, candidate, job, profile)
                assert_no_stop(page, status)
                if not ok:
                    reason = send_reason or "message_send_failed_or_editor_missing"
                    status["skipped_count"] += 1
                    status["skipped_by_reason"][reason] += 1
                    record_skip(state, candidate, reason, {"job": job, "profile": profile})
                    save_state(state)
                    save_json(STATUS_PATH, status)
                    append_action("skip", job=job, name=candidate.name, profile_url=candidate.profile_url, reason=reason, profile=profile)
                    dismiss_open_message_draft(page)
                    continue

                sent_for_job += 1
                status["sent_count"] += 1
                status["jobs"][job]["sent"] += 1
                state["sent_profiles"][candidate.profile_url] = {"full_name": candidate.name, "job": job, "sent_at": now_iso(), "profile": profile}
                state["sent_full_names"][normalize_name_key(candidate.name)] = {"profile_url": candidate.profile_url, "job": job, "sent_at": now_iso()}
                dialog_names.add(normalize_name_key(candidate.name))
                save_state(state)
                save_dialog_names(dialog_names, [candidate.name])
                append_action("sent", job=job, name=candidate.name, profile_url=candidate.profile_url, profile=profile)
                print(f"[SENT] {job}: {candidate.name} ({status['sent_count']}/{args.max_messages}; job {sent_for_job}/{args.max_per_job})", flush=True)
                save_json(STATUS_PATH, status)
            except SystemExit:
                raise
            except Exception as exc:
                reason = "candidate_error"
                status["errors"].append({"job": job, "name": candidate.name, "profile_url": candidate.profile_url, "error": repr(exc)[:500]})
                status["skipped_count"] += 1
                status["skipped_by_reason"][reason] += 1
                record_skip(state, candidate, reason, {"job": job, "error": repr(exc)[:500]})
                save_state(state)
                append_action("error", job=job, name=candidate.name, profile_url=candidate.profile_url, error=repr(exc)[:500])
                dismiss_open_message_draft(page)

        if active_for_job() >= args.max_per_job or active_total() >= args.max_messages:
            break
        if page_no >= max_pages:
            status["jobs"][job]["stop_reason"] = "max_pages_reached"
            append_action("job_done", job=job, reason="max_pages_reached", pages=page_no, sent=sent_for_job)
            break
        assert_no_stop(page, status)
        if page.url != current_search_page_url:
            human_delay(args, "return_to_search_page")
            page.goto(current_search_page_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            assert_no_stop(page, status)
        if not click_next_page(page, args):
            status["jobs"][job]["stop_reason"] = "no_next_page"
            append_action("job_done", job=job, reason="no_next_page", pages=page_no, sent=sent_for_job)
            break


def make_status(args: argparse.Namespace) -> dict[str, Any]:
    jobs = args.job if args.job != "all" else "DevOps,SRE,Platform"
    selected = [j.strip() for j in jobs.split(",") if j.strip()]
    return {
        "started_at": now_iso(),
        "finished_at": None,
        "dry_run": bool(args.dry_run),
        "selected_jobs": selected,
        "max_messages": args.max_messages,
        "max_per_job": args.max_per_job,
        "max_pages": args.max_pages,
        "sent_count": 0,
        "would_send_count": 0,
        "skipped_count": 0,
        "skipped_by_reason": defaultdict(int),
        "errors": [],
        "jobs": defaultdict(lambda: {"sent": 0, "would_send": 0, "pages_visited": 0, "stop_reason": None}),
        "last_profile": None,
        "stop_reason": None,
        "block_screenshot": None,
        "next_run": next_9am_iso(),
        "status_path": str(STATUS_PATH),
        "state_path": str(STATE_PATH),
        "dialog_stoplist_path": str(DIALOG_STOPLIST_PATH),
        "weekly_log_path": str(weekly_log_path()),
    }


def jsonable_status(status: dict[str, Any]) -> dict[str, Any]:
    out = dict(status)
    out["skipped_by_reason"] = dict(status.get("skipped_by_reason", {}))
    out["jobs"] = {k: dict(v) for k, v in status.get("jobs", {}).items()}
    return out


def run(args: argparse.Namespace) -> int:
    ensure_dirs()
    status = make_status(args)
    save_json(STATUS_PATH, jsonable_status(status))
    append_action("run_start", dry_run=args.dry_run, max_messages=args.max_messages, max_per_job=args.max_per_job, max_pages=args.max_pages, job=args.job)

    if args.self_test:
        assert "mohammad" in BLOCKED_FIRST_NAMES
        assert first_name_key("  Ali Khan  ") == "ali"
        assert evaluate_candidate({"full_name": "Ali Khan", "location": "London, England", "headline": "Recruiter"}, set()).startswith("blocked_first_name")
        assert evaluate_candidate({"full_name": "John Smith", "location": "India", "headline": "Recruiter"}, set()).startswith("blocked_location")
        assert evaluate_candidate({"full_name": "John Smith", "location": "Mumbai, Maharashtra", "headline": "Recruiter"}, set()).startswith("blocked_location")
        assert evaluate_candidate({"full_name": "John Smith", "location": "", "headline": "Recruiter"}, set()) is None
        assert evaluate_candidate({"full_name": "John Smith", "location": "Unknown", "headline": "Recruiter"}, set()) is None
        assert evaluate_candidate({"full_name": "AmirAbbas Yousefi", "location": "London", "headline": "Recruiter"}, set()).startswith("blocked_first_name")
        assert evaluate_candidate({"full_name": "Simran K.", "location": "Canada", "headline": "Recruiter"}, set()) == "non_ascii_or_invalid_name"
        assert evaluate_candidate({"full_name": "💾 Dan Lopatkin", "location": "Germany", "headline": "Recruiter"}, set()) == "non_ascii_or_invalid_name"
        status["finished_at"] = now_iso()
        status["stop_reason"] = "self_test_ok"
        save_json(STATUS_PATH, jsonable_status(status))
        print(json.dumps(jsonable_status(status), ensure_ascii=False, indent=2))
        return 0

    state = load_state()
    selected_jobs = [args.job] if args.job != "all" else ["DevOps", "SRE", "Platform"]
    for job in selected_jobs:
        if job not in SEARCHES:
            raise SystemExit(f"Unknown job: {job}")

    with sync_playwright() as p:
        browser_handle, context, page, browser_mode = open_linkedin_context(p, status)
        status["browser_mode"] = browser_mode
        page.set_default_timeout(10000)

        dialog_names = scan_dialog_stoplist(page, args, status)
        print(f"[STOPLIST] dialog full names: {len(dialog_names)}", flush=True)

        for job in selected_jobs:
            if (status["would_send_count"] if args.dry_run else status["sent_count"]) >= args.max_messages:
                break
            process_search(page, args, status, state, dialog_names, job)
            if status.get("stop_reason"):
                break

        try:
            if browser_mode in {"storage_state", "persistent_context"}:
                browser_handle.close()
        except Exception:
            pass

    status["finished_at"] = now_iso()
    status["stop_reason"] = status.get("stop_reason") or "completed"
    save_json(STATUS_PATH, jsonable_status(status))
    append_action("run_finish", sent=status["sent_count"], would_send=status["would_send_count"], skipped=status["skipped_count"], stop_reason=status["stop_reason"])
    print(json.dumps(jsonable_status(status), ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinkedIn daily message outreach")
    parser.add_argument("--dry-run", action="store_true", help="Collect/verify candidates without sending messages")
    parser.add_argument("--max-messages", type=int, default=60, help="Daily total message cap")
    parser.add_argument("--max-per-job", type=int, default=20, help="Per-job message cap")
    parser.add_argument("--max-pages", type=int, default=0, help="Max search pages per job; 0 = all pages until no Next")
    parser.add_argument("--job", default="all", choices=["all", "DevOps", "SRE", "Platform"], help="Job search to process")
    parser.add_argument("--headful", action="store_true", help="Accepted for compatibility; CDP browser is already headful if launched that way")
    parser.add_argument("--delay-base", type=float, default=10.0, help="Base delay before LinkedIn actions")
    parser.add_argument("--delay-jitter", type=float, default=3.0, help="Random delay jitter added to base")
    parser.add_argument("--no-delay", action="store_true", help="Disable delays for self-test/local diagnostics only")
    parser.add_argument("--dialog-scrolls", type=int, default=80, help="Max scrolls while collecting messaging dialog names")
    parser.add_argument("--skip-dialog-scan", action="store_true", help="Use existing dialog stop-list without opening messaging")
    parser.add_argument("--self-test", action="store_true", help="Run local logic checks without opening LinkedIn")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return run(args)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        print(str(exc), file=sys.stderr)
        return 14
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        ensure_dirs()
        append_action("fatal", reason="unexpected_error", error=repr(exc))
        status = load_json(STATUS_PATH, {})
        status.update({"finished_at": now_iso(), "stop_reason": "unexpected_error", "error": repr(exc)[:1000]})
        save_json(STATUS_PATH, status)
        print(f"Unexpected error: {exc!r}", file=sys.stderr)
        return 14


if __name__ == "__main__":
    raise SystemExit(main())
