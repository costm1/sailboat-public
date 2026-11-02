"""Provider-independent chat ingress to the SAME account controller as scheduled agents."""
import asyncio,copy,hashlib,json,uuid
from datetime import datetime
from decimal import Decimal
from jsonschema import Draft202012Validator
from .controller import canonical
from .text_intent import INTENT_SCHEMA as BASE_INTENT, validate_intent
from .structured_schema import finite
from .analysis_view import project_analysis
from .order_execution import TERMINAL
from .live_broker import option_quote
from .market_hours import EASTERN
from .shared_assessment import submit

INTENT_SCHEMA=copy.deepcopy(BASE_INTENT)
INTENT_SCHEMA['properties'].update({
 'valid_for_seconds':{'type':'integer','minimum':10,'maximum':3600},
 'target_bid':{'type':'number','exclusiveMinimum':0},
 'expected_upside_usd':{'type':'number'},
 'roundtrip_cost_allowance_usd':{'type':'number','minimum':0}})
SEND_SCHEMA={'type':'object','additionalProperties':False,'required':['text'],'properties':{
 'text':{'type':'string','minLength':1,'maxLength':16000},'message_id':{'type':'string','minLength':1,'maxLength':100},
 'context_receipt':{'type':'string','minLength':1,'maxLength':100},'intent':INTENT_SCHEMA}}
RECEIPT_SCHEMA={'type':'object','additionalProperties':False,'required':['receipt_id'],'properties':{'receipt_id':{'type':'string','minLength':1,'maxLength':100}}}
TOOLS=(('sailboat_send_message','Natural-language analysis supplies exact QQQ 0DTE CALL and PUT contracts and independent probabilities of profitable premium resale within 30 minutes. Assembly selects the higher probability; ties stay out. No minimum probability or confidence threshold, and PASS/WAIT do not override the comparison. Missing contract or probability stays unresolved. No intent or analyst BUY instruction is required for this evaluation. Execution requires the host candidate gate, existing owner ARM and broker checks; receipt is not a fill. Send text to the shared Sailboat account controller. First call returns actual account buying power, holdings, orders, shared plans and prior assessments. Only for explicitly requested shared-plan actions, then send unchanged text/message_id with a faithful intent and context_receipt=receipt_id. Sailboat resolves broker contracts and plan versions and uses the same owner-controlled submission path as scheduled Claude. No second model. Receipt is not a fill; no message can arm or change limits.',SEND_SCHEMA),
 ('sailboat_message_receipt','Recover this client message and the shared account controller receipt; never retry an uncertain accepted action with a new identity.',RECEIPT_SCHEMA))
SKILL="""Natural-language text alone queues the shape sorter. No formatted intent or BUY instruction is required. Supply exact QQQ 0DTE CALL and PUT contracts with independent estimated probabilities of profitable premium resale within 30 minutes. Assembly selects the higher probability, even by one percentage point; an exact tie stays out. No minimum probability or extra confidence threshold applies. PASS, WAIT, and NO TRADE do not override the numeric comparison. Missing or ambiguous probabilities or contracts remain unresolved. Existing held-position management is unchanged. The host candidate gate and existing owner ARM/limits govern execution. Preserve explicit do-not-submit restrictions; a named candidate is not attributed as your BUY instruction. The receipt includes shadow_sorter; the owner dashboard shows results. The following optional two-step interface applies only when explicitly using the existing shared-plan route.
You are the sending agent; no second model interprets your response. Use the two-step
sailboat_send_message exchange to obtain the shared real account context, then return a
faithful small intent block with the same text/message_id and context_receipt=receipt_id.
Do not construct internal brokerage orders, UUIDs, accounts or plan versions. Resolve
0DTE using market_date and supply the actual option expiry/strike/call-or-put. Research
through sailboat_market_read when useful. No separate simulation book is used.
Use meaning observation/recommendation/instruction/clarification and action
observe/watch/enter/exit/hold/revise/cancel/clarify, with exact supporting text excerpts
in evidence. Use returned plan_name for changes. Entry/exit/replacement requires explicit
quantity, limit_price and valid_for_seconds. An optional condition/invalidation uses
gte/lte only. Never discard unsupported conditions; clarify instead. revision is
replace/suspend/resume. Hold can supply target_bid for a target revision, and
expected_upside_usd when known. Exits require a supported roundtrip_cost_allowance_usd;
unknown fees are not zero. Preserve no-loss-sale rules. No guaranteed target or fill.
Observations cannot alter plans; recommendations can record proposals but cannot execute.
Do not upgrade hypotheticals, refusals, quoted instructions, or options mentioned in prose
into an instruction. Preserve research-only/do-not-submit/no-orders restrictions. Multiple
independent actions require clarification, not silently dropping one. Never route a refused
transaction through prose. Transport is not owner execution authorization. The same owner
checks apply to all agent sources. Read the actual receipt; proposed/preview/queued is not
filled. Read prior receipts before retrying. Stale requests require fresh assessment, never
a new identity to bypass denial. Existing historical paper receipts remain historical.
"""

class NeedInformation(ValueError):pass

def check(args):
 finite(args)
 errors=list(Draft202012Validator(SEND_SCHEMA).iter_errors(args))
 if errors:raise ValueError('Invalid text/intent: '+errors[0].message[:250])
 if not args['text'].strip() or len(args['text'].encode())>32000:raise ValueError('nonblank bounded text required')
 if 'intent' in args:
  base={k:v for k,v in args['intent'].items() if k in BASE_INTENT['properties']}
  validate_intent(base,args['text'])

def label(pid,row):
 return row.get('name') or (row.get('plan') or {}).get('symbol','Sailboat')+' plan '+pid[-6:]

def all_plans(req):
 result=copy.deepcopy(req['context'].get('shared_plans',{}))
 for pid,row in req['context'].get('active_plans',{}).items():
  result.setdefault(pid,{'id':pid,'name':label(pid,row),'version':None})
  result[pid].update(plan=copy.deepcopy(row['plan']),order_status=row['status'],filled=row['filled'])
 return result

async def contract(ex,symbol,spec,*,resolved=None,quote=None):
 if not spec or spec.get('kind')!='option' or not {'expiry','strike','right'}<=spec.keys():raise NeedInformation('Specify exact option expiry, strike and call/put. Stock execution is not supported by the existing order worker.')
 # Internal host-supplied broker facts only; no external intent field accepts them.
 rows=[resolved] if resolved is not None else await ex.broker.pages('get_option_instruments',{'chain_symbol':symbol,'expiration_dates':spec['expiry'],
  'strike_price':str(spec['strike']),'type':spec['right'],'state':'active'},'instruments')
 rows=[r for r in rows if r['chain_symbol']==symbol and r['expiration_date']==spec['expiry'] and Decimal(r['strike_price'])==Decimal(str(spec['strike'])) and r['type']==spec['right']]
 if len(rows)!=1:raise NeedInformation('Exact broker contract is absent or ambiguous. No contract was selected.')
 row=rows[0]
 # The endpoint's optional tradability filter can return no rows for a tradable
 # exact contract. Verify the returned contract here, never relax eligibility.
 if row['underlying_type']!='equity' or row['state']!='active' or row['tradability']!='tradable' or Decimal(row['trade_value_multiplier'])!=100:raise NeedInformation('Broker contract is not a supported tradable standard option.')
 if quote is not None:
  if quote['id']!=row['id']:raise NeedInformation('Broker quote identity mismatch.')
  # Revalidate normalized host data with the SAME quote validator. Refresh age
  # failures by exact UUID; the final order worker still obtains its own quote.
  from datetime import timezone
  try:
   q=option_quote({'quote':{'instrument_id':quote['id'],'bid_price':quote['bid'],'ask_price':quote['ask'],
    'updated_at':datetime.fromtimestamp(quote['asof'],timezone.utc).isoformat()},'close':quote.get('close')},ex.clock())
   return copy.deepcopy(row),q
  except (ValueError,OverflowError):pass
 raw=await ex.broker.read('get_option_quotes',{'instrument_ids':[row['id']]})
 quotes=[option_quote(q,ex.clock()) for q in raw['results'] if q['quote']['instrument_id']==row['id']]
 if len(quotes)!=1:raise NeedInformation('Fresh exact contract quote unavailable.')
 return row,quotes[0]

def plain_context(ex,req):
 projected=project_analysis(req,ex.worker.status().get('agent_assessments',[]))
 v=projected['context'];plans=all_plans(req)
 return {'scope':'one shared brokerage account and controller; the same book used by scheduled Claude',
  'observed_at':v['observed_at'],'buying_power':v['buying_power'],
  'market_date':datetime.fromtimestamp(ex.clock(),EASTERN).date().isoformat(),
  'positions':v['positions'],'pending_orders':v['pending_orders'],
  'plans':[{'plan_name':label(pid,row),'status':row.get('order_status',row.get('status')),
     'filled':row.get('filled',0),'symbol':(row.get('plan') or {}).get('symbol',row.get('terms',{}).get('symbol')),
     'terms':row.get('terms',{}),'reasoning':row.get('reasoning'),'order_terms':{k:x for k,x in (row.get('plan') or {}).items() if k in ('symbol','side','quantity','limit_prices','condition','invalidation','expires_at')}} for pid,row in plans.items()],
  'previous_assessments':projected['previous_assessments'],'execution_feedback':projected['execution_feedback'],
  'reply_before':req['reply_before']}

async def compile_intent(ex,req,i,*,broker_facts=None):
 action=i['action'];plans=all_plans(req);old=None;pid=None
 if i.get('plan_name'):
  matches=[(k,v) for k,v in plans.items() if label(k,v)==i['plan_name']]
  if len(matches)>1:raise NeedInformation('Plan name is ambiguous; use the exact returned plan reference.')
  if matches:pid,old=matches[0]
 if action in ('revise','cancel') and not old:
  matches=[(k,v) for k,v in plans.items() if (v.get('plan') or {}).get('symbol',v.get('terms',{}).get('symbol'))==i.get('symbol') and v.get('order_status',v.get('status')) not in TERMINAL|{'CANCELED','SUSPENDED'}]
  if i.get('plan_name') or len(matches)!=1:raise NeedInformation('Specify one existing plan_name from the shared context.')
  pid,old=matches[0]
 if action in ('enter','watch') and old:raise NeedInformation('This plan already exists; revise it rather than duplicating it.')
 reply={'request_id':req['request_id'],'context_hash':req['context_hash'],'action':'observe','plan':None,'plan_id':None,'plan_version':None}
 meta=None;reads=[]
 if action in ('observe','clarify'):return reply,None,reads
 if not i.get('symbol'):raise NeedInformation('Market symbol required.')
 if action in ('cancel','revise') and (action=='cancel' or i.get('revision')=='suspend'):
  order=req['context']['active_plans'].get(pid)
  reply.update(action='cancel',plan_id=pid,plan_version=order['plan']['plan_version'] if order else old['version'])
  meta={'id':pid,'name':label(pid,old),'action':'suspend' if action=='revise' else 'cancel','expected_version':old.get('version'),'terms':i}
  return reply,meta,reads
 if action=='watch' or (action=='revise' and i.get('revision')=='resume'):
  meta={'id':pid or req['new_plan_id'],'name':i.get('plan_name') or (label(pid,old) if old else None),'action':'watch','expected_version':(old or {}).get('version'),'terms':i}
  return reply,meta,reads
 ins=q=None
 if 'instrument' in i:
  ins,q=await contract(ex,i['symbol'],i['instrument'],resolved=broker_facts[0] if broker_facts else None,quote=broker_facts[1] if broker_facts else None)
  reads.append({'contract':ins,'quote':q})
 position=None
 if action in ('hold','exit'):
  matches=[p for p in req['context']['positions'] if p['symbol']==i['symbol'] and p['kind']=='option' and (not ins or p['id']==ins['id'])]
  if len(matches)!=1:raise NeedInformation('Specify the held contract; position reference is missing or ambiguous.')
  position=matches[0]
  if not ins:
   rows=await ex.broker.pages('get_option_instruments',{'ids':position['id']},'instruments')
   if len(rows)!=1:raise NeedInformation('Held contract lookup unavailable.')
   r=rows[0];ins,q=await contract(ex,position['symbol'],{'kind':'option','expiry':r['expiration_date'],'strike':r['strike_price'],'right':r['type']});reads.append({'contract':ins,'quote':q})
  related={k:r for k,r in req['context']['active_plans'].items() if r['plan']['option_id']==position['id']}
  working=[(k,r) for k,r in related.items() if r['status'] not in TERMINAL]
  if len(working)>1:raise NeedInformation('Multiple working plans need broker reconciliation before a new position instruction.')
  ref=(working or list(related.items()))[-1] if related else None
  prior=req['context']['position_theses'].get(position['position_id']);version=prior['target_version'] if prior else 0
  target=i.get('target_bid')
  block={'position_id':position['position_id'],'option_id':position['id'],'expected_position_quantity':position['quantity'],
    'expected_unit_cost':position['unit_cost'],'expected_target_version':version,'target_version':version+1,
    'plan_id':ref[0] if ref else None,'plan_version':ref[1]['plan']['plan_version'] if ref else None,
    'action':'SELL_FOR_PROFIT' if action=='exit' else ('REVISE_TARGET' if target is not None else 'HOLD'),
    'target_bid':str(target) if target is not None else (prior['target_bid'] if prior else None),
    'expected_upside_usd':str(i['expected_upside_usd']) if 'expected_upside_usd' in i else None,
    'roundtrip_cost_allowance_usd':str(i['roundtrip_cost_allowance_usd']) if 'roundtrip_cost_allowance_usd' in i else None,
    'reasoning':i.get('reasoning') or 'Explicit position instruction from the sending agent; upside estimate not supplied.'}
  reply['position_blocks']=[block]
  if action=='hold':reply['action']='hold';return reply,None,reads
 if not ins:raise NeedInformation('Exact option expiry/strike/call-or-put required; no catalog registration is necessary.')
 if any(k not in i for k in ('quantity','limit_price','valid_for_seconds')):raise NeedInformation('Specify quantity, limit premium and how long the order instruction remains valid.')
 if action=='revise' and old and (old.get('plan') or {}).get('side')=='sell':raise NeedInformation('Use a fresh position-specific exit assessment to revise a closing order.')
 def cond(key):
  c=i.get(key)
  if not c:return None
  if c['op'] not in ('gte','lte'):raise NeedInformation('The existing order worker supports at-or-above and at-or-below conditions only; clarify this condition.')
  return {'symbol':c['symbol'],'operator':c['op'],'price':str(c['price'])}
 order=req['context']['active_plans'].get(pid) if pid else None
 if order and order['status'] not in TERMINAL:raise NeedInformation('Cancel the working order and wait for broker-confirmed cancellation before replacement.')
 p={'decision_id':req['decision_id'],'plan_id':pid or req['new_plan_id'],'plan_version':order['plan']['plan_version']+1 if order else 1,
  'account_number':ex.broker.account_number,'option_id':ins['id'],'symbol':i['symbol'],'side':'sell' if action=='exit' else 'buy',
  'position_effect':'close' if action=='exit' else 'open','quantity':i['quantity'],'order_type':'limit','limit_prices':[str(i['limit_price'])],
  'stop_price':None,'condition':cond('condition'),'invalidation':cond('invalidation'),'created_at':ex.clock(),
  'expires_at':ex.clock()+i['valid_for_seconds'],'replace_after_seconds':30,'max_transport_attempts':3,
  'position_id':position['position_id'] if position else None,'expected_position_quantity':position['quantity'] if position else None,
  'time_in_force':'gfd','market_hours':'regular_hours'}
 reply.update(action='execute',plan=p)
 meta={'id':p['plan_id'],'name':i.get('plan_name') or (label(pid,old) if old else None),'action':action,'expected_version':(old or {}).get('version'),'terms':i}
 return reply,meta,reads

class SharedChat:
 def __init__(self,service,execution):self.service,self.ex=service,execution;self.lock=asyncio.Lock()
 async def exchange(self,principal,name,args):
  owner=hashlib.sha256(canonical({k:principal[k] for k in ('label','source','conversation')}).encode()).hexdigest()
  if principal['source']!='external-agent' or principal['conversation']!='shared-paper':raise ValueError('shared account access denied')
  if not hasattr(self.ex,'journal'):raise ValueError('Shared account controller unavailable; no simulation fallback')
  if name=='sailboat_message_receipt':
   errors=list(Draft202012Validator(RECEIPT_SCHEMA).iter_errors(args))
   if errors:raise ValueError('Invalid receipt request')
   row=self.ex.worker.status().get('chat_messages',{}).get(args['receipt_id'])
   if row:
    if row['owner']!=owner:raise ValueError('message belongs to a different client')
    result=copy.deepcopy(row['result']);shadow=result.get('shadow_sorter')
    if shadow:
     state=self.ex.worker.status();job=state.get('shape_jobs',{}).get(shadow['receipt_id'],{})
     shadow['status']=job.get('status',shadow['status'])
     sorted_result=state.get('shape_recent',{}).get(shadow['receipt_id'])
     if sorted_result:
      shadow['status']=sorted_result['status'];shadow['sorting']=copy.deepcopy(sorted_result.get('sorting'))
      shadow['research']=copy.deepcopy(sorted_result.get('research',[]))
      shadow['candidate_entry']=copy.deepcopy(sorted_result.get('candidate_entry'))
    return result
   import anyio
   return await anyio.to_thread.run_sync(self.service.execute,principal,name,args) # Historical receipt only.
  check(args)
  async with self.lock:
   key=owner+':'+(args.get('message_id') or hashlib.sha256(args['text'].encode()).hexdigest())
   state=self.ex.worker.status();ident=state.get('chat_keys',{}).get(key);row=state.get('chat_messages',{}).get(ident)
   if row:
    if row['text']!=args['text']:raise ValueError('message_id reused with different text')
    if 'intent' not in args:return row['result']
    fingerprint=hashlib.sha256(canonical(args['intent']).encode()).hexdigest()
    if row.get('intent_hash') and row['intent_hash']!=fingerprint:raise ValueError('accepted or pending message intent cannot be overwritten')
    if row['result']['interpretation_status']!='NEEDS_INTENT':return row['result']
    if args.get('context_receipt')!=ident:raise ValueError('use this message receipt as context_receipt')
    req=self.ex.request_payload(row['request_id'])
    try:
     if row.get('compiled'):reply,proposal,reads=row['compiled']
     else:
      reply,proposal,reads=await compile_intent(self.ex,req,args['intent'])
      self.ex.journal.edit(lambda s:s['chat_messages'][ident].update(intent_hash=fingerprint,compiled=[reply,proposal,reads]))
     result=await submit(self.ex,req,reply,source='chat:'+principal['label'],reasoning=args['intent'].get('reasoning') or args['text'][:4000],
       allow_execution=args['intent']['meaning']=='instruction',proposal=proposal)
     output=row['result']|{'interpretation_status':'COMPLETE','meaning':args['intent']['meaning'],'controller_receipt':result,
       'response':'Resolved against the shared account controller. Read controller_receipt for permission, proposal and execution outcome.',
       'research_results':reads}
    except ValueError as exc:
     output=row['result']|{'interpretation_status':'NEEDS_INFORMATION' if isinstance(exc,NeedInformation) else 'REJECTED',
       'response':str(exc)[:700],'controller_receipt':{'status':'NEEDS_INFORMATION' if isinstance(exc,NeedInformation) else 'REJECTED','reason':str(exc)[:700]}}
    def save(s):s['chat_messages'][ident].update(intent_hash=fingerprint,result=copy.deepcopy(output))
    self.ex.journal.edit(save);return output
   if args.get('context_receipt'):raise ValueError('unknown message context; start with text')
   if len(state.get('chat_messages',{}))>=1024:raise ValueError('message capacity reached; owner maintenance required')
   req=await self.ex.context('Interpret the sender message against this shared account context. No permission change.')
   ident='chat-message-'+uuid.uuid4().hex
   result={'receipt_id':ident,'status':'RECEIVED','interpretation_status':'NEEDS_INTENT',
     'context':plain_context(self.ex,req),'response':'Supply faithful intent using this context and receipt. No plan or order was created by receipt alone.'}
   def save(s):
    s.setdefault('chat_keys',{})[key]=ident
    s.setdefault('chat_messages',{})[ident]={'owner':owner,'text':args['text'],'request_id':req['request_id'],'result':copy.deepcopy(result)}
    if getattr(self.ex,'shape_runtime',None):
     from .shape_runtime import enqueue
     result['response']='Text received for sorting and owner-policy candidate evaluation; no BUY instruction or intent block is required for that evaluation. Execution uses the configured candidate gate and existing owner controls. Recover this receipt for the result; receipt alone is not an order or a fill.'
     result['shadow_sorter']={'receipt_id':enqueue(s,'chat:'+ident,args['text'],self.ex.clock(),'chat:'+principal['label'],candidate_live_enabled=self.ex.shape_runtime.candidate_entry.allow_execution),'status':'PENDING','broker_submission':False}
     s['chat_messages'][ident]['result']=copy.deepcopy(result)
   self.ex.journal.edit(save);return result
