#!/usr/bin/env python3
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
URL='https://hirehi.ru/devops/devops-inzhener-65256'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=False, slow_mo=100)
    ctx=browser.new_context(viewport={'width':1500,'height':1000})
    page=ctx.new_page(); page.goto(URL, wait_until='domcontentloaded'); page.wait_for_timeout(3000)
    print('before pages', [p.url for p in ctx.pages])
    loc=page.locator("a:has-text('LinkedIn')").first
    print('count', loc.count(), 'visible', loc.is_visible())
    try:
        with ctx.expect_page(timeout=10000) as ep:
            loc.click(timeout=5000)
        newp=ep.value; newp.wait_for_load_state('domcontentloaded', timeout=60000); newp.wait_for_timeout(5000)
        print('popup', newp.url, newp.title())
    except Exception as e:
        print('no popup', repr(e))
        page.wait_for_timeout(5000)
        print('after pages', [p.url for p in ctx.pages])
        print('page url', page.url)
    browser.close()
