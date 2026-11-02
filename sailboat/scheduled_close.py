"""Owner-enabled scheduled limit exits, exclusively through the existing worker."""
import asyncio,copy
from . import owner_settings as policy
from .order_execution import TERMINAL

def managed(state,p):
 if p.get('kind')!='option' or state.get('manual_management_holds',{}).get(p['position_id'],{}).get('active'):return False
 return (p.get('symbol')=='QQQ' or p['position_id'] in state.get('position_theses',{})
         or p['position_id'] in state.get('owner_targets',{}))

class ScheduledClose:
 def __init__(self,ex):self.ex=ex;self.bounds={}
 async def tick(self,view):
  ex=self.ex;now=ex.clock();state=await asyncio.to_thread(ex.worker.status);closing=set()
  if not ex.worker._armed() or not policy.close_window(state,now):return closing
  if state.get('owner_session') and now<state['owner_session'].get('starts_at',0):return closing
  for p in view['positions']:
   pid=p['position_id']
   if not managed(state,p):continue
   if now>=policy.close_window(state,now)[0]:closing.add(pid)
   try:
    cached=self.bounds.get(p['id'])
    if not cached or now-cached[0]>=60:
     details=await ex.profit_manager.port.exit_details(p,state,{})
     cached=(now,details['close_at']);self.bounds[p['id']]=cached
    starts,ends=policy.close_window(state,now,cached[1])
    if now<starts:continue
    closing.add(pid)
    await self.close(p,view,ends)
   except Exception as exc:
    # Errors remain visible; ordinary profit management must not compete with a due close.
    if pid in closing:
     await asyncio.to_thread(ex.journal.edit,lambda s:s.setdefault('scheduled_closes',{}).setdefault(pid,{}).update(status='BLOCKED',last_error=str(exc)[:250],checked_at=ex.clock()))
  return closing
 async def close(self,p,view,ends):
  ex=self.ex;state=await asyncio.to_thread(ex.worker.status);pid=p['position_id']
  if not managed(state,p):return
  previous=state.get('scheduled_closes',{}).get(pid,{})
  prior=state['orders'].get(previous.get('plan_id'),{})
  if prior.get('status') in {'NEEDS_OWNER','REJECTED','STOPPED'}:raise ValueError('Previous scheduled exit needs owner review; no automatic replacement')
  active=[r for r in state['orders'].values() if r['plan']['option_id']==p['id'] and r['status'] not in TERMINAL]
  if active:
   for row in active:
    if row['plan']['decision_id'] not in state.get('scheduled_close_authorizations',{}):
     await asyncio.to_thread(ex.worker.cancel,row['plan']['plan_id'])
   return
  if ex.clock()>=ends-5:raise ValueError('Closing deadline reached; holding may remain open')
  # Shared quote/contract preparation performs no broker writes and creates no order.
  from .owner_controls import handle
  summary=await handle(ex,None,{'command':'preview_close','position_id':pid})
  state=await asyncio.to_thread(ex.worker.status);preview=state['owner_close_previews'][summary['token']]
  plan=copy.deepcopy(preview['plan']);plan['quantity']=min(plan['quantity'],ex.broker.limits['max_order_quantity'])
  plan['expires_at']=min(plan['expires_at'],ends)
  authorization={'plan':plan,'policy':policy.close_policy(state),'boot_id':preview['boot_id'],
   'control_epoch':preview['control_epoch'],'position_epoch':preview['position_epoch']}
  def accept(s):
   from .position_updates import order_block
   if not managed(s,p) or authorization['policy']!=policy.close_policy(s):raise ValueError('Scheduled close settings or ownership changed')
   if not policy.close_window(s,ex.clock()) or ex.clock()<policy.close_window(s,ex.clock(),ends)[0]:raise ValueError('Scheduled close window changed')
   s.setdefault('scheduled_close_authorizations',{})[plan['decision_id']]=authorization
   blocked=order_block(s,plan,ex.clock())
   if blocked:raise ValueError(blocked)
   s.setdefault('scheduled_closes',{})[pid]={'plan_id':plan['plan_id'],'status':'ACCEPTED','at':ex.clock(),'limit_price':plan['limit_prices'][0],'quantity':plan['quantity'],'last_error':None}
   policy.event(s,'SCHEDULED_CLOSE_ACCEPTED',ex.clock(),position_id=pid,plan_id=plan['plan_id'])
  await asyncio.to_thread(ex.worker.accept,plan,state_update=accept)
