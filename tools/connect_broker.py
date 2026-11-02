"""Owner-invoked OAuth preparation for this instance. Never sends an order."""
import argparse,base64,hashlib,json,os,secrets,time
from pathlib import Path
from urllib.parse import urlencode
import httpx
from sailboat.instance_config import validate_environment
from sailboat.robinhood_readonly import ENDPOINT

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args();config=json.loads(args.config.read_text())
    base=validate_environment(config)['base_url']
    if config.get('RH_OAUTH_BOOTSTRAP'):raise ValueError('Broker setup already exists; do not replace consent state')
    if config.get('SAILBOAT_BROKER_WRITES_DISABLED','1')!='1':raise ValueError('Disable host writes before setup')
    redirect=base+'/oauth/robinhood/callback'
    metadata={'client_name':'Sailboat','redirect_uris':[redirect],'token_endpoint_auth_method':'none',
        'grant_types':['authorization_code','refresh_token'],'response_types':['code']}
    with httpx.Client(timeout=30,follow_redirects=False) as client:
        result=client.post('https://agent.robinhood.com/oauth/trading/register',json=metadata)
        result.raise_for_status();registered=result.json()
    if redirect not in registered.get('redirect_uris',[]):raise ValueError('Provider did not authorize this callback')
    verifier=secrets.token_urlsafe(64);state=secrets.token_urlsafe(40)
    bootstrap={'client_id':registered['client_id'],'redirect_uri':redirect,'verifier':verifier,'state':state,'expires_at':time.time()+1800}
    config['RH_OAUTH_BOOTSTRAP']=json.dumps(bootstrap)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    url='https://robinhood.com/oauth?'+urlencode({'client_id':bootstrap['client_id'],'redirect_uri':redirect,
        'response_type':'code','scope':'internal','state':state,'code_challenge':challenge,'code_challenge_method':'S256','resource':ENDPOINT})
    output=args.config.parent/'broker-consent-url.txt'
    with output.open('x',encoding='utf-8') as f:f.write(url+'\n')
    output.chmod(0o600)
    temp=args.config.with_name(secrets.token_hex(12)+'.tmp')
    with temp.open('x',encoding='utf-8') as f:json.dump(config,f,indent=2);f.flush();os.fsync(f.fileno())
    temp.chmod(0o600);os.replace(temp,args.config)
    print('OAuth preparation saved privately. Restart this instance DISARMED, then have the owner open broker-consent-url.txt within 30 minutes.')

if __name__=='__main__':main()
