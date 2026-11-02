"""One durable sorter for chat and scheduled text; candidate routing is host-gated."""
import asyncio,copy,hashlib,json
from .controller import canonical
from .shape_sorter import sort_text,research,VERSION

def enqueue(state,key,text,at,source,intent=None,recovered=False,*,candidate_live_enabled=False,domain_context=None):
 jobs=state.setdefault('shape_jobs',{})
 digest=hashlib.sha256(canonical({'text':text,'intent':intent,'source':source}).encode()).hexdigest()
 old=jobs.get(key)
 if old:
  if old['input_hash']!=digest:raise ValueError('sorter input identity conflict')
  return key
 jobs[key]={'id':key,'text':text,'message_at':at,'source':source,'intent':copy.deepcopy(intent),'input_hash':digest,'status':'PENDING','recovered':recovered,
  'entry_authority':{'enabled':candidate_live_enabled,'mode':state.get('mode'),'control_epoch':state.get('control_epoch',0),'boot_id':state.get('boot_id')}}
 if domain_context:jobs[key]['domain_context']=copy.deepcopy(domain_context)
 return key

class ShapeRuntime:
 def __init__(self,execution,*,candidate_live_enabled=False):
  self.ex=execution;self.lock=asyncio.Lock();self.next_at={}
  from .candidate_entry import CandidateEntry
  self.candidate_entry=CandidateEntry(execution,allow_execution=candidate_live_enabled) # Host-controlled; production remains false.
 def recover(self):
  # One actual prior scheduled message is enough to verify recovery. Never relabel it fresh.
  def op(state):
   previous=[r for r in state.get('agent_assessments',[]) if r.get('source','').startswith('scheduled:') and r.get('reasoning')]
   if previous:
    r=previous[-1];enqueue(state,'scheduled:'+r['request_id'],r['reasoning'],r['at'],r['source'],recovered=True)
  self.ex.journal.edit(op)
 async def once(self):
  async with self.lock:
   state=await asyncio.to_thread(self.ex.worker.status);pending=[r for r in state.get('shape_jobs',{}).values() if r['status']=='PENDING' and max(r.get('next_at',0),self.next_at.get(r['id'],0))<=self.ex.clock()]
   if not pending:return None
   job=min(pending,key=lambda r:r['message_at']);key=job['id']
   # A result archived before a crash is terminal; do not research it a second time.
   def archived(c,s):
    c.db.execute('CREATE TABLE IF NOT EXISTS shape_results(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,value TEXT NOT NULL)')
    row=c.db.execute('SELECT value FROM shape_results WHERE id=?',(key,)).fetchone();return json.loads(row[0]) if row else None
   result=await asyncio.to_thread(self.ex.service.scheduler_transaction,archived)
   if result is None:
    now=self.ex.clock()
    result={'message_id':key,'created_at':now,'message_at':job['message_at'],'source':job['source'],'version':VERSION,'input_hash':job['input_hash'],'recovered':job['recovered'],'broker_submission':False,'live_execution_enabled':self.candidate_entry.allow_execution,'controller_preview':None,'research':[]}
    try:
     result['sorting']=sort_text(job['text'],job['message_at'],job.get('intent'))
     if not -1<=now-job['message_at']<=900:
      result['research_note']='Historical/stale message: meanings preserved; no fresh contract choice or controller proposal inferred.'
     else:
      intent=result['sorting']['sending_agent_intent']
      if intent:
       try:result['research']=await asyncio.wait_for(research(self.ex,result['sorting']),30)
       except TimeoutError:result['research_note']='Read-only research timed out. Missing facts remain unresolved.'
      if intent and not result['sorting']['warnings'] and not any(b['kind']=='volume_condition' for b in result['sorting']['blocks']):
       from .shared_chat import compile_intent
       from .order_schedule import preview
       async def compile_preview():
        req=await self.ex.context('Shadow text sorting only. Do not submit, arm, cancel, change plans or set trading preferences.')
        reply,proposal,reads=await compile_intent(self.ex,req,intent)
        return await preview(self.ex,req,reply)
       try:result['controller_preview']=await asyncio.wait_for(compile_preview(),40)
       except Exception as exc:result['controller_preview']={'status':'NEEDS_CLARIFICATION','reason':str(exc)[:300]}
     assembly_input=copy.deepcopy(result['sorting'])
     if job.get('domain_context'):assembly_input['trading_domain_context']=copy.deepcopy(job['domain_context'])
     result['candidate_entry']=await self.candidate_entry.evaluate(key,assembly_input,job['message_at'],job['source'],recovered=job['recovered'],entry_authority=job.get('entry_authority'))
     if not result['sorting']['sending_agent_intent']:
      assembly=result['candidate_entry'].get('assembly') or {}
      markets=[assembly['market']] if assembly.get('market') else assembly.get('market_evaluations',[])
      result['research']=[{'supplied_terms':m['terms'],'status':'RESOLVED' if m.get('valid') else 'UNAVAILABLE',
       'contract':m.get('contract'),'quote':m.get('quote'),'reason':m.get('reason')} for m in markets]
      if not markets and result['sorting']['candidate_disposition']!='NONE' and not job['recovered'] and -1<=now-job['message_at']<=900:
       result['research']=[{'supplied_terms':b['terms'],'status':'UNAVAILABLE','reason':'Assembly has no selected candidate requiring broker validation.'}
        for b in result['sorting']['blocks'] if b['kind']=='contract_reference' and not b['missing']]
      result['research_note']='Broker evidence from Assembly validation of the selected candidate; source claims remain in research_evidence.'
     if result['candidate_entry']['status']=='WAITING_ASSEMBLY':
      # Poll timing is not execution authority. Keep this backoff in memory;
      # preserve the first retry time across restart, not a new durable timer
      # every five seconds. Source age and owner epoch still gate every retry.
      self.next_at[key]=self.ex.clock()+5
      def waiting(s):
       s['shape_jobs'][key].setdefault('next_at',self.next_at[key])
       s['shape_jobs'][key]['assembly_status']='WAITING_ASSEMBLY'
      await asyncio.to_thread(self.ex.journal.edit,waiting)
      result['status']='WAITING_ASSEMBLY'
      return result
     result['status']=result['sorting']['status']
    except Exception as exc:
     from .cloud_store import StorageUnavailable
     if isinstance(exc,StorageUnavailable):raise
     pending=self.ex.worker.status().get('candidate_messages',{}).get(key,{})
     if pending.get('prepared') and not pending.get('result'):raise # Recover the SAME durable decision, never abandon a lost acknowledgment.
     result.update(status='FAILED',error=type(exc).__name__+': '+str(exc)[:250])
   def finish(c,s):
    c.db.execute('CREATE TABLE IF NOT EXISTS shape_results(id TEXT PRIMARY KEY,input_hash TEXT NOT NULL,value TEXT NOT NULL)')
    c.db.execute('INSERT OR IGNORE INTO shape_results VALUES(?,?,?)',(key,job['input_hash'],canonical(result)))
    row=c.db.execute('SELECT input_hash,value FROM shape_results WHERE id=?',(key,)).fetchone()
    if row[0]!=job['input_hash']:raise ValueError('sorter archive identity conflict')
    saved=json.loads(row[1]);state=json.loads(c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()[0])
    state['shape_jobs'][key].update(status='COMPLETE',finished_at=self.ex.clock())
    # Completed job bodies are preserved in the archive, not repeated in hot state.
    state['shape_jobs'][key].pop('text',None);state['shape_jobs'][key].pop('intent',None)
    state.setdefault('shape_recent',{})[key]=saved
    keep=sorted(state['shape_recent'],key=lambda k:state['shape_recent'][k]['created_at'],reverse=True)[:6]
    state['shape_recent']={k:state['shape_recent'][k] for k in keep}
    c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(state),))
    c._event('SHAPE_SORTED',{'id':key,'status':saved['status'],'broker_submission':False})
    return saved
   saved=await asyncio.to_thread(self.ex.service.scheduler_transaction,finish);self.next_at.pop(key,None);return saved
 async def run(self):
  recovered=False
  while True:
   try:
    if not recovered:self.recover();recovered=True
    await self.once()
   except asyncio.CancelledError:raise
   except Exception:
    # Durability/transport failure leaves the pending item for recovery. No trade fallback.
    await asyncio.sleep(5)
   await asyncio.sleep(1)
