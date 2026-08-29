#!/usr/bin/env python3
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as o
PROFILE='https://www.linkedin.com/in/chakayyagari/'
class Args:
    no_delay=True; delay_base=0; delay_jitter=0; headful=False; skip_dialog_scan=True
with sync_playwright() as p:
    bh, ctx, page, mode=o.open_linkedin_context(p,{})
    page.goto(PROFILE, wait_until='domcontentloaded', timeout=45000); page.wait_for_timeout(2000)
    o.open_message_box(page, Args()); page.wait_for_timeout(1000)
    s=o.find_subject_input(page); print('subj', s is not None)
    if s is not None: s.fill(o.compose_subject('DevOps'))
    e=o.find_message_editor(page); print('editor', e is not None)
    if e is not None: e.click(); page.keyboard.insert_text(o.compose_message('DevOps'))
    page.wait_for_timeout(1000)
    for i,fr in enumerate(page.frames):
        try:
            txt=fr.evaluate('document.body ? document.body.innerText : ""')
            if any(x in txt for x in ['New message','DevOps Engineer opportunity','Rewrite with AI','Would it be okay']):
                print('FRAME_TEXT_HIT',i,fr.url,txt[-2000:])
            else:
                print('frame',i,'no hit',fr.url,'len',len(txt))
        except Exception as ex: print('frameerr',i,repr(ex))
    # point scan around send icon area from screenshot
    for x,y in [(1088,912),(1085,910),(1080,910),(1090,900),(1050,910)]:
        try:
            info=page.evaluate('(p)=>{let e=document.elementFromPoint(p.x,p.y); let a=[]; while(e&&a.length<6){a.push({tag:e.tagName,text:(e.innerText||"").replace(/\\s+/g," ").trim().slice(0,120),aria:e.getAttribute("aria-label"),role:e.getAttribute("role"),cls:String(e.className).slice(0,160)}); e=e.parentElement;} return a;}', {'x':x,'y':y})
            print('POINT',x,y,info)
        except Exception as ex: print('pointerr',x,y,repr(ex))
    page.screenshot(path='/Users/deploydog-ai/LinkedIn/data/state/linkedin_company_jobs/debug_message_pointscan.png')
    if mode in {'storage_state','persistent_context'}: bh.close()
