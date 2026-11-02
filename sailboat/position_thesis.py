"""Versioned agent judgments queued for broker-reconciled profit management."""
import copy,hashlib,json
from decimal import Decimal

DEC={'type':'string','pattern':r'^\d+(\.\d+)?$'}
NULL_DEC={'anyOf':[DEC,{'type':'null'}]}
BLOCK_FIELDS={
 'position_id':{'type':'string','minLength':1},'option_id':{'type':'string','format':'uuid'},
 'expected_position_quantity':{'type':'integer','minimum':1},'expected_unit_cost':DEC,
 'expected_target_version':{'type':'integer','minimum':0},'target_version':{'type':'integer','minimum':1},
 'plan_id':{'type':['string','null']},'plan_version':{'type':['integer','null']},
 'action':{'enum':['HOLD','REVISE_TARGET','SELL_FOR_PROFIT']},'target_bid':NULL_DEC,
 'expected_upside_usd':{'anyOf':[{'type':'string','pattern':r'^-?\d+(\.\d+)?$'},{'type':'null'}]},
 'roundtrip_cost_allowance_usd':NULL_DEC,
 'reasoning':{'type':'string','minLength':1,'maxLength':2000}}
BLOCKS_SCHEMA={'type':'array','maxItems':8,'items':{'type':'object','additionalProperties':False,
 'required':list(BLOCK_FIELDS),'properties':BLOCK_FIELDS}}

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,allow_nan=False).encode()).hexdigest()

def related(state,option_id):
 return {k:(v['plan']['plan_version'],v['status'],v['filled'],v.get('stop_reason'))
         for k,v in state.get('orders',{}).items() if v['plan']['option_id']==option_id}

def prepare(response,view,state,now):
 """Return a pure validated update, then commit it with the decision/receipt."""
 from .order_execution import TERMINAL
 blocks=response.get('position_blocks') or [];seen=set();updates=[];guard=None
 if blocks and (response['action'] not in ('hold','execute') or (response['action']=='execute' and (response.get('plan') or {}).get('side')!='sell')):raise ValueError('position blocks require HOLD or explicit profit-sale action')
 for block in blocks:
  b=copy.deepcopy(block);pid=b['position_id']
  if pid in seen:raise ValueError('duplicate position block')
  seen.add(pid)
  p=next((p for p in view['positions'] if p['position_id']==pid),None)
  if not p or p['kind']!='option' or p['id']!=b['option_id']:raise ValueError('position identity changed')
  if p['quantity']!=b['expected_position_quantity'] or Decimal(p['unit_cost'])!=Decimal(b['expected_unit_cost']):raise ValueError('position quantity or cost changed')
  old=state.get('position_theses',{}).get(pid);version=old['target_version'] if old else 0
  if b['expected_target_version']!=version or b['target_version']!=version+1:raise ValueError('stale position target version')
  # A changed size/basis requires a fresh revision, not silently carrying an old target.
  same=old and old['option_id']==p['id'] and old['expected_position_quantity']==p['quantity'] and Decimal(old['expected_unit_cost'])==Decimal(p['unit_cost'])
  if b['action']=='HOLD' and b['target_bid']!=(old['target_bid'] if same else None):raise ValueError('HOLD cannot revise a target; use REVISE_TARGET')
  if b['action']!='HOLD':
   from datetime import datetime
   from .market_hours import EASTERN
   if p.get('symbol')!='QQQ' or p.get('expiration_date')!=datetime.fromtimestamp(now,EASTERN).date().isoformat():raise ValueError('adaptive profit instructions require a current QQQ 0DTE holding')
  if b['action']=='REVISE_TARGET' and (b['target_bid'] is None or Decimal(b['target_bid'])<=0):raise ValueError('revision needs a positive resale target')
  plans=related(state,p['id']);ref=b['plan_id']
  if ref is None:
   if b['plan_version'] is not None or plans:raise ValueError('reference an existing position plan/version')
  elif ref not in plans or plans[ref][0]!=b['plan_version']:raise ValueError('position plan version mismatch')
  working={k for k,v in plans.items() if v[1] not in TERMINAL}
  if working and ref not in working:raise ValueError('reference the current working plan before replacing or cancelling it')
  # Only queues a directive. AdaptiveProfitManager confirms cancellation and remaining
  # quantity before any replacement; this function never places an order.
  if b['action']=='SELL_FOR_PROFIT':
   order=response.get('plan')
   if guard is not None:raise ValueError('only one profit sale per decision')
   if response['action']!='execute' or not order or order['side']!='sell' or order['position_effect']!='close' or order['position_id']!=pid or order['option_id']!=p['id'] or order['expected_position_quantity']!=p['quantity']:raise ValueError('profit sale needs an explicit matching sell/close plan')
   qty=order['quantity'];basis=Decimal(p['unit_cost'])
   if qty>p['quantity']:raise ValueError('profit sale exceeds held quantity')
   if (Decimal(p['mark_bid'])*100-basis)*qty<=0:raise ValueError('current bid does not exceed purchase premium')
   if (min(map(Decimal,order['limit_prices']))*100-basis)*qty<=0:raise ValueError('profit-sale price ladder permits a nonprofitable sale')
   guard={'position_id':pid,'unit_cost':str(basis),'profit_basis':'premium_before_fees','minimum_return':'0','require_current_profit':True}
  b.update(updated_at=now,request_id=response['request_id'],position_opened_at=p.get('opened_at'),requested_plan=copy.deepcopy(response.get('plan')) if b['action']=='SELL_FOR_PROFIT' else None)
  updates.append({'block':b,'related_plans':plans})
 if response.get('action')=='execute' and (response.get('plan') or {}).get('side')=='sell' and blocks and guard is None:
  raise ValueError('held-position block must explicitly authorize the closing action')
 return {'request_id':response['request_id'],'response_hash':fingerprint(response),'updates':updates,'profit_guard':guard}

def commit(state,change,decision_id=None):
 """CAS against target and plan versions inside the existing durable transaction."""
 rid=change['request_id'];old=state.get('position_block_commits',{}).get(rid)
 if old:
  if old['response_hash']!=change['response_hash']:raise ValueError('position request reused with different content')
  return
 for item in change['updates']:
  b=item['block'];current=state.get('position_theses',{}).get(b['position_id']);version=current['target_version'] if current else 0
  if version!=b['expected_target_version']:raise ValueError('position target changed concurrently')
  if related(state,b['option_id'])!=item['related_plans']:raise ValueError('working plan changed concurrently; refresh context')
 if not change['updates']:return
 for item in change['updates']:
  b=item['block'];old=state.setdefault('position_theses',{}).get(b['position_id'])
  if old:state.setdefault('position_thesis_history',[]).append(copy.deepcopy(old))
  state['position_theses'][b['position_id']]=copy.deepcopy(b)
  # The latest accepted judgment immediately fences old owned profit orders.
  # This is only a cancellation request: OrderWorker must reconcile the broker.
  from .order_execution import TERMINAL
  reaffirmed=old and b['action']==old['action']=='REVISE_TARGET' and all(b.get(k)==old.get(k) for k in ('option_id','expected_position_quantity','expected_unit_cost','target_bid','position_opened_at'))
  if reaffirmed:continue
  for row in state.get('orders',{}).values():
   if (row['plan']['option_id']==b['option_id'] and row['plan']['side']=='sell'
       and row['status'] not in TERMINAL
       and row['plan']['decision_id'] in state.get('profit_exit_guards',{})):
    # A new analyst judgment supersedes managed profit exits, not a separate
    # owner-authorized order that happens to use the same option contract.
    row['stop_reason']=row.get('stop_reason') or 'CANCELLED'
 # Mark the received valid judgment atomically with its target. This closes the race
 # between target acceptance and the scheduler's later completion/health transaction.
 at=max(item['block']['updated_at'] for item in change['updates'])
 health=state.setdefault('agent_health',{})
 # Acceptance completes the old outage for execution purposes, even if the
 # scheduler has not recorded its final receipt yet. Future overdue slots still
 # accrue from next_due; repeated reads cannot extend that deadline.
 health.pop('active_assessment',None)
 health.update(failures=0,last_valid_at=at,next_due=max(health.get('next_due',0),at+health.get('interval',120)))
 state.setdefault('position_block_commits',{})[rid]={'response_hash':change['response_hash']}
 if decision_id and change['profit_guard']:
  state.setdefault('profit_exit_guards',{})[decision_id]=copy.deepcopy(change['profit_guard'])
