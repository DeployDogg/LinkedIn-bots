#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as o
PROFILE='https://www.linkedin.com/in/chakayyagari/'
class Args:
    no_delay=True; delay_base=0; delay_jitter=0; headful=False; skip_dialog_scan=True
with sync_playwright() as p:
    bh, ctx, page, mode = o.open_linkedin_context(p, {})
    page.set_default_timeout(10000)
    page.goto(PROFILE, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(2500)
    print('buttons before open')
    print(json.dumps(page.evaluate(r'''
    () => Array.from(document.querySelectorAll('button,a[role=button],a')).map((e,i)=>{const r=e.getBoundingClientRect(); const s=(e.innerText||e.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(); return {i,tag:e.tagName,text:(e.innerText||'').replace(/\s+/g,' ').trim(),aria:e.getAttribute('aria-label'),href:e.href||'',x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),visible:!!(r.width||r.height)}}).filter(x=>x.visible && /Message/i.test([x.text,x.aria,x.href].join(' '))).slice(0,50)
    '''), ensure_ascii=False, indent=2))
    o.open_message_box(page, Args())
    page.wait_for_timeout(1000)
    subj=o.find_subject_input(page)
    if subj is not None:
        try: subj.fill(o.compose_subject('DevOps'))
        except Exception as e: print('subj fill err',repr(e))
    ed=o.find_message_editor(page)
    if ed is not None:
        try: ed.click(); page.keyboard.insert_text(o.compose_message('DevOps'))
        except Exception as e: print('ed fill err',repr(e))
    page.wait_for_timeout(1000)
    print('frames', len(page.frames))
    for idx,fr in enumerate(page.frames):
        try:
            data=fr.evaluate(r'''
            () => Array.from(document.querySelectorAll('button,a[role=button],div[role=button]')).map((e,i)=>{const r=e.getBoundingClientRect(); return {i,tag:e.tagName,text:(e.innerText||'').replace(/\s+/g,' ').trim(),aria:e.getAttribute('aria-label'),role:e.getAttribute('role'),type:e.getAttribute('type'),disabled:e.disabled===true || e.getAttribute('disabled')!==null,ariaDisabled:e.getAttribute('aria-disabled'),cls:String(e.className).slice(0,120),x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height),visible:!!(r.width||r.height)}}).filter(x=>x.visible).slice(-120)
            ''')
            print('---FRAME',idx,fr.url)
            print(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            print('frame err',idx,repr(e))
    page.screenshot(path='/Users/deploydog-ai/LinkedIn/data/state/linkedin_company_jobs/debug_message_buttons.png', full_page=True)
    if mode in {'storage_state','persistent_context'}: bh.close()
