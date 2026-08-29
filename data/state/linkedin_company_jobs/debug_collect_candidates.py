#!/usr/bin/env python3
import json
from playwright.sync_api import sync_playwright
import linkedin_message_outreach as outreach
with sync_playwright() as p:
    bh,ctx,page,mode=outreach.open_linkedin_context(p,{})
    page.goto(outreach.SEARCHES['DevOps'], wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    rows=outreach.collect_search_candidates(page)
    print(json.dumps([r.__dict__ for r in rows], ensure_ascii=False, indent=2))
    try:
      if mode in {'storage_state','persistent_context'}: bh.close()
    except Exception: pass
