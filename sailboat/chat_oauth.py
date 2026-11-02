"""MCP SDK OAuth for manual chats. Owner approval is separate from ARM."""
import asyncio,base64,hashlib,html,json,secrets,time
from urllib.parse import urlsplit,urlencode
import anyio
from cryptography.fernet import Fernet
from pydantic import AnyHttpUrl
from starlette.responses import HTMLResponse,JSONResponse,RedirectResponse
from starlette.routing import Route
from mcp.server.auth.provider import (OAuthAuthorizationServerProvider,AuthorizationCode,AccessToken,RefreshToken,RegistrationError,AuthorizeError,TokenError)
from mcp.server.auth.routes import create_auth_routes,create_protected_resource_routes
from mcp.server.auth.settings import ClientRegistrationOptions,RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull,OAuthToken
SCOPE='sailboat:chat'
def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def redirect_ok(value):
 u=urlsplit(str(value))
 return u.scheme=='https' and u.hostname in {'chatgpt.com','chat.openai.com','claude.ai','claude.com'} and u.port in (None,443) and not u.username and not u.password and not u.fragment and not u.query and (u.path.startswith('/connector/oauth/') or u.path.startswith('/aip/') or u.path=='/api/mcp/auth_callback')
class ChatOAuth(OAuthAuthorizationServerProvider):
 def __init__(self,service,key,base):
  self.service=service;self.base=base.rstrip('/');self.resource=self.base+'/mcp';self.lock=asyncio.Lock()
  self.cipher=Fernet(base64.urlsafe_b64encode(hashlib.sha256(('sailboat-chat-oauth-v1:'+key).encode()).digest()))
  raw=service.private_read('chat_oauth_v1')
  self.state=json.loads(self.cipher.decrypt(raw.encode())) if raw else {'clients':{},'pending':{},'codes':{},'access':{},'refresh':{}}
 async def save(self):
  value=self.cipher.encrypt(json.dumps(self.state).encode()).decode()
  await anyio.to_thread.run_sync(self.service.private_write,'chat_oauth_v1',value)
 def prune(self):
  now=time.time()
  for section in ('pending','codes','access','refresh'):
   self.state[section]={k:v for k,v in self.state[section].items() if v['expires_at']>now}
 async def get_client(self,client_id):
  v=self.state['clients'].get(client_id)
  return OAuthClientInformationFull.model_validate(v) if v else None
 async def register_client(self,client_info):
  if not client_info.redirect_uris or len(client_info.redirect_uris)>4 or not all(redirect_ok(u) for u in client_info.redirect_uris):raise RegistrationError('invalid_redirect_uri','Only supported ChatGPT and Claude HTTPS callbacks are permitted')
  if client_info.token_endpoint_auth_method not in ('none','client_secret_post','client_secret_basic'):raise RegistrationError('invalid_client_metadata','Unsupported client auth method')
  async with self.lock:
   if len(self.state['clients'])>=32:raise RegistrationError('invalid_client_metadata','Owner connection capacity reached')
   self.state['clients'][client_info.client_id]=client_info.model_dump(mode='json');await self.save()
 async def authorize(self,client,params):
  if params.resource not in (None,self.resource):raise AuthorizeError('invalid_request','Wrong resource')
  if not redirect_ok(params.redirect_uri):raise AuthorizeError('invalid_request','Unsupported redirect')
  scopes=params.scopes or [SCOPE]
  if set(scopes)!={SCOPE}:raise AuthorizeError('invalid_scope','Only sailboat:chat is supported')
  async with self.lock:
   self.prune()
   if len(self.state['pending'])>=20:raise AuthorizeError('temporarily_unavailable','Too many pending requests')
   ticket=secrets.token_urlsafe(32);code=secrets.token_hex(6).upper()
   self.state['pending'][digest(ticket)]={'client_id':client.client_id,'client_name':client.client_name or 'Chat client','params':params.model_dump(mode='json'),'scopes':scopes,'approval_code':code,'approved':False,'expires_at':time.time()+900}
   await self.save()
  return self.base+'/chat/consent?ticket='+ticket
 async def load_authorization_code(self,client,authorization_code):
  v=self.state['codes'].get(digest(authorization_code))
  return AuthorizationCode(**v,code=authorization_code) if v and v['client_id']==client.client_id else None
 def issue(self,client_id,scopes):
  now=int(time.time());access=secrets.token_urlsafe(48);refresh=secrets.token_urlsafe(48)
  common={'client_id':client_id,'scopes':scopes,'resource':self.resource,'subject':'sailboat-owner'}
  self.state['access'][digest(access)]=common|{'expires_at':now+3600}
  self.state['refresh'][digest(refresh)]=common|{'expires_at':now+30*86400}
  return OAuthToken(access_token=access,token_type='Bearer',expires_in=3600,refresh_token=refresh,scope=' '.join(scopes))
 async def exchange_authorization_code(self,client,authorization_code):
  async with self.lock:
   v=self.state['codes'].pop(digest(authorization_code.code),None)
   if not v or v['expires_at']<=time.time() or v['client_id']!=client.client_id:raise TokenError('invalid_grant','Code expired or already consumed')
   result=self.issue(client.client_id,v['scopes']);await self.save();return result
 async def load_refresh_token(self,client,refresh_token):
  v=self.state['refresh'].get(digest(refresh_token))
  return RefreshToken(**v,token=refresh_token) if v and v['client_id']==client.client_id else None
 async def exchange_refresh_token(self,client,refresh_token,scopes):
  async with self.lock:
   v=self.state['refresh'].get(digest(refresh_token.token))
   if not v or v['expires_at']<=time.time() or v['client_id']!=client.client_id:raise TokenError('invalid_grant','Refresh token expired or consumed')
   if not set(scopes)<=set(v['scopes']):raise TokenError('invalid_scope','Scope escalation denied')
   self.state['refresh'].pop(digest(refresh_token.token));result=self.issue(client.client_id,scopes);await self.save();return result
 async def load_access_token(self,token):
  v=self.state['access'].get(digest(token))
  if not self.service.ready or not v or v['expires_at']<=time.time() or v['resource']!=self.resource or v['scopes']!=[SCOPE]:return None
  return AccessToken(**v,token=token)
 async def revoke_token(self,token):
  async with self.lock:
   for section in ('access','refresh'):
    self.state[section]={k:v for k,v in self.state[section].items() if v['client_id']!=token.client_id}
   await self.save()
 async def owner_action(self,body):
  async with self.lock:
   self.prune()
   if body=={'command':'list'}:
    return {'pending':[{'approval_code':v['approval_code'],'client_id':v['client_id'],'client_name':v['client_name'],'redirect_uri':v['params']['redirect_uri'],'scope':SCOPE,'expires_at':v['expires_at'],'approved':v['approved']} for v in self.state['pending'].values()]}
   if set(body)=={'command','approval_code'} and body['command']=='approve':
    matches=[v for v in self.state['pending'].values() if secrets.compare_digest(v['approval_code'],body['approval_code'])]
    if len(matches)!=1:raise ValueError('Unknown or expired approval code')
    matches[0]['approved']=True;await self.save()
    return {'status':'APPROVED','client_name':matches[0]['client_name'],'scope':SCOPE,'arming_granted':False}
   if set(body)=={'command','client_id'} and body['command']=='revoke':
    ident=body['client_id']
    for section in ('access','refresh','pending','codes'):
     self.state[section]={k:v for k,v in self.state[section].items() if v['client_id']!=ident}
    await self.save();return {'status':'REVOKED'}
   raise ValueError('Expected list, approve with approval_code, or revoke with client_id')
 async def consent(self,request):
  ticket=request.query_params.get('ticket','')
  async with self.lock:
   v=self.state['pending'].get(digest(ticket))
   if not v or v['expires_at']<=time.time():return HTMLResponse('Connection request expired. Start sign-in again.',status_code=400)
   headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; base-uri 'none'"}
   if v['approved']:
    code=secrets.token_urlsafe(40);p=v['params'];self.state['codes'][digest(code)]={'client_id':v['client_id'],'scopes':v['scopes'],'expires_at':time.time()+300,'code_challenge':p['code_challenge'],'redirect_uri':p['redirect_uri'],'redirect_uri_provided_explicitly':p['redirect_uri_provided_explicitly'],'resource':self.resource,'subject':'sailboat-owner'}
    self.state['pending'].pop(digest(ticket));await self.save()
    query={'code':code}
    if p['state'] is not None:query['state']=p['state']
    return RedirectResponse(p['redirect_uri']+'?'+urlencode(query),status_code=303,headers=headers)
   body='<html><head><title>Connect Sailboat</title><meta http-equiv="refresh" content="5"></head><body><h1>Connect Sailboat</h1><p>Client: '+html.escape(v['client_name'])+'</p><p>Callback: '+html.escape(v['params']['redirect_uri'])+'</p><p>Access: read account and market context, send text and sending-agent intent to the same Sailboat plans used by scheduled Claude, and retrieve receipts. Existing owner authorization and risk checks govern execution. This connection cannot arm or change limits; text transport is not trading authorization. No second model is called.</p><p>Access tokens last one hour; refresh access lasts up to 30 days and can be revoked.</p><h2>Owner approval required</h2><p>Ask Codex to approve this Sailboat chat connection code:</p><pre>'+v['approval_code']+'</pre><p>This page will continue automatically after approval. Approval does not arm Sailboat.</p></body></html>'
   return HTMLResponse(body,headers=headers)
 def routes(self):
  routes=create_auth_routes(self,AnyHttpUrl(self.base),client_registration_options=ClientRegistrationOptions(enabled=True,valid_scopes=[SCOPE],default_scopes=[SCOPE]),revocation_options=RevocationOptions(enabled=True))
  routes+=create_protected_resource_routes(AnyHttpUrl(self.resource),[AnyHttpUrl(self.base)],scopes_supported=[SCOPE],resource_name='Sailboat')
  routes.append(Route('/chat/consent',self.consent,methods=['GET']))
  self.public_paths={r.path for r in routes}
  self.public_rate=[]
  return routes
 async def public(self,app,scope,receive,send):
  from urllib.parse import parse_qs
  now=time.time();self.public_rate=[x for x in self.public_rate if x>now-60]
  if len(self.public_rate)>=120:return await JSONResponse({'error':'slow_down'},429)(scope,receive,send)
  self.public_rate.append(now)
  if scope['method']=='POST':
   body=b''
   while True:
    msg=await receive()
    if msg['type']!='http.request':return
    body+=msg.get('body',b'')
    if len(body)>16384:return await JSONResponse({'error':'request_too_large'},413)(scope,receive,send)
    if not msg.get('more_body'):break
   # SDK 1.30 revocation model requires the nullable field even for public clients.
   if scope['path']=='/revoke' and 'client_secret' not in parse_qs(body.decode(errors='replace'),keep_blank_values=True):
    body+=b'&client_secret='
   if scope['path']=='/token':
    params=parse_qs(body.decode(errors='replace'))
    if 'resource' in params and params['resource']!=[self.resource]:return await JSONResponse({'error':'invalid_target'},400)(scope,receive,send)
   consumed=False
   async def replay():
    nonlocal consumed
    if consumed:return await receive()
    consumed=True;return {'type':'http.request','body':body,'more_body':False}
   return await app(scope,replay,send)
  return await app(scope,receive,send)
