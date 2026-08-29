#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LINKEDIN_JOBSEEKER_STATE_DIR", "/Users/deploydog-ai/LinkedIn/shared/legacy_state/jobseeker"))
LOG_DIR = Path(os.environ.get("LINKEDIN_JOBSEEKER_LOG_DIR", "/shared/logs/JobSeeker"))
DAILY_LIMIT = int(os.environ.get("LINKEDIN_JOBSEEKER_DAILY_LIMIT", os.environ.get("LINKEDIN_WORKER_MAX_JOBS", "60")) or "60")
MAX_START = os.environ.get("LINKEDIN_EXTRACTOR_MAX_START", "2000")
DELAY = os.environ.get("LINKEDIN_EXTRACTOR_DELAY", "0.25")
WORKER_BATCH = int(os.environ.get("LINKEDIN_JOBSEEKER_WORKER_BATCH", "120"))
MAX_ROUNDS_PER_LABEL = int(os.environ.get("LINKEDIN_JOBSEEKER_MAX_ROUNDS_PER_LABEL", "8"))
MIN_EXTRACTED = int(os.environ.get("MIN_EXTRACTED_JOBS", "5"))

DEFAULT_SRE_URL = os.environ.get(
    "LINKEDIN_JOBSEEKER_SRE_URL",
    "https://www.linkedin.com/jobs/search-results/?currentJobId=4445094102&keywords=Site%20Reliability%20engineer%20Easy%20Apply%20under%2010%20applicants%20remote&origin=SEMANTIC_SEARCH_LANDING_PAGE",
)
DEFAULT_DEVOPS_URL = os.environ.get(
    "LINKEDIN_JOBSEEKER_DEVOPS_URL",
    "https://www.linkedin.com/jobs/search-results/?currentJobId=4445088884&keywords=DevOps%20Easy%20Apply%20under%2010%20applicants%20remote&origin=SEMANTIC_SEARCH_LANDING_PAGE",
)
RECOMMENDED_URL = os.environ.get(
    "LINKEDIN_JOBSEEKER_RECOMMENDED_URL",
    "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4435558076&discover=recommended&discoveryOrigin=JOBS_HOME_JYMBII",
)

DEFAULT_SOURCES: dict[str, list[str]] = {
    "SRE": [
        DEFAULT_SRE_URL,
        "https://www.linkedin.com/jobs/search-results/?keywords=Site%20Reliability%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=SRE&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=Production%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
    ],
    "DevOps": [
        DEFAULT_DEVOPS_URL,
        "https://www.linkedin.com/jobs/search-results/?keywords=DevOps%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=DevSecOps%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=Platform%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=Cloud%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=Infrastructure%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=Kubernetes%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=MLOps%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        "https://www.linkedin.com/jobs/search-results/?keywords=AI%20Platform%20Engineer&f_AL=true&f_WT=2&f_TPR=r604800&origin=JOB_SEARCH_PAGE_SEARCH_BUTTON",
        RECOMMENDED_URL,
    ],
}


def env_json(name: str, default: Any) -> Any:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1]
    try:
        return json.loads(raw)
    except Exception as exc:
        print(json.dumps({"event": "bad_env_json", "name": name, "error": repr(exc)}, ensure_ascii=False), flush=True)
        return default


def ba_now() -> datetime:
    return datetime.now(BA_TZ)


def ba_date() -> str:
    return ba_now().date().isoformat()


def run(cmd: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    child_env = os.environ.copy()
    if env:
        child_env.update(env)
    print(json.dumps({"event": "exec", "cmd": cmd}, ensure_ascii=False), flush=True)
    return subprocess.run(cmd, cwd=str(SCRIPT_DIR), env=child_env, text=True)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def submitted_today(progress_json: Path) -> int:
    today = ba_date()
    data = load_json(progress_json, {})
    count = 0
    for rec in data.get("records", []) if isinstance(data, dict) else []:
        if rec.get("status") == "applied/completed" and str(rec.get("submitted_at") or "").startswith(today):
            count += 1
    return count


def total_submitted_today_for_labels(labels: list[str]) -> int:
    total = 0
    for label in labels:
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        total += submitted_today(STATE_DIR / f"li_apply_{slug}_quota_progress.json")
    return total


def progress_counts(progress_json: Path) -> dict[str, int]:
    data = load_json(progress_json, {})
    out: dict[str, int] = {}
    for rec in data.get("records", []) if isinstance(data, dict) else []:
        out[rec.get("status", "pending")] = out.get(rec.get("status", "pending"), 0) + 1
    return out


def split_targets(total: int) -> list[tuple[str, int]]:
    small = total // 3
    large = total - small
    weekday = int(ba_now().strftime("%u"))
    if weekday % 2 == 0:
        return [("SRE", large), ("DevOps", small)]
    return [("DevOps", large), ("SRE", small)]


def source_groups() -> dict[str, list[str]]:
    raw = env_json("LINKEDIN_JOBSEEKER_SOURCE_URLS_JSON", DEFAULT_SOURCES)
    groups: dict[str, list[str]] = {"SRE": [], "DevOps": []}
    if isinstance(raw, dict):
        for label, urls in raw.items():
            key = "SRE" if str(label).lower() == "sre" else "DevOps"
            if isinstance(urls, str):
                urls = [urls]
            for url in urls or []:
                if isinstance(url, str) and url.startswith("https://www.linkedin.com/jobs/") and url not in groups[key]:
                    groups[key].append(url)
    # Always preserve Андрей's explicit URLs first, then broad fallbacks.
    for key, urls in DEFAULT_SOURCES.items():
        for url in urls:
            if url and url not in groups[key]:
                groups[key].append(url)
    return groups


def extract_label(label: str, urls: list[str]) -> tuple[Path, Path, int]:
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    parts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, url in enumerate(urls, 1):
        part_md = STATE_DIR / f"checked_li_jobs_{slug}_source{idx}.md"
        part_json = STATE_DIR / f"checked_li_jobs_{slug}_source{idx}.json"
        cmd = ["python3", "linkedin_extractor.py", url, str(part_md), "--json", str(part_json), "--max-start", MAX_START, "--delay", DELAY]
        rc = run(cmd).returncode
        if rc not in (0, 2):
            return part_md, part_json, -rc
        jobs = load_json(part_json, [])
        added = 0
        for job in jobs if isinstance(jobs, list) else []:
            job_url = job.get("url")
            if not job_url or job_url in seen:
                continue
            item = dict(job)
            item["queue_label"] = label
            item["source_url"] = url
            parts.append(item)
            seen.add(job_url)
            added += 1
        print(json.dumps({"event": "source_extracted", "label": label, "source_index": idx, "jobs": len(jobs) if isinstance(jobs, list) else 0, "added": added, "url": url}, ensure_ascii=False), flush=True)
    out_md = STATE_DIR / f"checked_li_jobs_{slug}_quota.md"
    out_json = STATE_DIR / f"checked_li_jobs_{slug}_quota.json"
    out_json.write_text(json.dumps(parts, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# LinkedIn extracted jobs", ""]
    for job in parts:
        title = job.get("title") or "LinkedIn job"
        company = job.get("company") or ""
        location = job.get("location") or job.get("geo") or ""
        url = job.get("url") or ""
        lines.append(f"## Вакансия {title}")
        lines.append(f"- [{label}] {company} — {location} - {url}")
        lines.append("")
    out_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return out_md, out_json, len(parts)


def run_label(label: str, target: int, urls: list[str]) -> tuple[int, int]:
    source_md, _source_json, extracted = extract_label(label, urls)
    slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
    progress_json = STATE_DIR / f"li_apply_{slug}_quota_progress.json"
    progress_md = STATE_DIR / f"li_apply_{slug}_quota_progress.md"
    print(json.dumps({"event": "label_start", "label": label, "target_submissions": target, "extracted": extracted, "progress": str(progress_json)}, ensure_ascii=False), flush=True)
    if extracted < MIN_EXTRACTED:
        return 70, 0
    total_submitted_before = submitted_today(progress_json)
    for round_no in range(1, MAX_ROUNDS_PER_LABEL + 1):
        current = submitted_today(progress_json)
        remaining = max(0, target - current)
        counts = progress_counts(progress_json)
        print(json.dumps({"event": "quota_round", "label": label, "round": round_no, "submitted_today": current, "remaining": remaining, "counts": counts}, ensure_ascii=False), flush=True)
        if remaining <= 0:
            break
        if counts and counts.get("pending", 0) == 0:
            break
        cmd = [
            "python3",
            "linkedin_worker.py",
            "--retry-blocked",
            "--max-jobs",
            str(WORKER_BATCH),
            "--max-submissions",
            str(remaining),
        ]
        env = {
            "LINKEDIN_JOBS_SOURCE": str(source_md),
            "LINKEDIN_PROGRESS_JSON": str(progress_json),
            "LINKEDIN_PROGRESS_MD": str(progress_md),
        }
        rc = run(cmd, env=env).returncode
        after = submitted_today(progress_json)
        print(json.dumps({"event": "quota_worker_done", "label": label, "round": round_no, "exit_code": rc, "submitted_delta": after - current, "submitted_today": after, "counts": progress_counts(progress_json)}, ensure_ascii=False), flush=True)
        if rc in (11, 12):
            return rc, after - total_submitted_before
        if after >= target:
            break
        if after == current and progress_counts(progress_json).get("pending", 0) == 0:
            break
    return 0, submitted_today(progress_json) - total_submitted_before


def main() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    groups = source_groups()
    targets = split_targets(DAILY_LIMIT)
    print(json.dumps({"event": "quota_plan", "ba_time": ba_now().isoformat(timespec="seconds"), "daily_limit_submissions": DAILY_LIMIT, "targets": targets, "source_counts": {k: len(v) for k, v in groups.items()}}, ensure_ascii=False), flush=True)
    total_new = 0
    max_exit = 0
    labels = [label for label, _target in targets]
    for label, target in targets:
        rc, delta = run_label(label, target, groups.get(label, []))
        total_new += delta
        submitted_total = total_submitted_today_for_labels(labels)
        dod_reached = submitted_total >= DAILY_LIMIT
        if rc in (11, 12):
            print(json.dumps({"event": "quota_stopped", "label": label, "exit_code": rc, "total_new_submissions": total_new, "submitted_today_total": submitted_total, "target": DAILY_LIMIT, "dod_reached": dod_reached}, ensure_ascii=False), flush=True)
            return rc
        max_exit = max(max_exit, rc)
    submitted_total = total_submitted_today_for_labels(labels)
    dod_reached = submitted_total >= DAILY_LIMIT
    print(json.dumps({"event": "quota_done", "target": DAILY_LIMIT, "new_submissions": total_new, "submitted_today_total": submitted_total, "dod_reached": dod_reached, "exit_code": max_exit}, ensure_ascii=False), flush=True)
    return max_exit


if __name__ == "__main__":
    raise SystemExit(main())
