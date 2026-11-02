OWNER_SUB='synthetic-owner-subject'
import json
from urllib.parse import urlsplit,parse_qs
import pytest
from starlette.testclient import TestClient
from sailboat.cloud_server import make_app
from sailboat.owner_panel import OwnerPanel,SESSION,FLOW,hashed
from test_cloud import Store,KEYS
from sailboat.cloud_store import DurableController

BASE='https://sailboat.test'
class FakeExecution:
 def __init__(self):self.mode='DISARMED';self.arms=0;self.stops=0;self.fail=False
 def status(self):return {'mode':self.mode,'live_ready':True,'blockers':[]}
 async def refresh(self):return self.status()
 async def arm(self):
  self.arms+=1
  if self.fail:raise ValueError('LIVE_NOT_READY')
  self.mode='LIVE_ARMED';return self.status()
 def disarm(self):self.stops+=1;self.mode='DISARMED';return self.status()
@pytest.fixture
def panel(tmp_path):
 service=DurableController(Store(tmp_path),tmp_path/'db');ex=FakeExecution();clock=[1000.]
 p=OwnerPanel(ex,None,service,'test-client','test-secret',owner_sub=OWNER_SUB,base=BASE,clock=lambda:clock[0])
 calls=[]
 async def identity(code,verifier):calls.append((code,verifier));return {'sub':OWNER_SUB,'preferred_username':'synthetic-owner'}
 p.identity=identity
 app=make_app(service,KEYS,['sailboat.test'],execution=ex,owner_panel=p)
 with TestClient(app,base_url=BASE) as c:yield c,p,ex,clock,calls

def signin(c):
 r=c.get('/owner/login',follow_redirects=False);q=parse_qs(urlsplit(r.headers['location']).query)
 assert q['scope']==['openid profile'] and q['code_challenge_method']==['S256']
 assert q['redirect_uri']==[BASE+'/owner/callback']
 r=c.get('/owner/callback',params={'state':q['state'][0],'code':'test-code'},follow_redirects=False)
 assert r.status_code==303 and r.headers['location']=='/'
 assert 'Secure' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
 return c.get('/owner/view').json()['csrf']

def test_static_page_does_not_reveal_state_or_credentials(panel):
 c,p,x,clock,calls=panel
 r=c.get('/');assert r.status_code==200 and 'test-secret' not in r.text and 'synthetic-owner' not in r.text
 assert 'frame-ancestors' in r.headers['content-security-policy']
 assert c.get('/owner/view').status_code==401
 assert c.post('/owner/action',json={'command':'arm'}).status_code==401
 assert c.get('/owner/view',headers={'Authorization':'Bearer '+'a'*40}).status_code==401
 assert c.get('/owner/action').status_code==405
 assert x.arms==0

def test_owner_signin_read_and_csrf(panel):
 c,p,x,clock,calls=panel;csrf=signin(c)
 assert x.arms==0 and len(calls)==1
 assert c.get('/owner/view').json()['control']['mode']=='DISARMED'
 for h in ({},{'Origin':'https://evil.test','X-Sailboat-CSRF':csrf},{'Origin':BASE,'X-Sailboat-CSRF':'wrong'}):
  assert c.post('/owner/action',json={'command':'arm','confirmation':'ARM LIVE'},headers=h).status_code==403
 h={'Origin':BASE,'X-Sailboat-CSRF':csrf}
 assert c.post('/owner/action',json={'command':'arm'},headers=h).status_code==400
 assert c.post('/owner/action',json={'command':'arm','confirmation':'ARM LIVE'},headers=h).json()['mode']=='LIVE_ARMED'
 assert x.arms==1
 assert c.post('/owner/action',json={'command':'disarm'},headers=h).json()['mode']=='DISARMED'
 assert x.stops==1

def test_wrong_owner_even_with_same_username(panel):
 c,p,x,clock,calls=panel
 async def other(code,verifier):return {'sub':'other-user','preferred_username':'synthetic-owner'}
 p.identity=other;r=c.get('/owner/login',follow_redirects=False);state=parse_qs(urlsplit(r.headers['location']).query)['state'][0]
 assert c.get('/owner/callback',params={'state':state,'code':'x'}).status_code==403
 assert c.get('/owner/view').status_code==401 and x.arms==0

def test_state_binding_replay_and_expiry(panel):
 c,p,x,clock,calls=panel
 r=c.get('/owner/login',follow_redirects=False);state=parse_qs(urlsplit(r.headers['location']).query)['state'][0]
 assert c.get('/owner/callback',params={'state':'wrong','code':'x'}).status_code==400
 clock[0]+=601
 assert c.get('/owner/callback',params={'state':state,'code':'x'}).status_code==400
 assert not calls
 signin(c)
 assert c.get('/owner/callback',params={'state':state,'code':'x'}).status_code==400
 clock[0]+=3601
 assert c.get('/owner/view').status_code==401
 assert c.post('/owner/action',json={'command':'arm','confirmation':'ARM LIVE'}).status_code==401

def test_readiness_storage_and_logout(panel):
 c,p,x,clock,calls=panel;csrf=signin(c);h={'Origin':BASE,'X-Sailboat-CSRF':csrf}
 x.fail=True
 assert c.post('/owner/action',json={'command':'arm','confirmation':'ARM LIVE'},headers=h).status_code==409
 assert x.mode=='DISARMED'
 p.service.ready=False
 assert c.post('/owner/action',json={'command':'arm','confirmation':'ARM LIVE'},headers=h).status_code==503
 assert c.post('/owner/logout',json={},headers=h).json()['controller_unchanged']
 assert c.get('/owner/view').status_code==401

def test_restart_forgets_sessions(panel):
 c,p,x,clock,calls=panel;signin(c)
 restored=OwnerPanel(x,None,p.service,'test-client','test-secret',base=BASE)
 assert not restored.sessions and not restored.pending
 assert p.sessions
 p.sessions.clear()
 assert c.get('/owner/view').status_code==401

def test_latest_receipt_ignores_unsubmitted_requests_and_preserves_state():
 import copy
 from sailboat.owner_panel import latest_decision
 state={'live_requests':{
  'earlier':{'receipt':{'status':'ACCEPTED','received_at':100,'result':{'status':'HOLD'}}},
  'recent':{'receipt':{'request_id':'recent','status':'ACCEPTED','received_at':200,'result':{'status':'OBSERVE'}}},
  'new-context':{'payload':{'created_at':300},'receipt':None}},'agent_assessments':[]}
 original=copy.deepcopy(state);v=latest_decision(state)
 assert v['request_id']=='recent' and v['action']=='observe' and v['received_at']==200
 assert v['receipt']['result']=={'status':'OBSERVE'} and state==original
 assert latest_decision({'live_requests':{'context':{'receipt':None}}}) is None
 state['agent_assessments']=[{'request_id':'scheduled','at':250,'action':'execute','result':{'status':'PREVIEW_BLOCKED','action':'execute','blocker':'stale quote','broker_submission':False}}]
 v=latest_decision(state)
 assert v['request_id']=='scheduled' and v['source']=='Scheduled assessment'
 assert v['receipt']['status']=='PREVIEW_BLOCKED' and v['receipt']['broker_submission'] is False
 state['live_requests']['filled']={'receipt':{'status':'ACCEPTED','received_at':400,'result':{'status':'WAITING','filled':0,'plan':{'account_number':'secret-account'}}}}
 v=latest_decision(state)
 assert v['action']=='execute' and v['receipt']['result']=={'status':'WAITING','filled':0}
 assert 'secret-account' not in json.dumps(v)


def test_latest_decision_requires_owner_session(panel):
 from types import SimpleNamespace
 c,p,x,clock,calls=panel
 state={'live_requests':{'owner-receipt':{'receipt':{'status':'ACCEPTED','received_at':950,'result':{'status':'OBSERVE'}}}}}
 x.worker=SimpleNamespace(status=lambda:state)
 assert c.get('/owner/view').status_code==401
 signin(c);view=c.get('/owner/view').json()
 assert view['latest_decision']['request_id']=='owner-receipt'
 assert view['latest_decision']['action']=='observe' and x.arms==0 and x.stops==0
 assert 'owner-receipt' not in c.get('/').text


def test_response_history_deduplicates_caps_and_preserves_saved_text():
 import copy
 from sailboat.owner_panel import recent_decisions
 state={'live_requests':{},'agent_assessments':[]}
 for i in range(5):
  rid=f'r{i}';receipt={'request_id':rid,'status':'ACCEPTED','received_at':i,'result':{'status':'OBSERVE'}}
  state['live_requests'][rid]={'receipt':receipt}
  state['agent_assessments'].append({'request_id':rid,'at':i+.1,'action':'observe','result':receipt,'reasoning':f'analysis {i}','uncertainty':'limited data','missing_information':['quote']})
 state['agent_assessments'][-1]['reasoning']='<img src=x onerror=alert(1)> literal agent text'
 state['live_requests']['context-only']={'receipt':None,'payload':{'created_at':999}}
 before=copy.deepcopy(state);history=recent_decisions(state)
 assert [x['request_id'] for x in history]==['r4','r3','r2']
 assert history[0]['reasoning']=='<img src=x onerror=alert(1)> literal agent text'
 assert history[0]['source']=='Scheduled assessment' and history[0]['missing_information']==['quote']
 assert state==before
 state['live_requests']['manual']={'receipt':{'received_at':10,'status':'ACCEPTED','result':{'status':'HOLD'}}}
 assert recent_decisions(state)[0]['reasoning'] is None


def test_response_text_requires_owner_session(panel):
 from types import SimpleNamespace
 c,p,x,clock,calls=panel
 x.worker=SimpleNamespace(status=lambda:{'agent_assessments':[{'request_id':'private','at':950,'action':'hold','reasoning':'owner only thesis','result':{'status':'PREVIEW_VALIDATED'}}]})
 assert c.get('/owner/view').status_code==401
 assert 'owner only thesis' not in c.get('/').text
 signin(c);v=c.get('/owner/view').json()
 assert v['recent_decisions'][0]['reasoning']=='owner only thesis'
 assert v['latest_decision']==v['recent_decisions'][0] and x.arms==0 and x.stops==0


def test_direct_dashboard_shell_keeps_controls_locked_before_owner_verification(panel):
 c,p,x,clock,calls=panel;html=c.get('/').text
 assert 'id="gate"' not in html and 'Open owner controls' not in html
 assert '<section id="panel">' in html
 assert '<button id="arm" disabled>' in html and '<button id="disarm" disabled>' in html
 assert 'test-secret' not in html
 assert c.get('/owner/view').status_code==401
 assert c.post('/owner/action',json={'command':'disarm'}).status_code==401
 assert x.arms==0 and x.stops==0

def test_shadow_dashboard_asset_and_owner_only_projection(panel):
 c,p,x,clock,calls=panel
 assert c.get('/owner/view').status_code==401
 assert 'renderShadow' in c.get('/owner/shadow.js').text
 signin(c);assert c.get('/owner/view').json()['shadow_decisions']==[]
 assert x.arms==0 and x.stops==0
