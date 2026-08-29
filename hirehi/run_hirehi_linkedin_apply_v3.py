#!/usr/bin/env python3
from __future__ import annotations
import csv,json,re,time,html,urllib.parse,urllib.request,urllib.error
from pathlib import Path
import browser_cookie3
from playwright.sync_api import sync_playwright
BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
CSV=BASE/'output/linkedin_candidates.csv'
STATE=BASE/'output/linkedin_chrome_cookie_state.json'
REPORT=BASE/'output/linkedin_hirehi_apply_run_v3.json'
RESUME=Path('/Users/deploydog-ai/LinkedIn/shared/resumes/andrew-anashkin-eu-devops-cv.pdf')
SKIP_ID={'66506'}
OFFICE_RE=re.compile(r'офис|гибрид|hybrid|on-site|onsite|алматы|варшава', re.I)
BLOCK_RE=re.compile(r'captcha|checkpoint|security verification|verify your identity|account restricted|try again later|safeguard|rate limit', re.I)
DM_TEMPLATE="Здравствуйте! Увидел вакансию {title} через HireHi / LinkedIn. Я Senior DevOps Engineer: Kubernetes, Terraform, CI/CD, IaC, cloud/production infrastructure, automation. Рассматриваю remote/relocation. Прикладываю CV; буду рад обсудить роль."
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
    return opener,token

def normalize_hirehi_url(u):
    u=u.replace('/devops/devops/sre-', '/devops/devops-sre-')
    pr=urllib.parse.urlsplit(u)
    path=urllib.parse.quote(urllib.parse.unquote(pr.path), safe='/')
    return urllib.parse.urlunsplit((pr.scheme,pr.netloc,path,pr.query,pr.fragment))

def get_open_url(opener,token,row):
    url=normalize_hirehi_url(row['url'])
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Authorization':'Bearer '+token})
    text=opener.open(req,timeout=30).read().decode('utf-8','ignore')
    if 'Страница не найдена' in text[:2000]: raise urllib.error.HTTPError(url,404,'not found',{},None)
    m=re.search(r'<script[^>]+id="vacancy-data-json"[^>]*>\s*(\{.*?\})\s*</script>',text,re.S)
    data=json.loads(html.unescape(m.group(1))) if m else {}
    payload={'type':'direct_contact','job_id':int(row['id'])}
    if data.get('contact_ticket'): payload['contact_ticket']=data['contact_ticket']
    req=urllib.request.Request('https://hirehi.ru/api/limits/consume',data=json.dumps(payload).encode(),headers={'User-Agent':'Mozilla/5.0','Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
    out=json.loads(opener.open(req,timeout=30).read().decode('utf-8','ignore'))
    return out.get('open_url'), {'url':url,'consume':out,'vacancy':{k:data.get(k) for k in ['is_authenticated','has_pro','has_application','contact_ticket']}}

def check_block(page):
    try: txt=page.locator('body').inner_text(timeout=8000)
    except Exception: txt=''
    return bool(BLOCK_RE.search(page.url+' '+txt)), txt

def author_from_post_url(url):
    m=re.search(r'linkedin\.com/posts/([^_/?#]+)',url)
    return f'https://www.linkedin.com/in/{m.group(1)}/' if m else None

def author_from_dom(page):
    try:
        val=page.evaluate(r"""() => {
          const a=document.querySelector('a.update-components-actor__meta-link[href*="/in/"]');
          if(a) return {text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(), href:a.href.split('?')[0]};
          const img=document.querySelector('a.update-components-actor__image[href*="/in/"]');
          if(img) return {text:(img.innerText||img.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(), href:img.href.split('?')[0]};
          return null;
        }""")
        return val
    except Exception: return None

def find_editor(page):
    for scope in [page,*page.frames]:
        for sel in ["div.msg-form__contenteditable[contenteditable='true']","div[role='textbox'][contenteditable='true']","div[contenteditable='true'][aria-label*='Write']","div[contenteditable='true']"]:
            try:
                loc=scope.locator(sel)
                if loc.count() and loc.last.is_visible(timeout=1500): return loc.last
            except Exception: pass
    return None

def click_top_action(page, regex):
    return page.evaluate(r"""(pat)=>{
      const re=new RegExp(pat,'i'); const norm=s=>(s||'').replace(/\s+/g,' ').trim();
      const vis=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};
      const xs=[...document.querySelectorAll('main button, main a[role=button], main a[href*="/messaging/compose"]')]
       .map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),href:el.href||'',r:el.getBoundingClientRect()}))
       .filter(x=>vis(x.el)&&re.test([x.txt,x.href].join(' '))&&x.r.top>200&&x.r.top<850&&x.r.left<950);
      xs.sort((a,b)=>(a.r.top-b.r.top)||(a.r.left-b.r.left)); if(!xs.length)return null; xs[0].el.click(); return {txt:xs[0].txt,href:xs[0].href,x:xs[0].r.x,y:xs[0].r.y};
    }""", regex)

def click_any(page, regex):
    return page.evaluate(r"""(pat)=>{
      const re=new RegExp(pat,'i'); const norm=s=>(s||'').replace(/\s+/g,' ').trim(); const vis=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};
      const xs=[...document.querySelectorAll('button,a[role=button]')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),r:el.getBoundingClientRect()})).filter(x=>vis(x.el)&&re.test(x.txt)); xs.sort((a,b)=>(b.r.width*b.r.height-a.r.width*a.r.height)); if(!xs.length)return null; xs[0].el.click(); return xs[0].txt;
    }""", regex)

def attach_resume(page):
    if not RESUME.exists(): return 'resume_missing'
    try:
        inp=page.locator('input[type=file]'); n=inp.count()
        if n:
            inp.nth(n-1).set_input_files(str(RESUME),timeout=7000); page.wait_for_timeout(3500); return 'attached'
    except Exception as e: return 'attach_failed:'+repr(e)[:100]
    return 'no_file_input'

def send_profile(ctx, profile, title):
    page=ctx.new_page(); page.goto(profile,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(5000)
    blocked,txt=check_block(page)
    if blocked: return {'status':'blocked','reason':'linkedin_stop_pattern','profile_url':page.url,'head':txt[:500]}
    if 'authwall' in page.url or re.search(r'Войти|Sign in|Join now|Присоединитесь',txt[:1500]): return {'status':'blocked','reason':'authwall','profile_url':page.url,'head':txt[:500]}
    click=click_top_action(page,r'Message|messaging/compose'); page.wait_for_timeout(3500)
    if click:
        ed=find_editor(page)
        if ed:
            msg=DM_TEMPLATE.format(title=title); ed.click(); ed.fill(msg); attach=attach_resume(page); page.keyboard.press('Control+Enter'); page.wait_for_timeout(7000)
            txt2=page.locator('body').inner_text(timeout=10000)
            ok='Увидел вакансию' in txt2 and 'Senior DevOps Engineer' in txt2
            return {'status':'sent' if ok else 'uncertain','method':'dm','verified':ok,'attach':attach,'profile_url':profile,'click':click}
    conn=click_top_action(page,r'^Connect$|Connect'); page.wait_for_timeout(2500)
    if conn:
        click_any(page,r'Add a note|Добавить заметку'); page.wait_for_timeout(1200)
        note=NOTE_TEMPLATE.format(title=title)[:280]
        loc=page.locator('textarea, div[contenteditable=true]').last
        if loc.count(): loc.fill(note)
        click_any(page,r'^Send$|Отправить'); page.wait_for_timeout(6000)
        txt3=page.locator('body').inner_text(timeout=10000)
        ok=bool(re.search(r'Pending|Ожидает|Invitation sent|Приглашение',txt3,re.I))
        return {'status':'sent' if ok else 'uncertain','method':'connect_note','verified':ok,'profile_url':profile,'click':conn}
    return {'status':'skipped','reason':'no_message_or_connect','profile_url':profile}

def process(ctx,dest,row):
    if '/posts/' in dest:
        profile=author_from_post_url(dest)
        return send_profile(ctx,profile,row['title']) | {'destination':dest,'post_author':{'href':profile,'source':'url_slug'}}
    page=ctx.new_page(); page.goto(dest,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(6000)
    blocked,txt=check_block(page)
    if blocked: return {'status':'blocked','reason':'linkedin_stop_pattern','destination':page.url,'head':txt[:500]}
    if '/feed/update/' in page.url:
        a=author_from_dom(page)
        if not a: return {'status':'skipped','reason':'post_no_author_profile','destination':page.url}
        res=send_profile(ctx,a['href'],row['title']); res.update({'destination':dest,'post_author':a}); return res
    if '/in/' in page.url:
        res=send_profile(ctx,page.url.split('?')[0],row['title']); res['destination']=dest; return res
    return {'status':'skipped','reason':'unsupported_destination','destination':page.url}

def main():
    if not export_linkedin_state(): raise SystemExit('No LinkedIn auth cookie')
    opener,token=hirehi_client(); assert token
    rows=list(csv.DictReader(CSV.open(encoding='utf-8')))
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=False,slow_mo=120)
        ctx=browser.new_context(storage_state=str(STATE),viewport={'width':1500,'height':1000},accept_downloads=True)
        smoke=ctx.new_page(); smoke.goto('https://www.linkedin.com/feed/',wait_until='domcontentloaded',timeout=60000); smoke.wait_for_timeout(3000)
        st=smoke.locator('body').inner_text(timeout=10000)
        if 'authwall' in smoke.url or re.search(r'Войти|Sign in|Join now|Присоединитесь',st[:1200]): raise SystemExit('LinkedIn auth failed')
        for row in rows:
            rid=row.get('id');
            if not rid: continue
            item={'id':rid,'title':row['title'],'company':row['company'],'format':row['format'],'hirehi_url':row['url']}
            if rid in SKIP_ID: item.update({'status':'skipped','reason':'excluded_already_applied'}); results.append(item); continue
            if OFFICE_RE.search(row['format']): item.update({'status':'skipped','reason':'office_or_hybrid'}); results.append(item); continue
            try:
                dest,meta=get_open_url(opener,token,row); item['destination']=dest; item['hirehi_meta']=meta
                if not dest or 'linkedin.com' not in dest: item.update({'status':'skipped','reason':'no_linkedin_open_url'}); results.append(item); continue
                res=process(ctx,dest,row); item.update(res)
                try: ctx.pages[-1].screenshot(path=str(BASE/f'output/linkedin_apply_v3_{rid}.png'),full_page=False)
                except Exception: pass
                if item.get('status')=='blocked': results.append(item); break
                time.sleep(10)
            except urllib.error.HTTPError as e:
                item.update({'status':'skipped','reason':f'hirehi_http_{e.code}'})
            except Exception as e:
                item.update({'status':'error','reason':repr(e)[:500]})
            results.append(item); REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'sent':sum(r.get('status')=='sent' for r in results),'uncertain':sum(r.get('status')=='uncertain' for r in results),'skipped':sum(r.get('status')=='skipped' for r in results),'blocked':sum(r.get('status')=='blocked' for r in results),'errors':sum(r.get('status')=='error' for r in results),'report':str(REPORT),'results':results},ensure_ascii=False,indent=2))
if __name__=='__main__': main()
