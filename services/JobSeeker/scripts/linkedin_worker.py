#!/usr/bin/env python3
"""LinkedIn Easy Apply worker.

Runs one or more jobs from /Users/deploydog-ai/Downloads/checked_li_jobs.md using the
central persistent Chromium session owned by linkedin-browser.

Watcher contract:
  - stdout emits JSON events.
  - exit 0: no pending jobs or batch finished without watcher input.
  - exit 10: required question could not be answered. JSON includes question/context.
  - exit 11: session/login problem; open noVNC and run linkedin-browser central_auth.py.
  - exit 12: captcha/security/rate-limit.
  - exit 13: required resume missing/unavailable.
  - exit 14: unexpected automation error.

The worker is intentionally conservative: it never submits if an unknown
required question remains or if the review screen does not show the expected
resume filename.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from linkedin_central_browser import central_page  # connect_over_cdp
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {name}: {exc}") from exc


def env_regex(name: str, default: str) -> re.Pattern[str]:
    return re.compile(env_str(name, default), re.I)


BASE_DIR = Path(env_str("LINKEDIN_LEGACY_STATE_DIR", "/Users/deploydog-ai/LinkedIn/shared/legacy_state"))
QA_PATH = Path(env_str("LINKEDIN_QA_PATH", str(BASE_DIR / "qa_database.json")))
SOURCE_PATH = Path(env_str("LINKEDIN_JOBS_SOURCE", "/Users/deploydog-ai/Downloads/checked_li_jobs.md"))
PROGRESS_JSON = Path(env_str("LINKEDIN_PROGRESS_JSON", "/Users/deploydog-ai/Downloads/li_apply_progress.json"))
PROGRESS_MD = Path(env_str("LINKEDIN_PROGRESS_MD", "/Users/deploydog-ai/Downloads/li_apply_progress.md"))
RESUME_DIR = Path(env_str("LINKEDIN_RESUME_DIR", "/Users/deploydog-ai/Downloads/RESUME"))

CONTACT_FIRST_NAME = env_str("LINKEDIN_CONTACT_FIRST_NAME", "Andrew")
CONTACT_LAST_NAME = env_str("LINKEDIN_CONTACT_LAST_NAME", "Anashkin")
CONTACT_EMAIL = env_str("LINKEDIN_CONTACT_EMAIL", "aay9898@gmail.com")
CONTACT_PHONE_COUNTRY_LABEL = env_str("LINKEDIN_CONTACT_PHONE_COUNTRY_LABEL", "Argentina (+54)")
# LinkedIn separates the +54 country code from the local mobile number.
CONTACT_PHONE_LOCAL = env_str("LINKEDIN_CONTACT_PHONE_LOCAL", "91171454477")

TERMINAL_STATUSES = {
    "applied/completed",
    "expired/completed",
    "not_applicable/completed",
}

_RESUME_MATRIX_RAW = env_json("LINKEDIN_RESUME_MATRIX_JSON", {"sre.us": "andrew-anashkin-us-sre-cv.pdf", "sre.eu": "andrew-anashkin-eu-sre-cv.pdf", "ai.us": "andrew-anashkin-us-ai-cv.pdf", "ai.eu": "andrew-anashkin-eu-ai-platform-cv.pdf", "devops.us": "andrew-anashkin-us-devops-cv.pdf", "devops.eu": "andrew-anashkin-eu-devops-cv.pdf"})
RESUME_MATRIX = {tuple(key.split(".", 1)): value for key, value in _RESUME_MATRIX_RAW.items()}

SRE_RE = env_regex("LINKEDIN_SRE_TITLE_REGEX", r"\b(sre|site reliability|reliability engineer|production engineer|observability|infrastructure reliability)\b")
AI_RE = env_regex("LINKEDIN_AI_TITLE_REGEX", r"\b(ai|artificial intelligence|applied ai|mlops|ml engineer|machine learning|llm|agentic|genai|generative ai|data engineer|data technology|data delivery|data consultant)\b")
DEVOPS_RE = env_regex("LINKEDIN_DEVOPS_TITLE_REGEX", r"\b(devops|platform|cloud|infrastructure|kubernetes|automation engineer|architect|cto)\b")
US_RE = env_regex("LINKEDIN_US_GEO_REGEX", r"\b(us|usa|united states|america|new york|san francisco|california|\bca\b|\bny\b|texas|austin|seattle|boston|chicago|los angeles|washington|remote us|remote usa)\b")
ALLOWED_TITLE_RE = env_regex("LINKEDIN_ALLOWED_TITLE_REGEX", r"\b(devops|sre|site\s+reliability|reliability|ai\s+platform|platform|cloud\s+architect|architect)\b")
DISALLOWED_TITLE_RE = env_regex("LINKEDIN_DISALLOWED_TITLE_REGEX", r"\b(manager|lead|sales|account executive|business development|recruiter|marketing|talent acquisition|project manager|product manager|program manager|hr|help desk|desktop support|data entry|data engineer|analytics engineer|business intelligence|supply chain|etl developer|java engineer|java backend|backend engineer|software engineer java|full-stack software engineer|financial systems|mainframe|migration developer|microsoft word|office administrator|internship|intern)\b")
REMOTE_RE = env_regex("LINKEDIN_REMOTE_REGEX", r"\b(remote|work from home|working from home|anywhere|distributed|relocat(e|ion)|visa sponsorship)\b")
NON_REMOTE_RE = env_regex("LINKEDIN_NON_REMOTE_REGEX", r"\b(hybrid|on-site|onsite|on site|office-based|in office|commut(e|ing)|\d+\s+days?\s+onsite|\d+\s+days?\s+on-site)\b")
US_ONLY_RE = env_regex("LINKEDIN_US_ONLY_REGEX", r"\b(us citizen|u\.s\. citizen|u\.s\. citizenship|us citizenship|security clearance|active clearance|secret clearance|top secret|ts/sci|public trust|must be located in (the )?(u\.s\.|us|united states)|must reside in (the )?(u\.s\.|us|united states)|only candidates? (located|based) in (the )?(u\.s\.|us|united states))\b")
CAPTCHA_DIR = Path(env_str("LINKEDIN_CAPTCHA_DIR", str(BASE_DIR / "captcha_screenshots")))
HUMAN_DELAY_MIN = float(os.environ.get("LINKEDIN_HUMAN_DELAY_MIN", "2.8"))
HUMAN_DELAY_MAX = float(os.environ.get("LINKEDIN_HUMAN_DELAY_MAX", "7.5"))


@dataclass
class ApplyOutcome:
    status: str
    notes: str
    questions: list[Any] | None = None
    exit_code: int = 0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(event: str, **payload: Any) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False), flush=True)


def human_delay(multiplier: float = 1.0) -> None:
    lo = max(0.3, HUMAN_DELAY_MIN * multiplier)
    hi = max(lo + 0.2, HUMAN_DELAY_MAX * multiplier)
    time.sleep(random.uniform(lo, hi))


def save_block_screenshot(page, reason: str) -> str:
    CAPTCHA_DIR.mkdir(parents=True, exist_ok=True)
    safe_reason = re.sub(r"[^a-z0-9_-]+", "_", reason.lower()).strip("_") or "linkedin_block"
    path = CAPTCHA_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_reason}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        return str(path)
    except Exception as exc:
        return f"screenshot_failed:{exc!r}"


def norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_jobs() -> list[dict[str, Any]]:
    text = SOURCE_PATH.read_text(encoding="utf-8")
    jobs: list[dict[str, Any]] = []
    title = ""
    for line in text.splitlines():
        m = re.match(r"##\s+Вакансия\s+(.+)", line)
        if m:
            title = norm(m.group(1))
            continue
        m = re.match(r"-\s*(.+?)\s+—\s+(.+?)\s+-\s+(https://www\.linkedin\.com/jobs/view/\d+/)", line)
        if m and title:
            jobs.append({"company": norm(m.group(1)), "geo": norm(m.group(2)), "url": m.group(3), "title": title})
    return jobs


def classify(title: str, geo: str) -> tuple[str, str, str]:
    if SRE_RE.search(title):
        job_type = "sre"
    elif AI_RE.search(title):
        job_type = "ai"
    elif DEVOPS_RE.search(title):
        job_type = "devops"
    else:
        job_type = "devops"
    resume_format = "us" if US_RE.search(geo) else "eu"
    return job_type, resume_format, RESUME_MATRIX[(job_type, resume_format)]


def load_progress(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    current_urls = {job["url"] for job in jobs}
    if PROGRESS_JSON.exists():
        data = json.loads(PROGRESS_JSON.read_text(encoding="utf-8"))
        # Each run is a fresh extraction. Keep prior outcomes only for URLs that are
        # still present in the current source; drop stale jobs that disappeared or came
        # from an older/broader run so progress counts stay truthful.
        data["records"] = [rec for rec in data.get("records", []) if rec.get("url") in current_urls]
        data["source_file"] = str(SOURCE_PATH)
    else:
        data = {"source_file": str(SOURCE_PATH), "created_at": now_iso(), "total_jobs": len(jobs), "records": []}
    by_url = {rec.get("url"): rec for rec in data.setdefault("records", [])}
    for job in jobs:
        if job["url"] not in by_url:
            job_type, resume_format, resume = classify(job["title"], job["geo"])
            data["records"].append(
                {
                    **job,
                    "job_type": job_type,
                    "resume_format": resume_format,
                    "resume_used": resume,
                    "status": "pending",
                    "questions": [],
                    "notes": "",
                    "updated_at": "",
                }
            )
    data["total_jobs"] = len(jobs)
    return data


def save_progress(data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    # Save blockers to a separate file
    blockers = [r for r in data.get("records", []) if r.get("status") == "blocked_by_question"]
    BASE_DIR.joinpath("blocked_questions.json").write_text(json.dumps(blockers, indent=2, ensure_ascii=False), encoding="utf-8")

    PROGRESS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    counts = {"applied/completed": 0, "expired/completed": 0, "blocked_by_question": 0, "not_applicable/completed": 0, "pending": 0}
    for rec in data.get("records", []):
        counts[rec.get("status", "pending")] = counts.get(rec.get("status", "pending"), 0) + 1
    processed = sum(counts.get(s, 0) for s in ("applied/completed", "expired/completed", "blocked_by_question", "not_applicable/completed"))
    lines = [
        "# LinkedIn Apply Progress",
        "",
        "## Summary",
        f"- Applied: {counts.get('applied/completed', 0)}",
        f"- Expired: {counts.get('expired/completed', 0)}",
        f"- Blocked by questions: {counts.get('blocked_by_question', 0)}",
        f"- Skipped / not applicable: {counts.get('not_applicable/completed', 0)}",
        f"- Total processed: {processed}",
        f"- Total remaining: {max(0, data.get('total_jobs', 0) - processed)}",
        "",
    ]
    sections = [
        ("applied/completed", "Applied"),
        ("expired/completed", "Expired / unavailable"),
        ("blocked_by_question", "Blocked by questions"),
        ("not_applicable/completed", "Skipped / not applicable"),
        ("pending", "Pending"),
    ]
    for status, heading in sections:
        lines += [f"## {heading}"]
        for rec in data.get("records", []):
            if rec.get("status") != status:
                continue
            if status == "blocked_by_question":
                lines.append(f"- {rec.get('url')} — {rec.get('company')} — {rec.get('title')} — {rec.get('geo')}")
                lines.append(f"  - Resume selected: {rec.get('resume_used', '')}")
                for question in rec.get("questions") or []:
                    lines.append(f"  - Question: {question}")
                lines.append(f"  - Why blocked: {rec.get('notes', '')}")
            else:
                lines.append(
                    f"- {rec.get('url')} — {rec.get('company')} — {rec.get('title')} — {rec.get('geo')} — {rec.get('resume_used', '')} — {rec.get('notes', '')}"
                )
        lines.append("")
    PROGRESS_MD.write_text("\n".join(lines), encoding="utf-8")


def load_qa() -> dict[str, Any]:
    return json.loads(QA_PATH.read_text(encoding="utf-8"))


def qa_match(question: str, qa: dict[str, Any], required: bool) -> tuple[str | None, str | None]:
    q = question.lower()
    q_compact = re.sub(r"[^a-z0-9]+", " ", q).strip()

    # Exact/specific form-field routing first. Generic keywords like "address" or
    # "city" are too broad for grouped LinkedIn forms and caused wrong answers to
    # be applied to state/postal/country dropdowns.
    specific_rules = [
        (r"legally\s+authori[sz]ed.*(united\s+kingdom|uk)|legally\s+authori[sz]ed.*work.*without.*sponsorship|authorised\s+to\s+work\s+in\s+the\s+united\s+kingdom", "No", "work_auth_uk_no"),
        (r"are\s+you\s+aware.*salary|aware\s+the\s+salary|base\s+salary.*are\s+you\s+aware", "Yes", "salary_awareness_yes"),
        (r"supported\s+cloud\s+infrastructure.*customer-facing.*(software\s+product|saas)|customer-facing\s+software\s+product|saas\s+platform", "Yes", "customer_facing_saas_cloud_yes"),
        (r"hands-on\s+experience\s+with\s+genai|experience\s+with\s+genai\s+models|generative\s+ai\s+models", "Yes", "genai_experience_yes"),
        (r"hands-on\s+experience\s+with\s+ai-assisted\s+development|ai-assisted\s+development", "Yes", "ai_assisted_development_yes"),
        (r"hands-on\s+experience\s+with\s+azure(\s|$)|experience\s+with\s+azure", "Yes", "azure_experience_yes"),
        (r"do\s+you\s+have\s+8\+?\s+years\s+software\s+development|8\+?\s+years\s+software\s+development", "No", "software_dev_8plus_no"),
        (r"do\s+you\s+have\s+5\+?\s+years.*(devops|site\s+reliability|cloud\s+engineering|infrastructure\s+automation)|5\+?\s+years.*devops", "Yes", "devops_5plus_yes"),
        (r"experience\s+using\s+ai\s+to\s+assist\s+with\s+programming|ai\s+to\s+assist\s+with\s+coding|claude|anthropic|codex|co-pilot|copilot", "Yes", "ai_assisted_coding_yes"),
        (r"how\s+many\s+years.*software\s+development|software\s+development\s+experience", "6", "years_software_development"),
        (r"how\s+many\s+years.*azure\s+devops\s+services|work\s+experience.*azure\s+devops\s+services|azure\s+devops\s+services", "6", "years_azure_devops"),
        (r"what\s+is\s+your\s+desired\s+salary|desired\s+salary|what\s+are\s+your\s+salary\s+expectations|salary\s+expectations", "6000", "salary_monthly_usd"),
        (r"how\s+many\s+years.*amazon\s+web\s+services|how\s+many\s+years.*\baws\b|work\s+experience.*amazon\s+web\s+services|work\s+experience.*\baws\b", "6", "years_aws_infra"),
        (r"how\s+many\s+years.*artificial\s+intelligence|work\s+experience.*\bai\b|\bai\b\s+platform", "6", "years_ai_platform"),
        (r"how\s+many\s+years.*platform\s+engineering|work\s+experience.*platform\s+engineering", "6", "years_platform_engineering"),
        (r"how\s+many\s+years.*(hands-on\s+)?devops/sre.*aws|devops/sre\s+experience.*aws\s+environments|aws\s+environments", "6", "years_aws_infra"),
        (r"how\s+many\s+years.*kubernetes.*(microservices|production)|kubernetes\s+microservices.*production", "6", "years_kubernetes"),
        (r"how\s+many\s+years.*(ci\s*cd|ci/cd|cicd).*pipelines|developing\s+ci\s*cd\s+pipelines|developing\s+ci/cd\s+pipelines", "6", "years_cicd"),
        (r"how\s+many\s+years.*(infrastructure-as-code|infrastructure\s+as\s+code|terraform|cloudformation)|developing\s+infrastructure-as-code", "6", "years_iac_terraform"),
        (r"active\s+certified\s+kubernetes\s+administrator|\bcka\b\s+certification|certified\s+kubernetes\s+administrator", "No", "cka_not_in_resume_or_qa"),
        (r"where\s+did\s+you\s+hear\s+about\s+us|how\s+did\s+you\s+hear\s+about\s+us|source\s+of\s+application", "LinkedIn", "application_source_linkedin"),
        (r"phone\s+country\s+code|phonenumber\s+country", "Argentina (+54)", "phone_country_argentina"),
        (r"current\s+address.*state|address.*state|state/province", "Foreign Countries", "address_state_foreign"),
        (r"how\s+many\s+years.*azure\s+data\s+factory", "3", "years_azure_data_factory"),
        (r"how\s+many\s+years.*data\s+engineering", "6", "years_data_engineering"),
        (r"how\s+many\s+years.*azure\s+databricks", "3", "years_azure_databricks"),
        (r"working\s+hours.*overlap.*8\s*00\s*pm\s*cet|are\s+you\s+ok\s+with\s+this", "Yes", "working_hours_overlap_ok"),
        (r"did\s+you\s+apply\s+with\s+your\s+english\s+cv|english\s+cv", "Yes", "english_cv_yes"),
        (r"current\s+address.*postal|postal\s+code|zip\s+code|postcode", "1425", "postal_code"),
        (r"current\s+address.*country|address.*country", "ARG", "address_country"),
        (r"current\s+address.*city|address.*city|current\s+city", "Buenos Aires", "address_city"),
        (r"current\s+street\s+address|street\s+address|address\s+line\s+1", "Austria 1938", "street_address"),
        (r"over\s+the\s+age\s+of\s+18|older\s+than\s+18|are\s+you\s+18", "Yes", "age_over_18"),
        (r"ever\s+worked\s+at\s+gopuff|previously\s+worked\s+at\s+gopuff", "No", "worked_at_company_no"),
        (r"currently\s+work\s+for\s+gopuff", "No", "currently_work_company_no"),
        (r"family\s+members?.*work\s+at\s+gopuff|relatives?.*work\s+at", "No", "family_at_company_no"),
        (r"require\s+sponsorship.*united\s+states|sponsorship\s+now\s+or\s+in\s+the\s+future", "Yes", "us_sponsorship_required"),
        (r"consent\s+to\s+texts?|receive\s+text\s+messages", "No", "recruiting_texts_consent_no"),
    ]
    for pattern, answer, source in specific_rules:
        if re.search(pattern, q_compact):
            return answer, source

    for block in qa.get("block_keywords", []):
        if block.lower() in q:
            return None, f"blocked_keyword:{block}"

    # Prefer longer/more specific keywords over broad fallbacks.
    answer_items = sorted(
        qa.get("answers", []),
        key=lambda item: max((len(str(k)) for k in item.get("keywords", [])), default=0),
        reverse=True,
    )
    for item in answer_items:
        if any(keyword.lower() in q for keyword in item.get("keywords", [])):
            if item.get("id") == "salary_monthly_usd" and not required:
                return "", item.get("id")
            return str(item.get("answer", "")), item.get("id")
    return None, None


def body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def click_if_visible(page, pattern: str, timeout: int = 1500) -> bool:
    try:
        page.evaluate("""() => {
          const dialog = document.querySelector('[role=dialog]');
          if (dialog) dialog.scrollTop = dialog.scrollHeight;
          const forms = document.querySelectorAll('form, .jobs-easy-apply-content');
          forms.forEach(x => { try { x.scrollTop = x.scrollHeight; } catch(e) {} });
        }""")
    except Exception:
        pass
    try:
        page.evaluate("() => { try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {} }")
    except Exception:
        pass

    loc = page.get_by_role("button", name=re.compile(pattern, re.I))
    try:
        if loc.count() > 0:
            for i in range(min(loc.count(), 5)):
                candidate = loc.nth(i)
                try:
                    if candidate.is_visible(timeout=timeout) and candidate.is_enabled(timeout=timeout):
                        candidate.click(timeout=timeout)
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # LinkedIn Easy Apply buttons sometimes have a usable visible text/aria label
    # but Playwright role-click misses them after React-controlled selects update.
    # Fall back to a DOM click on an enabled button inside the active dialog.
    try:
        return bool(page.evaluate(
            """
            (pattern) => {
              const re = new RegExp(pattern, 'i');
              const root = document.querySelector('[role=dialog]') || document;
              const buttons = [...root.querySelectorAll('button')].filter(btn => {
                if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') return false;
                const txt = `${btn.innerText || ''} ${btn.getAttribute('aria-label') || ''}`.replace(/\s+/g, ' ').trim();
                return re.test(txt);
              });
              if (!buttons.length) return false;
              const btn = buttons[buttons.length - 1];
              btn.scrollIntoView({block:'center', inline:'center'});
              btn.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
              btn.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
              btn.click();
              return true;
            }
            """,
            pattern,
        ))
    except Exception:
        return False


def select_resume(page, expected_resume: str) -> bool:
    """Select or upload the exact expected resume.

    LinkedIn often hides resumes behind a collapsed picker. If the exact filename is
    not visible, try the local file upload path before treating it as blocked.
    """
    resume_path = RESUME_DIR / expected_resume
    # Expand hidden resume list if LinkedIn provides such a button.
    for pattern in ("show more resumes", "see more resumes", "more resumes"):
        click_if_visible(page, pattern, timeout=1200)
    text = body_text(page)
    if expected_resume in text:
        try:
            page.locator(f"text={expected_resume}").first.click(timeout=2500)
        except Exception:
            # Some review screens only show the selected resume and no click is needed.
            pass
        return True

    if not resume_path.exists():
        return False

    # Try direct visible/hidden file input first.
    try:
        inputs = page.locator("input[type='file']")
        if inputs.count() > 0:
            inputs.first.set_input_files(str(resume_path), timeout=5000)
            page.wait_for_timeout(2500)
            return expected_resume in body_text(page) or "uploaded" in body_text(page).lower()
    except Exception:
        pass

    # Then try to reveal an upload input.
    for pattern in ("upload resume", "upload cv", "choose file", "add resume"):
        if click_if_visible(page, pattern, timeout=1500):
            try:
                inputs = page.locator("input[type='file']")
                if inputs.count() > 0:
                    inputs.first.set_input_files(str(resume_path), timeout=5000)
                    page.wait_for_timeout(3000)
                    return expected_resume in body_text(page) or "uploaded" in body_text(page).lower()
            except Exception:
                pass
    return False


def has_resume_controls(page) -> bool:
    js = r"""
    () => {
      const els = [...document.querySelectorAll('button,label,input,a,span,div')].filter(el => el.offsetParent !== null);
      const textHit = els.some(el => /upload resume|choose file|show more resumes|see more resumes|replace resume|resume\s*\*/i.test((el.innerText || el.getAttribute('aria-label') || '').trim()));
      const fileHit = [...document.querySelectorAll('input[type=file]')].some(el => el.offsetParent !== null || /resume|cv/i.test(el.outerHTML || ''));
      return textHit || fileHit;
    }
    """
    try:
        return bool(page.evaluate(js))
    except Exception:
        return False


def check_required_acknowledgment_checkboxes(page) -> int:
    """Tick only mandatory application-accuracy acknowledgement checkboxes.

    This is not a marketing/follow consent: LinkedIn/Lever Easy Apply sometimes
    requires a checkbox that says the application information is complete and
    accurate before Next. Submitting the application already implies this
    acknowledgement, so it is safe to automate. Avoid optional side-effect boxes.
    """
    js = r"""
    () => {
      const root = document.querySelector('[role=dialog]') || document;
      const textFor = (el) => {
        const vals = [];
        if (el.id) document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach(x => vals.push(x.innerText || ''));
        vals.push(el.getAttribute('aria-label') || '', el.name || '', el.value || '');
        let cur = el;
        for (let i = 0; i < 6 && cur; i++, cur = cur.parentElement) vals.push(cur.innerText || '');
        return vals.join(' ').replace(/\s+/g, ' ').trim();
      };
      const allow = /\b(i\s+confirm|i\s+certify|i\s+acknowledge|complete\s+and\s+accurate|misrepresentation|omission|select\s+checkbox\s+to\s+proceed|applicant\s+acknowledg)/i;
      const deny = /\b(follow|marketing|newsletter|text\s+messages?|sms|promotional|future\s+opportunities|talent\s+community|terms\s+of\s+service|privacy\s+policy)\b/i;
      let clicked = 0;
      for (const cb of [...root.querySelectorAll('input[type="checkbox"]')]) {
        if (cb.checked || cb.disabled || cb.getAttribute('aria-disabled') === 'true') continue;
        const visible = cb.offsetParent !== null || cb.closest('label,[role=checkbox]');
        if (!visible) continue;
        const txt = textFor(cb);
        const required = cb.required || cb.getAttribute('aria-required') === 'true' || /required|select checkbox to proceed/i.test(txt);
        if (required && allow.test(txt) && !deny.test(txt)) {
          try { cb.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
          cb.click();
          cb.dispatchEvent(new Event('input', {bubbles:true}));
          cb.dispatchEvent(new Event('change', {bubbles:true}));
          clicked += 1;
        }
      }
      return clicked;
    }
    """
    try:
        return int(page.evaluate(js) or 0)
    except Exception:
        return 0


def fill_contact_defaults(page) -> None:
    """Fill LinkedIn contact controls that are required but not always marked as such.

    LinkedIn contact pages sometimes use required native selects whose `.value`
    starts as the literal placeholder `Select an option`. Set the exact select by
    id/label with Playwright first, then run a DOM fallback with the native value
    setter so React/Ember sees the change.
    """
    # High-level Playwright path for native selects seen in LinkedIn contact info.
    try:
        phone_select = page.locator("select[id*='phoneNumber-country'], select[name*='phoneNumber-country']")
        if phone_select.count() > 0:
            phone_select.first.select_option(label=CONTACT_PHONE_COUNTRY_LABEL, timeout=2500)
    except Exception:
        pass
    try:
        email_select = page.locator("select").filter(has_text=re.compile(r"aay9898@gmail\.com|andrew@anashkin1\.ru", re.I))
        if email_select.count() > 0:
            selected = email_select.first.evaluate("el => el.options[el.selectedIndex]?.text || el.value || ''")
            if re.search(r"select an option|please make a selection", selected or "", re.I):
                options = email_select.first.evaluate("el => [...el.options].map(o => ({label:o.text, value:o.value}))")
                preferred = next((o for o in options if re.search(r"aay9898@gmail\.com", f"{o.get('label', '')} {o.get('value', '')}", re.I)), None)
                fallback = next((o for o in options if not re.search(r"select an option|please make a selection", o.get("label", ""), re.I)), None)
                opt = preferred or fallback
                if opt:
                    email_select.first.select_option(value=opt["value"], timeout=2500)
    except Exception:
        pass
    try:
        page.evaluate(
            r"""
            () => {
              const setValue = (el, value) => {
                const tag = el.tagName.toLowerCase();
                const proto = tag === 'select' ? HTMLSelectElement.prototype : tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value); else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                try { el.blur(); } catch(e) {}
              };
              function labelText(el) {
                const labels = [];
                if (el.id) document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach(x => labels.push(x.innerText || ''));
                labels.push(el.id || '', el.name || '', el.getAttribute('aria-label') || '');
                let cur = el;
                for (let i=0; i<4 && cur; i++, cur=cur.parentElement) labels.push(cur.innerText || '');
                return labels.join(' ').replace(/\s+/g,' ').toLowerCase();
              }
              for (const el of [...document.querySelectorAll('input, textarea')]) {
                if (el.offsetParent === null || ['hidden','submit','button','file'].includes((el.type||'').toLowerCase())) continue;
                const txt = labelText(el);
                const current = (el.value || '').trim();
                let value = null;
                if (/first name/.test(txt)) value = 'Andrew';
                else if (/last name/.test(txt)) value = 'Anashkin';
                else if (/mobile phone number|phone number/.test(txt)) value = '91171454477';
                else if (/email address/.test(txt)) value = 'aay9898@gmail.com';
                if (value && current !== value) {
                  el.focus();
                  setValue(el, value);
                }
              }
              for (const el of [...document.querySelectorAll('select')]) {
                if (el.offsetParent === null) continue;
                const txt = labelText(el);
                const selectedText = el.options[el.selectedIndex]?.text || '';
                if ((/phone country code|phonenumber-country|phone.*country/.test(txt) || /contact info/.test((document.body.innerText || '').toLowerCase())) && !/argentina\s*\(\+54\)/i.test(selectedText)) {
                  const opt = [...el.options].find(o => /argentina\s*\(\+54\)/i.test(o.text || '') || /argentina/i.test(o.text || ''));
                  if (opt) setValue(el, opt.value);
                } else if ((/city|location|state|province/.test(txt)) && /select an option|please make a selection/i.test(selectedText)) {
                  // For native location/state dropdowns, pick the first valid option.
                  const options = [...el.options].filter(o => !/select an option|please make a selection/i.test(o.text || ''));
                  if (options.length > 0) setValue(el, options[0].value);
                } else if (/email address|email/.test(txt) && /select an option|please make a selection/i.test(selectedText)) {
                  const opt = [...el.options].find(o => /aay9898@gmail\.com/i.test(o.text || o.value || '')) || [...el.options].find(o => !/select an option|please make a selection/i.test(o.text || ''));
                  if (opt) setValue(el, opt.value);
                }
              }

            }
            """
        )
        page.evaluate("() => { try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {} }")
    except Exception:
        pass

    # Playwright-level fallback: LinkedIn contact fields are React-controlled and
    # sometimes exposed more reliably through labels than DOM walking.
    for label_pattern, value in [
        (r"^First name$", CONTACT_FIRST_NAME),
        (r"^Last name$", CONTACT_LAST_NAME),
        (r"Mobile phone number|Phone number", CONTACT_PHONE_LOCAL),
        (r"Email address", CONTACT_EMAIL),
    ]:
        try:
            loc = page.get_by_label(re.compile(label_pattern, re.I))
            if loc.count() > 0:
                loc.first.fill(value, timeout=1200)
        except Exception:
            pass
    # Location/city typeaheads (e.g. Buenos Aires): fill and choose the first
    # suggestion. LinkedIn requires selecting the suggestion, not just typing text.
    # Include the generated location-GEO-LOCATION id seen in Easy Apply contact forms.
    for label_pattern, value in [
        (r"Location|City|Current city|Where are you located|Current location", "Buenos Aires"),
        (r"State|Province", "Buenos Aires"),
    ]:
        try:
            loc = page.get_by_label(re.compile(label_pattern, re.I))
            if loc.count() == 0:
                loc = page.locator("input[id*='location-GEO-LOCATION'], input[id*='location']")
            if loc.count() > 0:
                field = loc.first
                field.click(timeout=1200)
                try:
                    field.fill(value, timeout=1200)
                except Exception:
                    field.type(value, delay=20, timeout=1500)
                page.wait_for_timeout(900)
                # Take first visible suggestion regardless of exact spelling.
                suggestions = page.locator('[role="option"], .basic-typeahead__selectable, .search-typeahead-v2__hit, li').filter(has_text=re.compile(r"Buenos Aires|Argentina", re.I))
                if suggestions.count() > 0:
                    suggestions.first.click(timeout=1500)
                else:
                    page.keyboard.press("ArrowDown")
                    page.keyboard.press("Enter")
                page.wait_for_timeout(500)
        except Exception:
            pass
    try:
        # DOM fallback for generated LinkedIn location inputs where get_by_label
        # does not bind to the combobox. This is intentionally direct: fill city,
        # commit first suggestion, then let Next proceed.
        loc = page.locator("input[id*='location-GEO-LOCATION']")
        if loc.count() > 0:
            field = loc.first
            current = field.input_value(timeout=1000)
            if not current.strip():
                field.click(timeout=1200)
                field.fill("Buenos Aires", timeout=1500)
                page.wait_for_timeout(900)
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        loc = page.get_by_label(re.compile(r"Email address", re.I))
        if loc.count() > 0:
            loc.first.click(timeout=1200)
            page.wait_for_timeout(250)
            opt = page.get_by_text(re.compile(r"aay9898@gmail\.com", re.I))
            if opt.count() > 0:
                opt.last.click(timeout=1200)
    except Exception:
        pass
    # LinkedIn phone block: choose the country dropdown first, then fill the
    # separate local phone input to the right. Keep these actions independent:
    # a failed country select must not prevent phone number filling.
    try:
        page.evaluate(
            """
            (args) => {
              const setValue = (el, value) => {
                const tag = el.tagName.toLowerCase();
                const proto = tag === 'select' ? HTMLSelectElement.prototype : tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, value); else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                try { el.blur(); } catch(e) {}
              };
              const labelText = (el) => {
                const labels = [];
                if (el.id) document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach(x => labels.push(x.innerText || ''));
                labels.push(el.id || '', el.name || '', el.getAttribute('aria-label') || '');
                let cur = el;
                for (let i=0; i<5 && cur; i++, cur=cur.parentElement) labels.push(cur.innerText || '');
                return labels.join(' ').replace(/\s+/g,' ').toLowerCase();
              };
              const countrySelects = [...document.querySelectorAll('select')]
                .filter(el => el.offsetParent !== null && /phone country code|phonenumber-country|phone.*country/.test(labelText(el)));
              for (const sel of countrySelects) {
                const opt = [...sel.options].find(o => (o.text || '').trim() === args.country)
                  || [...sel.options].find(o => /argentina\s*\(\+54\)|argentina/i.test(o.text || ''));
                if (opt) setValue(sel, opt.value);
              }
              const inputs = [...document.querySelectorAll('input')]
                .filter(el => el.offsetParent !== null && !['hidden','submit','button','file'].includes((el.type||'').toLowerCase()));
              const phoneInputs = inputs.filter(el => {
                const txt = labelText(el);
                return /mobile phone number|phone number|phone$/i.test(txt) && !/country|code/.test(txt);
              });
              for (const input of phoneInputs) setValue(input, args.phone);
            }
            """,
            {"country": CONTACT_PHONE_COUNTRY_LABEL, "phone": CONTACT_PHONE_LOCAL},
        )
        page.evaluate("() => { try { document.activeElement && document.activeElement.blur && document.activeElement.blur(); } catch(e) {} }")
        page.wait_for_timeout(250)
    except Exception:
        pass
    try:
        # Native Playwright fallback for real selects.
        sel = page.locator("select[id*='phoneNumber-country'], select[name*='phoneNumber-country']")
        if sel.count() > 0:
            sel.first.select_option(label=CONTACT_PHONE_COUNTRY_LABEL, timeout=1500)
    except Exception:
        pass
    try:
        # Local phone input is separate from country select.
        phone = page.locator("input[id*='phoneNumber-nationalNumber'], input[name*='phoneNumber-nationalNumber'], input[id*='phone-number'], input[name*='phone-number']")
        if phone.count() > 0:
            phone.first.fill(CONTACT_PHONE_LOCAL, timeout=1500)
    except Exception:
        pass


def visible_required_questions(page) -> list[dict[str, Any]]:
    js = r"""
    () => {
      const out = [];
      const controls = [...document.querySelectorAll('input, textarea, select')]
        .filter(el => el.offsetParent !== null && !['hidden','submit','button','file'].includes((el.type||'').toLowerCase()));
      function labelFor(el) {
        const id = el.id;
        const labels = [];
        if (id) document.querySelectorAll(`label[for="${CSS.escape(id)}"]`).forEach(x => labels.push(x.innerText));
        let cur = el;
        for (let i=0; i<5 && cur; i++, cur=cur.parentElement) {
          const txt = (cur.innerText || '').trim();
          if (txt && txt.length < 500) labels.push(txt);
        }
        return [...new Set(labels.map(x => x.replace(/\s+/g,' ').trim()).filter(Boolean))][0] || el.getAttribute('aria-label') || el.name || '';
      }
      function radioGroupQuestion(el) {
        const labels = [];
        const fieldset = el.closest('fieldset');
        const legend = fieldset ? fieldset.querySelector('legend') : null;
        if (legend) labels.push(legend.innerText || '');
        let cur = el;
        for (let i=0; i<7 && cur; i++, cur=cur.parentElement) {
          const txt = (cur.innerText || '').replace(/\s+/g, ' ').trim();
          if (txt && txt.length < 700) labels.push(txt);
        }
        labels.push(el.getAttribute('aria-label') || '', el.name || '');
        const cleaned = labels.map(raw => String(raw || '')
          .replace(/\b(Yes|No)\b/gi, ' ')
          .replace(/\b(required|select an option|please make a selection)\b/gi, ' ')
          .replace(/\s+/g, ' ')
          .trim()
        ).filter(Boolean);
        cleaned.sort((a, b) => b.length - a.length);
        return cleaned[0] || labelFor(el);
      }
      const radioGroups = new Map();
      for (const el of controls) {
        if ((el.type || '').toLowerCase() === 'radio') {
          const key = el.name || el.id || radioGroupQuestion(el);
          if (!radioGroups.has(key)) radioGroups.set(key, []);
          radioGroups.get(key).push(el);
          continue;
        }
        const label = labelFor(el);
        const required = el.required || /\*/.test(label) || /required/i.test(label) || el.getAttribute('aria-required') === 'true';
        const value = el.value || '';
        const isSelect = el.tagName.toLowerCase()==='select';
        const invalidSelect = isSelect && (!value.trim() || /select an option|please make a selection/i.test(value) || /select an option|please make a selection/i.test((el.options[el.selectedIndex]?.text || '')));
        if (required && (!value.trim() || invalidSelect)) {
          out.push({label, tag: el.tagName.toLowerCase(), type: (el.type || '').toLowerCase(), name: el.name || '', options: isSelect ? [...el.options].map(o => o.text) : []});
        }
      }
      for (const group of radioGroups.values()) {
        const label = radioGroupQuestion(group[0]);
        const required = group.some(el => el.required || el.getAttribute('aria-required') === 'true') || /\*|required/i.test(label);
        const checked = group.some(el => el.checked);
        if (required && !checked) {
          out.push({label, tag: 'input', type: 'radio', name: group[0].name || '', options: group.map(el => labelFor(el) || el.value || '').filter(Boolean)});
        }
      }
      return out;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return []


def fill_known_required_fields(page, qa: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    unresolved: list[dict[str, Any]] = []
    questions = visible_required_questions(page)
    for question in questions:
        label = norm(question.get("label"))
        answer, source = qa_match(label, qa, required=True)
        if answer is None:
            unresolved.append(question)
            continue
        if answer == "":
            continue
        # Fill the first visible empty control with matching label text in its ancestor.
        ok = page.evaluate(
            r"""
            ({needle, value}) => {
              needle = (needle || '').toLowerCase().slice(0, 120);
              const controls = [...document.querySelectorAll('input, textarea, select')]
                .filter(el => {
                  if (el.offsetParent === null || ['hidden','submit','button','file'].includes((el.type||'').toLowerCase())) return false;
                  if (el.tagName.toLowerCase() === 'select') {
                    const txt = el.options[el.selectedIndex]?.text || '';
                    return /select an option|please make a selection/i.test(txt);
                  }
                  return !(el.value||'').trim();
                });
              function setValue(el, val) {
                const tag = el.tagName.toLowerCase();
                const proto = tag === 'select' ? HTMLSelectElement.prototype : tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(el, val); else el.value = val;
                el.dispatchEvent(new Event('input', {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
                try { el.blur(); } catch(e) {}
              }
              function text(el) {
                let cur = el, vals = [];
                if (el.id) document.querySelectorAll(`label[for="${CSS.escape(el.id)}"]`).forEach(x => vals.push(x.innerText));
                vals.push(el.id || '', el.name || '', el.getAttribute('aria-label') || '');
                for (let i=0; i<5 && cur; i++, cur=cur.parentElement) vals.push(cur.innerText || '');
                return vals.join(' ').replace(/\s+/g,' ').toLowerCase();
              }
              function radioGroupQuestion(el) {
                const vals = [];
                const fieldset = el.closest('fieldset');
                const legend = fieldset ? fieldset.querySelector('legend') : null;
                if (legend) vals.push(legend.innerText || '');
                let cur = el;
                for (let i=0; i<7 && cur; i++, cur=cur.parentElement) vals.push(cur.innerText || '');
                vals.push(el.getAttribute('aria-label') || '', el.name || '');
                const cleaned = vals.map(raw => String(raw || '')
                  .replace(/\b(Yes|No)\b/gi, ' ')
                  .replace(/\b(required|select an option|please make a selection)\b/gi, ' ')
                  .replace(/\s+/g, ' ')
                  .trim()
                ).filter(Boolean);
                cleaned.sort((a, b) => b.length - a.length);
                return (cleaned[0] || '').toLowerCase();
              }
              function selectRequiredRadioByQuestion(needle, value) {
                const wanted = String(value || '').trim().toLowerCase();
                const groups = new Map();
                for (const radio of [...document.querySelectorAll('input[type="radio"]')].filter(el => el.offsetParent !== null)) {
                  const key = radio.name || radio.id || radioGroupQuestion(radio);
                  if (!groups.has(key)) groups.set(key, []);
                  groups.get(key).push(radio);
                }
                const fragment = needle.substring(0, Math.min(needle.length, 60));
                for (const group of groups.values()) {
                  const question = radioGroupQuestion(group[0]);
                  if (!question.includes(fragment)) continue;
                  const option = group.find(radio => {
                    const optionText = text(radio);
                    return (radio.value || '').trim().toLowerCase() === wanted || optionText.split(/\s+/).includes(wanted);
                  });
                  if (!option) return false;
                  option.checked = true;
                  option.dispatchEvent(new Event('input', {bubbles:true}));
                  option.dispatchEvent(new Event('change', {bubbles:true}));
                  try { option.click(); } catch(e) {}
                  try { option.blur(); } catch(e) {}
                  return true;
                }
                return false;
              }
              if (selectRequiredRadioByQuestion(needle, value)) return true;
              const exactNeedle = needle;
              const el = controls.find(c => text(c).includes(exactNeedle.substring(0, Math.min(exactNeedle.length, 60)))) ||
                         (/phone\s+country\s+code|phonenumber\s+country/.test(needle) ? controls.find(c => /phone\s+country\s+code|phonenumber-country|phone.*country/.test(text(c))) : null) ||
                         controls[0];
              if (!el) return false;
              if (el.tagName.toLowerCase() === 'select') {
                const wanted = value.toLowerCase();
                const opt = [...el.options].find(o => (o.text || '').trim().toLowerCase() === wanted || (o.value || '').trim().toLowerCase() === wanted) ||
                            [...el.options].find(o => (o.text || '').toLowerCase().includes(wanted) || (o.value || '').toLowerCase().includes(wanted));
                if (!opt) return false;
                setValue(el, opt.value);
              } else {
                el.focus(); setValue(el, value);
              }
              return true;
            }
            """,
            {"needle": label, "value": answer},
        )
        if not ok:
            unresolved.append({**question, "answer_found": answer, "source": source, "fill_failed": True})
    return not unresolved, unresolved


def mark_record(data: dict[str, Any], rec: dict[str, Any], outcome: ApplyOutcome) -> None:
    rec["status"] = outcome.status
    rec["notes"] = outcome.notes
    rec["questions"] = outcome.questions or []
    rec["updated_at"] = now_iso()
    if outcome.status == "applied/completed" and "submitted successfully" in (outcome.notes or "").lower():
        rec["submitted_at"] = rec["updated_at"]
    job_type, resume_format, resume = classify(rec.get("title", ""), rec.get("geo", ""))
    rec.setdefault("job_type", job_type)
    rec.setdefault("resume_format", resume_format)
    rec.setdefault("resume_used", resume)
    save_progress(data)


def is_allowed_title(title: str) -> bool:
    return bool(ALLOWED_TITLE_RE.search(title or "")) and not bool(DISALLOWED_TITLE_RE.search(title or ""))


def classify_remote_status(page_text_value: str, geo: str, title: str) -> tuple[bool, str]:
    """Remote/relocation gate.

    The extractor's geo field is not enough: LinkedIn often returns city/state while
    the job detail page says Hybrid/On-site. Allow explicit Remote or relocation,
    but reject Hybrid/On-site/commute signals.
    """
    combined = "\n".join([geo or "", title or "", page_text_value or ""])
    if NON_REMOTE_RE.search(combined):
        return False, "non_remote_signal_hybrid_onsite_or_commute"
    if REMOTE_RE.search(combined):
        return True, "remote_or_relocation_signal_found"
    if env_bool("LINKEDIN_ASSUME_REMOTE_FILTERED_SOURCE", True):
        return True, "assumed_remote_from_linkedin_f_wt_filter_no_non_remote_signal"
    return False, "remote_or_relocation_signal_missing"


def is_us_only_blocked(page_text_value: str) -> bool:
    return bool(US_ONLY_RE.search(page_text_value or ""))


def process_record(page, rec: dict[str, Any], qa: dict[str, Any]) -> ApplyOutcome:
    job_type, resume_format, expected_resume = classify(rec.get("title", ""), rec.get("geo", ""))
    rec["job_type"] = job_type
    rec["resume_format"] = resume_format
    rec["resume_used"] = expected_resume
    if not is_allowed_title(rec.get("title", "")):
        return ApplyOutcome(
            "not_applicable/completed",
            "Skipped before apply: title does not contain required title keywords: DevOps, Site, Reliability, Platform, AI Platform, SRE, Cloud Architect, Architect.",
        )
    if not (RESUME_DIR / expected_resume).exists():
        return ApplyOutcome("blocked_by_question", f"Required resume missing locally: {expected_resume}", [expected_resume], exit_code=13)

    page.goto(rec["url"], wait_until="domcontentloaded", timeout=60000)
    human_delay(0.8)
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    human_delay(0.5)
    text = body_text(page)
    low = text.lower()
    url = page.url.lower()

    if any(x in url for x in ("/login", "authwall", "checkpoint", "challenge")):
        shot = save_block_screenshot(page, "login_or_checkpoint")
        return ApplyOutcome("blocked_by_question", "LinkedIn session/login/checkpoint required; stopped safely.", [page.url, f"screenshot={shot}"], exit_code=11)
    if any(x in low for x in ("captcha", "security verification", "verify your identity", "unusual activity")):
        shot = save_block_screenshot(page, "captcha_security")
        return ApplyOutcome("blocked_by_question", "LinkedIn security/captcha/rate-limit challenge; stopped safely.", [page.url, f"screenshot={shot}"], exit_code=12)
    if any(x in low for x in ("daily limit", "daily application limit", "reached the limit", "limit daily submissions", "apply tomorrow", "save this job and apply tomorrow")):
        shot = save_block_screenshot(page, "daily_apply_limit")
        return ApplyOutcome("blocked_by_question", "LinkedIn daily Easy Apply limit reached; task complete for today.", [body_text(page)[:1200], f"screenshot={shot}"], exit_code=12)
    if any(x in low for x in ("applying at a fast pace", "briefly paused easy apply", "safeguard against automated", "we limit daily submissions", "prevent bots")):
        shot = save_block_screenshot(page, "rate_limit_safeguard")
        return ApplyOutcome("blocked_by_question", "LinkedIn temporarily paused Easy Apply due to daily/rate-limit safeguard.", [body_text(page)[:1200], f"screenshot={shot}"], exit_code=12)
    if any(
        x in low
        for x in (
            "no longer accepting applications",
            "job is no longer available",
            "this job is no longer available",
            "page not found",
            "страница не найдена",
            "не удаётся найти искомую вами страницу",
            "не удается найти искомую вами страницу",
        )
    ):
        return ApplyOutcome("expired/completed", "LinkedIn showed expired/unavailable/no longer accepting applications/page not found.")
    if "application submitted" in low or "you’ve already applied" in low or "you've already applied" in low or "applied" in low and "easy apply" not in low:
        return ApplyOutcome("applied/completed", "LinkedIn already shows application submitted/applied.")
    if "apply on company website" in low or "responses managed off linkedin" in low:
        return ApplyOutcome("not_applicable/completed", "Not Easy Apply: LinkedIn routes application off-platform.")
    remote_ok, remote_reason = classify_remote_status(text, rec.get("geo", ""), rec.get("title", ""))
    if not remote_ok:
        return ApplyOutcome("not_applicable/completed", f"Skipped before apply: not verified Remote/relocation ({remote_reason}).")
    if is_us_only_blocked(text):
        return ApplyOutcome("not_applicable/completed", "Skipped before apply: US-only/citizenship/clearance requirement detected.")

    # Prefer direct Easy Apply button; fallback to SDUI apply URL.
    if not click_if_visible(page, r"easy apply|apply now|подать заявку", timeout=4000):
        apply_url = rec["url"].rstrip("/") + "/apply/?openSDUIApplyFlow=true"
        page.goto(apply_url, wait_until="domcontentloaded", timeout=45000)
    try:
        human_delay(0.6)
    except Exception:
        pass

    for _ in range(15):
        text = body_text(page)
        low = text.lower()
        if any(
            x in low
            for x in (
                "no longer accepting applications",
                "job is no longer available",
                "this job is no longer available",
                "page not found",
                "страница не найдена",
                "не удаётся найти искомую вами страницу",
                "не удается найти искомую вами страницу",
            )
        ):
            return ApplyOutcome("expired/completed", "LinkedIn showed expired/unavailable/no longer accepting applications/page not found.")
        if any(x in low for x in ("application submitted", "your application was sent")):
            return ApplyOutcome("applied/completed", "LinkedIn Easy Apply submitted successfully.")
        if any(x in low for x in ("daily limit", "daily application limit", "reached the limit", "limit daily submissions", "apply tomorrow", "save this job and apply tomorrow")):
            shot = save_block_screenshot(page, "daily_apply_limit")
            return ApplyOutcome(
                "blocked_by_question",
                "LinkedIn daily Easy Apply limit reached; task complete for today.",
                [body_text(page)[:1200], f"screenshot={shot}"],
                exit_code=12,
            )
        if any(x in low for x in ("captcha", "security verification", "verify your identity", "unusual activity", "applying at a fast pace", "paused easy apply", "automated inauthentic", "we limit daily submissions", "prevent bots")):
            shot = save_block_screenshot(page, "security_or_rate_limit")
            return ApplyOutcome(
                "blocked_by_question",
                "LinkedIn security/captcha/rate-limit safeguard is active; stopped safely.",
                [body_text(page)[:1200], f"screenshot={shot}"],
                exit_code=12,
            )
        if re.search(r"already\s+applied|you[’']ve\s+already\s+applied", low, re.I):
            return ApplyOutcome("applied/completed", "LinkedIn reports this job was already applied.")
        if "apply on company website" in low or "responses managed off linkedin" in low:
            return ApplyOutcome("not_applicable/completed", "Not Easy Apply: LinkedIn routes application off-platform.")
        fill_contact_defaults(page)
        # Review screen: submit before trying generic Next/Review buttons. LinkedIn
        # review pages contain the word "Review" in headings, so generic matching can
        # click the wrong thing and loop at 100%.
        if re.search(r"submit application|send application|отправить заявку", text, re.I):
            if expected_resume not in text:
                return ApplyOutcome(
                    "blocked_by_question",
                    f"Review screen does not show expected resume: {expected_resume}",
                    [f"Expected resume: {expected_resume}"],
                    exit_code=13,
                )
            try:
                page.evaluate(
                    """() => [...document.querySelectorAll('input[type=checkbox]')].forEach(cb => { if (cb.checked) { cb.click(); } })"""
                )
            except Exception:
                pass
            if click_if_visible(page, r"submit application|send application|отправить заявку", timeout=3000):
                page.wait_for_timeout(3000)
                if re.search(r"application submitted|your application was sent|application sent", body_text(page), re.I):
                    return ApplyOutcome("applied/completed", "LinkedIn Easy Apply submitted successfully. Page confirmed submission.")
                return ApplyOutcome("blocked_by_question", "Submit clicked but confirmation was not detected.", [body_text(page)[:800]], exit_code=14)
        # Only touch resume on the actual resume/review step. Contact-info pages can
        # contain unrelated hidden/profile text with words like CV/resume in embedded data.
        if has_resume_controls(page) and not select_resume(page, expected_resume):
            try:
                debug_base = BASE_DIR / f"debug_resume_missing_{re.sub(r'[^0-9]', '', rec.get('url', '')) or 'job'}"
                page.screenshot(path=str(debug_base.with_suffix('.png')), full_page=True)
                debug_base.with_suffix('.html').write_text(page.content(), encoding='utf-8')
            except Exception:
                pass
            return ApplyOutcome(
                "blocked_by_question",
                f"Expected resume not visible on LinkedIn Easy Apply screen: {expected_resume}",
                [f"Expected resume: {expected_resume}"],
                exit_code=13,
            )
        check_required_acknowledgment_checkboxes(page)
        known_ok, unresolved = fill_known_required_fields(page, qa)
        if not known_ok:
            joined_unresolved = "\n".join(str(q.get("label", q)) for q in unresolved).lower()
            if re.search(r"in-person|face\s+to\s+face|meet\s+in\s+person|onsite\s+interview|local\s+interview", joined_unresolved, re.I):
                return ApplyOutcome("not_applicable/completed", "Skipped Easy Apply form: requires in-person/local interview commitment.", unresolved)
            if re.search(r"identity\s+verification|process\s+my\s+image|image\s+and\s+related\s+identity|biometric", joined_unresolved, re.I):
                return ApplyOutcome("not_applicable/completed", "Skipped Easy Apply form: requires identity/image verification consent not approved for automation.", unresolved)
            return ApplyOutcome("blocked_by_question", "Unknown required LinkedIn question.", unresolved, exit_code=10)
        # Move through flow.
        if click_if_visible(page, r"next|review|continue", timeout=2500):
            page.wait_for_timeout(1200)
            continue
        # Review screen: verify exact resume filename before submit.
        if re.search(r"submit application|отправить заявку", text, re.I):
            if expected_resume not in text:
                return ApplyOutcome(
                    "blocked_by_question",
                    f"Review screen does not show expected resume: {expected_resume}",
                    [f"Expected resume: {expected_resume}"],
                    exit_code=13,
                )
            # Avoid side-effect checkboxes such as follow company when possible.
            try:
                page.evaluate(
                    """() => [...document.querySelectorAll('input[type=checkbox]')].forEach(cb => { if (cb.checked) { cb.click(); } })"""
                )
            except Exception:
                pass
            if click_if_visible(page, r"submit application|отправить заявку", timeout=3000):
                page.wait_for_timeout(2500)
                if re.search(r"application submitted|your application was sent", body_text(page), re.I):
                    return ApplyOutcome("applied/completed", "LinkedIn Easy Apply submitted successfully. Page confirmed submission.")
                return ApplyOutcome("blocked_by_question", "Submit clicked but confirmation was not detected.", [body_text(page)[:800]], exit_code=14)
        # Nothing actionable.
        return ApplyOutcome("blocked_by_question", "Could not find next/review/submit action in Easy Apply flow.", [body_text(page)[:1200]], exit_code=14)

    remaining = visible_required_questions(page)
    if remaining:
        return ApplyOutcome("blocked_by_question", "Unresolved required LinkedIn question(s) after step limit.", remaining, exit_code=10)
    return ApplyOutcome("blocked_by_question", "Easy Apply flow exceeded step limit.", [body_text(page)[:1200]], exit_code=14)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-jobs", type=int, default=0, help="How many pending/blocked jobs to attempt this run; 0 means all.")
    parser.add_argument("--retry-blocked", action="store_true", help="Also retry records currently blocked_by_question.")
    parser.add_argument("--stop-on-blocker", action="store_true", help="Stop at the first non-critical blocker instead of marking it and continuing.")
    parser.add_argument("--max-submissions", type=int, default=0, help="Stop after this many new Easy Apply submissions in this worker run; 0 means unlimited.")
    args = parser.parse_args()

    jobs = parse_jobs()
    data = load_progress(jobs)
    qa = load_qa()
    save_progress(data)

    candidates: Iterable[dict[str, Any]] = data.get("records", [])
    if args.retry_blocked:
        candidates = [r for r in candidates if r.get("status") in ("pending", "blocked_by_question")]
    else:
        candidates = [r for r in candidates if r.get("status", "pending") not in TERMINAL_STATUSES and r.get("status") != "blocked_by_question"]

    candidates = list(candidates)
    if args.max_jobs > 0:
        candidates = candidates[: args.max_jobs]
    if not candidates:
        emit("done", message="No pending jobs to process.")
        return 0

    with sync_playwright() as p:
        try:
            lease_cm = central_page(p)
            lease = lease_cm.__enter__()
        except Exception as exc:
            emit("login_required", code=11, message=f"Could not attach to central LinkedIn browser: {exc!r}")
            return 11
        try:
            page = lease.page
            max_exit = 0
            submitted_count = 0
            for rec in candidates:
                human_delay(1.0)
                emit("processing", url=rec.get("url"), company=rec.get("company"), title=rec.get("title"), geo=rec.get("geo"))
                try:
                    outcome = process_record(page, rec, qa)
                except PlaywrightTimeoutError as exc:
                    outcome = ApplyOutcome("blocked_by_question", f"Playwright timeout: {exc}", [repr(exc)], exit_code=14)
                except Exception as exc:
                    outcome = ApplyOutcome("blocked_by_question", f"Unexpected worker error: {exc!r}", [repr(exc)], exit_code=14)
                mark_record(data, rec, outcome)
                emit(
                    "outcome",
                    url=rec.get("url"),
                    status=outcome.status,
                    notes=outcome.notes,
                    questions=outcome.questions or [],
                    code=outcome.exit_code,
                )
                if outcome.status == "applied/completed" and "submitted successfully" in (outcome.notes or "").lower():
                    submitted_count += 1
                    if args.max_submissions > 0 and submitted_count >= args.max_submissions:
                        break
                if outcome.exit_code:
                    # Login/security/rate-limit challenges are global blockers. Return the
                    # exact global-block code so wrapper/cron reports the real stop reason.
                    if outcome.exit_code in (11, 12):
                        max_exit = outcome.exit_code
                        break
                    if args.stop_on_blocker:
                        max_exit = max(max_exit, outcome.exit_code)
                        break
                    # Without --stop-on-blocker, ordinary per-job form blockers are
                    # recorded on the job and the batch continues; they must not make
                    # a quota-seeking daily run fail or stop after one vacancy.
                    continue
            return max_exit
        finally:
            lease_cm.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
