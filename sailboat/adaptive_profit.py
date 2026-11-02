"""Adaptive profit-order orchestration. No transport: all orders use OrderWorker.
The host must supply fresh positions and contract bounds. Fees are informational.
"""
import asyncio,copy,math,uuid
from decimal import Decimal,ROUND_CEILING
from .order_execution import TERMINAL
D=Decimal

def record_health(state,cycle_id,valid,now,next_due,interval=120,started_at=None):
 h=state.setdefault('agent_health',{})
 if h.get('cycle_id')==cycle_id:return
 h.pop('active_assessment',None)
 h.update(cycle_id=cycle_id,failures=0 if valid else h.get('failures',0)+1,
          finished_at=now,next_due=next_due,interval=interval)
 if valid:h['last_valid_at']=now
 h['recent_results']=(h.get('recent_results',[])+[{'started_at':now if started_at is None else started_at,
    'finished_at':now,'valid':valid}])[-32:]

def missed_count(state,now):
 h=state.get('agent_health',{})
 active=h.get('active_assessment')
 if active:
  # A persisted, bounded processing window; polling cannot extend it. Already
  # missed assessments/failures remain counted even while a new one is running.
  elapsed=max(0,math.ceil((now-active['deadline'])/h.get('interval',120)))
  return h.get('failures',0)+active['overdue_at_start']+elapsed
 # An overdue assessment gets its full 110-second generation budget first.
 overdue=max(0,math.floor((now-h['next_due']-110)/h.get('interval',120))+1) if 'next_due' in h else 0
 return h.get('failures',0)+overdue

def position_missed_count(state,pid,now):
 """Failures/overdue slots before this holding cannot enable its fallback."""
 epoch=state.get('position_management_epochs',{}).get(pid)
 if epoch is None:return missed_count(state,now) # Legacy/synthetic callers without broker book.
 if epoch.get('closed'):return 0
 since=epoch['since'];h=copy.deepcopy(state.get('agent_health',{}));failures=0
 for result in reversed(h.get('recent_results',[])):
  if result['started_at']<since or result['valid']:break
  failures+=1
 h['failures']=min(failures,h.get('failures',0))
 interval=h.get('interval',120);h['next_due']=max(h.get('next_due',since+interval),since+interval)
 active=h.get('active_assessment')
 if active:
  if active['started_at']<since:h.pop('active_assessment')
  else:
   eligible=max(0,math.floor((active['started_at']-(since+interval)-110)/interval)+1)
   active['overdue_at_start']=min(active['overdue_at_start'],eligible)
 return missed_count({'agent_health':h},now)

def sync_health_schedule(state,next_due,interval,active=None):
 h=state.setdefault('agent_health',{})
 h.update(next_due=next_due,interval=interval)
 if active and (h.get('active_assessment') or {}).get('cycle_id')!=active['cycle_id']:
  # Preserve missed slots before this attempt; never hide an existing outage.
  overdue=max(0,math.floor((active['started_at']-next_due-110)/interval)+1)
  h['active_assessment']={'cycle_id':active['cycle_id'],'started_at':active['started_at'],
   'deadline':active['started_at']+110,'overdue_at_start':overdue}


def round_tick(price,details):
 value=D(price)
 for _ in range(3):
  tick=D(details['below_tick'] if value<D(details['cutoff_price']) else details['above_tick'])
  value=(value/tick).to_integral_value(rounding=ROUND_CEILING)*tick
 return format(value,'f')

class AdaptiveProfitManager:
 def __init__(self,journal,worker,port,clock):
  self.journal,self.worker,self.port,self.clock=journal,worker,port,clock
  self.lock=asyncio.Lock()
 def save(self,pid,**fields):
  self.journal.edit(lambda s:s.setdefault('adaptive_profit',{}).setdefault(pid,{}).update(fields))
 async def tick(self):
  if self.lock.locked():return
  async with self.lock:
   if not self.worker._armed():return
   view=await self.port.position_view();state=self.worker.status();now=self.clock()
   positions={p['position_id']:p for p in view['positions']}
   closing=await self.scheduled_close.tick(view) if hasattr(self,'scheduled_close') else set()
   for pid,m in state.get('adaptive_profit',{}).items():
    if pid not in positions:
     for key,row in state['orders'].items():
      if row['plan'].get('position_id')==pid and row['status'] not in TERMINAL:self.worker.cancel(key)
   for pid,p in positions.items():
    if pid in closing:continue
    if p.get('kind')!='option' or p.get('symbol')!='QQQ':continue
    try:
     await self.position(p,view)
     self.save(pid,last_error=None)
    except Exception as exc:self.save(pid,last_error=type(exc).__name__+': '+str(exc)[:240])
 async def position(self,p,view):
  state=self.worker.status();now=self.clock();pid=p['position_id'];block=state.get('position_theses',{}).get(pid)
  from .position_updates import management_block
  blocked=management_block(state,pid)
  if blocked:raise ValueError(blocked)
  current=copy.deepcopy(state.get('adaptive_profit',{}).get(pid,{}));source=current.get('source');version=current.get('agent_version',0)
  from .owner_settings import settings
  rules=settings(state);owner_target=state.get('owner_targets',{}).get(pid)
  if owner_target and owner_target['position_epoch']!=state.get('position_management_epochs',{}).get(pid):owner_target=None
  if owner_target:
   source='owner';version=block['target_version'] if block else 0
  elif position_missed_count(state,pid,now)>=rules['fallback_misses']:
   source='fallback';version=block['target_version'] if block else version
  elif block and (block['target_version']>version or source=='owner'):
   source='agent';version=block['target_version']
  elif source in (None,'owner'):return
  def select_source(s):
   if s.get('position_management_epochs',{}).get(pid)!=state.get('position_management_epochs',{}).get(pid):
    raise ValueError('Holding lifecycle changed before profit-source selection')
   s.setdefault('adaptive_profit',{}).setdefault(pid,{}).update(source=source,agent_version=version)
  self.journal.edit(select_source)
  if source=='agent':
   if not block or block['target_version']!=version:raise ValueError('fresh agent instruction required')
   if block.get('position_opened_at')!=p.get('opened_at') or D(block['expected_unit_cost'])!=D(p['unit_cost']) or p['quantity']>block['expected_position_quantity']:raise ValueError('position basis/size changed; fresh assessment required')
   desired=None if block['action']=='HOLD' else block
  else:desired='fallback'
  # Every conflicting owned intent must reach a reconciled terminal state first.
  matching={k:r for k,r in state['orders'].items() if r['plan']['option_id']==p['id'] and r['status'] not in TERMINAL}
  if desired is not None:
   for key,row in matching.items():
    if row['plan']['side']=='sell' and row['plan']['decision_id'] not in state.get('profit_exit_guards',{}):
     if source in ('fallback','owner') or key!=block.get('plan_id') or row['plan']['created_at']>block.get('updated_at',0):
      raise ValueError('newer or separately authorized closing order takes precedence; refresh/reconcile it')
  if desired is None:
   for key,r in matching.items():
    if r['plan']['side']=='sell' and r['plan']['decision_id'] in state.get('profit_exit_guards',{}):
     self.worker.cancel(key)
   return
  details=await self.port.exit_details(p,state,current)
  if p['expiration_date']!=details['trading_date']:return # bounded QQQ 0DTE feature
  qty=p['quantity'];basis=D(p['unit_cost'])
  if source=='fallback':
   minimum=str((D(str(rules['fallback_profit_percent']))/100).quantize(D('0.0001')))
   price=format(D(round_tick(basis*(1+D(minimum))/100,details)),'.2f');prices=[price];original=None
  elif source=='owner':
   if not owner_target:raise ValueError('Owner target no longer applies to this holding')
   prices=[owner_target['limit_price']];minimum='0';original=None
  else:
   minimum='0';original=block.get('requested_plan')
   if original:
    if now>=original['expires_at']:raise ValueError('agent sell instruction expired')
    qty=max(0,original['quantity']-(block['expected_position_quantity']-p['quantity']))
    if not qty:return
    prices=original['limit_prices']
   else:prices=[block['target_bid']]
   if not prices[0]:raise ValueError('explicit target required')
  floor=basis*(1+D(minimum))
  if min(map(D,prices))*100<floor or min(map(D,prices))*100<=basis:
   raise ValueError('requested limit does not exceed purchase premium')
  for key,r in matching.items():
   reaffirmed=(source=='agent' and block['action']=='REVISE_TARGET')
   same=(key==current.get('plan_id') and current.get('order_source')==source and (current.get('order_version')==version or reaffirmed)
         and not r.get('stop_reason') and r['plan']['side']=='sell' and r['plan']['limit_prices']==prices
         and r['plan']['quantity']-r['filled']==qty)
   if same:
    self.save(pid,order_version=version)
    return
  if matching:
   for key in matching:self.worker.cancel(key)
   return
  if p.get('pending_sell') or any(any(l.get('option_id')==p['id'] for l in o.get('legs',[])) for o in view.get('pending_orders',[])):
   raise ValueError('broker still reports a conflicting order; awaiting reconciliation')
  # The entry/exit decisions are bound to the current remaining position, never guessed fills.
  stamp=uuid.uuid4().hex;key='profit-plan-'+stamp;decision_id='profit-decision-'+stamp
  plan={'decision_id':decision_id,'plan_id':key,'plan_version':1,'account_number':view['account_number'],
   'option_id':p['id'],'symbol':p['symbol'],'side':'sell','position_effect':'close','quantity':qty,
   'order_type':'limit','limit_prices':prices,'stop_price':None,'condition':None,'invalidation':None,
   'created_at':now,'expires_at':min(details['close_at'],original['expires_at'] if original else details['close_at']),
   'replace_after_seconds':original['replace_after_seconds'] if original else 60,'max_transport_attempts':original['max_transport_attempts'] if original else 3,
   'position_id':pid,'expected_position_quantity':p['quantity'],'time_in_force':'gfd','market_hours':'regular_hours'}
  if original:
   # Preserve explicitly selected order semantics and conditions; never widen price bounds.
   for k in ('order_type','stop_price','condition','invalidation'):plan[k]=copy.deepcopy(original[k])
  guard={'position_id':pid,'unit_cost':str(basis),'profit_basis':'premium_before_fees',
   'minimum_return':minimum,
   'require_current_profit':bool(source=='agent' and block['action']=='SELL_FOR_PROFIT')}
  if source=='fallback':guard['owner_settings_revision']=state.get('owner_settings',{}).get('revision',0)
  if source=='owner':guard['owner_target_version']=owner_target.get('target_id',owner_target['at'])
  position_epoch=copy.deepcopy(state.get('position_management_epochs',{}).get(pid))
  if position_epoch is not None:guard['position_epoch']=position_epoch
  health=copy.deepcopy(state.get('agent_health',{}));epoch=state.get('control_epoch',0)
  def atomic(s):
   if source=='owner' and s.get('owner_targets',{}).get(pid)!=owner_target:raise ValueError('Owner target changed during preparation')
   if source=='fallback' and s.get('owner_settings',{}).get('revision',0)!=guard['owner_settings_revision']:raise ValueError('Fallback rules changed during preparation')
   latest=s.get('position_theses',{}).get(pid)
   if s.get('position_management_epochs',{}).get(pid)!=position_epoch:
    raise ValueError('Holding lifecycle changed during profit-order preparation')
   # Owner cancellation/resume may arrive while exit_details is awaiting I/O.
   if (s.get('manual_management_holds',{}).get(pid)!=state.get('manual_management_holds',{}).get(pid)
       or s.get('management_resume_after',{}).get(pid)!=state.get('management_resume_after',{}).get(pid)):
    raise ValueError('Owner intervention changed during profit-order preparation')
   blocked=management_block(s,pid)
   if blocked:raise ValueError(blocked)
   if s.get('control_epoch',0)!=epoch or s.get('agent_health',{})!=health:raise ValueError('control/agent state changed during reconciliation')
   if (latest['target_version'] if latest else 0)!=(block['target_version'] if block else 0):raise ValueError('newer agent target wins')
   s.setdefault('profit_exit_guards',{})[decision_id]=guard
   s.setdefault('adaptive_profit',{}).setdefault(pid,{}).update(plan_id=key,order_source=source,order_version=version,last_error=None)
  self.worker.accept(plan,state_update=atomic)
 async def run(self):
  while True:
   try:await self.tick()
   except Exception:pass # uncertain data cannot create orders; next tick re-reads
   await asyncio.sleep(5)


class ProfitPort:
 """Read-only evidence adapter; the manager never calls brokerage write methods."""
 def __init__(self,broker,clock):self.broker,self.clock=broker,clock
 async def position_view(self):return await self.broker.account_view(force=True)
 async def exit_details(self,p,state,current):
  from datetime import datetime
  from .market_hours import EASTERN
  from .scheduler import timestamp
  now=self.clock();session=self.broker.calendar.status(now)
  if not session['open']:raise ValueError('outside regular session')
  rows=await self.broker.pages('get_option_instruments',{'ids':p['id']},'instruments')
  if len(rows)!=1 or rows[0]['id']!=p['id']:raise ValueError('exact held contract unavailable')
  ins=rows[0]
  if ins['expiration_date']!=p['expiration_date'] or ins['chain_symbol']!=p['symbol']:raise ValueError('held contract metadata mismatch')
  fees=state.get('broker_fee_quotes',{});entry_quotes=[];covered=0;unknown=False
  opened=timestamp(p['opened_at']) if p.get('opened_at') else None
  for row in state.get('orders',{}).values():
   plan=row['plan']
   if plan['option_id']!=p['id'] or plan['side']!='buy' or row['filled']<=0:continue
   if opened is not None and plan['created_at']+120<opened:continue
   q=fees.get(plan['decision_id'])
   if not q or q.get('fees') is None:unknown=True;continue
   covered+=row['filled'];entry_quotes.append(D(q['fees']))
  # Total known opening-review costs are conservatively allocated to the remainder.
  # Missing provenance never silently becomes zero fees.
  entry=sum(entry_quotes,D(0))/p['quantity'] if covered>=p['quantity'] and entry_quotes and not unknown else None
  old=state.get('orders',{}).get(current.get('plan_id'),{}).get('plan',{})
  exit_quote=fees.get(old.get('decision_id'))
  return dict(ins['min_ticks'],trading_date=datetime.fromtimestamp(now,EASTERN).date().isoformat(),
   close_at=min(session['close'],timestamp(ins['sellout_datetime'])),
   entry_fee_per_contract=str(entry) if entry is not None else None,
   exit_fee_total=exit_quote['fees'] if exit_quote else None)
