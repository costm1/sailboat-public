"""Authenticated simulation tools plus separately owner-armed live decision tools."""
import asyncio,contextvars,copy,hashlib,hmac,json,os
from contextlib import asynccontextmanager
import anyio
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
from mcp.server import Server
from mcp import types
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from .mcp_server import REQUEST_SCHEMA,SUBMIT_SCHEMA,RECEIPT_SCHEMA,SKILL
from .cloud_store import DurableController,HubSnapshots,StorageUnavailable
from .controller import canonical
MARKET_READS=['get_equity_quotes','get_equity_historicals','get_option_chains','get_option_instruments','get_option_quotes','get_option_historicals']
MARKET_READ_SCHEMA={'type':'object','additionalProperties':False,'required':['tool','arguments'],'properties':{'tool':{'type':'string','enum':MARKET_READS},'arguments':{'type':'object'}}}
principal=contextvars.ContextVar('sailboat_principal')

def analysis_context(request,notes=()):
    """Project account evidence for analysis; retain the full request in the controller.

    Authorization remains part of the opaque context hash and submission checks.
    Actual positions, orders and recorded outcomes remain evidence.
    """
    from .analysis_view import project_analysis
    result=project_analysis(request,notes)
    result['analysis_instructions']='Assess the market, holdings and opportunities from buying power, positions, market evidence and prior reasoning. Controller permission checks happen after submission; recorded rejection outcomes are operational feedback, not market signals. Use the same evidentiary standards for every assessment. Keep your chosen decision separate from its eventual execution receipt.'
    result['submission_instructions']='For a Sailboat market or portfolio assessment, research then submit one chosen structured decision through sailboat_live_submit and report its receipt, including observe or hold when no order is warranted. Honor explicit research-only, analysis-only, do-not-submit or no-orders restrictions. Simple quote, status, holdings, explanation and receipt requests are read-only. Do not require a second prompt solely for submission. Follow host confirmation requirements. Use exactly the returned schema; refresh expired context, retain plan identities, never invent missing terms, and recover uncertain submissions by their original receipt. Do not infer or report controller ARM state from this analysis context. Submission does not change owner controls.'
    return result

class Authentication:
    def __init__(self,app,keys,hosts,oauth_callback=False,chat_auth=None,owner_panel=None):
        self.app=app;self.keys=keys;self.hosts=hosts;self.oauth_callback=oauth_callback;self.chat_auth=chat_auth;self.owner_panel=owner_panel
        if not keys or any(len(k)!=64 for k in keys):raise ValueError('configure SHA256 hashes of strong client tokens')
        for p in keys.values():
            if set(p)!={'label','source','conversation'}:raise ValueError('invalid principal mapping')
    async def __call__(self,scope,receive,send):
        if scope['type']!='http':return await self.app(scope,receive,send)
        headers=dict(scope['headers']);host=headers.get(b'host',b'').decode()
        if host not in self.hosts:return await JSONResponse({'error':'invalid host'},400)(scope,receive,send)
        if self.owner_panel and scope['path'] in self.owner_panel.paths:
            return await self.app(scope,receive,send)
        if self.chat_auth and scope['path'] in self.chat_auth.public_paths:
            return await self.chat_auth.public(self.app,scope,receive,send)
        origin=headers.get(b'origin')
        if origin and origin.decode() not in {'https://'+h for h in self.hosts}:
            return await JSONResponse({'error':'origin denied'},403)(scope,receive,send)
        if self.oauth_callback and scope['path']=='/oauth/robinhood/callback':
            return await self.app(scope,receive,send)
        auth=headers.get(b'authorization',b'').decode()
        digest=hashlib.sha256(auth[7:].encode()).hexdigest() if auth.startswith('Bearer ') else ''
        selected=None
        for key,p in self.keys.items():
            if hmac.compare_digest(key,digest):selected=p
        if selected is None and self.chat_auth and auth.startswith('Bearer '):
            access=await self.chat_auth.load_access_token(auth[7:])
            if access:
                if scope['path']!='/mcp':return await JSONResponse({'error':'chat token is limited to MCP'},403)(scope,receive,send)
                selected={'label':'chat:'+access.client_id,'source':'external-agent','conversation':'shared-paper','interface':'text'}
        if selected is None:
            challenge='Bearer realm="sailboat"'
            if self.chat_auth:challenge+=', resource_metadata="'+self.chat_auth.base+'/.well-known/oauth-protected-resource/mcp", scope="sailboat:chat"'
            return await JSONResponse({'error':'authentication required'},401,headers={'WWW-Authenticate':challenge})(scope,receive,send)
        token=principal.set(selected)
        try:await self.app(scope,receive,send)
        finally:principal.reset(token)

def make_app(service,keys,hosts,monitor_interval=1,broker=None,scheduler=None,owner_key_hash=None,execution=None,chat_auth=None,owner_panel=None,text_adapter=None):
    from .robinhood_orders import PreparedLiveControls
    execution=execution or PreparedLiveControls(service)
    from .shared_chat import SharedChat,TOOLS as CHAT_TOOLS,SKILL as CHAT_SKILL
    shared_chat=text_adapter or SharedChat(service,execution)
    shape=None
    if text_adapter is None and hasattr(execution,'journal'):
        from .shape_runtime import ShapeRuntime
        shape=ShapeRuntime(execution,candidate_live_enabled=os.environ.get('SAILBOAT_CANDIDATE_ENTRY_LIVE','0')=='1');execution.shape_runtime=shape
    unified=text_adapter is None
    keys=dict(keys)
    if owner_key_hash:
        if owner_key_hash in keys:raise ValueError('owner credential must be separate from agent keys')
        keys[owner_key_hash]={'label':'owner-control','source':'owner-control','conversation':'owner'}
    server=Server('sailboat-cloud',version='1.0.0',instructions='Use only the tools exposed to your authenticated connection. Chat and scheduled agents use the same account and controller. Send text and faithful intent; no second model. A receipt is not a fill or independent execution authorization. No tool can arm or change limits.')
    @server.list_tools()
    async def list_tools():
        if principal.get().get('interface')=='text':
            tools=CHAT_TOOLS if unified else text_adapter.tools
            items=[types.Tool(name=n,description=d,inputSchema=s,annotations=types.ToolAnnotations(readOnlyHint=n.endswith('receipt'),destructiveHint=not n.endswith('receipt'),idempotentHint=True,openWorldHint=True)) for n,d,s in tools]
            if unified and hasattr(execution,'context'):items.append(types.Tool(name='sailboat_market_read',description='Read market quotes, option contracts and history through the shared account connection; no orders.',inputSchema=MARKET_READ_SCHEMA,annotations=types.ToolAnnotations(readOnlyHint=True)))
            return items
        items=[types.Tool(name=n,description=d,inputSchema=s) for n,d,s in [
          ('sailboat_get_context','Get fresh owned simulated state and complete decision contract. May process existing conditions.',REQUEST_SCHEMA),
          ('sailboat_submit_decision','Validate and durably commit a structured simulated decision. Exact retry is idempotent.',SUBMIT_SCHEMA),
          ('sailboat_get_receipt','Recover the durable receipt for an owned request.',RECEIPT_SCHEMA)]]
        if hasattr(execution,'journal'):items=[types.Tool(name=n,description=d,inputSchema=s) for n,d,s in CHAT_TOOLS] # One shared account; historical paper book is read-only.
        if hasattr(execution,'context'):
            from .live_controls import LIVE_REPLY_SCHEMA
            items.extend([types.Tool(name='sailboat_live_context',description='Get current account holdings, plans, market evidence, prior assessments and the decision contract for analysis.',inputSchema=REQUEST_SCHEMA),types.Tool(name='sailboat_live_submit',description='Submit a version-bound explicit live instruction; cannot arm or change limits.',inputSchema=LIVE_REPLY_SCHEMA),types.Tool(name='sailboat_live_receipt',description='Recover original live decision receipt.',inputSchema=RECEIPT_SCHEMA)])
        if hasattr(execution,'context'):items.append(types.Tool(name='sailboat_market_read',description='Read supported quotes/chains/contracts through Sailboat; no orders or account changes.',inputSchema=MARKET_READ_SCHEMA))
        return items
    @server.list_resources()
    async def list_resources():
        if principal.get().get('interface')=='text':
            return [types.Resource(uri='sailboat://text-channel',name='Sailboat text channel',mimeType='text/markdown')]
        resources=[types.Resource(uri='sailboat://skill',name='Sailboat Skill',mimeType='text/markdown')]
        if hasattr(execution,'context'):resources.append(types.Resource(uri='sailboat://order-agent',name='Sailboat shared order-agent prompt',mimeType='text/markdown'))
        return resources
    @server.read_resource()
    async def read_resource(uri):
        if principal.get().get('interface')=='text':
            if str(uri)!='sailboat://text-channel':raise ValueError('Resource is not available on the text channel')
            return CHAT_SKILL if unified else text_adapter.skill
        if str(uri)=='sailboat://order-agent' and hasattr(execution,'context'):
            from .agent_contract import PROMPT
            return PROMPT
        if str(uri)!='sailboat://skill':raise ValueError('unknown resource')
        return CHAT_SKILL if hasattr(execution,'journal') else SKILL.read_text()
    @server.call_tool()
    async def call_tool(name,arguments):
        try:
            p=principal.get()
            if (p.get('interface')=='text' and name!='sailboat_market_read') or (unified and name in ('sailboat_send_message','sailboat_message_receipt')):
                if name not in ('sailboat_send_message','sailboat_message_receipt'):raise ValueError('Chat connections support text transport only')
                result=await shared_chat.exchange(p,name,arguments)
            elif name.startswith('sailboat_live_') or name=='sailboat_market_read':
                p=principal.get()
                if not (p['source']=='external-agent' and p['conversation']=='shared-paper'):raise ValueError('live account access denied')
                if not hasattr(execution,'context'):raise ValueError('live interface not configured')
                if name=='sailboat_market_read':
                    if p.get('interface')=='text' and not unified:raise ValueError('Chat connections support text transport only')
                    if arguments['tool'] not in MARKET_READS:raise ValueError('market read tool denied')
                    result=await execution.broker.readonly.read(arguments['tool'],arguments['arguments'],persist=False)
                elif name=='sailboat_live_context':result=analysis_context(await execution.context(arguments['operator_request']),execution.worker.status().get('agent_assessments',[]))
                elif name=='sailboat_live_submit':
                    from .shared_assessment import submit
                    req=execution.request_payload(arguments['request_id'])
                    result=await submit(execution,req,arguments,source='agent:'+p['label'])
                elif name=='sailboat_live_receipt':result=await anyio.to_thread.run_sync(execution.receipt,arguments['request_id'])
                else:raise ValueError('unknown live tool')
            else:
                if hasattr(execution,'journal') and name in ('sailboat_get_context','sailboat_submit_decision'):raise ValueError('Use the shared account interface; legacy paper book is historical only')
                result=await anyio.to_thread.run_sync(service.execute,principal.get(),name,arguments)
        except (ValueError,KeyError) as exc:
            result={'status':'REJECTED','reason':str(exc)[:500]}
        except StorageUnavailable:
            result={'status':'UNAVAILABLE','reason':'Durability uncertain. Obtain the original receipt after recovery; do not invent a replacement trade.'}
        rejected=result.get('status') in ('REJECTED','UNAVAILABLE') or result.get('result',{}).get('status')=='REJECTED'
        return types.CallToolResult(content=[types.TextContent(type='text',text=canonical(result))],structuredContent=result,isError=rejected)
    manager=StreamableHTTPSessionManager(server,json_response=True,stateless=True,max_request_body_size=65536,
      security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,allowed_hosts=hosts,allowed_origins=['https://'+h for h in hosts]))
    async def monitor():
        while True:
            await asyncio.sleep(monitor_interval)
            if service.ready:
                try:await anyio.to_thread.run_sync(service.monitor)
                except StorageUnavailable:pass
    @asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            tasks=[asyncio.create_task(monitor())]
            if shape:tasks.append(asyncio.create_task(shape.run()))
            if scheduler:tasks.extend([asyncio.create_task(scheduler.run()),asyncio.create_task(scheduler.monitor())])
            if hasattr(execution,'run'):tasks.append(asyncio.create_task(execution.run()))
            try:yield
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                if broker and hasattr(broker,'close'):await broker.close()
    async def status(request):return JSONResponse(service.metrics(),status_code=200 if service.ready else 503)
    app=Starlette(routes=[Route('/mcp',endpoint=manager.handle_request,methods=['POST','GET','DELETE']),Route('/status',status)],lifespan=lifespan)
    # Starlette function routes expect Request; use a callable ASGI instance for MCP.
    class MCPRoute:
        async def __call__(self,scope,receive,send):await manager.handle_request(scope,receive,send)
    app.router.routes[0]=Route('/mcp',endpoint=MCPRoute(),methods=['POST','GET','DELETE'])
    if broker:
        def broker_owner():
            p=principal.get()
            return p['source']=='external-agent' and p['conversation']=='shared-paper'
        async def broker_status(request):
            if not broker_owner():return JSONResponse({'error':'portfolio access denied'},403)
            return JSONResponse(await broker.status())
        async def broker_read(request):
            if not broker_owner():return JSONResponse({'error':'portfolio access denied'},403)
            return await broker.read_endpoint(request)
        app.router.routes.extend([Route('/oauth/robinhood/callback',broker.callback),Route('/broker/status',broker_status),Route('/broker/read',broker_read,methods=['POST'])])
    if scheduler:
        async def scheduler_status(request):
            p=principal.get()
            if p['source']!='external-agent' or p['conversation']!='shared-paper':return JSONResponse({'error':'access denied'},403)
            return JSONResponse(await scheduler.status())
        app.router.routes.append(Route('/scheduler/status',scheduler_status))
    async def execution_status(request):
        p=principal.get()
        if not (p['source']=='owner-control' or (p['source']=='external-agent' and p['conversation']=='shared-paper')):return JSONResponse({'error':'access denied'},403)
        result=await execution.refresh() if hasattr(execution,'refresh') else execution.status()
        return JSONResponse(result)
    async def owner_control(request):
        if principal.get()['source']!='owner-control':return JSONResponse({'error':'owner credential required'},403)
        try:
            body=await request.json()
            if set(body)!={'command'} or body['command'] not in {'status','arm','disarm'}:raise ValueError('expected status, arm or disarm')
            if body['command']=='arm':result=await execution.arm()
            elif body['command']=='disarm':result=await anyio.to_thread.run_sync(execution.disarm)
            else:result=await execution.refresh() if hasattr(execution,'refresh') else execution.status()
            return JSONResponse(result)
        except ValueError as exc:return JSONResponse({'error':str(exc),'mode':'DISARMED'},409)
    app.router.routes.extend([Route('/execution/status',execution_status),Route('/owner/control',owner_control,methods=['POST'])])
    if chat_auth:
        app.router.routes.extend(chat_auth.routes())
        async def chat_owner(request):
            if principal.get()['source']!='owner-control':return JSONResponse({'error':'owner credential required'},403)
            try:return JSONResponse(await chat_auth.owner_action(await request.json()))
            except ValueError as exc:return JSONResponse({'error':str(exc)},400)
        app.router.routes.append(Route('/owner/chat-connections',chat_owner,methods=['POST']))
    if owner_panel:app.router.routes.extend(owner_panel.routes())
    return Authentication(app,keys,hosts,oauth_callback=broker is not None,chat_auth=chat_auth,owner_panel=owner_panel)

def main():
    import uvicorn
    from .instance_config import validate_environment
    config=validate_environment(os.environ)
    keys=json.loads(os.environ['SAILBOAT_KEYS_JSON'])
    hosts=os.environ['SAILBOAT_ALLOWED_HOSTS'].split(',')
    if config['storage']=='local':
        from .local_store import LocalSnapshots
        store=LocalSnapshots(os.environ['SAILBOAT_STATE_DIRECTORY'])
    elif os.environ.get('SAILBOAT_STATE_BUCKET'):
        from .bucket_store import BucketSnapshots
        storage_mode=os.environ.get('SAILBOAT_STORAGE_MODE','delta-v1')
        if storage_mode not in ('snapshot-v1','delta-v1'):raise ValueError('Unknown Sailboat storage mode')
        if storage_mode=='delta-v1':
            from .delta_store import DeltaBucketSnapshots
            backend=DeltaBucketSnapshots
        else:backend=BucketSnapshots
        store=backend(os.environ['SAILBOAT_STATE_BUCKET'],os.environ['SAILBOAT_STATE_KEY'],namespace=os.environ['SAILBOAT_STATE_NAMESPACE'],
            access_key=os.environ['SAILBOAT_STATE_S3_ACCESS_KEY'],secret_key=os.environ['SAILBOAT_STATE_S3_SECRET_KEY'])
    else:store=HubSnapshots(os.environ['SAILBOAT_STATE_REPO'],os.environ['HF_TOKEN'])
    service=DurableController(store)
    service.broker_writes_disabled=os.environ.get('SAILBOAT_BROKER_WRITES_DISABLED','1')=='1'
    if os.environ.get("SAILBOAT_DAILY_LOSS_LIMIT"):
        service.scheduler_transaction(lambda c,s:c.configure_daily_loss_limit(os.environ["SAILBOAT_DAILY_LOSS_LIMIT"]))
    broker=None
    if os.environ.get('RH_OAUTH_BOOTSTRAP'):
        from .robinhood_readonly import RobinhoodReadOnly
        broker=RobinhoodReadOnly(service,json.loads(os.environ['RH_OAUTH_BOOTSTRAP']),os.environ['RH_ENCRYPTION_KEY'])
        # Owner-approved one-time encrypted credential import; no mode changes.
        initial=os.environ.get('RH_INITIAL_TOKENS')
        if initial and not service.private_read('robinhood_tokens'):
            imported=json.loads(broker.cipher.decrypt(initial.encode()))
            if not isinstance(imported.get('access_token'),str) or not imported.get('access_token') or not isinstance(imported.get('expires_at'),(int,float)):
                raise ValueError('invalid encrypted broker credential import')
            service.private_write('robinhood_tokens',initial)
    scheduler=None
    selected=os.environ.get('SAILBOAT_AGENT_PROVIDER')
    if selected:
        if not broker:raise ValueError('scheduler requires authenticated broker reads')
        from .agent_provider import ClaudeSubscription,HTTPSAgent
        from .scheduler import Scheduler
        if selected=='claude_subscription':
            initial=os.environ.get('SAILBOAT_CLAUDE_INITIAL')
            if initial and not service.private_read('claude_subscription'):
                # Freshly owner-authorized profile only; never another app's credential cache.
                service.private_write('claude_subscription',initial)
            provider=ClaudeSubscription(service,os.environ['SAILBOAT_AGENT_KEY'],os.environ.get('SAILBOAT_AGENT_MODEL','sonnet'),broker=broker)
        elif selected=='https_agent':provider=HTTPSAgent(os.environ['SAILBOAT_AGENT_URL'],os.environ.get('SAILBOAT_AGENT_TOKEN'))
        else:raise ValueError('unsupported agent provider')
        from .market_hours import RegularMarketHours
        hours=os.environ.get('SAILBOAT_MARKET_HOURS','')
        if hours not in ('','NYSE_REGULAR'):raise ValueError('unsupported market calendar')
        # One owner-configured cadence for entry research and held-position analysis.
        agent_interval=int(os.environ.get('SAILBOAT_AGENT_INTERVAL','120'))
        scheduler=Scheduler(service,broker,provider,market_hours=RegularMarketHours() if hours else None,interval=agent_interval,held_interval=agent_interval,
            symbols=tuple(os.environ.get('SAILBOAT_AGENT_SYMBOLS','QQQ,SPY,SMH').split(',')),
            max_cycles=int(os.environ.get('SAILBOAT_AGENT_MAX_CYCLES','0')),
            not_before=float(os.environ.get('SAILBOAT_AGENT_NOT_BEFORE','0')),
            stop_at=float(os.environ.get('SAILBOAT_AGENT_STOP_AT','0')),
            event_wakeups=os.environ.get('SAILBOAT_AGENT_EVENT_WAKEUPS','1')=='1')
    execution=None
    if broker and os.environ.get('SAILBOAT_BROKER_ACCOUNT'):
        from .live_controls import LiveControls
        from dataclasses import asdict
        limits=service.scheduler_transaction(lambda c,s:asdict(c.limits))
        execution=LiveControls(service,broker,os.environ['SAILBOAT_BROKER_ACCOUNT'],limits)
    if scheduler and execution:
        scheduler.execution=execution;execution.scheduler_connected=True
    chat_auth=None
    if os.environ.get('SAILBOAT_AGENT_KEY') and os.environ.get('SAILBOAT_OWNER_KEY_SHA256'):
        from .chat_oauth import ChatOAuth
        chat_auth=ChatOAuth(service,os.environ['SAILBOAT_AGENT_KEY'],config['base_url'])
    from .owner_panel import OwnerPanel
    from .robinhood_orders import PreparedLiveControls as PreparedLiveControlsForPanel
    panel=OwnerPanel(execution or PreparedLiveControlsForPanel(service),scheduler,service,os.environ.get('OAUTH_CLIENT_ID',''),os.environ.get('OAUTH_CLIENT_SECRET',''),owner_sub=os.environ.get('SAILBOAT_OWNER_SUB',''),base=config['base_url'])
    uvicorn.run(make_app(service,keys,hosts,broker=broker,scheduler=scheduler,owner_key_hash=os.environ.get('SAILBOAT_OWNER_KEY_SHA256'),execution=execution,chat_auth=chat_auth,owner_panel=panel),host=os.environ.get('SAILBOAT_BIND_HOST','127.0.0.1'),port=int(os.environ.get('PORT','7860')),access_log=False,workers=1)
if __name__=='__main__':main()
