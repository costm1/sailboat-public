"""Owner-session actions use the existing journal and order worker only."""
import asyncio,copy,uuid
from decimal import Decimal
from . import owner_settings as policy
from .order_execution import TERMINAL
from .live_broker import option_quote
from .scheduler import timestamp

COMMANDS={'save_settings','restore_settings','operating_mode','arm_session','take_control','cancel_orders','set_target','clear_target','request_assessment','preview_close','confirm_close'}

async def handle(ex,scheduler,body):
 command=body['command'];now=ex.clock();state=await asyncio.to_thread(ex.worker.status)
 async def edit(fn):return await asyncio.to_thread(ex.journal.edit,fn)
 def keys(*names):
  if set(body)!={'command',*names}:raise ValueError('Unexpected or missing action fields')
 if command in ('save_settings','restore_settings'):
  if command=='save_settings':keys('values','expected_revision');values=body['values']
  else:
   keys('restore_revision','expected_revision')
   old=next((h for h in state.get('owner_settings_history',[]) if h['revision']==body['restore_revision']),None)
   if not old:raise ValueError('Saved settings revision unavailable')
   values=policy.DEFAULTS|old['values']
  return await edit(lambda s:policy.save(s,values,body['expected_revision'],ex.broker.limits,now))
 if command=='operating_mode':
  keys('mode');return await edit(lambda s:policy.set_mode(s,body['mode'],now))
 if command=='arm_session':
  bound='session_starts_at' in body or 'session_expires_at' in body
  keys('confirmation','expected_revision',*(['session_starts_at','session_expires_at'] if bound else []))
  if body['confirmation']!='ARM LIVE' or body['expected_revision']!=policy.revision(state):raise ValueError('Review current settings and confirm ARM LIVE')
  window=policy.arm_window(state,now);ends=window['expires_at']
  if bound and (body['session_starts_at']!=window['starts_at'] or body['session_expires_at']!=ends):raise ValueError('Trading session changed; review the displayed dates and arm again')
  def arm_state(s):
   if policy.revision(s)!=body['expected_revision']:raise ValueError('Settings changed during arm checks; review again')
   if ex.clock()>=ends:raise ValueError('Management window ended during arm checks')
   s['owner_session']={**window,'boot_id':s['boot_id'],'settings_revision':policy.revision(s),'armed_at':ex.clock()}
   policy.event(s,'ARMED_NEXT_SESSION' if window['next_session'] else 'ARMED_FOR_TODAY',ex.clock(),**window)
  result=await ex.arm(expected_epoch=state.get('control_epoch',0),state_update=arm_state)
  return result|window
 if command=='take_control':
  keys('position_id');await edit(lambda s:policy.take_control(s,body['position_id'],now))
  return {'status':'MANUAL_CONTROL','detail':'Automatic management paused; cancellation requests await broker confirmation.'}
 if command=='cancel_orders':
  keys('confirmation')
  if body['confirmation']!='CANCEL ORDERS':raise ValueError('Confirm CANCEL ORDERS')
  def cancel(s):
   policy.set_mode(s,'manage_only',now)
   for row in s['orders'].values():
    if row['status'] not in TERMINAL:
     row['stop_reason']='CANCELLED'
     if row['plan']['side']=='sell':
      pid=row['plan']['position_id'];s.setdefault('manual_management_holds',{})[pid]={'active':True,'at':now,'reason':'Owner cancelled working orders'}
   policy.event(s,'CANCEL_ORDERS_REQUESTED',now)
  await edit(cancel);return {'status':'CANCEL_REQUESTED','detail':'New entries paused. Affected exits stay under manual control until resumed.'}
 if command=='request_assessment':
  keys()
  if scheduler is None:raise ValueError('Scheduler unavailable')
  def wake(c,s):
   if s.get('active'):return {'status':'ALREADY_ANALYZING'}
   s['next_due']=min(s.get('next_due',now),now);s['wake_reason']='Owner requested fresh assessment'
   return {'status':'ASSESSMENT_REQUESTED'}
  return await scheduler.tx(wake)
 if command in ('set_target','clear_target'):
  keys('position_id',*(['limit_price'] if command=='set_target' else []));pid=body['position_id']
  view=await ex.broker.account_view(force=True);p=next((p for p in view['positions'] if p['position_id']==pid),None)
  if not p:raise ValueError('Position no longer exists')
  if command=='set_target':
   price=Decimal(str(body['limit_price']))
   if not price.is_finite() or price<=0 or price*100<=Decimal(p['unit_cost']):raise ValueError('Automatic target must exceed purchase premium; use explicit Close for a loss-making exit')
   from .adaptive_profit import round_tick
   details=await ex.profit_manager.port.exit_details(p,state,{})
   if Decimal(round_tick(price,details))!=price:raise ValueError('Price does not match the contract tick size')
  def target(s):
   if s.get('position_management_epochs',{}).get(pid)!=state.get('position_management_epochs',{}).get(pid):raise ValueError('Holding changed; refresh target')
   if command=='clear_target':s.setdefault('owner_targets',{}).pop(pid,None)
   else:s.setdefault('owner_targets',{})[pid]={'limit_price':str(price),'at':now,'target_id':uuid.uuid4().hex,'position_epoch':copy.deepcopy(s.get('position_management_epochs',{}).get(pid))}
   policy.event(s,command.upper(),now,position_id=pid)
  await edit(target);return {'status':'TARGET_SAVED','detail':'Applies when Sailboat manages this holding; competing exits reconcile before replacement.'}
 if command=='preview_close':
  keys('position_id');pid=body['position_id'];view=await ex.broker.account_view(force=True)
  p=next((p for p in view['positions'] if p['position_id']==pid and p['kind']=='option'),None)
  if not p:raise ValueError('Verified option holding unavailable')
  state=await asyncio.to_thread(ex.worker.status)
  if any(r['plan']['option_id']==p['id'] and r['status'] not in TERMINAL for r in state['orders'].values()) or p.get('pending_sell'):
   raise ValueError('Take manual control and wait for working orders to reconcile before preparing this exit')
  if any(any(l.get('option_id')==p['id'] for l in o.get('legs',[])) for o in view['pending_orders']):raise ValueError('Broker reports a competing order; reconcile it first')
  session=ex.broker.calendar.status(ex.clock())
  if not session['open']:raise ValueError('Outside regular session; no exit was queued')
  instruments=await ex.broker.pages('get_option_instruments',{'ids':p['id']},'instruments')
  if len(instruments)!=1 or instruments[0]['id']!=p['id']:raise ValueError('Exact contract unavailable')
  ins=instruments[0];rows=(await ex.broker.read('get_option_quotes',{'instrument_ids':[p['id']]}))['results']
  if len(rows)!=1:raise ValueError('Exact quote unavailable')
  quote=option_quote(rows[0],ex.clock())
  if quote['id']!=p['id'] or Decimal(quote['bid'])<=0:raise ValueError('No positive executable bid')
  now=ex.clock();expires=min(now+30,session['close'],timestamp(ins['sellout_datetime']),policy.boundary(now,policy.settings(state)['management_stop']))
  if expires-now<5:raise ValueError('Insufficient session time for a reviewed exit')
  token='owner-close-'+uuid.uuid4().hex
  plan={'decision_id':token,'plan_id':token,'plan_version':1,'account_number':view['account_number'],
   'option_id':p['id'],'symbol':p['symbol'],'side':'sell','position_effect':'close','quantity':p['quantity'],
   'order_type':'limit','limit_prices':[quote['bid']],'stop_price':None,'condition':None,'invalidation':None,
   'created_at':now,'expires_at':expires,'replace_after_seconds':60,'max_transport_attempts':1,
   'position_id':pid,'expected_position_quantity':p['quantity'],'time_in_force':'gfd','market_hours':'regular_hours'}
  preview={'token':token,'plan':plan,'position_epoch':copy.deepcopy(state.get('position_management_epochs',{}).get(pid)),
   'boot_id':state['boot_id'],'control_epoch':state.get('control_epoch',0),'unit_cost':p['unit_cost'],
   'loss':Decimal(quote['bid'])*100<Decimal(p['unit_cost']),'quote':quote,'expires_at':expires,'confirmed':False}
  await edit(lambda s:s.setdefault('owner_close_previews',{}).__setitem__(token,preview))
  return {'status':'PREVIEW','token':token,'quantity':p['quantity'],'symbol':p['symbol'],'strike':ins['strike_price'],'right':ins['type'],
   'expiry':ins['expiration_date'],'limit_price':quote['bid'],'estimated_premium':str(Decimal(quote['bid'])*100*p['quantity']),
   'estimated_pnl_before_fees':str((Decimal(quote['bid'])*100-Decimal(p['unit_cost']))*p['quantity']),
   'loss':preview['loss'],'expires_at':expires}
 if command=='confirm_close':
  keys('token','confirmation','allow_loss')
  preview=state.get('owner_close_previews',{}).get(body['token'])
  if not preview or body['confirmation']!='CLOSE POSITION' or type(body['allow_loss']) is not bool:raise ValueError('Exact owner exit confirmation required')
  if preview['confirmed']:
   return {'status':'ACCEPTED','plan_id':preview['plan']['plan_id'],'duplicate':True}
  if preview['loss'] and not body['allow_loss']:raise ValueError('Explicit loss acknowledgement required')
  if now>=preview['expires_at'] or preview['boot_id']!=state['boot_id'] or preview['control_epoch']!=state.get('control_epoch',0):raise ValueError('Exit preview expired or owner controls changed; prepare again')
  if state['mode']!='LIVE_ARMED':raise ValueError('Arm in manage-only mode before confirming an exit')
  view=await ex.broker.account_view(force=True);p=next((p for p in view['positions'] if p['position_id']==preview['plan']['position_id']),None)
  if not p or p['quantity']!=preview['plan']['quantity'] or p['unit_cost']!=preview['unit_cost']:raise ValueError('Position changed; prepare a fresh exit')
  def authorize(s):
   if s.get('control_epoch',0)!=preview['control_epoch'] or s['boot_id']!=preview['boot_id']:raise ValueError('Owner controls changed')
   s.setdefault('owner_exit_authorizations',{})[body['token']]=copy.deepcopy(preview)
   s['owner_close_previews'][body['token']]['confirmed']=True
   pid=preview['plan']['position_id'];s.setdefault('manual_management_holds',{})[pid]={'active':True,'at':now,'reason':'Owner explicitly requested close'}
   policy.event(s,'OWNER_CLOSE_ACCEPTED',now,position_id=pid,plan_id=preview['plan']['plan_id'])
  await asyncio.to_thread(ex.worker.accept,preview['plan'],state_update=authorize)
  return {'status':'ACCEPTED','plan_id':preview['plan']['plan_id'],'detail':'Awaiting existing broker review and fill confirmation; no fill is assumed.'}
 raise ValueError('Unknown owner action')
