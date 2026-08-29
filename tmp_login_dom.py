import os, json
from playwright.sync_api import sync_playwright
args=json.loads(os.environ.get('LINKEDIN_BROWSER_ARGS_JSON','["--no-sandbox"]'))
with sync_playwright() as p:
    ctx=p.chromium.launch_persistent_context('/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_chromium_profile_debug2', headless=False, executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or None, args=args, viewport={'width':1440,'height':1000})
    page=ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto('https://www.linkedin.com/login/', wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(5000)
    print('url', page.url)
    data=page.evaluate("""
    () => ({
      inputs: Array.from(document.querySelectorAll('input')).map((e,i)=>{const r=e.getBoundingClientRect(); return {i,type:e.type,name:e.name,id:e.id,autocomplete:e.autocomplete,placeholder:e.placeholder,aria:e.getAttribute('aria-label'),visible:!!e.offsetParent,w:r.width,h:r.height,x:r.x,y:r.y,valueLen:(e.value||'').length}}),
      buttons: Array.from(document.querySelectorAll('button,[role=button],a')).map((e,i)=>{const r=e.getBoundingClientRect(); return {i,tag:e.tagName,type:e.type,text:(e.innerText||'').trim().slice(0,100),aria:e.getAttribute('aria-label'),visible:!!e.offsetParent,w:r.width,h:r.height,x:r.x,y:r.y}}).filter(x=>x.visible && (x.text||x.aria||x.type))
    })
    """)
    print(json.dumps(data, ensure_ascii=False, indent=2)[:8000])
    ctx.close()
