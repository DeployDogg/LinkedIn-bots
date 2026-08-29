#!/usr/bin/env python3
"""Central Chromium CDP runtime helpers for JobSeeker.

JobSeeker workers share the single persistent Chromium owned by the
linkedin-browser service. They may create and close only their own page.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Any

DEFAULT_CDP_ENDPOINT = "http://linkedin-browser:9222"


@dataclass
class CentralPageLease:
    browser: Any
    context: Any
    page: Any


def cdp_endpoint() -> str:
    value = os.environ.get("LINKEDIN_CDP_ENDPOINT")
    return str(value).strip() if value else DEFAULT_CDP_ENDPOINT


def open_central_page(playwright: Any) -> CentralPageLease:
    browser = playwright.chromium.connect_over_cdp(cdp_endpoint())
    contexts = list(getattr(browser, "contexts", []) or [])
    if len(contexts) != 1:
        raise RuntimeError(
            f"expected exactly one persistent default context from central Chromium; got {len(contexts)}"
        )
    context = contexts[0]
    page = context.new_page()
    return CentralPageLease(browser=browser, context=context, page=page)


@contextmanager
def central_page(playwright: Any) -> Iterator[CentralPageLease]:
    lease = open_central_page(playwright)
    try:
        yield lease
    finally:
        try:
            lease.page.close()
        except Exception:
            pass
