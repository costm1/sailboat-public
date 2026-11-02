import hashlib,json
from pathlib import Path
import pytest
from starlette.testclient import TestClient
from sailboat.instance_setup import initialize,isolated_environment
from sailboat.instance_config import validate_environment
from sailboat.local_store import LocalSnapshots
from sailboat.cloud_store import DurableController,StorageUnavailable
from sailboat.cloud_server import make_app
from sailboat.owner_panel import OwnerPanel
from sailboat.robinhood_orders import PreparedLiveControls
from sailboat.bucket_store import BucketSnapshots

def new(tmp_path,name='a'):
    path=initialize(tmp_path/name,'https://'+name+'.example.org')
    return path,json.loads(path.read_text())

def test_independent_instances_start_empty_and_do_not_share_keys(tmp_path):
    pa,a=new(tmp_path,'a');pb,b=new(tmp_path,'b')
    assert a['SAILBOAT_OWNER_KEY_SHA256']!=b['SAILBOAT_OWNER_KEY_SHA256']
    assert a['SAILBOAT_KEYS_JSON']!=b['SAILBOAT_KEYS_JSON']
    assert a['RH_ENCRYPTION_KEY']!=b['RH_ENCRYPTION_KEY']
    assert a['SAILBOAT_STATE_DIRECTORY']!=b['SAILBOAT_STATE_DIRECTORY']
    sa=DurableController(LocalSnapshots(a['SAILBOAT_STATE_DIRECTORY']),tmp_path/'recover-a')
    sb=DurableController(LocalSnapshots(b['SAILBOAT_STATE_DIRECTORY']),tmp_path/'recover-b')
    from sailboat.agent_link import AgentLink
    for s in (sa,sb):
        c=AgentLink(s.db)
        assert c.db.execute("SELECT v FROM meta WHERE k='mode'").fetchone()[0]=='DISARMED'
        for table in ('orders','positions','fills','messages'):
            assert c.db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert c.verify_journal();c.close()
        assert PreparedLiveControls(s).status()['broker_write_calls_enabled'] is False
    with pytest.raises(FileExistsError):initialize(pa.parent,'https://a.example.org')

def test_restart_disarms_and_fences_old_writer(tmp_path):
    _,config=new(tmp_path)
    store=LocalSnapshots(config['SAILBOAT_STATE_DIRECTORY'])
    first=DurableController(store,tmp_path/'first')
    first.scheduler_transaction(lambda c,s:c.set_mode('PAPER_ARMED'))
    second=DurableController(LocalSnapshots(config['SAILBOAT_STATE_DIRECTORY']),tmp_path/'second')
    from sailboat.agent_link import AgentLink
    c=AgentLink(second.db)
    assert c.db.execute("SELECT v FROM meta WHERE k='mode'").fetchone()[0]=='DISARMED';c.close()
    with pytest.raises(StorageUnavailable):first.scheduler_transaction(lambda c,s:c.set_mode('DISARMED'))

def test_missing_and_corrupt_state_never_reset(tmp_path):
    with pytest.raises(StorageUnavailable):LocalSnapshots(tmp_path/'absent')
    _,config=new(tmp_path)
    state=Path(config['SAILBOAT_STATE_DIRECTORY'])/'state.snapshot';state.write_bytes(b'corrupt')
    with pytest.raises(StorageUnavailable):DurableController(LocalSnapshots(state.parent),tmp_path/'bad')
    assert state.read_bytes()==b'corrupt'

def test_inherited_credentials_and_endpoints_are_not_reused(tmp_path,monkeypatch):
    _,config=new(tmp_path)
    for k in ('HF_TOKEN','RH_INITIAL_TOKENS','SAILBOAT_STATE_BUCKET','SAILBOAT_BROKER_ACCOUNT','SAILBOAT_AGENT_URL','ANTHROPIC_API_KEY','PYTHONPATH'):
        monkeypatch.setenv(k,'foreign-instance')
    env=isolated_environment(config)
    assert all(env.get(k)!='foreign-instance' for k in ('HF_TOKEN','RH_INITIAL_TOKENS','SAILBOAT_STATE_BUCKET','SAILBOAT_BROKER_ACCOUNT','SAILBOAT_AGENT_URL','ANTHROPIC_API_KEY','PYTHONPATH'))
    assert env['SAILBOAT_BROKER_WRITES_DISABLED']=='1'

def test_no_implicit_owner_or_storage_namespace(tmp_path):
    with pytest.raises(ValueError,match='namespace'):BucketSnapshots('bucket','key',access_key='synthetic',secret_key='synthetic')
    _,config=new(tmp_path)
    for bad in ({},config|{'SAILBOAT_PUBLIC_URL':'https://example.invalid'},config|{'SAILBOAT_BROKER_WRITES_DISABLED':'false'},config|{'SAILBOAT_OWNER_KEY_SHA256':next(iter(json.loads(config['SAILBOAT_KEYS_JSON'])))}):
        with pytest.raises(ValueError):validate_environment(bad)

def test_dashboard_auth_and_cross_instance_credentials(tmp_path):
    path,config=new(tmp_path)
    other,b=new(tmp_path,'b')
    service=DurableController(LocalSnapshots(config['SAILBOAT_STATE_DIRECTORY']),tmp_path/'run')
    execution=PreparedLiveControls(service)
    panel=OwnerPanel(execution,None,service)
    app=make_app(service,json.loads(config['SAILBOAT_KEYS_JSON']),['a.example.org'],owner_key_hash=config['SAILBOAT_OWNER_KEY_SHA256'],owner_panel=panel)
    token=json.loads((path.parent/'agent-credentials.json').read_text())['agent_token']
    foreign=json.loads((other.parent/'agent-credentials.json').read_text())['agent_token']
    with TestClient(app,base_url='https://a.example.org') as client:
        assert client.get('/').status_code==200
        assert client.get('/owner/panel.js').status_code==200
        assert client.get('/owner/view').status_code==401
        assert client.get('/owner/login').status_code==503
        assert client.get('/status').status_code==401
        assert client.get('/status',headers={'Authorization':'Bearer '+foreign}).status_code==401
        assert client.get('/status',headers={'Authorization':'Bearer '+token}).status_code==200
        denied=client.post('/owner/control',json={'command':'arm'},headers={'Authorization':'Bearer '+token})
        assert denied.status_code==403
        assert execution.status()['mode']=='DISARMED'

def test_startup_uses_explicit_instance_identity_and_disabled_gates(tmp_path,monkeypatch):
    _,config=new(tmp_path)
    import os,uvicorn
    from sailboat.cloud_server import main
    monkeypatch.setattr(os,'environ',config.copy())
    observed={}
    def run(app,**kwargs):
        observed.update(kwargs)
        assert app.owner_panel.base==config['SAILBOAT_PUBLIC_URL']
        assert app.owner_panel.owner_sub==''
        assert app.owner_panel.service.broker_writes_disabled is True
        assert app.owner_panel.execution.status()['mode']=='DISARMED'
        assert app.chat_auth.base==config['SAILBOAT_PUBLIC_URL']
        assert app.owner_panel.scheduler is None
    monkeypatch.setattr(uvicorn,'run',run)
    main()
    assert observed['host']=='127.0.0.1' and observed['workers']==1

def test_network_guard_rejects_outbound_sockets():
    import socket
    with socket.socket() as s:
        with pytest.raises(AssertionError,match='Outbound'):s.connect(('192.0.2.1',443))
    with pytest.raises(AssertionError):socket.getaddrinfo('example.org',443)
