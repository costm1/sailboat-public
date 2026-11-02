"""Verified SQLite snapshots on local disk; versioned remote CAS commits before acknowledgment."""
import gzip,hashlib,io,json,sqlite3,tempfile,threading,time
from pathlib import Path
from .agent_link import AgentLink
from .controller import canonical

# The transfer cap applies to compressed snapshots. Retention bounds routine research;
# actual trade evidence is permanent. Generation is an identity, never a lifetime quota.
MAX_SNAPSHOT_BYTES=32*1024*1024
MAX_DATABASE_BYTES=16*MAX_SNAPSHOT_BYTES # bounded decoding of a corrupt compressed file
CHECKPOINT_HISTORY_SECONDS=86400
class StorageUnavailable(RuntimeError): pass

def checked_snapshot(path):
    c=sqlite3.connect(path)
    try:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise StorageUnavailable('SQLite integrity failure')
    finally:c.close()
    c=AgentLink(path)
    try:
        if not c.verify_journal():raise StorageUnavailable('journal hash failure')
    finally:c.close()

class HubSnapshots:
    def __init__(self,repo,token=None):
        from huggingface_hub import HfApi
        self.repo=repo;self.api=HfApi(token=token);self.token=token
        self.upload_bytes=0;self.download_bytes=0
        self.snapshot_bytes=0;self.history_compacted_at=time.time();self.baseline=None
    def head(self):return self.api.repo_info(self.repo,repo_type='model').sha
    def _read(self,name,head):
        from huggingface_hub import hf_hub_download
        return Path(hf_hub_download(self.repo,name,revision=head,token=self.token)).read_bytes()
    def load(self,dest):
        head=self.head()
        m=json.loads(self._read('snapshot.json',head))
        compressed=m.get('format')=='sqlite-gzip-v1'
        raw=self._read('state.sqlite.gz' if compressed else 'state.sqlite',head)
        if compressed:
            if len(raw)>MAX_SNAPSHOT_BYTES or hashlib.sha256(raw).hexdigest()!=m['compressed_sha256']:raise StorageUnavailable('compressed snapshot hash/size mismatch')
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:data=f.read(MAX_DATABASE_BYTES+1)
            if len(data)>MAX_DATABASE_BYTES:raise StorageUnavailable('decoded snapshot limit reached')
        else:data=raw
        if hashlib.sha256(data).hexdigest()!=m['sha256']:raise StorageUnavailable('snapshot hash mismatch')
        dest.write_bytes(data);checked_snapshot(dest);self.download_bytes+=len(raw)
        self.snapshot_bytes=len(raw);self.history_compacted_at=m.get('history_compacted_at',time.time())
        # One immutable migration baseline protects all pre-retention evidence, even records
        # written by older versions that did not yet record explicit analysis/order links.
        if not compressed:self.baseline=(gzip.compress(data,mtime=0),canonical(m|{'source_revision':head}).encode())
        return head,m['generation']
    def save(self,path,parent,generation):
        from huggingface_hub import CommitOperationAdd,CommitOperationDelete
        data=path.read_bytes()
        packed=gzip.compress(data,compresslevel=6,mtime=0);now=time.time()
        if len(packed)>MAX_SNAPSHOT_BYTES or len(data)>MAX_DATABASE_BYTES:raise StorageUnavailable('retained trade snapshot storage limit reached')
        rotate=now-self.history_compacted_at>=CHECKPOINT_HISTORY_SECONDS
        manifest={'format':'sqlite-gzip-v1','sha256':hashlib.sha256(data).hexdigest(),'compressed_sha256':hashlib.sha256(packed).hexdigest(),
                  'generation':generation,'created_at':now,'database_bytes':len(data),'snapshot_bytes':len(packed),
                  'history_compacted_at':now if rotate else self.history_compacted_at}
        m=canonical(manifest).encode()
        operations=[CommitOperationAdd(path_in_repo='state.sqlite.gz',path_or_fileobj=packed),
                    CommitOperationAdd(path_in_repo='snapshot.json',path_or_fileobj=m)]
        if self.baseline:
            operations.extend([CommitOperationAdd(path_in_repo='retention-baseline.sqlite.gz',path_or_fileobj=self.baseline[0]),
                               CommitOperationAdd(path_in_repo='retention-baseline.json',path_or_fileobj=self.baseline[1]),
                               CommitOperationDelete(path_in_repo='state.sqlite')])
        result=self.api.create_commit(self.repo,repo_type='model',parent_commit=parent,
          commit_message='Sailboat durable checkpoint '+str(generation),operations=operations)
        head=result.oid
        if rotate:
            # Only obsolete full-snapshot versions are removed. Every retained trade,
            # source analysis and replay receipt is present in the current snapshot.
            if self.head()!=head or self._read('snapshot.json',head)!=m:raise StorageUnavailable('checkpoint rotation writer conflict')
            self.api.super_squash_history(self.repo,repo_type='model',branch='main',commit_message='Roll Sailboat checkpoint history; retain trade evidence')
            head=self.head()
            if self._read('snapshot.json',head)!=m or self._read('state.sqlite.gz',head)!=packed:raise StorageUnavailable('checkpoint rotation verification failed')
            self.history_compacted_at=now
        self.baseline=None;self.snapshot_bytes=len(packed)
        self.upload_bytes+=len(packed)+len(m)
        return head

class CloudLink(AgentLink):
    @staticmethod
    def _state_hash(c):
        # Market polling does not invalidate a request; portfolio/plan changes still do.
        return AgentLink._state_hash({k:v for k,v in c.items() if k!='brokerage_observations'})
    def _context(self):
        c=super()._context()
        c['brokerage_observations']=[]
        if self.source=='external-agent' and self.conversation=='shared-paper':
            exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='broker_observations'").fetchone()
            if exists:c['brokerage_observations']=[json.loads(r[0]) for r in self.db.execute('SELECT value FROM broker_observations ORDER BY name')]
        if getattr(self,'market_only',False):
            c['brokerage_observations']=[o for o in c['brokerage_observations'] if 'quotes' in o and 'result' not in o]
        c['brokerage_positions_are_separate']=True
        return c

class DurableController:
    def __init__(self,store,directory=None):
        self.store=store;self.lock=threading.Lock();self.ready=False
        self.directory=Path(directory or tempfile.mkdtemp(prefix='sailboat-'))
        self.directory.mkdir(parents=True,exist_ok=True);self.db=self.directory/'controller.sqlite'
        if self.db.exists():raise StorageUnavailable('startup requires a fresh local recovery directory')
        self.head,self.generation=store.load(self.db)
        self.boot_id=__import__('uuid').uuid4().hex
        self.calls=0;self.commit_seconds=0.;self.last_error=None;self.last_error_status=None
        # Every new process comes up disarmed; never resume autonomous execution on boot.
        c=AgentLink(self.db)
        try:c.set_mode('DISARMED')
        finally:c.close()
        from .request_archive import migrate_database
        self.archived_on_recovery=migrate_database(self.db,time.time())
        self._checkpoint();self.ready=True
    def _checkpoint(self):
        from .storage_retention import compact
        try:self.retention=compact(self.db)
        except Exception as exc:raise StorageUnavailable('retention failed; no checkpoint acknowledged') from exc
        dest=self.directory/'snapshot.tmp.sqlite'
        if dest.exists():dest.unlink() # private scratch file, never canonical evidence
        src=sqlite3.connect(self.db);out=sqlite3.connect(dest)
        try:src.backup(out)
        finally:out.close();src.close()
        checked_snapshot(dest)
        t=time.perf_counter()
        self.head=self.store.save(dest,self.head,self.generation+1)
        self.commit_seconds+=time.perf_counter()-t;self.generation+=1
    def execute(self,principal,operation,args):
        with self.lock:
            if not self.ready:raise StorageUnavailable('storage unavailable; restart and recover before continuing')
            c=None
            try:
                if operation!='tick' and self.store.head()!=self.head:raise StorageUnavailable('remote checkpoint changed; writer fenced')
                c=CloudLink(self.db,principal['source'],principal['conversation'])
                before=c.db.total_changes
                if operation in ('sailboat_send_message','sailboat_message_receipt'):
                    from .text_channel import exchange
                    result=exchange(c,principal,operation,args)
                elif operation=='sailboat_get_context':result=c.request(args['operator_request'],plan_ttl=3600)
                elif operation=='sailboat_submit_decision':result=c.respond(canonical(args))
                elif operation=='sailboat_get_receipt':result=c.receipt(args['request_id'])
                elif operation=='tick':c.tick();result={'status':'MONITORED'}
                else:raise ValueError('unknown operation')
                if operation not in ('tick','sailboat_message_receipt'):
                    with c.transaction():c._event('CLOUD_MCP_CALL',{'client':principal['label'],'tool':operation,'request_id':args.get('request_id',result.get('request_id'))})
                changed=c.db.total_changes>before
                c.close();c=None
                if changed:self._checkpoint()
                self.calls+=operation!='tick'
                return result
            except ValueError:
                raise
            except Exception as exc:
                # A commit could have reached the Hub despite a lost response. Do not roll it back or acknowledge it.
                self.ready=False;self.last_error=type(exc).__name__;self.last_error_status=getattr(getattr(exc,'response',None),'status_code',None)
                raise StorageUnavailable('durability uncertain; reconnect after owner recovery') from exc
            finally:
                if c is not None:c.close()
    def scheduler_transaction(self, operation):
        """One fenced local transaction and remote checkpoint, before acknowledgment."""
        with self.lock:
            if not self.ready:raise StorageUnavailable('not ready')
            c=None
            try:
                if self.store.head()!=self.head:raise StorageUnavailable('writer fenced')
                c=CloudLink(self.db,'external-agent','shared-paper')
                c.market_only=True
                c.db.execute('CREATE TABLE IF NOT EXISTS scheduler_state(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
                row=c.db.execute('SELECT value FROM scheduler_state WHERE id=1').fetchone()
                state=json.loads(row[0]) if row else {}
                before=c.db.total_changes
                with c.transaction():
                    result=operation(c,state)
                    value=canonical(state)
                    if not row or value!=row[0]:
                        c.db.execute('INSERT INTO scheduler_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',(value,))
                changed=c.db.total_changes>before
                c.close();c=None
                if changed:self._checkpoint()
                return result
            except Exception as exc:
                self.ready=False;self.last_error=type(exc).__name__;self.last_error_status=getattr(getattr(exc,'response',None),'status_code',None)
                raise StorageUnavailable('scheduler checkpoint uncertain; recover before continuing') from exc
            finally:
                if c is not None:c.close()
    def scheduler_view(self):
        with self.lock:
            c=CloudLink(self.db,'external-agent','shared-paper')
            try:
                exists=c.db.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_state'").fetchone()
                row=c.db.execute('SELECT value FROM scheduler_state WHERE id=1').fetchone() if exists else None
                state=json.loads(row[0]) if row else {}
                return state,c._context()
            finally:c.close()
    def execution_view(self):
        """Local, read-only view. All mutations still use fenced transactions."""
        with self.lock:
            if not self.ready:raise StorageUnavailable('not ready')
            c=sqlite3.connect(self.db)
            try:
                c.execute('PRAGMA query_only=ON')
                exists=c.execute("SELECT 1 FROM sqlite_master WHERE name='execution_state'").fetchone()
                row=c.execute('SELECT value FROM execution_state WHERE id=1').fetchone() if exists else None
                return json.loads(row[0]) if row else {'mode':'DISARMED','boot_id':None,'orders':{},'decisions':{}}
            finally:c.close()
    def private_read(self,name):
        with self.lock:
            c=sqlite3.connect(self.db)
            try:
                if not c.execute("SELECT 1 FROM sqlite_master WHERE name='broker_private'").fetchone():return None
                row=c.execute('SELECT value FROM broker_private WHERE name=?',(name,)).fetchone()
                return row[0] if row else None
            finally:c.close()
    def _owner_write(self,table,name,value):
        with self.lock:
            if not self.ready:raise StorageUnavailable('not ready')
            try:
                if self.store.head()!=self.head:raise StorageUnavailable('writer fenced')
                c=sqlite3.connect(self.db)
                try:
                    c.execute('CREATE TABLE IF NOT EXISTS '+table+'(name TEXT PRIMARY KEY,value TEXT)')
                    c.execute('INSERT INTO '+table+' VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',(name,value));c.commit()
                finally:c.close()
                self._checkpoint()
            except Exception as exc:
                self.ready=False;self.last_error=type(exc).__name__;self.last_error_status=getattr(getattr(exc,'response',None),'status_code',None);raise StorageUnavailable('owner write not acknowledged') from exc
    def private_write(self,name,encrypted):self._owner_write('broker_private',name,encrypted)
    def observation_write(self,name,value):self._owner_write('broker_observations',name,canonical(value))
    def monitor(self):
        return self.execute({'source':'external-agent','conversation':'shared-paper','label':'monitor'},'tick',{})
    def live_guard(self):
        with self.lock:
            if not self.ready:return False
            c=sqlite3.connect(self.db)
            try:return c.execute("SELECT v FROM meta WHERE k='estop'").fetchone()[0]=='0'
            finally:c.close()
    def metrics(self):
        import psutil
        p=psutil.Process()
        return {'ready':self.ready,'boot_id':self.boot_id,'generation':self.generation,
          'checkpoint':self.head,'rss_bytes':p.memory_info().rss,'process_cpu_seconds':sum(p.cpu_times()[:2]),
          'database_bytes':self.db.stat().st_size,'max_database_bytes':MAX_DATABASE_BYTES,
          'snapshot_bytes':getattr(self.store,'snapshot_bytes',None),'max_snapshot_bytes':MAX_SNAPSHOT_BYTES,
          'storage_adapter':type(self.store).__name__,'storage_metrics':getattr(self.store,'metrics',{}),
          'archived_requests_on_recovery':self.archived_on_recovery,
          'max_checkpoint_generation':None,'retention':getattr(self,'retention',{}),'uploaded_snapshot_bytes':self.store.upload_bytes,
          'downloaded_snapshot_bytes':self.store.download_bytes,'calls':self.calls,
          'commit_seconds':self.commit_seconds,'last_error':self.last_error,'last_error_status':self.last_error_status}
