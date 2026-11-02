"""One submission path and proposal journal for chat and scheduled agents."""
import asyncio,copy,hashlib
from .controller import canonical
from .order_execution import TERMINAL
from .analysis_view import ANALYSIS_CONTEXT_VERSION

async def submit(ex,req,response,*,source,reasoning='',uncertainty='',missing_information=None,
                 allow_execution=True,proposal=None,metadata=None):
 async with ex.assessment_lock:
  digest=hashlib.sha256(canonical({'response':response,'allow_execution':allow_execution,'proposal':proposal,
    'source':source,'reasoning':reasoning}).encode()).hexdigest()
  state=ex.worker.status();stored=state.get('assessment_receipts',{}).get(req['request_id'])
  if stored:
   if stored['hash']!=digest:raise ValueError('assessment already received with different content')
   return stored['receipt']
  if req.get('retained_receipt_only'):raise ValueError('Expired research context; retention cannot authorize replay')
  # Existing accepted submissions are recovered by respond before stale-mode checks.
  accepted=state.get('decisions',{}).get(req['decision_id'])
  mode=state['mode'];epoch=state.get('control_epoch',0)
  if req['context']['mode']!=mode and not accepted:raise ValueError('owner mode changed; fresh assessment required')
  if response['request_id']!=req['request_id']:raise ValueError('assessment request mismatch')
  if proposal:
   old=req['context'].get('shared_plans',{}).get(proposal['id'])
   if proposal['id']!=req['new_plan_id'] and old is None and proposal['id'] not in req['context']['active_plans']:
    raise ValueError('unknown shared plan reference')
   if old and proposal.get('expected_version')!=old['version']:raise ValueError('shared plan version mismatch')
  from .order_schedule import preview
  draft_cancel=response['action']=='cancel' and response.get('plan_id') not in req['context']['active_plans']
  if accepted or (mode=='LIVE_ARMED' and allow_execution and not draft_cancel):receipt=await ex.respond(response)
  else:
   # Only the host's flat scheduled analyst handoff may use a record-only
   # receipt. Candidate evaluation later obtains its own fresh broker context.
   record_only=(not allow_execution and source.startswith('scheduled:') and
       (metadata or {}).get('analyst_protocol')=='qqq_options_analyst_v1' and
       response['action']=='observe' and not req['context']['positions'] and
       not response.get('position_blocks') and not proposal)
   receipt=await preview(ex,req,response,record_only=True) if record_only else await preview(ex,req,response)
  now=ex.clock();p=response.get('plan');pid=(p or {}).get('plan_id') or response.get('plan_id')
  if proposal:pid=proposal['id']
  note={'analysis_context_version':ANALYSIS_CONTEXT_VERSION,'request_id':req['request_id'],'at':now,
    'source':source,'mode':mode,'action':response['action'],'reasoning':reasoning,'uncertainty':uncertainty,
    'missing_information':missing_information or [],'result':receipt,'plan_id':pid,
    'position_blocks':copy.deepcopy(response.get('position_blocks',[]))}
  if metadata:note.update(copy.deepcopy(metadata))
  def commit(s):
   previous=s.setdefault('assessment_receipts',{}).get(req['request_id'])
   if previous:
    if previous['hash']!=digest:raise ValueError('assessment receipt conflict')
    return previous['receipt']
   if s.get('control_epoch',0)!=epoch or s['mode']!=mode:raise ValueError('owner controls changed before receipt; recover original submission')
   if pid:
    prior=s.setdefault('shared_plans',{}).get(pid)
    expected=req['context'].get('shared_plans',{}).get(pid)
    if (prior or {}).get('version')!=(expected or {}).get('version'):raise ValueError('shared plan changed concurrently')
    actual=s.get('orders',{}).get(pid)
    status=(actual['status'] if actual else 'PROPOSED')
    if receipt.get('status')=='PREVIEW_BLOCKED':status='BLOCKED'
    if proposal and proposal['action']=='watch':status='WATCHING'
    if response['action']=='cancel':status='CANCEL_REQUESTED' if actual and mode=='LIVE_ARMED' and allow_execution else ('CANCEL_PROPOSED' if actual else 'CANCELED')
    if proposal and proposal['action']=='suspend':status='SUSPENDED'
    row={'id':pid,'version':(prior['version'] if prior else 0)+1,'name':(proposal or {}).get('name') or (prior or {}).get('name') or ((p or {}).get('symbol','Sailboat')+' plan '+pid[-6:]),
      'status':status,'source':source,'updated_at':now,'request_id':req['request_id'],
      'plan':copy.deepcopy(p or (prior or {}).get('plan')),'terms':copy.deepcopy((proposal or {}).get('terms',(prior or {}).get('terms',{}))),
      'reasoning':reasoning,'receipt':copy.deepcopy(receipt),'execution_requires_fresh_assessment':True}
    if prior:s.setdefault('shared_plan_history',[]).append(copy.deepcopy(prior))
    s['shared_plans'][pid]=row
   s.setdefault('agent_assessments',[]).append(note);s['agent_assessments']=s['agent_assessments'][-20:]
   s['assessment_receipts'][req['request_id']]={'hash':digest,'receipt':copy.deepcopy(receipt)}
   if req['request_id'] in s.get('live_requests',{}) and not s['live_requests'][req['request_id']]['receipt']:
    s['live_requests'][req['request_id']].update(receipt=copy.deepcopy(receipt),response_hash=hashlib.sha256(canonical(response).encode()).hexdigest())
   return receipt
  return await asyncio.to_thread(ex.journal.edit,commit)
