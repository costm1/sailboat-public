"""Owner-policy candidate -> existing shared controller. No second model or broker.

Live routing requires the existing host gate and owner ARM epoch. Tests use only
the synthetic brokerage; source preference never supplies owner authorization.
"""
import asyncio,copy,hashlib,re
from datetime import date,datetime
from decimal import Decimal
from .controller import canonical
from .market_hours import EASTERN
from .order_execution import TERMINAL
from .shared_chat import contract,compile_intent
from .shared_assessment import submit
from .assembly_bridge import AssemblyBridge,ENTRY_POLICY
from .text_bindings import current_pass,sentences,evidence

POLICY=ENTRY_POLICY

class CandidateEntry:
 def __init__(self,execution,*,allow_execution=False):
  self.ex=execution;self.allow_execution=allow_execution;self.lock=asyncio.Lock();self.assembly=AssemblyBridge(execution)
 async def evaluate(self,key,sorted_text,at,source,*,recovered=False,entry_authority=None):
  async with self.lock:
   digest=hashlib.sha256(canonical({'text':sorted_text['original_text'],'at':at,'source':source}).encode()).hexdigest()
   old=self.ex.worker.status().get('candidate_messages',{}).get(key)
   if old and old['hash']!=digest:raise ValueError('candidate message identity conflict')
   if old and old.get('result') and old['result'].get('status')!='WAITING_ASSEMBLY':return copy.deepcopy(old['result'])
   if old and old.get('prepared'):
    # Same persisted decision identity after a lost acknowledgment; never reprice.
    return await self._deliver(key,old['prepared'])
   result={'policy':POLICY,'source':source,'source_decisions':copy.deepcopy(sorted_text['agent_stated_decisions']),
    'decision_author':'Sailboat owner policy, not the outside agent','live_execution_enabled':self.allow_execution,
    'status':'NO_CANDIDATE','broker_submission':False}
   async def finish(status,reason):
    result.update(status=status,reason=reason)
    await asyncio.to_thread(self.ex.journal.edit,lambda s:s.setdefault('candidate_messages',{}).__setitem__(key,{'hash':digest,'result':copy.deepcopy(result)}))
    return result
   text=sorted_text['original_text'];now=self.ex.clock()
   if sorted_text.get('sending_agent_intent'):
    return await finish('EXPLICIT_ROUTE','Explicit instructions retain the existing owner-authorized route.')
   assembled=await self.assembly.prepare(key,sorted_text,at,source,recovered=recovered)
   result['assembly']=assembled
   if assembled['status']!='READY':return await finish(assembled['status'],assembled['reason'])
   if self.allow_execution and not self._authority_ok(entry_authority):
    return await finish('AUTHORIZATION_CHANGED','Fresh source in the current armed owner epoch and boot required.')
   terms=assembled['terms']
   result.update(candidate=copy.deepcopy(terms),evidence=copy.deepcopy(sorted_text['research_evidence']),
    source_conditions=assembled['market']['conditions'],source_opinion_overridden=bool(current_pass(text)[0]),
    economics=assembled['market']['economics'],
    policy_note='Assembly selects the higher 30-minute resale probability; a tie or winning estimate below the saved owner minimum stays out. Estimates and affordability are not economic proof.')
   try:
    expiry=date.fromisoformat(terms['expiry'])
    if expiry<datetime.fromtimestamp(now,EASTERN).date():return await finish('EXPIRED','The named contract has expired.')
    req=await self.ex.context('Owner-policy candidate evaluation; retain existing limits and permissions.')
    ctx=req['context']
    if any(p['symbol']=='QQQ' and p['quantity'] for p in ctx['positions']):
     return await finish('MANAGE_EXISTING','A QQQ position already exists; give it to the agent for management, not another entry.')
    if any(r['plan']['symbol']=='QQQ' and r['status'] not in TERMINAL for r in ctx['active_plans'].values()):
     return await finish('ENTRY_EXISTS','An existing QQQ order must finish or reconcile first.')
    if any((r.get('plan') or {}).get('symbol')=='QQQ' and r['status'] in ('PROPOSED','WATCHING','WORKING','WAITING')
     and (r.get('plan') or {}).get('expires_at',float('inf'))>now for r in ctx['shared_plans'].values()):
     return await finish('ENTRY_EXISTS','An existing shared QQQ plan needs management rather than duplication.')
    ins,q=await contract(self.ex,terms['symbol'],{'kind':'option',**{k:terms[k] for k in ('right','strike','expiry')}},resolved=assembled['market']['contract'])
    if self.ex.worker.status().get('manual_management_holds',{}).get('rh-option:'+ins['id'],{}).get('active'):
     return await finish('OWNER_REVIEW','An externally cancelled order for this contract awaits owner review; no replacement entry.')
    ask=Decimal(q['ask']);result['quote']=copy.deepcopy(q)
    from .scheduler import timestamp
    session=self.ex.broker.calendar.status(self.ex.clock())
    if not session['open']:return await finish('MARKET_CLOSED','Fresh regular-session evaluation required; no overnight queue.')
    deadline=min(req['reply_before'],session['close'],timestamp(ins['sellout_datetime']),assembled['reference']['expires_at'],now+self.ex.broker.limits['instruction_ttl_seconds'])
    seconds=int(deadline-self.ex.clock())
    if seconds<10:return await finish('TOO_LATE','Insufficient remaining session/contract time for this instruction.')
    # Analyst sizing is capped at the worst permitted repricing cost; absent sizing keeps one.
    from .analyst_preferences import entry_size
    from .entry_repricing import ceiling
    cap=ceiling(ask,ins['min_ticks'])
    from .owner_settings import settings,revision
    owner_state=self.ex.worker.status();rules=settings(owner_state)
    size_limits=dict(self.ex.broker.limits,max_order_notional=min(self.ex.broker.limits['max_order_notional'],rules['position_budget']))
    size_text=text
    if rules['sizing_mode']!='analyst':
     amount=f"{rules['fixed_contracts']} contracts" if rules['sizing_mode']=='contracts' else f"${rules['trade_budget']} budget"
     size_text=f"{terms['symbol']} ${terms['strike']} {terms['right']} expiring {terms['expiry']}. Suggested size: {amount}"
    qty,sizing=entry_size(size_text,terms,at,cap,min(Decimal(ctx['buying_power']),Decimal(str(rules['capital_budget']))),size_limits)
    if rules['sizing_mode']!='analyst':sizing['source']='owner_'+rules['sizing_mode']
    result['sizing']=sizing;cost=ask*100*qty
    limits=self.ex.broker.limits
    if qty>limits['max_order_quantity'] or cost>Decimal(str(limits['max_order_notional'])) or cost>Decimal(ctx['buying_power']):
     return await finish('UNAFFORDABLE','Proposed quantity exceeds buying power or the existing owner limit.')
    intent={'meaning':'instruction','action':'enter','symbol':terms['symbol'],
     'instrument':{'kind':'option',**{k:terms[k] for k in ('right','strike','expiry')}},
     'quantity':qty,'limit_price':float(ask),'valid_for_seconds':seconds,
     'reasoning':'Sailboat owner-policy probability comparison; this is not attributed as an agent BUY recommendation.'}
    reply,proposal,reads=await compile_intent(self.ex,req,intent,broker_facts=(ins,q))
    reply['plan']['limit_prices']=list(dict.fromkeys([str(ask),cap]))
    reply['plan']['replace_after_seconds']=10
    repricing={'original_limit':str(ask),'ceiling':cap,'ticks':copy.deepcopy(ins['min_ticks']),
      'max_increase_percent':5,'max_replacements':1}
    # Compilation must not extend the absolute source deadline by lookup time.
    reply['plan']['expires_at']=min(reply['plan']['expires_at'],deadline)
    if reply['plan']['option_id']!=assembled['market']['contract']['id']:raise ValueError('Assembly broker identity changed')
    # Original quote and the owner-approved ceiling remain separately inspectable.
    result.update(proposed_order=copy.deepcopy(reply['plan']),quantity=qty,limit_price=str(ask),repricing=repricing,
     premium_cost=str(cost),fee_status='Final fee-inclusive affordability is checked by existing broker review before placement.')
    prepared={'req':req,'reply':reply,'proposal':proposal,'source':'candidate:'+source,
      'reasoning':text,'assembly_reference':assembled['reference'],'allow_execution':self.allow_execution,'entry_authority':copy.deepcopy(entry_authority),'result':result}
    def persist(s):
     s.setdefault('candidate_messages',{})[key]={'hash':digest,'prepared':copy.deepcopy(prepared)}
     s.setdefault('assembly_bindings',{})[req['decision_id']]=copy.deepcopy(assembled['reference'])
     s.setdefault('entry_repricing',{})[req['decision_id']]=repricing
     s.setdefault('owner_policy_versions',{})[req['decision_id']]=revision(owner_state)
    await asyncio.to_thread(self.ex.journal.edit,persist)
   except (ValueError,KeyError,TypeError) as exc:
    return await finish('BLOCKED',str(exc)[:300])
   return await self._deliver(key,prepared)
 def _authority_ok(self,a):
  s=self.ex.worker.status()
  return bool(a and a.get('enabled') is True and a.get('mode')==s['mode']=='LIVE_ARMED'
   and a.get('control_epoch')==s.get('control_epoch',0) and a.get('boot_id')==s.get('boot_id')==self.ex.worker.boot_id)
 async def _deliver(self,key,p):
  result=copy.deepcopy(p['result'])
  # A preview saved under DISARMED can never become executable after re-arming.
  state=self.ex.worker.status();accepted=state.get('decisions',{}).get(p['req']['decision_id'])
  allowed=p['allow_execution']
  try:
   if allowed and not accepted and (not self.allow_execution or not self._authority_ok(p.get('entry_authority'))):
    raise ValueError('candidate authorization changed; fresh message required')
   if not accepted:
    ref=p.get('assembly_reference')
    if not ref or not self.assembly.valid_snapshot(ref,self.ex.clock()):raise ValueError('Assembly snapshot invalidated or expired')
    bound=state.get('assembly_bindings',{}).get(p['req']['decision_id'])
    if bound and bound!=ref:raise ValueError('Assembly decision binding changed')
    if not bound:self.ex.journal.edit(lambda s:s.setdefault('assembly_bindings',{}).__setitem__(p['req']['decision_id'],copy.deepcopy(ref)))
   receipt=await submit(self.ex,p['req'],p['reply'],source=p['source'],reasoning=p['reasoning'],
    proposal=p['proposal'],allow_execution=allowed,
    metadata={'candidate_policy':POLICY,'decision_author':'Sailboat owner policy',
     'source_opinion_overridden':result.get('source_opinion_overridden',False),'assembly_reference':copy.deepcopy(p.get('assembly_reference'))})
   result.update(status=receipt['status'],controller_receipt=receipt,
    broker_submission=False,controller_accepted=receipt['status']=='ACCEPTED')
  except ValueError as exc:result.update(status='BLOCKED',reason=str(exc)[:300])
  def commit(s):
   s['candidate_messages'][key].update(result=copy.deepcopy(result))
   s['candidate_messages'][key].pop('prepared',None) # Full request already persists in the shared journal.
  await asyncio.to_thread(self.ex.journal.edit,commit)
  return result
