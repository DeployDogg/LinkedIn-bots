#!/usr/bin/env python3
"""Extract LinkedIn Jobs search results into linkedin_worker.py input format.

Input can be either:
  - a LinkedIn Jobs search URL, e.g. /jobs/search-results/?keywords=DevOps...
  - a local markdown/text file containing LinkedIn job URLs

Output format matches linkedin_worker.parse_jobs():

## Вакансия <title>
- <company> — <location> - https://www.linkedin.com/jobs/view/<id>/
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

def env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    value = str(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


DEFAULT_OUTPUT = Path(env_str("LINKEDIN_EXTRACTOR_DEFAULT_OUTPUT", "/Users/deploydog-ai/Downloads/checked_li_jobs.md"))
DEFAULT_JSON = Path(env_str("LINKEDIN_EXTRACTOR_DEFAULT_JSON", "/Users/deploydog-ai/LinkedIn/shared/legacy_state/extracted_linkedin_jobs.json"))
BASE_DIR = Path(env_str("LINKEDIN_LEGACY_STATE_DIR", "/Users/deploydog-ai/LinkedIn/shared/legacy_state"))
USER_AGENT = env_str("LINKEDIN_USER_AGENT", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36")
ALLOWED_TITLE_RE = re.compile(env_str("LINKEDIN_ALLOWED_TITLE_REGEX", r"\b(devops|sre|site\s+reliability|reliability|ai\s+platform|platform|cloud\s+architect|architect)\b"), re.I)
DISALLOWED_TITLE_RE = re.compile(env_str("LINKEDIN_DISALLOWED_TITLE_REGEX", r"\b(manager|lead|sales|account executive|business development|recruiter|marketing|talent acquisition|project manager|hr|help desk|desktop support|data entry|internship|intern)\b"), re.I)
LINKEDIN_BASE_URL = env_str("LINKEDIN_BASE_URL", "https://www.linkedin.com")
LINKEDIN_JOBS_GUEST_API_URL_BASE = env_str("LINKEDIN_JOBS_GUEST_API_URL_BASE", "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search")


def is_allowed_title(title: str) -> bool:
    return bool(ALLOWED_TITLE_RE.search(title or "")) and not bool(DISALLOWED_TITLE_RE.search(title or ""))


@dataclass
class Job:
    job_id: str
    title: str
    company: str
    location: str
    url: str
    source_start: int | None = None


def norm(text: str | None) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def canonical_job_url(job_id: str) -> str:
    return f"{LINKEDIN_BASE_URL}/jobs/view/{job_id}/"


def extract_param(url: str, name: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    values = qs.get(name)
    return values[0] if values else None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return str(value).strip().strip("'\"").lower() in {"1", "true", "yes", "on"}


def build_guest_url(search_url: str, start: int) -> str:
    """Convert a LinkedIn search-results URL to the guest pagination endpoint.

    Андрей's saved semantic-search URLs sometimes say "Easy Apply remote" only in
    keywords. The guest endpoint does not infer that reliably, so force the real
    LinkedIn filters by default: f_AL=true (Easy Apply) and f_WT=2 (Remote).
    """
    parsed = urllib.parse.urlparse(search_url)
    qs = urllib.parse.parse_qs(parsed.query)
    keep: dict[str, str] = {"start": str(start)}
    safe_filter_keys = (
        "keywords",
        "location",
        "geoId",
        "f_AL",   # Easy Apply
        "f_C",    # company
        "f_E",    # experience
        "f_JT",   # job type
        "f_TPR",  # time posted
        "f_WT",   # workplace type
        "sortBy",
    )
    for key in safe_filter_keys:
        if qs.get(key):
            keep[key] = qs[key][0]
    if "keywords" not in keep:
        keep["keywords"] = ""
    if env_bool("LINKEDIN_EXTRACTOR_FORCE_EASY_APPLY_REMOTE", True):
        keep.setdefault("f_AL", "true")
        keep.setdefault("f_WT", "2")
        keep.setdefault("f_TPR", env_str("LINKEDIN_EXTRACTOR_FORCE_TPR", "r604800"))
    return LINKEDIN_JOBS_GUEST_API_URL_BASE + "?" + urllib.parse.urlencode(keep)


def fetch(url: str, attempts: int = 4) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    last_status = 0
    last_text = ""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return int(resp.status), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_status = int(exc.code)
            last_text = exc.read().decode("utf-8", "replace")
            if last_status < 500 and last_status != 429:
                return last_status, last_text
        except (urllib.error.URLError, http.client.IncompleteRead, http.client.RemoteDisconnected) as exc:
            last_status = 0
            last_text = str(exc)
        if attempt < attempts:
            time.sleep(min(8.0, 1.5 * attempt))
    return last_status, last_text


def parse_cards(page_html: str, start: int | None = None) -> list[Job]:
    """Parse LinkedIn guest job cards without external dependencies."""
    chunks = re.split(r'<div[^>]+class="[^"]*job-search-card[^"]*"', page_html)
    out: list[Job] = []
    for chunk in chunks[1:]:
        job_id = None
        id_match = re.search(r"urn:li:jobPosting:(\d{6,})", chunk) or re.search(r"/jobs/view/(\d{6,})", chunk)
        if id_match:
            job_id = id_match.group(1)
        if not job_id:
            continue
        title_match = re.search(r"base-search-card__title[^>]*>(.*?)</", chunk, re.S) or re.search(r"<h3[^>]*>(.*?)</h3>", chunk, re.S)
        company_match = re.search(r"base-search-card__subtitle[^>]*>\s*<a[^>]*>(.*?)</a>", chunk, re.S) or re.search(r"base-search-card__subtitle[^>]*>(.*?)</", chunk, re.S)
        location_match = re.search(r"job-search-card__location[^>]*>(.*?)</", chunk, re.S)
        title = norm(title_match.group(1) if title_match else "Unknown")
        company = norm(company_match.group(1) if company_match else "Unknown")
        location = norm(location_match.group(1) if location_match else "Unknown")
        out.append(Job(job_id=job_id, title=title or "Unknown", company=company or "Unknown", location=location or "Unknown", url=canonical_job_url(job_id), source_start=start))
    return out


def extract_from_search_url(search_url: str, max_jobs: int = 0, max_start: int = 1000, delay: float = 0.4) -> list[Job]:
    seen: dict[str, Job] = {}
    empty_pages = 0
    for start in range(0, max_start + 1, 10):
        guest_url = build_guest_url(search_url, start)
        status, text = fetch(guest_url)
        if status >= 400:
            print(f"stop: LinkedIn returned HTTP {status} at start={start}", file=sys.stderr)
            break
        jobs = parse_cards(text, start=start)
        new_count = 0
        for job in jobs:
            if not is_allowed_title(job.title):
                continue
            if job.job_id not in seen:
                seen[job.job_id] = job
                new_count += 1
                if max_jobs and len(seen) >= max_jobs:
                    return list(seen.values())
        print(json.dumps({"event": "page", "start": start, "cards": len(jobs), "new": new_count, "total": len(seen)}, ensure_ascii=False), flush=True)
        if not jobs or new_count == 0:
            empty_pages += 1
            if empty_pages >= 2:
                break
        else:
            empty_pages = 0
        time.sleep(delay)
    return list(seen.values())


def is_linkedin_blocker(page) -> tuple[bool, str]:
    url = str(getattr(page, "url", "") or "").lower()
    try:
        text = page.locator("body").inner_text(timeout=3000).lower()
    except Exception:
        text = ""
    if any(x in url for x in ("/login", "authwall", "checkpoint", "challenge")):
        return True, f"blocked LinkedIn URL: {getattr(page, 'url', '')}"
    if any(x in text for x in ("captcha", "security verification", "verify your identity", "unusual activity", "authwall")):
        return True, "blocked LinkedIn page text detected"
    return False, ""


def extract_from_collection_url(collection_url: str, max_jobs: int = 0, max_start: int = 1000, delay: float = 0.4) -> list[Job]:
    """Extract visible jobs from authenticated LinkedIn collection pages."""
    from linkedin_central_browser import central_page  # connect_over_cdp
    from playwright.sync_api import sync_playwright

    max_scrolls = max(1, min(120, max_start // 10 if max_start else 60))
    seen: dict[str, Job] = {}
    with sync_playwright() as p:
        with central_page(p) as lease:
            page = lease.page
            page.goto(collection_url, wait_until="domcontentloaded", timeout=60000)
            blocked, reason = is_linkedin_blocker(page)
            if blocked:
                raise RuntimeError(reason)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            stable_rounds = 0
            last_total = 0
            last_scroll_top = -1
            for idx in range(max_scrolls):
                blocked, reason = is_linkedin_blocker(page)
                if blocked:
                    raise RuntimeError(reason)
                rows = page.evaluate(
                    """
                    () => {
                      const clean = (x) => (x || '').replace(/\s+/g, ' ').trim();
                      const hasJobCards = (el) => el && el.querySelectorAll('[data-job-id], [data-occludable-job-id], a[href*="/jobs/view/"]').length >= 3;
                      const scrollables = [...document.querySelectorAll('body *')]
                        .filter(el => el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 250 && hasJobCards(el))
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                      const root = scrollables[0] || document;
                      const scope = root === document ? document : root;
                      const cards = [...scope.querySelectorAll('[data-job-id], [data-occludable-job-id], .job-card-container, .jobs-search-results__list-item')];
                      const out = [];
                      const seenIds = new Set();
                      for (const card of cards) {
                        const link = card.querySelector('a[href*="/jobs/view/"]') || card.closest('a[href*="/jobs/view/"]');
                        const href = link ? link.href : '';
                        const rawId = String(card.getAttribute('data-job-id') || card.getAttribute('data-occludable-job-id') || '');
                        const idMatch = href.match(/\/jobs\/view\/(\d+)/) || rawId.match(/(\d{6,})/);
                        if (!idMatch || seenIds.has(idMatch[1])) continue;
                        seenIds.add(idMatch[1]);
                        const titleEl = card.querySelector('.job-card-list__title, .job-card-container__link, a[href*="/jobs/view/"], strong, h3');
                        const title = clean(titleEl ? titleEl.innerText : '');
                        if (!title) continue;
                        const companyEl = card.querySelector('.artdeco-entity-lockup__subtitle, .job-card-container__primary-description, .job-card-container__company-name');
                        const locationEl = card.querySelector('.job-card-container__metadata-item, .job-card-container__metadata-wrapper li');
                        out.push({
                          job_id: idMatch[1],
                          title,
                          company: clean(companyEl ? companyEl.innerText : 'Unknown'),
                          location: clean(locationEl ? locationEl.innerText : 'Unknown'),
                        });
                      }
                      const before = root === document ? window.scrollY : root.scrollTop;
                      const maxTop = root === document ? Math.max(document.body.scrollHeight, document.documentElement.scrollHeight) - window.innerHeight : root.scrollHeight - root.clientHeight;
                      return {rows: out, before, maxTop, rootClass: root === document ? 'document' : String(root.className || '').slice(0, 120)};
                    }
                    """
                )
                new_count = 0
                for row in rows.get("rows", []):
                    job_id = str(row.get("job_id") or "")
                    title = norm(row.get("title") or "Unknown")
                    if not job_id or not is_allowed_title(title):
                        continue
                    if job_id not in seen:
                        seen[job_id] = Job(
                            job_id=job_id,
                            title=title,
                            company=norm(row.get("company") or "Unknown"),
                            location=norm(row.get("location") or "Unknown"),
                            url=canonical_job_url(job_id),
                            source_start=idx * 10,
                        )
                        new_count += 1
                        if max_jobs and len(seen) >= max_jobs:
                            return list(seen.values())
                scroll_top = int(rows.get("before") or 0)
                max_top = int(rows.get("maxTop") or 0)
                print(json.dumps({"event": "collection_scroll", "scroll": idx, "scroll_top": scroll_top, "max_top": max_top, "root": rows.get("rootClass"), "cards": len(rows.get("rows", [])), "new": new_count, "total": len(seen)}, ensure_ascii=False), flush=True)
                if len(seen) == last_total and scroll_top == last_scroll_top:
                    stable_rounds += 1
                    if stable_rounds >= 5:
                        break
                else:
                    stable_rounds = 0
                    last_total = len(seen)
                    last_scroll_top = scroll_top
                if scroll_top >= max_top - 5:
                    break
                page.evaluate(
                    """
                    () => {
                      const hasJobCards = (el) => el && el.querySelectorAll('[data-job-id], [data-occludable-job-id], a[href*="/jobs/view/"]').length >= 3;
                      const scrollables = [...document.querySelectorAll('body *')]
                        .filter(el => el.scrollHeight > el.clientHeight + 80 && el.clientHeight > 250 && hasJobCards(el))
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                      const root = scrollables[0];
                      if (root) root.scrollBy(0, Math.max(600, Math.floor(root.clientHeight * 0.85)));
                      else window.scrollBy(0, Math.max(document.body.scrollHeight, 2500));
                    }
                    """
                )
                time.sleep(delay)
    return list(seen.values())


def extract_from_file(path: Path, max_jobs: int = 0) -> list[Job]:
    text = path.read_text(encoding="utf-8")
    seen: dict[str, Job] = {}
    current_title = "Unknown"
    for line in text.splitlines():
        header = re.match(r"##\s+Вакансия\s+(.+)", line)
        if header:
            current_title = norm(header.group(1)) or "Unknown"
        for m in re.finditer(r"https://www\.linkedin\.com/jobs/view/(\d{6,})/?", line):
            job_id = m.group(1)
            if job_id in seen:
                continue
            company = "Unknown"
            location = "Unknown"
            row = re.match(r"-\s*(.+?)\s+—\s+(.+?)\s+-\s+https://www\.linkedin\.com/jobs/view/\d+/?", line)
            if row:
                company = norm(row.group(1)) or "Unknown"
                location = norm(row.group(2)) or "Unknown"
            if not is_allowed_title(current_title):
                continue
            seen[job_id] = Job(job_id=job_id, title=current_title, company=company, location=location, url=canonical_job_url(job_id))
            if max_jobs and len(seen) >= max_jobs:
                return list(seen.values())
    return list(seen.values())


def write_outputs(jobs: Iterable[Job], output_md: Path, output_json: Path) -> None:
    jobs = list(jobs)
    output_md.parent.mkdir(parents=True, exist_ok=True)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = ["# LinkedIn extracted jobs", ""]
    for job in jobs:
        lines.append(f"## Вакансия {job.title}")
        lines.append(f"- {job.company} — {job.location} - {job.url}")
        lines.append("")
    output_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    output_json.write_text(json.dumps([asdict(job) for job in jobs], ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract LinkedIn search results to linkedin_worker input markdown.")
    parser.add_argument("source", help="LinkedIn jobs search URL or local markdown/text file")
    parser.add_argument("output", nargs="?", type=Path, default=DEFAULT_OUTPUT, help=f"Output markdown path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--json", dest="json_path", type=Path, default=DEFAULT_JSON, help=f"Output JSON path (default: {DEFAULT_JSON})")
    parser.add_argument("--max-jobs", type=int, default=0, help="Maximum jobs to extract; 0 means as many as available")
    parser.add_argument("--max-start", type=int, default=int(os.environ.get("LINKEDIN_EXTRACTOR_MAX_START", "1000")), help="Maximum LinkedIn pagination start offset")
    parser.add_argument("--delay", type=float, default=float(os.environ.get("LINKEDIN_EXTRACTOR_DELAY", "0.4")), help="Delay between guest pagination requests")
    args = parser.parse_args()

    if re.match(r"https://(www\.)?linkedin\.com/jobs/collections/", args.source):
        jobs = extract_from_collection_url(args.source, max_jobs=args.max_jobs, max_start=args.max_start, delay=args.delay)
    elif re.match(r"https://(www\.)?linkedin\.com/jobs/", args.source):
        jobs = extract_from_search_url(args.source, max_jobs=args.max_jobs, max_start=args.max_start, delay=args.delay)
    else:
        path = Path(args.source)
        if not path.exists():
            print(f"Input file not found: {path}", file=sys.stderr)
            return 1
        jobs = extract_from_file(path, max_jobs=args.max_jobs)

    write_outputs(jobs, args.output, args.json_path)
    print(json.dumps({"event": "done", "count": len(jobs), "output": str(args.output), "json": str(args.json_path)}, ensure_ascii=False))
    return 0 if jobs else 2


if __name__ == "__main__":
    raise SystemExit(main())
