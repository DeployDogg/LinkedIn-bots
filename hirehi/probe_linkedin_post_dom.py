#!/usr/bin/env python3
import json, re
from pathlib import Path
from playwright.sync_api import sync_playwright
STATE='/Users/deploydog-ai/LinkedIn/hirehi/output/linkedin_chrome_cookie_state.json'
URL='https://www.linkedin.com/feed/update/urn:li:activity:7480558795950096384/'
with sync_playwright() as p:
 b=p.chromium.launch(headless=False)
 c=b.new_context(storage_state=STATE, viewport={'width':1500,'height':1000})
 page=c.new_page(); page.goto(URL, wait_until='domcontentloaded', timeout=60000); page.wait_for_timeout(6000)
 data=page.evaluate(r"""() => [...document.querySelectorAll('a[href*="/in/"]')].map((a,i)=>{const r=a.getBoundingClientRect(); let anc=a; let sample=''; for(let k=0;k<5&&anc;k++,anc=anc.parentElement){sample=(anc.innerText||'').replace(/\s+/g,' ').trim(); if(sample.length>80) break;} return {i,text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\s+/g,' ').trim(),href:a.href,x:r.x,y:r.y,w:r.width,h:r.height,sample:sample.slice(0,300), cls:a.className};}).filter(x=>x.w>0||x.h>0).slice(0,80)""")
 print('url',page.url,'title',page.title())
 print(json.dumps(data,ensure_ascii=False,indent=2)[:12000])
 page.screenshot(path='/Users/deploydog-ai/LinkedIn/hirehi/output/post_dom_61537.png', full_page=False)
 c.close(); b.close()
