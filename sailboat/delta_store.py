"""Staged v2 storage: immutable SQLite page deltas + one conditional commit head.

The existing state.snapshot key is the CAS head, including during migration.
An interrupted upload is not committed until that head changes. Recovery uses
one full base and at most 32 deltas. All previous commit objects remain intact.
"""
import gzip,hashlib,io,json,struct
from botocore.exceptions import ClientError
from .bucket_store import BucketSnapshots,MAGIC as LEGACY_MAGIC
from .cloud_store import StorageUnavailable,checked_snapshot,MAX_DATABASE_BYTES,MAX_SNAPSHOT_BYTES
from .controller import canonical

HEAD_MAGIC=b'SAILBOAT-DELTA-HEAD-1\n'
PATCH_MAGIC=b'SAILBOAT-PAGE-DELTA-1\n'
MAX_CHAIN=32
MAX_HEAD_BYTES=64*1024
MAX_PATCH_HEADER=1024*1024
sha=lambda data:hashlib.sha256(data).hexdigest()

def page_size(data):
    if not data.startswith(b'SQLite format 3\0') or len(data)<100:raise StorageUnavailable('invalid SQLite page image')
    size=int.from_bytes(data[16:18],'big');size=65536 if size==1 else size
    if size<512 or size>65536 or size&(size-1) or len(data)%size:raise StorageUnavailable('invalid SQLite page size')
    if len(data)>MAX_DATABASE_BYTES:raise StorageUnavailable('database limit reached')
    return size

def encode_delta(before,after,generation):
    size=page_size(after)
    if page_size(before)!=size:raise StorageUnavailable('page size changed; full checkpoint required')
    indices=[i for i in range(len(after)//size) if before[i*size:(i+1)*size]!=after[i*size:(i+1)*size]]
    header={'format':'sqlite-pages-v1','previous_sha256':sha(before),'sha256':sha(after),
        'generation':generation,'page_size':size,'database_bytes':len(after),'pages':indices}
    raw=canonical(header).encode()
    if len(raw)>MAX_PATCH_HEADER:raise StorageUnavailable('page delta header limit')
    packed=gzip.compress(b''.join(after[i*size:(i+1)*size] for i in indices),compresslevel=1,mtime=0)
    return PATCH_MAGIC+struct.pack('>I',len(raw))+raw+packed

def apply_delta(before,body,generation):
    if not body.startswith(PATCH_MAGIC) or len(body)>MAX_SNAPSHOT_BYTES:raise StorageUnavailable('invalid page delta')
    offset=len(PATCH_MAGIC)
    if len(body)<offset+4:raise StorageUnavailable('truncated delta header')
    n=int.from_bytes(body[offset:offset+4],'big');offset+=4
    if not 0<n<=MAX_PATCH_HEADER or offset+n>len(body):raise StorageUnavailable('delta header limit')
    h=json.loads(body[offset:offset+n]);offset+=n
    size=page_size(before);length=h['database_bytes'];indices=h['pages']
    if h['format']!='sqlite-pages-v1' or h['generation']!=generation or h['previous_sha256']!=sha(before):raise StorageUnavailable('delta ancestry mismatch')
    if h['page_size']!=size or type(length) is not int or not size<=length<=MAX_DATABASE_BYTES or length%size:raise StorageUnavailable('invalid target size')
    if not isinstance(indices,list) or any(type(i) is not int or not 0<=i<length//size for i in indices) or indices!=sorted(set(indices)):raise StorageUnavailable('invalid page index')
    expected=len(indices)*size
    with gzip.GzipFile(fileobj=io.BytesIO(body[offset:])) as stream:patch=stream.read(expected+1)
    if len(patch)!=expected:raise StorageUnavailable('page data size mismatch')
    result=bytearray(before[:length]);result.extend(b'\0'*(length-len(result)))
    for pos,index in enumerate(indices):result[index*size:(index+1)*size]=patch[pos*size:(pos+1)*size]
    result=bytes(result)
    if sha(result)!=h['sha256']:raise StorageUnavailable('replayed database hash mismatch')
    return result

class DeltaBucketSnapshots(BucketSnapshots):
    """Incremental durable checkpoints with bounded recovery and CAS fencing."""
    def __init__(self,*args,max_chain=MAX_CHAIN,**kwargs):
        super().__init__(*args,**kwargs)
        if type(max_chain) is not int or not 1<=max_chain<=MAX_CHAIN:raise ValueError('invalid recovery chain bound')
        self.max_chain=max_chain;self.prefix=self.key+'.ledger-v1/'
        self._raw=None;self._manifest=None;self._legacy_body=None;self._etag=None;self._generation=None
        self.metrics={'delta_commits':0,'full_checkpoints':0,'head_bytes':0,'blob_bytes':0,'orphan_uploads_possible':False}
    def _get(self,key,limit):
        response=self.client.get_object(Bucket=self.bucket,Key=key);stream=response['Body']
        try:body=stream.read(limit+1)
        finally:stream.close()
        if len(body)>limit:raise StorageUnavailable('stored object exceeds bound')
        self.download_bytes+=len(body)
        return body,response['ETag']
    def _immutable(self,body,kind):
        key=self.prefix+kind+'/'+sha(body)
        try:self.client.put_object(Bucket=self.bucket,Key=key,Body=body,ContentType='application/octet-stream',IfNoneMatch='*')
        except ClientError as exc:
            if exc.response.get('ResponseMetadata',{}).get('HTTPStatusCode')!=412:raise
            existing,_=self._get(key,len(body))
            if existing!=body:raise StorageUnavailable('immutable object identity conflict')
        self.upload_bytes+=len(body);self.metrics['blob_bytes']+=len(body)
        return {'key':key,'sha256':sha(body),'bytes':len(body)}
    def _read_ref(self,ref,kind):
        if set(ref)!={'key','sha256','bytes'} or ref['key']!=self.prefix+kind+'/'+ref['sha256']:raise StorageUnavailable('invalid object reference')
        if type(ref['bytes']) is not int or not 0<ref['bytes']<=MAX_SNAPSHOT_BYTES+4096:raise StorageUnavailable('object reference size limit')
        data,_=self._get(ref['key'],ref['bytes'])
        if len(data)!=ref['bytes'] or sha(data)!=ref['sha256']:raise StorageUnavailable('immutable object hash mismatch')
        return data
    def load(self,dest):
        body,etag=self._get(self.key,MAX_SNAPSHOT_BYTES+4096)
        if body.startswith(LEGACY_MAGIC):
            raw,m=self.decode(body);generation=m['generation'];manifest=None;legacy=body
            self.snapshot_bytes=m['snapshot_bytes']
        else:
            if not body.startswith(HEAD_MAGIC) or len(body)>MAX_HEAD_BYTES:raise StorageUnavailable('unknown commit head')
            manifest=json.loads(body[len(HEAD_MAGIC):]);legacy=None
            if manifest['format']!='sqlite-delta-chain-v1' or len(manifest['deltas'])>MAX_CHAIN:raise StorageUnavailable('invalid recovery chain')
            if type(manifest['generation']) is not int:raise StorageUnavailable('invalid generation')
            total=sum(r['bytes'] for r in [manifest['base']]+manifest['deltas'])
            if total>MAX_SNAPSHOT_BYTES:raise StorageUnavailable('recovery chain exceeds compressed limit')
            raw,m=self.decode(self._read_ref(manifest['base'],'bases'));generation=m['generation']
            for ref in manifest['deltas']:
                generation+=1;raw=apply_delta(raw,self._read_ref(ref,'deltas'),generation)
            if generation!=manifest['generation'] or sha(raw)!=manifest['database_sha256']:raise StorageUnavailable('commit head database mismatch')
            self.snapshot_bytes=total
        dest.write_bytes(raw);checked_snapshot(dest)
        self._raw=raw;self._manifest=manifest;self._legacy_body=legacy;self._etag=etag;self._generation=generation
        return etag,generation
    def save(self,path,parent,generation):
        if self._raw is None or parent!=self._etag or generation!=self._generation+1:raise StorageUnavailable('save requires exact acknowledged parent')
        raw=path.read_bytes();page_size(raw)
        # All artifacts are immutable; only this final head can make them active.
        # No write is retried after a lost response, including the final CAS.
        previous=self._manifest
        if previous is None:
            # Retain and link the exact legacy checkpoint even if migration
            # immediately needs a new full base (for example after VACUUM).
            legacy_base=self._immutable(self._legacy_body,'bases')
            previous={'format':'sqlite-delta-chain-v1','generation':self._generation,
                'database_sha256':sha(self._raw),'base':legacy_base,'deltas':[],'parent_commit':None}
            self._immutable(HEAD_MAGIC+canonical(previous).encode(),'commits')
        parent_commit=(self.prefix+'commits/'+sha(HEAD_MAGIC+canonical(previous).encode())) if previous else None
        chain=list(previous['deltas']) if previous else []
        base=previous['base'] if previous else None
        rollover=len(chain)>=self.max_chain or page_size(raw)!=page_size(self._raw)
        patch=None if rollover else encode_delta(self._raw,raw,generation)
        baseline_bytes=base['bytes'] if base else len(self._legacy_body)
        if patch is not None and baseline_bytes+sum(r['bytes'] for r in chain)+len(patch)>MAX_SNAPSHOT_BYTES:rollover=True
        if rollover:
            body,_=self.encode(raw,generation)
            if len(body)>MAX_SNAPSHOT_BYTES:raise StorageUnavailable('full recovery base exceeds limit')
            base=self._immutable(body,'bases');chain=[];kind='full_checkpoints'
        else:
            if base is None:base=self._immutable(self._legacy_body,'bases')
            chain.append(self._immutable(patch,'deltas'));kind='delta_commits'
        manifest={'format':'sqlite-delta-chain-v1','generation':generation,'database_sha256':sha(raw),
            'base':base,'deltas':chain,'parent_commit':parent_commit}
        body=HEAD_MAGIC+canonical(manifest).encode()
        if len(body)>MAX_HEAD_BYTES:raise StorageUnavailable('commit head size limit')
        self._immutable(body,'commits')
        try:response=self.client.put_object(Bucket=self.bucket,Key=self.key,Body=body,ContentType='application/octet-stream',IfMatch=parent)
        except BaseException:
            self.metrics['orphan_uploads_possible']=True
            raise
        etag=response.get('ETag')
        if not etag or etag==parent:raise StorageUnavailable('commit head identity unverified')
        self.upload_bytes+=len(body);self.metrics['head_bytes']+=len(body);self.metrics[kind]+=1
        self.snapshot_bytes=sum(r['bytes'] for r in [base]+chain)
        self._raw=raw;self._manifest=manifest;self._legacy_body=None;self._etag=etag;self._generation=generation
        return etag
