"""Lossless cold storage for completed request payloads in the SAME checkpoint.

Receipts, response hashes and identities stay in hot state. No execution history
is deleted. Unanswered, unexpired and recently completed requests stay hot.
"""
import hashlib,json,zlib
from .controller import canonical

MARKER='archived_payload_sha256'

def migrate_database(path,now):
    """One-time lossless repack before startup's fenced checkpoint.

    Only the private recovered database changes until the remote CAS succeeds.
    VACUUM removes unused physical pages, not logical rows or evidence.
    """
    import sqlite3
    with sqlite3.connect(path) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_request_payloads'").fetchone():return 0
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_state'").fetchone():return 0
        row=db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
        if not row:return 0
        state=json.loads(row[0]);count=archive_completed(db,state,now-120)
        if count:db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(state),))
    if count:
        with sqlite3.connect(path) as db:db.execute('VACUUM')
    return count

def archive_completed(db,state,before):
    count=0
    for key,row in state.get('live_requests',{}).items():
        p=row.get('payload') or {}
        if not row.get('receipt') or MARKER in p or p.get('retained_receipt_only'):continue
        if not isinstance(p.get('reply_before'),(int,float)) or p['reply_before']>=before:continue
        raw=canonical(p).encode();sha=hashlib.sha256(raw).hexdigest()
        db.execute('CREATE TABLE IF NOT EXISTS live_request_payloads(id TEXT PRIMARY KEY,sha256 TEXT NOT NULL,payload BLOB NOT NULL)')
        previous=db.execute('SELECT sha256,payload FROM live_request_payloads WHERE id=?',(key,)).fetchone()
        if previous:
            if previous[0]!=sha or zlib.decompress(previous[1])!=raw:raise ValueError('Archived request identity conflict')
        else:db.execute('INSERT INTO live_request_payloads VALUES(?,?,?)',(key,sha,zlib.compress(raw)))
        summary={k:p[k] for k in ('request_id','context_hash','created_at','reply_before','new_plan_id','decision_id') if k in p}
        # Retention protection still sees holding-related request identities.
        summary['context']={'mode':p.get('context',{}).get('mode'),
            'positions':[{k:v for k,v in x.items() if k in ('position_id','id','symbol','quantity')} for x in p.get('context',{}).get('positions',[])]}
        summary[MARKER]=sha;row['payload']=summary;count+=1
    return count

def payload(service,request_id,summary):
    if MARKER not in summary:return summary
    import sqlite3
    with service.lock:
        if not service.ready:raise ValueError('Controller storage unavailable')
        db=sqlite3.connect(service.db)
        try:
            db.execute('PRAGMA query_only=ON')
            row=db.execute('SELECT sha256,payload FROM live_request_payloads WHERE id=?',(request_id,)).fetchone()
        finally:db.close()
    if not row or row[0]!=summary[MARKER]:raise ValueError('Archived request identity unavailable')
    raw=zlib.decompress(row[1])
    if hashlib.sha256(raw).hexdigest()!=row[0]:raise ValueError('Archived request integrity failure')
    restored=json.loads(raw)
    if restored['request_id']!=request_id:raise ValueError('Archived request identity mismatch')
    return restored

def export_legacy_database(source,destination):
    """Offline rollback preparation only. Never writes to the source or remote head."""
    import sqlite3
    from pathlib import Path
    from .cloud_store import checked_snapshot
    if Path(destination).exists():raise FileExistsError(destination)
    src=sqlite3.connect('file:'+Path(source).resolve().as_posix()+'?mode=ro',uri=True)
    out=sqlite3.connect(destination)
    try:
        src.backup(out)
        row=out.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
        if row:
            state=json.loads(row[0])
            for rid,item in state.get('live_requests',{}).items():
                summary=item.get('payload') or {}
                if MARKER not in summary:continue
                stored=out.execute('SELECT sha256,payload FROM live_request_payloads WHERE id=?',(rid,)).fetchone()
                if not stored or stored[0]!=summary[MARKER]:raise ValueError('Archived request identity unavailable')
                raw=zlib.decompress(stored[1])
                if hashlib.sha256(raw).hexdigest()!=stored[0]:raise ValueError('Archived request integrity failure')
                restored=json.loads(raw)
                if restored['request_id']!=rid:raise ValueError('Archived request identity mismatch')
                item['payload']=restored
            out.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(state),));out.commit()
        out.execute('VACUUM')
    finally:out.close();src.close()
    checked_snapshot(destination)
