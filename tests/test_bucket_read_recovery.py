"""Oct 30 HEAD connection-loss regression; all storage and broker calls synthetic."""
import copy
import pytest
from botocore.exceptions import (ClientError,ConnectionClosedError,ConnectTimeoutError,
    EndpointConnectionError,ReadTimeoutError)
from sailboat.bucket_store import BucketSnapshots
from sailboat.cloud_store import DurableController,StorageUnavailable
from sailboat.order_execution import Journal,OrderWorker
from test_bucket_store import setup
from test_order_execution import Broker,plan,run

def closed():return ConnectionClosedError(endpoint_url='https://synthetic.invalid')
def http(status):return ClientError({'Error':{'Code':str(status)},'ResponseMetadata':{'HTTPStatusCode':status}},'HeadObject')

def fail_heads(monkeypatch,fake,failures):
    original=fake.head_object;calls=[];sleeps=[]
    monkeypatch.setattr('sailboat.bucket_store.time.sleep',sleeps.append)
    def head(**kw):
        calls.append(kw)
        if failures:raise failures.pop(0)
        return original(**kw)
    monkeypatch.setattr(fake,'head_object',head)
    return calls,sleeps

@pytest.mark.parametrize('error',[closed(),ConnectTimeoutError(endpoint_url='https://synthetic.invalid'),
    EndpointConnectionError(endpoint_url='https://synthetic.invalid'),ReadTimeoutError(endpoint_url='https://synthetic.invalid'),
    *[http(n) for n in (429,500,502,503,504)]])
def test_transient_head_recovers_identical_identity(tmp_path,monkeypatch,error):
    fake,store=setup(tmp_path);expected=store.head();before=fake.body
    calls,sleeps=fail_heads(monkeypatch,fake,[error,closed()])
    assert store.head()==expected
    assert len(calls)==3 and sleeps==[.25,.5]
    assert all(call==calls[0] for call in calls)
    assert fake.puts==0 and fake.body==before

@pytest.mark.parametrize('error',[http(401),http(403),http(404),http(412),ValueError('invalid response')])
def test_permission_missing_object_and_conflict_are_not_retried(tmp_path,monkeypatch,error):
    fake,store=setup(tmp_path);calls,sleeps=fail_heads(monkeypatch,fake,[error])
    with pytest.raises(type(error)):store.head()
    assert len(calls)==1 and sleeps==[] and fake.puts==0

def test_exact_owner_observation_failure_path_preserves_history_and_recovers(tmp_path,monkeypatch):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'live')
    c.observation_write('preserved',{'orders':['historical']});generation=c.generation
    fail_heads(monkeypatch,fake,[closed()])
    c.observation_write('fresh',{'positions':[]})
    assert c.ready and c.generation==generation+1
    next_c=DurableController(store,tmp_path/'restart')
    import json,sqlite3
    with sqlite3.connect(next_c.db) as db:
        observations={k:json.loads(v) for k,v in db.execute('SELECT name,value FROM broker_observations')}
    assert observations=={'preserved':{'orders':['historical']},'fresh':{'positions':[]}}

def test_exhausted_head_fences_before_mutation_and_recovers_disarmed(tmp_path,monkeypatch):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'live');before=fake.body;puts=fake.puts
    calls,sleeps=fail_heads(monkeypatch,fake,[closed(),closed(),closed()])
    with pytest.raises(StorageUnavailable):c.observation_write('must-not-commit',{'new':True})
    assert not c.ready and c.last_error=='ConnectionClosedError'
    assert len(calls)==3 and sleeps==[.25,.5] and fake.body==before and fake.puts==puts
    next_c=DurableController(store,tmp_path/'restart')
    assert next_c.ready and next_c.execution_view()['mode']=='DISARMED'

def test_retry_does_not_hide_competing_checkpoint(tmp_path,monkeypatch):
    fake,store=setup(tmp_path);a=DurableController(store,tmp_path/'a')
    b=DurableController(store,tmp_path/'b');before=fake.body;puts=fake.puts
    fail_heads(monkeypatch,fake,[closed()])
    with pytest.raises(StorageUnavailable):a.observation_write('stale',{'unsafe':True})
    assert not a.ready and b.ready and fake.body==before and fake.puts==puts

def test_recovered_head_reaches_existing_worker_once_and_restarts_disarmed(tmp_path,monkeypatch):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'live');clock=[1000.]
    b=Broker(clock);w=OrderWorker(Journal(c),b,clock=lambda:clock[0],timeout=1)
    run(w.arm());w.accept(plan())
    fail_heads(monkeypatch,fake,[closed()])
    run(w.tick());assert c.ready and len(b.calls)==1 and b.calls[0][0]=='place'
    next(iter(b.orders.values())).update(state='filled',filled=3)
    clock[0]+=5
    run(w.tick());assert w.status()['orders']['plan-1']['status']=='FILLED'
    w.accept(plan());run(w.tick());assert len(b.calls)==1
    before=copy.deepcopy(w.status()['orders'])
    restored=DurableController(store,tmp_path/'restart');next_w=OrderWorker(Journal(restored),b,clock=lambda:clock[0])
    run(next_w.tick())
    assert next_w.status()['mode']=='DISARMED' and next_w.status()['orders']==before
    assert len(b.calls)==1

def test_persistent_head_failure_cannot_reach_broker(tmp_path,monkeypatch):
    fake,store=setup(tmp_path);c=DurableController(store,tmp_path/'live');clock=[1000.]
    b=Broker(clock);w=OrderWorker(Journal(c),b,clock=lambda:clock[0],timeout=1)
    run(w.arm());w.accept(plan())
    fail_heads(monkeypatch,fake,[closed(),closed(),closed()])
    with pytest.raises(StorageUnavailable):run(w.tick())
    assert not c.ready and not b.calls
