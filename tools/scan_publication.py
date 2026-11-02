"""Scan tracked source and every local Git object; print locations, never matches.

Complements a general-purpose secret scanner. It is not proof that secrets cannot exist.
"""
import argparse,json,re,subprocess
from pathlib import Path

RULES={
 'private-key':re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 'provider-token':re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|hf_[A-Za-z0-9]{25,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,})'),
 'personal-windows-path':re.compile(rb'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s"\']+'),
 'credential-in-url':re.compile(rb'https?://[^\s/"\']+:[^\s/@"\']+@'),
 'credential-pem':re.compile(rb'\bAKIA[A-Z0-9]{16}\b'),
}
BANNED_PARTS={'.env','state','instances','evidence','checkpoints','node_modules','.venv','.app.json'}
BANNED_ENDINGS=('.sqlite','.sqlite-wal','.sqlite-shm','.snapshot','.db','.zip','.exe','.whl','.jsonl','.pem','.key','.log')

def git(*args):return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL)

def scan_bytes(data,location,denylist=()):
    hits=[]
    for label,pattern in RULES.items():
        if pattern.search(data):hits.append({'location':location,'rule':label})
    lowered=data.lower()
    if any(value.lower().encode() in lowered for value in denylist if value):
        hits.append({'location':location,'rule':'private-denylist'})
    return hits

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--denylist',type=Path)
    args=p.parse_args()
    denylist=json.loads(args.denylist.read_text()) if args.denylist else []
    paths=git('ls-files','-z').decode().split('\0');hits=[];count=0
    for name in filter(None,paths):
        path=Path(name);count+=1
        if path.is_symlink():hits.append({'location':name,'rule':'symlink'});continue
        if set(path.parts)&BANNED_PARTS or name.endswith(BANNED_ENDINGS) or name.endswith('/mcp.json'):
            hits.append({'location':name,'rule':'private-or-packaged-file'})
        hits.extend(scan_bytes(path.read_bytes(),name,denylist))
    # batch-all-objects includes unreachable objects, not just branch history.
    objects=git('cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').decode().splitlines()
    for row in objects:
        oid,kind=row.split()
        if kind in ('blob','commit','tag'):
            hits.extend(scan_bytes(git('cat-file',kind,oid),'git:'+oid,denylist))
    print(json.dumps({'tracked_files':count,'git_objects_scanned':len(objects),'findings':hits},indent=2))
    raise SystemExit(bool(hits))

if __name__=='__main__':main()
