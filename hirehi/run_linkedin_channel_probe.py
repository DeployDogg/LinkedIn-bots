#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, time
from pathlib import Path
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

ROOT = Path('/Users/deploydog-ai/LinkedIn/hirehi')
OUT = ROOT/'output'
CAND = OUT/'linkedin_candidates.csv'
SESSION = OUT/'session.json'
REPORT = OUT/'linkedin_channel_probe_report.json'

TOKEN = json.loads(SESSION.read_text(encoding='utf-8')).get('access_token')
rows = list(csv.DictReader(CAND.open(encoding='utf-8')))
allowed = []
for r in rows:
    fmt=(r.get('format') or '').lower()
    # user's policy: remote ok; skip office/hybrid unless relocation explicitly requested; for this run keep remote only
    if 'удал' in fmt and 'рф' not in fmt:  # prefer truly remote, not Russia-only for Андрей abroad
        allowed.append(r)
    elif 'удал' in fmt:
        allowed.append(r)  # still probe, may be acceptable later

def visible_text(page):
    try:
        return page.locator('body').inner_text(timeout=3000)
    except Exception:
        return ''

results=[]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=120)
    context = browser.new_context(viewport={'width': 1500, 'height': 1000}, locale='ru-RU')
    page = context.new_page()
    # seed auth in every new document; use both forms likely used by app
    auth_state = {'logged_in': True, 'expires': int((time.time()+86400*7)*1000)}
    init_payload = json.dumps({'auth_state': auth_state, 'token': TOKEN})
    page.add_init_script(f"""(() => {{
        const data = {init_payload};
        localStorage.setItem('hirehi_auth_state', JSON.stringify(data.auth_state));
        localStorage.setItem('access_token', data.token || '');
        localStorage.setItem('hirehi_access_token', data.token || '');
        localStorage.setItem('token', data.token || '');
    }})()""")
    context.add_cookies([
        {'name':'access_token','value':TOKEN or '', 'domain':'hirehi.ru','path':'/','httpOnly':False,'secure':True},
        {'name':'hirehi_access_token','value':TOKEN or '', 'domain':'hirehi.ru','path':'/','httpOnly':False,'secure':True},
    ])
    for r in allowed:
        item={k:r.get(k,'') for k in ['id','title','company','level','format','url']}
        try:
            page.goto(r['url'], wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2000)
            txt = visible_text(page)
            item['page_title']=page.title()
            item['current_url']=page.url
            item['logged_in'] = ('Войти' not in txt[:2000]) or ('Что нового' in txt)
            item['has_apply'] = 'Откликнуться' in txt
            item['has_linkedin_text'] = 'LinkedIn' in txt
            # inspect links/buttons from DOM
            dom = page.evaluate("""() => ({
              links:[...document.querySelectorAll('a[href]')].map(a=>({text:(a.innerText||'').trim(), href:a.href, cls:String(a.className||'')})),
              buttons:[...document.querySelectorAll('button,a')].map(e=>({tag:e.tagName,text:(e.innerText||'').trim(), href:e.href||'', cls:String(e.className||''), aria:e.getAttribute('aria-label')||''}))
            })""")
            li=[x for x in dom['links'] if 'linkedin.com' in x['href'].lower() and 'company/107994980' not in x['href']]
            item['linkedin_links']=li[:10]
            candidates=[x for x in dom['buttons'] if re.search(r'Отклик|LinkedIn|контакт|contact|apply', (x.get('text','')+' '+x.get('href','')+' '+x.get('cls','')+' '+x.get('aria','')), re.I)]
            item['controls']=candidates[:20]
            # do NOT click send here; user asked run, but first this probe identifies the exact safe control/contact.
            item['status']='probed'
        except Exception as e:
            item['status']='error'; item['error']=repr(e)
        results.append(item)
    REPORT.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'probed={len(results)} report={REPORT}')
    for it in results:
        print(it['id'], it['company'], it.get('format'), 'li_links', len(it.get('linkedin_links',[])), 'apply', it.get('has_apply'), 'linkedin_text', it.get('has_linkedin_text'), 'status', it.get('status'))
    browser.close()
