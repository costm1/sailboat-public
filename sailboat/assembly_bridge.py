"""Trading domain for the installed Assembly engine; existing runtime supplies I/O.

Assembly and core SQLite files are checkpointed as blobs in the EXISTING controller
transaction. Temporary working copies have no authority and no independent lifetime.
No broker, order worker, model, or source-selected Python policy is created here.
"""
import asyncio, copy, hashlib, json, sqlite3, tempfile, zlib
from pathlib import Path
from datetime import datetime
from decimal import Decimal
from sailboat_core import Box, Contribution, Catalog, Recognition, Block, Evidence
from sailboat_core.contracts import digest
from sailboat_assembly import Assembly, AssemblySpec, Piece, Decision
from .controller import canonical
from .market_hours import EASTERN
from .preferred_candidate import candidate_pieces

VERSION = 'qqq-premium-assembly-v1'
ENTRY_POLICY = 'higher-30m-resale-probability-floor40-v2'
PROBABILITY_HORIZONS = {'higher-15m-resale-probability-v1':900, 'higher-30m-resale-probability-v1':1800, ENTRY_POLICY:1800}
SLOTS = ('candidate', 'conditions', 'market', 'controller')


class TradingDomain:
    catalog = Catalog('qqq-premium', VERSION, VERSION,
        {'trading.evidence': {'type': 'object'}}, {'trading.evidence': 'evaluate_candidate'})

    def recognize(self, contribution):
        # Only the host builds annotations, from the existing extractor or readers.
        packet = contribution.annotations['packet']
        return Recognition([Block('evidence', 'trading.evidence', packet,
            [Evidence(0, len(contribution.text), contribution.text)], 'unspecified' if packet['kind']=='analyst' else 'asserted')])


class TradingMatcher:
    matcher_id = 'qqq-contract-evidence'
    version = VERSION

    def match(self, bundle):
        packet = bundle['blocks'][0]['payload']
        return [Piece(slot, row['terms'], ('evidence',))
                for row in packet['candidates'] for slot in SLOTS]


def support(candidate):
    sources, markets = [], []
    for item in candidate['supports']:
        b = item['bundle']; p = b['blocks'][0]['payload']
        for row in p['candidates']:
            if row['terms'] == candidate['value']:
                value = copy.deepcopy(row)
                value.update(source=b['contribution']['source'], at=b['contribution']['observed_at'],
                             message_id=b['contribution']['message_id'])
                (sources if p['kind']=='analyst' else markets).append(value)
    # A source may withdraw ITS own contribution. It cannot revoke owner policy
    # or erase another source's support. Every original claim remains in Assembly.
    withdrawals = {}
    for s in sources:
        if s.get('withdrawn'):withdrawals[s['source']]=max(withdrawals.get(s['source'],-1),s['at'])
    current=[s for s in sources if s['at']>withdrawals.get(s['source'],-1)]
    return current, max(enumerate(markets), key=lambda x:(x[1]['at'],x[0]))[1] if markets else None


class TradingPolicy:
    policy_id = ENTRY_POLICY
    version = VERSION

    def __init__(self,purpose='entry',policy_id=None):
        self.policy_id=policy_id or ENTRY_POLICY
        if purpose=='holding':self.policy_id='existing-held-position-manager-v1'

    def decide(self, slot, candidates, context):
        assessments = {}; eligible = []
        selected = context['slots']['candidate'].get('selected_id') if slot!='candidate' else None
        now = context['spec']['config']['started_at']
        if self.policy_id in PROBABILITY_HORIZONS:
            # Broker lookup is downstream of selection. Later source pieces
            # must participate even if their contract has never been quoted.
            now=max([now]+[s['bundle']['contribution']['observed_at'] for c in candidates for s in c['supports']])
        for c in candidates:
            sources, market = support(c)
            evaluated_at = now if self.policy_id in PROBABILITY_HORIZONS else market['at'] if market else now
            sources = [s for s in sources if 0 <= evaluated_at-s['at'] <= 120 and not s.get('withdrawn')]
            good = [s for s in sources if s['supported'] and not s['restricted']]
            reason = 'No current supported source candidate'
            ok = bool(good)
            if ok:
                reason = 'Named source candidate; analyst preference is not owner authority'
                # Explicit conflicting source requirements remain attached. A PASS
                # or execution disclaimer alone is not a withdrawal or a condition.
                if slot != 'candidate':
                    expected = 'candidate-'+digest({'slot':'candidate','value':c['value']})
                    ok = selected == expected
                    reason = 'Must bind to the selected exact contract'
                if ok and slot in ('conditions','market','controller'):
                    ok = bool(market and market.get('conditions_satisfied'))
                    reason = (market or {}).get('condition_reason','Fresh condition evaluation required')
                if ok and slot in ('market','controller'):
                    ok = bool(market and market.get('valid'))
                    reason = (market or {}).get('reason','Fresh broker identity and quote required')
            assessments[c['id']] = {'status':'ELIGIBLE' if ok else 'PENDING','reason':reason}
            if ok:eligible.append((c, good, market))
        if slot=='candidate' and self.policy_id in PROBABILITY_HORIZONS:
            sides={'call':[],'put':[]}
            for c,good,market in eligible:
                values={Decimal(s['probability']['percent']) for s in good if s.get('probability') and 'percent' in s['probability'] and s['probability']['horizon_seconds']==PROBABILITY_HORIZONS[self.policy_id]}
                if not values and not any(s.get('probability') for s in good):continue
                if len(values)!=1 or any((s.get('probability') or {}).get('error') for s in good):
                    return Decision('CONFLICTED' if len(values)>1 else 'PROPOSED',None,f'Missing or conflicting {PROBABILITY_HORIZONS[self.policy_id]//60}-minute resale probability',assessments)
                if c['value']['symbol']!='QQQ' or c['value']['right'] not in sides:continue
                sides[c['value']['right']].append((c,next(iter(values))))
            if any(len(v)!=1 for v in sides.values()):
                return Decision('CONFLICTED' if any(len(v)>1 for v in sides.values()) else 'PROPOSED',None,
                                'One exact CALL and PUT with explicit probabilities required',assessments)
            call,put=sides['call'][0],sides['put'][0]
            if call[1]==put[1]:return Decision('PROPOSED',None,'Equal resale probabilities: STAY_OUT',assessments)
            winner=call if call[1]>put[1] else put
            floor=context['spec']['config'].get('minimum_probability',40)
            if self.policy_id==ENTRY_POLICY and winner[1]<Decimal(str(floor)):
                return Decision('PROPOSED',None,f'Below {floor:g}% entry floor: STAY_OUT',assessments)
            return Decision('SELECTED',winner[0]['id'],f"Higher {PROBABILITY_HORIZONS[self.policy_id]//60}-minute resale probability: CALL {call[1]}%, PUT {put[1]}%",assessments)
        if slot=='candidate' and len(eligible)>1:
            preferred = [r for r in eligible if any(s['preferred'] for s in r[1])]
            if len(preferred)==1:eligible=preferred
            else:
                # Competing conditional opportunities resolve only when exactly one
                # has satisfied measurable source conditions; no invented ranking.
                triggered=[r for r in eligible if r[2] and r[2].get('conditions_satisfied')
                           and any(s['conditions'] for s in r[1])]
                if len(triggered)==1 and not preferred:eligible=triggered
        if len(eligible)==1:
            c=eligible[0][0]
            return Decision('SELECTED',c['id'],assessments[c['id']]['reason'],assessments)
        return Decision('CONFLICTED' if len(eligible)>1 else ('PROPOSED' if candidates else 'MISSING'),None,
                        'Competing candidates need resolution' if len(eligible)>1 else 'Prerequisites unresolved',assessments)

    def complete(self, selections, context):
        return len(selections)==len(SLOTS) and len({canonical(s['value']) for s in selections.values()})==1


def backup_bytes(db):
    # sqlite backup includes committed WAL pages. Force a standalone rollback-journal
    # header so the blob can be reopened independently after remote recovery.
    with tempfile.TemporaryDirectory(prefix='sailboat-snapshot-') as directory:
        path=Path(directory)/'state.sqlite';dest=sqlite3.connect(path)
        try:db.backup(dest);dest.execute('PRAGMA journal_mode=DELETE')
        finally:dest.close()
        return path.read_bytes()


class AssemblyBridge:
    def __init__(self, execution=None,*,service=None):self.ex=execution;self.service=service or execution.service

    def _operate(self, key, operation):
        def transaction(c, scheduler):
            c.db.execute('CREATE TABLE IF NOT EXISTS trading_assemblies(id TEXT PRIMARY KEY,spec TEXT NOT NULL,core BLOB,ledger BLOB,view TEXT NOT NULL)')
            row=c.db.execute('SELECT spec,core,ledger,view FROM trading_assemblies WHERE id=?',(key,)).fetchone()
            purpose='holding' if key.startswith('held:') else 'entry'
            if row:
                value=json.loads(row[0]);value['slots']=tuple(value['slots']);spec=AssemblySpec(**value)
                policy=TradingPolicy(purpose,spec.policy_id)
            else:
                policy=TradingPolicy(purpose)
                from .owner_settings import settings,revision
                owner_row=c.db.execute("SELECT value FROM execution_state WHERE id=1").fetchone() if c.db.execute("SELECT 1 FROM sqlite_master WHERE name='execution_state'").fetchone() else None
                owner=json.loads(owner_row[0]) if owner_row else {}
                spec=AssemblySpec(key,VERSION,'qqq-premium',VERSION,VERSION,SLOTS,
                    TradingMatcher.matcher_id,VERSION,policy.policy_id,VERSION,
                    dependencies={'conditions':['candidate'],'market':['candidate','conditions'],
                                  'controller':['candidate','conditions','market']},
                    config={'started_at':self.ex.clock(),'source_max_age_seconds':120,'minimum_probability':settings(owner)['minimum_probability'],'owner_settings_revision':revision(owner),
                            'owner_policy':policy.policy_id,'purpose':purpose,'economic_edge_rule':policy.policy_id if purpose=='entry' else 'NOT_DEFINED'})
            with tempfile.TemporaryDirectory(prefix='sailboat-assembly-',dir=self.ex.service.directory) as directory:
                core=Path(directory)/'core.sqlite';ledger=Path(directory)/'assembly.sqlite'
                if row:core.write_bytes(zlib.decompress(row[1]));ledger.write_bytes(zlib.decompress(row[2]))
                with Box(core,clock=self.ex.clock) as box, Assembly(ledger,spec,TradingMatcher(),policy,clock=self.ex.clock) as engine:
                    result=operation(box,engine)
                    view=engine.inspect()
                    projection=zlib.compress(canonical(view).encode())
                    if row and projection==row[3]:return result
                    c.db.execute('INSERT INTO trading_assemblies VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET core=excluded.core,ledger=excluded.ledger,view=excluded.view',
                        (key,canonical(view['spec']),zlib.compress(backup_bytes(box.store.db)),zlib.compress(backup_bytes(engine.db)),projection))
                    c._event('ASSEMBLY_CHECKPOINT',{'id':key,'revision':view['state']['revision'],'status':view['state']['status']})
                    return result
        return self.ex.service.scheduler_transaction(transaction)

    def inspect(self,key):
        service=self.service
        with service.lock:
            if not service.ready:raise ValueError('Controller storage unavailable')
            db=sqlite3.connect(service.db)
            try:
                db.execute('PRAGMA query_only=ON')
                row=db.execute('SELECT view FROM trading_assemblies WHERE id=?',(key,)).fetchone()
                if not row:raise ValueError('Unknown assembly')
                return json.loads(zlib.decompress(row[0]))
            finally:db.close()

    def ingest(self,key,message_id,text,at,source,packet,*,finalize=False):
        def op(box,engine):
            result=box.sort(Contribution(source,message_id,text,at,{'packet':packet}),TradingDomain())
            receipt=box.deliver(result['result_id'],engine)
            if receipt['delivery_status']!='ACKNOWLEDGED':raise ValueError('Assembly ingestion unconfirmed')
            state=engine.inspect()['state']
            snapshot=engine.finalize(expected_revision=state['revision']) if finalize and state['status']=='READY' else None
            return {'receipt':receipt,'state':state,'snapshot':snapshot}
        return self._operate(key,op)

    def invalidate(self,key,candidate_id,reason):
        return self._operate(key,lambda box,engine:engine.invalidate(candidate_id,reason=reason,expected_revision=engine.inspect()['state']['revision']))

    def finalize(self,key):
        return self._operate(key,lambda box,engine:engine.finalize(expected_revision=engine.inspect()['state']['revision']))

    def valid_snapshot(self,ref,now):
        view=self.inspect(ref['assembly_id']);state=view['state']
        if view['spec']['config']['purpose']=='entry':
            from .owner_settings import revision
            if view['spec']['config'].get('owner_settings_revision',0)!=revision(self.service.execution_view()):return False
        if view['spec']['config']['purpose']=='entry' and view['spec']['policy_id']!=ENTRY_POLICY:return False
        snap=next((s for s in view['snapshots'] if s['snapshot_id']==ref['snapshot_id']),None)
        return bool(snap and state['status']=='READY' and state['revision']==snap['revision']
                    and now < ref['expires_at'] and all(not state['candidates'][s['candidate_id']]['invalidated'] for s in snap['selections'].values()))

    def held(self,req,text,facts,assessments):
        """Assemble broker holdings and recognized opinions; existing manager owns exits."""
        records=[];now=self.ex.clock()
        for p in req['context']['positions']:
            if p['kind']!='option' or p['symbol']!='QQQ':continue
            fact=facts[p['position_id']];ins=fact['contract'];quote=fact['quote']
            opinion=next((r for r in assessments if r.get('position_id')==p['position_id']),{})
            recognized=opinion.get('status')=='RECOGNIZED'
            terms={'symbol':p['symbol'],'expiry':p['expiration_date'],'right':ins['type'],'strike':float(ins['strike_price'])}
            row={'terms':terms,'preferred':recognized,'supported':recognized,'restricted':False,'withdrawn':False,
                 'conditions':[],'unresolved':[] if recognized else ['Held opinion unresolved by existing position manager'],
                 'holding':copy.deepcopy(p),'assessment':copy.deepcopy(opinion),'economics':{'target_bid':opinion.get('target_bid')}}
            key='held:'+req['request_id']+':'+p['id']
            self.ingest(key,'analysis',text,now,'scheduled:held-analyst',{'kind':'analyst','candidates':[row]})
            # Reuse the quote freshness tolerance used by the existing reader;
            # ISO microsecond serialization may round a timestamp slightly ahead.
            market={'terms':terms,'valid':-1<=now-quote['asof']<=30,'conditions_satisfied':recognized,
                    'condition_reason':'Existing held-position recognition','reason':'Exact broker holding, cost basis and fresh quote',
                    'holding':copy.deepcopy(p),'contract':ins,'quote':quote}
            self.ingest(key,'position',canonical(market),now,'host:broker',{'kind':'broker','candidates':[market]})
            view=self.inspect(key)
            snapshot=self.finalize(key) if view['state']['status']=='READY' else None
            records.append({'assembly_id':key,'status':view['state']['status'],'snapshot_id':snapshot['snapshot_id'] if snapshot else None,
                            'position_id':p['position_id'],'execution_path':'existing profit manager only'})
        return records

    async def prepare(self,key,sorted_text,at,source,*,recovered=False):
        message_key=key
        def group(state):
            groups=state.setdefault('assembly_message_groups',{})
            if message_key in groups:return groups[message_key]
            current=state.get('assembly_active',{})
            live=not recovered and 0<=self.ex.clock()-at<=120
            sealed=any(ref['assembly_id']==current.get('id') for ref in state.get('assembly_bindings',{}).values())
            source_hash=digest({'source':source,'text':sorted_text['original_text']})
            from .owner_settings import revision
            active=live and current and 0<=self.ex.clock()-current['at']<=120 and current['epoch']==state.get('control_epoch',0) and current['boot']==state['boot_id'] and current.get('owner_settings_revision',0)==revision(state)
            if active and sealed and source_hash in current.get('source_hashes',[]):
                # An identical repeated message cannot disrupt an accepted order.
                # Keep the active group so NEW contradictions/withdrawals still
                # invalidate the snapshot already bound to that order.
                groups[message_key]=message_key
                return message_key
            if not active:
                current={'id':message_key,'at':at,'epoch':state.get('control_epoch',0),'boot':state['boot_id'],'source_hashes':[],'owner_settings_revision':revision(state)}
                if live:state['assembly_active']=current
            if source_hash not in current.setdefault('source_hashes',[]):current['source_hashes'].append(source_hash)
            groups[message_key]=current['id']
            return current['id']
        key=await asyncio.to_thread(self.ex.journal.edit,group)
        rows=candidate_pieces(sorted_text)
        packet={'kind':'analyst','candidates':rows,'extraction':sorted_text}
        await asyncio.to_thread(self.ingest,key,message_key,sorted_text['original_text'],at,source,packet)
        owner=self.ex.worker.status()
        if at<owner.get('owner_settings',{}).get('updated_at',0):
            return {'status':'STALE','reason':'Settings changed after this source; fresh assessment required','assembly_id':key}
        if self.inspect(key)['spec']['policy_id']!=ENTRY_POLICY:
            return {'status':'STALE','reason':'Historical selection policy; fresh source required','assembly_id':key}
        if recovered or not 0 <= self.ex.clock()-at <= 120:
            view=self.inspect(key)
            ids=[c['id'] for c in view['state']['candidates'].values() if not c['invalidated']]
            if ids:
                def invalidate_stale(box,engine):
                    for identity in ids:
                        engine.invalidate(identity,reason='Historical source; never replay as fresh authority',expected_revision=engine.inspect()['state']['revision'])
                await asyncio.to_thread(self._operate,key,invalidate_stale)
            return {'status':'STALE','reason':'Historical source cannot authorize a new entry','assembly_id':key}
        if not rows or not any(r['supported'] for r in rows):
            return {'status':'NO_CANDIDATE','reason':'No supported current source candidate; observations preserved','assembly_id':key}
        return await self.refresh(key)

    async def refresh(self,key):
        from .shared_chat import contract
        from .scheduler import normalize_quotes,timestamp
        view=self.inspect(key);now=self.ex.clock();markets=[]
        probability_policy=view['spec']['policy_id'] in PROBABILITY_HORIZONS
        candidates=[c for c in view['state']['candidates'].values() if c['slot']=='candidate' and not c['invalidated']]
        # Selection belongs to Assembly. Validate only its winner; retain the
        # competing source evidence and never substitute an affordable loser.
        selected=view['state']['slots']['candidate'].get('selected_id')
        comparison_expires=min([s['at']+120 for c in candidates for s in support(c)[0]
            if s.get('probability') and s['supported'] and not s['restricted'] and 0<=now-s['at']<=120],default=now+120)
        if probability_policy and not selected:
            state=view['state'];tied=state['slots']['candidate'].get('reason').endswith(': STAY_OUT')
            return {'status':'STAY_OUT' if tied else 'WAITING_ASSEMBLY','assembly_id':key,
                    'state':{k:copy.deepcopy(state[k]) for k in ('status','revision','slots')},'market_evaluations':[],
                    'reason':state['slots']['candidate'].get('reason') if tied else 'Assembly prerequisites unresolved'}
        for candidate in candidates:
            if probability_policy and candidate['id']!=selected:continue
            sources,_=support(candidate);sources=[s for s in sources if not s.get('withdrawn') and s['supported'] and not s['restricted']]
            terms=candidate['value'];conditions=[];unresolved=[]
            for s in sources:
                conditions.extend(s['conditions']);unresolved.extend(s['unresolved'])
            # The owner selected unconditional resale probabilities at the configured horizon. Narrative
            # waiting preferences cannot introduce a separate entry threshold.
            # A conditional probability claim is rejected by the selection policy.
            if probability_policy:conditions=[];unresolved=[]
            conditions=list({canonical(v):v for v in conditions}.values())
            item={'terms':terms,'valid':False,'conditions':conditions,'conditions_satisfied':False,
                  'condition_reason':'Conditions not evaluated','reason':'Broker facts unavailable','expires_at':min([s['at']+120 for s in sources],default=now)}
            if probability_policy:item['expires_at']=min(item['expires_at'],comparison_expires)
            markets.append(item)
            if not sources or any(not 0<=now-s['at']<=120 for s in sources):
                self.invalidate(key,candidate['id'],'Source evidence expired or withdrawn');continue
            if terms['symbol']!='QQQ' or terms['expiry']!=datetime.fromtimestamp(now,EASTERN).date().isoformat():
                item['reason']='Only the actual QQQ 0DTE expiration is in scope';continue
            try:
                if unresolved: item['condition_reason']='; '.join(unresolved)
                elif conditions:
                    observation=await self.ex.broker.readonly.read('get_equity_quotes',{'symbols':sorted({c['symbol'] for c in conditions})},persist=False)
                    quotes={q['symbol']:{'last':str(q['last']),'asof':q['timestamp']} for q in normalize_quotes(observation,sorted({c['symbol'] for c in conditions}),now,max_age=15)}
                    item['underlying_quotes']=quotes
                    item['conditions_satisfied']=all(condition_holds(c,quotes,now) for c in conditions)
                    item['condition_reason']='Measurable source thresholds satisfied' if item['conditions_satisfied'] else 'Source threshold not satisfied'
                else:item.update(conditions_satisfied=True,condition_reason='No market prerequisites stated')
                ins,quote=await contract(self.ex,terms['symbol'],{'kind':'option',**{k:terms[k] for k in ('right','strike','expiry')}})
                item.update(contract=ins,quote=quote,valid=True,reason='Exact broker identity and fresh executable bid/ask')
                item['expires_at']=min(item['expires_at'],timestamp(ins['sellout_datetime']))
                item['economics']={'fresh_bid':quote['bid'],'fresh_ask':quote['ask'],
                    'spread_cost_per_contract':str((Decimal(quote['ask'])-Decimal(quote['bid']))*100),
                    'source_forecasts':[s.get('economics',{}) for s in sources],
                    'fees':'Existing order review must verify opening fees; closing fees unresolved until supported',
                    'economic_edge_rule':self.inspect(key)['spec']['policy_id'],'favorable_economics_established':False,
                    'resale_probabilities':[s.get('probability') for s in sources],
                    'policy_scope':'Owner probability comparison; estimates are not verified profitability'}
            except (ValueError,KeyError,TypeError) as exc:item['reason']=str(exc)[:300]
        text=canonical({'evaluated_at':now,'contracts':[m['terms'] for m in markets], 'kind':'Host broker validation'})
        packet={'kind':'broker','candidates':markets}
        outcome=await asyncio.to_thread(self.ingest,key,'broker:'+digest({'text':text,'packet':packet}),text,now,'host:broker',packet,finalize=True)
        view=self.inspect(key);state=view['state']
        if state['status']!='READY':
            expired=not candidates or (bool(markets) and all(m['expires_at']<=now for m in markets))
            # Full provenance stays in the durable engine. Do not replicate every
            # support bundle into hot worker state on each condition poll.
            summary={k:copy.deepcopy(state[k]) for k in ('status','revision','slots')}
            tied=state['slots']['candidate'].get('reason').endswith(': STAY_OUT')
            return {'status':'STALE' if expired else 'STAY_OUT' if tied else 'WAITING_ASSEMBLY','assembly_id':key,'state':summary,'market_evaluations':markets,'reason':state['slots']['candidate'].get('reason') if tied else 'Assembly prerequisites unresolved'}
        snapshot=outcome['snapshot'];terms=snapshot['selections']['candidate']['value']
        selected=next(m for m in markets if m['terms']==terms)
        return {'status':'READY','assembly_id':key,'snapshot_id':snapshot['snapshot_id'],
                'revision':snapshot['revision'],'terms':terms,'market':selected,
                'reference':{'assembly_id':key,'snapshot_id':snapshot['snapshot_id'],'expires_at':selected['expires_at'],
                             'conditions':selected['conditions']}}


def condition_holds(c,quotes,now):
    q=quotes.get(c['symbol'])
    if not q or not -1<=now-q['asof']<=30:return False
    price=Decimal(q['last']);threshold=Decimal(str(c['price']))
    return {'gt':price>threshold,'lt':price<threshold,'gte':price>=threshold,'lte':price<=threshold}[c['op']]
