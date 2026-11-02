"""Validate deployment identity before touching storage, credentials or networks."""
import json
from urllib.parse import urlsplit

def validate_environment(env):
    base=env.get('SAILBOAT_PUBLIC_URL','').rstrip('/')
    u=urlsplit(base)
    if (u.scheme!='https' or not u.hostname or u.username or u.password
            or u.path or u.query or u.fragment or u.hostname.endswith('.invalid')):
        raise ValueError('SAILBOAT_PUBLIC_URL must be this owner\'s explicit HTTPS origin')
    hosts=env.get('SAILBOAT_ALLOWED_HOSTS','').split(',')
    if u.netloc not in hosts or any(not h or '*' in h or '/' in h for h in hosts):
        raise ValueError('Allowed hosts must explicitly include the public origin')
    keys=json.loads(env.get('SAILBOAT_KEYS_JSON','{}'))
    owner=env.get('SAILBOAT_OWNER_KEY_SHA256','')
    if not keys or not owner or owner in keys:
        raise ValueError('Separate fresh agent and owner credentials required')
    import re
    if any(not re.fullmatch('[a-f0-9]{64}',k) for k in [owner,*keys]):
        raise ValueError('Credential hashes must be SHA256 hex')
    for flag,default in [('SAILBOAT_BROKER_WRITES_DISABLED','1'),('SAILBOAT_CANDIDATE_ENTRY_LIVE','0')]:
        if env.get(flag,default) not in ('0','1'):raise ValueError('Invalid boolean: '+flag)
    storage=env.get('SAILBOAT_STORAGE_BACKEND','local')
    required={'local':['SAILBOAT_STATE_DIRECTORY'],
      'bucket':['SAILBOAT_STATE_NAMESPACE','SAILBOAT_STATE_BUCKET','SAILBOAT_STATE_KEY','SAILBOAT_STATE_S3_ACCESS_KEY','SAILBOAT_STATE_S3_SECRET_KEY'],
      'hub':['SAILBOAT_STATE_REPO','HF_TOKEN']}
    if storage not in required:raise ValueError('Unknown storage backend')
    if any(not env.get(k) for k in required[storage]):raise ValueError('Storage configuration incomplete')
    if storage=='local' and (env.get('SAILBOAT_STATE_BUCKET') or env.get('SAILBOAT_STATE_REPO')):
        raise ValueError('Conflicting local and remote storage configuration')
    if storage=='hub' and env.get('SAILBOAT_STATE_BUCKET'):raise ValueError('Conflicting remote storage configuration')
    if env.get('RH_INITIAL_TOKENS'):
        raise ValueError('Authorize this owner through the broker OAuth flow; broker token migration is disabled')
    oauth=any(env.get(k) for k in ('OAUTH_CLIENT_ID','OAUTH_CLIENT_SECRET','SAILBOAT_OWNER_SUB'))
    if oauth and not all(env.get(k) for k in ('OAUTH_CLIENT_ID','OAUTH_CLIENT_SECRET','SAILBOAT_OWNER_SUB')):
        raise ValueError('Owner OAuth client and verified subject must be configured together')
    if env.get('RH_OAUTH_BOOTSTRAP'):
        bootstrap=json.loads(env['RH_OAUTH_BOOTSTRAP'])
        if bootstrap.get('redirect_uri')!=base+'/oauth/robinhood/callback':
            raise ValueError('Broker callback must belong to this instance')
        if not env.get('RH_ENCRYPTION_KEY'):raise ValueError('Independent broker encryption key required')
    if env.get('SAILBOAT_AGENT_PROVIDER') and not env.get('RH_OAUTH_BOOTSTRAP'):
        raise ValueError('Scheduled analysis needs owner-authorized broker reads')
    return {'base_url':base,'storage':storage}
