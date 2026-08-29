#!/usr/bin/env python3
import json, time, re
from pathlib import Path
from playwright.sync_api import sync_playwright
STATE='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_chrome_cookie_state.json'
REPORT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_hirehi_apply_retry_direct_compose.json')
RESUME=Path('/Users/deploydog-ai/LinkedIn/shared/resumes/andrew-anashkin-eu-devops-cv.pdf')
DM="Здравствуйте! Увидел вакансию devops engineer через HireHi / LinkedIn. Я Senior DevOps Engineer: Kubernetes, Terraform, CI/CD, IaC, cloud/production infrastructure, automation. Рассматриваю remote/relocation. Прикладываю CV; буду рад обсудить роль."
COMPOSE={
 '46159': 'https://www.linkedin.com/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAEJKjEAB18oPVY2txGpSMmQKascz0nY3q-s&recipient=ACoAAEJKjEAB18oPVY2txGpSMmQKascz0nY3q-s&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay',
 '46011': 'https://www.linkedin.com/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAFiRmpcBMIyDVMSTV5Z9XbiFxnXzkh7JTvU&recipient=ACoAAFiRmpcBMIyDVMSTV5Z9XbiFxnXzkh7JTvU&screenContext=NON_SELF_PROFILE_VIEW&interop=msgOverlay'
}

def find_editor(page):
    for scope in [page,*page.frames]:
        for sel in ["div.msg-form__contenteditable[contenteditable='true']", "div[role='textbox'][contenteditable='true']", "div[contenteditable='true']"]:
            loc=scope.locator(sel)
            try:
                if loc.count() and loc.last.is_visible(timeout=2500): return loc.last
            except Exception: pass
    return None

def send_button(page):
    return page.evaluate("""()=>{const norm=s=>(s||'').replace(/\s+/g,' ').trim(); const vis=el=>{const r=el.getBoundingClientRect(); const st=getComputedStyle(el); return r.width>0&&r.height>0&&!el.disabled&&st.display!=='none'&&st.visibility!=='hidden'}; const xs=[...document.querySelectorAll('button')].map(el=>({el,txt:norm(el.innerText||el.getAttribute('aria-label')||''),r:el.getBoundingClientRect()})).filter(x=>vis(x.el)&&/^Send$/i.test(x.txt)); xs.sort((a,b)=>b.r.top-a.r.top); if(xs[0]){xs[0].el.click(); return xs[0].txt;} return null;}""")

with sync_playwright() as pw:
    b=pw.chromium.launch(headless=False, slow_mo=120)
    ctx=b.new_context(storage_state=STATE, viewport={'width':1500,'height':1000}, accept_downloads=True)
    results=[]
    for jid,url in COMPOSE.items():
        page=ctx.new_page(); page.goto(url, wait_until='domcontentloaded', timeout=60000); page.wait_for_timeout(8000)
        out={'id':jid,'compose_url':url,'final_url':page.url}
        body=page.locator('body').inner_text(timeout=10000)
        if re.search(r'captcha|checkpoint|security verification|verify your identity|account restricted|try again later|safeguard|rate limit', body+page.url, re.I):
            out.update(status='blocked',reason='linkedin_stop_pattern',head=body[:500]); results.append(out); break
        ed=find_editor(page)
        if not ed:
            out.update(status='skipped',reason='no_editor_direct',head=body[:1000])
            page.screenshot(path=f'/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_apply_direct_{jid}.png', full_page=False)
            results.append(out); continue
        ed.click(force=True, timeout=5000)
        page.keyboard.insert_text(DM)
        attach='no_file_input'
        try:
            inputs=page.locator('input[type=file]')
            n=inputs.count()
            if n:
                inputs.nth(n-1).set_input_files(str(RESUME), timeout=7000); page.wait_for_timeout(4000); attach='attached'
        except Exception as e:
            attach='attach_failed:'+repr(e)[:120]
        clicked=send_button(page)
        if not clicked:
            page.keyboard.press('Control+Enter')
        page.wait_for_timeout(8000)
        body2=page.locator('body').inner_text(timeout=10000)
        ok=('Увидел вакансию devops engineer' in body2 and ('Download' in body2 or 'andrew-anashkin' in body2))
        out.update(status='sent' if ok else 'uncertain', verified=ok, attach=attach, clicked_send=clicked, head=body2[:1000])
        page.screenshot(path=f'/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_apply_direct_{jid}.png', full_page=False)
        results.append(out)
        time.sleep(8)
    b.close()
REPORT.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False,indent=2))
