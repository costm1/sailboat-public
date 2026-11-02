import asyncio,copy,json,sqlite3,uuid
from pathlib import Path
import pytest
from sailboat.agent_link import AgentLink
from sailboat.cloud_store import DurableController,StorageUnavailable
from sailboat.order_execution import Journal,OrderWorker,validate_plan,broker_arguments
from sailboat.robinhood_orders import RobinhoodOptionTransport,PreparedLiveControls,reconcile_known_order

class Store:
 def __init__(self,tmp):
  p=tmp/'seed.sqlite';c=AgentLink(p);c.close();self.bytes=p.read_bytes();self.rev='0';self.generation=0;self.upload_bytes=0;self.download_bytes=0;self.fail=False
 def head(self):return self.rev
 def load(self,p):p.write_bytes(self.bytes);return self.rev,self.generation
 def save(self,p,parent,generation):
  if self.fail:raise OSError('checkpoint failure')
  assert parent==self.rev
  self.bytes=p.read_bytes();self.generation=generation;self.rev=str(generation);return self.rev

class Broker:
 def __init__(self,clock):
  self.clock=clock;self.orders={};self.calls=[];self.place_error=False;self.before_accept_error=False;self.incomplete=False;self.alerts=[];self.approval=False;self.on_review=None;self.hang=False
  self.view={'market_open':True,'account_ok':True,'risk_ok':True,'entry_locked':False,'contract_ok':True,'quote_fresh':True,
   'buying_power':'10000','max_contracts':10,'max_order_cost':'2000','held_quantity':5,'position_id':'held',
   'reserved_cost':'0','reserved_close_quantity':0,'underlying_quotes':{'QQQ':{'last':'710','asof':clock[0]}}}
 async def readiness(self):return {'ready':True}
 async def snapshot(self,p,remaining):
  if self.hang:await asyncio.sleep(1)
  return self.view|{'observed_at':self.clock[0]}
 async def reconcile(self,p,a):return {'complete':not self.incomplete,'order':copy.deepcopy(self.orders.get(a['ref_id']))}
 async def review(self,p,a):
  if self.on_review:self.on_review()
  return {'approved':not self.alerts,'alerts':self.alerts,'customer_approval_required':self.approval}
 async def place(self,p,a):
  self.calls.append(('place',copy.deepcopy(broker_arguments(p,a))))
  if self.before_accept_error:raise TimeoutError('lost before acceptance')
  self.orders.setdefault(a['ref_id'],{'id':str(uuid.uuid4()),'ref_id':a['ref_id'],'option_id':p['option_id'],
   'quantity':a['quantity'],'filled':0,'state':'confirmed','side':p['side'],'price':a['price']})
  if self.place_error:raise TimeoutError('response lost after acceptance')
  return {'order_id':self.orders[a['ref_id']]['id']}
 async def cancel(self,p,a):
  self.calls.append(('cancel',a['ref_id']));self.orders[a['ref_id']]['state']='cancelled'

def plan(now=1000,**kw):
 p={'decision_id':'decision-1','plan_id':'plan-1','plan_version':1,'account_number':'TEST-ONLY',
  'option_id':'11111111-1111-4111-8111-111111111111','symbol':'QQQ','side':'buy','position_effect':'open',
  'quantity':3,'order_type':'limit','limit_prices':['1.00','1.10','1.20'],'stop_price':None,'condition':None,'invalidation':None,
  'created_at':now,'expires_at':now+100,'replace_after_seconds':10,'max_transport_attempts':3,
  'position_id':None,'expected_position_quantity':None,'time_in_force':'gfd','market_hours':'regular_hours'}
 return p|kw

@pytest.fixture
def rig(tmp_path):
 t=[1000.];store=Store(tmp_path);service=DurableController(store,tmp_path/'a');b=Broker(t);w=OrderWorker(Journal(service),b,clock=lambda:t[0],timeout=.03)
 return t,store,service,b,w

def run(coro):return asyncio.run(coro)
def arm_accept(rig,p=None):
 *_,w=rig;run(w.arm());w.accept(p or plan())
def step(rig,seconds=5):
 t,_,_,b,w=rig;run(w.tick());t[0]+=seconds;b.view['underlying_quotes']['QQQ']['asof']=t[0]
def row(rig,key='plan-1'):return rig[-1].status()['orders'][key]

def test_disarmed_rejects_before_network(rig):
 with pytest.raises(ValueError,match='DISARMED'):rig[-1].accept(plan())
 step(rig);assert not rig[-2].calls

def test_accepted_order_is_not_a_fill(rig):
 arm_accept(rig);step(rig);assert row(rig)['filled']==0;assert row(rig)['attempts'][0]['order_id']
 step(rig);assert row(rig)['status']=='WORKING';assert len(rig[-2].orders)==1

def test_fill_exactly_once_and_duplicate_decision(rig):
 arm_accept(rig);step(rig);b=rig[-2];o=next(iter(b.orders.values()));o.update(state='filled',filled=3)
 step(rig);assert row(rig)['status']=='FILLED'
 rig[-1].accept(plan());step(rig);assert len(b.orders)==1

def test_partial_cancel_ack_replaces_only_remainder(rig):
 arm_accept(rig);step(rig,10);b=rig[-2];next(iter(b.orders.values())).update(state='partially_filled',filled=1)
 step(rig);assert row(rig)['status']=='CANCEL_PENDING';assert len(b.orders)==1
 step(rig);assert len(b.orders)==2
 orders=list(b.orders.values());assert orders[1]['quantity']==2 and orders[1]['price']=='1.10'
 orders[1].update(state='filled',filled=2);step(rig);assert row(rig)['filled']==3

def test_fill_races_cancel_no_extra_buy(rig):
 arm_accept(rig);step(rig,10);step(rig)
 next(iter(rig[-2].orders.values())).update(state='filled',filled=3)
 step(rig);assert row(rig)['status']=='FILLED';assert len(rig[-2].orders)==1

def test_lost_response_reconciles_without_duplicate(rig):
 arm_accept(rig);rig[-2].place_error=True;step(rig);step(rig)
 assert len(rig[-2].calls)==1 and row(rig)['status']=='WORKING'

def test_timeout_before_acceptance_reuses_ref_and_identical_payload(rig):
 arm_accept(rig);rig[-2].before_accept_error=True;step(rig);rig[-2].before_accept_error=False;step(rig)
 calls=[x[1] for x in rig[-2].calls if x[0]=='place'];assert len(calls)==2 and calls[0]==calls[1]

def test_retry_cap_keeps_read_recovery(rig):
 arm_accept(rig);rig[-2].before_accept_error=True
 for _ in range(9):step(rig,15)
 assert len(rig[-2].calls)==3;assert row(rig)['status']=='RECONCILING';assert len(row(rig)['attempts'])==1

def test_incomplete_reconcile_never_resubmits(rig):
 arm_accept(rig);rig[-2].before_accept_error=True;step(rig);rig[-2].incomplete=True
 for _ in range(3):step(rig)
 assert len(rig[-2].calls)==1

def test_manual_broker_cancel_does_not_reenter(rig):
 arm_accept(rig);step(rig);next(iter(rig[-2].orders.values()))['state']='cancelled';step(rig)
 assert row(rig)['status']=='CANCELLED';assert len(rig[-2].orders)==1

def test_rejection_does_not_loop(rig):
 arm_accept(rig);step(rig);next(iter(rig[-2].orders.values()))['state']='rejected';step(rig);step(rig)
 assert row(rig)['status']=='REJECTED';assert len(rig[-2].orders)==1

def test_stop_limit_exact_fields_no_reprice(rig):
 p=plan(order_type='stop_limit',stop_price='1.10',limit_prices=['1.20']);arm_accept(rig,p);step(rig,15);step(rig)
 args=rig[-2].calls[0][1];assert args['type']=='stop_limit' and args['stop_price']=='1.10' and args['price']=='1.20'
 assert len(rig[-2].calls)==1

def test_underlying_condition_does_not_become_option_stop(rig):
 arm_accept(rig,plan(condition={'symbol':'QQQ','operator':'gte','price':'711'}));step(rig);assert not rig[-2].calls
 rig[-2].view['underlying_quotes']['QQQ']['last']='712';step(rig);args=rig[-2].calls[0][1];assert args['type']=='limit' and 'stop_price' not in args

def test_expiry_cancels_unfilled_without_liquidation(rig):
 arm_accept(rig);step(rig,101);step(rig);step(rig)
 assert row(rig)['status']=='EXPIRED';assert all(x['side']!='sell' for x in rig[-2].orders.values())

def test_disarm_during_review_no_order(rig):
 arm_accept(rig);rig[-2].on_review=rig[-1].disarm;step(rig);assert not rig[-2].calls

def test_disarm_cancels_owned_working_order_not_positions(rig):
 arm_accept(rig);step(rig);rig[-1].disarm();step(rig);step(rig)
 assert row(rig)['status']=='CANCELLED';assert len(rig[-2].orders)==1

def test_restart_recovers_disarmed_and_cancel_reconciles(rig,tmp_path):
 arm_accept(rig);step(rig)
 t,store,_,b,_=rig;s=DurableController(store,tmp_path/'recover');w=OrderWorker(Journal(s),b,clock=lambda:t[0]);rr=(t,store,s,b,w)
 assert w.status()['mode']=='DISARMED';step(rr);step(rr);assert row(rr)['status']=='CANCELLED'
 assert len(b.orders)==1

def test_storage_failure_before_submission_no_network(rig):
 arm_accept(rig);rig[1].fail=True
 with pytest.raises(StorageUnavailable):run(rig[-1].tick())
 assert not rig[-2].calls

@pytest.mark.parametrize('bad',[{'quantity':0},{'limit_prices':['1.20','1.00']},{'stop_price':'1.00'},
 {'order_type':'market'},{'limit_prices':['NaN']},{'option_id':'invented'},
 {'order_type':'stop_limit','stop_price':'1.00'},{'order_type':'stop_limit','stop_price':'1.30','limit_prices':['1.20']},
 {'side':'sell','position_effect':'open'},{'side':'sell','position_effect':'close'},{'expires_at':999}])
def test_bad_contracts_rejected(bad):
 with pytest.raises(Exception):validate_plan(plan(**bad),1000)

@pytest.mark.parametrize('field,value',[('risk_ok',False),('account_ok',False),('market_open',False),('quote_fresh',False),('contract_ok',False),('buying_power','5'),('max_contracts',1),('max_order_cost','10')])
def test_independent_guard_blocks(rig,field,value):
 arm_accept(rig);rig[-2].view[field]=value;step(rig);assert not rig[-2].calls

def test_daily_lock_no_sell_and_exits_still_work(rig):
 arm_accept(rig);rig[-2].view['entry_locked']=True;step(rig);step(rig);assert row(rig)['status']=='DAILY_LOCKED';assert not rig[-2].calls
 rig[-1].accept(plan(decision_id='exit',plan_id='exit',side='sell',position_effect='close',quantity=1,limit_prices=['1.20'],position_id='held',expected_position_quantity=5))
 step(rig);assert len(rig[-2].orders)==1;assert next(iter(rig[-2].orders.values()))['side']=='sell'

def test_invalidation_cancels_without_new_order(rig):
 arm_accept(rig,plan(invalidation={'symbol':'QQQ','operator':'lte','price':'709'}));step(rig)
 rig[-2].view['underlying_quotes']['QQQ']['last']='708';step(rig);step(rig);step(rig)
 assert row(rig)['status']=='INVALIDATED';assert len(rig[-2].orders)==1

def test_duplicate_conflict_and_other_plan_same_contract(rig):
 arm_accept(rig)
 with pytest.raises(ValueError,match='different terms'):rig[-1].accept(plan(quantity=4))
 with pytest.raises(ValueError,match='active intent'):rig[-1].accept(plan(decision_id='two',plan_id='two'))

def test_one_unavailable_intent_does_not_block_other(rig):
 arm_accept(rig);rig[-1].accept(plan(decision_id='two',plan_id='two',option_id='22222222-2222-4222-8222-222222222222'))
 b=rig[-2];original=b.snapshot
 async def snapshot(p,n):
  if p['plan_id']=='plan-1':await asyncio.sleep(1)
  return await original(p,n)
 b.snapshot=snapshot;step(rig);assert len(b.orders)==1;assert row(rig,'two')['attempts']

def test_review_alert_and_approval_never_accepted_automatically(rig):
 arm_accept(rig);rig[-2].alerts=['Explicit broker alert'];step(rig);assert row(rig)['status']=='NEEDS_OWNER';assert not rig[-2].calls

def test_production_prepared_arm_refuses(rig):
 controls=PreparedLiveControls(rig[2]);assert controls.status()['mode']=='DISARMED'
 with pytest.raises(ValueError,match='LIVE_NOT_READY'):run(controls.arm())

def test_official_transport_disarmed_before_network():
 t=RobinhoodOptionTransport(None,lambda *_:False)
 with pytest.raises(ValueError,match='DISARMED'):run(t.call('place_option_order',{}))

def test_payloads_match_actual_official_input_schemas():
 from jsonschema import validate
 from sailboat.live_broker import CONTRACTS
 schemas={k:{'inputSchema':v['input']} for k,v in CONTRACTS.items()}
 for p in [plan(),plan(order_type='stop_limit',stop_price='1.00',limit_prices=['1.10'])]:
  a={'quantity':p['quantity'],'price':p['limit_prices'][0],'ref_id':str(uuid.uuid4())}
  payload=broker_arguments(p,a);validate(payload,schemas['place_option_order']['inputSchema'])
  payload.pop('ref_id');validate(payload,schemas['review_option_order']['inputSchema'])

def test_unknown_broker_order_identity_not_matched_by_price():
 assert run(reconcile_known_order(None,plan(),{'ref_id':str(uuid.uuid4()),'order_id':None}))['complete'] is False

def test_price_ladder_stops_at_cap_then_expires(rig):
 arm_accept(rig)
 for _ in range(9):step(rig,10)
 prices=[v['price'] for v in rig[-2].orders.values()]
 assert prices==['1.00','1.10','1.20'];assert sum(v['filled'] for v in rig[-2].orders.values())==0
 step(rig,20);step(rig);step(rig)
 assert row(rig)['status']=='EXPIRED';assert len(rig[-2].orders)==3

def test_cancel_pending_does_not_start_replacement(rig):
 arm_accept(rig);step(rig,10);b=rig[-2]
 async def cancel(p,a):b.calls.append(('cancel',a['ref_id']));b.orders[a['ref_id']]['state']='pending_cancelled'
 b.cancel=cancel
 for _ in range(3):step(rig)
 assert len(b.orders)==1;assert row(rig)['filled']==0

def test_known_order_disappearing_is_not_absence_proof(rig):
 arm_accept(rig);step(rig);rig[-2].orders.clear();step(rig)
 assert len(rig[-2].calls)==1;assert 'disappeared' in row(rig)['last_error']

def test_fill_regression_refuses_replacement(rig):
 arm_accept(rig);step(rig);o=next(iter(rig[-2].orders.values()));o.update(state='partially_filled',filled=1);step(rig)
 o['filled']=0;step(rig)
 assert 'regressed' in row(rig)['last_error'];assert len(rig[-2].orders)==1

def test_position_changed_outside_controller_blocks_exit(rig):
 p=plan(side='sell',position_effect='close',quantity=1,limit_prices=['1.20'],position_id='held',expected_position_quantity=5)
 arm_accept(rig,p);rig[-2].view['held_quantity']=4;step(rig);assert not rig[-2].calls

def test_rejected_input_does_not_fence_controller(rig):
 with pytest.raises(ValueError):rig[-1].accept(plan())
 assert rig[2].ready
 arm_accept(rig);step(rig);assert rig[-2].orders

def test_review_customer_approval_blocks_submission(rig):
 arm_accept(rig);rig[-2].approval=True;step(rig);assert row(rig)['status']=='NEEDS_OWNER';assert not rig[-2].calls

def test_only_one_decision_worker(rig):
 arm_accept(rig);w=rig[-1]
 w.lock.acquire()
 try:assert run(w.tick())=={'status':'BUSY'}
 finally:w.lock.release()
 assert not rig[-2].calls

def test_owner_route_is_separate_and_arm_fails_closed(rig):
 import hashlib
 from starlette.testclient import TestClient
 from sailboat.cloud_server import make_app
 key='agent-'+'x'*40;owner='owner-'+'y'*40
 keys={hashlib.sha256(key.encode()).hexdigest():{'source':'external-agent','conversation':'shared-paper','label':'test'}}
 with TestClient(make_app(rig[2],keys,['testserver'],owner_key_hash=hashlib.sha256(owner.encode()).hexdigest())) as c:
  h={'Authorization':'Bearer '+key};oh={'Authorization':'Bearer '+owner}
  assert c.post('/owner/control',json={'command':'arm'},headers=h).status_code==403
  assert c.get('/execution/status',headers=h).json()['live_ready'] is False
  response=c.post('/owner/control',json={'command':'arm'},headers=oh)
  assert response.status_code==409 and 'LIVE_NOT_READY' in response.json()['error']
  assert c.post('/owner/control',json={'command':'disarm'},headers=oh).json()['mode']=='DISARMED'
 assert rig[2].ready and not rig[-2].calls

def test_accepted_receipt_replay_after_expiry(rig):
 arm_accept(rig);step(rig);rig[0][0]+=1000
 old=rig[-1].accept(plan());assert old['plan']['decision_id']=='decision-1';assert len(rig[-2].orders)==1

def test_cancel_transport_retry_bound(rig):
 arm_accept(rig);step(rig);b=rig[-2];rig[-1].disarm()
 async def cancel(p,a):b.calls.append(('cancel',a['ref_id']));raise TimeoutError('unknown cancel outcome')
 b.cancel=cancel
 for _ in range(7):step(rig)
 assert len([x for x in b.calls if x[0]=='cancel'])==3
 assert len(b.orders)==1 and row(rig)['status'] not in {'FILLED','CANCELLED'}


def test_cancel_prepared_never_submitted_order_can_finish_without_broker_cancel(rig):
 arm_accept(rig);rig[-2].on_review=lambda:rig[-2].view.update(profit_ok=False);step(rig)
 assert row(rig)['attempts'][0]['transport_attempts']==0 and not rig[-2].orders
 rig[-1].cancel('plan-1');step(rig,20);step(rig)
 assert row(rig)['status']=='CANCELLED' and not rig[-2].calls
