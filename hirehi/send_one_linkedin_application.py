#!/usr/bin/env python3
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
PROFILE=Path('/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_chromium_profile')
PROFILE_URL='https://www.linkedin.com/in/tatyana-l-8a82b7221/'
MESSAGE='''Татьяна, добрый день! Увидел ваш пост про Senior DevOps Engineer (bare metal / Linux / Kubernetes / Terraform / CI/CD). Мне релевантно: Senior DevOps Engineer, Cloud/CI/CD/IaC, Kubernetes/Terraform, automation, production infrastructure. Рассматриваю remote/relocation. Буду рад обсудить вакансию и отправить CV.'''
OUT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/send_one_65256_result.txt')
SHOT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/send_one_65256_result.png')

def find_member(page):
    # LinkedIn often embeds member ids / urns in JSON scripts
    html=page.content()
    import re
    urns=re.findall(r'urn:li:fsd_profile:([A-Za-z0-9_-]+)|urn:li:member:(\d+)|profileUrn":"urn:li:fsd_profile:([A-Za-z0-9_-]+)', html)
    return urns[:20]

with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context(str(PROFILE), headless=False, viewport={'width':1500,'height':1000}, args=['--disable-blink-features=AutomationControlled'])
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(PROFILE_URL, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    body=page.locator('body').inner_text(timeout=10000)
    status=[]
    status.append('url='+page.url)
    status.append('title='+page.title())
    status.append('logged_in='+str(('Sign in' not in body[:2000] and 'Join now' not in body[:2000] and 'Войти' not in body[:1000])))
    status.append('body_head='+body[:1000].replace('\n',' | '))
    status.append('member_candidates='+repr(find_member(page)))
    # Try normal Message button in current profile main area
    clicked=False
    selectors=["main button[aria-label*='Message']", "main a[aria-label*='Message']", "main button:has-text('Message')", "main a:has-text('Message')", "button[aria-label*='Message']", "button:has-text('Message')"]
    for sel in selectors:
        try:
            loc=page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=2000):
                loc.click(timeout=5000)
                clicked=True
                status.append('clicked='+sel)
                break
        except Exception as e:
            status.append('click_fail '+sel+' '+repr(e)[:160])
    page.wait_for_timeout(3000)
    # Find editor
    editors=page.locator("[contenteditable='true']")
    status.append('editors_count='+str(editors.count()))
    sent=False
    if editors.count():
        ed=editors.last
        ed.click(timeout=5000)
        ed.fill(MESSAGE)
        page.keyboard.press('Control+Enter')
        page.wait_for_timeout(5000)
        txt=page.locator('body').inner_text(timeout=10000)
        sent = 'Увидел ваш пост про Senior DevOps Engineer' in txt or 'Senior DevOps Engineer (bare metal' in txt
        status.append('sent_verified='+str(sent))
    else:
        status.append('no_editor_after_click')
    page.screenshot(path=str(SHOT), full_page=False)
    OUT.write_text('\n'.join(status), encoding='utf-8')
    print('\n'.join(status))
    print('SHOT='+str(SHOT))
    ctx.close()
