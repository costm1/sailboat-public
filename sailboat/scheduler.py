"""Small durable wake-up loop. External inference never holds the controller lock."""
import asyncio,copy,hashlib,json,math,time
from datetime import datetime,timezone
import anyio
from .controller import canonical
from .cloud_store import StorageUnavailable

DEFAULT_REQUEST=('Assess the supplied fresh market observations and existing simulated plans/positions. '
 'Return one structured decision using the supplied contract. Update existing plans rather than duplicate them. '
 'You supply trading decisions and explicit conditions; Sailboat supplies no strategy. '
 'A drop in position value alone does not authorize liquidation. A bearish assessment alone is not a sell instruction. '
 'Choose hold, an explicit exit proposal, or a plan revision using the existing structured interface and authorized scope. '
 'An owner daily entry lock blocks new risk, not monitoring or already authorized exits. '
 'No strategy, contract or quantity is implied by this scheduled wake-up. If critical instructions or '
 'information are missing, observe or request clarification. Never request live execution or change owner controls.')

def timestamp(value):
    if not isinstance(value,str):raise ValueError('missing venue timestamp')
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timestamp needs timezone')
    return d.timestamp()

def normalize_quotes(observation,symbols,now,max_age=30):
    """Use broker venue times, never request arrival as a substitute for freshness."""
    if observation.get('source')!='https://agent.robinhood.com/mcp/trading' or observation.get('tool')!='get_equity_quotes':
        raise ValueError('untrusted quote source')
    result=observation['result']
    if result.get('isError'):raise ValueError('broker read failed')
    rows=result.get('structuredContent',{}).get('data',{}).get('results',[])
    quotes={}
    for row in rows:
        q=row['quote'];symbol=q['symbol']
        if symbol not in symbols:continue
        if symbol in quotes or q.get('state')!='active':raise ValueError('duplicate or inactive quote')
        trades=[(timestamp(q['venue_last_trade_time']),float(q['last_trade_price']))]
        if q.get('last_non_reg_trade_price') is not None:
            trades.append((timestamp(q['venue_last_non_reg_trade_time']),float(q['last_non_reg_trade_price'])))
        trade_time,last=max(trades)
        ts=min(timestamp(q['venue_bid_time']),timestamp(q['venue_ask_time']),trade_time)
        bid,ask=float(q['bid_price']),float(q['ask_price'])
        if not all(math.isfinite(v) and v>0 for v in (bid,ask,last,ts)) or bid>ask or not -1<=now-ts<=max_age:
            raise ValueError('stale, crossed or invalid quote')
        quotes[symbol]={'symbol':symbol,'bid':bid,'ask':ask,'last':last,'timestamp':ts}
    if set(quotes)!=set(symbols):raise ValueError('missing requested quotes')
    return list(quotes.values())

def apply_quotes(c,quotes):
    for q in quotes:
        prior=c.db.execute('SELECT ts FROM quotes WHERE symbol=?',(q['symbol'],)).fetchone()
        if not prior or q['timestamp']>prior[0]:c.market_snapshot(**q)

def event_keys(context,now):
    """Stable event identity; owner catalog supplies actual expiry, never guessed contracts."""
    keys=[]
    positions=sorted((p['id'],p['instrument'],p['quantity']) for p in context['positions'])
    keys.append('positions:'+hashlib.sha256(canonical(positions).encode()).hexdigest())
    quotes={q['symbol']:q for q in context['quotes'] if q['fresh']}
    for p in context.get('plans',[]):
        if p['status'] not in ('WATCHING','PROPOSED','ACTIVE'):continue
        b=p.get('content',{})
        if isinstance(b,str):b=json.loads(b)
        cond=b.get('condition')
        if not cond:continue
        q=quotes.get(cond['symbol'])
        if not q:continue
        op,v,threshold=cond['op'],q['last'],cond['price'];prev=q['previous']
        hit={'gt':v>threshold,'gte':v>=threshold,'lt':v<threshold,'lte':v<=threshold,
             'cross_above':prev is not None and prev<=threshold<v,
             'cross_below':prev is not None and prev>=threshold>v}[op]
        if hit:keys.append('price:'+p['id']+':'+str(p['version']))
    instruments={i['id']:i for i in context['instruments']}
    for p in context['positions']:
        i=instruments[p['instrument']]
        if i['kind']!='option':continue
        d=json.loads(i['details']) if isinstance(i['details'],str) else i['details']
        expiry=d.get('expiry')
        try:
            # Date-only catalog entries wake on that date; no assumed settlement time.
            if isinstance(expiry,str) and len(expiry)==10:
                if datetime.fromtimestamp(now,timezone.utc).date().isoformat()==expiry:keys.append('expiry:'+p['id']+':'+expiry)
            elif 0<=timestamp(expiry)-now<=3600:keys.append('expiry:'+p['id']+':'+expiry)
        except (ValueError,TypeError):pass
    return keys

class Scheduler:
    def __init__(self,service,broker,provider,interval=300,symbols=('QQQ','SPY','SMH'),clock=time.time,operator_request=DEFAULT_REQUEST,max_cycles=0,not_before=0,stop_at=0,event_wakeups=True,market_hours=None,execution=None,analysis_only=True,held_interval=300):
        if interval<60:raise ValueError('minimum assessment interval is 60 seconds')
        if held_interval<60:raise ValueError('minimum held assessment interval is 60 seconds')
        if not symbols:raise ValueError('configure at least one market symbol')
        self.service,self.broker,self.provider=service,broker,provider
        self.interval,self.symbols,self.clock=interval,tuple(symbols),clock
        self.held_interval=held_interval
        self.operator_request,self.max_cycles=operator_request,max_cycles
        if not all(math.isfinite(v) and v>=0 for v in (not_before,stop_at)):raise ValueError('invalid assessment window')
        if stop_at and stop_at<=not_before:raise ValueError('empty assessment window')
        self.not_before,self.stop_at,self.event_wakeups=not_before,stop_at,event_wakeups
        self.market_hours=market_hours;self.execution=execution;self.analysis_only=analysis_only
        self.cycle_lock=asyncio.Lock();self.last_error=None;self.polls=0
    async def tx(self,fn):return await anyio.to_thread.run_sync(self.service.scheduler_transaction,fn)
    async def view(self):return await anyio.to_thread.run_sync(self.service.scheduler_view)
    def period(self,managing=False):
        return self.held_interval if self.execution is not None and self.analysis_only and managing else self.interval
    def sync_health_schedule(self,c,s,active=None):
        if self.execution is None:return
        row=c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
        if not row:return
        from .adaptive_profit import sync_health_schedule
        execution=json.loads(row[0])
        sync_health_schedule(execution,s.get('next_due',self.clock()),s.get('effective_interval_seconds',self.interval),active)
        c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(execution),))
    def finish(self,c,s,status,**extra):
        active=s.pop('active',{})
        managing=active.get('managing_positions',s.get('last_cycle',{}).get('managing_positions',False))
        period=self.period(managing)
        result={'cycle_id':active.get('cycle_id'),'request_id':active.get('request_id'),'status':status,'started_at':active.get('started_at'),'finished_at':self.clock(),'managing_positions':managing,**extra}
        from .analysis_log import finish as finish_analysis
        finish_analysis(c,active,result)
        s['last_cycle']=result;s['cycles']=s.get('cycles',0)+1;s['next_due']=max(self.clock(),active.get('started_at',self.clock())+period)
        s['effective_interval_seconds']=period
        if self.execution is not None and status!='MARKET_CLOSED_NO_ACTION':
            row=c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
            if row:
                from .adaptive_profit import record_health
                execution=json.loads(row[0])
                # Analysis availability and processing completion are distinct.
                # Keep held fallback semantics while exposing actual pipeline failures.
                processing_ok=status in ('ANALYSIS_RECEIVED','COMPLETED','RECOVERED_ACCEPTED_DECISION')
                previous=execution.get('processing_health',{})
                execution['processing_health']={'status':status,'ok':processing_ok,
                    'consecutive_failures':0 if processing_ok else previous.get('consecutive_failures',0)+1,
                    'finished_at':self.clock(),'request_id':active.get('request_id'),
                    'error':extra.get('error'),'detail':extra.get('detail')}
                valid=bool(active.get('analysis_received')) or (status=='ANALYSIS_RECEIVED' and bool(extra.get('assessment',{}).get('analysis','').strip())) or bool(extra.get('assessment',{}).get('decision')) or status=='RECOVERED_ACCEPTED_DECISION'
                assessment=extra.get('assessment') or active.get('assessment') or {}
                held_blocks=assessment.get('held_position_blocks',[])
                if active.get('managing_positions') or held_blocks:
                    # Receiving text is not a usable held-position assessment.
                    # Unrecognized/blocked opinions must not suppress the existing
                    # two-failure fallback. Valid HOLD remains healthy, even with
                    # no numeric target. Recovery never replays saved source text.
                    receipt=extra.get('receipt') or {}
                    valid=bool(held_blocks) and all(b.get('status')=='RECOGNIZED' for b in held_blocks) and receipt.get('status') in ('ACCEPTED','PREVIEW_VALIDATED')
                    if status=='RECOVERED_ACCEPTED_DECISION':
                        valid=active.get('request_id') in execution.get('position_block_commits',{})
                record_health(execution,active.get('cycle_id'),valid,self.clock(),s['next_due'],period,started_at=active.get('started_at'))
                c.db.execute('UPDATE execution_state SET value=? WHERE id=1',(canonical(execution),))
        c._event('SCHEDULER_RESULT',result)
        return result
    async def recover(self):
        def recovery(c,s):
            s.setdefault('next_due',self.clock());s.setdefault('cycles',0)
            if s.get('interval_seconds')!=self.interval or s.get('held_interval_seconds')!=self.held_interval:
                last=s.get('last_cycle') or {}
                period=self.period(last.get('managing_positions',False))
                s['next_due']=max(self.clock(),(last.get('started_at') or self.clock())+period) if last else self.clock()
                s['interval_seconds']=self.interval
                s['held_interval_seconds']=self.held_interval;s['effective_interval_seconds']=period
                self.sync_health_schedule(c,s)
            active=s.get('active')
            if active:
                if active.get('route') in ('sailboat_order_agent_v1','qqq_options_analyst_v1'):
                    # Boot is DISARMED. Never turn a saved preview/response into a fresh live order.
                    row=c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
                    execution=json.loads(row[0]) if row else {}
                    receipt=execution.get('live_requests',{}).get(active.get('request_id'),{}).get('receipt')
                    accepted=execution.get('decisions',{}).get(active.get('decision_id')) or execution.get('position_block_commits',{}).get(active.get('request_id'))
                    self.finish(c,s,'RECOVERED_ACCEPTED_DECISION' if receipt or accepted else 'INTERRUPTED_NO_ACTION',receipt=receipt,
                        reason='Boot disarmed; existing accepted intents reconcile independently. No agent response replayed.',usage=active.get('usage'))
                elif self.market_hours and not self.market_hours.status(self.clock())['open']:
                    self.finish(c,s,'MARKET_CLOSED_NO_ACTION',usage=active.get('usage'))
                elif active['phase']=='RESPONSE_READY':
                    receipt=c.respond(canonical(active['response']))
                    self.finish(c,s,'RECOVERED',receipt=receipt,usage=active.get('usage'))
                else:self.finish(c,s,'INTERRUPTED_NO_ACTION',reason='Restart during an unconfirmed provider request; not replayed')
            s['boot_id']=self.service.boot_id
        await self.tx(recovery)
    async def fetch(self):
        _,context=await self.view()
        instruments={i['id']:i for i in context['instruments']}
        symbols=set(self.symbols)
        for position in context['positions']:
            inst=instruments.get(position['instrument'])
            if inst and inst['kind']=='stock':symbols.add(inst['symbol'])
        symbols=sorted(symbols)
        obs=await self.broker.read('get_equity_quotes',{'symbols':symbols},persist=False)
        return obs,normalize_quotes(obs,symbols,self.clock())
    async def cycle(self):
        if self.cycle_lock.locked():return {'status':'BUSY'}
        async with self.cycle_lock:
            market=self.market_hours.status(self.clock()) if self.market_hours else None
            if market and not market["open"]:return {"status":market["reason"],"next_open":market["next_open"]}
            s,_=await self.view()
            # Existing broker/profit monitoring supplies fresh holdings without a
            # new inference or account fetch on each scheduler poll.
            book=getattr(getattr(self.execution,'broker',None),'view',None)
            if book and 0<=self.clock()-book['observed_at']<=30 and s.get('last_cycle'):
                managing=any(p.get('kind')=='option' and p.get('symbol')=='QQQ' and p.get('quantity') for p in book['positions'])
                period=self.period(managing)
                if s.get('effective_interval_seconds')!=period:
                    def switch(c,state):
                        state['effective_interval_seconds']=period
                        state['next_due']=max(self.clock(),(state['last_cycle'].get('started_at') or self.clock())+period)
                        self.sync_health_schedule(c,state)
                    await self.tx(switch);s,_=await self.view()
            if self.execution is not None and self.event_wakeups and not s.get('active'):
                def broker_event(c,st):
                    row=c.db.execute('SELECT value FROM execution_state WHERE id=1').fetchone()
                    execution=json.loads(row[0]) if row else {}
                    observation=execution.get('position_observation',{})
                    version=observation.get('version',0)
                    if version>st.get('broker_position_version',0):
                        st['broker_position_version']=version
                        st['wake_reason']='Broker holding changed; fresh quantity and cost basis'
                        st['next_due']=min(st.get('next_due',self.clock()),max(self.clock(),(st.get('last_cycle',{}).get('started_at') or 0)+60))
                        self.sync_health_schedule(c,st)
                await self.tx(broker_event);s,_=await self.view()
            if self.clock()<self.not_before:return {'status':'BEFORE_ASSESSMENT_WINDOW'}
            if self.stop_at and self.clock()>=self.stop_at:return {'status':'ASSESSMENT_WINDOW_COMPLETE'}
            if s.get('active'):return {'status':'RECOVERY_REQUIRED'}
            if self.max_cycles and s.get('cycles',0)>=self.max_cycles:return {'status':'BOUNDED_TEST_COMPLETE'}
            if self.clock()<s.get('next_due',0):return {'status':'NOT_DUE'}
            # Readiness is journaled BEFORE any agent invocation. No repeated accepted decision.
            def claim(c,s):
                import uuid
                if s.get('active'):raise ValueError('cycle already claimed')
                s['active']={'cycle_id':uuid.uuid4().hex,'phase':'FETCHING','started_at':self.clock(),'reason':s.pop('wake_reason','scheduled')}
                self.sync_health_schedule(c,s,s['active'])
            await self.tx(claim)
            try:
                if self.execution is not None:
                    if self.analysis_only:
                        from .market_analyst import assess
                    else:
                        from .order_schedule import assess # Retained protocol compatibility; not the production analyst role.
                    budget=min(110,self.stop_at-self.clock()) if self.stop_at else 110
                    if market:budget=min(budget,market['close']-self.clock())
                    return await asyncio.wait_for(assess(self,market),timeout=max(0,budget))
                obs,quotes=await self.fetch()
                def prepare(c,s):
                    apply_quotes(c,quotes)
                    # Only normalized public market observations go to the agent. Brokerage balances/account IDs do not.
                    c.db.execute('CREATE TABLE IF NOT EXISTS broker_observations(name TEXT PRIMARY KEY,value TEXT)')
                    c.db.execute('INSERT INTO broker_observations VALUES(?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value',
                      ('scheduler_quotes',canonical({'source':obs['source'],'retrieved_at':obs['retrieved_at'],'quotes':quotes})))
                    request=c.request(self.operator_request,ttl=120,plan_ttl=3600)
                    request=copy.deepcopy(request)
                    request['context']['brokerage_observations']=[{'source':obs['source'],'retrieved_at':obs['retrieved_at'],'quotes':quotes}]
                    request['context']['market_data_origin']='Authenticated Robinhood read-only quotes with venue timestamps; simulated portfolio only'
                    s['active'].update(phase='DISPATCHED',request_id=request['request_id'])
                    return request
                request=await self.tx(prepare)
                remaining=min(95,self.stop_at-self.clock()) if self.stop_at else 95
                if market:remaining=min(remaining,market["close"]-self.clock())
                if remaining<=0:raise TimeoutError('assessment window ended')
                response,usage=await asyncio.wait_for(self.provider.decide(request),timeout=remaining)
                # Durable response allows restart without making a second inference request.
                def save(c,s):s['active'].update(phase='RESPONSE_READY',response=response,usage=usage)
                await self.tx(save)
                def apply(c,s):
                    a=s['active']
                    if self.market_hours and not self.market_hours.status(self.clock())['open']:
                        return self.finish(c,s,'MARKET_CLOSED_NO_ACTION',usage=a['usage'])
                    receipt=c.respond(canonical(a['response']))
                    return self.finish(c,s,'COMPLETED',receipt=receipt,usage=a['usage'],provider=self.provider.name)
                return await self.tx(apply)
            except StorageUnavailable:raise
            except Exception as exc:
                self.last_error=type(exc).__name__
                return await self.tx(lambda c,s:self.finish(c,s,'UNAVAILABLE_NO_ACTION',error=type(exc).__name__,detail=str(exc)[:180],usage=s.get('active',{}).get('usage')))
    async def poll(self):
        """Independent of inference; only fetch extra quotes for an actual monitored plan/order."""
        s,context=await self.view()
        monitored=bool(context['positions'] or context['instructions'] or context['orders'] or any(
            p.get('status') in ('WATCHING','PROPOSED','ACTIVE') and (p.get('content') or {}).get('condition')
            for p in context.get('plans',[]) if isinstance(p.get('content',{}),dict)))
        quotes=[]
        if monitored:_,quotes=await self.fetch()
        keys=event_keys(context,self.clock())
        if not quotes and s.get('event_keys')==keys:return
        def record(c,s):
            apply_quotes(c,quotes);current=event_keys(c._context(),self.clock());old=s.get('event_keys')
            # Initial fingerprint establishes baseline, not a fictitious position change.
            if old is not None:
                new=set(current)-set(old)
                new={k for k in new if k.startswith('positions:') or k not in s.get('seen_events',[])}
                if new and self.event_wakeups:
                    s['wake_reason']=sorted(new);s['next_due']=min(s.get('next_due',self.clock()),max(self.clock(),s.get('last_event_at',0)+60))
                    s['last_event_at']=self.clock();s['seen_events']=(s.get('seen_events',[])+sorted(new))[-512:]
                    self.sync_health_schedule(c,s)
            s['event_keys']=current
        await self.tx(record);self.polls+=1
    async def run(self):
        await self.recover()
        while True:
            if self.service.ready:
                try:await self.cycle()
                except StorageUnavailable:self.last_error='StorageUnavailable'
            await asyncio.sleep(1)
    async def monitor(self):
        while True:
            try:
                if self.service.ready:await self.poll()
            except Exception as exc:self.last_error=type(exc).__name__
            await asyncio.sleep(15)
    async def status(self):
        s,context=await self.view()
        market=self.market_hours.status(self.clock()) if self.market_hours else None
        # Never expose raw provider output, credentials, or request payload via status.
        return {'provider':self.provider.name,'interval_seconds':self.interval,'held_interval_seconds':self.held_interval,'effective_interval_seconds':s.get('effective_interval_seconds',self.interval),'symbols':self.symbols,
          'cycles':s.get('cycles',0),'next_due':s.get('next_due'),'active_phase':s.get('active',{}).get('phase'),
          'active_started_at':s.get('active',{}).get('started_at'),'active_budget_seconds':110,
          'last_cycle':s.get('last_cycle'),'last_error':self.last_error,'monitor_polls':self.polls,
          'market_hours':market,'daily_risk':context.get('daily_risk'),'mode':context['mode'],'emergency_stop':context['emergency_stop'],
          'max_cycles':self.max_cycles,'not_before':self.not_before,'stop_at':self.stop_at,'event_wakeups':self.event_wakeups,'live_execution':('Owner-gated real-account decisions: DISARMED previews only; ARM enables next fresh scheduled live decision' if self.execution else 'This scheduler uses paper decisions only; owner live mode is reported by /execution/status'),
          'agent_role':'market_analyst' if self.execution and self.analysis_only else 'legacy_decision_agent',
          'decision_route':'owner_gated_live' if self.execution else 'paper','execution_mode':self.execution.worker.status()['mode'] if self.execution else context['mode'],
          'hosting':'Runs only while host is awake; free Spaces may sleep'}
