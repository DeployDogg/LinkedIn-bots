#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as outreach
JS=r'''
() => {
 const norm=s=>(s||'').replace(/\s+/g,' ').trim();
 const anchors=Array.from(document.querySelectorAll('a[href*="/in/"]')).filter(a=>a.offsetParent!==null);
 return anchors.map(a=>{
   let node=a, steps=[];
   for(let i=0;i<10&&node;i++,node=node.parentElement){
     const txt=norm(node.innerText||node.textContent||'');
     steps.push({tag:node.tagName, len:txt.length, hasRel:/(?:2nd|3rd\+)/.test(txt), hasAction:/\b(?:Connect|Message|Follow|Pending)\b/i.test(txt), sample:txt.slice(0,160)});
   }
   return {href:a.href.split('?')[0], direct: norm(a.innerText||a.getAttribute('aria-label')||'').slice(0,250), directHas3:/3rd\+/.test(norm(a.innerText||a.getAttribute('aria-label')||'')), steps};
 }).slice(0,20);
}
'''
with sync_playwright() as p:
 bh,ctx,page,mode=outreach.open_linkedin_context(p,{})
 page.goto(outreach.SEARCHES['DevOps'], wait_until='domcontentloaded', timeout=60000)
 page.wait_for_timeout(5000)
 print(json.dumps(page.evaluate(JS), ensure_ascii=False, indent=2))
 try:
  if mode in {'storage_state','persistent_context'}: bh.close()
 except Exception: pass
