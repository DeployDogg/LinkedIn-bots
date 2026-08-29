#!/usr/bin/env python3
import csv,json,re,html,urllib.request,urllib.parse
from pathlib import Path
from collections import defaultdict
OUT=Path('/Users/deploydog-ai/LinkedIn/hirehi/output')
SEARCH_URL="https://hirehi.ru/?format=%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D0%BE&level=middle&level=senior&level=lead&search=DevOps&page=1"

def get(url):
 return urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'}),timeout=30).read().decode('utf-8','ignore')
def page_url(page):
 pr=urllib.parse.urlsplit(SEARCH_URL); q=[(k,v) for k,v in urllib.parse.parse_qsl(pr.query) if k!='page']; q.append(('page',str(page))); return urllib.parse.urlunsplit((pr.scheme,pr.netloc,pr.path,urllib.parse.urlencode(q),pr.fragment))
def total_pages(t):
 m=re.search(r'data-total-pages="(\d+)"',t); return int(m.group(1)) if m else 1
def norm(s): return re.sub(r'\s+',' ',html.unescape(s or '')).strip()
def meta_from_title(title):
 s=norm(title)
 # senior DevOps Engineer в EPAM, зп не указана, удалённо
 m=re.match(r'(?P<level>junior|middle|senior|lead)\s+(?P<title>.+?)\s+в\s+(?P<rest>.+)$',s,re.I)
 out={'level':'','title':'','company':'','format':''}
 if m:
  out['level']=m.group('level').lower(); out['title']=norm(m.group('title'))
  parts=[norm(x) for x in m.group('rest').split(',')]
  out['company']=parts[0] if parts else ''
  out['format']=parts[-1] if len(parts)>1 else ''
 return out
first=get(page_url(1)); pages=total_pages(first); meta={}
for p in range(1,pages+1):
 t=first if p==1 else get(page_url(p))
 for href,title in re.findall(r'<a href="(/devops/[^"]+)"[^>]*title="([^"]+)"',t):
  jid=re.search(r'-(\d+)$',href)
  if jid: meta[jid.group(1)]=meta_from_title(title)|{'href':'https://hirehi.ru'+href}
print('search meta',len(meta),'pages',pages)
# load rows
rows=list(csv.DictReader(open(OUT/'jobs_fresh.csv',encoding='utf-8')))
for r in rows:
 m=meta.get(r['id'])
 if m:
  for k in ['title','company','level','format']:
   if m.get(k): r[k]=m[k]
fields=['id','title','company','level','format','url','contact_channel','detail_status','raw_contact_urls']
with open(OUT/'jobs_fresh.csv','w',encoding='utf-8',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(rows)
# channel CSVs
for channel,name in [('linkedin','linkedin_candidates.csv'),('telegram','telegram_candidates.csv')]:
 xs=[{k:r.get(k,'') for k in ['id','title','company','level','format','url']} for r in rows if r.get('contact_channel')==channel]
 with open(OUT/name,'w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=['id','title','company','level','format','url']); w.writeheader(); w.writerows(xs)
 with open(OUT/name.replace('.csv','_fresh.csv'),'w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=['id','title','company','level','format','url']); w.writeheader(); w.writerows(xs)
# contacts merge
contacts=list(csv.DictReader(open(OUT/'linkedin_contacts_by_type.csv',encoding='utf-8')))
for c in contacts:
 m=meta.get(c['id'])
 if m:
  for k in ['title','company','level','format']:
   if m.get(k): c[k]=m[k]
 if c['id']=='58219' and not c.get('profile_url'):
  c['profile_source']='linkedin_post_unavailable_actor_null'
  c['profile_text']='LinkedIn says: This post cannot be displayed; voyager API returned actor:null. No user/profile link available from source.'
fields=['id','title','company','level','format','work_mode_status','url','destination','connect_type','profile_url','profile_source','profile_text','consume_status','contact_ticket','error']
with open(OUT/'linkedin_contacts_by_type.csv','w',encoding='utf-8',newline='') as f:
 w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(contacts)
# JSON/MD rebuild
by=defaultdict(list)
for c in contacts: by[c.get('connect_type','unknown')].append(c)
lines=['# HireHi LinkedIn contacts by connect type','',f'Source: {SEARCH_URL}',f'Total vacancies refreshed: {len(rows)}',f'LinkedIn vacancies: {len(contacts)}','', 'Channel counts:']
for ch in sorted(set(r['contact_channel'] for r in rows)):
 lines.append(f'- {ch}: {sum(r["contact_channel"]==ch for r in rows)}')
lines.append('')
for typ in sorted(by):
 lines.append(f'## {typ} ({len(by[typ])})')
 for c in by[typ]:
  lines.append(f"- {c['id']} | {c['level']} {c['title']} | {c['company']} | {c['format']} | user: {c.get('profile_url') or 'N/A'} | dest: {c.get('destination') or 'N/A'} | source: {c.get('profile_source','')}")
 lines.append('')
(OUT/'linkedin_contacts_by_type.md').write_text('\n'.join(lines),encoding='utf-8')
(OUT/'linkedin_contacts_by_type.json').write_text(json.dumps({'source_url':SEARCH_URL,'total_pages':pages,'total_vacancies':len(rows),'channel_counts':{ch:sum(r['contact_channel']==ch for r in rows) for ch in sorted(set(r['contact_channel'] for r in rows))},'linkedin_contacts':contacts},ensure_ascii=False,indent=2),encoding='utf-8')
# all channels MD
bych=defaultdict(list)
for r in rows: bych[r['contact_channel']].append(r)
lines=['# HireHi fresh vacancies by channel','',f'Source: {SEARCH_URL}',f'Total: {len(rows)}','']
for ch in sorted(bych): lines.append(f'- {ch}: {len(bych[ch])}')
lines.append('')
for ch in sorted(bych):
 lines.append(f'## {ch} ({len(bych[ch])})')
 for r in bych[ch]: lines.append(f"- {r['id']} | {r['level']} {r['title']} | {r['company']} | {r['format']} | {r['url']}")
 lines.append('')
(OUT/'vacancies_by_channel_fresh.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps({'total':len(rows),'contacts':len(contacts),'channels':{ch:len(bych[ch]) for ch in sorted(bych)},'missing_profiles':[c['id'] for c in contacts if not c.get('profile_url')]},ensure_ascii=False,indent=2))
