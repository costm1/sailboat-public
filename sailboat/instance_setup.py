"""Initialize independent state and credentials. Never arms or submits orders."""
import argparse, hashlib, json, os, secrets, subprocess, sys
from pathlib import Path
from cryptography.fernet import Fernet
from .instance_config import validate_environment

def write_private(path,value):
    with path.open('x',encoding='utf-8') as f:
        f.write(json.dumps(value,indent=2)+'\n')
    path.chmod(0o600)

def initialize(directory,public_url):
    directory=Path(directory).resolve()
    # Exclusive directory creation is deliberate: never reset another installation.
    directory.mkdir(parents=True,exist_ok=False)
    owner=secrets.token_urlsafe(48);agent=secrets.token_urlsafe(48)
    digest=lambda s:hashlib.sha256(s.encode()).hexdigest()
    env={
      'SAILBOAT_PUBLIC_URL':public_url.rstrip('/'),
      'SAILBOAT_ALLOWED_HOSTS':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(public_url).netloc+',127.0.0.1:7860,localhost:7860',
      'SAILBOAT_KEYS_JSON':json.dumps({digest(agent):{'label':'owner-agent','source':'external-agent','conversation':'shared-paper'}}),
      'SAILBOAT_OWNER_KEY_SHA256':digest(owner),
      'SAILBOAT_AGENT_KEY':Fernet.generate_key().decode(),
      'RH_ENCRYPTION_KEY':Fernet.generate_key().decode(),
      'SAILBOAT_STORAGE_BACKEND':'local',
      'SAILBOAT_STATE_DIRECTORY':str(directory/'state'),
      'SAILBOAT_BROKER_WRITES_DISABLED':'1',
      'SAILBOAT_CANDIDATE_ENTRY_LIVE':'0',
      'SAILBOAT_BIND_HOST':'127.0.0.1',
      'SAILBOAT_MARKET_HOURS':'NYSE_REGULAR',
      'SAILBOAT_AGENT_INTERVAL':'120'
    }
    validate_environment(env)
    state=directory/'state';state.mkdir()
    from .agent_link import AgentLink
    from .bucket_store import BucketSnapshots
    from .cloud_store import checked_snapshot
    seed=directory/'seed.sqlite';link=AgentLink(seed)
    try:link.set_mode('DISARMED')
    finally:link.close()
    checked_snapshot(seed)
    (state/'state.snapshot').write_bytes(BucketSnapshots.encode(seed.read_bytes(),0)[0])
    seed.unlink() # this setup's empty scratch database only
    write_private(directory/'instance.json',env)
    write_private(directory/'owner-credentials.json',{'owner_token':owner})
    write_private(directory/'agent-credentials.json',{'agent_token':agent})
    return directory/'instance.json'

def isolated_environment(config):
    validate_environment(config)
    # Avoid accidental reuse of another installation's broker, cloud, agent or proxy environment.
    keep=('PATH','SystemRoot','SYSTEMROOT','WINDIR','TEMP','TMP','LANG','LC_ALL','SSL_CERT_FILE')
    env={k:os.environ[k] for k in keep if k in os.environ}
    env.update({k:str(v) for k,v in config.items()})
    env.update(PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1',HF_HUB_DISABLE_IMPLICIT_TOKEN='1')
    return env

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init');init.add_argument('--directory',required=True);init.add_argument('--public-url',required=True)
    for name in ('check','serve'):
        p=sub.add_parser(name);p.add_argument('--config',required=True,type=Path)
    plugin=sub.add_parser('plugin');plugin.add_argument('--config',required=True,type=Path);plugin.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if args.command=='init':
        result=initialize(args.directory,args.public_url)
        print('Independent DISARMED instance created. Private configuration: '+str(result));return
    config=json.loads(args.config.read_text(encoding='utf-8'))
    validated=validate_environment(config)
    if args.command=='check':
        print(json.dumps({'configuration':'valid','storage':validated['storage'],
          'startup_mode':'DISARMED','broker_writes_disabled':config.get('SAILBOAT_BROKER_WRITES_DISABLED','1')=='1',
          'network_calls':0}));return
    if args.command=='plugin':
        write_private(args.output,{'$schema':'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json',
          'mcpServers':{'sailboat':{'type':'streamable-http','url':validated['base_url']+'/mcp'}}})
        print('Instance-specific MCP connection created; configure authentication in your host.');return
    raise SystemExit(subprocess.call([sys.executable,'-m','sailboat.cloud_server'],env=isolated_environment(config)))

if __name__=='__main__':main()
