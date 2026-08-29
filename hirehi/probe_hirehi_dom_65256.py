#!/usr/bin/env python3
import json, re
from pathlib import Path
from playwright.sync_api import sync_playwright
TOKEN=json.loads(Path('/Users/deploydog-ai/LinkedIn/hirehi/output/session.json').read_text())['access_token']
URL='https://hirehi.ru/devops/devops-inzhener-65256'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=False, slow_mo=100)
    ctx=browser.new_context(viewport={'width':1500,'height':1000})
    page=ctx.new_page()
    page.add_init_script(f"""(() => {{
      localStorage.setItem('hirehi_auth_state', JSON.stringify({{'access_token': {json.dumps(TOKEN)}}}));
      localStorage.setItem('access_token', {json.dumps(TOKEN)});
      localStorage.setItem('token', {json.dumps(TOKEN)});
    }})()""")
    page.goto(URL, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    txt=page.locator('body').inner_text(timeout=10000)
    print('url',page.url,'title',page.title())
    print(txt[:2000])
    print('buttons/links')
    print(page.evaluate("""() => [...document.querySelectorAll('a,button,[role=button]')].map(e=>{const r=e.getBoundingClientRect(); return {tag:e.tagName,text:(e.innerText||'').trim(),aria:e.getAttribute('aria-label')||'',href:e.href||'',x:r.x,y:r.y,w:r.width,h:r.height,visible:r.width>0&&r.height>0}}).filter(x=>x.visible).slice(0,120)"""))
    page.screenshot(path='/Users/deploydog-ai/LinkedIn/hirehi/output/hirehi_65256_dom_probe.png', full_page=False)
    browser.close()
