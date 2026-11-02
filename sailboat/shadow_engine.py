"""QQQ 0DTE research policy. There is deliberately no live submit/arm path."""
import asyncio,copy,hashlib,math
from dataclasses import dataclass,asdict
from datetime import datetime
from decimal import Decimal,ROUND_CEILING
from .controller import canonical
from .market_hours import EASTERN,RegularMarketHours
from .scheduler import timestamp
from .live_broker import option_quote
from .shadow_clues import extract,values
D=lambda x:Decimal(str(x))

@dataclass(frozen=True)
class ShadowPolicy:
 version:str='qqq-breakout-shadow-v1'
 # Draft research settings only. Never owner-approved live strategy limits.
 max_premium_usd:float=200
 buying_power_fraction:float=.15
 max_quantity:int=1
 max_quote_age:float=15
 max_message_age:float=900
 max_underlying_drift:float=.10
 max_spread:float=.08
 max_relative_spread:float=.05
 min_size:int=5
 min_volume:int=100
 min_open_interest:int=100
 delta_min:float=.25
 delta_max:float=.75
 volume_ratio:float=1.5
 horizon_seconds:int=120
 max_projected_move:float=.50
 minimum_scenario_profit_usd:float=5
 iv_down_points:float=1
 fee_per_contract_roundtrip:float|None=None
 fee_evidence:str|None=None
 greek_units_evidence:str|None=None
 def __post_init__(self):
  for k,v in asdict(self).items():
   if isinstance(v,(int,float)) and (not math.isfinite(v) or v<=0):
    if k in ('fee_per_contract_roundtrip','iv_down_points') and v==0:continue
    raise ValueError('invalid policy '+k)
  if any(type(getattr(self,k)) is not int for k in ('max_quantity','min_size','min_volume','min_open_interest','horizon_seconds')):raise ValueError('integer policy counts required')
  if not 0<self.buying_power_fraction<=1 or self.delta_min>self.delta_max:raise ValueError('invalid policy bounds')
  if self.fee_per_contract_roundtrip is not None and not self.fee_evidence:raise ValueError('fee allowance needs provenance')

class ResearchPort:
 """Uses the EXISTING broker read adapter, without order review/place/cancel methods."""
 def __init__(self,broker):self.broker=broker
 async def read(self,name,args):
  if name not in ('get_equity_quotes','get_equity_historicals','get_option_instruments','get_option_quotes'):raise ValueError('research read denied')
  return await self.broker.read(name,args)

async def gather(port,clues,now,account,policy):
 """Bounded chain discovery; no synthetic IDs, guessed quote times or stale-price fallback."""
 reads=[];errors=[]
 async def read(name,args):
  v=await port.read(name,args);reads.append({'tool':name,'arguments':args,'research_started_at':now,'data':copy.deepcopy(v)});return v
 packet={'asof':now,'origin':'broker_read','account':copy.deepcopy(account),'reads':reads,'errors':errors,'instruments':[],'quotes':[],'bars':[]}
 day=datetime.fromtimestamp(now,EASTERN).date().isoformat()
 try:
  raw=await read('get_equity_quotes',{'symbols':['QQQ']});packet['underlying']=raw['results'][0]['quote']
  spot=float(packet['underlying']['last_trade_price'])
  if not math.isfinite(spot) or spot<=0:raise ValueError('invalid underlying')
  from datetime import timezone
  iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
  hist=await read('get_equity_historicals',{'symbols':['QQQ'],'start_time':iso(now-2400),'end_time':iso(now),'interval':'5minute','bounds':'regular'})
  rows=[r for r in hist['results'] if r['symbol']=='QQQ' and r['interval']=='5minute' and r['bounds']=='regular']
  if len(rows)!=1:raise ValueError('historical identity mismatch')
  packet['bars']=rows[0]['bars']
  strikes=sorted(set(values(clues,'strike')))
  if len(strikes)>3:raise ValueError('too many or ambiguous explicit strikes')
  for side in ('call','put'):
   rows=[];cursor=None
   for _ in range(3):
    args={'chain_symbol':'QQQ','expiration_dates':day,'type':side,'state':'active'}
    if len(strikes)==1:args['strike_price']=str(strikes[0])
    if cursor:args['cursor']=cursor
    page=await read('get_option_instruments',args);rows+=page.get('instruments') or []
    next_cursor=page.get('next')
    if not next_cursor:break
    if next_cursor==cursor:raise ValueError('repeated chain cursor')
    cursor=next_cursor
   else:raise ValueError('chain incomplete within research budget')
   rows=[r for r in rows if r['chain_symbol']=='QQQ' and r['expiration_date']==day and r['type']==side]
   if strikes:rows=[r for r in rows if float(r['strike_price']) in strikes]
   rows=sorted(rows,key=lambda r:(abs(float(r['strike_price'])-spot),float(r['strike_price']),r['id']))[:3]
   packet['instruments']+=rows
  ids=[r['id'] for r in packet['instruments']]
  if len(ids)!=len(set(ids)):raise ValueError('duplicate instrument identity')
  if ids:packet['quotes']=(await read('get_option_quotes',{'instrument_ids':ids}))['results']
  packet['underlying_after']=(await read('get_equity_quotes',{'symbols':['QQQ']}))['results'][0]['quote']
 except Exception as exc:errors.append(type(exc).__name__+': '+str(exc)[:160])
 return packet

def evaluate(clues,packet,now,message_at,policy):
 p=policy;issues=list(clues['warnings']);inferences=[];candidates=[]
 day=datetime.fromtimestamp(now,EASTERN).date().isoformat();session=RegularMarketHours().status(now)
 if not session['open']:issues.append('market_closed')
 if session.get('close') and now>=session['close']-1800:issues.append('last_30_minutes_no_new_entry')
 if not -1<=now-message_at<=p.max_message_age:issues.append('message_stale_or_future')
 if not -1<=now-packet.get('asof',0)<=p.max_quote_age:issues.append('research_packet_stale')
 issues+=packet.get('errors',[])
 if any(v!=day for v in values(clues,'expiry')):issues.append('explicit_expiry_not_today')
 if values(clues,'restriction'):issues.append('explicit_source_restriction_preserved')
 acct=packet.get('account',{})
 if not -1<=now-acct.get('observed_at',0)<=30:issues.append('account_context_stale')
 if acct.get('positions') or acct.get('pending_orders'):issues.append('existing_exposure_no_new_shadow_entry')
 def number(v):
  n=float(v)
  if not math.isfinite(n):raise ValueError('nonfinite number')
  return n
 spot=None;bars=[];ratio=None;move=None
 try:
  for key in ('underlying','underlying_after'):
   q=packet[key]
   if q['symbol']!='QQQ' or q.get('state')!='active':raise ValueError('underlying identity')
   for field in ('venue_bid_time','venue_ask_time','venue_last_trade_time'):
    if not -1<=now-timestamp(q[field])<=p.max_quote_age:raise ValueError('underlying stale')
   if not 0<number(q['bid_price'])<=number(q['ask_price']):raise ValueError('crossed underlying')
  spot=number(packet['underlying_after']['last_trade_price'])
  if abs(spot-number(packet['underlying']['last_trade_price']))>p.max_underlying_drift:issues.append('price_moved_during_research')
  for b in packet['bars']:
   if b is None or b.get('interpolated'):continue
   ts=timestamp(b['begins_at'])
   if now-2400<=ts<=now-300:bars.append(b)
  bars.sort(key=lambda b:timestamp(b['begins_at']));bars=bars[-6:]
  if len(bars)<6 or any(abs(timestamp(bars[i+1]['begins_at'])-timestamp(bars[i]['begins_at'])-300)>1 for i in range(5)):raise ValueError('six contiguous completed bars required')
  if now-(timestamp(bars[-1]['begins_at'])+300)>300:raise ValueError('completed bar stale')
  for b in bars:
   lo,hi=number(b['low_price']),number(b['high_price'])
   if not 0<lo<=number(b['close_price'])<=hi or not lo<=number(b['open_price'])<=hi or number(b['volume'])<0:raise ValueError('invalid OHLCV')
  baseline=sum(number(b['volume']) for b in bars[:-1])/5
  if baseline<=0:raise ValueError('volume baseline missing')
  ratio=number(bars[-1]['volume'])/baseline
  move=max(-p.max_projected_move,min(p.max_projected_move,(number(bars[-1]['close_price'])-number(bars[-2]['close_price']))*p.horizon_seconds/300))
 except (KeyError,ValueError,TypeError) as e:issues.append('market_evidence: '+str(e))
 conds={}
 for side in ('call','put'):
  direct=[f for f in clues['fields'] if f['field']=='entry_condition' and f['value']['right']==side]
  if len(direct)==1:conds[side]=direct[0]['value']|{'origin':'extracted','evidence':direct[0]['evidence']}
  elif len(direct)>1:issues.append('ambiguous_'+side+'_condition')
  else:
   op='gte' if side=='call' else 'lte'
   thresholds=[f for f in clues['fields'] if f['field']=='threshold' and f['value']['op']==op]
   if len(thresholds)==1:
    conds[side]=thresholds[0]['value']|{'right':side,'origin':'policy_inference','evidence':thresholds[0]['evidence']}
    inferences.append({'rule':'breakout_direction_mapping','value':conds[side],'note':'Sailboat maps an unbound above/below threshold to call/put; not an agent recommendation.'})
   elif len(thresholds)>1:issues.append('ambiguous_'+side+'_threshold')
   elif len(bars)==6:
    price=(max(number(b['high_price']) for b in bars[:-1]) if side=='call' else min(number(b['low_price']) for b in bars[:-1]))
    conds[side]={'right':side,'op':op,'price':price,'origin':'policy_inference','evidence':'prior five completed bar high/low'}
    inferences.append({'rule':'prior_25_minute_range','value':conds[side]})
 for side,c in conds.items():
  if c['op']!=('gte' if side=='call' else 'lte'):issues.append('condition_incompatible_with_breakout_policy')
 ids=[i.get('id') for i in packet.get('instruments',[])]
 if len(ids)!=len(set(ids)):issues.append('duplicate_instrument_identity')
 quotes={}
 for q in packet.get('quotes',[]):
  key=(q.get('quote') or {}).get('instrument_id')
  if key in quotes:issues.append('duplicate_quote')
  quotes[key]=q
 explicit=set(values(clues,'strike'));seen=set()
 for ins in packet.get('instruments',[]):
  cid=ins.get('id');side=ins.get('type');reasons=[];row={'contract':copy.deepcopy(ins),'origin':'sailboat_policy','blockers':reasons,'qualified':False}
  candidates.append(row)
  try:
   if cid in seen:raise ValueError('duplicate instrument')
   seen.add(cid)
   if ins['chain_symbol']!='QQQ' or ins['expiration_date']!=day or side not in ('call','put') or number(ins['trade_value_multiplier'])!=100 or ins['underlying_type']!='equity':raise ValueError('unsupported contract identity')
   if explicit and number(ins['strike_price']) not in explicit:raise ValueError('explicit strike mismatch')
   if ins['state']!='active' or ins['tradability']!='tradable':reasons.append('contract_not_openable')
   if timestamp(ins['sellout_datetime'])<=now+p.horizon_seconds+60:reasons.append('contract_cutoff')
   raw=quotes[cid];q=raw['quote'];row['raw_quote']=copy.deepcopy(q);oq=option_quote(raw,now)
   if now-oq['asof']>p.max_quote_age:raise ValueError('quote too old for policy')
   bid,ask=number(oq['bid']),number(oq['ask']);spread=ask-bid
   if bid<=0 or spread>p.max_spread or spread/ask>p.max_relative_spread:reasons.append('spread_or_zero_bid')
   if min(number(q['bid_size']),number(q['ask_size']))<p.min_size or number(q['volume'])<p.min_volume or number(q['open_interest'])<p.min_open_interest:reasons.append('liquidity')
   c=conds.get(side);row['entry_condition']=c
   if not c or spot is None or len(bars)!=6:reasons.append('condition_unresolved')
   else:
    hit=spot>c['price'] and number(bars[-1]['close_price'])>c['price'] if side=='call' else spot<c['price'] and number(bars[-1]['close_price'])<c['price']
    if not hit:reasons.append('breakout_not_confirmed')
   if ratio is None or ratio<p.volume_ratio:reasons.append('volume_not_confirmed')
   row['volume_ratio']=ratio
   delta,gamma,theta,vega,iv=[number(q[x]) for x in ('delta','gamma','theta','vega','implied_volatility')]
   if (side=='call' and not 0<delta<=1) or (side=='put' and not -1<=delta<0) or gamma<0 or theta>0 or vega<0 or iv<=0:raise ValueError('invalid Greeks')
   if not p.delta_min<=abs(delta)<=p.delta_max:reasons.append('delta_outside_policy')
   if not p.greek_units_evidence:reasons.append('Greek_units_unverified')
   if move is None or delta*move<=0:reasons.append('momentum_opposes_side')
   fee=p.fee_per_contract_roundtrip
   if fee is None:reasons.append('costs_unknown')
   # Conditional sensitivity scenario, NOT a probability or expected value estimate.
   # Units: prices per share, theta per calendar day, vega per IV percentage point.
   scenario=bid+delta*(move or 0)+.5*gamma*(move or 0)**2+theta*p.horizon_seconds/86400-vega*p.iv_down_points
   row['scenario']={'underlying_move':move,'iv_change_percentage_points':-p.iv_down_points,'resale_bid':round(max(0,scenario),6),'horizon_seconds':p.horizon_seconds,'interpretation':'conditional continuation sensitivity, not calibrated expected profit','Greek_units_evidence':p.greek_units_evidence}
   ticks=ins['min_ticks'];tick=D(ticks['above_tick'] if D(ask)>=D(ticks['cutoff_price']) else ticks['below_tick'])
   if tick<=0:raise ValueError('invalid minimum tick')
   limit=(D(ask)/tick).to_integral_value(rounding=ROUND_CEILING)*tick
   power=number(acct['buying_power']);budget=min(p.max_premium_usd,max(0,power)*p.buying_power_fraction)
   qty=min(p.max_quantity,int(budget//(float(limit)*100+(fee or 0))),int(number(q['ask_size'])),int(number(q['bid_size']))) if fee is not None else 0
   target=( (limit+(D(fee)+D(p.minimum_scenario_profit_usd))/100)/tick).to_integral_value(rounding=ROUND_CEILING)*tick if fee is not None else None
   # Target can cross the tick regime; round once more using the target regime.
   if target is not None:
    tt=D(ticks['above_tick'] if target>=D(ticks['cutoff_price']) else ticks['below_tick']);target=(target/tt).to_integral_value(rounding=ROUND_CEILING)*tt
   net=(scenario-float(limit))*100-fee if fee is not None else None
   if qty<1:reasons.append('not_affordable_with_cost_allowance')
   if net is None or net<p.minimum_scenario_profit_usd or (target is not None and scenario<float(target)):reasons.append('scenario_profit_insufficient')
   row.update(limit_entry=str(limit),quantity=qty,target_bid=str(target) if target is not None else None,net_scenario_per_contract=round(net,4) if net is not None else None,fee_allowance=fee,fee_evidence=p.fee_evidence,budget_usd=budget)
   row['qualified']=not reasons and not issues
  except (KeyError,TypeError,ValueError,ArithmeticError) as exc:reasons.append('unavailable_or_invalid: '+str(exc)[:150])
 if not candidates:issues.append('no_resolved_contracts')
 candidates.sort(key=lambda c:(not c['qualified'],c.get('net_scenario_per_contract') is None,-(c.get('net_scenario_per_contract') or 0),str(c['contract'].get('id'))))
 for rank,c in enumerate(candidates,1):
  c['research_rank']=rank;c['ranking_basis']='conditional_net_premium_scenario' if c.get('net_scenario_per_contract') is not None else 'unscored_missing_evidence; display order is not preference'
 winners=[c for c in candidates if c['qualified']];selected=winners[0] if winners else None
 if len(winners)>1 and winners[0]['contract']['type']!=winners[1]['contract']['type'] and abs(winners[0]['net_scenario_per_contract']-winners[1]['net_scenario_per_contract'])<.01:issues.append('tied_opposing_candidates');selected=None
 proposal=None
 if selected:
  ins=selected['contract'];c=selected['entry_condition']
  proposal={'author':'sailboat_policy','direction':ins['type'].upper(),'symbol':'QQQ','strike':ins['strike_price'],'expiration':ins['expiration_date'],'option_id':ins['id'],'limit_entry':selected['limit_entry'],'quantity':selected['quantity'],'condition':{'symbol':'QQQ','operator':c['op'],'price':str(c['price'])},'target_bid':selected['target_bid'],'horizon_seconds':p.horizon_seconds,'reason':'Confirmed range break, volume expansion, bounded liquidity/spread, affordable size and positive conditional premium scenario after stated cost allowance.','target_criteria':'Recheck executable bid and actual costs; target is not guaranteed. No automatic loss sale or max-hold liquidation.'}
 return {'policy':asdict(p),'policy_authority':'DRAFT_SHADOW_ONLY_REQUIRES_SEPARATE_LIVE_APPROVAL','status':'SHADOW_CANDIDATE' if proposal else 'NO_QUALIFYING_CANDIDATE','extraction':clues,'inferences':inferences,'resolved_conditions':conds,'research':packet,'global_blockers':sorted(set(issues)),'candidates':candidates,'proposal':proposal,'would_submit':False,'broker_submission':False,'decision_author':'sailboat_policy','agent_decision_unchanged':clues['agent_stated_decisions']}

class ShadowEngine:
 """Explicitly invoked shadow path. Not attached to scheduler or chat ingress by default."""
 def __init__(self,execution,policy=None):self.ex=execution;self.policy=policy or ShadowPolicy();self.lock=asyncio.Lock()
 async def assess(self,message_id,text,message_at,port=None,packet=None):
  async with self.lock:
   digest=hashlib.sha256(canonical({'text':text,'at':message_at,'policy':asdict(self.policy)}).encode()).hexdigest()
   old=self.ex.worker.status().get('shadow_decisions',{}).get(message_id)
   if old:
    if old['input_hash']!=digest:raise ValueError('shadow message identity conflict; prior result preserved')
    return copy.deepcopy(old)
   if len(self.ex.worker.status().get('shadow_decisions',{}))>=100:raise ValueError('shadow evidence capacity reached')
   req=await self.ex.context('Shadow research only; never submit, arm, cancel or modify a trading plan.')
   now=self.ex.clock();clues=extract(text)
   if packet is None:
    try:packet=await asyncio.wait_for(gather(port or ResearchPort(self.ex.broker),clues,now,req['context'],self.policy),timeout=30)
    except TimeoutError:packet={'asof':now,'account':req['context'],'errors':['research_deadline_exceeded'],'reads':[],'instruments':[],'quotes':[],'bars':[]}
   else:packet=copy.deepcopy(packet);packet['account']=copy.deepcopy(req['context'])
   result=evaluate(clues,packet,self.ex.clock(),message_at,self.policy)
   result.update(message_id=message_id,input_hash=digest,created_at=self.ex.clock(),context_request_id=req['request_id'],research_sha256=hashlib.sha256(canonical(packet).encode()).hexdigest())
   result['controller_preview']=None
   if result['proposal']:
    c=result['proposal'];p={'decision_id':req['decision_id'],'plan_id':req['new_plan_id'],'plan_version':1,'account_number':self.ex.broker.account_number,'option_id':c['option_id'],'symbol':'QQQ','side':'buy','position_effect':'open','quantity':c['quantity'],'order_type':'limit','limit_prices':[c['limit_entry']],'stop_price':None,'condition':c['condition'],'invalidation':None,'created_at':self.ex.clock(),'expires_at':self.ex.clock()+60,'replace_after_seconds':30,'max_transport_attempts':3,'position_id':None,'expected_position_quantity':None,'time_in_force':'gfd','market_hours':'regular_hours'}
    response={'request_id':req['request_id'],'context_hash':req['context_hash'],'action':'execute','plan':p,'plan_id':None,'plan_version':None}
    from .order_schedule import preview
    try:result['controller_preview']=await preview(self.ex,req,response)
    except (ValueError,KeyError) as exc:result['controller_preview']={'status':'REJECTED','reason':str(exc)[:200]}
    result['controller_response_proposal']=response
   def save(s):
    existing=s.setdefault('shadow_decisions',{}).get(message_id)
    if existing:
     if existing['input_hash']!=digest:raise ValueError('concurrent shadow identity conflict')
     return existing
    s['shadow_decisions'][message_id]=copy.deepcopy(result);return result
   return self.ex.journal.edit(save)
