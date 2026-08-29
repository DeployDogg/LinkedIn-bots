#!/usr/bin/env python3
import json
import re
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as outreach

JS = r'''
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const links = Array.from(document.querySelectorAll('a[href*="/in/"]'))
    .map(a => {
      let el = a;
      let best = norm(a.innerText || a.textContent || '');
      let bestTag = a.tagName;
      for (let i=0; i<8 && el; i++, el=el.parentElement) {
        const t = norm(el.innerText || el.textContent || '');
        if (t.length > best.length && t.length < 1200) { best = t; bestTag = el.tagName + '.' + (el.className || ''); }
      }
      return {href: a.href.split('?')[0], anchor: norm(a.innerText || a.textContent || ''), card: best, tag: bestTag};
    })
    .filter(x => x.href.includes('/in/'));
  const uniq = [];
  const seen = new Set();
  for (const x of links) { if (!seen.has(x.href)) { seen.add(x.href); uniq.push(x); } }
  const body = norm(document.body.innerText || '');
  const interesting = body.split(/(?=\b(?:Actively|DevOps|Site Reliability|Platform|3rd\+|People|All filters|Current company|Follow|Message|Connect)\b)/).slice(0,80);
  return {url: location.href, title: document.title, bodyLen: body.length, interesting, links: uniq.slice(0,12)};
}
'''

def main():
    out = {}
    with sync_playwright() as p:
        bh, ctx, page, mode = outreach.open_linkedin_context(p, {})
        page.set_default_timeout(15000)
        for job, url in outreach.SEARCHES.items():
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
            page.wait_for_timeout(5000)
            out[job] = page.evaluate(JS)
        try:
            if mode in {'storage_state','persistent_context'}: bh.close()
        except Exception: pass
    print(json.dumps(out, ensure_ascii=False, indent=2))
if __name__ == '__main__': main()
