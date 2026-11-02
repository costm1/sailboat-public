"""Observe complete broker books; no inference about who traded and no order transport."""
import copy
from .controller import canonical

def reconcile_lifecycles(state,positions,now):
    """Scope managed profit state to a holding period, not a reusable contract ID."""
    from .order_execution import TERMINAL
    from .scheduler import timestamp
    epochs=state.setdefault('position_management_epochs',{})
    current={p['position_id']:p for p in positions}
    changed=False
    for pid in set(epochs)|set(state.get('adaptive_profit',{}))|set(current):
        p=current.get(pid);old=epochs.get(pid)
        if p is None:
            reset=old is None or not old.get('closed')
            epoch={'closed':True,'since':now,'opened_at':old.get('opened_at') if old else None}
        else:
            opened=p.get('opened_at')
            reset=bool(old and (old.get('closed') or old.get('opened_at')!=opened))
            # Upgrade legacy state conservatively when its managed order belongs
            # to an earlier opening of this exact contract.
            managed=state.get('adaptive_profit',{}).get(pid,{})
            plan=state.get('orders',{}).get(managed.get('plan_id'),{}).get('plan',{})
            if old is None and opened and plan and plan['created_at']<timestamp(opened):reset=True
            epoch={'closed':False,'since':now,'opened_at':opened}
            if old is None and not reset and opened:epoch['since']=min(now,timestamp(opened))
        if reset:
            prior={'position_id':pid,'closed_or_reopened_at':now,
                'epoch':copy.deepcopy(old),'adaptive_profit':state.get('adaptive_profit',{}).pop(pid,None),
                'position_thesis':state.get('position_theses',{}).pop(pid,None)}
            state['position_management_history']=(state.get('position_management_history',[])+[prior])[-40:]
            # Fence obsolete managed orders immediately. The existing worker
            # performs cancellation and reconciles fills; no cancellation assumed.
            for row in state.get('orders',{}).values():
                plan=row['plan']
                if (plan.get('position_id')==pid and plan['side']=='sell'
                    and plan['decision_id'] in state.get('profit_exit_guards',{}) and row['status'] not in TERMINAL):
                    row['stop_reason']='CANCELLED'
        if reset or old is None:
            epochs[pid]=epoch;changed=True
    return changed

def record_book(state,view):
    positions=[{k:p.get(k) for k in ('position_id','id','symbol','kind','quantity','unit_cost','opened_at','expiration_date','mark_bid')} for p in view['positions']]
    signature=canonical(sorted([(p['position_id'],p['quantity'],p['unit_cost'],p['opened_at']) for p in positions]))
    prior=state.get('position_observation');version=prior.get('version',0) if prior else 0
    changed=prior is not None and prior['signature']!=signature
    lifecycle_changed=reconcile_lifecycles(state,positions,view['observed_at'])
    if prior is not None and not changed:return lifecycle_changed
    if changed:
        version+=1
        event={'version':version,'at':view['observed_at'],'before':prior['positions'],'after':positions,
               'reason':'Broker-confirmed holding changed; refresh analysis against quantity and average cost'}
        state['position_changes']=(state.get('position_changes',[])+[event])[-20:]
    state['position_observation']={'signature':signature,'version':version,'observed_at':view['observed_at'],
        'positions':copy.deepcopy(positions),'buying_power':view['buying_power']}
    return changed

def resume_management(state,pid,now):
    row=state.get('manual_management_holds',{}).get(pid)
    if not row or not row.get('active'):raise ValueError('No manual-intervention hold for that position')
    row.update(active=False,resumed_at=now)
    # Resuming never resurrects a stale sell thesis. A new assessment is required.
    state.setdefault('management_resume_after',{})[pid]=now
    from .owner_settings import event
    event(state,'MANAGEMENT_RESUMED',now,position_id=pid)

def management_block(state,pid):
    row=state.get('manual_management_holds',{}).get(pid,{})
    if row.get('active'):return 'Broker cancellation observed; owner must resume management'
    after=state.get('management_resume_after',{}).get(pid)
    thesis=state.get('position_theses',{}).get(pid,{})
    if after is not None and thesis.get('updated_at',0)<=after:return 'Awaiting fresh assessment after owner resumed management'
    return None

def order_block(state,plan,now=None):
    """Final worker fence as well as proposal-time checking; never replay pre-resume work."""
    pid=plan.get('position_id') or 'rh-option:'+plan['option_id']
    if now is not None:
        from .owner_settings import policy_block
        blocked=policy_block(state,plan,now)
        if blocked:return blocked
    manual_exit=state.get('owner_exit_authorizations',{}).get(plan['decision_id'])
    scheduled=state.get('scheduled_close_authorizations',{}).get(plan['decision_id'])
    if scheduled:
        from .owner_settings import close_policy
        if scheduled['plan']!=plan or scheduled['policy']!=close_policy(state):return 'Scheduled close settings changed'
        if scheduled['boot_id']!=state.get('boot_id') or scheduled['control_epoch']!=state.get('control_epoch',0):return 'Scheduled close authority changed'
        if state.get('manual_management_holds',{}).get(pid,{}).get('active'):return 'Owner took manual control'
        if scheduled['position_epoch']!=state.get('position_management_epochs',{}).get(pid):return 'Holding changed since scheduled close'
        return None
    if manual_exit and manual_exit['plan']==plan and manual_exit['boot_id']==state.get('boot_id'):
        if manual_exit['position_epoch']==state.get('position_management_epochs',{}).get(pid):return None
        return 'Holding changed since owner exit confirmation'
    guard=state.get('profit_exit_guards',{}).get(plan['decision_id'],{})
    if 'owner_settings_revision' in guard and guard['owner_settings_revision']!=state.get('owner_settings',{}).get('revision',0):return 'Fallback rules changed'
    target=state.get('owner_targets',{}).get(pid,{})
    if 'owner_target_version' in guard and guard['owner_target_version']!=target.get('target_id',target.get('at')):return 'Owner target changed'
    if 'position_epoch' in guard:
        epoch=state.get('position_management_epochs',{}).get(pid)
        if epoch!=guard['position_epoch'] or epoch.get('closed'):
            return 'Holding lifecycle changed; stale managed exit'
    if state.get('manual_management_holds',{}).get(pid,{}).get('active'):
        return 'Owner intervention paused this contract'
    after=state.get('management_resume_after',{}).get(pid)
    if after is not None and plan['created_at']<=after:
        return 'Order predates owner resume; fresh instruction required'
    if plan['side']=='sell':return management_block(state,pid)
    return None
