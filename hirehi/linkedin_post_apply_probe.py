#!/usr/bin/env python3
from pathlib import Path
from playwright.sync_api import sync_playwright
STATE='/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_session.json'
URL='https://www.linkedin.com/feed/update/urn:li:share:7485275227275550720/'
OUT='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_post_65256_probe.txt'
SHOT='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_post_65256_probe.png'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=False, slow_mo=100)
    ctx=browser.new_context(storage_state=STATE, viewport={'width':1500,'height':1000})
    page=ctx.new_page()
    page.goto(URL, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    txt=page.locator('body').inner_text(timeout=10000)
    links=page.evaluate("""() => [...document.querySelectorAll('a[href]')].map(a=>({text:(a.innerText||'').trim(), href:a.href, aria:a.getAttribute('aria-label')||''})).filter(x=>x.href.includes('/in/') || x.text.includes('Tatyana') || x.aria.includes('Tatyana')).slice(0,50)""")
    buttons=page.evaluate("""() => [...document.querySelectorAll('button,a')].map(e=>({tag:e.tagName,text:(e.innerText||'').trim(), aria:e.getAttribute('aria-label')||'', href:e.href||''})).filter(x=>/Message|Comment|Like|Apply|Connect|Tatyana|Send|Отклик|Написать/i.test(x.text+' '+x.aria+' '+x.href)).slice(0,80)""")
    page.screenshot(path=SHOT, full_page=False)
    Path(OUT).write_text('URL='+page.url+'\nTITLE='+page.title()+'\nTEXT_HEAD:\n'+txt[:4000]+'\n\nLINKS:\n'+repr(links)+'\n\nBUTTONS:\n'+repr(buttons), encoding='utf-8')
    print('url',page.url,'title',page.title(),'out',OUT,'shot',SHOT)
    print('login?', 'Sign in' not in txt[:1000] and 'Join now' not in txt[:1000])
    print('links',links[:10])
    print('buttons',buttons[:20])
    browser.close()
