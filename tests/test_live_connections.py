"""End-to-end live wiring tests use a schema-checked fake broker only."""
import asyncio,copy,json,uuid
from dataclasses import asdict
from datetime import datetime,timezone
from decimal import Decimal
from pathlib import Path
import pytest
from jsonschema import validate
from sailboat.controller import Limits
from sailboat.order_execution import Journal,broker_arguments
from sailboat.live_broker import AccountRisk,CONTRACTS
from sailboat.live_controls import LiveControls
from sailboat.robinhood_orders import RobinhoodOptionTransport
from sailboat.cloud_store import DurableController
from test_order_execution import Store,plan,run

ID='11111111-1111-4111-8111-111111111111'
CHAIN='22222222-2222-4222-8222-222222222222'
NOW=datetime(2026,10,23,15,0,tzinfo=timezone.utc).timestamp()
def iso(t):return datetime.fromtimestamp(t,timezone.utc).isoformat()
def sample(schema):
 t=schema.get('type','object')
 if isinstance(t,list):t=next(x for x in t if x!='null')
 if 'const' in schema:return schema['const']
 if 'enum' in schema:return schema['enum'][0]
 if t=='object':return {k:sample(schema['properties'][k]) for k in schema.get('required',[])}
 if t=='array':return []
 if t=='boolean':return False
 if t in ['integer','number']:return max(0,schema.get('minimum',0))
 if t=='null':return None
 return '0'
def body(name):return sample(CONTRACTS[name]['output'])['data']
def item(name,key):return sample(CONTRACTS[name]['output']['properties']['data']['properties'][key]['items'])
def optionq(t):
 q=sample(CONTRACTS['get_option_quotes']['output']['properties']['data']['properties']['results']['items']['properties']['quote'])
 q.update(instrument_id=ID,ask_price='1.10',bid_price='1.00',mark_price='1.05',adjusted_mark_price='1.05',updated_at=iso(t),previous_close_date='2026-10-22')
 return q
class FakeRead:
 def __init__(self,service,t):
  self.service=service;self.t=t;self.calls=[];self.orders={};self.refs={};self.positions=[];self.pnl='0';self.approval=False;self.lost=False;self.fail_pnl=False;self.quote_stale=False;self.fees='0';self.bad_tick=False;self.null_rows=False;self.repeated_cursor=False
 async def read(self,name,args,persist=False):
  validate(args,CONTRACTS[name]['input']);self.calls.append(name);d=body(name)
  if name=='get_accounts':
   a=item(name,'accounts');a.update(account_number='TEST-ONLY',rhs_account_number='1234',agentic_allowed=True,option_level='option_level_3',state='active',deactivated=False,permanently_deactivated=False);d['accounts']=[a]
  elif name=='get_portfolio':
   for key in ['total_value','cash']:d[key]='10000'
   for key in ['equity_value','options_value','futures_value','event_contracts_value','crypto_value','pending_deposits','mutual_funds_value','fixed_income_value']:d[key]='0'
   d['currency']='USD';d['buying_power'].update(buying_power='10000',unleveraged_buying_power='10000',display_currency='USD')
  elif name=='get_realized_pnl':
   if self.fail_pnl:raise ValueError('PnL unavailable')
   d.update(account_number='1234',display_currency='USD',window='day',total_returns=self.pnl,data_points=[])
  elif name=='get_trade_approval_setting':d['setting'].update(account_number='TEST-ONLY',human_must_approve_trades=self.approval)
  elif name=='get_option_positions':d.update(positions=copy.deepcopy(self.positions),next='')
  elif name=='get_equity_positions':d.update(positions=[],next='')
  elif name=='get_option_orders':
   orders=list(self.orders.values());d.update(orders=[copy.deepcopy(o) for o in orders if not args.get('order_id') or o['id']==args['order_id']],next='')
   if self.null_rows:d['orders']=None
   if self.repeated_cursor:d['next']='repeat'
  elif name=='get_equity_orders':d.update(orders=[],next='')
  elif name=='get_trade_approvals':d.update(approvals=[],next='')
  elif name=='get_option_instruments':
   ins=item(name,'instruments');ins.update(id=ID,chain_id=CHAIN,chain_symbol='QQQ',underlying_type='equity',state='active',tradability='tradable',trade_value_multiplier='100',expiration_date='2026-10-30',sellout_datetime='2026-10-30T19:30:00+00:00',strike_price='710',type='call',min_ticks={'above_tick':'0.05','below_tick':'0.01' if not self.bad_tick else '0.25','cutoff_price':'3'})
   d.update(instruments=[ins],next='')
  elif name=='get_option_chains':
   ch=item(name,'chains');ch.update(id=CHAIN,symbol='QQQ',can_open_position=True,cash_component='0',trade_value_multiplier='100');d['chains']=[ch]
  elif name=='get_option_quotes':
   q=optionq(self.t[0]-100 if self.quote_stale else self.t[0]);d['results']=[{'quote':q,'close':{'instrument_id':ID,'symbol':'QQQ','date':'2026-10-22','price':'1','interpolated':False,'source':'fixture'}}]
  else:raise ValueError('unexpected fake read '+name)
  raw={'structuredContent':{'data':d,'guide':'Synthetic fixture following official schema'},'isError':False}
  validate(raw['structuredContent'],CONTRACTS[name]['output']);return {'source':'https://agent.robinhood.com/mcp/trading','tool':name,'retrieved_at':self.t[0],'result':raw}
 def fill(self,quantity):
  o=list(self.orders.values())[-1];o.update(processed_quantity=str(quantity),state='filled' if quantity==int(o['quantity']) else 'partially_filled')
  p=item('get_option_positions','positions');p.update(option_id=ID,chain_id=CHAIN,chain_symbol='QQQ',type='long',quantity=str(quantity),average_price='100',trade_value_multiplier='100',expiration_date='2026-10-30',intraday_quantity=str(quantity),intraday_average_open_price='100',pending_buy_quantity='0',pending_sell_quantity='0',pending_exercise_quantity='0',pending_assignment_quantity='0',pending_expiration_quantity='0')
  self.positions=[p]
class FakeTransport(RobinhoodOptionTransport):
 def __init__(self,read,permit):super().__init__(read,permit);self.writes=[]
 async def call(self,name,args):
  assert self.permit(name,args),'broker boundary must authorize exact args'
  validate(args,CONTRACTS[name]['input']);self.writes.append((name,copy.deepcopy(args)));b=self.readonly
  if name=='review_option_order':
   d=body(name);d.update({k:v for k,v in args.items() if k not in ['chain_symbol','underlying_type']});d.update(direction='debit' if args['legs'][0]['side']=='buy' else 'credit',order_checks={},option_quotes=[optionq(b.t[0])],customer_approval_required=False)
   d['fees']=sample(CONTRACTS[name]['output']['properties']['data']['properties']['fees']);d['fees']['total_fee']=b.fees
  elif name=='place_option_order':
   ref=args['ref_id']
   if ref not in b.refs:
    o=item('get_option_orders','orders');leg=item('get_option_orders','orders')['legs']
    legschema=CONTRACTS['get_option_orders']['output']['properties']['data']['properties']['orders']['items']['properties']['legs']['items']
    leg=sample(legschema);leg.update(args['legs'][0]);leg.update(expiration_date='2026-10-30',strike_price='710',option_type='call')
    oid=str(uuid.uuid4());o.update(id=oid,chain_id=CHAIN,chain_symbol='QQQ',quantity=args['quantity'],processed_quantity='0',pending_quantity=args['quantity'],canceled_quantity='0',price=args['price'],stop_price=args.get('stop_price'),state='confirmed',type='limit' if args['type']=='stop_limit' else args['type'],trigger='stop' if args['type']=='stop_limit' else 'immediate',time_in_force='gfd',market_hours='regular_hours',legs=[leg],trade_value_multiplier='100',created_at=iso(b.t[0]))
    b.refs[ref]=oid;b.orders[oid]=o
   d={'order':copy.deepcopy(b.orders[b.refs[ref]])}
   if b.lost:raise TimeoutError('lost after acceptance')
  elif name=='cancel_option_order':b.orders[args['order_id']]['state']='cancelled';d={'accepted':True}
  else:raise ValueError('unexpected fake mutation')
  validate({'data':d,'guide':'Synthetic broker response'},CONTRACTS[name]['output']);return d

@pytest.fixture
def setup(tmp_path):
 t=[NOW];s=DurableController(Store(tmp_path),tmp_path/'service');ro=FakeRead(s,t);ctl=LiveControls(s,ro,'TEST-ONLY',asdict(Limits()),clock=lambda:t[0]);tr=FakeTransport(ro,ctl.broker.permit);ctl.broker.transport=tr
 return t,s,ro,ctl,tr

def answer(req,t,**kw):
 p=plan(now=t,decision_id=req['decision_id'],plan_id=req['new_plan_id'],account_number='TEST-ONLY',quantity=3,**kw)
 return {'request_id':req['request_id'],'context_hash':req['context_hash'],'action':'execute','plan':p,'plan_id':None,'plan_version':None}
def advance(x,seconds=5):
 run(x[3].worker.tick());x[0][0]+=seconds;x[3].broker.view=None

def test_live_preflight_read_only_and_disarmed(setup):
 _,_,ro,c,tr=setup;status=run(c.refresh());assert status['live_ready'] and status['mode']=='DISARMED';assert not tr.writes

def test_complete_owner_arm_context_decision_review_place_fill(setup):
 t,s,ro,c,tr=setup;run(c.arm());req=run(c.context('Explicit fixture instruction'));a=answer(req,t[0]);receipt=run(c.respond(a));assert receipt['status']=='ACCEPTED'
 advance(setup);assert len(ro.orders)==1,c.worker.status()['orders'][req['new_plan_id']]['last_error'];assert [x[0] for x in tr.writes]==['review_option_order','place_option_order']
 ro.fill(3);advance(setup);assert c.worker.status()['orders'][req['new_plan_id']]['status']=='FILLED'
 assert run(c.respond(a))==receipt;assert len(ro.orders)==1

def test_disarmed_live_decision_denied(setup):
 t,_,_,c,tr=setup;req=run(c.context('Fixture only'))
 with pytest.raises(ValueError,match='DISARMED'):run(c.respond(answer(req,t[0])))
 assert not tr.writes

def test_real_position_context_includes_basis_and_marks(setup):
 t,_,ro,c,_=setup;run(c.refresh());run(c.arm());req=run(c.context('Fixture entry'));run(c.respond(answer(req,t[0])));advance(setup);ro.fill(3);advance(setup)
 ctx=run(c.context('Assess current holding'))['context'];p=ctx['positions'][0]
 assert p['position_id']=='rh-option:'+ID and p['cost_basis']=='300' and p['market_value']=='300.00'

def test_unknown_submission_retries_same_ref_id_through_transport(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])));ro.lost=True;advance(setup);ro.lost=False;advance(setup)
 places=[a for n,a in tr.writes if n=='place_option_order'];assert len(places)==2 and places[0]==places[1];assert len(ro.orders)==1
 assert c.worker.status()['orders'][req['new_plan_id']]['attempts'][0]['order_id']

def test_daily_real_loss_latches_and_survives_recovery(setup,tmp_path):
 _,s,ro,c,tr=setup;ro.pnl='-500';v=run(c.refresh());assert v['daily_risk']['entry_locked']
 ro.pnl='0';c.broker.view=None;v=run(c.refresh());assert v['daily_risk']['entry_locked']
 recovered=LiveControls(s,ro,'TEST-ONLY',asdict(Limits()),clock=c.clock);assert run(recovered.refresh())['daily_risk']['entry_locked'];assert not tr.writes

def test_daily_lock_entry_rejected_without_liquidation(setup):
 t,_,ro,c,tr=setup;ro.pnl='-500';run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])));advance(setup);advance(setup)
 assert c.worker.status()['orders'][req['new_plan_id']]['status']=='DAILY_LOCKED';assert not tr.writes

def test_missing_pnl_never_zero_or_entry_authorization(setup):
 _,_,ro,c,tr=setup;ro.fail_pnl=True;v=run(c.refresh());assert v['daily_risk']['pnl_usd'] is None and v['daily_risk']['entry_locked'];assert not tr.writes

@pytest.mark.parametrize('fault',['stale','tick','fees'])
def test_contract_quote_fee_limits_block_submission(setup,fault):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])))
 if fault=='stale':ro.quote_stale=True
 if fault=='tick':ro.bad_tick=True
 if fault=='fees':ro.fees='10000'
 advance(setup);assert not any(n=='place_option_order' for n,_ in tr.writes)

def test_complete_pagination_and_null_list_fail_closed(setup):
 _,_,ro,c,_=setup;ro.repeated_cursor=True;v=run(c.refresh());assert not v['live_ready'] and 'pagination' in v['blockers'][0]
 ro.repeated_cursor=False;ro.null_rows=True;v=run(c.refresh());assert not v['live_ready']

def test_owner_limit_and_account_cannot_be_changed_by_decision(setup):
 t,_,_,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));a=answer(req,t[0]);a['plan']['account_number']='ANOTHER'
 with pytest.raises(ValueError,match='binding'):run(c.respond(a))
 assert not tr.writes

def test_unverified_opening_book_blocks_entries_not_invents_basis(setup):
 _,_,_,c,_=setup;r=AccountRisk(c.journal,clock=c.clock)
 p={'id':ID,'quantity':1,'unit_cost':'100','multiplier':100}
 with pytest.raises(ValueError,match='initial daily basis'):r.update('0',[p],{ID:{'bid':'1'}})

def test_risk_only_writes_on_controls_or_position_changes(setup):
 _,s,_,c,_=setup;r=AccountRisk(c.journal,clock=c.clock);r.update('0',[],{});generation=s.generation
 r.update('5',[],{});r.update('10',[],{});assert s.generation==generation

def test_emergency_latch_independent_of_order_mode(setup):
 t,s,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])))
 s.scheduler_transaction(lambda ctl,state:ctl.emergency_stop());advance(setup);assert not tr.writes

def test_broker_approval_setting_blocks_arm_without_changing_it(setup):
 _,_,ro,c,tr=setup;ro.approval=True
 with pytest.raises(ValueError,match='approval'):run(c.arm())
 assert c.status()['mode']=='DISARMED' and not tr.writes

def test_disarm_cancels_known_order_and_preserves_position(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])));advance(setup);ro.fill(1);c.disarm();advance(setup);advance(setup)
 assert c.status()['mode']=='DISARMED';assert ro.positions[0]['quantity']=='1';assert not any(a.get('legs',[{}])[0].get('side')=='sell' for n,a in tr.writes)

def test_lost_decision_receipt_recovers_accepted_intent(setup):
 t,_,_,c,tr=setup;run(c.arm());req=run(c.context('Fixture'));a=answer(req,t[0]);c.worker.accept(a['plan'])
 result=run(c.respond(a));assert result['status']=='ACCEPTED';assert len(c.worker.status()['orders'])==1 and not tr.writes

def test_stop_limit_native_type_and_trigger_reconcile(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture stop-limit'));run(c.respond(answer(req,t[0],order_type='stop_limit',stop_price='1.10',limit_prices=['1.20'])));advance(setup);advance(setup)
 o=next(iter(ro.orders.values()));assert o['type']=='limit' and o['trigger']=='stop'
 assert c.worker.status()['orders'][req['new_plan_id']]['status']=='WORKING'

def test_projected_entry_spread_cannot_cross_daily_loss_limit(setup):
 t,_,ro,c,tr=setup;ro.pnl='-450';run(c.arm());req=run(c.context('Fixture'));run(c.respond(answer(req,t[0])));advance(setup)
 assert not any(n=='place_option_order' for n,_ in tr.writes)

def test_risk_reset_requires_verified_opening_marks(setup):
 t,_,_,c,_=setup;r=AccountRisk(c.journal,clock=c.clock);r.update('0',[],{})
 p={'id':ID,'quantity':2,'unit_cost':'100','multiplier':100};r.update('0',[p],{ID:{'bid':'1'}})
 t[0]=datetime(2026,10,26,14,0,tzinfo=timezone.utc).timestamp()
 with pytest.raises(ValueError,match='opening mark'):r.update('0',[p],{ID:{'bid':'1'}})
 v=r.update('0',[p],{ID:{'bid':'0.5','close':{'price':'1','date':'2026-10-23','interpolated':False}}});assert v['pnl_usd']=='-100.0'

def test_owner_disarm_during_arm_preflight_wins(setup):
 _,_,_,c,_=setup;original=c.broker.readiness
 async def race():
  result=await original();c.worker.disarm();return result
 c.broker.readiness=race
 with pytest.raises(ValueError,match='disarmed'):run(c.arm())
 assert c.worker.status()['mode']=='DISARMED'

def test_live_mcp_context_and_owner_isolation(setup):
 import hashlib
 from starlette.testclient import TestClient
 from sailboat.cloud_server import make_app
 _,s,_,c,tr=setup
 # This test drives tools explicitly; it does not start a background execution cycle.
 async def idle():await asyncio.sleep(3600)
 c.run=idle
 keys={hashlib.sha256(k.encode()).hexdigest():p for k,p in [('a'*40,{'source':'external-agent','conversation':'shared-paper','label':'a'}),('b'*40,{'source':'other','conversation':'other','label':'b'})]}
 with TestClient(make_app(s,keys,['testserver'],execution=c,owner_key_hash=hashlib.sha256(('o'*40).encode()).hexdigest())) as client:
  def rpc(name,args,key='a'*40):return client.post('/mcp',headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream'},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}).json()['result']
  reply=rpc('sailboat_live_context',{'operator_request':'Read-only fixture context'})
  view=reply['structuredContent']
  assert 'mode' not in view['context'] and 'execution' not in view
  assert 'DISARMED' not in json.dumps(reply) and 'LIVE_ARMED' not in json.dumps(reply)
  internal=c.worker.status()['live_requests'][view['request_id']]['payload']
  assert internal['context']['mode']=='DISARMED'
  assert view['context_hash']==internal['context_hash']
  for key in ('positions','pending_orders','active_plans'):
   assert view['context'][key]==internal['context'][key]
  assert 'risk' not in view['context'] and 'limits' not in view['context'] and 'approval_required' not in view['context']
  rejected=rpc('sailboat_live_submit',answer(view,c.clock()))
  assert not rejected['isError'] and rejected['structuredContent']['status']=='PREVIEW_VALIDATED'
  assert not c.worker.status()['orders'] # Shared disarmed proposal, never an executable order.
  assert not tr.writes
  run(c.arm())  # Fake broker fixture only; prove projection also applies while armed.
  armed=rpc('sailboat_live_context',{'operator_request':'Read-only fixture context'})['structuredContent']
  assert 'mode' not in armed['context'] and 'execution' not in armed
  assert c.worker.status()['live_requests'][armed['request_id']]['payload']['context']['mode']=='LIVE_ARMED'
  assert armed['context_hash']!=view['context_hash']
  c.disarm()
  assert rpc('sailboat_live_context',{'operator_request':'No access'},'b'*40)['isError']
  assert client.post('/owner/control',headers={'Authorization':'Bearer '+'a'*40},json={'command':'arm'}).status_code==403
 assert not tr.writes

def test_unknown_opening_basis_cannot_be_washed_by_closing(setup):
 _,_,_,c,_=setup;r=AccountRisk(c.journal,clock=c.clock);p={'id':ID,'quantity':1,'unit_cost':'100','multiplier':100}
 with pytest.raises(ValueError):r.update('0',[p],{ID:{'bid':'1'}})
 with pytest.raises(ValueError,match='remains unverified'):r.update('100',[],{})

def test_owner_authorized_close_survives_daily_entry_lock(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture entry'));run(c.respond(answer(req,t[0])));advance(setup);ro.fill(3);advance(setup)
 ro.pnl='-500';req=run(c.context('Explicit close fixture'))
 a=answer(req,t[0],side='sell',position_effect='close',position_id='rh-option:'+ID,expected_position_quantity=3,limit_prices=['1.00'])
 run(c.respond(a));advance(setup)
 places=[a for n,a in tr.writes if n=='place_option_order'];assert places[-1]['legs'][0]['side']=='sell'

def test_owner_limit_reprice_partial_real_adapter(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture entry'));run(c.respond(answer(req,t[0])));advance(setup,10);ro.fill(1);advance(setup);advance(setup)
 places=[a for n,a in tr.writes if n=='place_option_order'];assert len(places)==2 and places[1]['quantity']=='2' and places[1]['price']=='1.10'

def test_context_cannot_be_reused_after_position_change(setup):
 t,_,ro,c,tr=setup;run(c.arm());req=run(c.context('Fixture entry'));a=answer(req,t[0]);run(c.respond(a));advance(setup)
 old=run(c.context('Observation before fill'));ro.fill(1);c.broker.view=None
 reply={'request_id':old['request_id'],'context_hash':old['context_hash'],'action':'observe','plan':None,'plan_id':None,'plan_version':None}
 with pytest.raises(ValueError,match='state changed'):run(c.respond(reply))

def test_revision_preserves_original_decision_receipt(setup):
 t,_,_,c,_=setup;run(c.arm());req=run(c.context('Fixture'));a=answer(req,t[0]);first=run(c.respond(a));c.worker.cancel(req['new_plan_id']);advance(setup)
 req2=run(c.context('Revise cancelled plan'));p=answer(req2,t[0])['plan'];p['plan_id']=req['new_plan_id'];p['plan_version']=2
 response={'request_id':req2['request_id'],'context_hash':req2['context_hash'],'action':'execute','plan':p,'plan_id':None,'plan_version':None};run(c.respond(response))
 assert len(c.worker.status()['plan_history'])==1;assert run(c.respond(a))==first


def no_activity_pnl(now=NOW):
 from sailboat.market_hours import EASTERN
 midnight=datetime.fromtimestamp(now,EASTERN).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
 return {'account_number':'1234','display_currency':'USD','window':'day','total_returns':'0',
  'data_points':[{'start_time':iso(midnight),'end_time':iso(now),'realized_gain':None,'rate_of_realized_gain':None,'number_of_trades':0}]}


def test_null_zero_trade_bucket_requires_corroboration():
 from sailboat.live_broker import daily_realized
 p=no_activity_pnl();before=copy.deepcopy(p)
 old={'processed_quantity':'1','created_at':iso(NOW-86400),'updated_at':iso(NOW-86400),'legs':[]}
 total,source=daily_realized(p,'1234',[],None,[old],[],NOW)
 assert total==0 and source=='broker_zero_total_verified_flat_no_activity' and p==before
 for changes in ({'account_number':'wrong'},{'display_currency':'EUR'},{'window':'week'},{'total_returns':'-500'},{'data_points':None}):
  with pytest.raises(ValueError):daily_realized(p|changes,'1234',[],None,[old],[],NOW)
 for row_change in ({'number_of_trades':1},{'number_of_trades':-1},{'start_time':iso(NOW-86400)},{'end_time':iso(NOW+300)}):
  q=copy.deepcopy(p);q['data_points'][0].update(row_change)
  with pytest.raises(ValueError):daily_realized(q,'1234',[],None,[old],[],NOW)
 for positions,prior in (([{'id':ID}],None),([] ,{'positions':[{'id':ID}]})):
  with pytest.raises(ValueError):daily_realized(p,'1234',positions,prior,[old],[],NOW)
 for order in (old|{'updated_at':iso(NOW)},old|{'updated_at':None},old|{'legs':[{'executions':[{'timestamp':iso(NOW)}]}]}):
  with pytest.raises(ValueError):daily_realized(p,'1234',[],None,[order],[],NOW)
 with pytest.raises(ValueError):daily_realized(p,'1234',[],None,[],[{'cumulative_quantity':'1','created_at':iso(NOW-86400),'last_transaction_at':iso(NOW)}],NOW)
 with pytest.raises(ValueError):daily_realized(p,'1234',[],None,None,[],NOW)


def test_verified_zero_trade_bucket_refresh_and_latched_loss(setup):
 t,s,ro,c,tr=setup;old=ro.read
 async def read(name,args,**kw):
  result=await old(name,args,**kw)
  if name=='get_realized_pnl':
   assert args['timezone']=='America/New_York'
   result['result']['structuredContent']['data'].update(no_activity_pnl(t[0]))
  return result
 ro.read=read
 v=run(c.refresh());assert v['daily_risk']['available'] and v['daily_risk']['pnl_usd']=='0'
 assert not v['daily_risk']['entry_locked'] and v['mode']=='DISARMED' and not tr.writes
 c.broker.risk.update('-500',[],{})
 v=run(c.refresh());assert v['daily_risk']['entry_locked'] and not tr.writes


def test_null_gain_with_a_trade_still_blocks_entry(setup):
 t,s,ro,c,tr=setup;old=ro.read
 async def read(name,args,**kw):
  result=await old(name,args,**kw)
  if name=='get_realized_pnl':
   p=no_activity_pnl(t[0]);p['data_points'][0]['number_of_trades']=1
   result['result']['structuredContent']['data'].update(p)
  return result
 ro.read=read;run(c.arm());request=run(c.context('Synthetic entry'))
 run(c.respond(answer(request,t[0])));advance(setup)
 assert not tr.writes and run(c.refresh())['daily_risk']['entry_locked']
