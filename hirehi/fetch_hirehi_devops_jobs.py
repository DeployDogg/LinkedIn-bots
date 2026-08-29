#!/usr/bin/env python3
from __future__ import annotations

import csv
import dataclasses
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_SEARCH_URL = "https://hirehi.ru/?format=%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D0%BE&level=middle&level=senior&level=lead&search=DevOps&page=1"
DEFAULT_OUTDIR = Path("/Users/deploydog-ai/LinkedIn/hirehi/output")
BLACKLIST_SUBSTRINGS = [
    "t.me/tribute",
    "jun_hi_devops",
    "generalsupport",
    "hirehi.ru/static",
    "productradar.ru",
    "google.com/search",
    "chatgpt.com",
    "claude.ai",
    "perplexity.ai",
    "grok.com",
    "linkedin.com/company/107994980",  # HireHi social footer
]


def log(msg: str) -> None:
    print(msg, flush=True)


class HireHiClient:
    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.headers = {"User-Agent": "Mozilla/5.0"}
        self.access_token = None

    def request(self, url: str, data: bytes | None = None, extra_headers: dict[str, str] | None = None, timeout: int = 30) -> str:
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(url, data=data, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "ignore")

    def login(self, email: str, password: str) -> dict[str, Any]:
        body = json.dumps({"email": email, "password": password}).encode("utf-8")
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                raw = self.request(
                    "https://hirehi.ru/api/auth/login",
                    data=body,
                    extra_headers={"Content-Type": "application/json"},
                    timeout=30,
                )
                payload = json.loads(raw)
                self.access_token = payload.get("access_token")
                return payload
            except Exception as e:
                last_error = e
                code = getattr(e, 'code', None)
                if code != 429 or attempt == 4:
                    raise
                time.sleep(2 ** attempt)
        raise last_error or RuntimeError("login failed")

    def get(self, url: str, timeout: int = 30) -> str:
        return self.request(url, timeout=timeout)

    def get_json(self, url: str, timeout: int = 30) -> dict[str, Any]:
        return json.loads(self.get(url, timeout=timeout))


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


@dataclass
class JobRecord:
    job_url: str
    slug: str
    title: str = ""
    company: str = ""
    location: str = ""
    direct_apply: bool = False
    contact_channel: str = "unknown"
    contact_urls: list[str] = dataclasses.field(default_factory=list)
    detail_status: str = "ok"
    notes: str = ""


def extract_json_ld(html_text: str) -> list[dict[str, Any]]:
    blocks = re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html_text, flags=re.S)
    out = []
    for block in blocks:
        try:
            obj = json.loads(block)
        except Exception:
            continue
        if isinstance(obj, list):
            out.extend([x for x in obj if isinstance(x, dict)])
        elif isinstance(obj, dict):
            out.append(obj)
    return out


def extract_job_urls(search_html: str) -> list[str]:
    urls = []
    for href in re.findall(r'href="(/devops/[^"]+)"', search_html):
        if href not in urls:
            urls.append(href)
    return urls


def get_total_pages(search_html: str) -> int:
    m = re.search(r'data-total-pages="(\d+)"', search_html)
    if m:
        return int(m.group(1))
    return 1


def build_url_with_page(url: str, page: int) -> str:
    parsed = urllib.parse.urlparse(url)
    pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    filtered = [(k, v) for k, v in pairs if k != 'page']
    filtered.append(('page', str(page)))
    return urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(filtered, doseq=True)))


def fetch_all_search_pages(client: HireHiClient, first_url: str) -> tuple[list[str], dict[int, int], int]:
    parsed = urllib.parse.urlparse(first_url)
    qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    first_page = 1
    for k, v in qs:
        if k == 'page' and v.isdigit():
            first_page = int(v)
            break
    first_url = build_url_with_page(first_url, first_page)
    first_html = client.get(first_url)
    total_pages = get_total_pages(first_html)
    page_job_urls: dict[int, list[str]] = {first_page: extract_job_urls(first_html)}
    all_urls = list(page_job_urls[first_page])
    for page in range(first_page + 1, total_pages + 1):
        page_url = build_url_with_page(first_url, page)
        html_text = client.get(page_url)
        urls = extract_job_urls(html_text)
        page_job_urls[page] = urls
        for u in urls:
            if u not in all_urls:
                all_urls.append(u)
    return all_urls, {p: len(u) for p, u in page_job_urls.items()}, total_pages


def classify_external_links(links: Iterable[str], job_url: str) -> list[str]:
    out = []
    job_url = job_url.rstrip('/')
    for link in links:
        if not link.startswith("http"):
            continue
        if link.rstrip('/') == job_url:
            continue
        if 'hirehi.ru' in link and 't.me/' not in link and 'linkedin.com' not in link:
            continue
        if any(substr in link for substr in BLACKLIST_SUBSTRINGS):
            continue
        out.append(link)
    return out


def classify_channel(direct_apply: bool, links: list[str]) -> str:
    if links:
        if any("t.me/" in link for link in links):
            return "telegram"
        if any("linkedin.com" in link for link in links):
            return "linkedin"
        return "external_site"
    if direct_apply:
        return "hirehi_internal"
    return "unknown"


def parse_job_detail(client: HireHiClient, slug: str) -> JobRecord:
    url = f"https://hirehi.ru{slug}"
    try:
        html_text = client.get(url)
    except Exception as e:
        return JobRecord(job_url=url, slug=slug, detail_status=f"fetch_error: {type(e).__name__}", notes=str(e))

    record = JobRecord(job_url=url, slug=slug)
    # Default extraction from JSON-LD job posting.
    for obj in extract_json_ld(html_text):
        if obj.get("@type") == "JobPosting":
            record.title = normalize_ws(obj.get("title", ""))
            hiring = obj.get("hiringOrganization") or {}
            if isinstance(hiring, dict):
                record.company = normalize_ws(hiring.get("name", ""))
            loc = obj.get("jobLocation") or {}
            if isinstance(loc, dict):
                addr = loc.get("address") or {}
                if isinstance(addr, dict):
                    record.location = normalize_ws(addr.get("addressLocality", ""))
            record.direct_apply = bool(obj.get("directApply"))
            break

    if not record.title:
        m = re.search(r'<meta property="og:title" content="([^"]+)"', html_text)
        if m:
            record.title = normalize_ws(html.unescape(m.group(1)))

    # Use only main content for job-specific links, excluding footer / site social area.
    main = html_text.split("<footer", 1)[0]
    raw_links = re.findall(r'href="([^"]+)"', main)
    ext_links = classify_external_links(raw_links, url)
    record.contact_urls = ext_links
    record.contact_channel = classify_channel(record.direct_apply, ext_links)

    if record.contact_channel == "unknown" and record.direct_apply:
        record.notes = "internal apply button only"
    elif record.contact_channel == "unknown" and not record.direct_apply:
        record.notes = "no clear contact/apply signal in main content"
    else:
        record.notes = " | ".join(record.contact_urls[:3])
    return record


def write_outputs(outdir: Path, jobs: list[JobRecord], page_counts: dict[int, int], total_pages: int, source_url: str) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    json_path = outdir / "jobs.json"
    csv_path = outdir / "jobs.csv"
    md_path = outdir / "summary.md"

    data = [dataclasses.asdict(job) for job in jobs]
    json_path.write_text(json.dumps({"source_url": source_url, "total_pages": total_pages, "page_counts": page_counts, "jobs": data}, ensure_ascii=False, indent=2), encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["title", "company", "location", "contact_channel", "direct_apply", "job_url", "contact_urls", "detail_status", "notes"])
        writer.writeheader()
        for job in jobs:
            writer.writerow({
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "contact_channel": job.contact_channel,
                "direct_apply": job.direct_apply,
                "job_url": job.job_url,
                "contact_urls": " | ".join(job.contact_urls),
                "detail_status": job.detail_status,
                "notes": job.notes,
            })

    counts = {}
    for job in jobs:
        counts[job.contact_channel] = counts.get(job.contact_channel, 0) + 1
    md = [
        f"Source: {source_url}",
        f"Total pages: {total_pages}",
        f"Total vacancies: {len(jobs)}",
        "",
        "Channel counts:",
    ]
    for k in sorted(counts):
        md.append(f"- {k}: {counts[k]}")
    md.append("")
    md.append("Top sample:")
    for job in jobs[:20]:
        md.append(f"- {job.title} | {job.company} | {job.contact_channel} | {job.job_url}")
    md_path.write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> int:
    email = os.environ.get("HIREHI_EMAIL")
    password = os.environ.get("HIREHI_PASSWORD")
    source_url = os.environ.get("HIREHI_SEARCH_URL", DEFAULT_SEARCH_URL)
    outdir = Path(os.environ.get("HIREHI_OUTDIR", str(DEFAULT_OUTDIR)))

    if not email or not password:
        log("Need HIREHI_EMAIL and HIREHI_PASSWORD")
        return 2

    client = HireHiClient()
    login = {}
    try:
        login = client.login(email, password)
    except Exception as e:
        code = getattr(e, 'code', None)
        if code == 429:
            log('HireHi login rate-limited; continuing with public pages only')
        else:
            raise
    if login and not login.get("access_token"):
        log("Login failed: no access token; continuing with public pages only")

    urls, page_counts, total_pages = fetch_all_search_pages(client, source_url)
    log(f"Found {len(urls)} unique vacancies across {total_pages} pages")

    jobs: list[JobRecord] = []
    max_workers = min(12, max(4, (os.cpu_count() or 4) * 2))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(parse_job_detail, client, slug): slug for slug in urls}
        done = 0
        for fut in as_completed(futures):
            job = fut.result()
            jobs.append(job)
            done += 1
            if done % 25 == 0 or done == len(urls):
                log(f"Processed {done}/{len(urls)} job pages")

    jobs.sort(key=lambda j: (j.title.lower(), j.company.lower(), j.slug))
    write_outputs(outdir, jobs, page_counts, total_pages, source_url)
    log(f"Wrote {outdir / 'jobs.json'}")
    log(f"Wrote {outdir / 'jobs.csv'}")
    log(f"Wrote {outdir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
