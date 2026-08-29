#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, time
from pathlib import Path
from datetime import datetime
import browser_cookie3
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
CSV=BASE/'output/linkedin_candidates.csv'
HIREHI_SESSION=BASE/'output/session.json'
STATE=BASE/'output/linkedin_chrome_cookie_state.json'
REPORT=BASE/'output/linkedin_hirehi_apply_run.json'
RESUME=Path('/Users/deploydog-ai/LinkedIn/shared/resumes/andrew-anashkin-eu-devops-cv.pdf')
SKIP_ID={'66506'}
BLOCK_RE=re.compile(r'captcha|checkpoint|security verification|verify your identity|account restricted|try again later|safeguard|rate limit', re.I)
OFFICE_RE=re.compile(r'офис|гибрид|hybrid|on-site|onsite|алматы|варшава', re.I)

DM_TEMPLATE_RU = """Здравствуйте! Увидел вакансию {title} через HireHi / LinkedIn. Я Senior DevOps Engineer: Kubernetes, Terraform, CI/CD, IaC, cloud/production infrastructure, automation. Рассматриваю remote/relocation. Прикладываю CV; буду рад обсудить роль."""
NOTE_TEMPLATE = "Hi! I saw your {title} role via HireHi/LinkedIn. I’m Andrew, Senior DevOps Engineer (Kubernetes, Terraform, CI/CD, IaC). Remote/relocation works. Happy to share CV."


def export_linkedin_state():
    cookies=[]
    jar=browser_cookie3.chrome(domain_name='.linkedin.com')
    for c in jar:
        d={'name':c.name,'value':c.value,'domain':c.domain,'path':c.path or '/', 'sameSite':'Lax', 'secure':bool(getattr(c,'secure',False)), 'httpOnly':False}
        if c.expires: d['expires']=float(c.expires)
        cookies.append(d)
    STATE.write_text(json.dumps({'cookies':cookies,'origins':[]},ensure_ascii=False,indent=2),encoding='utf-8')
    return any(c['name']=='li_at' for c in cookies), len(cookies)

def visible_click_by_text(page, text_re):
    return page.evaluate(r"""(pat) => {
      const re = new RegExp(pat, 'i');
      const norm=s=>(s||'').replace(/\s+/g,' ').trim();
      const visible=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'};
      const els=[...document.querySelectorAll('a,button,[role=button]')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),r:el.getBoundingClientRect()})).filter(x=>visible(x.el)&&re.test(x.txt));
      els.sort((a,b)=>b.r.width*b.r.height-a.r.width*a.r.height);
      if(!els.length) return null;
      els[0].el.click();
      return {text:els[0].txt, x:els[0].r.x, y:els[0].r.y, w:els[0].r.width, h:els[0].r.height};
    }""", text_re)

def check_block(page):
    try:
        txt=page.locator('body').inner_text(timeout=5000)
    except Exception:
        txt=''
    return bool(BLOCK_RE.search(page.url+' '+txt)), txt[:2000]

def first_author_profile(page):
    links=page.evaluate(r"""() => [...document.querySelectorAll('a[href*="/in/"]')]
      .map(a=>({text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(), href:a.href}))
      .filter(x=>x.href.includes('/in/') && !/Andrew Anashkin/i.test(x.text+x.href)).slice(0,20)""")
    # Prefer actor name link, not comments/sidebar
    for l in links:
        if 'trk=public_post_feed-actor-name' in l['href'] or 'feed-actor-name' in l['href']:
            return l
    for l in links:
        if l['text'] and not re.search(r'Pavel|Andrew', l['text'], re.I):
            return l
    return links[0] if links else None

def find_editor(page):
    selectors=["div.msg-form__contenteditable[contenteditable='true']", "div[role='textbox'][contenteditable='true']", "div[contenteditable='true'][aria-label*='Write']", "div[contenteditable='true']"]
    for scope in [page,*page.frames]:
        for sel in selectors:
            try:
                loc=scope.locator(sel)
                if loc.count() and loc.last.is_visible(timeout=1500):
                    return loc.last
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
            .filter(x=>x.r.top>250 && x.r.top<850 && x.r.left<900);
          candidates.sort((a,b)=>(a.r.top-b.r.top)||(a.r.left-b.r.left));
          if(!candidates.length) return false;
          candidates[0].el.click(); return true;
        }""")
        page.wait_for_timeout(3000)
        return bool(clicked)
    except Exception:
        return False

def attach_resume_if_possible(page):
    if not RESUME.exists(): return 'resume_missing'
    inputs=page.locator('input[type=file]')
    try:
        n=inputs.count()
        if n:
            # try last input first; it was document attachment in previous runs
            inputs.nth(n-1).set_input_files(str(RESUME), timeout=5000)
            page.wait_for_timeout(2500)
            return 'attached'
    except Exception as e:
        return 'attach_failed:'+repr(e)[:120]
    return 'no_file_input'

def send_dm(page, profile_url, title):
    page.goto(profile_url, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    blocked, head=check_block(page)
    if blocked: return {'status':'blocked','reason':'linkedin_stop_pattern','head':head,'url':page.url}
    if 'authwall' in page.url or re.search(r'Войти|Sign in|Join now|Присоединитесь', head):
        return {'status':'blocked','reason':'not_logged_in_or_authwall','url':page.url,'head':head}
    if click_message(page):
        ed=find_editor(page)
        if ed:
            msg=DM_TEMPLATE_RU.format(title=title)
            ed.click(timeout=5000)
            ed.fill(msg)
            attach=attach_resume_if_possible(page)
            page.keyboard.press('Control+Enter')
            page.wait_for_timeout(6000)
            txt=page.locator('body').inner_text(timeout=10000)
            ok=('Увидел вакансию' in txt and 'Senior DevOps Engineer' in txt)
            return {'status':'sent' if ok else 'uncertain','method':'dm','attach':attach,'verified':ok,'url':page.url}
    # Try Connect with note
    try:
        clicked=visible_click_by_text(page, r'^Connect$|Connect')
        if clicked:
            page.wait_for_timeout(2000)
            visible_click_by_text(page, r'Add a note|Добавить заметку')
            page.wait_for_timeout(1000)
            note=NOTE_TEMPLATE.format(title=title)[:280]
            loc=page.locator('textarea, div[contenteditable=true]').last
            if loc.count(): loc.fill(note)
            visible_click_by_text(page, r'^Send$|Отправить')
            page.wait_for_timeout(5000)
            txt=page.locator('body').inner_text(timeout=10000)
            ok=bool(re.search(r'Pending|Ожидает|Invitation sent|Приглашение', txt, re.I))
            return {'status':'sent' if ok else 'uncertain','method':'connect_note','verified':ok,'url':page.url}
    except Exception as e:
        return {'status':'skipped','reason':'no_message_connect_failed:'+repr(e)[:160], 'url':page.url}
    return {'status':'skipped','reason':'no_message_or_connect','url':page.url}

def resolve_hirehi_destination(ctx, page, row, token_state):
    # ensure hirehi token exists before every navigation
    page.add_init_script(f"""(() => {{ localStorage.setItem('hirehi_auth_state', {json.dumps(json.dumps(token_state))}); }})()""")
    before=set(p.url for p in ctx.pages)
    page.goto(row['url'], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(3500)
    blocked, head=check_block(page)
    if blocked: return None, {'status':'blocked','reason':'hirehi_stop_pattern','head':head}
    try:
        with ctx.expect_page(timeout=7000) as ep:
            click=visible_click_by_text(page, r'^LinkedIn$')
        newp=ep.value
        newp.wait_for_load_state('domcontentloaded', timeout=60000)
        newp.wait_for_timeout(4000)
        return newp.url, {'click':click, 'opened':'new_page'}
    except Exception:
        # maybe same page or already opened another tab
        page.wait_for_timeout(3000)
        for p in ctx.pages:
            if p.url not in before and 'linkedin.com' in p.url:
                return p.url, {'opened':'detected_page'}
        return None, {'status':'skipped','reason':'no_linkedin_destination_after_click'}

def main():
    has_li_at, cookie_count=export_linkedin_state()
    token=json.loads(HIREHI_SESSION.read_text(encoding='utf-8'))['access_token']
    auth_state={'access_token':token}
    rows=list(csv.DictReader(CSV.open(encoding='utf-8')))
    results=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=False, slow_mo=120)
        ctx=browser.new_context(storage_state=str(STATE), viewport={'width':1500,'height':1000}, accept_downloads=True)
        page=ctx.new_page()
        # login smoke
        page.goto('https://www.linkedin.com/feed/', wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(3000)
        body=page.locator('body').inner_text(timeout=10000)
        if not has_li_at or 'authwall' in page.url or re.search(r'Присоединитесь|Join now|Войти|Sign in', body[:1200]):
            raise SystemExit('LinkedIn cookies not logged in')
        for row in rows:
            rid=row['id']
            if not rid or rid in SKIP_ID: 
                results.append({'id':rid,'url':row.get('url'),'status':'skipped','reason':'excluded_already_applied'})
                continue
            if OFFICE_RE.search(row.get('format','')):
                results.append({'id':rid,'url':row.get('url'),'status':'skipped','reason':'office_or_hybrid','format':row.get('format')})
                continue
            item={'id':rid,'title':row['title'],'company':row['company'],'format':row['format'],'hirehi_url':row['url']}
            try:
                dest, meta=resolve_hirehi_destination(ctx,page,row,auth_state)
                item['destination']=dest; item['resolve']=meta
                if not dest:
                    item.update({'status':'skipped','reason':meta.get('reason','no_destination')}); results.append(item); REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8'); continue
                # Use a fresh page for LinkedIn destination to avoid HireHi modal page confusion
                lpage=ctx.new_page(); lpage.goto(dest, wait_until='domcontentloaded', timeout=60000); lpage.wait_for_timeout(5000)
                blocked, head=check_block(lpage)
                if blocked:
                    item.update({'status':'blocked','reason':'linkedin_stop_pattern','head':head}); results.append(item); break
                if '/feed/update/' in lpage.url or '/posts/' in lpage.url:
                    author=first_author_profile(lpage)
                    item['post_author']=author
                    if author:
                        sendres=send_dm(lpage, author['href'].split('?')[0], row['title'])
                        item.update(sendres)
                    else:
                        item.update({'status':'skipped','reason':'post_no_author_profile'})
                elif '/in/' in lpage.url:
                    item.update(send_dm(lpage, lpage.url.split('?')[0], row['title']))
                else:
                    item.update({'status':'skipped','reason':'unsupported_linkedin_destination','dest_url':lpage.url})
                try: lpage.screenshot(path=str(BASE/f'output/linkedin_apply_{rid}.png'), full_page=False)
                except Exception: pass
                time.sleep(8)
            except Exception as e:
                item.update({'status':'error','reason':repr(e)[:500]})
            results.append(item)
            REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
    REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    sent=sum(1 for r in results if r.get('status')=='sent')
    skipped=sum(1 for r in results if r.get('status')=='skipped')
    blocked=[r for r in results if r.get('status')=='blocked']
    print(json.dumps({'sent':sent,'skipped':skipped,'blocked':len(blocked),'report':str(REPORT),'results':results},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
