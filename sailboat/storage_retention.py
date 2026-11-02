"""Two-day research retention, with permanent trade evidence and replay tombstones.

Runs only under DurableController's existing writer lock. It never evaluates,
replays, cancels or creates a trading instruction. Unknown record shapes stay.
"""
import hashlib,json,time,zlib
from .agent_link import AgentLink
from .controller import canonical

POLICY='two-day-research-preserve-trades-v1'
RESEARCH_SECONDS=2*86400
COMPACT_INTERVAL=3600
PREFIXES=('live-request-','live-decision-','live-plan-','profit-plan-','profit-decision-',
          'scheduled:','chat:','chat-message-','held:','assembly-')
REFERENCE_KEYS={'id','request_id','decision_id','plan_id','new_plan_id','assembly_id','message_id','receipt_id'}
SKIP_KEYS={'context','reasoning','text','original_text','research_evidence','evidence','execution_feedback'}
ROUTINE_EVENTS={'SCHEDULER_RESULT','EXECUTION_STATE','SHAPE_SORTED','ASSEMBLY_CHECKPOINT',
                'ANALYSIS_RECEIVED','AGENT_REQUEST','AGENT_RESPONSE','CLOUD_MCP_CALL','MCP_CALL',
                'SIMULATED_QUOTE','STORAGE_RETENTION'}

def references(value):
    out=set()
    if isinstance(value,dict):
        for key,item in value.items():
            if key in SKIP_KEYS:continue
            if key in REFERENCE_KEYS and isinstance(item,str) and item.startswith(PREFIXES):out.add(item)
            elif isinstance(item,(dict,list)):out.update(references(item))
    elif isinstance(value,list):
        for item in value:out.update(references(item))
    return out

def expand(identities):
    out=set(identities)
    for key in list(out):
        if key.startswith('live-decision-live-request-'):out.add(key[len('live-decision-'):])
        if key.startswith('scheduled:'):out.add(key[len('scheduled:'):])
        if key.startswith('chat:'):out.add(key[len('chat:'):])
        if key.startswith('held:live-request-'):out.add(key.split(':')[1])
    return out

def protection(state,analyses,shapes,assemblies,scheduler):
    # These maps are never pruned. Preserve failed/uncertain attempts as well as fills.
    roots=set();nodes={}
    for name in ('orders','plan_history','decisions','shared_plans','shared_plan_history',
                 'position_theses','position_thesis_history','assembly_bindings','position_block_commits'):
        value=state.get(name,{})
        if isinstance(value,dict):roots.update(k for k in value if k.startswith(PREFIXES))
        roots.update(references(value))
    roots.update(references(scheduler.get('active',{})))
    for key,row in state.get('shape_jobs',{}).items():
        if row.get('status')!='COMPLETE':roots.add(key)
    for key,row in state.get('candidate_messages',{}).items():
        if row.get('prepared'):roots.add(key)
    for key,row in state.get('live_requests',{}).items():
        payload=row.get('payload',{})
        # Includes unsuccessful HOLD/SELL opinions and the assessments preceding a fallback.
        if any(p.get('quantity') for p in payload.get('context',{}).get('positions',[])):roots.add(key)
        nodes[key]=references({k:v for k,v in payload.items() if k!='context'})|references(row.get('receipt'))
    for key,row in analyses.items():
        if row.get('held_position_blocks'):roots.add(key)
        nodes.setdefault(key,set()).update(references(row))
    for mapping in (state.get('candidate_messages',{}),state.get('chat_messages',{}),shapes):
        for key,row in mapping.items():nodes.setdefault(key,set()).update(references(row))
    for key,row in assemblies.items():nodes.setdefault(key,set()).update(references(row))
    for key,group in state.get('assembly_message_groups',{}).items():
        nodes.setdefault(key,set()).add(group)
    # Bidirectional closure binds source -> selected assembly -> controller request -> order.
    edges={}
    for key,refs in nodes.items():
        for ref in expand(refs|{key}):
            edges.setdefault(key,set()).add(ref);edges.setdefault(ref,set()).add(key)
    protected=expand(roots);pending=list(protected)
    while pending:
        for key in edges.get(pending.pop(),()):
            if key not in protected:protected.add(key);pending.append(key)
    return protected

def timestamp(row):
    for key in ('at','message_at','created_at','received_at'):
        value=row.get(key)
        if isinstance(value,(int,float)) and not isinstance(value,bool):return value
    return None

def compact(path,now=None,force=False):
    now=time.time() if now is None else now
    c=AgentLink(path)
    try:
        existing=c.db.execute("SELECT v FROM meta WHERE k='storage_retention'").fetchone()
        prior=json.loads(existing[0]) if existing else {}
        if not force and now-prior.get('compacted_at',0)<COMPACT_INTERVAL:return prior
        if not c.verify_journal():raise ValueError('Retention refused: journal integrity failure')
        tables={r[0] for r in c.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def records(table):
            return {r[0]:json.loads(r[1]) for r in c.db.execute('SELECT id,value FROM '+table)} if table in tables else {}
        state=records('execution_state').get(1,{})
        scheduler=records('scheduler_state').get(1,{})
        analyses=records('received_analyses');shapes=records('shape_results')
        assemblies={r[0]:json.loads(zlib.decompress(r[1])) for r in c.db.execute('SELECT id,view FROM trading_assemblies')} if 'trading_assemblies' in tables else {}
        protected=protection(state,analyses,shapes,assemblies,scheduler)
        cutoff=now-RESEARCH_SECONDS;removed={};trimmed={}
        def old(key,row):
            at=timestamp(row)
            return key not in protected and at is not None and at<cutoff
        with c.transaction():
            for table,rows in (('received_analyses',analyses),('shape_results',shapes),('trading_assemblies',assemblies)):
                keys=[]
                for key,row in rows.items():
                    dated=row if table!='trading_assemblies' else {'at':row.get('spec',{}).get('config',{}).get('started_at')}
                    if old(key,dated):keys.append(key)
                c.db.executemany('DELETE FROM '+table+' WHERE id=?',[(k,) for k in keys]) if table in tables else None
                removed[table]=len(keys)
            for key,row in state.get('live_requests',{}).items():
                p=row.get('payload',{})
                if old(key,p) and not p.get('retained_receipt_only') and not p.get('archived_payload_sha256'):
                    # Keep original expiry, identity, hashes and exact receipt. Old inputs cannot become fresh.
                    row['payload']={k:p[k] for k in ('request_id','context_hash','created_at','reply_before','new_plan_id','decision_id') if k in p}
                    row['payload'].update(context={'mode':p.get('context',{}).get('mode')},retained_receipt_only=True)
                    trimmed['live_requests']=trimmed.get('live_requests',0)+1
            for name in ('received_analyses','shape_recent'):
                rows=state.get(name,{})
                for key in list(rows):
                    if old(key,rows[key]):del rows[key]
            for key,row in state.get('candidate_messages',{}).items():
                job=state.get('shape_jobs',{}).get(key,{})
                if old(key,job) and row.get('result') and not row.get('prepared') and not row.get('retained_receipt_only'):
                    # Terminal identity stays for deduplication; no candidate is reevaluated on replay.
                    row['result']={k:v for k,v in row['result'].items() if k in ('status','reason','controller_receipt','broker_submission','controller_accepted','policy')}
                    row['result']['research_expired']=True;row['retained_receipt_only']=True
                    trimmed['candidate_messages']=trimmed.get('candidate_messages',0)+1
            if 'execution_state' in tables:c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(state),))
            # Retain every trade/owner event. For routine old rows, retain verifiable hash boundaries;
            # never rewrite a retained event or pretend omitted routine bodies still exist.
            c.db.execute('CREATE TABLE IF NOT EXISTS event_retention(first_seq INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,previous_hash TEXT NOT NULL,hash TEXT NOT NULL)')
            events=list(c.db.execute('SELECT * FROM events ORDER BY seq'))
            last=events[-1]['seq'] if events else 0;drop=[]
            for row in events:
                if row['seq']==last or row['ts']>=cutoff or row['kind'] not in ROUTINE_EVENTS:continue
                refs=references(json.loads(row['data'])) if row['kind']!='EXECUTION_STATE' else set()
                if not expand(refs)&protected:drop.append(row)
            gaps=[tuple(r) for r in c.db.execute('SELECT * FROM event_retention')]
            gaps.extend((r['seq'],r['seq'],r['previous_hash'],r['hash']) for r in drop)
            merged=[]
            for gap in sorted(gaps):
                if merged and merged[-1][1]+1==gap[0]:
                    prev=merged[-1]
                    if prev[3]!=gap[2]:raise ValueError('Retention gap hash mismatch')
                    merged[-1]=(prev[0],gap[1],prev[2],gap[3])
                else:merged.append(gap)
            c.db.execute('DELETE FROM event_retention')
            c.db.executemany('INSERT INTO event_retention VALUES(?,?,?,?)',merged)
            c.db.executemany('DELETE FROM events WHERE seq=?',[(r['seq'],) for r in drop])
            removed['routine_events']=len(drop)
            result={'policy':POLICY,'compacted_at':now,'research_retention_seconds':RESEARCH_SECONDS,
                    'cutoff':cutoff,'removed':removed,'trimmed':trimmed,'protected_identities':len(protected),
                    'pruned_journal_rows':sum(b-a+1 for a,b,_,_ in merged),
                    'removed_event_hashes_sha256':hashlib.sha256(canonical([r['hash'] for r in drop]).encode()).hexdigest()}
            c.db.execute("INSERT INTO meta VALUES('storage_retention',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",(canonical(result),))
            c._event('STORAGE_RETENTION',result)
        if not c.verify_journal():raise ValueError('Retention journal verification failed')
        c.db.execute('VACUUM')
        return result
    finally:c.close()
