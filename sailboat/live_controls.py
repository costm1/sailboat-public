"""Owner-only arming and provider-independent live decision/context bridge.
Scheduled routing is attached explicitly by the host; owner mode remains authoritative.
"""
import asyncio,copy,hashlib,json,time,uuid
from .controller import canonical
from .order_execution import Journal,OrderWorker,ORDER_CONTRACT,TERMINAL,validate_plan
from .live_broker import LiveBroker
from . import position_thesis

LIVE_REPLY_SCHEMA={'type':'object','additionalProperties':False,'required':['request_id','context_hash','action','plan','plan_id','plan_version'],
 'properties':{'request_id':{'type':'string'},'context_hash':{'type':'string'},'action':{'enum':['execute','cancel','hold','observe']},
 'plan':{'anyOf':[{'type':'null'},ORDER_CONTRACT]},'plan_id':{'type':['string','null']},'plan_version':{'type':['integer','null']}}}

LIVE_REPLY_SCHEMA['properties']['position_blocks']=position_thesis.BLOCKS_SCHEMA

class LoopBroker:
 """Keep all authenticated HTTP/OAuth locks on one event loop, DB writes in worker thread."""
 def __init__(self,broker):self.broker=broker;self.loop=None
 def __getattr__(self,name):
  async def call(*args,**kw):
   if self.loop is None or asyncio.get_running_loop() is self.loop:return await getattr(self.broker,name)(*args,**kw)
   future=asyncio.run_coroutine_threadsafe(getattr(self.broker,name)(*args,**kw),self.loop)
   return await asyncio.wrap_future(future)
  return call

class LiveControls:
 def __init__(self,service,readonly,account_number,limits,clock=time.time):
  self.service=service;self.clock=clock;self.journal=Journal(service)
  self.broker=LiveBroker(readonly,self.journal,account_number,limits,clock=clock)
  self.proxy=LoopBroker(self.broker);self.worker=OrderWorker(self.journal,self.proxy,clock=clock,boot_id=service.boot_id,timeout=90)
  self.broker.worker=self.worker;self.last_readiness={'ready':False,'blockers':['Read-only preflight has not completed']};self.last_preflight=0
  self.bridge_lock=asyncio.Lock();self.assessment_lock=asyncio.Lock();self.scheduler_connected=False
  from .adaptive_profit import AdaptiveProfitManager,ProfitPort
  self.profit_manager=AdaptiveProfitManager(self.journal,self.worker,ProfitPort(self.broker,clock),clock)
  from .scheduled_close import ScheduledClose
  self.profit_manager.scheduled_close=ScheduledClose(self)
 def status(self):
  s=self.worker.status();v=self.broker.view
  fresh=v is not None and 0<=self.clock()-v['observed_at']<=30
  blockers=list(self.last_readiness.get('blockers',[]))
  if not fresh:blockers.append('Fresh read-only preflight required')
  from .assembly_bridge import VERSION as ASSEMBLY_VERSION,ENTRY_POLICY
  from .owner_settings import settings,revision
  return {'mode':s['mode'],'candidate_entry_live_enabled':bool(getattr(getattr(self,'shape_runtime',None),'candidate_entry',None) and self.shape_runtime.candidate_entry.allow_execution),'live_ready':fresh and self.last_readiness.get('ready',False),
   'owner_entry_minimum_probability':settings(s)['minimum_probability'],'owner_settings_revision':revision(s),
   'owner_operating_mode':s.get('owner_operating_mode','automatic'),'owner_session_expires_at':s.get('owner_session',{}).get('expires_at'),
    'assembly_integration':{'version':ASSEMBLY_VERSION,'engine':'sailboat_assembly','selection_policy':ENTRY_POLICY,'economic_edge_rule':ENTRY_POLICY},
   'broker_write_calls_enabled':s['mode']=='LIVE_ARMED' and self.service.live_guard() and not getattr(self.service,'broker_writes_disabled',False),
   'broker_writes_disabled_by_host':getattr(self.service,'broker_writes_disabled',False),
   'account':'****'+self.broker.account_number[-4:],'blockers':blockers,'daily_risk':v['risk'] if v else None,
    'entries_allowed_after_arm':fresh and v['risk']['available'] and not v['risk']['entry_locked'] and not getattr(self.service,'broker_writes_disabled',False) if v else False,
   'limits':self.broker.limits,'open_plans':sum(r['status'] not in TERMINAL for r in s['orders'].values()),
   'order_types':['limit','stop_limit'],'last_preflight_at':self.last_preflight,
   'scheduler_live_enabled':self.scheduler_connected and s['mode']=='LIVE_ARMED','scheduler_connected':self.scheduler_connected,'broker_fill_tested':False,
   'hosting':'Free host can sleep; monitoring/cancellation resumes on wake; boot always disarms.'}
 async def refresh(self):
  self.last_readiness=await self.broker.readiness();self.last_preflight=self.clock()
  return self.status()
 async def arm(self,expected_epoch=None,state_update=None):
  if getattr(self.service,'broker_writes_disabled',False):raise ValueError('Brokerage writes disabled by host; owner reactivation required')
  async with self.bridge_lock:
   epoch=self.worker.status().get('control_epoch',0) if expected_epoch is None else expected_epoch
   await self.refresh()
   if self.worker.status()['mode']=='LIVE_ARMED':
    def update_armed(s):
     if s.get('control_epoch',0)!=epoch:raise ValueError('Owner controls changed during arm checks')
     if state_update:state_update(s)
    await asyncio.to_thread(self.journal.edit,update_armed)
    return self.status()
   if not self.last_readiness['ready']:raise ValueError('LIVE_NOT_READY: '+'; '.join(self.last_readiness['blockers']))
   # Only this owner-authenticated route reaches reset. Agent routes cannot set modes or limits.
   def reset(c,s):c.reset_emergency_stop();c._event('OWNER_LIVE_ARM_REQUEST',{'account_suffix':self.broker.account_number[-4:]})
   self.service.scheduler_transaction(reset)
   result=await self.worker.arm(expected_epoch=epoch,state_update=state_update)
   return result|{'scope':'next fresh scheduled assessment and explicit live instructions' if self.scheduler_connected else 'explicit live instructions only; scheduler not connected'}
 def disarm(self):
  self.worker.disarm()
  self.service.scheduler_transaction(lambda c,s:c.emergency_stop())
  return self.status()
 def _digest(self,v,s):
  stable={'account':self.broker.account_number,'positions':[(p['position_id'],p['quantity'],p['unit_cost'],p.get('opened_at')) for p in v['positions']],
   'orders':[(o['id'],o['state'],o.get('processed_quantity',o.get('cumulative_quantity'))) for o in v['pending_orders']],
   'plans':{k:(r['plan']['plan_version'],r['status'],r['filled'],r['stop_reason']) for k,r in s['orders'].items()},
   'position_target_versions':{k:v['target_version'] for k,v in s.get('position_theses',{}).items()},
   'manual_management_holds':s.get('manual_management_holds',{}),'management_resume_after':s.get('management_resume_after',{}),
   'shared_plans':{k:(r['version'],r['status']) for k,r in s.get('shared_plans',{}).items()},
   'mode':s['mode'],'control_epoch':s.get('control_epoch',0),'owner_policy_revision':s.get('owner_settings',{}).get('revision',0),'owner_operating_mode':s.get('owner_operating_mode','automatic'),'limits':self.broker.limits,'daily_lock':v['risk']['entry_locked'],'risk_day':v['risk'].get('day')}
  return hashlib.sha256(canonical(stable).encode()).hexdigest()
 async def context(self,operator_request):
  if not isinstance(operator_request,str) or not 1<=len(operator_request)<=6000:raise ValueError('operator_request required')
  async with self.bridge_lock:
   v=await self.broker.account_view(force=True);s=await asyncio.to_thread(self.worker.status);now=self.clock();rid='live-request-'+uuid.uuid4().hex
   req={'request_id':rid,'context_hash':self._digest(v,s),'created_at':now,'reply_before':now+120,
    'new_plan_id':'live-plan-'+uuid.uuid4().hex,'operator_request':operator_request,'execution':'live only after owner arming; disarmed submissions rejected',
    'context':v|{'mode':s['mode'],'active_plans':s['orders'],'prior_plan_versions':s.get('plan_history',[])[-10:],'position_theses':s.get('position_theses',{}),'profit_management':s.get('adaptive_profit',{}),'shared_plans':s.get('shared_plans',{})},
    'market_data_tool':'sailboat_market_read (exact option discovery and fresh quotes; no mutation)',
    'decision_schema':LIVE_REPLY_SCHEMA,'order_contract':ORDER_CONTRACT,'decision_id':'live-decision-'+rid}
   await asyncio.to_thread(self.journal.edit,lambda state:state.setdefault('live_requests',{}).__setitem__(rid,{'payload':req,'receipt':None,'response_hash':None}),archive_before=now-120)
   return req
 def request_payload(self,rid):
  from .request_archive import payload
  row=self.worker.status().get('live_requests',{}).get(rid)
  if not row:raise ValueError('unknown live request')
  return payload(self.service,rid,row['payload'])
 def receipt(self,rid):
  row=self.worker.status().get('live_requests',{}).get(rid)
  if not row:raise ValueError('unknown live request')
  return row['receipt'] or {'status':'AWAITING_RESPONSE','request_id':rid}
 async def respond(self,response):
  from jsonschema import validate
  validate(response,LIVE_REPLY_SCHEMA)
  digest=hashlib.sha256(canonical(response).encode()).hexdigest()
  async with self.bridge_lock:
   s=await asyncio.to_thread(self.worker.status);row=s.get('live_requests',{}).get(response['request_id'])
   if not row:raise ValueError('unknown live request')
   if row['receipt']:
    if row['response_hash']!=digest:raise ValueError('request already answered with different content')
    return row['receipt']
   req=row['payload'];action=response['action'];p=response['plan'];change=None
   if action=='cancel' and response.get('position_blocks'):raise ValueError('cancel alone, then reassess targets after reconciliation')
   # Recover a commit that accepted an intent before the HTTP receipt was checkpointed.
   accepted=s['decisions'].get(req['decision_id'])
   if accepted:
    if action!='execute' or p is None or hashlib.sha256(json.dumps(p,sort_keys=True,allow_nan=False).encode()).hexdigest()!=accepted['hash']:raise ValueError('decision already accepted with different content')
    if response.get('position_blocks') and s.get('position_block_commits',{}).get(response['request_id'],{}).get('response_hash')!=position_thesis.fingerprint(response):raise ValueError('accepted position-block content mismatch')
    result=await asyncio.to_thread(self.worker.accept,p)
   else:
    if response['context_hash']!=req['context_hash'] or self.clock()>=req['reply_before']:raise ValueError('expired or mismatched live context')
    v=await self.broker.account_view(force=True)
    if self._digest(v,self.worker.status())!=req['context_hash']:raise ValueError('live state changed; request fresh context')
    change=position_thesis.prepare(response,v,self.worker.status(),self.clock())
    if action=='execute':
     if p is None or response['plan_id'] is not None or response['plan_version'] is not None:raise ValueError('execute requires one complete plan')
     validate_plan(p,self.clock())
     if p['decision_id']!=req['decision_id'] or p['account_number']!=self.broker.account_number:raise ValueError('decision/account binding mismatch')
     old=req['context']['active_plans'].get(p['plan_id'])
     if old:
      if p['plan_version']!=old['plan']['plan_version']+1:raise ValueError('plan revision mismatch')
     elif (p['plan_id']!=req['new_plan_id'] and p['plan_id'] not in req['context'].get('shared_plans',{})) or p['plan_version']!=1:raise ValueError('new plan identity mismatch')
     if change['profit_guard']:
      result={'status':'PROFIT_INSTRUCTION_QUEUED','position_id':change['profit_guard']['position_id']}
     else:
      policy_revision=s.get('owner_settings',{}).get('revision',0)
      def commit_plan(state):
       if state.get('owner_settings',{}).get('revision',0)!=policy_revision:raise ValueError('Settings changed; request fresh context')
       if p['side']=='buy':state.setdefault('owner_policy_versions',{}).setdefault(p['decision_id'],policy_revision)
       position_thesis.commit(state,change,p['decision_id'])
      result=await asyncio.to_thread(self.worker.accept,p,state_update=commit_plan)
    elif action=='cancel':
     if p is not None:raise ValueError('cancel must not include order terms')
     old=s['orders'].get(response['plan_id'])
     if not old or old['plan']['plan_version']!=response['plan_version']:raise ValueError('cancel plan version mismatch')
     self.worker.cancel(response['plan_id']);result={'status':'CANCEL_REQUESTED'}
    else:
     if p is not None:raise ValueError('commentary cannot contain order terms')
     result={'status':action.upper()}
   receipt={'request_id':req['request_id'],'status':'ACCEPTED','result':result,'received_at':self.clock()}
   if change and change['updates']:receipt['position_updates']=[{k:i['block'][k] for k in ('position_id','target_version','action','target_bid')} for i in change['updates']]
   def save(state):
    if change and change['updates'] and (state['mode']!='LIVE_ARMED' or state.get('control_epoch',0)!=s.get('control_epoch',0) or state.get('boot_id')!=s.get('boot_id')):
     raise ValueError('Owner controls changed before position update')
    if change:position_thesis.commit(state,change)
    state['live_requests'][req['request_id']].update(receipt=receipt,response_hash=digest)
   await asyncio.to_thread(self.journal.edit,save);return receipt
 async def run(self):
  self.proxy.loop=asyncio.get_running_loop()
  async def poll():
   while True:
    try:
     s=await asyncio.to_thread(self.worker.status);session=s.get('owner_session')
     if s['mode']=='LIVE_ARMED' and session and self.clock()>=session['expires_at']:await asyncio.to_thread(self.disarm)
     await self.refresh()
    except Exception as exc:self.last_readiness={'ready':False,'blockers':[type(exc).__name__]}
    await asyncio.sleep(30)
  tasks=[asyncio.create_task(self.worker.run()),asyncio.create_task(poll()),asyncio.create_task(self.profit_manager.run())]
  try:await asyncio.gather(*tasks)
  finally:
   for task in tasks:task.cancel()
   await asyncio.gather(*tasks,return_exceptions=True)
