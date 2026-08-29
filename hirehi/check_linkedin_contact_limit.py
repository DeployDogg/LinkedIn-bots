#!/usr/bin/env python3
import json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
TOKEN=json.loads(Path('/Users/deploydog-ai/LinkedIn/hirehi/output/session.json').read_text()).get('access_token')
URL='https://hirehi.ru/devops/devops-engineer-66506'
SHOT='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_contact_limit_check.png'
TXT='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_contact_limit_check.txt'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=False, slow_mo=150)
    ctx=browser.new_context(viewport={'width':1500,'height':1000}, locale='ru-RU')
    page=ctx.new_page()
    payload=json.dumps({'token':TOKEN,'auth_state':{'logged_in':True,'expires':int((time.time()+86400*7)*1000)}})
    page.add_init_script(f"""(() => {{ const data={payload}; localStorage.setItem('hirehi_auth_state', JSON.stringify(data.auth_state)); localStorage.setItem('access_token', data.token||''); localStorage.setItem('hirehi_access_token', data.token||''); localStorage.setItem('token', data.token||''); }})()""")
    ctx.add_cookies([{'name':'access_token','value':TOKEN or '', 'domain':'hirehi.ru','path':'/','httpOnly':False,'secure':True},{'name':'hirehi_access_token','value':TOKEN or '', 'domain':'hirehi.ru','path':'/','httpOnly':False,'secure':True}])
    page.goto(URL, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(2000)
    before=page.locator('body').inner_text(timeout=5000)
    # click desktop LinkedIn channel button
    btn=page.locator('a.sidebar-apply-btn--desktop').first
    clicked=False
    try:
        btn.click(timeout=5000)
        clicked=True
    except Exception as e:
        print('click_error',repr(e))
    page.wait_for_timeout(2500)
    after=page.locator('body').inner_text(timeout=5000)
    page.screenshot(path=SHOT, full_page=False)
    Path(TXT).write_text('CLICKED='+str(clicked)+'\nURL='+page.url+'\n\nBEFORE:\n'+before[:4000]+'\n\nAFTER:\n'+after[:6000], encoding='utf-8')
    print('clicked',clicked,'url',page.url,'shot',SHOT,'txt',TXT)
    print('AFTER_SNIP')
    print(after[:1500])
    browser.close()
