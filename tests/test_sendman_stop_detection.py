"""Focused TDD tests for SendMan blocker detection.

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
SENDMAN_SCRIPTS = ROOT / "services" / "SendMan" / "scripts"
if str(SENDMAN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SENDMAN_SCRIPTS))


class FakeElement:
    def __init__(self, visible: bool = True) -> None:
        self._visible = visible

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible


class FakeLocator:
    def __init__(self, elements: list[FakeElement] | None = None, *, text: str = "") -> None:
        self._elements = elements or []
        self._text = text

    def inner_text(self, timeout: int = 0) -> str:
        return self._text

    def count(self) -> int:
        return len(self._elements)

    @property
    def first(self) -> FakeElement:
        return self._elements[0]


class FakePage:
    def __init__(
        self,
        *,
        body_text: str = "LinkedIn profile",
        url: str = "https://www.linkedin.com/in/ordinary-person/",
        visible_selectors: set[str] | None = None,
    ) -> None:
        self.body_text = body_text
        self.url = url
        self.visible_selectors = visible_selectors or set()

    def locator(self, selector: str):
        if selector == "body":
            return FakeLocator(text=self.body_text)
        if selector in self.visible_selectors:
            return FakeLocator([FakeElement(True)])
        return FakeLocator([])


def import_sendman(stop_patterns: list[str] | None = None):
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = lambda: None
    fake_sync_api.TimeoutError = TimeoutError
    fake_playwright = types.ModuleType("playwright")
    fake_playwright.sync_api = fake_sync_api
    env = {}
    if stop_patterns is not None:
        env["LINKEDIN_STOP_PATTERNS_JSON"] = __import__("json").dumps(stop_patterns)
    with patch.dict(sys.modules, {"playwright": fake_playwright, "playwright.sync_api": fake_sync_api}):
        with patch.dict("os.environ", env, clear=False):
            sys.modules.pop("linkedin_message_outreach", None)
            return importlib.import_module("linkedin_message_outreach")


class SendManStopDetectionTest(unittest.TestCase):
    AMBIGUOUS_ENV_PATTERNS = [
        "captcha",
        "challenge",
        "checkpoint",
        "security",
        "safeguard",
        "security verification",
        "verify your identity",
        "temporarily restricted",
        "you have reached the limit",
    ]

    def test_ordinary_profile_body_with_ambiguous_security_terms_does_not_stop(self) -> None:
        runtime = import_sendman(self.AMBIGUOUS_ENV_PATTERNS)
        page = FakePage(
            body_text=(
                "Jane Doe is a cyber security engineer. "
                "She helps safeguard critical infrastructure and leads checkpoint reviews. "
                "Her team runs captcha-resistant observability challenge exercises."
            )
        )

        self.assertIsNone(runtime.detect_stop(page))

    def test_checkpoint_challenge_url_stops_fail_closed(self) -> None:
        runtime = import_sendman(self.AMBIGUOUS_ENV_PATTERNS)
        page = FakePage(url="https://www.linkedin.com/checkpoint/challenge/abc123")

        self.assertEqual("url:checkpoint_challenge:https://www.linkedin.com/checkpoint/challenge/abc123", runtime.detect_stop(page))

    def test_visible_password_captcha_and_checkpoint_controls_stop(self) -> None:
        runtime = import_sendman(self.AMBIGUOUS_ENV_PATTERNS)
        cases = [
            ("input[type='password']", "selector:input[type='password']"),
            ("iframe[src*='captcha' i]", "selector:iframe[src*='captcha' i]"),
            ("form[action*='checkpoint' i]", "selector:form[action*='checkpoint' i]"),
        ]
        for selector, expected in cases:
            with self.subTest(selector=selector):
                self.assertEqual(expected, runtime.detect_stop(FakePage(visible_selectors={selector})))

    def test_explicit_body_blocker_phrases_stop(self) -> None:
        runtime = import_sendman(self.AMBIGUOUS_ENV_PATTERNS)
        cases = [
            ("Security verification is required to continue", "security verification"),
            ("Please verify your identity to keep your account safe", "verify your identity"),
            ("Your account is temporarily restricted", "temporarily restricted"),
            ("You have reached the limit for invitations", "you have reached the limit"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, runtime.detect_stop(FakePage(body_text=text)))


if __name__ == "__main__":
    unittest.main()
