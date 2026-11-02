"""Optional local durable storage using the existing verified snapshot format.

One writer is supported. Locked compare-and-swap fences competing controllers.
No blank state is silently substituted for a missing or corrupt checkpoint.
"""
import hashlib, os, uuid
from pathlib import Path
from filelock import FileLock
from .bucket_store import BucketSnapshots
from .cloud_store import StorageUnavailable,checked_snapshot

class LocalSnapshots:
    def __init__(self,directory):
        self.directory=Path(directory).resolve()
        self.path=self.directory/'state.snapshot'
        if not self.path.is_file():raise StorageUnavailable('Initialize a new instance explicitly before starting')
        self.lock=FileLock(str(self.path)+'.lock')
        self.upload_bytes=0;self.download_bytes=0;self.snapshot_bytes=0
    def head(self):return hashlib.sha256(self.path.read_bytes()).hexdigest()
    def load(self,dest):
        with self.lock:
            body=self.path.read_bytes()
            data,manifest=BucketSnapshots.decode(body)
            dest.write_bytes(data);checked_snapshot(dest)
            self.download_bytes+=len(body);self.snapshot_bytes=len(body)
            return hashlib.sha256(body).hexdigest(),manifest['generation']
    def save(self,path,parent,generation):
        body,_=BucketSnapshots.encode(path.read_bytes(),generation)
        with self.lock:
            if self.head()!=parent:raise StorageUnavailable('Local checkpoint changed; writer fenced')
            temp=self.directory/(uuid.uuid4().hex+'.tmp')
            with temp.open('xb') as stream:
                stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.replace(temp,self.path)
            self.upload_bytes+=len(body);self.snapshot_bytes=len(body)
            return hashlib.sha256(body).hexdigest()
