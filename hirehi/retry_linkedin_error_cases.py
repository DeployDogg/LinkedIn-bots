#!/usr/bin/env python3
import json,re,time
from pathlib import Path
from playwright.sync_api import sync_playwright
STATE='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_chrome_cookie_state.json'
REPORT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_hirehi_apply_retry_errors.json')
RESUME=Path('/Users/deploydog-ai/LinkedIn/shared/resumes/andrew-anashkin-eu-devops-cv.pdf')
DM="Здравствуйте! Увидел вакансию devops engineer через HireHi / LinkedIn. Я Senior DevOps Engineer: Kubernetes, Terraform, CI/CD, IaC, cloud/production infrastructure, automation. Рассматриваю remote/relocation. Прикладываю CV; буду рад обсудить роль."
DESTS={
 '46159':'https://www.linkedin.com/feed/update/urn:li:activity:7464953265135280129',
 '46011':'https://www.linkedin.com/feed/update/urn:li:activity:7464907126532882432',
}

def author(page):
 return page.evaluate("""() => {let a=document.querySelector('a.update-components-actor__meta-link[href*=\"/in/\"]')||document.querySelector('a.update-components-actor__image[href*=\"/in/\"]'); return a?{text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim(),href:a.href.split('?')[0]}:null;}""")

def click_msg(page):
 return page.evaluate("""()=>{const norm=s=>(s||'').replace(/\\s+/g,' ').trim(); const vis=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&st.display!=='none'&&st.visibility!=='hidden'}; const xs=[...document.querySelectorAll('main button,main a[role=button],main a[href*=\"/messaging/compose\"]')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),href:el.href||'',r:el.getBoundingClientRect()})).filter(x=>vis(x.el)&&/Message|messaging\/compose/i.test(x.txt+' '+x.href)&&x.r.top>200&&x.r.top<900&&x.r.left<950); xs.sort((a,b)=>(a.r.top-b.r.top)||(a.r.left-b.r.left)); if(!xs.length)return null; xs[0].el.click(); return {txt:xs[0].txt,href:xs[0].href};} """)

def send(ctx, jid, dest):
 out={'id':jid,'destination':dest}
 p=ctx.new_page(); p.goto(dest, wait_until='domcontentloaded', timeout=60000); p.wait_for_timeout(6000)
 a=author(p); out['author']=a
 if not a: out.update(status='skipped',reason='no_author'); return out
 prof=ctx.new_page(); prof.goto(a['href'], wait_until='domcontentloaded', timeout=60000); prof.wait_for_timeout(5000)
 out['profile_url']=prof.url
 cm=click_msg(prof); out['click_message']=cm; prof.wait_for_timeout(3500)
 ed=prof.locator("div.msg-form__contenteditable[contenteditable='true'], div[role='textbox'][contenteditable='true'], div[contenteditable='true']").last
 if not ed.count(): out.update(status='skipped',reason='no_editor'); return out
 ed.click(force=True, timeout=5000)
 prof.keyboard.insert_text(DM)
 attach='no_file_input'
 try:
  inp=prof.locator('input[type=file]'); n=inp.count()
  if n:
   inp.nth(n-1).set_input_files(str(RESUME), timeout=7000); prof.wait_for_timeout(3500); attach='attached'
 except Exception as e: attach='attach_failed:'+repr(e)[:120]
 out['attach']=attach
 # Try explicit send button first, then Ctrl+Enter
 sent_click=prof.evaluate("""()=>{const norm=s=>(s||'').replace(/\\s+/g,' ').trim(); const vis=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&!el.disabled&&st.display!=='none'&&st.visibility!=='hidden'}; const xs=[...document.querySelectorAll('button')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),r:el.getBoundingClientRect()})).filter(x=>vis(x.el)&&/^Send$/i.test(x.txt)); xs.sort((a,b)=>b.r.top-a.r.top); if(xs.length){xs[0].el.click(); return xs[0].txt;} return null;}""")
 if not sent_click:
  prof.keyboard.press('Control+Enter')
 prof.wait_for_timeout(7000)
 txt=prof.locator('body').inner_text(timeout=10000)
 ok='Увидел вакансию devops engineer' in txt and 'Download' in txt
 out.update(status='sent' if ok else 'uncertain', verified=ok, sent_click=sent_click)
 prof.screenshot(path=f'/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_apply_retry_{jid}.png', full_page=False)
 return out

with sync_playwright() as pw:
 b=pw.chromium.launch(headless=False, slow_mo=120)
 ctx=b.new_context(storage_state=STATE, viewport={'width':1500,'height':1000}, accept_downloads=True)
 res=[]
 for jid,d in DESTS.items():
  res.append(send(ctx,jid,d)); time.sleep(8)
 b.close()
REPORT.write_text(json.dumps(res,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(res,ensure_ascii=False,indent=2))
