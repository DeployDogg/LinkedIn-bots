#!/usr/bin/env python3
import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import linkedin_message_outreach as outreach

CHECKS = {
    "DevOps": {
        "url": outreach.SEARCHES["DevOps"],
        "expected_titles": ["DevOps Engineer", "DevOps Engineer manager"],
    },
    "SRE": {
        "url": outreach.SEARCHES["SRE"],
        "expected_titles": ["Site Reliability Engineer", "Site Reliability Engineer manager"],
    },
    "Platform": {
        "url": outreach.SEARCHES["Platform"],
        "expected_titles": ["Platform Engineer"],
    },
}

JS_EXTRACT = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const body = norm(document.body.innerText || '');
  const chips = Array.from(document.querySelectorAll([
    'button[aria-label]',
    'button',
    '.search-reusables__filter-pill-button',
    '.artdeco-pill',
    '.search-reusables__primary-filter',
    '.search-reusables__filter-value-item',
    '.search-reusables__filter-binary-toggle'
  ].join(',')))
    .map(el => ({text: norm(el.innerText), aria: norm(el.getAttribute('aria-label'))}))
    .filter(x => (x.text || x.aria));
  const results = Array.from(document.querySelectorAll('li.reusable-search__result-container, .reusable-search__result-container, [data-view-name="search-entity-result-universal-template"]'))
    .slice(0, 8)
    .map(el => {
      const text = norm(el.innerText);
      const href = Array.from(el.querySelectorAll('a[href*="/in/"]')).map(a => a.href.split('?')[0])[0] || '';
      return {text, href, hasHiringText: /#?hiring|actively hiring|is hiring/i.test(text)};
    });
  return {
    title: document.title,
    url: location.href,
    bodyHead: body.slice(0, 2500),
    chips,
    results,
    hasLogin: /sign in|join linkedin|email or phone/i.test(body),
    hasSecurity: /captcha|security verification|verify your identity|checkpoint|unusual activity/i.test(body) || location.href.includes('/checkpoint'),
  };
}
"""


def expected_present(text, title):
    compact = re.sub(r"\s+", " ", text).lower()
    title_l = title.lower()
    if title_l in compact:
        return True
    # LinkedIn sometimes renders singular/plural manager/management subtly.
    if title_l.endswith(" manager") and title_l.replace(" manager", "") in compact and "manager" in compact:
        return True
    return False


def main():
    status = {}
    out = {"ok": True, "checks": {}}
    with sync_playwright() as p:
        browser_handle, context, page, browser_mode = outreach.open_linkedin_context(p, status)
        out["browser_mode"] = browser_mode
        page.set_default_timeout(15000)
        for job, cfg in CHECKS.items():
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4500)
            data = page.evaluate(JS_EXTRACT)
            text = "\n".join([
                data.get("title", ""),
                data.get("url", ""),
                data.get("bodyHead", ""),
                json.dumps(data.get("chips", []), ensure_ascii=False),
                json.dumps(data.get("results", []), ensure_ascii=False),
            ])
            title_hits = {t: expected_present(text, t) for t in cfg["expected_titles"]}
            connection_3rd = any(s in text.lower() for s in ["3rd+", "3rd", "3rd degree"])
            actively_hiring_text = any(s in text.lower() for s in ["actively hiring", "hiring for job titles", "#hiring", "hiring"])
            result_count = len(data.get("results", []))
            hiring_results = sum(1 for r in data.get("results", []) if r.get("hasHiringText"))
            job_ok = (not data.get("hasLogin")) and (not data.get("hasSecurity")) and all(title_hits.values()) and result_count > 0
            out["ok"] = out["ok"] and job_ok
            out["checks"][job] = {
                "ok": job_ok,
                "expected_title_hits": title_hits,
                "connection_3rd_visible": connection_3rd,
                "actively_hiring_visible_anywhere": actively_hiring_text,
                "result_count_sample": result_count,
                "results_with_hiring_text_sample": hiring_results,
                "url_after_load": data.get("url"),
                "has_login": data.get("hasLogin"),
                "has_security": data.get("hasSecurity"),
                "chips_sample": data.get("chips", [])[:25],
                "results_sample": data.get("results", [])[:5],
            }
        try:
            if browser_mode in {"storage_state", "persistent_context"}:
                browser_handle.close()
        except Exception:
            pass
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
