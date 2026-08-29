#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, time, urllib.request, urllib.error, html
from pathlib import Path
import browser_cookie3
from playwright.sync_api import sync_playwright

BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
OUT=BASE/'output'
TG_CSV=OUT/'telegram_candidates.csv'
SENT_STATE=OUT/'telegram_send_state.json'
PRE=OUT/'telegram_preflight_report.json'
STORAGE=OUT/'telegram-web-storage-state.json'
PROFILE=OUT/'telegram-web-profile'


def norm_url(u):
    import urllib.parse
    u=u.replace('/devops/devops/sre-', '/devops/devops-sre-')
    pr=urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((pr.scheme,pr.netloc,urllib.parse.quote(urllib.parse.unquote(pr.path),safe='/'),pr.query,pr.fragment))

def opener_token():
    jar=browser_cookie3.chrome(domain_name='hirehi.ru')
    token=next((c.value for c in jar if c.name=='sb-access-token'), None)
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), token

def req(opener, url, data=None, headers=None):
    h={'User-Agent':'Mozilla/5.0', **(headers or {})}
    return opener.open(urllib.request.Request(url,data=data,headers=h),timeout=30).read().decode('utf-8','ignore')

def ticket_from_html(text):
    m=re.search(r'<script[^>]+id="vacancy-data-json"[^>]*>\s*(\{.*?\})\s*</script>',text,re.S)
    if not m: return None
    try: return json.loads(html.unescape(m.group(1))).get('contact_ticket')
    except Exception: return None

def consume(row, opener, token):
    out={'destination':'','status':'','error':'','telegram_handle':'','telegram_url':''}
    try:
        url=norm_url(row['url'])
        text=req(opener,url,headers={'Authorization':'Bearer '+token})
        ticket=ticket_from_html(text)
        payload={'type':'direct_contact','job_id':int(row['id'])}
        if ticket: payload['contact_ticket']=ticket
        raw=req(opener,'https://hirehi.ru/api/limits/consume',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
        js=json.loads(raw); out['status']='allowed' if js.get('allowed') else 'not_allowed'; out['destination']=js.get('open_url') or ''
        m=re.search(r'(?:https?://)?t\.me/([^/?#]+)',out['destination'])
        if m:
            out['telegram_handle']=m.group(1); out['telegram_url']='https://t.me/'+m.group(1)
        out['raw']=js
    except urllib.error.HTTPError as e:
        out['status']=f'http_{e.code}'
        try: out['error']=e.read().decode('utf-8','ignore')[:500]
        except Exception: out['error']=str(e)
    except Exception as e:
        out['status']='error'; out['error']=repr(e)[:500]
    return out

def check_tg_auth():
    res={'storage_exists':STORAGE.exists(),'profile_exists':PROFILE.exists(),'authenticated':False,'url':'','head':'','blocker':''}
    with sync_playwright() as p:
        ctx=p.chromium.launch_persistent_context(str(PROFILE), headless=False, viewport={'width':1280,'height':900})
        page=ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto('https://web.telegram.org/a/', wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(5000)
        res['url']=page.url
        try: body=page.locator('body').inner_text(timeout=10000)
        except Exception: body=''
        res['head']=body[:1000]
        # authenticated if search/chat list/message UI visible or no login phone/QR prompts
        if re.search(r'Log in by phone|Phone number|QR|Scan from mobile|Введите номер|Номер телефона', body, re.I):
            res['blocker']='telegram_login_required'
        elif re.search(r'Search|Saved Messages|Archived Chats|Chats|New Message|Menu', body, re.I):
            res['authenticated']=True
            ctx.storage_state(path=str(STORAGE))
        else:
            res['blocker']='telegram_unknown_ui_state'
        ctx.close()
    return res

def main():
    rows=list(csv.DictReader(open(TG_CSV,encoding='utf-8')))
    sent={}
    if SENT_STATE.exists():
        try: sent=json.loads(SENT_STATE.read_text(encoding='utf-8'))
        except Exception: sent={}
    sent_ids=set(sent.get('sent_ids',[])) | {r.get('id') for r in sent.get('results',[]) if r.get('status')=='sent'}
    opener,token=opener_token()
    contacts=[]
    for i,r in enumerate(rows,1):
        c=consume(r,opener,token) if token else {'status':'no_hirehi_token','destination':'','error':'no sb-access-token','telegram_handle':'','telegram_url':''}
        contacts.append({**r, **c, 'already_sent_local': r['id'] in sent_ids})
        if i%20==0 or i==len(rows): print(f'resolved {i}/{len(rows)}', flush=True)
    auth=check_tg_auth()
    summary={
        'telegram_candidates_total':len(rows),
        'sent_local_count':len(sent_ids),
        'not_sent_local_count':sum(1 for c in contacts if not c['already_sent_local']),
        'resolved_allowed':sum(c.get('status')=='allowed' for c in contacts),
        'resolved_tme':sum(bool(c.get('telegram_url')) for c in contacts),
        'unique_tg_handles':len({c.get('telegram_handle') for c in contacts if c.get('telegram_handle')}),
        'telegram_auth':auth,
        'can_send': auth.get('authenticated') and sum(bool(c.get('telegram_url')) and not c['already_sent_local'] for c in contacts)>0,
        'blocker': ''
    }
    if not auth.get('authenticated'):
        summary['blocker']=auth.get('blocker') or 'telegram_not_authenticated'
    elif not summary['resolved_tme']:
        summary['blocker']='no_resolved_telegram_urls_from_hirehi'
    PRE.write_text(json.dumps({'summary':summary,'contacts':contacts},ensure_ascii=False,indent=2),encoding='utf-8')
    # CSV for queue
    with (OUT/'telegram_contacts_queue.csv').open('w',encoding='utf-8',newline='') as f:
        fields=['id','title','company','level','format','url','destination','telegram_url','telegram_handle','status','already_sent_local','error']
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(contacts)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
