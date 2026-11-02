"""Small HF-identity owner panel. No credentials or sessions are exposed to MCP."""
import base64,copy,hashlib,json,secrets,time
from pathlib import Path
from urllib.parse import urlencode
import anyio,httpx
from starlette.responses import HTMLResponse,JSONResponse,RedirectResponse,Response
from starlette.routing import Route

BASE=''
OWNER_SUB=''
SESSION='__Host-sailboat-owner';FLOW='__Host-sailboat-flow'
def hashed(s):return hashlib.sha256(s.encode()).hexdigest()

def entry_progress(state,key,receipt):
 """Project current downstream state without altering an immutable submission."""
 shaped=state.get('shape_recent',{}).get(key,{})
 entry=state.get('candidate_messages',{}).get(key,{}).get('result') or shaped.get('candidate_entry') or {}
 job=state.get('shape_jobs',{}).get(key,{})
 controller=entry.get('controller_receipt') or {}
 out=copy.deepcopy(receipt)
 if not entry and not job:return out,None
 out['analysis_status']=receipt.get('status')
 out['processing_status']=shaped.get('status') or job.get('assembly_status') or job.get('status')
 if entry:
  out['candidate_status']=entry['status'];out['status']=entry['status']
  out['assembly_status']=(entry.get('assembly') or {}).get('status')
  if entry.get('candidate'):out['selected_contract']={k:entry['candidate'][k] for k in ('symbol','expiry','strike','right') if k in entry['candidate']}
  if entry.get('reason'):out['reason']=entry['reason']
 if controller:
  out['controller_status']=controller['status'];out['controller_request_id']=controller.get('request_id')
  result=controller.get('result') or {};plan=result.get('plan') or entry.get('proposed_order') or {}
  order=state.get('orders',{}).get(plan.get('plan_id'))
  if order:
   attempts=order.get('attempts',[]);count=sum(a.get('transport_attempts',0) for a in attempts)
   out.update(status=order['status'],order_status=order['status'],filled=order.get('filled',0),placement_attempts=count,
    broker_submission=True if any(a.get('order_id') for a in attempts) else None if count else False,
    broker_review_performed=plan.get('decision_id') in state.get('broker_fee_quotes',{}))
   if order.get('stop_reason') or order.get('last_error'):out['reason']=order.get('stop_reason') or order['last_error']
 return out,controller.get('request_id')

def recent_decisions(state):
 """Last three unique submissions, including plugin observations; read-only."""
 candidates={}
 for rid,row in state.get('live_requests',{}).items():
  receipt=row.get('receipt')
  if not receipt:continue
  result=receipt.get('result',{});status=result.get('status');plan=result.get('plan') or {}
  action={'OBSERVE':'observe','HOLD':'hold','CANCEL_REQUESTED':'cancel'}.get(status)
  if not action and plan:action='execute'
  candidates[rid]={'request_id':rid,'received_at':receipt.get('received_at',0),'action':action,
   'source':'Connected agent','receipt':receipt,'reasoning':None,'uncertainty':None,'missing_information':[]}
 for note in list(state.get('received_analyses',{}).values())+state.get('agent_assessments',[]):
  if not note.get('result'):continue
  rid=note['request_id'];receipt=note['result']
  candidates[rid]={'request_id':rid,'received_at':receipt.get('received_at',note.get('at',0)),
   'action':'analysis' if note.get('analyst_protocol') else note.get('action'),'source':('Chat assessment' if note.get('source','').startswith('chat:') else 'Connected agent' if note.get('source','').startswith('agent:') else 'Scheduled assessment'),'receipt':receipt,
   'reasoning':note.get('reasoning'),'uncertainty':note.get('uncertainty'),
   'held_position_blocks':note.get('held_position_blocks',[]),
   'research_evidence':note.get('research_evidence') or state.get('received_analyses',{}).get(rid,{}).get('research_evidence'),
   'missing_information':note.get('missing_information') or []}
 # Keep the analysis as the visible source and attach its actual candidate/order
 # result. A generated controller request is not a second analyst submission.
 for rid,item in list(candidates.items()):
  if item['source']!='Scheduled assessment':continue
  item['receipt'],alias=entry_progress(state,'scheduled:'+rid,item['receipt'])
  if alias and alias!=rid:candidates.pop(alias,None)
 for ident,row in state.get('chat_messages',{}).items():
  output=row.get('result') or {};shadow=output.get('shadow_sorter') or {};key=shadow.get('receipt_id')
  job=state.get('shape_jobs',{}).get(key,{})
  shaped=state.get('shape_recent',{}).get(key,{})
  entry=shaped.get('candidate_entry') or state.get('candidate_messages',{}).get(key,{}).get('result') or {}
  controller=output.get('controller_receipt') or entry.get('controller_receipt')
  aliases={row.get('request_id')}
  if controller:aliases.add(controller.get('request_id'))
  for note in state.get('agent_assessments',[]):
   if key and (note.get('assembly_reference') or {}).get('assembly_id')==key:aliases.add(note.get('request_id'))
  existing=candidates.get((controller or {}).get('request_id')) or candidates.get(row.get('request_id')) or next((candidates[rid] for rid in sorted(aliases-{None}) if rid in candidates),{})
  for rid in aliases:candidates.pop(rid,None)
  request=state.get('live_requests',{}).get(row.get('request_id'),{}).get('payload',{})
  at=request.get('created_at',job.get('message_at',output.get('context',{}).get('observed_at',0)))
  processing=shaped.get('status') or job.get('assembly_status') or job.get('status') or shadow.get('status') or output.get('status','RECEIVED')
  receipt=copy.deepcopy(controller) if controller else {'status':processing}
  if key:receipt['processing_status']=processing
  if entry.get('status'):receipt['candidate_status']=entry['status']
  if (entry.get('assembly') or {}).get('status'):receipt['assembly_status']=entry['assembly']['status']
  if not controller:
   for record in (entry,shaped,shadow):
    if 'broker_submission' in record:receipt['broker_submission']=record['broker_submission'];break
   if entry.get('reason') or shaped.get('error'):receipt['reason']=entry.get('reason') or shaped['error']
  sorting=shaped.get('sorting') or {}
  candidates[ident]=dict(existing,request_id=ident,received_at=at,source='Plugin submission',receipt=receipt,
   action=(receipt.get('action') or existing.get('action') or 'submission') if controller else 'analysis',
   reasoning=row.get('text'),uncertainty=existing.get('uncertainty'),
   research_evidence=sorting.get('research_evidence') or existing.get('research_evidence'),
   missing_information=sorting.get('missing_information') or existing.get('missing_information') or [])
  # Preserve existing plugin receipt semantics; attach live worker outcome when
  # an assembled entry actually created an order plan.
  progress,_=entry_progress(state,key,receipt)
  if progress.get('order_status'):candidates[ident]['receipt']=progress
 items=sorted(candidates.values(),key=lambda x:(x['received_at'],x['request_id']),reverse=True)[:3]
 for item in items:
  receipt=item['receipt'];result=receipt.get('result',{})
  summary={k:receipt[k] for k in ('request_id','status','received_at','action','blocker','reason','processing_status','assembly_status','candidate_status','analysis_status','controller_status','controller_request_id','selected_contract','order_status','filled','placement_attempts','broker_submission','broker_review_performed') if k in receipt}
  if item.get('held_position_blocks'):summary['held_position_blocks']=item['held_position_blocks']
  if result:summary['result']={k:result[k] for k in ('status','filled','blocker','broker_submission','broker_review_performed') if k in result}
  item['receipt']=summary
  from .interpretation_view import interpretation
  item['interpretation']=interpretation(state,item)
 return items

def latest_decision(state):
 items=recent_decisions(state)
 return items[0] if items else None

from .shadow_panel import shape_views

class OwnerPanel:
 def __init__(self,execution,scheduler,service,client_id='',client_secret='',owner_sub=OWNER_SUB,base=BASE,clock=time.time):
  self.execution=execution;self.scheduler=scheduler;self.service=service
  self.client_id=client_id;self.client_secret=client_secret;self.owner_sub=owner_sub;self.base=base;self.clock=clock
  self.pending={};self.sessions={};self.paths={'/','/owner/panel.js','/owner/shadow.js','/owner/panel.css','/owner/login','/owner/callback','/owner/view','/owner/action','/owner/logout'}
 def prune(self):
  for d in (self.pending,self.sessions):
   for k in list(d):
    if d[k]['expires']<=self.clock():d.pop(k,None)
 def secure(self,r):
  r.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
   'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'self' https://huggingface.co"})
  return r
 def cookie(self,response,name,value,max_age):
  response.set_cookie(name,value,max_age=max_age,secure=True,httponly=True,samesite='none' if name==SESSION else 'lax',path='/')
 def session(self,request):
  self.prune();s=self.sessions.get(hashed(request.cookies.get(SESSION,'')))
  return s if s and s['sub']==self.owner_sub else None
 async def page(self,request):
  return self.secure(HTMLResponse(Path(__file__).with_name('owner_panel.html').read_text(encoding='utf-8')))
 async def asset(self,request):
  kind='js' if request.url.path.endswith('.js') else 'css'
  if request.url.path=='/owner/shadow.js':return self.secure(Response(Path(__file__).with_name('shadow_panel.js').read_text(encoding='utf-8'),media_type='text/javascript'))
  return self.secure(Response(Path(__file__).with_name('owner_panel.'+kind).read_text(encoding='utf-8'),media_type='text/javascript' if kind=='js' else 'text/css'))
 async def login(self,request):
  if not self.client_id or not self.client_secret or not self.owner_sub or not self.base:return self.secure(HTMLResponse('Owner identity verification is not configured yet.',503))
  if self.session(request):return self.secure(RedirectResponse('/',303))
  self.prune()
  if len(self.pending)>=64:return self.secure(HTMLResponse('Please try again shortly.',429))
  state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(48)
  self.pending[hashed(state)]={'expires':self.clock()+600,'verifier':verifier}
  challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
  query=urlencode({'client_id':self.client_id,'redirect_uri':self.base+'/owner/callback','response_type':'code','scope':'openid profile','state':state,'code_challenge':challenge,'code_challenge_method':'S256'})
  result=self.secure(RedirectResponse('https://huggingface.co/oauth/authorize?'+query,303));self.cookie(result,FLOW,state,600);return result
 async def identity(self,code,verifier):
  async with httpx.AsyncClient(timeout=20,follow_redirects=False) as c:
   r=await c.post('https://huggingface.co/oauth/token',auth=(self.client_id,self.client_secret),data={'grant_type':'authorization_code','client_id':self.client_id,'code':code,'redirect_uri':self.base+'/owner/callback','code_verifier':verifier})
   r.raise_for_status();token=r.json()['access_token']
   u=await c.get('https://huggingface.co/oauth/userinfo',headers={'Authorization':'Bearer '+token});u.raise_for_status();return u.json()
 async def callback(self,request):
  self.prune();state=request.query_params.get('state','');bound=request.cookies.get(FLOW,'')
  if not state or not bound or not secrets.compare_digest(state,bound):return self.secure(HTMLResponse('Invalid sign-in state. Reopen the owner controls.',400))
  flow=self.pending.pop(hashed(state),None)
  if not flow or not request.query_params.get('code'):return self.secure(HTMLResponse('Sign-in expired or cancelled. Reopen the owner controls.',400))
  try:identity=await self.identity(request.query_params['code'],flow['verifier'])
  except Exception:return self.secure(HTMLResponse('Hugging Face verification failed. Reopen the owner controls.',502))
  if not self.owner_sub or identity.get('sub')!=self.owner_sub:return self.secure(HTMLResponse('These controls belong to the Sailboat owner.',403))
  self.prune()
  if len(self.sessions)>=16:return self.secure(HTMLResponse('Too many active owner sessions. Try again later.',429))
  sid=secrets.token_urlsafe(48);self.sessions[hashed(sid)]={'sub':self.owner_sub,'csrf':secrets.token_urlsafe(32),'expires':self.clock()+3600}
  result=self.secure(RedirectResponse('/',303));self.cookie(result,SESSION,sid,3600);self.cookie(result,FLOW,'',0);return result
 async def view(self,request):
  s=self.session(request)
  if not s:return self.secure(JSONResponse({'error':'owner_session_required'},401))
  try:
   if not self.service.ready:
    return self.secure(JSONResponse({'control':{'mode':'UNKNOWN','live_ready':False,'broker_write_calls_enabled':False,
      'blockers':['Durable storage is unavailable. New brokerage submissions are blocked; recovery is required. Existing broker orders may still be active.']},
      'scheduler':None,'latest_decision':None,'recent_decisions':[],'durable':False,'recovery_required':True,
      'observed_at':self.clock(),'csrf':s['csrf'],'session_expires':s['expires']}))
   # Background preflight owns broker polling. Viewing the dashboard must not trigger it.
   control=self.execution.status()
   schedule=await self.scheduler.status() if self.scheduler else None
   state=self.execution.worker.status() if hasattr(self.execution,'worker') else {}
   history=recent_decisions(state)
   decision=history[0] if history else None
   overview={'book':state.get('position_observation'), 'changes':state.get('position_changes',[])[-3:],
     'processing_health':state.get('processing_health'),
     'manual_holds':state.get('manual_management_holds',{}),
     'orders':[{'plan_id':k,'status':r['status'],'side':r['plan']['side'],'quantity':r['plan']['quantity'],
        'filled':r.get('filled',0),'prices':r['plan']['limit_prices'],'last_error':r.get('last_error'),
        'market_progress':r.get('market_progress')}
        for k,r in state.get('orders',{}).items() if r['status'] not in {'FILLED','CANCELLED','REJECTED','EXPIRED','STOPPED','INVALIDATED','NEEDS_OWNER','DAILY_LOCKED'}]}
   book=getattr(getattr(self.execution,'broker',None),'view',None)
   if book:
    overview['book']={k:copy.deepcopy(book[k]) for k in ('positions','buying_power','observed_at')}
    portfolio=book.get('portfolio') or {}
    overview['account_summary']={'account_value':portfolio.get('total_value'),'cash':portfolio.get('cash'),
     'buying_power':book.get('buying_power'),'currency':portfolio.get('currency','USD'),'observed_at':book['observed_at']}
    for p in overview['book']['positions']:
     p.update(state.get('position_labels',{}).get(p['id'],{}))
   from .owner_settings import view as policy_view
   overview['management']=state.get('adaptive_profit',{})
   overview['order_history']=[{'plan_id':k,'status':r['status'],'side':r['plan']['side'],'symbol':r['plan']['symbol'],
      'quantity':r['plan']['quantity'],'filled':r.get('filled',0),'at':r['plan']['created_at'],'error':r.get('last_error')}
      for k,r in sorted(state.get('orders',{}).items(),key=lambda kv:kv[1]['plan']['created_at'],reverse=True)[:20]]
   overview['position_epochs']=state.get('position_management_epochs',{})
   for row in overview['orders']:
    plan=state['orders'][row['plan_id']]['plan'];row.update(position_id=plan.get('position_id'),symbol=plan['symbol'])
   return self.secure(JSONResponse({'owner_policy':policy_view(state,self.clock()),'overview':overview,'control':control,'scheduler':schedule,'latest_decision':decision,'recent_decisions':history,'shadow_decisions':shape_views(state),'durable':self.service.ready,'observed_at':self.clock(),'csrf':s['csrf'],'session_expires':s['expires']}))
  except Exception:return self.secure(JSONResponse({'error':'Status unavailable. State is unknown; do not assume disarmed.'},503))
 async def action(self,request):
  s=self.session(request)
  if not s:return self.secure(JSONResponse({'error':'owner_session_required'},401))
  if request.headers.get('origin')!=self.base or not secrets.compare_digest(request.headers.get('x-sailboat-csrf',''),s['csrf']):
   return self.secure(JSONResponse({'error':'Owner action origin or session check failed'},403))
  if request.url.path=='/owner/logout':
   self.sessions.pop(hashed(request.cookies.get(SESSION,'')),None)
   r=self.secure(JSONResponse({'signed_out':True,'controller_unchanged':True}));self.cookie(r,SESSION,'',0);return r
  body=b''
  async for part in request.stream():
   body+=part
   if len(body)>8192:return self.secure(JSONResponse({'error':'Request too large'},413))
  try:
   v=json.loads(body)
   from .owner_controls import COMMANDS,handle
   if isinstance(v,dict) and v.get('command') in COMMANDS:result=await handle(self.execution,self.scheduler,v)
   elif v=={'command':'disarm'}:result=await anyio.to_thread.run_sync(self.execution.disarm)
   elif set(v)=={'command','position_id'} and v['command']=='resume_management':
    from .position_updates import resume_management
    self.execution.journal.edit(lambda state:resume_management(state,v['position_id'],self.clock()))
    result={'status':'AWAITING_FRESH_ASSESSMENT','mode':self.execution.status()['mode']}
   elif v=={'command':'arm','confirmation':'ARM LIVE'}:
    if not self.service.ready:return self.secure(JSONResponse({'error':'Durable state unavailable'},503))
    result=await self.execution.arm()
   else:return self.secure(JSONResponse({'error':'Explicit ARM LIVE confirmation or disarm required'},400))
   return self.secure(JSONResponse(result))
  except ValueError as exc:return self.secure(JSONResponse({'error':str(exc)[:300]},409))
  except Exception:return self.secure(JSONResponse({'error':'Action outcome unknown. Refresh status; no automatic retry was made.'},503))
 def routes(self):
  return [Route('/',self.page),Route('/owner/panel.js',self.asset),Route('/owner/shadow.js',self.asset),Route('/owner/panel.css',self.asset),Route('/owner/login',self.login),Route('/owner/callback',self.callback),Route('/owner/view',self.view),Route('/owner/action',self.action,methods=['POST']),Route('/owner/logout',self.action,methods=['POST'])]
