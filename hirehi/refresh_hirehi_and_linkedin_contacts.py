#!/usr/bin/env python3
from __future__ import annotations
import csv, json, re, html, time, urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from typing import Any
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import browser_cookie3
from playwright.sync_api import sync_playwright

BASE=Path('/Users/deploydog-ai/LinkedIn/hirehi')
OUT=BASE/'output'
OUT.mkdir(parents=True, exist_ok=True)
SEARCH_URL="https://hirehi.ru/?format=%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D0%BE&level=middle&level=senior&level=lead&search=DevOps&page=1"
UA='Mozilla/5.0'
OFFICE_RE=re.compile(r'офис|гибрид|hybrid|on-site|onsite|алматы|варшава|москва|питер|санкт|калининград|лимасол', re.I)
LINKEDIN_RE=re.compile(r'linkedin\.com', re.I)
TELEGRAM_RE=re.compile(r'(?:t\.me|telegram\.me)/', re.I)
EXCLUDE_HIREHI_SOCIAL=['linkedin.com/company/107994980','t.me/tribute','t.me/jun_hi','t.me/jun_hi_devops','t.me/generalsupport','telegram.me/generalsupport']


def norm(s:str)->str:
    return re.sub(r'\s+',' ', html.unescape(s or '')).strip()

def urlopen_text(url, opener=None, headers=None, data=None, timeout=30):
    headers={'User-Agent':UA, **(headers or {})}
    req=urllib.request.Request(url, data=data, headers=headers)
    op=opener or urllib.request.build_opener()
    return op.open(req, timeout=timeout).read().decode('utf-8','ignore')

def build_page_url(url,page):
    pr=urllib.parse.urlsplit(url)
    q=[(k,v) for k,v in urllib.parse.parse_qsl(pr.query, keep_blank_values=True) if k!='page']
    q.append(('page',str(page)))
    return urllib.parse.urlunsplit((pr.scheme,pr.netloc,pr.path,urllib.parse.urlencode(q),pr.fragment))

def total_pages(text):
    m=re.search(r'data-total-pages="(\d+)"', text)
    if m: return int(m.group(1))
    pages=[int(x) for x in re.findall(r'[?&]page=(\d+)', text) if x.isdigit()]
    return max(pages) if pages else 1

def extract_slugs(search_html):
    out=[]
    for href in re.findall(r'href="(/devops/[^"]+)"', search_html):
        href=html.unescape(href).split('#')[0]
        if href not in out: out.append(href)
    return out

def normalize_hirehi_url(u):
    u=u.replace('/devops/devops/sre-', '/devops/devops-sre-')
    pr=urllib.parse.urlsplit(u)
    path=urllib.parse.quote(urllib.parse.unquote(pr.path), safe='/')
    return urllib.parse.urlunsplit((pr.scheme,pr.netloc,path,pr.query,pr.fragment))

def slug_to_url(slug):
    return normalize_hirehi_url('https://hirehi.ru'+slug)

def parse_ld(text):
    for block in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', text, re.S):
        try: obj=json.loads(block)
        except Exception: continue
        xs=obj if isinstance(obj,list) else [obj]
        for x in xs:
            if isinstance(x,dict) and x.get('@type')=='JobPosting': return x
    return {}

def parse_vacancy_data(text):
    m=re.search(r'<script[^>]+id="vacancy-data-json"[^>]*>\s*(\{.*?\})\s*</script>', text, re.S)
    if not m: return {}
    try: return json.loads(html.unescape(m.group(1)))
    except Exception: return {}

def parse_detail(slug):
    url=slug_to_url(slug)
    row={'id':'','title':'','company':'','level':'','format':'','url':url,'contact_channel':'unknown','raw_contact_urls':[],'detail_status':'ok'}
    row['id']=re.search(r'-(\d+)(?:/?$)', urllib.parse.unquote(url)).group(1) if re.search(r'-(\d+)(?:/?$)', urllib.parse.unquote(url)) else ''
    try: text=urlopen_text(url)
    except Exception as e:
        row['detail_status']=f'fetch_error:{type(e).__name__}:{getattr(e,"code","")}'
        return row
    ld=parse_ld(text)
    row['title']=norm(ld.get('title',''))
    hiring=ld.get('hiringOrganization') or {}
    if isinstance(hiring,dict): row['company']=norm(hiring.get('name',''))
    loc=ld.get('jobLocation') or {}
    if isinstance(loc,dict):
        addr=loc.get('address') or {}
        if isinstance(addr,dict): row['format']=norm(addr.get('addressLocality',''))
    if not row['title']:
        m=re.search(r'<meta property="og:title" content="([^"]+)"', text); row['title']=norm(m.group(1)) if m else ''
    # level/format from page chips when available
    body_text=norm(re.sub(r'<[^>]+>',' ',text))
    for level in ['junior','middle','senior','lead']:
        if re.search(r'\b'+level+r'\b', body_text, re.I): row['level']=level; break
    # Preserve visible format markers better than JSON-LD when present
    for pat in [r'удалённо по РФ', r'удаленно по РФ', r'удалённо', r'удаленно', r'гибрид [А-ЯA-ZЁа-яa-zё\- ]+', r'офис [А-ЯA-ZЁа-яa-zё\- ]+', r'гибрид', r'офис']:
        m=re.search(pat, body_text, re.I)
        if m:
            row['format']=norm(m.group(0)); break
    main=text.split('<footer',1)[0]
    # static page/direct contact type lives in data-direct-kind; footer/marketing links are noisy
    direct_kinds=[]
    for m in re.finditer(r'data-direct-kind=["\']([^"\']+)["\']', main, re.I):
        k=norm(m.group(1)).lower()
        if k not in direct_kinds: direct_kinds.append(k)
    links=[]
    for href in re.findall(r'href="([^"]+)"', main):
        href=html.unescape(href)
        if href.startswith('/') or href.startswith('#'): continue
        if any(x in href for x in EXCLUDE_HIREHI_SOCIAL): continue
        if 'hirehi.ru' in href and 't.me/' not in href and 'linkedin.com' not in href: continue
        if href.rstrip('/') == url.rstrip('/'): continue
        links.append(href)
    row['raw_contact_urls']=list(dict.fromkeys(links))
    if 'linkedin' in direct_kinds: row['contact_channel']='linkedin'
    elif 'telegram' in direct_kinds: row['contact_channel']='telegram'
    elif 'email' in direct_kinds: row['contact_channel']='email'
    elif direct_kinds: row['contact_channel']='direct_'+direct_kinds[0]
    elif any(LINKEDIN_RE.search(x) for x in links): row['contact_channel']='linkedin'
    elif any(TELEGRAM_RE.search(x) for x in links): row['contact_channel']='telegram'
    elif links: row['contact_channel']='external_site'
    elif ld.get('directApply'): row['contact_channel']='hirehi_internal'
    row['direct_kinds']=direct_kinds
    return row

def get_cookie_opener_token():
    jar=browser_cookie3.chrome(domain_name='hirehi.ru')
    token=next((c.value for c in jar if c.name=='sb-access-token'), None)
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    return opener, token

def consume_linkedin(row, opener, token):
    url=normalize_hirehi_url(row['url'])
    out={'destination':'','consume_status':'','contact_ticket':False,'error':''}
    try:
        text=urlopen_text(url, opener=opener, headers={'Authorization':'Bearer '+token} if token else {})
        vd=parse_vacancy_data(text)
        ticket=vd.get('contact_ticket')
        out['contact_ticket']=bool(ticket)
        payload={'type':'direct_contact','job_id':int(row['id'])}
        if ticket: payload['contact_ticket']=ticket
        raw=urlopen_text('https://hirehi.ru/api/limits/consume', opener=opener, data=json.dumps(payload).encode(), headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json'})
        js=json.loads(raw)
        out['consume_status']='allowed' if js.get('allowed') else 'not_allowed'
        out['destination']=js.get('open_url') or ''
        out['consume']=js
    except urllib.error.HTTPError as e:
        out['consume_status']=f'http_{e.code}'
        try: out['error']=e.read().decode('utf-8','ignore')[:500]
        except Exception: out['error']=str(e)
    except Exception as e:
        out['consume_status']='error'; out['error']=repr(e)[:500]
    return out

def classify_destination(dest):
    if not dest: return 'none'
    d=dest.lower()
    if '/posts/' in d: return 'linkedin_post_slug'
    if '/feed/update/' in d: return 'linkedin_feed_post'
    if '/in/' in d: return 'linkedin_profile'
    if '/company/' in d: return 'linkedin_company'
    if '/messaging/compose' in d: return 'linkedin_message_compose'
    if 'linkedin.com' in d: return 'linkedin_other'
    return 'non_linkedin'

def export_linkedin_state():
    state=OUT/'linkedin_chrome_cookie_state.json'
    cookies=[]
    for c in browser_cookie3.chrome(domain_name='.linkedin.com'):
        d={'name':c.name,'value':c.value,'domain':c.domain,'path':c.path or '/', 'sameSite':'Lax','secure':bool(getattr(c,'secure',False)),'httpOnly':False}
        if c.expires: d['expires']=float(c.expires)
        cookies.append(d)
    state.write_text(json.dumps({'cookies':cookies,'origins':[]},ensure_ascii=False,indent=2),encoding='utf-8')
    return state, any(c['name']=='li_at' for c in cookies)

def author_from_post_url(url):
    m=re.search(r'linkedin\.com/posts/([^_/?#]+)', url or '')
    return f'https://www.linkedin.com/in/{m.group(1)}/' if m else ''

def extract_profile_from_linkedin(page, dest):
    typ=classify_destination(dest)
    if typ=='linkedin_post_slug':
        return {'profile_url':author_from_post_url(dest),'profile_source':'post_slug','profile_text':''}
    page.goto(dest, wait_until='domcontentloaded', timeout=60000)
    page.wait_for_timeout(5000)
    body=''
    try: body=page.locator('body').inner_text(timeout=10000)
    except Exception: pass
    if re.search(r'captcha|checkpoint|security verification|verify your identity|account restricted|try again later|safeguard|rate limit', body+' '+page.url, re.I):
        return {'profile_url':'','profile_source':'blocked','profile_text':body[:300]}
    if '/in/' in page.url:
        return {'profile_url':page.url.split('?')[0], 'profile_source':'destination_profile', 'profile_text':page.title()}
    if '/messaging/compose' in page.url:
        # recipient profile is often not directly visible; try links in compose body
        try:
            a=page.evaluate("""()=>{let a=[...document.querySelectorAll('a[href*=\"/in/\"]')].find(x=>x.href&&!x.href.includes('andrew-anashkin')); return a?{href:a.href.split('?')[0], text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim()}:null;}""")
            if a: return {'profile_url':a['href'],'profile_source':'compose_dom','profile_text':a.get('text','')}
        except Exception: pass
        return {'profile_url':'','profile_source':'compose_no_profile','profile_text':body[:300]}
    if '/feed/update/' in page.url:
        try:
            a=page.evaluate("""()=>{let a=document.querySelector('a.update-components-actor__meta-link[href*=\"/in/\"]')||document.querySelector('a.update-components-actor__image[href*=\"/in/\"]'); return a?{href:a.href.split('?')[0], text:(a.innerText||a.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim()}:null;}""")
            if a: return {'profile_url':a['href'],'profile_source':'feed_actor','profile_text':a.get('text','')}
        except Exception: pass
        return {'profile_url':'','profile_source':'feed_no_actor','profile_text':body[:300]}
    if '/company/' in page.url:
        return {'profile_url':page.url.split('?')[0], 'profile_source':'company_page', 'profile_text':page.title()}
    return {'profile_url':'','profile_source':'unsupported_linkedin_dest','profile_text':body[:300]}

def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8', newline='') as f:
        w=csv.DictWriter(f, fieldnames=fields, extrasaction='ignore'); w.writeheader(); w.writerows(rows)

def main():
    # 1) refresh search
    first=urlopen_text(build_page_url(SEARCH_URL,1))
    pages=total_pages(first)
    slugs=[]; page_counts={}
    for p in range(1,pages+1):
        text=first if p==1 else urlopen_text(build_page_url(SEARCH_URL,p))
        xs=extract_slugs(text); page_counts[str(p)]=len(xs)
        for x in xs:
            if x not in slugs: slugs.append(x)
    rows=[]
    workers=min(24, max(8, (os.cpu_count() or 4)*2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs={ex.submit(parse_detail, slug): slug for slug in slugs}
        for i,fut in enumerate(as_completed(futs),1):
            rows.append(fut.result())
            if i%25==0 or i==len(slugs): print(f'parsed {i}/{len(slugs)}', flush=True)
    rows.sort(key=lambda r:(r.get('id') and -int(r['id']) or 0))
    # 2) channel CSVs (for compatibility)
    linkedin=[]; telegram=[]; internal=[]; external=[]; unknown=[]
    for r in rows:
        ch=r['contact_channel']
        base={k:r.get(k,'') for k in ['id','title','company','level','format','url']}
        if ch.startswith('linkedin'): linkedin.append(base)
        elif ch=='telegram': telegram.append(base)
        elif ch=='hirehi_internal': internal.append(base)
        elif ch=='external_site': external.append(base)
        else: unknown.append(base)
    write_csv(OUT/'jobs_fresh.csv', rows, ['id','title','company','level','format','url','contact_channel','detail_status','raw_contact_urls'])
    write_csv(OUT/'linkedin_candidates_fresh.csv', linkedin, ['id','title','company','level','format','url'])
    write_csv(OUT/'telegram_candidates_fresh.csv', telegram, ['id','title','company','level','format','url'])
    # Do not overwrite old until fresh success
    write_csv(OUT/'linkedin_candidates.csv', linkedin, ['id','title','company','level','format','url'])
    write_csv(OUT/'telegram_candidates.csv', telegram, ['id','title','company','level','format','url'])
    # 3) resolve LinkedIn via HireHi consume
    opener,token=get_cookie_opener_token()
    contacts=[]
    for i,r in enumerate(linkedin,1):
        c=consume_linkedin(r, opener, token) if token else {'destination':'','consume_status':'no_hirehi_token','error':'No sb-access-token cookie'}
        rec={**r, **c}
        rec['connect_type']=classify_destination(rec.get('destination',''))
        rec['work_mode_status']='skip_office_hybrid' if OFFICE_RE.search(r.get('format','')) else 'eligible_remote_relocation'
        contacts.append(rec)
        print(f"resolved {i}/{len(linkedin)} {r['id']} {rec['connect_type']} {rec.get('consume_status')}", flush=True)
        time.sleep(0.4)
    # 4) extract profile/user links from LinkedIn destinations, read-only
    state,ok=export_linkedin_state()
    if ok and contacts:
        with sync_playwright() as pw:
            b=pw.chromium.launch(headless=False, slow_mo=80)
            ctx=b.new_context(storage_state=str(state), viewport={'width':1500,'height':1000})
            page=ctx.new_page()
            for i,rec in enumerate(contacts,1):
                dest=rec.get('destination') or ''
                if 'linkedin.com' not in dest:
                    rec.update({'profile_url':'','profile_source':'no_linkedin_destination','profile_text':''}); continue
                prof=extract_profile_from_linkedin(page, dest)
                rec.update(prof)
                print(f"profile {i}/{len(contacts)} {rec['id']} {rec.get('profile_source')} {rec.get('profile_url')}", flush=True)
                if prof.get('profile_source')=='blocked':
                    break
                time.sleep(1.0)
            b.close()
    else:
        for rec in contacts:
            rec.update({'profile_url':author_from_post_url(rec.get('destination','')) if '/posts/' in (rec.get('destination','')) else '', 'profile_source':'no_linkedin_auth' if not ok else 'no_destination', 'profile_text':''})
    # 5) outputs
    write_csv(OUT/'linkedin_contacts_by_type.csv', contacts, ['id','title','company','level','format','work_mode_status','url','destination','connect_type','profile_url','profile_source','profile_text','consume_status','contact_ticket','error'])
    (OUT/'linkedin_contacts_by_type.json').write_text(json.dumps({'source_url':SEARCH_URL,'total_pages':pages,'page_counts':page_counts,'total_vacancies':len(rows),'channel_counts':dict((k,len(v)) for k,v in {'linkedin':linkedin,'telegram':telegram,'hirehi_internal':internal,'external':external,'unknown':unknown}.items()),'linkedin_contacts':contacts}, ensure_ascii=False, indent=2), encoding='utf-8')
    by=defaultdict(list)
    for c in contacts: by[c.get('connect_type','unknown')].append(c)
    lines=['# HireHi LinkedIn contacts by connect type','',f'Source: {SEARCH_URL}',f'Total vacancies refreshed: {len(rows)}',f'LinkedIn vacancies: {len(contacts)}','', 'Channel counts:']
    for k,v in {'linkedin':linkedin,'telegram':telegram,'hirehi_internal':internal,'external':external,'unknown':unknown}.items(): lines.append(f'- {k}: {len(v)}')
    lines.append('')
    for typ in sorted(by):
        lines.append(f'## {typ} ({len(by[typ])})')
        for c in by[typ]:
            lines.append(f"- {c['id']} | {c['title']} | {c['company']} | {c['format']} | user: {c.get('profile_url') or 'N/A'} | dest: {c.get('destination') or 'N/A'} | source: {c.get('profile_source','')}")
        lines.append('')
    (OUT/'linkedin_contacts_by_type.md').write_text('\n'.join(lines), encoding='utf-8')
    # Sorted all channels markdown
    by_ch=defaultdict(list)
    for r in rows: by_ch[r['contact_channel']].append(r)
    lines=['# HireHi fresh vacancies by channel','',f'Source: {SEARCH_URL}',f'Total: {len(rows)}','']
    for ch in sorted(by_ch): lines.append(f'- {ch}: {len(by_ch[ch])}')
    lines.append('')
    for ch in sorted(by_ch):
        lines.append(f'## {ch} ({len(by_ch[ch])})')
        for r in by_ch[ch]: lines.append(f"- {r['id']} | {r['title']} | {r['company']} | {r['level']} | {r['format']} | {r['url']}")
        lines.append('')
    (OUT/'vacancies_by_channel_fresh.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps({'total_vacancies':len(rows),'pages':pages,'channel_counts':dict((k,len(v)) for k,v in {'linkedin':linkedin,'telegram':telegram,'hirehi_internal':internal,'external':external,'unknown':unknown}.items()),'linkedin_contacts':len(contacts),'reports':[str(OUT/'linkedin_contacts_by_type.csv'),str(OUT/'linkedin_contacts_by_type.md'),str(OUT/'vacancies_by_channel_fresh.md')]}, ensure_ascii=False, indent=2))

if __name__=='__main__':
    main()
