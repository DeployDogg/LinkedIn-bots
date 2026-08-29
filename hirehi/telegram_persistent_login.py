#!/usr/bin/env python3
import asyncio
import os
import re
import select
import sys
from pathlib import Path

from playwright.async_api import async_playwright

PHONE = os.environ["TG_PHONE"]
PROFILE_DIR = Path(os.environ.get("TG_PROFILE_DIR", "/Users/deploydog-ai/LinkedIn/hirehi/output/telegram-web-profile"))
STORAGE_STATE = Path(os.environ.get("TG_STORAGE_STATE", "/Users/deploydog-ai/LinkedIn/hirehi/output/telegram-web-storage-state.json"))


async def visible(locator, timeout=2000):
    try:
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def main():
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--start-maximized"],
        )
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://web.telegram.org/a/", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2500)

        if await visible(page.get_by_role("textbox", name="Search"), 3000):
            await context.storage_state(path=str(STORAGE_STATE))
            print("LOGIN_OK already_authenticated", flush=True)
        else:
            phone_login = page.get_by_role("button", name=re.compile("LOG IN BY PHONE NUMBER", re.I))
            if await visible(phone_login, 5000):
                await phone_login.click()

            phone = page.get_by_role("textbox", name=re.compile("phone|number|номер", re.I))
            try:
                await phone.wait_for(state="visible", timeout=8000)
            except Exception:
                # Telegram Web A/K change labels/locales often. Fall back to the
                # visible input whose value starts with a country prefix.
                phone = page.locator('input[type="tel"], input.input-field-input, input').last
                await phone.wait_for(state="visible", timeout=10000)
            await phone.fill(PHONE)

            next_button = page.get_by_role("button", name=re.compile("NEXT|ДАЛЕЕ|далее", re.I))
            await next_button.wait_for(state="visible", timeout=10000)
            await next_button.click()

            code = page.get_by_role("textbox", name=re.compile("Code|код", re.I))
            try:
                await code.wait_for(state="visible", timeout=20000)
            except Exception:
                code = page.locator('input[type="tel"], input.input-field-input, input').last
                await code.wait_for(state="visible", timeout=10000)
            print("WAITING_FOR_CODE visible_browser_ready", flush=True)
            print("TYPE_CODE_IN_BROWSER_OR_SEND_TO_PROCESS", flush=True)

            submitted = False
            while True:
                if await visible(page.get_by_role("textbox", name="Search"), 500):
                    await context.storage_state(path=str(STORAGE_STATE))
                    print(f"LOGIN_OK storage_state={STORAGE_STATE}", flush=True)
                    break

                if not submitted and select.select([sys.stdin], [], [], 0)[0]:
                    line = sys.stdin.readline()
                    if not line:
                        await asyncio.sleep(0.5)
                        continue
                    value = line.strip()
                    if value.lower() in {"quit", "exit"}:
                        await context.close()
                        return
                    if re.fullmatch(r"\d{5,6}", value):
                        await code.fill(value)
                        await code.press("Enter")
                        submitted = True
                        print("CODE_SUBMITTED", flush=True)
                    else:
                        print("IGNORED_INPUT expected_5_or_6_digits", flush=True)

                body_text = await page.locator("body").inner_text()
                if "Invalid code" in body_text or "Code invalid" in body_text:
                    print("CODE_INVALID", flush=True)
                    submitted = False
                    await code.fill("")

                await asyncio.sleep(0.5)

        print("BROWSER_STAYS_OPEN; enter quit in process stdin to close", flush=True)
        while True:
            await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
