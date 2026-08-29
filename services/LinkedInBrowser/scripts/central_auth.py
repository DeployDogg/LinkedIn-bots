#!/usr/bin/env python3
"""Browser-owned LinkedIn auth verifier and storage-state snapshot exporter.

Manual login happens only inside the persistent Chromium owned by linkedin-browser
(via localhost-only noVNC). This helper never reads credentials and never launches
Chromium; it only connects to the existing local CDP endpoint, verifies /feed,
and exports a secondary storage-state snapshot atomically.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

LOCAL_CDP_ENDPOINT = "http://127.0.0.1:9222"
DEFAULT_FEED_URL = "https://www.linkedin.com/feed/"
DEFAULT_BACKUP_PATH = "/session-backup/linkedin_session.json"
EXIT_SUCCESS = 0
EXIT_LOGIN_REQUIRED = 11
EXIT_SECURITY_BLOCKER = 12

AUTHWALL_RE = re.compile(
    r"\b(sign\s*in|join\s*now|log\s*in|login|authwall|войти|присоединитесь)\b",
    re.IGNORECASE,
)
SECURITY_BLOCKER_RE = re.compile(
    r"\b(captcha|checkpoint|security\s+verification|security\s+checkpoint|rate[-\s]*limit|safeguard|daily\s*limit|limit\s*reached|verify\s+(?:your\s+)?identity|verification\s+code|unusual\s+activity)\b",
    re.IGNORECASE,
)


def emit(status: str, **payload: Any) -> None:
    print(json.dumps({"status": status, **payload}, ensure_ascii=False), flush=True)


def page_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=int(os.environ.get("LINKEDIN_BODY_TIMEOUT_MS", "5000")))
    except Exception:
        return ""


def classify_page(url: str, text: str) -> int:
    haystack = f"{url}\n{text}"
    lowered_url = url.lower()
    if any(token in lowered_url for token in ("checkpoint", "challenge", "captcha", "/security", "rate-limit", "safeguard", "daily-limit")):
        return EXIT_SECURITY_BLOCKER
    if SECURITY_BLOCKER_RE.search(text):
        return EXIT_SECURITY_BLOCKER
    if "authwall" in lowered_url or "/login" in lowered_url or AUTHWALL_RE.search(haystack):
        return EXIT_LOGIN_REQUIRED
    if "/feed" not in lowered_url:
        return EXIT_LOGIN_REQUIRED
    return EXIT_SUCCESS


def atomic_export_storage_state(context: Any, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    tmp = destination.with_name(f".{destination.name}.tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    context.storage_state(path=str(tmp))
    tmp.chmod(0o600)
    os.replace(tmp, destination)
    destination.chmod(0o600)


def run(playwright: Any) -> int:
    feed_url = os.environ.get("LINKEDIN_FEED_URL", DEFAULT_FEED_URL)
    backup_path = Path(os.environ.get("LINKEDIN_SESSION_BACKUP_PATH", DEFAULT_BACKUP_PATH))
    page = None

    try:
        browser = playwright.chromium.connect_over_cdp(LOCAL_CDP_ENDPOINT)
    except Exception as exc:
        emit("cdp_connect_failed", code=EXIT_SECURITY_BLOCKER, endpoint=LOCAL_CDP_ENDPOINT, error=repr(exc))
        return EXIT_SECURITY_BLOCKER

    contexts = list(getattr(browser, "contexts", []) or [])
    if len(contexts) != 1:
        emit(
            "invalid_context_count",
            code=EXIT_SECURITY_BLOCKER,
            expected=1,
            actual=len(contexts),
        )
        return EXIT_SECURITY_BLOCKER

    context = contexts[0]
    try:
        page = context.new_page()
        page.goto(feed_url, wait_until="domcontentloaded", timeout=int(os.environ.get("LINKEDIN_PAGE_TIMEOUT_MS", "45000")))
        try:
            page.wait_for_load_state("networkidle", timeout=int(os.environ.get("LINKEDIN_NETWORKIDLE_TIMEOUT_MS", "8000")))
        except Exception:
            pass
        text = page_text(page)
        code = classify_page(getattr(page, "url", ""), text)
        if code == EXIT_LOGIN_REQUIRED:
            emit("login_required", code=code, url=getattr(page, "url", ""))
            return code
        if code == EXIT_SECURITY_BLOCKER:
            emit("security_blocker", code=code, url=getattr(page, "url", ""))
            return code
        atomic_export_storage_state(context, backup_path)
        emit("session_exported", code=EXIT_SUCCESS, url=getattr(page, "url", ""), session_path=str(backup_path))
        return EXIT_SUCCESS
    except Exception as exc:
        emit("auth_check_error", code=EXIT_SECURITY_BLOCKER, error=repr(exc), url=getattr(page, "url", ""))
        return EXIT_SECURITY_BLOCKER
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def main() -> int:
    with sync_playwright() as playwright:
        return run(playwright)


if __name__ == "__main__":
    sys.exit(main())
