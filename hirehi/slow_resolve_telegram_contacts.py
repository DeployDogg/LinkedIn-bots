#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, time, random, urllib.request, urllib.error, html, urllib.parse
from pathlib import Path
from datetime import datetime
import browser_cookie3

BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
OUT=BASE/'output'
TG_CSV=OUT/'telegram_candidates.csv'
STATE=OUT/'telegram_contacts_slow_state.json'
CSV_OUT=OUT/'telegram_contacts_all.csv'
MD_OUT=OUT/'telegram_contacts_all.md'
JSON_OUT=OUT/'telegram_contacts_all.json'
UA='Mozilla/5.0'
DELAY_MIN=10.0
DELAY_JITTER=3.0
MAX_RETRIES=1


def now(): return datetime.now().isoformat(timespec='seconds')

def norm_url(u):
    u=u.replace('/devops/devops/sre-', '/devops/devops-sre-')
    pr=urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((pr.scheme,pr.netloc,urllib.parse.quote(urllib.parse.unquote(pr.path),safe='/'),pr.query,pr.fragment))

def opener_token():
    jar=browser_cookie3.chrome(domain_name='hirehi.ru')
    token=next((c.value for c in jar if c.name=='sb-access-token'), None)
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, token

def req(opener, url, data=None, headers=None, timeout=30):
    h={'User-Agent':UA, **(headers or {})}
    return opener.open(urllib.request.Request(url,data=data,headers=h),timeout=timeout).read().decode('utf-8','ignore')

def ticket_from_html(text):
    m=re.search(r'<script[^>]+id="vacancy-data-json"[^>]*>\s*(\{.*?\})\s*</script>',text,re.S)
    if not m: return None
    try: return json.loads(html.unescape(m.group(1))).get('contact_ticket')
    except Exception: return None

def extract_tg(dest):
    m=re.search(r'(?:https?://)?t\.me/([^/?#]+)', dest or '')
    if not m: return ('','')
    h=m.group(1)
    return h, 'https://t.me/'+h

def consume(row, opener, token):
    out={'destination':'','status':'','error':'','telegram_handle':'','telegram_url':'','attempts':0,'resolved_at':''}
    url=norm_url(row['url'])
    for attempt in range(1,MAX_RETRIES+1):
        out['attempts']=attempt
        try:
            text=req(opener,url,headers={'Authorization':'Bearer '+token} if token else {})
            ticket=ticket_from_html(text)
            payload={'type':'direct_contact','job_id':int(row['id'])}
            if ticket: payload['contact_ticket']=ticket
            raw=req(opener,'https://hirehi.ru/api/limits/consume',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
            js=json.loads(raw)
            out['status']='allowed' if js.get('allowed') else 'not_allowed'
            out['destination']=js.get('open_url') or ''
            out['telegram_handle'],out['telegram_url']=extract_tg(out['destination'])
            out['raw']=js
            out['resolved_at']=now()
            return out
        except urllib.error.HTTPError as e:
            out['status']=f'http_{e.code}'
            try: out['error']=e.read().decode('utf-8','ignore')[:500]
            except Exception: out['error']=str(e)
            if e.code==429 and attempt<MAX_RETRIES:
                sleep_s=DELAY_MIN+random.random()*DELAY_JITTER
                print(f"{now()} retry_429 id={row['id']} attempt={attempt} sleep={sleep_s:.1f}s", flush=True)
                time.sleep(sleep_s)
                continue
            out['resolved_at']=now()
            return out
        except Exception as e:
            out['status']='error'; out['error']=repr(e)[:500]; out['resolved_at']=now(); return out
    return out

def write_outputs(rows, contacts, summary):
    data={'summary':summary,'contacts':contacts}
    JSON_OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    STATE.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    fields=['id','title','company','level','format','url','destination','telegram_url','telegram_handle','status','attempts','resolved_at','error']
    with CSV_OUT.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(contacts)
    by_handle={}
    for c in contacts:
        h=c.get('telegram_handle') or ''
        if h:
            by_handle.setdefault(h,[]).append(c)
    lines=['# HireHi Telegram contacts — slow resolve','',f'Updated: {now()}',f'Total TG vacancies: {len(rows)}',f'Resolved t.me contacts: {summary.get("resolved_tme",0)}',f'Unique handles: {summary.get("unique_handles",0)}',f'Blocked/error: {summary.get("blocked_or_error",0)}','']
    lines.append('## Contacts')
    for h in sorted(by_handle, key=lambda x:x.lower()):
        xs=by_handle[h]
        lines.append(f'- @{h} — {len(xs)} vacancy(s)')
        for c in xs:
            lines.append(f"  - {c['id']} | {c['level']} {c['title']} | {c['company']} | {c['format']} | {c['url']}")
    lines.append('')
    lines.append('## Unresolved / blocked')
    for c in contacts:
        if not c.get('telegram_handle'):
            lines.append(f"- {c['id']} | {c['title']} | {c['company']} | status={c.get('status')} | error={c.get('error','')[:160]}")
    MD_OUT.write_text('\n'.join(lines)+'\n',encoding='utf-8')

def main():
    rows=list(csv.DictReader(open(TG_CSV,encoding='utf-8')))
    existing=[]
    done_ids=set()
    if STATE.exists():
        try:
            existing=json.loads(STATE.read_text(encoding='utf-8')).get('contacts',[])
            # Reuse only successful or non-429 final records; retry 429 slowly now.
            for c in existing:
                if c.get('telegram_url') or (c.get('status') and c.get('status')!='http_429'):
                    done_ids.add(c['id'])
        except Exception:
            existing=[]
    by_id={c['id']:c for c in existing if c.get('id') in done_ids}
    opener,token=opener_token()
    if not token:
        raise SystemExit('No HireHi sb-access-token cookie')
    contacts=[]
    for idx,row in enumerate(rows,1):
        if row['id'] in by_id:
            c=by_id[row['id']]
            contacts.append(c)
            print(f"{now()} reuse {idx}/{len(rows)} id={row['id']} status={c.get('status')} tg={c.get('telegram_handle')}", flush=True)
            continue
        print(f"{now()} consume {idx}/{len(rows)} id={row['id']} {row['title']} {row['company']}", flush=True)
        c={**row, **consume(row,opener,token)}
        contacts.append(c)
        summary={
            'total':len(rows),
            'processed':len(contacts),
            'resolved_allowed':sum(x.get('status')=='allowed' for x in contacts),
            'resolved_tme':sum(bool(x.get('telegram_url')) for x in contacts),
            'unique_handles':len({x.get('telegram_handle') for x in contacts if x.get('telegram_handle')}),
            'blocked_or_error':sum(not bool(x.get('telegram_url')) for x in contacts),
            'updated_at':now(),
            'delay_policy':'10 + random(0,3) seconds between actions',
        }
        write_outputs(rows, contacts, summary)
        print(f"{now()} result id={row['id']} status={c.get('status')} tg={c.get('telegram_handle') or '-'}", flush=True)
        if idx < len(rows):
            sleep_s=DELAY_MIN+random.random()*DELAY_JITTER
            print(f"{now()} sleep {sleep_s:.1f}s", flush=True)
            time.sleep(sleep_s)
    summary={
        'total':len(rows),
        'processed':len(contacts),
        'resolved_allowed':sum(x.get('status')=='allowed' for x in contacts),
        'resolved_tme':sum(bool(x.get('telegram_url')) for x in contacts),
        'unique_handles':len({x.get('telegram_handle') for x in contacts if x.get('telegram_handle')}),
        'blocked_or_error':sum(not bool(x.get('telegram_url')) for x in contacts),
        'updated_at':now(),
        'delay_policy':'10 + random(0,3) seconds between actions',
    }
    write_outputs(rows, contacts, summary)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
