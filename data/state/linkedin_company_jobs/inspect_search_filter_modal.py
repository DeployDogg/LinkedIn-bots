#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as outreach

JS_MODAL = r'''
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const body = norm(document.body.innerText || '');
  const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .artdeco-modal, .search-reusables__filters-modal')).map(el => norm(el.innerText)).filter(Boolean);
  const checked = Array.from(document.querySelectorAll('input:checked, [aria-checked="true"], [aria-selected="true"]')).map(el => {
    let p = el;
    let txt = '';
    for (let i=0; i<6 && p; i++, p=p.parentElement) {
      const t = norm(p.innerText || p.textContent || '');
      if (t && t.length > txt.length && t.length < 600) txt = t;
    }
    return {tag: el.tagName, type: el.getAttribute('type'), aria: el.getAttribute('aria-label'), text: txt};
  });
  return {url: location.href, body: body.slice(0, 3000), dialogs, checked};
}
'''

def main():
    out = {}
    with sync_playwright() as p:
        bh, ctx, page, mode = outreach.open_linkedin_context(p, {})
        page.set_default_timeout(15000)
        for job, url in outreach.SEARCHES.items():
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(3000)
            try:
                page.get_by_text('All filters', exact=True).first.click(timeout=10000)
            except Exception as e:
                try:
                    page.locator('button:has-text("All filters")').first.click(timeout=10000)
                except Exception as e2:
                    out[job] = {'click_error': repr(e2), 'pre': page.evaluate(JS_MODAL)}
                    continue
            page.wait_for_timeout(2000)
            out[job] = page.evaluate(JS_MODAL)
            try:
                page.keyboard.press('Escape')
            except Exception:
                pass
        try:
            if mode in {'storage_state','persistent_context'}: bh.close()
        except Exception: pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == '__main__': main()
