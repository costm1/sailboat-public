"""Explicit stopped-server import of this owner's newly authorized agent profile."""
import argparse,json,tempfile
from pathlib import Path
from cryptography.fernet import Fernet
from sailboat.instance_config import validate_environment
from sailboat.local_store import LocalSnapshots
from sailboat.cloud_store import DurableController

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,type=Path)
    p.add_argument('--profile',required=True,type=Path)
    p.add_argument('--server-stopped',action='store_true')
    p.add_argument('--output-secret',type=Path,help='Prepare an encrypted one-time hosted startup secret instead of writing local state')
    args=p.parse_args();config=json.loads(args.config.read_text())
    deployment=validate_environment(config)
    if config.get('SAILBOAT_BROKER_WRITES_DISABLED','1')!='1':raise ValueError('Disable writes before importing')
    profile=json.loads(args.profile.read_text())
    credentials=profile.get('credentials',{}).get('claudeAiOauth',{})
    if credentials.get('subscriptionType') not in ('pro','max','team','enterprise') or not credentials.get('accessToken'):
        raise ValueError('Owner-authorized official subscription profile required')
    encrypted=Fernet(config['SAILBOAT_AGENT_KEY'].encode()).encrypt(json.dumps(profile).encode()).decode()
    if args.output_secret:
        with args.output_secret.open('x',encoding='utf-8') as f:f.write(encrypted)
        args.output_secret.chmod(0o600)
        print('Encrypted owner profile prepared privately; set SAILBOAT_CLAUDE_INITIAL only on this instance, then remove it after durable import.')
        return
    if deployment['storage']!='local' or not args.server_stopped:raise ValueError('Local import requires --server-stopped; use --output-secret for hosted setup')
    with tempfile.TemporaryDirectory() as scratch:
        service=DurableController(LocalSnapshots(config['SAILBOAT_STATE_DIRECTORY']),Path(scratch)/'recovery')
        if service.private_read('claude_subscription'):raise ValueError('Existing agent consent must not be overwritten')
        service.private_write('claude_subscription',encrypted)
    print('Owner profile stored encrypted; controller remains DISARMED. Preserve keys and private state.')

if __name__=='__main__':main()
