import os, json
from playwright.sync_api import sync_playwright

email = os.environ.get('LINKEDIN_EMAIL')
password = os.environ.get('LINKEDIN_PASSWORD')
args = json.loads(os.environ.get('LINKEDIN_BROWSER_ARGS_JSON', '["--no-sandbox"]'))
with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        '/Users/deploydog-ai/LinkedIn/shared/legacy_state/linkedin_chromium_profile_debug',
        headless=False,
        executable_path=os.environ.get('PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH') or None,
        args=args,
        viewport={'width': 1440, 'height': 1000},
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto('https://www.linkedin.com/login', wait_until='domcontentloaded', timeout=45000)
    page.wait_for_timeout(3000)
    print('before', page.url)
    forms = page.evaluate("""
    () => Array.from(document.forms).map((f,i)=>({
      i, action:f.action, method:f.method,
      controls:Array.from(f.querySelectorAll('input,button')).map(e=>({
        tag:e.tagName,type:e.type,name:e.name,id:e.id,aria:e.getAttribute('aria-label'),text:(e.innerText||'').trim()
      }))
    }))
    """)
    print('forms', json.dumps(forms, ensure_ascii=False)[:3000])
    page.locator('#username, input[name=session_key], input[autocomplete=username], input[type=email]').first.fill(email)
    page.locator('#password, input[name=session_password], input[autocomplete=current-password], input[type=password]').first.fill(password)
    filled = page.evaluate("""
    () => ({
      u: document.querySelector('#username, input[name=session_key], input[autocomplete=username], input[type=email]')?.value?.length,
      p: document.querySelector('#password, input[name=session_password], input[autocomplete=current-password], input[type=password]')?.value?.length
    })
    """)
    print('filled', filled)
    page.locator('form button[type=submit], button[type=submit]').first.click()
    try:
        page.wait_for_load_state('domcontentloaded', timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(8000)
    print('after', page.url)
    text = page.locator('body').inner_text(timeout=5000)
    print('text', text[:2500].replace('\n', ' | '))
    errors = page.evaluate("""
    () => Array.from(document.querySelectorAll('.error, .alert, [role=alert], .form__label--error, .input__message, .artdeco-inline-feedback, #error-for-password, #error-for-username'))
      .map(e=>e.innerText || e.textContent || '')
      .map(s=>s.trim()).filter(Boolean)
    """)
    print('errors', json.dumps(errors, ensure_ascii=False))
    ctx.close()
