import hashlib,json,sqlite3,threading,time
from pathlib import Path
import pytest
from starlette.testclient import TestClient
from sailboat.agent_link import AgentLink
from sailboat.cloud_store import DurableController,StorageUnavailable
from sailboat.cloud_server import make_app
from sailboat.structured_schema import empty_body

class Store:
    def __init__(self,tmp):
        self.data={};self.revision='seed';self.generation=0;self.fail=False;self.upload_bytes=0;self.download_bytes=0
        c=AgentLink(tmp/'seed.sqlite');c.close()
        self.data['seed']=(tmp/'seed.sqlite').read_bytes()
    def head(self):return self.revision
    def load(self,dest):dest.write_bytes(self.data[self.revision]);return self.revision,self.generation
    def save(self,path,parent,generation):
        if self.fail:raise OSError('simulated upload failure')
        if parent!=self.revision:raise RuntimeError('CAS conflict')
        self.revision=str(generation);self.generation=generation;self.data[self.revision]=path.read_bytes();return self.revision

P={'source':'external-agent','conversation':'shared-paper','label':'codex'}
Q=P|{'label':'claude'}
OTHER={'source':'other','conversation':'portfolio','label':'other'}
KEYS={hashlib.sha256(k.encode()).hexdigest():p for k,p in [('a'*40,P),('b'*40,Q),('c'*40,OTHER)]}

def rpc(client,method,params=None,key='a'*40,**kw):
 return client.post('/mcp',json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},headers={'Authorization':'Bearer '+key,'Accept':'application/json, text/event-stream'},**kw)
def call(client,name,args,key='a'*40):return rpc(client,'tools/call',{'name':name,'arguments':args},key).json()['result']
def req(client,key='a'*40):return call(client,'sailboat_get_context',{'operator_request':'Watch QQQ; no order.'},key)['structuredContent']
def body(r,**kw):return {'request_id':r['request_id'],'context_hash':r['context_hash'],'decision':empty_body(**kw)}
@pytest.fixture
def setup(tmp_path):
 store=Store(tmp_path);service=DurableController(store,tmp_path/'live')
 return store,service

def test_auth_and_discovery(setup):
 store,s=setup
 with TestClient(make_app(s,KEYS,['testserver'])) as c:
  assert c.post('/mcp',json={}).status_code==401
  assert c.get('/status').status_code==401
  assert rpc(c,'tools/list',key='invalid').status_code==401
  assert c.post('/mcp',json={},headers={'Host':'evil','Authorization':'Bearer '+'a'*40}).status_code==400
  assert c.post('/mcp',json={},headers={'Origin':'https://evil','Authorization':'Bearer '+'a'*40}).status_code==403
  init=rpc(c,'initialize',{'protocolVersion':'2025-03-26','capabilities':{},'clientInfo':{'name':'test','version':'1'}})
  assert init.status_code==200
  assert {t['name'] for t in rpc(c,'tools/list').json()['result']['tools']}=={'sailboat_get_context','sailboat_submit_decision','sailboat_get_receipt'}
  assert len(rpc(c,'resources/list').json()['result']['resources'])==1

def test_shared_plan_receipts_and_other_owner(setup,tmp_path):
 store,s=setup
 with TestClient(make_app(s,KEYS,['testserver'])) as c:
  r=req(c);v=body(r,action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0)
  assert call(c,'sailboat_submit_decision',v)['structuredContent']['result']['status']=='WATCHING'
  b=req(c,'b'*40);plan=b['context']['plans'][0]
  assert plan['id']==r['new_plan_id']
  v2=body(b,action='revise',revision='suspend',symbol='QQQ',plan_id=plan['id'],plan_version=1)
  result=call(c,'sailboat_submit_decision',v2,'b'*40)
  assert result['structuredContent']['result']['status']=='SUSPENDED'
  assert call(c,'sailboat_get_receipt',{'request_id':b['request_id']},'c'*40)['isError']
 recovered=DurableController(store,tmp_path/'recovered')
 with TestClient(make_app(recovered,KEYS,['testserver'])) as c:
  assert call(c,'sailboat_get_receipt',{'request_id':b['request_id']})['structuredContent']==result['structuredContent']
  assert call(c,'sailboat_submit_decision',v2)['structuredContent']['status']=='DUPLICATE'
  v2['decision']['assessment']='bullish'
  assert call(c,'sailboat_submit_decision',v2)['isError']

@pytest.mark.parametrize('kind',['missing','live','unknown','stale'])
def test_bad_and_stale(setup,kind):
 _,s=setup
 with TestClient(make_app(s,KEYS,['testserver'])) as c:
  r=req(c);v=body(r)
  if kind=='missing':del v['decision']['quantity']
  elif kind=='live':v['decision']['execution']='live'
  elif kind=='unknown':v['owner']='other'
  else:
   b=req(c);call(c,'sailboat_submit_decision',body(b,action='watch',symbol='QQQ',plan_id=b['new_plan_id'],plan_version=0))
  assert call(c,'sailboat_submit_decision',v)['isError']
  ctl=AgentLink(s.db);assert ctl.db.execute('select count(*) from fills').fetchone()[0]==0;ctl.close()

def test_commit_failure_never_acknowledged(setup,tmp_path):
 store,s=setup
 with TestClient(make_app(s,KEYS,['testserver'])) as c:
  r=req(c);store.fail=True
  result=call(c,'sailboat_submit_decision',body(r,action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0))
  assert result['isError'] and result['structuredContent']['status']=='UNAVAILABLE' and not s.ready
 store.fail=False;recovered=DurableController(store,tmp_path/'recover')
 assert recovered.execute(P,'sailboat_get_receipt',{'request_id':r['request_id']})['status']=='AWAITING_RESPONSE'

def test_competing_writer_is_fenced(setup,tmp_path):
 store,s=setup
 other=DurableController(store,tmp_path/'other')
 with pytest.raises(StorageUnavailable):s.execute(P,'sailboat_get_context',{'operator_request':'Observe'})
 assert not s.ready and other.ready

def test_monitor_runs_without_agent(setup):
 _,s=setup
 c=AgentLink(s.db);c.db.execute("insert into orders values('expired','external-agent/shared-paper','{}','PENDING',0,1,NULL,NULL)");c.close();s._checkpoint()
 with TestClient(make_app(s,KEYS,['testserver'],monitor_interval=.02)):
  time.sleep(.2)
 c=AgentLink(s.db);assert c.db.execute("select status from orders where id='expired'").fetchone()[0]=='EXPIRED';c.close()

def test_corrupt_snapshot_refused(setup,tmp_path):
 store,_=setup;store.data[store.revision]=b'not sqlite'
 with pytest.raises(sqlite3.DatabaseError):DurableController(store,tmp_path/'bad')


def test_lost_commit_response_recovers_exactly_once(setup,tmp_path):
 store,s=setup;r=s.execute(P,'sailboat_get_context',{'operator_request':'Watch QQQ'})
 v=body(r,action='watch',symbol='QQQ',plan_id=r['new_plan_id'],plan_version=0)
 original=store.save
 def lost(*args):original(*args);raise OSError('response lost after commit')
 store.save=lost
 with pytest.raises(StorageUnavailable):s.execute(P,'sailboat_submit_decision',v)
 store.save=original;recovered=DurableController(store,tmp_path/'recovered-lost')
 assert recovered.execute(Q,'sailboat_submit_decision',v)['status']=='DUPLICATE'
 c=AgentLink(recovered.db);assert c.db.execute('select count(*) from structured_plans').fetchone()[0]==1;c.close()

@pytest.mark.parametrize('tool',['place_equity_order','cancel_equity_order','place_option_order','exercise_option','create_watchlist','arbitrary'])
def test_broker_write_allowlist_denies_before_network(setup,tool):
 import asyncio
 from cryptography.fernet import Fernet
 from sailboat.robinhood_readonly import RobinhoodReadOnly
 _,s=setup;b=RobinhoodReadOnly(s,{},Fernet.generate_key().decode())
 with pytest.raises(ValueError,match='allowlist'):asyncio.run(b.read(tool,{}))

def test_callback_csrf_and_encrypted_credentials(setup):
 from cryptography.fernet import Fernet
 from sailboat.robinhood_readonly import RobinhoodReadOnly
 import asyncio
 _,s=setup;key=Fernet.generate_key().decode()
 b=RobinhoodReadOnly(s,{'state':'secret-state','expires_at':time.time()+120},key)
 with TestClient(make_app(s,KEYS,['testserver'],broker=b)) as c:
  assert c.get('/oauth/robinhood/callback?state=wrong&code=fake').status_code==400
  assert c.get('/broker/status').status_code==401
  other_headers={'Authorization':'Bearer '+'c'*40}
  assert c.get('/broker/status',headers=other_headers).status_code==403
  assert c.post('/broker/read',json={'tool':'get_accounts','arguments':{}},headers=other_headers).status_code==403
 asyncio.run(b._save({'access_token':'test-secret','refresh_token':'refresh-secret','expires_at':time.time()+120}))
 assert 'test-secret' not in s.private_read('robinhood_tokens')
 assert asyncio.run(b._load())['access_token']=='test-secret'
 c=AgentLink(s.db);c.close()
 r=s.execute(OTHER,'sailboat_get_context',{'operator_request':'Observe'})
 assert 'test-secret' not in json.dumps(r) and r['context']['brokerage_observations']==[]
