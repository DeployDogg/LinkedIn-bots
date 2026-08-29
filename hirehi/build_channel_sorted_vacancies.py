#!/usr/bin/env python3
import csv
from pathlib import Path
from collections import defaultdict

OUT = Path('/Users/deploydog-ai/LinkedIn/hirehi/output')
SOURCES = [
    ('Telegram', OUT/'telegram_candidates.csv'),
    ('LinkedIn', OUT/'linkedin_candidates.csv'),
    ('HireHi internal', OUT/'jobs.csv'),
]

rows=[]
seen=set()
for channel, path in SOURCES:
    if not path.exists():
        continue
    with path.open(newline='', encoding='utf-8') as f:
        r=csv.DictReader(f)
        for row in r:
            title=row.get('title','').strip()
            company=row.get('company','').strip()
            level=row.get('level','').strip()
            fmt=(row.get('format') or row.get('location') or '').strip()
            url=(row.get('url') or row.get('job_url') or '').strip()
            # preserve duplicate appearance across channels, dedupe only exact channel+url
            key=(channel,url)
            if not url or key in seen:
                continue
            seen.add(key)
            rows.append({
                'channel': channel,
                'id': row.get('id','').strip() or url.rstrip('/').split('-')[-1],
                'title': title,
                'company': company,
                'level': level,
                'format': fmt,
                'url': url,
            })

order={'Telegram':0,'LinkedIn':1,'HireHi internal':2}
rows.sort(key=lambda x:(order.get(x['channel'],99), x['company'].lower(), x['title'].lower(), x['id']))

csv_path=OUT/'vacancies_by_channel_sorted.csv'
md_path=OUT/'vacancies_by_channel_sorted.md'
with csv_path.open('w', newline='', encoding='utf-8') as f:
    w=csv.DictWriter(f, fieldnames=['channel','id','title','company','level','format','url'])
    w.writeheader(); w.writerows(rows)

by=defaultdict(list)
for r in rows:
    by[r['channel']].append(r)

lines=[]
lines.append('# HireHi DevOps vacancies sorted by contact channel')
lines.append('')
lines.append('Counts:')
for ch in sorted(by, key=lambda c: order.get(c,99)):
    lines.append(f'- {ch}: {len(by[ch])}')
lines.append(f'- Total channel records: {len(rows)}')
lines.append('')
for ch in sorted(by, key=lambda c: order.get(c,99)):
    lines.append(f'## {ch} ({len(by[ch])})')
    for i,r in enumerate(by[ch],1):
        meta=' | '.join(x for x in [r['company'], r['level'], r['format']] if x)
        lines.append(f"{i}. {r['title']} — {meta} — {r['url']}")
    lines.append('')
md_path.write_text('\n'.join(lines), encoding='utf-8')
print(csv_path)
print(md_path)
print('total', len(rows))
for ch in sorted(by, key=lambda c: order.get(c,99)):
    print(ch, len(by[ch]))
