"""Bounded shared-agent assessment, read-only preview until the owner arms."""
import asyncio,copy,json,time,hashlib
from datetime import datetime,timezone
from jsonschema import validate
from .agent_contract import PROTOCOL,PROMPT,OUTPUT_SCHEMA,READS,account_question,ASSESSMENT_PROMPT_VERSION
from .live_broker import CONTRACTS
from .live_controls import LIVE_REPLY_SCHEMA
from .order_execution import validate_plan,TERMINAL
from .controller import canonical

from .analysis_view import project_analysis,analysis_history,ANALYSIS_CONTEXT_VERSION

ACCOUNT_ALIAS='OWNER_SAILBOAT_ACCOUNT'

def envelope(value):
 validate(value,OUTPUT_SCHEMA)
 if value['market_reads']:
  if value['decision'] is not None:raise ValueError('read requests cannot also contain a decision')
 elif value['decision'] is None:raise ValueError('final decision required')
 return value

def replace_account(value,account,rhs=None):
 if isinstance(value,dict):return {k:replace_account(v,account,rhs) for k,v in value.items() if k!='rhs_account_number'}
 if isinstance(value,list):return [replace_account(v,account,rhs) for v in value]
 if isinstance(value,str) and value in (account,rhs):return ACCOUNT_ALIAS
 return value

ANALYSIS_VERSION='evidence_v1'
MAX_LOOKUP_ROUNDS=3

async def market_brief(s):
 """A small observed-price packet, no trading indicator or strategy invented here."""
 now=s.clock();start=datetime.fromtimestamp(now-7200,timezone.utc).isoformat()
 args={'symbols':list(s.symbols),'start_time':start,'end_time':datetime.fromtimestamp(now,timezone.utc).isoformat(),'interval':'5minute','bounds':'regular'}
 try:
  raw=await s.broker.read('get_equity_historicals',args,persist=False)
  from .robinhood_orders import data
  if raw['result'].get('structuredContent') is not None:validate(raw['result']['structuredContent'],CONTRACTS['get_equity_historicals']['output'])
  values=data(raw['result']);items=[]
  from .scheduler import timestamp
  for row in values.get('results') or []:
   if row['symbol'] not in s.symbols or row['interval']!='5minute':continue
   bars=[];skipped=0
   for bar in row.get('bars') or []:
    if bar is None or bar.get('interpolated') or not now-7200<=timestamp(bar['begins_at'])<=now-300:
     skipped+=1;continue
    bars.append({k:bar[k] for k in ('begins_at','open_price','high_price','low_price','close_price','volume')})
   bars.sort(key=lambda b:b['begins_at'])
   items.append({'symbol':row['symbol'],'interval':'5minute','bounds':row['bounds'],'bars':bars[-24:],'excluded_incomplete_interpolated_or_outside_window':skipped,'truncated':len(bars)>24})
  packet={'source':raw['source'],'retrieved_at':raw['retrieved_at'],'tool':'get_equity_historicals','requested':args,'results':items,
   'unavailable_symbols':sorted(set(s.symbols)-{r['symbol'] for r in items if r['bars']}),'note':'Observed completed bars only; no news feed or derived indicators implied.'}
  if len(canonical(packet))>45000:raise ValueError('historical packet exceeds bounded size')
  return packet
 except Exception as exc:
  return {'source':'Robinhood historical read','requested':args,'results':[],'unavailable_symbols':list(s.symbols),'error':type(exc).__name__,'note':'Historical evidence unavailable; do not infer missing bars.'}

async def assess(s,market):
 ex=s.execution
 req=await ex.context('Assess the shared real-account book under the versioned Sailboat order-agent protocol.')
 observed,quotes=await s.fetch()
 initial=ex.worker.status();mode=initial['mode'];epoch=initial.get('control_epoch',0)
 if req['context']['mode']!=mode:raise ValueError('mode changed during context preparation')
 account=ex.broker.account_number
 request=replace_account(project_analysis(req,initial.get('agent_assessments',[])),account,req['context'].get('rhs_account_number'))
 request.update(agent_protocol=PROTOCOL,instructions=PROMPT,response_schema=OUTPUT_SCHEMA,
  analysis_version=ANALYSIS_VERSION,
  current_time=s.clock(),market_session=market,lookup_rounds_remaining=MAX_LOOKUP_ROUNDS,
  research_symbols=list(s.symbols),market_observations={'source':observed['source'],'quotes':quotes},market_results=[],
  market_read_schemas={n:CONTRACTS[n]['input'] for n in READS},
  previous_assessments=replace_account(analysis_history(initial.get('agent_assessments',[])),account))
 request['account_question']=account_question(request['context'])
 if getattr(ex,'shape_runtime',None) and not request['context']['positions']:
  from .candidate_entry import POLICY
  request['candidate_entry_policy']={'id':POLICY,'quantity':1,'price':'fresh executable ask as a fixed limit cap',
   'owner_limits_enforced':True,'named_candidate_can_be_selected_by_controller':True}
  request['account_question']+=(" Candidate handoff: name one preferred QQQ call or put, strike and exact expiration in your reasoning, "
   "or state that you have no candidate. You need not construct a BUY order. Sailboat evaluates a named candidate "
   "under the owner's one-contract affordability policy; a mention can become a controller proposal independently "
   "of an OBSERVE decision. State explicit no-order restrictions when that is what you mean.")
 request['operator_request']=request['account_question']
 request['assessment_prompt_version']=ASSESSMENT_PROMPT_VERSION
 from .market_hours import EASTERN
 request['trading_date']=datetime.fromtimestamp(s.clock(),EASTERN).date().isoformat()
 request['assessment_interval_seconds']=s.interval
 request['historical_observations']=await market_brief(s)
 input_summary={'analysis_version':ANALYSIS_VERSION,'analysis_context_version':ANALYSIS_CONTEXT_VERSION,'assessment_prompt_version':ASSESSMENT_PROMPT_VERSION,'input_sha256':hashlib.sha256(canonical(request).encode()).hexdigest(),
  'context_fields':sorted(request['context']),'execution_feedback_count':len(request['execution_feedback']['recorded_outcomes']),
  'historical_bar_counts':{v['symbol']:len(v['bars']) for v in request['historical_observations']['results']},
  'historical_unavailable':request['historical_observations']['unavailable_symbols'],'historical_error':request['historical_observations'].get('error'),
  'execution_mode_fields_supplied':any(k in request for k in ('execution','execution_mode','trading_authorized')) or 'mode' in request['context']}
 await s.tx(lambda c,st:st['active'].update(route=PROTOCOL,phase='DISPATCHED',request_id=req['request_id'],decision_id=req['decision_id'],start_mode=mode,control_epoch=epoch,input_summary=input_summary))
 usage=[];last=None
 for round_no in range(MAX_LOOKUP_ROUNDS+1):
  request['current_time']=s.clock();request['lookup_rounds_remaining']=MAX_LOOKUP_ROUNDS-round_no
  request['assessment_stage']='initial' if round_no==0 else 'broker_lookup_followup'
  last,u=await s.provider.decide(request);usage.append(u);envelope(last)
  request['market_results'].extend(u.get('market_results',[]))
  if not last['market_reads']:break
  if round_no==MAX_LOOKUP_ROUNDS:raise ValueError('agent exceeded bounded data lookup rounds')
  for read in last['market_reads']:
   args=json.loads(read['arguments_json'])
   if not isinstance(args,dict):raise ValueError('market read arguments must be an object')
   validate(args,CONTRACTS[read['tool']]['input'])
   result=await s.broker.read(read['tool'],args,persist=False)
   from .robinhood_orders import data
   raw=data(result['result']);truncated=False
   for key in ('instruments','results','chains','historicals'):
    if isinstance(raw.get(key),list) and len(raw[key])>25:raw[key]=raw[key][:25];truncated=True
   item={'tool':read['tool'],'arguments':args,'retrieved_at':result['retrieved_at'],'data':raw,'truncated':truncated,'complete':not truncated and not bool(raw.get('next'))}
   if len(canonical(item))>60000:raise ValueError('market result exceeds bounded context; use a narrower query')
   request['market_results'].append(item)
  request['previous_read_request_reasoning']=last['reasoning']
  await s.tx(lambda c,st:st['active'].update(market_results=copy.deepcopy(request['market_results'])))
 if last['market_reads']:raise ValueError('no final decision')
 reply=copy.deepcopy(last['decision'])
 if reply['request_id']!=req['request_id'] or reply['context_hash']!=req['context_hash']:raise ValueError('agent changed request identity')
 if reply['plan'] is not None:
  if reply['plan']['account_number']!=ACCOUNT_ALIAS:raise ValueError('agent changed account alias')
  reply['plan']['account_number']=account
  allowed=set(s.symbols)|{p['symbol'] for p in req['context']['positions']}
  if reply['plan']['symbol'] not in allowed:raise ValueError('symbol outside shared research/holding scope')
 if reply['action'] not in ('observe','cancel'):
  held={p['position_id'] for p in req['context']['positions'] if p['kind']=='option'}
  if held-{b['position_id'] for b in reply.get('position_blocks',[])}:raise ValueError('held options require position-specific assessment blocks')
 # Persist the complete response and start-mode fence before any execution-interface call.
 await s.tx(lambda c,st:st['active'].update(phase='RESPONSE_READY',response=reply,assessment=last,usage={'provider':s.provider.name,'rounds':usage,'agent_calls':len(usage)}))
 current=ex.worker.status()
 if current['mode']!=mode or current.get('control_epoch',0)!=epoch:raise ValueError('owner mode changed during assessment; reassess next wake-up')
 if s.clock()>=req['reply_before']:raise ValueError('assessment expired')
 if s.market_hours and not s.market_hours.status(s.clock())['open']:raise ValueError('market closed during assessment')
 try:
  from .shared_assessment import submit
  receipt=await submit(ex,req,reply,source='scheduled:'+s.provider.name,reasoning=last['reasoning'],
    uncertainty=last['uncertainty'],missing_information=last['missing_information'],
    metadata={'analysis_version':ANALYSIS_VERSION,'assessment_prompt_version':ASSESSMENT_PROMPT_VERSION,'premium_thesis':copy.deepcopy(last.get('premium_thesis'))})
  if getattr(ex,'shape_runtime',None):
   from .shape_runtime import enqueue
   def queue_candidate(state):
    if state['mode']!=mode or state.get('control_epoch',0)!=epoch:raise ValueError('owner controls changed before candidate handoff')
    enqueue(state,'scheduled:'+req['request_id'],last['reasoning'],s.clock(),'scheduled:'+s.provider.name,
     candidate_live_enabled=ex.shape_runtime.candidate_entry.allow_execution)
   ex.journal.edit(queue_candidate)
  status='COMPLETED_LIVE_DECISION' if mode=='LIVE_ARMED' else 'PREVIEW_ONLY'
 except ValueError as exc:
  receipt={'status':'REJECTED','request_id':req['request_id'],'reason':str(exc)[:500]}
  status='REJECTED_DECISION'
  ex.journal.edit(lambda state:state.update(agent_assessments=(state.get('agent_assessments',[])+[{
   'analysis_context_version':ANALYSIS_CONTEXT_VERSION,'request_id':req['request_id'],'at':s.clock(),'mode':mode,
   'source':'scheduled:'+s.provider.name,'action':reply['action'],'reasoning':last['reasoning'],
   'uncertainty':last['uncertainty'],'missing_information':last['missing_information'],'result':receipt}])[-20:]))
 return await s.tx(lambda c,st:s.finish(c,st,status,receipt=receipt,assessment=last,usage=st['active']['usage'],provider=s.provider.name,input_summary=input_summary,market_results=request['market_results']))

async def preview(ex,req,response,*,record_only=False):
 """No worker.accept, cancel, review or placement; validated proposal only."""
 validate(response,LIVE_REPLY_SCHEMA);p=response['plan'];action=response['action']
 from . import position_thesis
 if action=='cancel' and response.get('position_blocks'):raise ValueError('cancel alone, then reassess targets')
 if response['context_hash']!=req['context_hash']:raise ValueError('preview context mismatch')
 if record_only:
  if action!='observe' or p is not None or response.get('plan_id') is not None or response.get('plan_version') is not None or response.get('position_blocks') or req['context']['positions']:
   raise ValueError('record-only receipt cannot contain position or order actions')
  if ex.clock()>=req['reply_before']:raise ValueError('preview expired')
  if ex._digest(req['context'],ex.worker.status())!=req['context_hash']:raise ValueError('controller state changed during analysis')
  return {'status':'PREVIEW_VALIDATED','action':'observe','broker_submission':False,'broker_review_performed':False,'scope':'analysis_record_only'}
 view=await ex.broker.account_view(force=True)
 if ex._digest(view,ex.worker.status())!=req['context_hash']:raise ValueError('account/plan state changed during preview')
 if ex.clock()>=req['reply_before']:raise ValueError('preview expired')
 change=position_thesis.prepare(response,view,ex.worker.status(),ex.clock())
 if action=='execute':
  if p is None or response['plan_id'] is not None or response['plan_version'] is not None:raise ValueError('execute needs one complete plan')
  validate_plan(p,ex.clock())
  if p['decision_id']!=req['decision_id'] or p['account_number']!=ex.broker.account_number:raise ValueError('preview decision/account mismatch')
  old=req['context']['active_plans'].get(p['plan_id'])
  if old:
   if old['status'] not in TERMINAL or p['plan_version']!=old['plan']['plan_version']+1:raise ValueError('old plan must be terminal before revision')
  elif (p['plan_id']!=req['new_plan_id'] and p['plan_id'] not in req['context'].get('shared_plans',{})) or p['plan_version']!=1:raise ValueError('preview plan identity mismatch')
  snapshot=await ex.broker.snapshot(p,p['quantity']);block=ex.worker._checks(p,snapshot,ex.clock(),p['quantity'])
  # Preview never activates position directives or cancels working orders.
  return {'status':'PREVIEW_BLOCKED' if block else 'PREVIEW_VALIDATED','action':action,'blocker':block,'broker_submission':False,'broker_review_performed':False}
 if p is not None:raise ValueError('non-execute action cannot contain a plan')
 if action=='cancel':
  old=req['context']['active_plans'].get(response['plan_id'])
  if old:
   if old['plan']['plan_version']!=response['plan_version']:raise ValueError('preview cancel identity mismatch')
  else:
   draft=req['context'].get('shared_plans',{}).get(response['plan_id'])
   if not draft or response['plan_version']!=draft['version']:raise ValueError('preview cancel identity mismatch')
 elif response['plan_id'] is not None or response['plan_version'] is not None:raise ValueError('hold/observe must not carry order changes')
 # Only the authorized respond path commits executable position directives.
 return {'status':'PREVIEW_VALIDATED','action':action,'broker_submission':False,'broker_review_performed':False}
