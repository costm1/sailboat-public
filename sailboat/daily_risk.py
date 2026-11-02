"""Owner daily entry lock. No price-based liquidation or model policy decisions."""
import json
from decimal import Decimal
from datetime import datetime
from zoneinfo import ZoneInfo
EASTERN=ZoneInfo('America/New_York')
class DailyRisk:
    def _daily_schema(self):
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS daily_risk_config(id INTEGER PRIMARY KEY CHECK(id=1),max_loss TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS daily_risk_days(day TEXT PRIMARY KEY,baseline TEXT NOT NULL,
            last_equity TEXT NOT NULL,locked INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL);
        """)
    def _daily_limit(self):
        row=self.db.execute('SELECT max_loss FROM daily_risk_config WHERE id=1').fetchone()
        return Decimal(row[0]) if row else None
    def configure_daily_loss_limit(self,amount):
        """Owner-only configuration, never a tool. Existing journals/realized P&L preserved."""
        limit=Decimal(str(amount))
        if not limit.is_finite() or limit<=0:raise ValueError('positive finite daily limit required')
        with self.transaction():
            old=self._daily_limit()
            if old==limit:
                self._reconcile_daily_activation()
                return
            if old is None and self.db.execute('SELECT 1 FROM positions WHERE quantity>0').fetchone():
                raise ValueError('initial daily baseline requires flat portfolio or owner reconciliation')
            self.db.execute('INSERT INTO daily_risk_config VALUES(1,?) ON CONFLICT(id) DO UPDATE SET max_loss=excluded.max_loss',(str(limit),))
            self._event('OWNER_DAILY_LOSS_LIMIT',{'max_loss_usd':str(limit),'timezone':'America/New_York','action':'block_entries_no_liquidation'})
            self._daily_refresh()
            self._reconcile_daily_activation()
    def _initial_daily_baseline(self,day):
        # Reconcile existing fills, so enabling the guard mid-day cannot erase today's loss.
        start=datetime.fromisoformat(day).replace(tzinfo=EASTERN).timestamp()
        books={};realized=Decimal(0);baseline=None
        for fill in self.db.execute('SELECT * FROM fills ORDER BY ts,rowid'):
            if baseline is None and fill['ts']>=start:
                if any(q for q,cost in books.values()):raise ValueError('opening positions need owner-reconciled prior-day marks')
                baseline=realized
            pos=self.db.execute('SELECT instrument FROM positions WHERE id=?',(fill['position_id'],)).fetchone()
            inst=self.db.execute('SELECT multiplier FROM instruments WHERE id=?',(pos[0],)).fetchone()
            qty,cost=books.get(fill['position_id'],(0,Decimal(0)));n=fill['quantity'];price=Decimal(fill['price'])
            if fill['action']=='buy':cost=(cost*qty+price*n)/(qty+n);qty+=n
            elif fill['action']=='sell' and 0<n<=qty:realized+=(price-cost)*n*inst[0];qty-=n
            else:raise ValueError('unreconciled historical fills')
            books[fill['position_id']]=(qty,cost)
        if baseline is None:
            if any(q for q,cost in books.values()):raise ValueError('opening positions need owner-reconciled prior-day marks')
            baseline=realized
        if realized!=Decimal(self._meta('realized')):raise ValueError('historical realized P&L does not reconcile')
        return baseline
    def _reconcile_daily_activation(self):
        if self.db.execute("SELECT 1 FROM meta WHERE k='daily_risk_basis_v1'").fetchone():return
        row=self.db.execute('SELECT * FROM daily_risk_days ORDER BY day LIMIT 1').fetchone()
        if row:
            baseline=self._initial_daily_baseline(row['day'])
            if baseline!=Decimal(row['baseline']):
                self.db.execute('UPDATE daily_risk_days SET baseline=? WHERE day=?',(str(baseline),row['day']))
                self._event('DAILY_BASELINE_RECONCILED',{'day':row['day'],'old_baseline':row['baseline'],'baseline':str(baseline),'basis':'historical fills before Eastern day start'})
        self.db.execute("INSERT INTO meta VALUES('daily_risk_basis_v1','1')")
        self._daily_refresh()
    def _marked_equity(self):
        # P&L-equity relative to original cost, including every controller identity.
        total=Decimal(self._meta('realized'))
        for pos in self.db.execute('SELECT * FROM positions WHERE quantity>0'):
            q=self._fresh_quote(pos['instrument'])
            inst=self.db.execute('SELECT multiplier FROM instruments WHERE id=?',(pos['instrument'],)).fetchone()
            if not q or not inst:return None
            total+=(Decimal(str(q['bid']))-Decimal(pos['cost']))*pos['quantity']*inst[0]
        return total
    def _daily_row(self):
        day=datetime.fromtimestamp(self.clock(),EASTERN).date().isoformat()
        return day,self.db.execute('SELECT * FROM daily_risk_days WHERE day=?',(day,)).fetchone()
    def _daily_cancel_entries(self):
        # Preserve explicit sell orders/conditions; no liquidation, no blanket disarm.
        for table,status in (('orders','PENDING'),('instructions','ACTIVE')):
            for row in list(self.db.execute('SELECT id,plan FROM '+table+' WHERE status=?',(status,))):
                if json.loads(row['plan']).get('action')=='buy':
                    self.db.execute('UPDATE '+table+" SET status='CANCELED' WHERE id=?",(row['id'],))
                    self._event('DAILY_ENTRY_CANCELED',{'table':table,'id':row['id']})
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='structured_plans'").fetchone():
                        column='linked_order' if table=='orders' else 'linked_instruction'
                        self.db.execute("UPDATE structured_plans SET status='DAILY_LOCKED' WHERE "+column+'=?',(row['id'],))
    def _daily_refresh(self):
        limit=self._daily_limit()
        if limit is None:return
        day,row=self._daily_row();equity=self._marked_equity()
        if row is None:
            prev=self.db.execute('SELECT * FROM daily_risk_days WHERE day<? ORDER BY day DESC LIMIT 1',(day,)).fetchone()
            # Carry the last recorded mark across overnight gaps; never rebase to hide a gap loss.
            baseline=Decimal(prev['last_equity']) if prev else self._initial_daily_baseline(day)
            if baseline is None:return
            self.db.execute('INSERT INTO daily_risk_days VALUES(?,?,?,?,?)',(day,str(baseline),str(baseline),0,self.clock()))
            self._event('DAILY_RISK_OPEN',{'day':day,'baseline_pnl_equity':str(baseline),'basis':'prior_day_last_recorded_mark' if prev else 'historical_fills_before_day_start'})
            _,row=self._daily_row()
        if equity is not None and Decimal(row['last_equity'])!=equity:
            self.db.execute('UPDATE daily_risk_days SET last_equity=?,updated=? WHERE day=?',(str(equity),self.clock(),day))
        if equity is not None and Decimal(row['baseline'])-equity>=limit and not row['locked']:
            self.db.execute('UPDATE daily_risk_days SET locked=1,updated=? WHERE day=?',(self.clock(),day))
            self._event('DAILY_ENTRY_LOCK',{'day':day,'loss_usd':str(Decimal(row['baseline'])-equity),'limit_usd':str(limit),'automatic_liquidation':False})
            self._daily_cancel_entries()
    def daily_risk_status(self):
        limit=self._daily_limit()
        if limit is None:return {'enabled':False}
        day,row=self._daily_row();equity=self._marked_equity()
        pnl=equity-Decimal(row['baseline']) if row and equity is not None else None
        locked=bool(row and row['locked']) or (pnl is not None and -pnl>=limit)
        return {'enabled':True,'max_loss_usd':str(limit),'day':day,'timezone':'America/New_York',
            'pnl_usd':str(pnl) if pnl is not None else None,'valuation_available':pnl is not None,
            'entry_locked':locked,'entries_allowed':not locked and pnl is not None,
            'authorized_exits_allowed':True,'automatic_liquidation':False,
            'baseline_pnl_equity':row['baseline'] if row else None,
            'basis':'net realized plus unrealized bid-mark P&L; prior day last recorded mark carried forward'}
    def position_valuations(self):
        result=[]
        for p in self.db.execute('SELECT * FROM positions WHERE owner=? AND quantity>0',(self.owner,)):
            inst=self.db.execute('SELECT multiplier FROM instruments WHERE id=?',(p['instrument'],)).fetchone()
            q=self._fresh_quote(p['instrument']);mult=inst[0]
            value=Decimal(str(q['bid']))*p['quantity']*mult if q else None
            basis=Decimal(p['cost'])*p['quantity']*mult
            result.append({'position_id':p['id'],'instrument':p['instrument'],'quantity':p['quantity'],
                'cost_basis_usd':str(basis),'mark_value_usd':str(value) if value is not None else None,
                'unrealized_pnl_usd':str(value-basis) if value is not None else None,'mark_source':'fresh bid' if q else 'UNAVAILABLE'})
        return result
