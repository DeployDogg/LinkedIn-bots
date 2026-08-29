#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as o
PROFILE='https://www.linkedin.com/in/chakayyagari/'
class Args:
    no_delay=True; delay_base=0; delay_jitter=0; headful=False; skip_dialog_scan=True
with sync_playwright() as p:
    bh, ctx, page, mode=o.open_linkedin_context(p,{})
    page.goto(PROFILE, wait_until='domcontentloaded', timeout=45000); page.wait_for_timeout(2000)
    o.open_message_box(page, Args()); page.wait_for_timeout(1500)
    for i,fr in enumerate(page.frames):
        try:
            txt=fr.evaluate('document.body ? document.body.innerText : ""')
            if 'New message' not in txt: continue
            print('FRAME',i,fr.url)
            data=fr.evaluate(r'''
            () => ({
              inputs:Array.from(document.querySelectorAll('input,textarea')).map((e,i)=>{const r=e.getBoundingClientRect();return {i,tag:e.tagName,type:e.type,name:e.name,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),value:e.value,x:r.x,y:r.y,w:r.width,h:r.height,visible:!!(r.width||r.height)}}),
              editors:Array.from(document.querySelectorAll("div[contenteditable='true'],div[role='textbox']")).map((e,i)=>{const r=e.getBoundingClientRect();return {i,tag:e.tagName,aria:e.getAttribute('aria-label'),role:e.getAttribute('role'),text:e.innerText,x:r.x,y:r.y,w:r.width,h:r.height,visible:!!(r.width||r.height)}}),
              buttons:Array.from(document.querySelectorAll('button,a[role=button],div[role=button]')).map((e,i)=>{const r=e.getBoundingClientRect();return {i,tag:e.tagName,text:(e.innerText||'').replace(/\s+/g,' ').trim(),aria:e.getAttribute('aria-label'),role:e.getAttribute('role'),type:e.getAttribute('type'),disabled:e.disabled===true||e.getAttribute('disabled')!==null,ariaDisabled:e.getAttribute('aria-disabled'),cls:String(e.className).slice(0,180),x:r.x,y:r.y,w:r.width,h:r.height,visible:!!(r.width||r.height)}}).filter(x=>x.visible)
            })
            ''')
            print(json.dumps(data,ensure_ascii=False,indent=2))
        except Exception as e: print('ERR',i,repr(e))
    if mode in {'storage_state','persistent_context'}: bh.close()
