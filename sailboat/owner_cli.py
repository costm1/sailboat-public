"""Owner-only cloud control CLI. Never takes a secret on a command line."""
import argparse,json,os
from pathlib import Path
from urllib.parse import urlparse
import httpx

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['status','arm','disarm']);p.add_argument('--url',required=True);p.add_argument('--key-file',type=Path)
 a=p.parse_args();u=urlparse(a.url)
 if u.scheme!='https' or u.username or u.password or u.query or u.fragment or u.path not in ('','/'):p.error('HTTPS origin required')
 key=a.key_file.read_text().strip() if a.key_file else os.environ.get('SAILBOAT_OWNER_KEY','')
 if not key:p.error('Set SAILBOAT_OWNER_KEY or --key-file to the dedicated owner credential; agent keys cannot arm.')
 with httpx.Client(timeout=90,follow_redirects=False,headers={'Authorization':'Bearer '+key}) as c:
  r=c.post(a.url.rstrip('/')+'/owner/control',json={'command':a.command})
 try:body=r.json()
 except Exception:body={'error':'non-JSON server response','http_status':r.status_code}
 print(json.dumps(body,indent=2))
 if r.status_code>=400:raise SystemExit(1)
if __name__=='__main__':main()
