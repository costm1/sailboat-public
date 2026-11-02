"""Durable owner policy. Analyst text cannot write this configuration."""
import copy,re,uuid
from datetime import datetime,time
from decimal import Decimal
from .market_hours import EASTERN,RegularMarketHours

DEFAULTS={'minimum_probability':40,'entry_start':'09:30','entry_stop':'16:00','management_stop':'16:00',
 'sizing_mode':'analyst','fixed_contracts':1,'trade_budget':250,'capital_budget':10000,'position_budget':2500,
 'fallback_profit_percent':20,'fallback_misses':2,'daily_loss_limit':500,
 'scheduled_close_enabled':False,'scheduled_close_time':'15:45'}

def settings(state):return copy.deepcopy(DEFAULTS|state.get('owner_settings',{}).get('values',{}))
def revision(state):return state.get('owner_settings',{}).get('revision',0)
def validate(values,limits):
 if set(values)!=set(DEFAULTS):raise ValueError('Submit all supported settings; unknown fields are not accepted')
 v=copy.deepcopy(values)
 if type(v['scheduled_close_enabled']) is not bool:raise ValueError('Scheduled close must be on or off')
 for key,lo,hi in [('minimum_probability',0,100),('fixed_contracts',1,limits['max_order_quantity']),('fallback_misses',1,10),
     ('trade_budget',1,limits['max_order_notional']),('capital_budget',1,limits['max_gross_notional']),
     ('position_budget',1,limits['max_order_notional']),('fallback_profit_percent',0.01,1000),('daily_loss_limit',1,500)]:
  x=v[key]
  if type(x) not in (int,float) or not Decimal(str(x)).is_finite() or not lo<=x<=hi:raise ValueError(f'{key} must be between {lo} and {hi}')
  if key in ('fixed_contracts','fallback_misses') and type(x) is not int:raise ValueError(key+' must be a whole number')
 for key in ('entry_start','entry_stop','management_stop','scheduled_close_time'):
  if not isinstance(v[key],str) or not re.fullmatch(r'(?:0\d|1\d|2[0-3]):[0-5]\d',v[key]):raise ValueError('Use HH:MM Eastern times')
 if not '09:30'<=v['entry_start']<v['entry_stop']<=v['management_stop']<='16:00':raise ValueError('Session must satisfy 09:30 <= start < entry stop <= management stop <= 16:00 ET')
 if v['scheduled_close_enabled'] and not v['entry_start']<v['scheduled_close_time']<v['management_stop']:raise ValueError('Scheduled close must be after entry start and at least one minute before management ends')
 if v['sizing_mode'] not in ('analyst','contracts','budget'):raise ValueError('Unknown sizing mode')
 if v['position_budget']>v['capital_budget']:raise ValueError('Per-position budget cannot exceed allocated capital')
 return v

def event(state,kind,now,**details):
 item={'id':uuid.uuid4().hex,'kind':kind,'at':now,**details}
 state.setdefault('owner_activity',[]).append(item)
 return item

def save(state,values,expected,limits,now):
 if type(expected) is not int or expected!=revision(state):raise ValueError('Settings changed in another tab. Refresh before saving.')
 v=validate(values,limits);old=settings(state)
 if old==v:return {'status':'UNCHANGED','revision':expected}
 state.setdefault('owner_settings_history',[]).append({'revision':expected,'values':old,'replaced_at':now})
 state['owner_settings']={'revision':expected+1,'values':v,'updated_at':now}
 if state.get('owner_session'):
  session=state['owner_session'];session['expires_at']=min(session['expires_at'],boundary(session.get('starts_at',now),v['management_stop']))
 # No accepted buy can execute under a different policy than its source.
 from .order_execution import TERMINAL
 for row in state['orders'].values():
  if row['plan']['side']=='buy' and row['status'] not in TERMINAL:row['stop_reason']='CANCELLED'
 event(state,'SETTINGS_SAVED',now,revision=expected+1,changes={k:{'before':old[k],'after':v[k]} for k in v if old[k]!=v[k]})
 return {'status':'SAVED','revision':expected+1,'effect':'Fresh entries use these rules. Unfilled entry remainder is cancelled. Enabled scheduled closing may submit exits when armed and due.'}

def boundary(now,clock_text):
 day=datetime.fromtimestamp(now,EASTERN).date();h,m=map(int,clock_text.split(':'))
 return datetime.combine(day,time(h,m),EASTERN).timestamp()

def close_policy(state):
 v=settings(state)
 return {'enabled':v['scheduled_close_enabled'],'time':v['scheduled_close_time']}

def close_window(state,now,contract_close=None):
 v=settings(state);session=RegularMarketHours().status(now)
 if not v['scheduled_close_enabled'] or not session['open']:return None
 end=min(session['close'],boundary(now,v['management_stop']),contract_close or session['close'])
 return min(boundary(now,v['scheduled_close_time']),end-60),end

def arm_window(state,now):
 calendar=RegularMarketHours();session=calendar.status(now);v=settings(state)
 if not session.get('next_open') or not session.get('close'):raise ValueError('Verified trading calendar unavailable')
 start=session['next_open'];end=min(session['close'],boundary(start,v['management_stop']))
 if now>=end:
  session=calendar.status(session['close']);start=session['next_open']
  if start is None:raise ValueError('Verified next trading session unavailable')
  end=min(session['close'],boundary(start,v['management_stop']))
 return {'starts_at':start,'expires_at':end,'next_session':now<start}

def policy_block(state,plan,now):
 values=settings(state);side=plan['side'];session=state.get('owner_session')
 if session and (session['boot_id']!=state.get('boot_id') or now>=session['expires_at']):return 'Owner armed session ended'
 if session and now<session.get('starts_at',0):return 'Armed for a future session; trading has not started'
 if 'owner_settings' in state or session:
  close=RegularMarketHours().session(datetime.fromtimestamp(now,EASTERN).date())
  if not close or now>=min(close[1],boundary(now,values['management_stop'])):return 'Management window ended'
 if side=='buy':
  window=close_window(state,now)
  if window and now>=window[0]:return 'Scheduled closing window; new entries paused'
  if state.get('owner_operating_mode')=='manage_only':return 'New entries paused; existing positions remain managed'
  if 'owner_settings' in state or session:
   if not boundary(now,values['entry_start'])<=now<boundary(now,values['entry_stop']):return 'Outside entry window'
  if state.get('owner_policy_versions',{}).get(plan['decision_id'],0)!=revision(state):return 'Settings changed; fresh entry assessment required'
 return None

def take_control(state,pid,now):
 from .order_execution import TERMINAL
 positions=state.get('position_observation',{}).get('positions',[])
 if not any(p['position_id']==pid and p['quantity'] for p in positions):raise ValueError('Position is no longer in the verified book')
 state.setdefault('manual_management_holds',{})[pid]={'active':True,'at':now,'reason':'Owner selected manual management'}
 state.setdefault('owner_targets',{}).pop(pid,None)
 for row in state['orders'].values():
  if (row['plan'].get('position_id') or 'rh-option:'+row['plan']['option_id'])==pid and row['status'] not in TERMINAL:row['stop_reason']='CANCELLED'
 event(state,'MANUAL_CONTROL',now,position_id=pid)

def set_mode(state,mode,now):
 if mode not in ('automatic','manage_only'):raise ValueError('Unknown operating mode')
 from .order_execution import TERMINAL
 state['owner_operating_mode']=mode
 if mode=='manage_only':
  for row in state['orders'].values():
   if row['plan']['side']=='buy' and row['status'] not in TERMINAL:row['stop_reason']='CANCELLED'
 event(state,'OPERATING_MODE',now,mode=mode)
 return {'status':'SAVED','operating_mode':mode}

def view(state,now):
 try:arming=arm_window(state,now)
 except ValueError as exc:arming={'error':str(exc)}
 warning=None;values=settings(state);today=RegularMarketHours().session(datetime.fromtimestamp(now,EASTERN).date())
 if values['scheduled_close_enabled'] and today and now>=min(today[1],boundary(now,values['management_stop'])):
  from .scheduled_close import managed
  if any(managed(state,p) for p in state.get('position_observation',{}).get('positions',[])):
   warning='Managed holdings remain after the closing deadline. Check the broker; no fill is assumed.'
 return {'arming_window':arming,'closing_warning':warning,'scheduled_closes':state.get('scheduled_closes',{}),'values':values,'revision':revision(state),'updated_at':state.get('owner_settings',{}).get('updated_at'),
  'operating_mode':state.get('owner_operating_mode','automatic'),'session':state.get('owner_session'),
  'history':list(reversed(state.get('owner_settings_history',[])[-12:])),
  'activity':list(reversed(state.get('owner_activity',[])[-30:])),
  'targets':state.get('owner_targets',{})}
