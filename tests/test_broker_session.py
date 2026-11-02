"""Exercise actual broker transport lifetime and permits with synthetic MCP."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from cryptography.fernet import Fernet
from sailboat.robinhood_readonly import RobinhoodReadOnly
from sailboat.robinhood_orders import RobinhoodOptionTransport
from test_order_execution import run

@pytest.fixture
def session(monkeypatch):
    import sailboat.robinhood_readonly as m
    state={'opens':0,'closed':0,'listed':0,'calls':[],'token':'first','error':False,'paused':None,'initialized':None}
    @asynccontextmanager
    async def transport(*args,**kwargs):
        state['opens']+=1
        try:yield None,None,None
        finally:state['closed']+=1
    class Session:
        def __init__(self,*args):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def initialize(self):
            if state['initialized']:state['initialized']()
        async def list_tools(self):
            state['listed']+=1
            return SimpleNamespace(tools=[SimpleNamespace(name=n,inputSchema={'type':'object'},outputSchema={'type':'object'}) for n in ('get_accounts','place_option_order','review_option_order')])
        async def call_tool(self,name,args):
            state['calls'].append((name,dict(args)))
            if state['paused']:await state['paused'].wait()
            if state['error']:raise TimeoutError('synthetic lost response after submission')
            return SimpleNamespace(model_dump=lambda **kw:{'structuredContent':{'data':{'ok':True}}})
    monkeypatch.setattr(m,'streamablehttp_client',transport);monkeypatch.setattr(m,'ClientSession',Session)
    reader=RobinhoodReadOnly(SimpleNamespace(ready=True),{},Fernet.generate_key().decode())
    async def token():return state['token']
    reader.token=token
    return reader,state

def test_reads_review_and_place_share_connection_and_discovery(session):
    reader,s=session;writer=RobinhoodOptionTransport(reader,lambda *_:True)
    async def go():
        for _ in range(3):await reader.read('get_accounts',{},persist=False)
        await writer.call('review_option_order',{});await writer.call('place_option_order',{'ref_id':'same'})
        assert s['opens']==s['listed']==1 and len(s['calls'])==5
        await reader.close();assert s['closed']==1
    run(go())

def test_token_rotation_reconnects_and_does_not_reuse_old_session(session):
    reader,s=session
    async def go():
        await reader.read('get_accounts',{},persist=False);s['token']='refreshed'
        await reader.read('get_accounts',{},persist=False)
        assert s['opens']==2 and s['closed']==1
        await reader.close();assert s['closed']==2
    run(go())

def test_failed_write_never_retries_automatically(session):
    reader,s=session;writer=RobinhoodOptionTransport(reader,lambda *_:True)
    async def go():
        s['error']=True
        with pytest.raises(TimeoutError):await writer.call('place_option_order',{'ref_id':'original'})
        assert s['calls']==[('place_option_order',{'ref_id':'original'})] and s['closed']==1
        s['error']=False
        await reader.read('get_accounts',{},persist=False)
        assert s['opens']==2
        await reader.close()
    run(go())

@pytest.mark.parametrize('when',['before_connection','during_initialization','warm_session'])
def test_owner_permit_is_checked_on_every_write(session,when):
    reader,s=session;armed=[True];writer=RobinhoodOptionTransport(reader,lambda *_:armed[0])
    async def go():
        if when=='warm_session':await reader.read('get_accounts',{},persist=False)
        if when=='during_initialization':s['initialized']=lambda:armed.__setitem__(0,False)
        else:armed[0]=False
        with pytest.raises(ValueError,match='disarmed|DISARMED'):await writer.call('place_option_order',{})
        assert not any(n=='place_option_order' for n,a in s['calls'])
        await reader.close()
    run(go())

def test_cancelled_call_closes_session_without_retry(session):
    reader,s=session
    async def go():
        s['paused']=asyncio.Event()
        task=asyncio.create_task(reader.read('get_accounts',{},persist=False))
        while not s['calls']:await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert s['closed']==1 and len(s['calls'])==1
    run(go())

def test_new_event_loop_cannot_reuse_closed_session(session):
    reader,s=session
    run(reader.read('get_accounts',{},persist=False))
    run(reader.read('get_accounts',{},persist=False))
    assert s['opens']==s['closed']==2
