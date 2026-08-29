#!/usr/bin/env python3
import browser_cookie3, urllib.request, urllib.error, json, http.cookiejar
jar=browser_cookie3.chrome(domain_name='hirehi.ru')
print('cookies', [(c.name,c.domain,c.path, bool(c.value)) for c in jar])
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
for url in ['https://hirehi.ru/api/auth/me','https://hirehi.ru/api/limits']:
 print('\nURL',url)
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','Accept':'application/json'})
  print(opener.open(req,timeout=20).read().decode()[:1000])
 except urllib.error.HTTPError as e:
  print('HTTP',e.code,e.read().decode('utf-8','ignore')[:500])
for jid in ['65256','61537']:
 body=json.dumps({'type':'direct_contact','job_id':int(jid)}).encode()
 req=urllib.request.Request('https://hirehi.ru/api/limits/consume',data=body,headers={'User-Agent':'Mozilla/5.0','Content-Type':'application/json','Accept':'application/json'})
 try:
  print('\nJOB',jid, opener.open(req,timeout=20).read().decode())
 except urllib.error.HTTPError as e:
  print('\nJOB',jid,'HTTP',e.code,e.read().decode('utf-8','ignore')[:500])
