#!/usr/bin/env python3
import browser_cookie3, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
OUT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_chrome_cookie_state.json')
REPORT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_cookie_login_check.txt')

def to_pw_cookie(c):
    d={'name':c.name,'value':c.value,'domain':c.domain,'path':c.path or '/'}
    if c.expires: d['expires']=float(c.expires)
    # Playwright wants sameSite one of Strict/Lax/None; browser_cookie3 often lacks it
    d['sameSite']='Lax'
    d['secure']=bool(getattr(c,'secure',False))
    d['httpOnly']=False
    return d
cookies=[]
for profile in [None, 'Default', 'Profile 1', 'Profile 2', 'Profile 3']:
    try:
        jar=browser_cookie3.chrome(domain_name='.linkedin.com') if profile is None else browser_cookie3.chrome(domain_name='.linkedin.com', chrome_profile=profile)
        got=[to_pw_cookie(c) for c in jar]
        if got:
            cookies.extend(got)
            print('profile',profile,'cookies',len(got))
    except Exception as e:
        print('profile',profile,'err',repr(e))
# dedupe prefer last
by={(c['domain'],c['path'],c['name']):c for c in cookies}
cookies=list(by.values())
state={'cookies':cookies,'origins':[]}
OUT.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=False)
    ctx=browser.new_context(storage_state=str(OUT), viewport={'width':1500,'height':1000})
    page=ctx.new_page()
    page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    txt=page.locator('body').inner_text(timeout=10000)
    result=f'url={page.url}\ntitle={page.title()}\ntext_head={txt[:1000]}\nli_at={any(c["name"]=="li_at" for c in cookies)}\ncookie_count={len(cookies)}\n'
    REPORT.write_text(result,encoding='utf-8')
    print(result)
    browser.close()
