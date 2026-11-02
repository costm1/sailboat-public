"""Research-only external role. Host journals text and forwards it to the existing sorter."""
import asyncio,copy,hashlib,json
from pathlib import Path
from datetime import datetime
from jsonschema import validate
from .controller import canonical
from .market_hours import EASTERN

PROTOCOL='qqq_options_analyst_v1'
PROMPT=Path(__file__).with_name('analyst_prompt.md').read_text(encoding='utf-8')
ENTRY_PROMPT=Path(__file__).with_name('entry_analyst_prompt.md').read_text(encoding='utf-8')
SCHEMA={'type':'object','additionalProperties':False,'required':['analysis','uncertainty','missing_information','market_reads'],'properties':{
 'analysis':{'type':'string','maxLength':12000},'uncertainty':{'type':'string','maxLength':1500},
 'missing_information':{'type':'array','maxItems':12,'items':{'type':'string','maxLength':250}},
 'market_reads':{'type':'array','maxItems':2,'items':{'type':'object','additionalProperties':False,'required':['tool','arguments_json'],'properties':{
  'tool':{'enum':['get_equity_quotes','get_equity_historicals','get_option_chains','get_option_instruments','get_option_quotes','get_option_historicals']},
  'arguments_json':{'type':'string','maxLength':4000}}}}}}

def envelope(value):
 validate(value,SCHEMA)
 if value['market_reads'] and value['analysis'].strip():raise ValueError('Research request must precede final analysis')
 if not value['market_reads'] and not value['analysis'].strip():raise ValueError('Nonblank final analysis required')
 return value

def prompt_for(context):
 # Holdings use their own management task, never the entry probability comparison.
 if context is None or any(h.get('instrument_id') and h.get('symbol')=='QQQ' for h in context.get('holdings',[])):return PROMPT
 return ENTRY_PROMPT

def research_context(req,notes):
 c=req['context'];holdings=[]
 for p in c['positions']:
  row={k:copy.deepcopy(p[k]) for k in ('symbol','quantity','unit_cost','cost_basis','multiplier','expiration_date','mark_bid','market_value','unrealized_pnl','strike_price','option_type') if k in p}
  if p.get('kind')=='option':row['instrument_id']=p['id']
  holdings.append(row)
 # Fresh broker holdings are authoritative. Do not recycle an account-wide narrative.
 return {'observed_at':c['observed_at'],'buying_power':c['buying_power'],'holdings':holdings}


def account_question(context):
 from decimal import Decimal
 held=[p for p in context['holdings'] if p.get('instrument_id')]
 if not held:
  return (f"You currently have ${context['buying_power']} available.\n\n"
   "Identify the most promising QQQ 0DTE CALL and the most promising PUT, with exact strike and expiration.\n\n"
   "For each, estimate the probability of profitable premium resale within the next 30 minutes as a percentage. The estimates are independent and need not add up to 100%. Include current ask, realistic future bid range, potential premium appreciation or deterioration over that period, and your reasoning. State any unavailable contract or probability explicitly.")
 details='; '.join(f"{p['quantity']} {p['symbol']} ${p['strike_price']} {p['option_type']} contracts expiring {p['expiration_date']}, "
  f"average cost ${Decimal(p['unit_cost'])/Decimal(p['multiplier'])} per share (${p['unit_cost']} per contract), "
  f"current bid/ask ${p['mark_bid']}/${p['mark_ask']}" for p in held)
 return (f"You currently hold {details}.\n\nLooking at QQQ right now, what do you think?\n\n"
  "Would you continue holding this position for further appreciation, or would you sell it?\n\n"
  "If you'd hold, what executable resale bid range would you reasonably expect over approximately 30 minutes, with what potential premium appreciation or deterioration, and why? Compare taking the current bid with waiting for further appreciation, after costs. This holding is reassessed every two minutes; 30 minutes is not an automatic exit deadline. Begin with one standalone Current conclusion line, identify the held contract, and keep Reasoning, Resale outlook and Future conditions separate."+
  ("\n\nPlease assess each position separately." if len(held)>1 else ''))

async def assess(s,market):
 from .order_schedule import market_brief,MAX_LOOKUP_ROUNDS
 from .agent_contract import READS
 from .live_broker import CONTRACTS
 from .robinhood_orders import data
 from .shared_assessment import submit
 from .shape_runtime import enqueue
 ex=s.execution
 if not getattr(ex,'shape_runtime',None):raise ValueError('Shared text sorter unavailable; no legacy order-agent fallback')
 # Independent input reads overlap; all results are awaited before inference.
 inputs=await asyncio.gather(ex.context('Internal context for market-analysis handoff.'),
     s.fetch(),market_brief(s),return_exceptions=True)
 for value in inputs:
  if isinstance(value,BaseException):raise value
 req,(obs,quotes),history=inputs
 start=ex.worker.status();mode=start['mode'];epoch=start.get('control_epoch',0);boot=start.get('boot_id')
 if req['context']['mode']!=mode:raise ValueError('Owner state changed during context preparation')
 from .held_analysis import enrich,compile_response,VERSION as HELD_VERSION
 context=research_context(req,start.get('agent_assessments',[]))
 held_facts=await enrich(ex,req,context)
 managing=any(p['kind']=='option' and p['symbol']=='QQQ' for p in req['context']['positions'])
 request={'agent_protocol':PROTOCOL,'instructions':prompt_for(context),'response_schema':SCHEMA,
  'current_time':s.clock(),'trading_date':datetime.fromtimestamp(s.clock(),EASTERN).date().isoformat(),
  'context':context,'question':account_question(context),
  'market_observations':{'source':obs['source'],'quotes':quotes},'historical_observations':history,
  'market_read_schemas':{n:CONTRACTS[n]['input'] for n in READS},'market_results':[]}
 summary={'assessment_prompt_version':PROTOCOL,'input_sha256':hashlib.sha256(canonical(request).encode()).hexdigest(),
  'context_fields':list(request['context']),'execution_mode_fields_supplied':False,'controller_decision_schema_supplied':False}
 await s.tx(lambda c,st:st['active'].update(route=PROTOCOL,phase='DISPATCHED',request_id=req['request_id'],decision_id=req['decision_id'],input_summary=summary,managing_positions=managing))
 usage=[];last=None
 for round_no in range(MAX_LOOKUP_ROUNDS+1):
  request['current_time']=s.clock()
  last,u=await s.provider.decide(request);usage.append(u);envelope(last)
  request['market_results'].extend(u.get('market_results',[]))
  if not last['market_reads']:break
  if round_no==MAX_LOOKUP_ROUNDS:raise ValueError('Research budget exhausted without final analysis')
  for read in last['market_reads']:
   args=json.loads(read['arguments_json']);validate(args,CONTRACTS[read['tool']]['input'])
   value=await s.broker.read(read['tool'],args,persist=False)
   if value['result'].get('isError'):raise ValueError('Market research unavailable')
   raw=data(value['result']);truncated=False
   for k in ('instruments','results','chains','historicals'):
    if isinstance(raw.get(k),list) and len(raw[k])>25:raw[k]=raw[k][:25];truncated=True
   packet={'tool':read['tool'],'arguments':args,'retrieved_at':value['retrieved_at'],'data':raw,'truncated':truncated,'complete':not truncated and not bool(raw.get('next'))}
   if len(canonical(packet))>60000:raise ValueError('Narrow the market research request')
   request['market_results'].append(packet)
 if last['market_reads']:raise ValueError('Final analysis missing')
 from .analysis_log import receive
 await s.tx(lambda c,st:receive(c,st,req,last,s.provider.name,{'provider':s.provider.name,'rounds':usage,'agent_calls':len(usage)},s.clock()))
 current=ex.worker.status()
 if current['mode']!=mode or current.get('control_epoch',0)!=epoch or current.get('boot_id')!=boot:raise ValueError('Owner state changed during analysis')
 if s.clock()>=req['reply_before']:raise ValueError('Analysis context expired')
 if s.market_hours and not s.market_hours.status(s.clock())['open']:raise ValueError('Market closed during analysis')
 # The analyst supplies prose; only the host compiles internal controller fields.
 response={'request_id':req['request_id'],'context_hash':req['context_hash'],'action':'observe','plan':None,'plan_id':None,'plan_version':None}
 held_results=[]
 held_assemblies=[]
 if managing:
  try:response,held_results=await compile_response(ex,req,last['analysis'],held_facts)
  except ValueError as exc:
   held_results=[{'status':'BLOCKED','reason':str(exc)[:400]}]
  from .assembly_bridge import AssemblyBridge
  held_assemblies=AssemblyBridge(ex).held(req,last['analysis'],held_facts,held_results)
 assessment=copy.deepcopy(last);assessment['reasoning']=last['analysis'];assessment['held_position_blocks']=held_results
 # receive() already saved flat analysis and usage durably. Held interpretation
 # adds new position-specific results and still needs its own durable save.
 if managing:
  await s.tx(lambda c,st:st['active'].update(phase='RESPONSE_READY',assessment=assessment,usage={'provider':s.provider.name,'rounds':usage,'agent_calls':len(usage)}))
 receipt=await submit(ex,req,response,source='scheduled:'+s.provider.name,reasoning=last['analysis'],
  uncertainty=last['uncertainty'],missing_information=last['missing_information'],allow_execution=managing,
  metadata={'analyst_protocol':PROTOCOL,'held_analysis_version':HELD_VERSION,'held_position_blocks':held_results,'held_assemblies':held_assemblies,
   'decision_author':'Sailboat translation of held-position analysis' if managing else 'Host analysis receipt; no agent controller decision'})
 def queue(state):
  if state['mode']!=mode or state.get('control_epoch',0)!=epoch or state.get('boot_id')!=boot:raise ValueError('Owner state changed before text handoff')
  return enqueue(state,'scheduled:'+req['request_id'],last['analysis'],s.clock(),'scheduled:'+s.provider.name,
     candidate_live_enabled=ex.shape_runtime.candidate_entry.allow_execution,
     domain_context={'symbol':'QQQ','expiry':datetime.fromtimestamp(req['context']['observed_at'],EASTERN).date().isoformat(),
                     'basis':'Host-issued QQQ 0DTE analyst task','request_id':req['request_id']})
 key=None if managing else await asyncio.to_thread(ex.journal.edit,queue)
 return await s.tx(lambda c,st:s.finish(c,st,'ANALYSIS_RECEIVED',receipt=receipt,assessment=assessment,
  usage=st['active']['usage'],provider=s.provider.name,input_summary=summary,sorter_receipt=key,market_results=request['market_results']))
