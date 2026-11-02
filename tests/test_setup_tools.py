"""Installation helpers use only temporary synthetic configuration and fake services."""
import importlib.util,json,sys
from pathlib import Path
import pytest
from sailboat.instance_setup import initialize
from sailboat.bucket_store import BucketSnapshots

def load(name):
    path=Path(__file__).resolve().parents[1]/'tools'/(name+'.py')
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def config(tmp_path):return initialize(tmp_path/'instance','https://instance.example.org')

def test_seed_only_new_empty_checkpoint(tmp_path,monkeypatch):
    path=config(tmp_path);env=json.loads(path.read_text())
    env.update(SAILBOAT_STORAGE_BACKEND='bucket',SAILBOAT_STATE_NAMESPACE='synthetic',SAILBOAT_STATE_BUCKET='empty',
        SAILBOAT_STATE_KEY='instance/state.snapshot',SAILBOAT_STATE_S3_ACCESS_KEY='fixture-access',SAILBOAT_STATE_S3_SECRET_KEY='fixture-secret') # pragma: allowlist secret -- synthetic offline storage fixture
    path.write_text(json.dumps(env));module=load('seed_bucket');writes=[]
    class Client:
        def put_object(self,**kw):
            assert kw['IfNoneMatch']=='*'
            if writes:raise ValueError('existing key')
            writes.append(kw)
    class Store:
        decode=staticmethod(BucketSnapshots.decode)
        def __init__(self,bucket,key,**kw):self.bucket=bucket;self.key=key;self.client=Client()
    monkeypatch.setattr(module,'BucketSnapshots',Store)
    monkeypatch.setattr(sys,'argv',['seed_bucket','--config',str(path)])
    module.main();assert len(writes)==1
    with pytest.raises(ValueError,match='existing key'):module.main()

def test_broker_setup_binds_callback_and_cannot_replace_consent(tmp_path,monkeypatch):
    path=config(tmp_path);module=load('connect_broker');registrations=[]
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'client_id':'synthetic-client','redirect_uris':['https://instance.example.org/oauth/robinhood/callback']}
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def post(self,url,json):registrations.append((url,json));return Response()
    monkeypatch.setattr(module.httpx,'Client',Client)
    monkeypatch.setattr(sys,'argv',['connect_broker','--config',str(path)])
    module.main();assert len(registrations)==1
    env=json.loads(path.read_text());bootstrap=json.loads(env['RH_OAUTH_BOOTSTRAP'])
    assert bootstrap['redirect_uri']=='https://instance.example.org/oauth/robinhood/callback'
    assert env['SAILBOAT_BROKER_WRITES_DISABLED']=='1'
    assert 'SAILBOAT_BROKER_ACCOUNT' not in env
    with pytest.raises(ValueError,match='already exists'):module.main()

def test_hosted_agent_profile_is_encrypted_and_not_overwritten(tmp_path,monkeypatch):
    path=config(tmp_path);module=load('import_agent_profile')
    profile=tmp_path/'fresh-profile.json';profile.write_text(json.dumps({'credentials':{'claudeAiOauth':{'subscriptionType':'pro','accessToken':'synthetic-fixture-value'}}}))
    secret=tmp_path/'agent-secret.txt'
    monkeypatch.setattr(sys,'argv',['import_agent_profile','--config',str(path),'--profile',str(profile),'--output-secret',str(secret)])
    module.main();assert 'synthetic-fixture-value' not in secret.read_text()
    from cryptography.fernet import Fernet
    decoded=Fernet(json.loads(path.read_text())['SAILBOAT_AGENT_KEY'].encode()).decrypt(secret.read_bytes())
    assert json.loads(decoded)==json.loads(profile.read_text())
    with pytest.raises(FileExistsError):module.main()
