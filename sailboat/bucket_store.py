"""Atomic rolling checkpoints on HF's conditional-write object API.

The existing SQLite controller, journal, retention, and acknowledgement boundaries
are unchanged. One object contains the manifest and database; no two-file race.
"""
import gzip,hashlib,io,json,time
from botocore.exceptions import (ClientError,ConnectionClosedError,ConnectTimeoutError,
    EndpointConnectionError,ReadTimeoutError)
from .cloud_store import StorageUnavailable,checked_snapshot,MAX_DATABASE_BYTES,MAX_SNAPSHOT_BYTES
from .controller import canonical

MAGIC=b'SAILBOAT-SNAPSHOT-1\n'
MAX_HEADER=4096
HEAD_ATTEMPTS=3

def transient_read_error(exc):
    if isinstance(exc,(ConnectionClosedError,ConnectTimeoutError,EndpointConnectionError,ReadTimeoutError)):
        return True
    return isinstance(exc,ClientError) and exc.response.get('ResponseMetadata',{}).get('HTTPStatusCode') in (429,500,502,503,504)

class BucketSnapshots:
    def __init__(self,bucket,key,*,namespace=None,access_key=None,secret_key=None,client=None):
        if not bucket or '/' in bucket or not key or any(x in key for x in ('..','\\','//')):raise ValueError('invalid checkpoint location')
        self.bucket,self.key=bucket,key
        if client is None:
            if not namespace or not __import__('re').fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*',namespace):raise ValueError('Explicit owner storage namespace required')
            import boto3
            from botocore.config import Config
            if not access_key or not secret_key:raise ValueError('HF S3 checkpoint credentials required')
            client=boto3.client('s3',endpoint_url='https://s3.hf.co/'+namespace,
                aws_access_key_id=access_key,aws_secret_access_key=secret_key,
                config=Config(region_name='us-east-1',s3={'addressing_style':'path'},
                    request_checksum_calculation='when_required',response_checksum_validation='when_required',
                    connect_timeout=10,read_timeout=30,retries={'total_max_attempts':1}))
        self.client=client;self.upload_bytes=0;self.download_bytes=0;self.snapshot_bytes=0
    def head(self):
        # A failed HEAD has no write outcome to reconcile. Retry only this read;
        # the caller still compares the returned identity before any mutation.
        # PUT retains one attempt: a lost write acknowledgment must still fence.
        for attempt in range(HEAD_ATTEMPTS):
            try:return self.client.head_object(Bucket=self.bucket,Key=self.key)['ETag']
            except Exception as exc:
                if not transient_read_error(exc) or attempt+1==HEAD_ATTEMPTS:raise
                time.sleep(.25*(2**attempt))
    @staticmethod
    def encode(data,generation):
        if len(data)>MAX_DATABASE_BYTES:raise StorageUnavailable('decoded snapshot limit reached')
        # Every acknowledgment waits for compression. Prefer the fast codec
        # setting, with the old setting as a size-cap fallback. Same bytes,
        # hashes, conditional writes and recovery format; no evidence removed.
        level=1;packed=gzip.compress(data,compresslevel=level,mtime=0)
        if len(packed)>MAX_SNAPSHOT_BYTES:
            level=6;packed=gzip.compress(data,compresslevel=level,mtime=0)
        if len(packed)>MAX_SNAPSHOT_BYTES:raise StorageUnavailable('retained trade snapshot storage limit reached')
        m={'format':'sqlite-gzip-v1','sha256':hashlib.sha256(data).hexdigest(),
            'compressed_sha256':hashlib.sha256(packed).hexdigest(),'generation':generation,
            'created_at':time.time(),'database_bytes':len(data),'snapshot_bytes':len(packed),'compression_level':level}
        header=canonical(m).encode()
        return MAGIC+len(header).to_bytes(4,'big')+header+packed,m
    @staticmethod
    def decode(body):
        if not body.startswith(MAGIC):raise StorageUnavailable('unknown checkpoint format')
        offset=len(MAGIC);size=int.from_bytes(body[offset:offset+4],'big');offset+=4
        if not 0<size<=MAX_HEADER:raise StorageUnavailable('invalid checkpoint header')
        m=json.loads(body[offset:offset+size]);packed=body[offset+size:]
        if m.get('format')!='sqlite-gzip-v1' or type(m.get('generation')) is not int or m['generation']<0:raise StorageUnavailable('invalid checkpoint manifest')
        if len(packed)>MAX_SNAPSHOT_BYTES or len(packed)!=m['snapshot_bytes'] or hashlib.sha256(packed).hexdigest()!=m['compressed_sha256']:raise StorageUnavailable('compressed snapshot hash/size mismatch')
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as stream:data=stream.read(MAX_DATABASE_BYTES+1)
        if len(data)>MAX_DATABASE_BYTES or len(data)!=m['database_bytes'] or hashlib.sha256(data).hexdigest()!=m['sha256']:raise StorageUnavailable('decoded snapshot hash/size mismatch')
        return data,m
    def load(self,dest):
        response=self.client.get_object(Bucket=self.bucket,Key=self.key)
        stream=response['Body']
        try:body=stream.read(MAX_SNAPSHOT_BYTES+MAX_HEADER+len(MAGIC)+5)
        finally:stream.close()
        if len(body)>MAX_SNAPSHOT_BYTES+MAX_HEADER+len(MAGIC)+4:raise StorageUnavailable('checkpoint size limit')
        data,m=self.decode(body);dest.write_bytes(data);checked_snapshot(dest)
        self.snapshot_bytes=m['snapshot_bytes'];self.download_bytes+=len(body)
        return response['ETag'],m['generation']
    def save(self,path,parent,generation):
        body,m=self.encode(path.read_bytes(),generation)
        # No blind retries or last-write-wins fallback. An unknown response fences
        # the controller; restart reads the actual acknowledged object identity.
        response=self.client.put_object(Bucket=self.bucket,Key=self.key,Body=body,
            ContentType='application/octet-stream',IfMatch=parent)
        head=response.get('ETag')
        if not head or head==parent:raise StorageUnavailable('checkpoint write identity unverified')
        self.upload_bytes+=len(body);self.snapshot_bytes=m['snapshot_bytes']
        return head
