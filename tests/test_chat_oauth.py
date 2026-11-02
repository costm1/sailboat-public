import base64,hashlib,json,time
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient
from sailboat.chat_oauth import ChatOAuth,SCOPE,digest
from sailboat.cloud_server import Authentication,principal
BASE='https://sailboat.test'
CALLBACK='https://chatgpt.com/connector/oauth/test'
OWNER='owner-test-secret';AGENT='agent-test-secret'
class Store:
 def __init__(self):self.values={};self.ready=True
 def private_read(self,n):return self.values.get(n)
 def private_write(self,n,v):self.values[n]=v
@pytest.fixture
def env():
 store=Store();auth=ChatOAuth(store,'encryption-test',BASE)
 async def owner(request):
  if principal.get()['source']!='owner-control':return JSONResponse({},403)
  return JSONResponse(await auth.owner_action(await request.json()))
 async def mcp(request):return JSONResponse(principal.get())
 app=Starlette(routes=auth.routes()+[Route('/owner/chat-connections',owner,methods=['POST']),Route('/mcp',mcp),Route('/owner/control',mcp)])
 keys={digest(OWNER):{'label':'owner','source':'owner-control','conversation':'owner'},digest(AGENT):{'label':'existing','source':'external-agent','conversation':'shared-paper'}}
 with TestClient(Authentication(app,keys,['sailboat.test'],chat_auth=auth),base_url=BASE) as c:yield c,auth,store

def register(c,redirect=CALLBACK,scope=SCOPE):
 return c.post('/register',json={'client_name':'ChatGPT test','redirect_uris':[redirect],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none','scope':scope})
def pending(c):
 client=register(c).json()['client_id'];verifier='x'*64;challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
 r=c.get('/authorize',params={'response_type':'code','client_id':client,'redirect_uri':CALLBACK,'code_challenge':challenge,'code_challenge_method':'S256','scope':SCOPE,'state':'state-value','resource':BASE+'/mcp'},follow_redirects=False)
 assert r.status_code==302
 return client,verifier,r.headers['location']
def approved(c):
 client,verifier,url=pending(c);r=c.get(url);assert 'Owner approval required' in r.text
 rows=c.post('/owner/chat-connections',json={'command':'list'},headers={'Authorization':'Bearer '+OWNER}).json()['pending'];code=rows[-1]['approval_code']
 assert c.post('/owner/chat-connections',json={'command':'approve','approval_code':code},headers={'Authorization':'Bearer '+AGENT}).status_code==403
 assert c.post('/owner/chat-connections',json={'command':'approve','approval_code':code},headers={'Authorization':'Bearer '+OWNER}).json()['arming_granted'] is False
 from urllib.parse import parse_qs,urlsplit
 r=c.get(url,follow_redirects=False);assert r.status_code==303
 q=parse_qs(urlsplit(r.headers['location']).query);assert q['state']==['state-value']
 return {'grant_type':'authorization_code','client_id':client,'code':q['code'][0],'code_verifier':verifier,'redirect_uri':CALLBACK,'resource':BASE+'/mcp'}

def test_discovery_and_existing_auth(env):
 c,a,s=env
 assert c.get('/.well-known/oauth-authorization-server').json()['code_challenge_methods_supported']==['S256']
 assert c.get('/.well-known/oauth-protected-resource/mcp').json()['resource']==BASE+'/mcp'
 assert 'resource_metadata=' in c.get('/mcp').headers['www-authenticate']
 assert c.get('/mcp',headers={'Authorization':'Bearer '+AGENT}).status_code==200
 assert c.get('/owner/control').status_code==401
@pytest.mark.parametrize('uri',['http://chatgpt.com/connector/oauth/test','https://evil.test/connector/oauth/test','https://chatgpt.com.evil.test/connector/oauth/test','https://chatgpt.com/other','https://chatgpt.com/connector/oauth/test#fragment'])
def test_bad_redirect(env,uri):assert register(env[0],uri).status_code==400

def test_scopes_and_pending_no_access(env):
 c,a,s=env
 assert register(c,scope='owner:arm').status_code==400
 _,_,url=pending(c);assert 'Owner approval required' in c.get(url).text
 assert not a.state['codes'] and not a.state['access']
 assert c.get('/mcp',headers={'Authorization':'Bearer '+url.split('ticket=')[1]}).status_code==401

def test_pkce_replay_scope_refresh_restart_and_revoke(env):
 c,a,s=env;form=approved(c)
 assert c.post('/token',data=form|{'resource':'https://evil.test/mcp'}).status_code==400
 assert c.post('/token',data=form|{'code_verifier':'wrong'}).status_code==400
 assert c.post('/token',data=form|{'redirect_uri':'https://chatgpt.com/connector/oauth/wrong'}).status_code==400
 r=c.post('/token',data=form);assert r.status_code==200;t=r.json();headers={'Authorization':'Bearer '+t['access_token']}
 assert c.post('/token',data=form).status_code==400
 assert c.get('/mcp',headers=headers).json()['conversation']=='shared-paper'
 assert c.get('/owner/control',headers=headers).status_code==403
 assert c.post('/owner/chat-connections',headers=headers,json={'command':'list'}).status_code==403
 restored=ChatOAuth(s,'encryption-test',BASE);assert restored.state==a.state
 assert t['access_token'] not in s.values['chat_oauth_v1']
 refresh={'grant_type':'refresh_token','client_id':form['client_id'],'refresh_token':t['refresh_token']}
 assert c.post('/token',data=refresh|{'scope':'owner:arm'}).status_code==400
 r=c.post('/token',data=refresh);assert r.status_code==200;new=r.json()
 assert c.post('/token',data=refresh).status_code==400
 assert c.post('/revoke',data={'client_id':form['client_id'],'token':new['refresh_token']}).status_code==200
 assert c.get('/mcp',headers=headers).status_code==401
 assert c.get('/mcp',headers={'Authorization':'Bearer '+new['access_token']}).status_code==401

def test_expired_tokens_and_restart_refresh(env):
 c,a,s=env;form=approved(c);t=c.post('/token',data=form).json()
 a.state['access'][digest(t['access_token'])]['expires_at']=int(time.time())-1
 assert c.get('/mcp',headers={'Authorization':'Bearer '+t['access_token']}).status_code==401
 import asyncio
 asyncio.run(a.save())
 restored=ChatOAuth(s,'encryption-test',BASE)
 client=asyncio.run(restored.get_client(form['client_id']))
 refresh=asyncio.run(restored.load_refresh_token(client,t['refresh_token']))
 assert refresh
 new=asyncio.run(restored.exchange_refresh_token(client,refresh,[SCOPE]))
 assert asyncio.run(restored.load_access_token(new.access_token))
 restored.state['refresh'][digest(new.refresh_token)]['expires_at']=int(time.time())-1
 from mcp.server.auth.provider import TokenError
 with pytest.raises(TokenError):
  asyncio.run(restored.exchange_refresh_token(client,asyncio.run(restored.load_refresh_token(client,new.refresh_token)),[SCOPE]))
 s.ready=False
 assert asyncio.run(restored.load_access_token(new.access_token)) is None

def test_expired_code_and_owner_revocation(env):
 c,a,s=env;form=approved(c)
 a.state['codes'][digest(form['code'])]['expires_at']=int(time.time())-1
 assert c.post('/token',data=form).status_code==400
 form=approved(c);t=c.post('/token',data=form).json()
 assert c.post('/owner/chat-connections',headers={'Authorization':'Bearer '+OWNER},json={'command':'revoke','client_id':form['client_id']}).status_code==200
 assert c.get('/mcp',headers={'Authorization':'Bearer '+t['access_token']}).status_code==401
 assert c.post('/token',data={'grant_type':'refresh_token','client_id':form['client_id'],'refresh_token':t['refresh_token']}).status_code==400

