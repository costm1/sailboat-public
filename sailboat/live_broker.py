"""Real-account reads, deterministic limits and a durable daily loss ledger.
No orders are constructed from commentary. Broker data is never replaced by paper state.
"""
import asyncio,copy,json,time
from datetime import datetime,timedelta
from decimal import Decimal
from pathlib import Path
from jsonschema import validate
from .market_hours import RegularMarketHours,EASTERN
from .order_execution import positive,TERMINAL
from .robinhood_orders import data,RobinhoodOptionTransport,reconcile_known_order
from .scheduler import timestamp,normalize_quotes

D=Decimal
CONTRACTS=json.loads(Path(__file__).with_name('broker_contracts.json').read_text())
def dec(x):
 v=D(str(x))
 if not v.is_finite():raise ValueError('nonfinite broker amount')
 return v

def integer(x):
 v=dec(x)
 if v<0 or v!=v.to_integral_value():raise ValueError('invalid contract count')
 return int(v)

def option_quote(row,now):
 q=row['quote'];bid=dec(q['bid_price']);ask=positive(q['ask_price']);ts=timestamp(q['updated_at'])
 if bid<0 or bid>ask or not -1<=now-ts<=30:raise ValueError('stale/crossed option quote')
 return {'id':q['instrument_id'],'bid':str(bid),'ask':str(ask),'asof':ts,'close':row.get('close')}

def daily_realized(pnl,account,positions,prior,option_orders,equity_orders,now):
 """Use a reported total; never convert a missing gain into a guessed zero."""
 if pnl['account_number']!=account or pnl['display_currency']!='USD' or pnl['window']!='day':
  raise ValueError('daily PnL account/currency/window mismatch')
 rows=pnl['data_points']
 if rows is None:raise ValueError('daily PnL buckets unavailable')
 total=dec(pnl['total_returns'])
 if all(x['realized_gain'] is not None for x in rows):
  return total,'broker_realized_total'
 # Broker may emit a null-gain, zero-trade placeholder on a no-activity day.
 # Only accept its explicit zero total with independent complete book/order evidence.
 if total!=0:
  raise ValueError('missing realized gain with nonzero reported total')
 day=datetime.fromtimestamp(now,EASTERN).date()
 midnight=datetime.combine(day,datetime.min.time(),EASTERN).timestamp()
 for row in rows:
  if row['number_of_trades']!=0 or (row['realized_gain'] is not None and dec(row['realized_gain'])!=0):
   raise ValueError('missing daily gain with reported trades or gains')
  start=timestamp(row['start_time']);end=timestamp(row['end_time'])
  if start!=midnight or not start<=end<=now+1:
   raise ValueError('no-activity bucket is not from the current Eastern day')
 if option_orders is None or equity_orders is None:raise ValueError('incomplete order history')
 if positions or (prior and prior.get('positions')):
  from .opening_only_risk import verified_opening_only
  return verified_opening_only(positions,prior,option_orders,equity_orders,now)
 for kind,orders in (('option',option_orders),('equity',equity_orders)):
  for order in orders:
   quantity=dec(order['processed_quantity' if kind=='option' else 'cumulative_quantity'])
   if quantity<0:raise ValueError('invalid filled quantity')
   if quantity==0:continue
   # An old GTC order may fill today. Do not rely on its creation date.
   stamp=order.get('updated_at') if kind=='option' else order.get('last_transaction_at')
   if not stamp or timestamp(stamp)>=midnight:
    raise ValueError('missing daily gain with recent or undated execution')
   executions=order.get('executions') or []
   if kind=='option':
    executions=[x for leg in order.get('legs') or [] for x in leg.get('executions') or []]
   if any(timestamp(x['timestamp'])>=midnight for x in executions):
    raise ValueError('missing daily gain with same-day execution')
 return total,'broker_zero_total_verified_flat_no_activity'

class AccountRisk:
 def __init__(self,journal,clock=time.time):self.journal=journal;self.clock=clock
 def update(self,realized,positions,quotes,observation=None):
  now=self.clock();day=datetime.fromtimestamp(now,EASTERN).date().isoformat()
  def edit(s):
   if observation is not None:
    from .position_updates import record_book
    record_book(s,observation)
   days=s.setdefault('live_risk_days',{});prior=s.get('live_risk_last');book=days.get(day)
   if day in s.get('live_risk_unverified_days',[]):return {'basis_error':'daily opening basis remains unverified; entries remain blocked'}
   if book is None:
    # Bootstrap only with a flat book; never assume an unknown opening basis is zero.
    if not prior:
     if positions:
      s.setdefault('live_risk_unverified_days',[]).append(day)
      s['live_risk_last']={'day':day,'positions':copy.deepcopy(positions),'observed_at':now}
      return {'basis_error':'initial daily basis unavailable while holdings exist'}
     baseline=D(0)
    elif prior['day']==day:raise ValueError('missing durable daily risk record')
    else:
     baseline=D(0)
     for p in prior['positions']:
      q=quotes.get(p['id']);close=q.get('close') if q else None
      if not close or close.get('interpolated') or close.get('price') is None:raise ValueError('official opening mark unavailable')
      close_day=close.get('date')
      yesterday=datetime.fromisoformat(day).date()-timedelta(days=1)
      calendar=RegularMarketHours()
      while calendar.session(yesterday) is None and yesterday.year in range(2026,2029):yesterday-=timedelta(days=1)
      if close_day!=yesterday.isoformat():raise ValueError('opening mark is not previous session close')
      baseline+=(dec(close['price'])*D(p['multiplier'])-dec(p['unit_cost']))*D(p['quantity'])
     # Overnight/manual book changes cannot be silently rebased.
     if prior['positions'] and sorted((p['id'],p['quantity']) for p in prior['positions'])!=sorted((p['id'],p['quantity']) for p in positions):
      raise ValueError('overnight book changed before baseline reconciliation')
    book={'baseline_unrealized':str(baseline),'locked':False};days[day]=book
   unreal=D(0)
   for p in positions:
    q=quotes.get(p['id'])
    if q is None:raise ValueError('missing held-position mark')
    unreal+=(dec(q['bid'])*D(p['multiplier'])-dec(p['unit_cost']))*D(p['quantity'])
   pnl=dec(realized)+unreal-dec(book['baseline_unrealized'])
   from .owner_settings import settings
   loss_limit=D(str(settings(s)['daily_loss_limit']))
   book['locked']=book['locked'] or pnl<=-loss_limit
   if not prior or prior['day']!=day or prior['positions']!=positions:
    s['live_risk_last']={'day':day,'positions':copy.deepcopy(positions),'observed_at':now}
   return {'day':day,'pnl_usd':str(pnl),'max_loss_usd':str(loss_limit),'entry_locked':book['locked'],
    'basis':'broker realized PnL today plus current bid-mark unrealized minus verified opening unrealized; fees reflected by broker realized PnL',
    'automatic_liquidation':False}
  result=self.journal.edit(edit)
  if 'basis_error' in result:raise ValueError(result['basis_error'])
  return result

class LiveBroker:
 def __init__(self,readonly,journal,account_number,limits,*,clock=time.time):
  self.readonly=readonly;self.journal=journal;self.account_number=account_number;self.limits=limits;self.clock=clock
  self.calendar=RegularMarketHours();self.risk=AccountRisk(journal,clock);self.worker=None
  self.view=None;self._view_lock=asyncio.Lock();self._view_task=None;self._view_completed_at=None
  self.account_metrics={'refreshes':0,'joined_refreshes':0,'cache_hits':0}
  self.fees={};self.transport=RobinhoodOptionTransport(readonly,self.permit)
 def permit(self,name,args):
  if getattr(self.readonly.service,'broker_writes_disabled',False):return False
  if not self.worker or not self.readonly.service.ready or args.get('account_number')!=self.account_number:return False
  state=self.worker.status()
  for r in state['orders'].values():
   p=r['plan']
   for a in r['attempts']:
    if name=='cancel_option_order' and args.get('order_id')==a.get('order_id') and a.get('cancel_reason'):return True
    if name=='decline_trade_approval' and args.get('approval_id')==a.get('approval_id') and r.get('stop_reason'):return True
    from .position_updates import order_block
    if order_block(state,p,self.clock()):continue
    if not self.readonly.service.live_guard() or state['mode']!='LIVE_ARMED' or state['boot_id']!=self.worker.boot_id or r.get('stop_reason') or self.clock()>=p['expires_at']:continue
    binding=state.get('assembly_bindings',{}).get(p['decision_id'])
    if binding:
     from .assembly_bridge import AssemblyBridge
     if not AssemblyBridge(service=self.readonly.service).valid_snapshot(binding,self.clock()):continue
    from .order_execution import broker_arguments
    expected=broker_arguments(p,a)
    if name=='place_option_order' and r['status']=='SUBMITTING' and a['transport_attempts']>0 and args==expected:return True
    if name=='review_option_order':
     expected.pop('ref_id');expected.update(chain_symbol=p['symbol'],underlying_type='equity')
     if args==expected:return True
  return False
 async def read(self,name,args):
  v=await self.readonly.read(name,args,persist=False)
  raw=v['result'];body=raw.get('structuredContent')
  if body is not None:validate(body,CONTRACTS[name]['output'])
  return data(raw)
 async def pages(self,name,args,key):
  out=[];seen=set()
  for _ in range(50):
   d=await self.read(name,args);rows=d.get(key)
   if rows is None:raise ValueError('null broker list is not a verified empty list')
   if any(not isinstance(r,dict) for r in rows):raise ValueError('null broker row')
   out.extend(rows);cursor=d.get('next')
   if not cursor:return out
   if cursor in seen:raise ValueError('repeated pagination cursor')
   seen.add(cursor);args={**args,'cursor':cursor}
  raise ValueError('broker pagination limit reached; not complete')
 async def quotes(self,positions):
  quotes={}
  for start in range(0,len(positions),20):
   group=positions[start:start+20];options=[p['id'] for p in group if p['kind']=='option'];symbols=[p['symbol'] for p in group if p['kind']=='equity']
   if options:
    d=await self.read('get_option_quotes',{'instrument_ids':options})
    for row in d['results']:
     q=option_quote(row,self.clock());quotes[q['id']]=q
   if symbols:
    obs=await self.readonly.read('get_equity_quotes',{'symbols':symbols},persist=False)
    normalized=normalize_quotes(obs,symbols,self.clock())
    closes={x['quote']['symbol']:x.get('close') for x in data(obs['result'])['results']}
    for q in normalized:quotes['equity:'+q['symbol']]={'bid':str(q['bid']),'ask':str(q['ask']),'asof':q['timestamp'],'close':closes[q['symbol']]}
  return quotes
 async def account_view(self,force=False):
  # All overlapping callers await ONE complete read. A forced sequential call
  # still refreshes. Cancellation of a waiter never cancels another's read.
  task=self._view_task
  if task is not None and not task.done():
   self.account_metrics['joined_refreshes']+=1
   return copy.deepcopy(await asyncio.shield(task))
  if task is not None and (task.cancelled() or task.exception() is not None):
   # Another caller can run before the scheduled completion callback.
   self.view=None;self._view_completed_at=None
  now=self.clock()
  if not force and self.view and self._view_completed_at is not None and 0<=now-self._view_completed_at<5 and 0<=now-self.view['observed_at']<=30:
   self.account_metrics['cache_hits']+=1
   return copy.deepcopy(self.view)
  self.account_metrics['refreshes']+=1
  self._view_task=asyncio.create_task(self._load_account_view())
  # Retrieve exceptions even when every waiter has been cancelled; a failed
  # refresh is never published or silently substituted with an older view.
  self._view_task.add_done_callback(self._account_done)
  return copy.deepcopy(await asyncio.shield(self._view_task))
 def _account_done(self,task):
  failed=task.cancelled() or task.exception() is not None
  if failed and self._view_task is task:
   self.view=None;self._view_completed_at=None
 async def _load_account_view(self):
  async with self._view_lock:
   started=self.clock()
   accounts=(await self.read('get_accounts',{}))['accounts'];a=next((a for a in accounts if a['account_number']==self.account_number),None)
   if not a or a['agentic_allowed'] is not True or a['state']!='active' or a['deactivated'] or a['permanently_deactivated'] or a['option_level'] not in {'option_level_2','option_level_3'}:raise ValueError('pinned account is not eligible')
   # These reads all depend only on the verified account. Await every result;
   # no partially fetched book is published and pagination stays sequential.
   results=await asyncio.gather(
    self.read('get_portfolio',{'account_number':self.account_number}),
    self.read('get_realized_pnl',{'account_number':a['rhs_account_number'],'span':'day','timezone':'America/New_York','display_currency':'USD'}),
    self.read('get_trade_approval_setting',{'account_number':self.account_number}),
    self.pages('get_option_positions',{'account_number':self.account_number,'nonzero':True},'positions'),
    self.pages('get_equity_positions',{'account_number':self.account_number},'positions'),
    self.pages('get_option_orders',{'account_number':self.account_number},'orders'),
    self.pages('get_equity_orders',{'account_number':self.account_number},'orders'),
    self.pages('get_trade_approvals',{'account_number':self.account_number},'approvals'),return_exceptions=True)
   for index,value in enumerate(results):
    if index!=1 and isinstance(value,BaseException):raise value
   pf,pnl,approval,raw_options,raw_equities,option_orders,equity_orders,pending_approvals=results
   if pf['currency']!='USD':raise ValueError('unsupported account currency')
   if any(dec(pf[k])!=0 for k in ['crypto_value','futures_value','event_contracts_value','mutual_funds_value','fixed_income_value']):raise ValueError('unsupported asset exposure in account risk')
   risk_error=None
   if isinstance(pnl,BaseException):risk_error=type(pnl).__name__+': '+str(pnl)[:120];pnl=None
   approval=approval['setting']
   if approval['account_number']!=self.account_number:raise ValueError('approval account mismatch')
   positions=[]
   for p in raw_options:
    qty=integer(p['quantity'])
    if not qty:continue
    if p['type']!='long' or dec(p['trade_value_multiplier'])!=100 or any(dec(p[k])!=0 for k in ['pending_exercise_quantity','pending_assignment_quantity','pending_expiration_quantity']):raise ValueError('unsupported option holding/event')
    positions.append({'id':p['option_id'],'position_id':'rh-option:'+p['option_id'],'kind':'option','symbol':p['chain_symbol'],'quantity':qty,
      'unit_cost':str(dec(p['average_price'])),'cost_basis':str(dec(p['average_price'])*qty),'multiplier':100,'pending_sell':integer(p['pending_sell_quantity']),
      'expiration_date':p['expiration_date'],'opened_at':p.get('opened_at')})
   for p in raw_equities:
    qty=dec(p['quantity'])
    if not qty:continue
    if qty<0 or p['type']!='long' or p.get('average_buy_price') is None or any(dec(p[k])!=0 for k in ['shares_held_for_options_events','shares_pending_from_options_events','shares_held_for_asset_transfer']):raise ValueError('unsupported equity holding/event')
    positions.append({'id':'equity:'+p['symbol'],'position_id':'rh-equity:'+p['symbol'],'kind':'equity','symbol':p['symbol'],'quantity':str(qty),'unit_cost':str(dec(p['average_buy_price'])),'cost_basis':str(qty*dec(p['average_buy_price'])),'multiplier':1})
   # Complete history prevents old GTC orders from being missed by a date cutoff.
   done={'filled','cancelled','canceled','rejected','failed','voided'}
   pending=[o|{'asset':'option'} for o in option_orders if o['state'] not in done]+[o|{'asset':'equity'} for o in equity_orders if o['state'] not in done]
   prior=(await asyncio.to_thread(self.journal.read)).get('live_risk_last')
   mark_list=list({p['id']:p for p in (prior['positions'] if prior and prior['day']!=datetime.fromtimestamp(self.clock(),EASTERN).date().isoformat() else [])+positions}.values())
   quotes=await self.quotes(mark_list)
   risk_positions=copy.deepcopy(positions)
   observation={'observed_at':started,'positions':positions,'buying_power':str(dec(pf['buying_power']['buying_power']))}
   for p in positions:
    q=quotes[p['id']];p.update(mark_bid=q['bid'],market_value=str(dec(q['bid'])*D(p['multiplier'])*D(p['quantity'])),unrealized_pnl=str((dec(q['bid'])*D(p['multiplier'])-dec(p['unit_cost']))*D(p['quantity'])))
   try:
    if pnl is None:raise ValueError(risk_error)
    realized,source=daily_realized(pnl,a['rhs_account_number'],positions,prior,option_orders,equity_orders,self.clock())
    risk=await asyncio.to_thread(self.risk.update,realized,risk_positions,quotes,observation);risk['available']=True; risk['realized_source']=source
   except ValueError as exc:
    risk={'available':False,'entry_locked':True,'pnl_usd':None,'max_loss_usd':'500','reason':str(exc),'automatic_liquidation':False}
    from .position_updates import record_book
    await asyncio.to_thread(self.journal.edit,lambda state:record_book(state,observation))
   if self.clock()-started>30:raise ValueError('account snapshot took too long; refresh required')
   view={'observed_at':started,'account_number':self.account_number,'rhs_account_number':a['rhs_account_number'],'account_ok':True,
    'buying_power':str(dec(pf['buying_power']['buying_power'])),'portfolio':pf,'positions':positions,'pending_orders':pending,'pending_approvals':pending_approvals,
    'risk':risk,'approval_required':approval['human_must_approve_trades'],'limits':self.limits}
   self.view=view;self._view_completed_at=self.clock();return copy.deepcopy(view)
 async def readiness(self):
  try:
   v=await self.account_view(force=True)
   blockers=[]
   # A daily lock blocks new risk, not owner arming for explicit closes.
   if v['pending_approvals']:blockers.append('pending brokerage approvals must be resolved by owner')
   if v['approval_required']:blockers.append('Robinhood requires individual trade approval; setting is unchanged')
   if not self.readonly.service.ready:blockers.append('durable storage unavailable')
   # Generation is monotonic identity; rolling retention replaces the old lifetime cap.
   from .cloud_store import MAX_SNAPSHOT_BYTES
   if getattr(getattr(self.readonly.service,'store',None),'snapshot_bytes',0)>=MAX_SNAPSHOT_BYTES:blockers.append('retained trade snapshot capacity exhausted')
   return {'ready':not blockers,'blockers':blockers,'view':v}
  except Exception as e:return {'ready':False,'blockers':[type(e).__name__+': '+str(e)[:220]]}
 async def snapshot(self,p,remaining):
  if p['account_number']!=self.account_number:raise ValueError('pinned account mismatch')
  # These facts are independent. Fetch together, await every result, then apply
  # the same identity, freshness and risk checks to the complete observation.
  results=await asyncio.gather(self.account_view(),
    self.pages('get_option_instruments',{'ids':p['option_id']},'instruments'),
    self.read('get_option_quotes',{'instrument_ids':[p['option_id']]}),return_exceptions=True)
  for value in results:
   if isinstance(value,BaseException):raise value
  v,instruments,quoted=results;now=self.clock();session=self.calendar.status(now)
  if len(instruments)!=1:raise ValueError('exact option UUID unavailable')
  ins=instruments[0]
  if ins['id']!=p['option_id'] or ins['chain_symbol']!=p['symbol'] or ins['underlying_type']!='equity' or ins['state']!='active' or ins['tradability']!='tradable' or dec(ins['trade_value_multiplier'])!=100:raise ValueError('contract mismatch/untradable/nonstandard')
  if not session['open'] or p['expires_at']>min(session['close'],timestamp(ins['sellout_datetime'])):raise ValueError('plan deadline exceeds permitted session/contract window')
  chains=(await self.read('get_option_chains',{'ids':ins['chain_id']}))['chains']
  if len(chains)!=1 or chains[0]['id']!=ins['chain_id'] or dec(chains[0]['cash_component'] or '0')!=0:raise ValueError('unsupported adjusted option chain')
  if p['side']=='buy' and chains[0]['can_open_position'] is not True:raise ValueError('chain disallows opening')
  ticks=ins['min_ticks']
  for price in p['limit_prices']+([p['stop_price']] if p['stop_price'] else []):
   d=positive(price);tick=positive(ticks['below_tick'] if d<dec(ticks['cutoff_price']) else ticks['above_tick'])
   if d%tick:raise ValueError('price violates broker tick size')
  qrows=quoted['results']
  if len(qrows)!=1:raise ValueError('missing exact option quote')
  # Concurrent account work can outlast the first quote. Refresh once by the
  # same UUID; venue timestamps and the existing 30-second limit still apply.
  # Never retry broker writes or reinterpret a crossed quote as valid.
  if self.clock()-timestamp(qrows[0]['quote']['updated_at'])>25:
   qrows=(await self.read('get_option_quotes',{'instrument_ids':[p['option_id']]}))['results']
   if len(qrows)!=1:raise ValueError('missing exact option quote')
  q=option_quote(qrows[0],self.clock())
  if q['id']!=p['option_id']:raise ValueError('quote instrument mismatch')
  state=await asyncio.to_thread(self.worker.status)
  assembly_ref=state.get('assembly_bindings',{}).get(p['decision_id'])
  assembly_conditions=(assembly_ref or {}).get('conditions',[])
  symbols=sorted({x['symbol'] for x in [p['condition'],p['invalidation']]+assembly_conditions if x})
  underlying={}
  if symbols:
   obs=await self.readonly.read('get_equity_quotes',{'symbols':symbols},persist=False)
   underlying={q['symbol']:{'last':str(q['last']),'asof':q['timestamp']} for q in normalize_quotes(obs,symbols,self.clock(),max_age=15)}
  state=await asyncio.to_thread(self.worker.status);owned=state['orders'].get(p['plan_id']);own_ids={a.get('order_id') for a in owned['attempts']} if owned else set()
  reserve=D(0);close_reserved=0;pending_qty=0;contracts={x['id'] for x in v['positions'] if x['quantity']}
  for o in v['pending_orders']:
   if o['id'] in own_ids:continue
   if o['asset']!='option' or len(o['legs'])!=1 or o['type'] not in {'limit','stop_limit'}:
    if p['side']=='buy':raise ValueError('unsupported working order prevents new autonomous risk')
    continue
   leg=o['legs'][0];left=integer(o['quantity'])-integer(o['processed_quantity'])
   if left<0 or leg['ratio_quantity']!=1:raise ValueError('inconsistent pending order')
   if leg['side']=='buy':
    reserve+=positive(o['price'])*100*left;contracts.add(leg['option_id'])
    if leg['option_id']==p['option_id']:pending_qty+=left
   elif leg['option_id']==p['option_id']:close_reserved+=left
  for key,r in state['orders'].items():
   if key==p['plan_id'] or r['status'] in TERMINAL:continue
   other=r['plan'];left=other['quantity']-r['filled']
   # Conservatively reserve local intents as well as broker working orders (no undercount).
   if other['side']=='buy':
    reserve+=positive(other['limit_prices'][-1])*100*left;contracts.add(other['option_id'])
    if other['option_id']==p['option_id']:pending_qty+=left
   elif other['option_id']==p['option_id']:close_reserved+=left
  held=next((x for x in v['positions'] if x['id']==p['option_id']),None);qty=held['quantity'] if held else 0
  own_close=sum(integer(o['quantity'])-integer(o['processed_quantity']) for o in v['pending_orders'] if o['asset']=='option' and o['id'] in own_ids and len(o['legs'])==1 and o['legs'][0]['side']=='sell')
  if held:close_reserved=max(close_reserved,max(0,held['pending_sell']-own_close))
  gross=sum((dec(x['market_value']) for x in v['positions']),D(0))
  risk_ok=True
  from .owner_settings import settings
  rules=settings(state)
  if p['side']=='buy':
   if 'owner_settings' in state:
    if gross+reserve+positive(p['limit_prices'][-1])*100*remaining>dec(rules['capital_budget']):risk_ok=False
    if positive(p['limit_prices'][-1])*100*(qty+pending_qty+remaining)>dec(rules['position_budget']):risk_ok=False
   if v['pending_approvals'] or v['approval_required'] or not v['risk']['available']:risk_ok=False
   if qty+pending_qty+remaining>self.limits['max_position_quantity'] or len(contracts|{p['option_id']})>self.limits['max_positions'] or gross+reserve+positive(p['limit_prices'][-1])*100*remaining>dec(self.limits['max_gross_notional']):risk_ok=False
  fee=self.fees.get(p['decision_id'],D(0))
  if p['side']=='buy' and v['risk']['available']:
   projected=dec(v['risk']['pnl_usd'])-max(D(0),positive(p['limit_prices'][-1])-dec(q['bid']))*100*remaining-fee
   if projected<=-dec(rules['daily_loss_limit']):risk_ok=False
  profit_ok=True
  guard=state.get('profit_exit_guards',{}).get(p['decision_id'])
  if guard:
   basis=dec(guard['unit_cost']) # Owner policy: premium gain, fees are informational.
   floor=basis*(1+dec(guard.get('minimum_return','0')))
   profit_ok=(p['side']=='sell' and held is not None and held['position_id']==guard['position_id']
    and dec(held['unit_cost'])==basis
    and (not guard.get('require_current_profit') or (dec(q['bid'])*100-basis)*remaining>0)
    and (min(map(dec,p['limit_prices']))*100-floor)*remaining>=0
    and (min(map(dec,p['limit_prices']))*100-basis)*remaining>0)
  assembly_ok=True
  if assembly_ref:
   from .assembly_bridge import AssemblyBridge,condition_holds
   # The existing worker refreshes this before review and again before placement.
   bridge=AssemblyBridge(service=self.readonly.service)
   assembly_ok=bridge.valid_snapshot(assembly_ref,self.clock()) and all(condition_holds(c,underlying,self.clock()) for c in assembly_conditions)
  return {'assembly_ok':assembly_ok,'profit_ok':profit_ok,'observed_at':v['observed_at'],'option_quote':copy.deepcopy(q),'account_ok':True,'risk_ok':risk_ok,'market_open':session['open'],
   'contract_ok':True,'quote_fresh':True,'entry_locked':v['risk']['available'] and v['risk']['entry_locked'],'buying_power':v['buying_power'],
   'max_contracts':self.limits['max_order_quantity'],'max_order_cost':str(dec(self.limits['max_order_notional'])-fee),
   'held_quantity':qty,'position_id':held['position_id'] if held else None,'reserved_cost':str(reserve+fee),
   'reserved_close_quantity':close_reserved,'underlying_quotes':underlying}
 async def reconcile(self,p,a):
  if a.get('approval_id'):
   approvals=(await self.read('get_trade_approvals',{'account_number':self.account_number,'approval_ids':[a['approval_id']]}))['approvals']
   if len(approvals)!=1:raise ValueError('approval identity unavailable')
   approval=approvals[0]
   if approval['approval_id']!=a['approval_id'] or approval['account_number']!=self.account_number:raise ValueError('approval identity mismatch')
   if approval['state'] in {'APPROVAL_STATE_DECLINED','APPROVAL_STATE_EXPIRED'}:return {'complete':True,'approval_terminal':True,'order':None}
   state=self.worker.status();r=state['orders'][p['plan_id']]
   if approval['state']=='APPROVAL_STATE_PENDING' and r['stop_reason'] and a.get('approval_decline_attempts',0)<3:
    a['approval_decline_attempts']=a.get('approval_decline_attempts',0)+1
    self.worker._save(p['plan_id'],r|{'attempts':r['attempts'][:-1]+[a]})
    await self.transport.call('decline_trade_approval',{'account_number':self.account_number,'approval_id':a['approval_id']})
   raise ValueError('broker approval requires owner recovery; no order ID can be inferred')
  if not a.get('order_id'):
   # Documented broker idempotency: retry exactly the same persisted request, never a new child.
   return {'complete':False,'retry_same_ref':True,'order':None}
  return await reconcile_known_order(self.readonly,p,a)
 async def review(self,p,a):
  result=await self.transport.review(p,a)
  value=result.get('fees');fee=None
  try:
   parsed=dec(value)
   if parsed>=0:fee=parsed
  except (ValueError,TypeError,ArithmeticError):pass
  self.fees.pop(p['decision_id'],None)
  if fee is not None:self.fees[p['decision_id']]=fee
  await asyncio.to_thread(self.journal.edit,lambda state:state.setdefault('broker_fee_quotes',{}).__setitem__(p['decision_id'],{'fees':str(fee) if fee is not None else None,'quantity':a['quantity'],'at':self.clock()}))
  return result
 async def place(self,p,a):return await self.transport.place(p,a)
 async def cancel(self,p,a):return await self.transport.cancel(p,a)
