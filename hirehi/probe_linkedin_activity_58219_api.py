#!/usr/bin/env python3
import browser_cookie3, urllib.request, urllib.parse, json, re
jar=browser_cookie3.chrome(domain_name='.linkedin.com')
csrf=next((c.value.strip('"') for c in jar if c.name=='JSESSIONID'), '')
opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
urns=['urn:li:activity:7478092570393231360','urn:li:share:7478092570393231360']
urls=[]
for urn in urns:
 q=urllib.parse.quote(urn, safe='')
 urls += [
  f'https://www.linkedin.com/voyager/api/feed/updates/{q}',
  f'https://www.linkedin.com/voyager/api/feed/updates/{q}?decorationId=com.linkedin.voyager.dash.deco.feed.UpdateFull-100',
  f'https://www.linkedin.com/voyager/api/voyagerFeedDashUpdates/{q}',
  f'https://www.linkedin.com/voyager/api/graphql?queryId=voyagerFeedDashUpdates.2a1c5b5e2cb66d6e9c0b6f&variables=(urn:{q})',
 ]
for url in urls:
 print('\nURL',url)
 try:
  req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0','csrf-token':csrf,'accept':'application/vnd.linkedin.normalized+json+2.1','x-restli-protocol-version':'2.0.0'})
  data=opener.open(req,timeout=30).read().decode('utf-8','ignore')
  print('status ok len',len(data), data[:1000])
  for m in re.finditer(r'publicIdentifier|urn:li:fsd_profile|firstName|lastName|profilePicture|miniProfile|actor', data):
   print(' hit',m.start(),data[max(0,m.start()-150):m.start()+350])
 except Exception as e:
  print('ERR',type(e).__name__,getattr(e,'code',''),str(e)[:200])
