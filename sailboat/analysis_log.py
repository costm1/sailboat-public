"""Durable received prose, separate from controller acceptance. Never executes it."""
import copy,json
from .controller import canonical
from .analysis_evidence import extract_research


def receive(c,st,request,assessment,provider,usage,now):
 rid=request['request_id']
 row={'request_id':rid,'at':now,'context_observed_at':request['context']['observed_at'],
  'source':'scheduled:'+provider,'analyst_protocol':'qqq_options_analyst_v1',
  'reasoning':assessment['analysis'],'uncertainty':assessment['uncertainty'],
  'missing_information':assessment['missing_information'],
  'research_evidence':extract_research(assessment['analysis'],now),
  'result':{'request_id':rid,'status':'RECEIVED_PROCESSING','received_at':now}}
 c.db.execute('CREATE TABLE IF NOT EXISTS received_analyses(id TEXT PRIMARY KEY,value TEXT NOT NULL)')
 old=c.db.execute('SELECT value FROM received_analyses WHERE id=?',(rid,)).fetchone()
 if old:
  saved=json.loads(old[0])
  if any(saved[k]!=row[k] for k in ('reasoning','uncertainty','missing_information')):raise ValueError('received analysis identity conflict')
  row=saved
 else:
  c.db.execute('INSERT INTO received_analyses VALUES(?,?)',(rid,canonical(row)))
  c._event('ANALYSIS_RECEIVED',{'request_id':rid,'received_at':now})
 execution=json.loads(c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()[0])
 execution.setdefault('received_analyses',{})[rid]=copy.deepcopy(row)
 keep=sorted(execution['received_analyses'],key=lambda k:execution['received_analyses'][k]['at'],reverse=True)[:20]
 execution['received_analyses']={k:execution['received_analyses'][k] for k in keep}
 c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(execution),))
 st['active'].update(analysis_received=True,phase='RESPONSE_READY',assessment=copy.deepcopy(assessment),usage=copy.deepcopy(usage))


def finish(c,active,result):
 """Archive both success and rejection; a receipt here never grants execution."""
 if not active.get('analysis_received'):return
 rid=active['request_id']
 row=c.db.execute('SELECT value FROM received_analyses WHERE id=?',(rid,)).fetchone()
 if not row:return
 note=json.loads(row[0]);receipt=result.get('receipt')
 note['result']=copy.deepcopy(receipt) if receipt else {'request_id':rid,'status':'PROCESSING_FAILED',
  'received_at':note['at'],'blocker':result.get('detail') or result.get('reason') or result['status'],
  'execution_outcome':'CONSULT_CONTROLLER_RECEIPT'}
 note['processing_result']={k:result[k] for k in ('status','error','detail','reason','finished_at') if k in result}
 note['held_position_blocks']=copy.deepcopy((active.get('assessment') or {}).get('held_position_blocks',[]))
 c.db.execute('UPDATE received_analyses SET value=? WHERE id=?',(canonical(note),rid))
 execution=json.loads(c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()[0])
 execution.setdefault('received_analyses',{})[rid]=note
 c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(execution),))
