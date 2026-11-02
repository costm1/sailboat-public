"""Conditional rolling storage; fixtures never contact a remote object store."""
import hashlib,io,sqlite3
import pytest
from sailboat.bucket_store import BucketSnapshots
from sailboat.cloud_store import DurableController,StorageUnavailable
from test_order_execution import Store

class ObjectStore:
    def __init__(self,body):self.body=body;self.puts=0;self.fail=None
    def head_object(self,**kw):return {'ETag':hashlib.sha256(self.body).hexdigest()}
    def get_object(self,**kw):return self.head_object()|{'Body':io.BytesIO(self.body)}
    def put_object(self,**kw):
        assert kw['IfMatch']==self.head_object()['ETag'],'conditional writer conflict'
        if self.fail=='before':raise OSError('synthetic storage unavailable')
        self.body=kw['Body'];self.puts+=1
        if self.fail=='after':raise TimeoutError('synthetic lost acknowledgment')
        return self.head_object()

def setup(tmp_path):
    repo=Store(tmp_path);db=tmp_path/'seed.sqlite';_,generation=repo.load(db)
    body,_=BucketSnapshots.encode(db.read_bytes(),generation)
    fake=ObjectStore(body);store=BucketSnapshots('synthetic','state.snapshot',client=fake)
    return fake,store

def test_atomic_snapshot_recovery_generation_and_single_rolling_object(tmp_path):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'one')
    for i in range(30):c.scheduler_transaction(lambda ctl,s:s.update(cycle=i))
    copy=tmp_path/'loaded.sqlite';head,g=store.load(copy)
    assert g==c.generation and head==c.head and fake.puts==31
    assert sqlite3.connect(copy).execute('PRAGMA integrity_check').fetchone()[0]=='ok'
    next_controller=DurableController(store,tmp_path/'two')
    assert next_controller.scheduler_view()[0]['cycle']==29
    assert next_controller.ready

def test_competing_writer_is_fenced(tmp_path):
    fake,store=setup(tmp_path);a=DurableController(store,tmp_path/'a');b=DurableController(store,tmp_path/'b')
    with pytest.raises(StorageUnavailable):a.scheduler_transaction(lambda c,s:s.update(stale=True))
    assert not a.ready and b.ready

def test_hour_of_two_minute_cycles_has_no_git_commit_dependency(tmp_path):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'hour')
    for cycle in range(30):
        for step in range(20):
            c.scheduler_transaction(lambda ctl,s:s.update(cycle=cycle,step=step))
    assert c.ready and fake.puts==601
    recovered=DurableController(store,tmp_path/'restart')
    assert recovered.scheduler_view()[0]=={'cycle':29,'step':19}

@pytest.mark.parametrize('failure',['before','after'])
def test_failed_or_unknown_commit_is_never_acknowledged(tmp_path,failure):
    fake,store=setup(tmp_path);a=DurableController(store,tmp_path/'a');fake.fail=failure
    with pytest.raises(StorageUnavailable):a.scheduler_transaction(lambda c,s:s.update(unacknowledged=True))
    assert not a.ready
    fake.fail=None;b=DurableController(store,tmp_path/'b')
    assert bool(b.scheduler_view()[0].get('unacknowledged'))==(failure=='after')

@pytest.mark.parametrize('target',['header','body','truncated'])
def test_corrupt_object_never_recovers(tmp_path,target):
    fake,store=setup(tmp_path)
    if target=='header':fake.body=b'wrong'+fake.body
    elif target=='body':fake.body=fake.body[:-1]+bytes([fake.body[-1]^1])
    else:fake.body=fake.body[:20]
    with pytest.raises((StorageUnavailable,ValueError,KeyError)):
        DurableController(store,tmp_path/'bad')
