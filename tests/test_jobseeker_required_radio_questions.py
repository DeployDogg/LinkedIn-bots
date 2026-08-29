"""Focused TDD tests for JobSeeker required duplicated radio questions.

No Docker, no browser startup, no LinkedIn/network calls. Playwright is faked.
"""

from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
JOBSEEKER_SCRIPTS = ROOT / "services" / "JobSeeker" / "scripts"
if str(JOBSEEKER_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(JOBSEEKER_SCRIPTS))


def import_worker():
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    fake_sync_api.TimeoutError = TimeoutError
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
        sys.modules.pop("linkedin_worker", None)
        return importlib.import_module("linkedin_worker")


class FakeDuplicatedRadioPage:
    """DOM-like fake for two required radio groups whose option labels duplicate.

    The visible option labels are both Yes/No. The required question labels are the
    real LinkedIn texts; selecting by option label alone is ambiguous unless the
    worker scopes the radio by group/question text.
    """

    def __init__(self) -> None:
        self.groups = {
            "Are you located in the US?": None,
            "Will you now or in the future require sponsorship from any employer?": None,
        }
        self.next_clicked = False

    def evaluate(self, script: str, arg=None):
        if "function radioGroupQuestion" in script and "radioGroups" in script:
            return [
                {"label": label, "tag": "input", "type": "radio", "name": f"radio-{i}", "options": ["Yes", "No"]}
                for i, (label, selected) in enumerate(self.groups.items(), start=1)
                if selected is None
            ]
        if "selectRequiredRadioByQuestion" in script:
            needle = (arg or {}).get("needle", "").lower()
            value = (arg or {}).get("value")
            for label in self.groups:
                if needle[:60] in label.lower():
                    if value not in {"Yes", "No"}:
                        return False
                    self.groups[label] = value
                    return True
            return False
        if "enabledActionButtons" in script:
            return all(v is not None for v in self.groups.values())
        return False


class JobSeekerRequiredRadioQuestionsTest(unittest.TestCase):
    def test_qa_match_has_precise_located_in_us_no_separate_from_work_authorization(self) -> None:
        worker = import_worker()
        qa = worker.load_qa()

        answer, source = worker.qa_match("Are you located in the US?", qa, required=True)
        auth_answer, auth_source = worker.qa_match("Are you legally authorized to work in the United States?", qa, required=True)
        sponsorship_answer, sponsorship_source = worker.qa_match(
            "Will you now or in the future require sponsorship from any employer?", qa, required=True
        )

        self.assertEqual(("No", "located_in_us_no"), (answer, source))
        self.assertEqual(("No", "work_auth_country"), (auth_answer, auth_source))
        self.assertEqual("Yes", sponsorship_answer)
        self.assertIn(sponsorship_source, {"visa_sponsorship", "us_sponsorship_required"})

    def test_duplicated_required_radio_groups_select_no_for_location_and_yes_for_sponsorship(self) -> None:
        worker = import_worker()
        page = FakeDuplicatedRadioPage()
        qa = worker.load_qa()

        ok, unresolved = worker.fill_known_required_fields(page, qa)

        self.assertTrue(ok, unresolved)
        self.assertEqual("No", page.groups["Are you located in the US?"])
        self.assertEqual("Yes", page.groups["Will you now or in the future require sponsorship from any employer?"])

    def test_step_can_advance_after_both_required_radio_groups_are_answered(self) -> None:
        worker = import_worker()
        page = FakeDuplicatedRadioPage()
        qa = worker.load_qa()

        ok, unresolved = worker.fill_known_required_fields(page, qa)
        can_advance = page.evaluate("() => { const enabledActionButtons = true; return enabledActionButtons; }")

        self.assertTrue(ok, unresolved)
        self.assertTrue(can_advance)

    def test_unknown_required_question_still_stops_unresolved(self) -> None:
        worker = import_worker()
        qa = worker.load_qa()
        answer, source = worker.qa_match("What is your favorite production-only secret passphrase?", qa, required=True)
        self.assertEqual((None, None), (answer, source))


if __name__ == "__main__":
    unittest.main()
