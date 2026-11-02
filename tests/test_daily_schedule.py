import asyncio,json,time
from datetime import datetime
from pathlib import Path
import pytest
from sailboat.market_hours import RegularMarketHours
from sailboat.controller import Controller,Limits,Rejected
from sailboat.agent_link import AgentLink
from sailboat.schema import Message
from sailboat.structured_schema import empty_body
from sailboat.scheduler import Scheduler
from sailboat.cloud_store import DurableController
from test_cloud import Store
from test_scheduler import Broker

def at(s):return datetime.fromisoformat(s).timestamp()
class Clock:
 def __init__(self,s='2026-10-23T10:00:00-04:00'):self.now=at(s)
 def __call__(self):return self.now
 def advance(self,seconds=1):self.now+=seconds

def msg(i,text):return Message('external-agent','shared-paper',i,text)
def order(side,q=10,**kw):return dict(tool='create_simulated_order',instrument='QQQ',action=side,quantity=q,**kw)
def rig(tmp):
 clock=Clock();c=Controller(tmp/'daily.sqlite',clock=clock);c.register_instrument('QQQ');c.configure_daily_loss_limit(500)
 c.set_mode('PAPER_ARMED');c.market_snapshot('QQQ',100,100)
 assert c.apply(msg('buy','Buy 10 QQQ shares.'),order('buy'))['status']=='FILLED'
 return c,clock

@pytest.mark.parametrize('date,opened,reason',[
 ('2026-10-23T09:29:59-04:00',False,'MARKET_CLOSED'),('2026-10-23T09:30:00-04:00',True,'REGULAR_SESSION'),
 ('2026-10-23T16:00:00-04:00',False,'MARKET_CLOSED'),('2026-10-24T10:00:00-04:00',False,'MARKET_CLOSED'),
 ('2026-11-26T10:00:00-05:00',False,'MARKET_CLOSED'),('2026-11-27T12:59:59-05:00',True,'REGULAR_SESSION'),
 ('2026-11-27T13:00:00-05:00',False,'MARKET_CLOSED'),('2026-12-24T13:00:00-05:00',False,'MARKET_CLOSED'),
 ('2026-11-02T09:30:00-05:00',True,'REGULAR_SESSION'),('2027-12-31T10:00:00-05:00',True,'REGULAR_SESSION'),
 ('2028-07-03T13:00:00-04:00',False,'MARKET_CLOSED'),('2029-01-02T10:00:00-05:00',False,'CALENDAR_UNVERIFIED')])
def test_official_hours(date,opened,reason):
 result=RegularMarketHours().status(at(date));assert result['open']==opened and result['reason']==reason

def test_loss_locks_entries_without_liquidation_and_survives_restart(tmp_path):
 c,clock=rig(tmp_path);clock.advance();c.market_snapshot('QQQ',50,51)
 assert c.daily_risk_status()['entry_locked'] and c.daily_risk_status()['pnl_usd']=='-500.0'
 assert c._meta('mode')=='PAPER_ARMED' and c._meta('estop')=='0'
 assert c.db.execute('SELECT quantity FROM positions').fetchone()[0]==10
 assert c.db.execute('SELECT count(*) FROM fills').fetchone()[0]==1
 result=c.apply(msg('add','Buy 1 QQQ share.'),order('buy',1));assert result['status']=='REJECTED'
 c.close();c=Controller(tmp_path/'daily.sqlite',clock=clock)
 clock.advance();c.market_snapshot('QQQ',100,100)
 assert c.daily_risk_status()['entry_locked'] # recovery of price is not reset
 c.close()

def test_authorized_exit_condition_survives_daily_lock(tmp_path):
 c,clock=rig(tmp_path)
 entry=c.apply(msg('pending','Buy 1 QQQ share at limit 50.'),order('buy',1,limit_price=50))
 exit=c.apply(msg('exit','Sell 2 QQQ shares when QQQ is at or below 50.'),dict(tool='monitor_price',instrument='QQQ',action='sell',quantity=2,condition_symbol='QQQ',operator='lte',price=50))
 clock.advance();c.market_snapshot('QQQ',50,51,last=50)
 assert c.db.execute('SELECT status FROM orders WHERE id=?',(entry['order_id'],)).fetchone()[0]=='CANCELED'
 assert c.db.execute('SELECT quantity FROM positions').fetchone()[0]==8
 assert c.daily_risk_status()['entry_locked'] and c._meta('mode')=='PAPER_ARMED'
 # Explicit later close is permitted, unlike an automatic price-loss exit.
 result=c.apply(msg('close','Sell 8 QQQ shares.'),order('sell',8));assert result['status']=='FILLED'
 assert c.daily_risk_status()['pnl_usd']=='-500.0';c.close()

def test_new_day_reset_is_not_a_lifetime_loss_or_rearm(tmp_path):
 c,clock=rig(tmp_path);clock.advance();c.market_snapshot('QQQ',50,50)
 c.apply(msg('close','Sell 10 QQQ shares.'),order('sell'))
 c.set_mode('DISARMED');clock.now=at('2026-10-24T00:00:00-04:00');c.tick()
 r=c.daily_risk_status();assert not r['entry_locked'] and r['pnl_usd']=='0.0'
 assert c._meta('mode')=='DISARMED';c.set_mode('PAPER_ARMED');c.market_snapshot('QQQ',50,50)
 assert c.apply(msg('new','Buy 1 QQQ share.'),order('buy',1))['status']=='FILLED';c.close()

def test_overnight_gap_uses_prior_recorded_mark_and_stale_blocks_entries(tmp_path):
 c,clock=rig(tmp_path);clock.advance();c.market_snapshot('QQQ',90,90)
 clock.now=at('2026-10-24T09:30:00-04:00');c.tick()
 assert not c.daily_risk_status()['entries_allowed'] and c.daily_risk_status()['pnl_usd'] is None
 c.market_snapshot('QQQ',40,40)
 assert c.daily_risk_status()['pnl_usd']=='-500.0' and c.daily_risk_status()['entry_locked'];c.close()

def test_option_multiplier_and_no_per_position_stop(tmp_path):
 clock=Clock();c=Controller(tmp_path/'option.sqlite',clock=clock)
 c.register_instrument('QQQ-C','QQQ','option',100,{'expiry':'2026-10-30','strike':500,'right':'call'})
 c.configure_daily_loss_limit(500);c.set_mode('PAPER_ARMED');c.market_snapshot('QQQ-C',10,10)
 assert c.apply(msg('entry','Buy 1 QQQ-C contract.'),dict(tool='create_simulated_order',instrument='QQQ-C',quantity=1,action='buy'))['status']=='FILLED'
 clock.advance();c.market_snapshot('QQQ-C',6.4,6.4)
 assert not c.daily_risk_status()['entry_locked'] and c.daily_risk_status()['pnl_usd']=='-360.0'
 assert c.db.execute('SELECT count(*) FROM fills').fetchone()[0]==1
 clock.advance();c.market_snapshot('QQQ-C',5,5)
 assert c.daily_risk_status()['entry_locked'] and c.db.execute('SELECT quantity FROM positions').fetchone()[0]==1;c.close()

def test_projected_spread_cannot_cross_daily_limit(tmp_path):
 c,clock=rig(tmp_path);clock.advance();c.market_snapshot('QQQ',51,61)
 assert c.daily_risk_status()['pnl_usd']=='-490.0'
 assert c.apply(msg('add','Buy 1 QQQ share.'),order('buy',1))['status']=='REJECTED'
 assert c.db.execute('SELECT quantity FROM positions').fetchone()[0]==10;c.close()

def test_no_calendar_dispatch_and_closed_recovery(tmp_path):
 store=Store(tmp_path);svc=DurableController(store,tmp_path/'live')
 class Never:
  name='never'
  async def decide(self,r):raise AssertionError('outside-hours inference')
 s=Scheduler(svc,Broker(),Never(),market_hours=RegularMarketHours(),clock=Clock('2026-11-26T10:00:00-05:00'))
 async def run():
  await s.recover();assert (await s.cycle())['status']=='MARKET_CLOSED'
  await s.tx(lambda c,st:st.update(active={'cycle_id':'staged','phase':'RESPONSE_READY','response':{}}))
  await s.recover();state,_=await s.view();assert state['last_cycle']['status']=='MARKET_CLOSED_NO_ACTION'
 asyncio.run(run())

def test_existing_position_and_thesis_reach_agent_while_locked(tmp_path):
 store=Store(tmp_path);svc=DurableController(store,tmp_path/'live');broker=Broker()
 def seed(c,state):
  c.register_instrument('QQQ');c.register_instrument('MSFT');c.configure_daily_loss_limit(500)
  # Isolated synthetic existing holding; never touches cloud portfolio.
  c.db.execute("INSERT INTO positions VALUES('held','external-agent/shared-paper','QQQ',10,'150')")
  c.db.execute("INSERT INTO positions VALUES('second','external-agent/shared-paper','MSFT',1,'100')")
  c.market_snapshot('MSFT',100,102)
  c.market_snapshot('QQQ',100,102)
  r=c.request('Existing thesis: hold while the authorized thesis remains valid.')
  c.respond(json.dumps({'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body(action='observe',symbol='QQQ',reasoning='Prior thesis retained')}))
 svc.scheduler_transaction(seed)
 class Inspect:
  name='test_inspect'
  async def decide(self,r):
   ctx=r['context'];assert {p['id']:p for p in ctx['positions']}['held']['quantity']==10
   assert len(ctx['positions'])==2 and {p['instrument'] for p in ctx['positions']}=={'QQQ','MSFT'}
   held={p['position_id']:p for p in ctx['position_valuations']}['held']
   assert held['cost_basis_usd']=='1500' and held['mark_value_usd']=='1000.0'
   assert ctx['daily_risk']['entry_locked'] and r['recent_receipts'][-1]['decision']['reasoning']=='Prior thesis retained'
   assert ctx['mode']=='DISARMED'
   return dict(request_id=r['request_id'],context_hash=r['context_hash'],decision=empty_body(action='hold',symbol='QQQ',position_id='held',position_quantity=10)),{'test':True}
 s=Scheduler(svc,broker,Inspect(),interval=120,symbols=('SPY',))
 async def run():
  await s.recover();await s.poll();assert broker.calls==1 # held position triggers independent monitoring
  result=await s.cycle();assert result['status']=='COMPLETED' and result['receipt']['result']['status']=='HELD'
  assert (await s.cycle())['status']=='NOT_DUE'
 asyncio.run(run())
 c=AgentLink(svc.db);assert c.db.execute('SELECT quantity FROM positions').fetchone()[0]==10;c.close()


def test_daily_limit_shared_across_identities_and_emergency_reset_does_not_clear_it(tmp_path):
 c,clock=rig(tmp_path)
 c.apply(Message('another','portfolio','buy','Buy 1 QQQ share.'),order('buy',1))
 clock.advance();c.market_snapshot('QQQ',54,54)
 assert c.daily_risk_status()['entry_locked'] # 11 shares times $46, aggregated
 c.emergency_stop();c.reset_emergency_stop();c.set_mode('PAPER_ARMED')
 assert c.daily_risk_status()['entry_locked']
 assert c.apply(Message('another','portfolio','add','Buy 2 QQQ shares.'),order('buy',2))['status']=='REJECTED'
 c.close()


def test_enabling_midday_preserves_existing_daily_realized_loss(tmp_path):
 clock=Clock();c=Controller(tmp_path/'existing.sqlite',clock=clock);c.register_instrument('QQQ');c.set_mode('PAPER_ARMED')
 c.market_snapshot('QQQ',100,100);c.apply(msg('buy','Buy 2 QQQ shares.'),order('buy',2))
 clock.advance();c.market_snapshot('QQQ',95,95);c.apply(msg('sell','Sell 2 QQQ shares.'),order('sell',2))
 c.configure_daily_loss_limit(500);assert c.daily_risk_status()['pnl_usd']=='-10.0'
 assert c._meta('realized')=='-10.0';c.close()
