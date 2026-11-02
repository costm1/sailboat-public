import asyncio,copy,json,time
from datetime import datetime,timezone
from pathlib import Path
import pytest
from sailboat.scheduler import Scheduler,normalize_quotes,event_keys
from sailboat.cloud_store import DurableController
from sailboat.structured_schema import empty_body
from sailboat.agent_link import AgentLink
from test_cloud import Store,P

class Broker:
 def __init__(self):self.calls=0;self.stale=False
 async def read(self,name,args,**kw):
  self.calls+=1;t=time.time()-(1000 if self.stale else 0);iso=datetime.fromtimestamp(t,timezone.utc).isoformat()
  return {'source':'https://agent.robinhood.com/mcp/trading','tool':name,'retrieved_at':time.time(),
   'result':{'structuredContent':{'data':{'results':[{'quote':{'symbol':s,'state':'active','last_trade_price':'101','bid_price':'100','ask_price':'102',
   'venue_bid_time':iso,'venue_ask_time':iso,'venue_last_trade_time':iso}} for s in args['symbols']]}}}}
class Agent:
 name='test_agent'
 def __init__(self):self.calls=0;self.started=None;self.release=None
 async def decide(self,r):
  self.calls+=1
  if self.started:self.started.set();await self.release.wait()
  return {'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body(action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0)}, {'test':True}

def setup(tmp):
 store=Store(tmp);svc=DurableController(store,tmp/'live');broker=Broker();agent=Agent();s=Scheduler(svc,broker,agent,symbols=('QQQ',))
 return store,svc,broker,agent,s

def test_scheduled_cycle_and_restart_no_duplicate(tmp_path):
 store,svc,b,a,s=setup(tmp_path)
 async def run():
  await s.recover();out=await s.cycle()
  assert out['status']=='COMPLETED' and out['receipt']['result']['status']=='WATCHING'
  assert (await s.cycle())['status']=='NOT_DUE'
  recovered=DurableController(store,tmp_path/'restart');again=Scheduler(recovered,b,a,symbols=('QQQ',))
  await again.recover();assert (await again.cycle())['status']=='NOT_DUE'
  state,ctx=await again.view();assert state['cycles']==1 and len(ctx['plans'])==1 and ctx['mode']=='DISARMED'
  c=AgentLink(recovered.db);assert c.db.execute('SELECT count(*) FROM fills').fetchone()[0]==0;c.close()
  assert a.calls==1
 asyncio.run(run())

def test_response_saved_before_crash_recovers_without_inference(tmp_path):
 store,svc,b,a,s=setup(tmp_path)
 async def run():
  await s.recover()
  def stage(c,state):
   r=c.request('Watch QQQ')
   response={'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body(action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0)}
   state['active']={'cycle_id':'test','request_id':r['request_id'],'phase':'RESPONSE_READY','response':response}
  await s.tx(stage)
  recovered=DurableController(store,tmp_path/'restart');again=Scheduler(recovered,b,a,symbols=('QQQ',));await again.recover()
  st,ctx=await again.view();assert st['last_cycle']['status']=='RECOVERED' and len(ctx['plans'])==1 and a.calls==0
 asyncio.run(run())

def test_restart_during_inference_does_not_reinvoke(tmp_path):
 store,svc,b,a,s=setup(tmp_path)
 async def run():
  await s.recover();await s.tx(lambda c,st:st.update(active={'cycle_id':'test','phase':'DISPATCHED'}))
  recovered=DurableController(store,tmp_path/'restart');again=Scheduler(recovered,b,a,symbols=('QQQ',));await again.recover()
  st,_=await again.view();assert st['last_cycle']['status']=='INTERRUPTED_NO_ACTION' and a.calls==0
 asyncio.run(run())

def test_stale_market_data_never_invokes_agent(tmp_path):
 _,_,b,a,s=setup(tmp_path);b.stale=True
 async def run():
  await s.recover();out=await s.cycle();assert out['status']=='UNAVAILABLE_NO_ACTION' and a.calls==0
 asyncio.run(run())

def test_monitor_and_overlap_while_inference_waits(tmp_path):
 _,svc,b,a,s=setup(tmp_path)
 async def run():
  await s.recover();a.started=asyncio.Event();a.release=asyncio.Event()
  task=asyncio.create_task(s.cycle());await a.started.wait()
  assert (await s.cycle())['status']=='BUSY'
  await s.poll();assert s.polls==1
  await asyncio.to_thread(svc.monitor)
  a.release.set();assert (await task)['status']=='COMPLETED';assert a.calls==1
 asyncio.run(run())

def test_market_updates_dont_stale_request_but_plan_updates_do(tmp_path):
 _,svc,_,_,_=setup(tmp_path)
 r=svc.execute(P,'sailboat_get_context',{'operator_request':'Observe'})
 svc.observation_write('scheduler_quotes',{'quotes':[]})
 response={'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body()}
 assert svc.execute(P,'sailboat_submit_decision',response)['result']['status']!='REJECTED'

@pytest.mark.parametrize('kind',['missing','crossed','future','duplicate'])
def test_bad_quotes_fail_closed(kind):
 async def run():
  obs=await Broker().read('get_equity_quotes',{'symbols':['QQQ']});rows=obs['result']['structuredContent']['data']['results'];q=rows[0]['quote']
  if kind=='missing':rows.clear()
  if kind=='crossed':q['bid_price']='200'
  if kind=='future':
   t=datetime.fromtimestamp(time.time()+600,timezone.utc).isoformat()
   for k in ('venue_bid_time','venue_ask_time','venue_last_trade_time'):q[k]=t
  if kind=='duplicate':rows.append(copy.deepcopy(rows[0]))
  with pytest.raises(ValueError):normalize_quotes(obs,['QQQ'],time.time())
 asyncio.run(run())

def test_event_price_and_expiration():
 now=time.time();ctx={'positions':[{'id':'p','instrument':'opt','quantity':1}],
  'instruments':[{'id':'opt','kind':'option','details':{'expiry':datetime.fromtimestamp(now+600,timezone.utc).isoformat()}}],
  'plans':[{'id':'plan','version':1,'status':'WATCHING','content':{'condition':{'symbol':'QQQ','op':'gte','price':100}}}],
  'quotes':[{'symbol':'QQQ','last':101,'previous':99,'fresh':True}]}
 keys=event_keys(ctx,now);assert 'price:plan:1' in keys and any(k.startswith('expiry:p:') for k in keys)


def test_price_monitor_updates_during_slow_agent(tmp_path):
 _,svc,b,a,s=setup(tmp_path)
 async def run():
  await s.recover()
  def seed(c,st):
   c.register_instrument('QQQ')
   r=c.request('Watch above 100')
   c.respond(json.dumps({'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body(action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0,condition={'symbol':'QQQ','op':'gte','price':100})}))
  await s.tx(seed)
  await s.poll()
  a.started=asyncio.Event();a.release=asyncio.Event()
  task=asyncio.create_task(s.cycle());await a.started.wait();before=b.calls
  await s.poll();assert b.calls==before+1
  st,ctx=await s.view();assert any(k.startswith('price:') for k in st['event_keys']) and ctx['quotes'][0]['fresh']
  a.release.set();await task
 asyncio.run(run())

def test_hub_snapshot_budget_refuses_before_upload(tmp_path,monkeypatch):
 from sailboat.cloud_store import HubSnapshots,StorageUnavailable
 monkeypatch.setattr('sailboat.cloud_store.MAX_SNAPSHOT_BYTES',8)
 p=tmp_path/'fake.sqlite';p.write_bytes(b'x')
 store=object.__new__(HubSnapshots)
 with pytest.raises(StorageUnavailable,match='storage limit'):store.save(p,'parent',4001)

def test_scheduler_request_excludes_broker_account_data(tmp_path):
 _,svc,b,a,s=setup(tmp_path)
 svc.observation_write('get_accounts',{'result':{'private_account':'not-for-inference'}})
 async def run():
  await s.recover();out=await s.cycle()
  c=AgentLink(svc.db);raw=c.db.execute('SELECT payload FROM agent_requests WHERE id=?',(out['request_id'],)).fetchone()[0];c.close()
  assert 'private_account' not in raw and 'not-for-inference' not in raw
 asyncio.run(run())

def test_bounded_assessment_window_survives_restart(tmp_path):
 store,svc,b,a,_=setup(tmp_path);now=time.time();clock=[now]
 s=Scheduler(svc,b,a,symbols=('QQQ',),clock=lambda:clock[0],not_before=now+30,stop_at=now+60,max_cycles=1)
 async def run():
  await s.recover();assert (await s.cycle())['status']=='BEFORE_ASSESSMENT_WINDOW';assert a.calls==0
  clock[0]=now+61
  recovered=DurableController(store,tmp_path/'window-restart')
  again=Scheduler(recovered,b,a,symbols=('QQQ',),clock=lambda:clock[0],not_before=now+30,stop_at=now+60,max_cycles=1)
  await again.recover();assert (await again.cycle())['status']=='ASSESSMENT_WINDOW_COMPLETE';assert a.calls==0
 asyncio.run(run())

def test_cadence_is_start_to_start_and_cap_stops(tmp_path):
 _,svc,b,a,s=setup(tmp_path);s.interval=120;s.max_cycles=1
 async def run():
  await s.recover();out=await s.cycle();st,_=await s.view()
  assert abs(st['next_due']-out['started_at']-120)<.001
  assert (await s.cycle())['status']=='BOUNDED_TEST_COMPLETE';assert a.calls==1
 asyncio.run(run())

def test_trial_disables_extra_event_wakeups(tmp_path):
 _,svc,b,a,s=setup(tmp_path);s.event_wakeups=False
 async def run():
  await s.recover();await s.poll()
  def change(c,st):st['event_keys']=['positions:previous'];st['next_due']=time.time()+120
  await s.tx(change);before,_=await s.view();await s.poll();after,_=await s.view()
  assert after['next_due']==before['next_due'] and 'wake_reason' not in after
 asyncio.run(run())
