#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, time, html
from pathlib import Path
import urllib.request, urllib.error
import browser_cookie3
from playwright.sync_api import sync_playwright

BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
CSV=BASE/'output/linkedin_candidates.csv'
STATE=BASE/'output/linkedin_chrome_cookie_state.json'
REPORT=BASE/'output/linkedin_hirehi_apply_run.json'
RESUME=Path('/Users/deploydog-ai/LinkedIn/shared/resumes/andrew-anashkin-eu-devops-cv.pdf')
SKIP_ID={'66506'}
OFFICE_RE=re.compile(r'офис|гибрид|hybrid|on-site|onsite|алматы|варшава', re.I)
BLOCK_RE=re.compile(r'captcha|checkpoint|security verification|verify your identity|account restricted|try again later|safeguard|rate limit', re.I)
DM_TEMPLATE_RU="""Здравствуйте! Увидел вакансию {title} через HireHi / LinkedIn. Я Senior DevOps Engineer: Kubernetes, Terraform, CI/CD, IaC, cloud/production infrastructure, automation. Рассматриваю remote/relocation. Прикладываю CV; буду рад обсудить роль."""
NOTE_TEMPLATE="Hi! I saw your {title} role via HireHi/LinkedIn. I’m Andrew, Senior DevOps Engineer (Kubernetes, Terraform, CI/CD, IaC). Remote/relocation works. Happy to share CV."

def export_linkedin_state():
    cookies=[]
    for c in browser_cookie3.chrome(domain_name='.linkedin.com'):
        d={'name':c.name,'value':c.value,'domain':c.domain,'path':c.path or '/', 'sameSite':'Lax','secure':bool(getattr(c,'secure',False)),'httpOnly':False}
        if c.expires: d['expires']=float(c.expires)
        cookies.append(d)
    STATE.write_text(json.dumps({'cookies':cookies,'origins':[]},ensure_ascii=False,indent=2),encoding='utf-8')
    return any(c['name']=='li_at' for c in cookies)

def hirehi_client():
    jar=browser_cookie3.chrome(domain_name='hirehi.ru')
    token=next((c.value for c in jar if c.name=='sb-access-token'), None)
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, token

def get_ticket_and_url(opener, token, row):
    req=urllib.request.Request(row['url'], headers={'User-Agent':'Mozilla/5.0','Authorization':'Bearer '+token})
    text=opener.open(req,timeout=30).read().decode('utf-8','ignore')
    m=re.search(r'<script[^>]+id="vacancy-data-json"[^>]*>\s*(\{.*?\})\s*</script>', text, re.S)
    data=json.loads(html.unescape(m.group(1))) if m else {}
    ticket=data.get('contact_ticket')
    payload={'type':'direct_contact','job_id':int(row['id'])}
    if ticket: payload['contact_ticket']=ticket
    body=json.dumps(payload).encode()
    req=urllib.request.Request('https://hirehi.ru/api/limits/consume', data=body, headers={'User-Agent':'Mozilla/5.0','Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
    raw=opener.open(req,timeout=30).read().decode('utf-8','ignore')
    out=json.loads(raw)
    return out.get('open_url'), {'vacancy_data': {k:data.get(k) for k in ['is_authenticated','has_pro','has_application','contact_ticket','recruiter_full_name']}, 'consume': out}

def check_block(page):
    try: txt=page.locator('body').inner_text(timeout=8000)
    except Exception: txt=''
    return bool(BLOCK_RE.search(page.url+' '+txt)), txt

def first_author_profile(page):
    links=page.evaluate(r"""() => [...document.querySelectorAll('a[href*="/in/"]')]
      .map(a=>({text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(), href:a.href}))
      .filter(x=>x.href.includes('/in/') && !/Andrew Anashkin/i.test(x.text+x.href)).slice(0,40)""")
    for l in links:
        if 'feed-actor-name' in l['href'] or 'update-components-actor' in l['href'] or l['text']:
            return {'text':l['text'], 'href':l['href'].split('?')[0]}
    return {'text':'','href':links[0]['href'].split('?')[0]} if links else None

def find_editor(page):
    selectors=["div.msg-form__contenteditable[contenteditable='true']", "div[role='textbox'][contenteditable='true']", "div[contenteditable='true'][aria-label*='Write']", "div[contenteditable='true']"]
    for scope in [page,*page.frames]:
        for sel in selectors:
            try:
                loc=scope.locator(sel)
                if loc.count() and loc.last.is_visible(timeout=1500): return loc.last
            except Exception: pass
    return None

def click_message(page):
    try:
        clicked=page.evaluate(r"""() => {
          const norm=s=>(s||'').replace(/\s+/g,' ').trim();
          const visible=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};
          const candidates=[...document.querySelectorAll('a[href*="/messaging/compose"],button,a[role=button]')]
            .map(el=>({el,r:el.getBoundingClientRect(),txt:norm(el.innerText),aria:norm(el.getAttribute('aria-label')),href:el.href||''}))
            .filter(x=>visible(x.el) && /message/i.test([x.txt,x.aria,x.href].join(' ')))
            .filter(x=>x.r.top>180 && x.r.top<900 && x.r.left<950);
          candidates.sort((a,b)=>(a.r.top-b.r.top)||(a.r.left-b.r.left));
          if(!candidates.length) return null;
          candidates[0].el.click(); return {txt:candidates[0].txt, aria:candidates[0].aria, href:candidates[0].href};
        }""")
        page.wait_for_timeout(3500)
        return clicked
    except Exception:
        return None

def attach_resume(page):
    if not RESUME.exists(): return 'resume_missing'
    try:
        inputs=page.locator('input[type=file]'); n=inputs.count()
        if n:
            inputs.nth(n-1).set_input_files(str(RESUME), timeout=7000)
            page.wait_for_timeout(3500)
            return 'attached'
    except Exception as e: return 'attach_failed:'+repr(e)[:120]
    return 'no_file_input'

def click_text(page, pat):
    return page.evaluate(r"""(pat) => {
      const re=new RegExp(pat,'i'); const norm=s=>(s||'').replace(/\s+/g,' ').trim();
      const visible=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};
      const xs=[...document.querySelectorAll('button,a[role=button],a')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')),r:el.getBoundingClientRect()})).filter(x=>visible(x.el)&&re.test(x.txt));
      xs.sort((a,b)=>(a.r.top-b.r.top)); if(!xs.length) return false; xs[0].el.click(); return xs[0].txt;
    }""", pat)

def send_to_profile(page, profile_url, title):
    page.goto(profile_url, wait_until='domcontentloaded', timeout=60000); page.wait_for_timeout(5000)
    blocked, txt=check_block(page)
    if blocked: return {'status':'blocked','reason':'linkedin_stop_pattern','url':page.url,'head':txt[:1000]}
    if 'authwall' in page.url or re.search(r'Войти|Sign in|Join now|Присоединитесь', txt[:1500]):
        return {'status':'blocked','reason':'linkedin_authwall','url':page.url,'head':txt[:1000]}
    clicked=click_message(page)
    if clicked:
        ed=find_editor(page)
        if ed:
            msg=DM_TEMPLATE_RU.format(title=title)
            ed.click(timeout=5000); ed.fill(msg)
            attach=attach_resume(page)
            page.keyboard.press('Control+Enter')
            page.wait_for_timeout(7000)
            txt2=page.locator('body').inner_text(timeout=10000)
            ok='Увидел вакансию' in txt2 and 'Senior DevOps Engineer' in txt2
            return {'status':'sent' if ok else 'uncertain','method':'dm','attach':attach,'verified':ok,'profile_url':profile_url}
    # fallback connect note
    try:
        c=click_text(page, r'^Connect$|Connect')
        if c:
            page.wait_for_timeout(2000)
            click_text(page, r'Add a note|Добавить заметку')
            page.wait_for_timeout(1000)
            loc=page.locator('textarea, div[contenteditable=true]').last
            if loc.count(): loc.fill(NOTE_TEMPLATE.format(title=title)[:280])
            click_text(page, r'^Send$|Отправить')
            page.wait_for_timeout(5000)
            txt3=page.locator('body').inner_text(timeout=10000)
            ok=bool(re.search(r'Pending|Ожидает|Invitation sent|Приглашение', txt3, re.I))
            return {'status':'sent' if ok else 'uncertain','method':'connect_note','verified':ok,'profile_url':profile_url}
    except Exception as e:
        return {'status':'skipped','reason':'connect_failed:'+repr(e)[:160],'profile_url':profile_url}
    return {'status':'skipped','reason':'no_message_or_connect','profile_url':profile_url}

def process_dest(ctx, dest, row):
    page=ctx.new_page(); page.goto(dest, wait_until='domcontentloaded', timeout=60000); page.wait_for_timeout(6000)
    blocked, txt=check_block(page)
    if blocked: return {'status':'blocked','reason':'linkedin_stop_pattern','destination':page.url,'head':txt[:1000]}
    if '/feed/update/' in page.url or '/posts/' in page.url:
        author=first_author_profile(page)
        if not author: return {'status':'skipped','reason':'post_no_author_profile','destination':page.url}
        res=send_to_profile(page, author['href'], row['title']); res['post_author']=author; res['destination']=dest; return res
    if '/in/' in page.url:
        res=send_to_profile(page, page.url.split('?')[0], row['title']); res['destination']=dest; return res
    return {'status':'skipped','reason':'unsupported_destination','destination':page.url}

def main():
    if not export_linkedin_state(): raise SystemExit('No LinkedIn li_at cookie')
    opener,token=hirehi_client()
    if not token: raise SystemExit('No HireHi sb-access-token cookie')
    rows=list(csv.DictReader(CSV.open(encoding='utf-8')))
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=False, slow_mo=100)
        ctx=browser.new_context(storage_state=str(STATE), viewport={'width':1500,'height':1000}, accept_downloads=True)
        smoke=ctx.new_page(); smoke.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded', timeout=60000); smoke.wait_for_timeout(3000)
        smoke_text=smoke.locator('body').inner_text(timeout=10000)
        if 'authwall' in smoke.url or re.search(r'Войти|Sign in|Join now|Присоединитесь', smoke_text[:1200]): raise SystemExit('LinkedIn auth failed')
        for row in rows:
            rid=row.get('id','')
            item={'id':rid,'title':row.get('title'), 'company':row.get('company'), 'format':row.get('format'), 'hirehi_url':row.get('url')}
            if not rid: continue
            if rid in SKIP_ID:
                item.update({'status':'skipped','reason':'excluded_already_applied'}); results.append(item); continue
            if OFFICE_RE.search(row.get('format','')):
                item.update({'status':'skipped','reason':'office_or_hybrid'}); results.append(item); continue
            try:
                dest,meta=get_ticket_and_url(opener,token,row)
                item['destination']=dest; item['hirehi_meta']=meta
                if not dest:
                    item.update({'status':'skipped','reason':'no_open_url'}); results.append(item); continue
                if 'linkedin.com' not in dest:
                    item.update({'status':'skipped','reason':'not_linkedin_destination'}); results.append(item); continue
                res=process_dest(ctx,dest,row); item.update(res)
                try: ctx.pages[-1].screenshot(path=str(BASE/f'output/linkedin_apply_{rid}.png'), full_page=False)
                except Exception: pass
                if item.get('status')=='blocked': results.append(item); break
                time.sleep(9)
            except urllib.error.HTTPError as e:
                item.update({'status':'skipped','reason':f'hirehi_http_{e.code}', 'body':e.read().decode('utf-8','ignore')[:500]})
            except Exception as e:
                item.update({'status':'error','reason':repr(e)[:500]})
            results.append(item)
            REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'sent':sum(r.get('status')=='sent' for r in results),'uncertain':sum(r.get('status')=='uncertain' for r in results),'skipped':sum(r.get('status')=='skipped' for r in results),'blocked':sum(r.get('status')=='blocked' for r in results),'errors':sum(r.get('status')=='error' for r in results),'report':str(REPORT),'results':results},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
