"""Durable option execution state machine; independent of inference.

This module never interprets prose. A host supplies a normalized broker adapter and
fresh independent risk checks. Production wiring is deliberately not enabled by
importing this module. Persist every outbound attempt before network I/O.
"""
import asyncio, copy, hashlib, json, time, uuid, threading
from decimal import Decimal, InvalidOperation
from jsonschema import Draft202012Validator

D=Decimal
TERMINAL={'FILLED','CANCELLED','EXPIRED','INVALIDATED','REJECTED','NEEDS_OWNER','DAILY_LOCKED'}
BROKER_OPEN={'queued','confirmed','partially_filled','pending_cancelled','unconfirmed','new'}
BROKER_DONE={'filled','cancelled','rejected','failed','voided'}

def positive(x):
    try:v=D(str(x))
    except (InvalidOperation,ValueError):raise ValueError('invalid decimal')
    if not v.is_finite() or v<=0:raise ValueError('positive finite decimal required')
    return v

def condition_schema():
    return {'anyOf':[{'type':'null'},{'type':'object','additionalProperties':False,
      'required':['symbol','operator','price'], 'properties':{'symbol':{'type':'string','minLength':1},
      'operator':{'enum':['gte','lte']},'price':{'type':'string','pattern':r'^\d+(\.\d+)?$'}}}]}

ORDER_CONTRACT={'type':'object','additionalProperties':False,'properties':{
 'decision_id':{'type':'string','minLength':1,'maxLength':100},
 'plan_id':{'type':'string','minLength':1,'maxLength':100},
 'plan_version':{'type':'integer','minimum':1},
 'account_number':{'type':'string','minLength':1},
 'option_id':{'type':'string','format':'uuid'},
 'symbol':{'type':'string','minLength':1},
 'side':{'enum':['buy','sell']},'position_effect':{'enum':['open','close']},
 'quantity':{'type':'integer','minimum':1},
 'order_type':{'enum':['limit','stop_limit']},
 'limit_prices':{'type':'array','minItems':1,'maxItems':5,'items':{'type':'string','pattern':r'^\d+(\.\d+)?$'}},
 'stop_price':{'anyOf':[{'type':'null'},{'type':'string','pattern':r'^\d+(\.\d+)?$'}]},
 'condition':condition_schema(),'invalidation':condition_schema(),
 'created_at':{'type':'number'},'expires_at':{'type':'number'},
 'replace_after_seconds':{'type':'integer','minimum':10,'maximum':600},
 'max_transport_attempts':{'type':'integer','minimum':1,'maximum':3},
 'position_id':{'anyOf':[{'type':'null'},{'type':'string','minLength':1}]},
 'expected_position_quantity':{'anyOf':[{'type':'null'},{'type':'integer','minimum':1}]},
 'time_in_force':{'const':'gfd'},'market_hours':{'const':'regular_hours'}}}
ORDER_CONTRACT['required']=list(ORDER_CONTRACT['properties'])

def validate_plan(p, now):
    Draft202012Validator(ORDER_CONTRACT).validate(p)
    uuid.UUID(p['option_id'])
    if not 0 <= now-p['created_at'] <=120:raise ValueError('stale or future decision')
    if not now<p['expires_at']<=now+86400:raise ValueError('explicit expiry within 24h required')
    prices=[positive(x) for x in p['limit_prices']]
    if (p['side'],p['position_effect']) not in {('buy','open'),('sell','close')}:raise ValueError('long options only')
    if prices != sorted(prices,reverse=p['side']=='sell') or len(set(prices))!=len(prices):raise ValueError('explicit monotone price ladder required')
    if p['order_type']=='stop_limit':
        stop=positive(p['stop_price'])
        if len(prices)!=1:raise ValueError('stop-limit repricing disabled; one explicit price only')
        if (p['side']=='buy' and stop>prices[0]) or (p['side']=='sell' and stop<prices[0]):raise ValueError('stop/limit prices cross the allowed bound')
    elif p['stop_price'] is not None:raise ValueError('stop price only belongs to stop_limit')
    for c in (p['condition'],p['invalidation']):
        if c:positive(c['price'])
    if p['side']=='sell':
        if not p['position_id'] or not p['expected_position_quantity'] or p['quantity']>p['expected_position_quantity']:raise ValueError('explicit held position and closing quantity required')
    elif p['position_id'] is not None or p['expected_position_quantity'] is not None:raise ValueError('entry cannot reference a closing position')
    return p

def broker_arguments(plan, attempt):
    args={'account_number':plan['account_number'],'legs':[{'option_id':plan['option_id'],
      'side':plan['side'],'position_effect':plan['position_effect'],'ratio_quantity':1}],
      'quantity':str(attempt['quantity']),'type':plan['order_type'],'price':attempt['price'],
      'time_in_force':'gfd','market_hours':'regular_hours','ref_id':attempt['ref_id']}
    if plan['order_type']=='stop_limit':args['stop_price']=plan['stop_price']
    return args

class Journal:
    """Uses existing remote-CAS controller snapshots; no parallel state file."""
    def __init__(self,service):self.service=service
    def read(self):return self.service.execution_view()
    def edit(self,fn,*,archive_before=None):
        def op(c,s):
            c.db.execute('CREATE TABLE IF NOT EXISTS execution_state(id INTEGER PRIMARY KEY,value TEXT NOT NULL)')
            row=c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
            state=json.loads(row[0]) if row else {'mode':'DISARMED','boot_id':None,'orders':{},'decisions':{}}
            before=json.dumps(state,sort_keys=True)
            try:result=fn(state)
            except ValueError as exc:return {'_input_error':str(exc)}
            if archive_before is not None:
                from .request_archive import archive_completed
                archive_completed(c.db,state,archive_before)
            after=json.dumps(state,sort_keys=True,allow_nan=False)
            if before!=after or not row:
                c.db.execute('INSERT INTO execution_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value',(after,))
                c._event('EXECUTION_STATE',{'sha256':hashlib.sha256(after.encode()).hexdigest(),'mode':state['mode'],
                  'orders':{k:v['status'] for k,v in state['orders'].items()}})
            return copy.deepcopy(result)
        result=self.service.scheduler_transaction(op)
        if isinstance(result,dict) and '_input_error' in result:raise ValueError(result['_input_error'])
        return result

class OrderWorker:
    """Broker protocol: snapshot, reconcile, review, place, cancel, readiness.

    reconcile returns {complete: True, order: None|{id, ref_id, state, filled,
    quantity, option_id, side, price}}. None is safe ONLY after complete pagination.
    Snapshot must include observed_at, market_open, account_ok, risk_ok,
    entry_locked, buying_power, max_contracts, max_order_cost, held_quantity,
    position_id, reserved_cost, reserved_close_quantity and underlying_quotes.
    Uncertain/malformed broker data never authorizes a replacement.
    """
    def __init__(self,journal,broker,*,clock=time.time,boot_id=None,timeout=15):
        self.journal=journal;self.broker=broker;self.clock=clock;self.timeout=timeout
        self.lock=threading.Lock();self.boot_id=boot_id or uuid.uuid4().hex
        self.journal.edit(lambda s:s.update(mode='DISARMED',boot_id=self.boot_id))
    def status(self):return self.journal.read()
    def disarm(self):
        def change(s):
            s['mode']='DISARMED';s['control_epoch']=s.get('control_epoch',0)+1
            from .owner_settings import event
            event(s,'DISARMED',self.clock())
            for r in s['orders'].values():
                if r['status'] not in TERMINAL:r['stop_reason']='CANCELLED'
            return {'mode':'DISARMED','message':'New submissions blocked. Existing owned orders will be reconciled/cancelled; positions are not liquidated.'}
        return self.journal.edit(change)
    async def arm(self,expected_epoch=None,state_update=None):
        epoch=self.status().get('control_epoch',0) if expected_epoch is None else expected_epoch
        if not self.lock.acquire(blocking=False):raise ValueError('order worker busy; retry owner arm')
        try:
            readiness=await self._io(self.broker.readiness())
            if readiness.get('ready') is not True:raise ValueError('Not ready: '+', '.join(readiness.get('blockers',['unverified broker runtime'])))
            def change(s):
                if s.get('control_epoch',0)!=epoch:raise ValueError('owner disarmed during arm checks')
                if any(r['status'] not in TERMINAL for r in s['orders'].values()):raise ValueError('recover outstanding orders before arming')
                if state_update:state_update(s)
                s['mode']='LIVE_ARMED';return {'mode':s['mode']}
            return self.journal.edit(change)
        finally:self.lock.release()
    def accept(self,plan,state_update=None):
        p=copy.deepcopy(plan)
        fingerprint=hashlib.sha256(json.dumps(p,sort_keys=True,allow_nan=False).encode()).hexdigest()
        existing=self.status()
        old=existing['decisions'].get(p.get('decision_id'))
        if old:
            if old['hash']!=fingerprint:raise ValueError('decision ID reused with different terms')
            return old.get('accepted',existing['orders'][old['plan_id']])
        validate_plan(p,self.clock())
        def change(s):
            old=s['decisions'].get(p['decision_id'])
            if old:
                if old['hash']!=fingerprint:raise ValueError('decision ID reused with different terms')
                return old.get('accepted',s['orders'][old['plan_id']])
            if s['mode']!='LIVE_ARMED':raise ValueError('DISARMED: no live decision accepted')
            previous=s['orders'].get(p['plan_id'])
            if previous:
                if previous['status'] not in TERMINAL:raise ValueError('existing plan must be explicitly cancelled and reconciled before replacement')
                if p['plan_version']!=previous['plan']['plan_version']+1:raise ValueError('stale plan version')
                s.setdefault('plan_history',[]).append(copy.deepcopy(previous))
            elif p['plan_version']!=1:raise ValueError('new plan must start at version 1')
            if any(r['plan']['account_number']==p['account_number'] and r['plan']['option_id']==p['option_id'] and r['status'] not in TERMINAL for r in s['orders'].values()):raise ValueError('contract already has active intent')
            if state_update:state_update(s)
            from .position_updates import order_block
            blocked=order_block(s,p,self.clock())
            if blocked:raise ValueError(blocked)
            r={'plan':p,'status':'WAITING','attempts':[],'filled':0,'price_index':0,'next_at':0,'stop_reason':None,'last_error':None}
            s['orders'][p['plan_id']]=r;s['decisions'][p['decision_id']]={'hash':fingerprint,'plan_id':p['plan_id'],'accepted':copy.deepcopy(r)}
            return r
        return self.journal.edit(change)
    def cancel(self,plan_id):
        return self.journal.edit(lambda s:s['orders'][plan_id].update(stop_reason='CANCELLED'))
    async def _io(self,awaitable):return await asyncio.wait_for(awaitable,timeout=self.timeout)
    def _save(self,key,r):
        def save(s):
            current=s['orders'][key]
            # A concurrent owner disarm/cancel always wins over a network response.
            if current['stop_reason']:r['stop_reason']=current['stop_reason']
            from .position_updates import order_block
            if order_block(s,r['plan'],self.clock()):r['stop_reason']=r['stop_reason'] or 'CANCELLED'
            s['orders'][key]=copy.deepcopy(r)
        self.journal.edit(save)
    def _armed(self):
        s=self.status();return s['mode']=='LIVE_ARMED' and s['boot_id']==self.boot_id and self.journal.service.live_guard()
    def _submission_allowed(self,key,p):
        from .position_updates import order_block
        state=self.status()
        if order_block(state,p,self.clock()):
            self.cancel(key)
            return False
        return (state['mode']=='LIVE_ARMED' and state['boot_id']==self.boot_id
                and self.journal.service.live_guard() and self.clock()<p['expires_at']
                and not state['orders'][key]['stop_reason'])
    @staticmethod
    def _condition(c,snapshot,now):
        if c is None:return True
        q=snapshot['underlying_quotes'].get(c['symbol'])
        if not q or not 0<=now-q['asof']<=15:raise ValueError('missing/stale underlying quote')
        value=positive(q['last']);threshold=positive(c['price'])
        return value>=threshold if c['operator']=='gte' else value<=threshold
    def _checks(self,p,v,now,remaining):
        if v.get('assembly_ok') is False:return 'INVALIDATED'
        if not 0<=now-v['observed_at']<=30:raise ValueError('stale account/risk snapshot')
        if v['account_ok'] is not True:raise ValueError('account unavailable')
        if p['side']=='buy' and v['entry_locked']:return 'DAILY_LOCKED'
        if v['risk_ok'] is not True:raise ValueError('risk unavailable or owner limit exceeded')
        if v.get('profit_ok') is False:raise ValueError('profit-sale net premium no longer positive')
        if v['market_open'] is not True:raise ValueError('market closed')
        if v['contract_ok'] is not True or v['quote_fresh'] is not True:raise ValueError('contract/option quote unavailable')
        if remaining>v['max_contracts']:raise ValueError('owner quantity limit')
        if p['side']=='buy':
            if v['entry_locked']:return 'DAILY_LOCKED'
            cost=positive(p['limit_prices'][-1])*D(100)*remaining
            if cost>positive(v['max_order_cost']) or cost+D(v['reserved_cost'])>D(v['buying_power']):raise ValueError('owner notional/buying-power limit')
        elif v['position_id']!=p['position_id'] or v['held_quantity']!=p['expected_position_quantity']-(p['quantity']-remaining) or v['held_quantity']<remaining+v['reserved_close_quantity']:raise ValueError('closing position changed or reserved')
        if p['invalidation'] and self._condition(p['invalidation'],v,now):return 'INVALIDATED'
        return None
    async def tick(self):
        if not self.lock.acquire(blocking=False):return {'status':'BUSY'}
        try:
            state=self.status()
            for key,r in state['orders'].items():
                if r['status'] in TERMINAL or r['next_at']>self.clock():continue
                try:await self._step(key,r)
                except Exception as exc:
                    # No new ID after an unknown result. Other intents/agent monitoring continue.
                    r['last_error']=type(exc).__name__+': '+str(exc)[:180]
                    r['next_at']=self.clock()+5
                    self._save(key,r)
            return {'status':'MONITORED'}
        finally:self.lock.release()
    async def _cancel(self,key,r,p,a):
        if a.get('cancel_attempts',0)>=3:
            r['last_error']='cancel retry limit; broker order may still be active; reconciliation continues'
            self._save(key,r);return
        a['cancel_attempts']=a.get('cancel_attempts',0)+1
        self._save(key,r)
        await self._io(self.broker.cancel(p,a))
    async def _step(self,key,r):
        p=r['plan'];now=self.clock();a=r['attempts'][-1] if r['attempts'] else None
        from .position_updates import order_block
        if order_block(self.status(),p,self.clock()):r['stop_reason']=r['stop_reason'] or 'CANCELLED'
        if not self._armed():r['stop_reason']=r['stop_reason'] or 'CANCELLED'
        if now>=p['expires_at']:r['stop_reason']=r['stop_reason'] or 'EXPIRED'
        if a and r['stop_reason'] and a['transport_attempts']==0 and not a.get('order_id') and not a.get('approval_id'):
            # Prepared/reviewed only: the durable before-I/O counter proves no submission.
            # Unknown attempted submissions still take the full reconciliation path below.
            r['status']=r['stop_reason'];self._save(key,r);return
        if a and a['transport_attempts']>0:
            if r['stop_reason']:self._save(key,r)
            found=await self._io(self.broker.reconcile(p,a))
            if found.get('complete') is not True and not (found.get('retry_same_ref') is True and a['transport_attempts']>0 and not a.get('order_id') and not a.get('approval_id')):raise ValueError('incomplete reconciliation')
            if found.get('approval_terminal') is True:
                r['status']=r['stop_reason'] or 'CANCELLED';self._save(key,r);return
            order=found['order']
            if order is not None:
                if order['ref_id']!=a['ref_id'] or order['option_id']!=p['option_id'] or order['side']!=p['side'] or order['quantity']!=a['quantity'] or D(order['price'])!=D(a['price']):raise ValueError('broker identity/terms mismatch')
                filled=order['filled']
                if type(filled) is not int or not a['filled']<=filled<=a['quantity']:raise ValueError('invalid or regressed fill quantity')
                if order['state'] not in BROKER_OPEN|BROKER_DONE:raise ValueError('unknown broker state')
                if order['state']=='filled' and filled!=a['quantity']:raise ValueError('inconsistent terminal fill')
                a.update(order_id=order['id'],filled=filled,broker_state=order['state'])
                r['filled']=sum(x['filled'] for x in r['attempts'])
                if r['filled']>p['quantity']:raise ValueError('overfill detected')
                if r['filled']==p['quantity']:
                    r['status']='FILLED';self._save(key,r);return
                if order['state'] in BROKER_DONE:
                    if order['state']!='cancelled' or a.get('cancel_reason')!='REPRICE':
                        if order['state']=='cancelled' and not a.get('cancel_reason') and not r['stop_reason']:
                            pid=p.get('position_id') or 'rh-option:'+p['option_id']
                            self.journal.edit(lambda state:state.setdefault('manual_management_holds',{}).__setitem__(pid,
                                {'active':True,'at':now,'plan_id':key,'reason':'Broker cancellation outside the controller; do not recreate automatically'}))
                        r['status']=r['stop_reason'] or ('REJECTED' if order['state'] in {'rejected','failed','voided'} else 'CANCELLED')
                        self._save(key,r);return
                    if r['stop_reason']:r['status']=r['stop_reason'];self._save(key,r);return
                    if not a.get('replacement_consumed'):
                        r['price_index']+=1;a['replacement_consumed']=True
                    a=None
                else:
                    r['status']='PARTIAL' if r['filled'] else 'WORKING'
            elif a.get('order_id'):
                raise ValueError('previously observed order disappeared; no replacement')
            elif r['stop_reason']:
                r['status']='RECONCILING';self._save(key,r);return
        remaining=p['quantity']-r['filled']
        if not a and r['stop_reason']:
            r['status']=r['stop_reason'];self._save(key,r);return
        # Pending cancel must be reconciled to a terminal state before replacement.
        if a and a.get('cancel_reason'):
            if a.get('order_id'):
                self._save(key,r)
                await self._cancel(key,r,p,a)
            r['next_at']=now+5;self._save(key,r);return
        if r['stop_reason'] and a:
            if a.get('order_id'):
                a['cancel_reason']=r['stop_reason'];r['status']='CANCEL_PENDING';self._save(key,r)
                await self._cancel(key,r,p,a)
            r['next_at']=now+5;self._save(key,r);return
        try:
            v=await self._io(self.broker.snapshot(p,remaining))
            stop=self._checks(p,v,self.clock(),remaining)
        except Exception:
            # Unavailable risk data cancels a known entry; an uncertain submission stays unresolved.
            if a and a.get('order_id') and p['side']=='buy':
                a['cancel_reason']='RISK_UNAVAILABLE';r['stop_reason']='NEEDS_OWNER';self._save(key,r)
                await self._cancel(key,r,p,a)
            raise
        if stop:
            r['stop_reason']=stop;self._save(key,r);return
        reprice=self.status().get('entry_repricing',{}).get(p['decision_id']) if p['side']=='buy' else None
        price_now=None
        if reprice:
            from .entry_repricing import fresh_price
            price_now=fresh_price(reprice,v.get('option_quote'),p['option_id'],self.clock())
            r['market_progress']={'ask':v['option_quote']['ask'],'quote_at':v['option_quote']['asof'],
                'limit':a['price'] if a else p['limit_prices'][0],'ceiling':reprice['ceiling'],
                'state':'PRICE_ABOVE_CEILING' if price_now is None else 'WITHIN_CEILING'}
        if a and a.get('order_id'):
            eligible=not reprice or (price_now is not None and D(price_now)>D(a['price']))
            if eligible and now-a['started_at']>=p['replace_after_seconds'] and r['price_index']+1<len(p['limit_prices']):
                a['cancel_reason']='REPRICE';r['status']='CANCEL_PENDING';self._save(key,r)
                await self._cancel(key,r,p,a)
            r['next_at']=now+5;self._save(key,r);return
        if not a:
            if reprice and price_now is None:
                r['status']='WAITING';r['next_at']=now+5;self._save(key,r);return
            if not self._condition(p['condition'],v,self.clock()):
                r['status']='WAITING';r['next_at']=now+5;self._save(key,r);return
            a={'ref_id':str(uuid.uuid4()),'quantity':remaining,'price':price_now if reprice else p['limit_prices'][r['price_index']],
               'filled':0,'started_at':now,'transport_attempts':0,'order_id':None,'cancel_reason':None}
            r['attempts'].append(a)
        if a['transport_attempts']>=p['max_transport_attempts']:
            r['status']='RECONCILING';r['last_error']='transport retry limit; read-only recovery continues';r['next_at']=now+15;self._save(key,r);return
        self._save(key,r) # bind reviewed terms to a durable owned attempt before broker access
        a.setdefault('timing',{})['review_started_at']=self.clock()
        review=await self._io(self.broker.review(p,a))
        a['timing']['review_finished_at']=self.clock()
        if review.get('approved') is not True or review.get('alerts') or review.get('customer_approval_required') is not False:
            # Never acknowledge a broker alert or fabricate customer approval.
            if not a['transport_attempts']:r['status']='NEEDS_OWNER'
            else:r['status']='RECONCILING'
            r['last_error']='broker review or customer approval required';r['next_at']=now+15;self._save(key,r);return
        # Recheck after network latency; owner disarm/expiry can happen during review.
        if not self._submission_allowed(key,p):return
        a['timing']['final_checks_started_at']=self.clock()
        v=await self._io(self.broker.snapshot(p,remaining))
        a['timing']['final_checks_finished_at']=self.clock()
        if v.get('option_quote'):a['final_quote']=copy.deepcopy(v['option_quote'])
        if not self._condition(p['condition'],v,self.clock()):
            r['next_at']=self.clock()+5;self._save(key,r);return
        stop=self._checks(p,v,self.clock(),remaining)
        if stop:r['stop_reason']=stop;self._save(key,r);return
        if not self._submission_allowed(key,p):return
        a['transport_attempts']+=1;r['status']='SUBMITTING';r['next_at']=self.clock()+5
        self._save(key,r) # durable remote acknowledgment BEFORE place
        if not self._submission_allowed(key,p):return
        # Place response is not fill truth. Next tick reconciles by the same persisted ref_id.
        a['timing']['place_started_at']=self.clock()
        response=await self._io(self.broker.place(p,a))
        a['timing']['place_finished_at']=self.clock()
        if isinstance(response,dict) and isinstance(response.get('order_id'),str):
            a['order_id']=response['order_id']
        if isinstance(response,dict) and isinstance(response.get('approval_id'),str):
            a['approval_id']=response['approval_id'];r['last_error']='Broker approval pending; do not resubmit'
        r['status']='RECONCILING';self._save(key,r)
    async def run(self):
        while True:
            try:await asyncio.to_thread(lambda:asyncio.run(self.tick()))
            except Exception:pass # storage failure prohibits broker calls; host exposes readiness
            await asyncio.sleep(1)
