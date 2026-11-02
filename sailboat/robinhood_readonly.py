"""Official Robinhood MCP only. A closed allowlist excludes every account mutation."""
import asyncio,json,time,hmac
import httpx,anyio
from cryptography.fernet import Fernet
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from jsonschema import validate,validators
from starlette.responses import JSONResponse
ENDPOINT='https://agent.robinhood.com/mcp/trading'
TOKEN_ENDPOINT='https://api.robinhood.com/oauth2/token/'
READ_TOOLS=frozenset({'get_accounts','get_portfolio','get_equity_quotes','get_equity_historicals',
 'get_option_chains','get_option_instruments','get_option_quotes','get_option_historicals',
 'get_equity_positions','get_option_positions','get_equity_orders','get_option_orders',
 'get_realized_pnl','get_trade_approval_setting','get_trade_approvals'})

class RobinhoodReadOnly:
    def __init__(self,service,bootstrap,encryption_key):
        self.service=service;self.bootstrap=bootstrap;self.cipher=Fernet(encryption_key.encode());self.lock=asyncio.Lock()
        self.schemas={};self.last_error=None
        self._session=None
        self.transport_metrics={'connections':0,'tool_calls':0,'connection_seconds':0.,'call_seconds':0.}
    async def _serve_session(self,entry):
        """One task owns the MCP context managers for their entire lifetime."""
        started=time.perf_counter()
        try:
            async with streamablehttp_client(ENDPOINT,headers={'Authorization':'Bearer '+entry['token']},timeout=30) as (r,w,_):
                async with ClientSession(r,w) as client:
                    await client.initialize();listed=await client.list_tools()
                    self.transport_metrics['connections']+=1
                    self.transport_metrics['connection_seconds']+=time.perf_counter()-started
                    if not entry['ready'].done():entry['ready'].set_result((client,{t.name:t for t in listed.tools}))
                    await entry['stop'].wait()
        except BaseException as exc:
            if not entry['ready'].done():
                if isinstance(exc,asyncio.CancelledError):entry['ready'].cancel()
                else:entry['ready'].set_exception(exc)
            raise
    async def _connected(self):
        async with self.lock:
            token=await self.token()
            entry=self._session
            if entry and (entry['token']!=token or entry['task'].done() or entry['task'].get_loop() is not asyncio.get_running_loop()):
                entry['stop'].set();self._session=None
                if entry['task'].get_loop() is asyncio.get_running_loop():
                    await asyncio.gather(entry['task'],return_exceptions=True)
                entry=None
            if entry is None:
                entry={'token':token,'ready':asyncio.get_running_loop().create_future(),'stop':asyncio.Event()}
                entry['task']=asyncio.create_task(self._serve_session(entry));self._session=entry
        try:
            client,tools=await asyncio.shield(entry['ready'])
            if entry['task'].done():raise ValueError('Broker session closed; fresh connection required')
            return entry,client,tools
        except BaseException:
            await self._disconnect(entry)
            raise
    async def _disconnect(self,entry):
        if self._session is entry:self._session=None
        entry['stop'].set()
        if entry['task'].get_loop() is asyncio.get_running_loop():
            entry['task'].cancel()
            await asyncio.gather(entry['task'],return_exceptions=True)
    async def close(self):
        if self._session:await self._disconnect(self._session)
    async def _call(self,name,args,*,permit=None):
        # No automatic transport retry: an uncertain write belongs to the existing
        # order worker's persisted identity and reconciliation path.
        if permit is not None and permit(name,args) is not True:raise ValueError('DISARMED: brokerage mutation pathway blocked')
        entry,client,tools=await self._connected()
        if name not in tools:raise ValueError('required broker tool is not offered')
        tool=tools[name];validate(args,tool.inputSchema)
        if permit is not None and permit(name,args) is not True:raise ValueError('owner disarmed during broker initialization')
        self.schemas={n:t.inputSchema for n,t in tools.items() if n in READ_TOOLS}
        started=time.perf_counter()
        try:
            result=await client.call_tool(name,args)
            raw=result.model_dump(mode='json',exclude_none=True)
            if permit is not None and getattr(tool,'outputSchema',None) and raw.get('structuredContent') is not None:
                validator=validators.validator_for(tool.outputSchema)(tool.outputSchema)
                for error in validator.iter_errors(raw['structuredContent']):
                    path=list(error.absolute_path)
                    # Owner policy makes only review fees informational. All account,
                    # terms, approval, alert and order-status checks remain mandatory.
                    fee_only=(path[:2]==['data','fees'] or (path==['data'] and error.validator=='required' and error.message=="'fees' is a required property"))
                    if name!='review_option_order' or not fee_only:raise error
            return raw
        except BaseException:
            await self._disconnect(entry)
            raise
        finally:
            self.transport_metrics['tool_calls']+=1;self.transport_metrics['call_seconds']+=time.perf_counter()-started
    async def _load(self):
        value=await anyio.to_thread.run_sync(self.service.private_read,'robinhood_tokens')
        return json.loads(self.cipher.decrypt(value.encode())) if value else None
    async def _save(self,tokens):
        encrypted=self.cipher.encrypt(json.dumps(tokens).encode()).decode()
        await anyio.to_thread.run_sync(self.service.private_write,'robinhood_tokens',encrypted)
    async def token(self):
        if not self.service.ready:raise ValueError("controller storage unavailable")
        tokens=await self._load()
        if not tokens:raise ValueError('Robinhood account consent not completed')
        if tokens['expires_at']>time.time()+60:return tokens['access_token']
        if not tokens.get('refresh_token'):raise ValueError('Robinhood reauthorization required; no refresh token')
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
            r=await client.post(TOKEN_ENDPOINT,data={'grant_type':'refresh_token','refresh_token':tokens['refresh_token'],'client_id':self.bootstrap['client_id'],'resource':ENDPOINT})
        if r.status_code!=200:raise ValueError('Robinhood refresh rejected; reauthorization required')
        fresh=r.json();fresh.setdefault('refresh_token',tokens['refresh_token'])
        fresh['expires_at']=time.time()+float(fresh['expires_in'])
        await self._save(fresh)
        return fresh['access_token']
    async def callback(self,request):
        q=request.query_params
        if not hmac.compare_digest(q.get('state',''),self.bootstrap['state']) or time.time()>self.bootstrap['expires_at']:
            return JSONResponse({'error':'invalid or expired OAuth state'},400)
        if q.get('iss') and q['iss']!=ENDPOINT:return JSONResponse({'error':'issuer mismatch'},400)
        if q.get('error'):return JSONResponse({'error':'Robinhood consent was not granted'},400)
        if not q.get('code'):return JSONResponse({'error':'authorization code missing'},400)
        async with self.lock:
            if await self._load():return JSONResponse({'error':'authorization already completed'},409)
            try:
                async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
                    r=await client.post(TOKEN_ENDPOINT,data={'grant_type':'authorization_code','client_id':self.bootstrap['client_id'],
                      'redirect_uri':self.bootstrap['redirect_uri'],'code':q['code'],'code_verifier':self.bootstrap['verifier'],'resource':ENDPOINT})
                if r.status_code!=200:return JSONResponse({'error':'Robinhood token exchange rejected','status':r.status_code},400)
                tokens=r.json();tokens['expires_at']=time.time()+float(tokens['expires_in'])
                await self._save(tokens)
                return JSONResponse({'status':'Sailboat connected','brokerage_access':'read tools only','execution':'simulation only'})
            except Exception:
                return JSONResponse({'error':'authorization could not be durably saved; owner recovery required'},503)
    async def read(self,name,args,*,persist=True):
        if name not in READ_TOOLS:raise ValueError('tool is not on the immutable read-only allowlist')
        data=await self._call(name,args)
        observation={'source':ENDPOINT,'tool':name,'arguments':args,'retrieved_at':time.time(),'result':data,'execution':'not a broker fill'}
        if persist:await anyio.to_thread.run_sync(self.service.observation_write,name,observation)
        return observation
    async def status(self):
        t=await self._load()
        return {'connected':bool(t),'access_expires_at':t.get('expires_at') if t else None,
          'refresh_token_present':bool(t and t.get('refresh_token')),'allowed_tools':sorted(READ_TOOLS),'discovered_schemas':self.schemas,'last_error':self.last_error,
          'transport':dict(self.transport_metrics)}
    async def read_endpoint(self,request):
        try:
            value=await request.json()
            if set(value)!={'tool','arguments'}:raise ValueError('expected tool and arguments')
            return JSONResponse(await self.read(value['tool'],value['arguments']))
        except Exception as exc:
            self.last_error=type(exc).__name__
            return JSONResponse({'error':'read-only brokerage request failed','kind':type(exc).__name__},400)
