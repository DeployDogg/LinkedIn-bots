#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as o
PROFILE='https://www.linkedin.com/in/chakayyagari/'
class Args:
    no_delay=True
    delay_base=0
    delay_jitter=0
    headful=False
    skip_dialog_scan=True

def dump(page, stage):
    data = page.evaluate(r'''
    () => {
      const norm=s=>(s||'').replace(/\s+/g,' ').trim();
      return {
        url: location.href,
        title: document.title,
        subjectInputs: Array.from(document.querySelectorAll("input,textarea")).map((e,i)=>({i, tag:e.tagName, type:e.getAttribute('type'), name:e.getAttribute('name'), placeholder:e.getAttribute('placeholder'), aria:e.getAttribute('aria-label'), value:e.value, visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)})).filter(x=>/subject/i.test([x.name,x.placeholder,x.aria,x.value].join(' ')) || x.visible),
        editors: Array.from(document.querySelectorAll("div[contenteditable='true'],div[role='textbox']")).map((e,i)=>({i, aria:e.getAttribute('aria-label'), role:e.getAttribute('role'), text:norm(e.innerText), html:e.innerHTML.slice(0,300), visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)})),
        buttons: Array.from(document.querySelectorAll('button,a[role=button],div[role=button]')).map((e,i)=>({i, tag:e.tagName, text:norm(e.innerText), aria:e.getAttribute('aria-label'), role:e.getAttribute('role'), type:e.getAttribute('type'), disabled:e.disabled===true || e.getAttribute('disabled')!==null, ariaDisabled:e.getAttribute('aria-disabled'), class:e.className, visible:!!(e.offsetWidth||e.offsetHeight||e.getClientRects().length)})).filter(x=>x.visible && /send|message|close|discard|leave|cancel|inmail/i.test([x.text,x.aria,x.class].join(' '))).slice(-80),
        leaveText: /Are you sure you want to discard this message|Leave\?/i.test(document.body.innerText),
        bodyText: norm(document.body.innerText).slice(-3000)
      };
    }
    ''')
    print('---STAGE', stage)
    print(json.dumps(data, ensure_ascii=False, indent=2))

with sync_playwright() as p:
    bh, ctx, page, mode = o.open_linkedin_context(p, {})
    page.set_default_timeout(10000)
    page.goto(PROFILE, wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(2500)
    dump(page, 'profile')
    print('open_message_box', o.open_message_box(page, Args()))
    page.wait_for_timeout(1500)
    dump(page, 'opened')
    subj=o.find_subject_input(page)
    print('subject_found', bool(subj))
    if subj:
        subj.fill(o.compose_subject('DevOps'))
    ed=o.find_message_editor(page)
    print('editor_found', bool(ed))
    if ed:
        ed.click(); page.keyboard.insert_text(o.compose_message('DevOps'))
    page.wait_for_timeout(1500)
    dump(page, 'filled')
    try:
        page.screenshot(path='/Users/deploydog-ai/LinkedIn/data/state/linkedin_company_jobs/debug_message_compose_dom.png', full_page=True)
    except Exception as e:
        print('screenshot_error', repr(e))
    if mode in {'storage_state','persistent_context'}:
        bh.close()
