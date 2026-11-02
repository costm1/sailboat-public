"""Transactional simulation. No brokerage SDK, credentials, URLs or LIVE adapter."""
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from decimal import Decimal
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from .schema import Message, validate_plan, number

def canonical(obj):
    return json.dumps(obj,sort_keys=True,separators=(",",":"),allow_nan=False)
def money(v):
    return Decimal(str(v))
@dataclass(frozen=True)
class Limits:
    max_order_quantity: int = 10
    max_position_quantity: int = 20
    max_positions: int = 4
    max_gross_notional: float = 10000.
    max_order_notional: float = 2500.
    max_session_loss: float = 500.
    max_quote_age_seconds: float = 30.
    instruction_ttl_seconds: float = 3600.
    def __post_init__(self):
        for k,v in asdict(self).items(): number(v,k)
        for k in ("max_order_quantity","max_position_quantity","max_positions"):
            if type(getattr(self,k)) is not int: raise ValueError(k+" must be integer")
class Rejected(ValueError): pass

from .daily_risk import DailyRisk

class Controller(DailyRisk):
    def __init__(self,path,limits=None,clock=time.time):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.clock=clock
        self.limits=limits or Limits()
        self.db=sqlite3.connect(self.path,timeout=10,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS messages(owner TEXT,id TEXT,text TEXT,fingerprint TEXT,
          plan TEXT,outcome TEXT,ts REAL,PRIMARY KEY(owner,id),UNIQUE(owner,fingerprint));
        CREATE TABLE IF NOT EXISTS instruments(id TEXT PRIMARY KEY,symbol TEXT,kind TEXT,
          multiplier INTEGER,details TEXT);
        CREATE TABLE IF NOT EXISTS quotes(symbol TEXT PRIMARY KEY,bid REAL,ask REAL,last REAL,
          previous REAL,ts REAL);
        CREATE TABLE IF NOT EXISTS instructions(id TEXT PRIMARY KEY,owner TEXT,plan TEXT,
          status TEXT,created REAL,expires REAL);
        CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,owner TEXT,plan TEXT,status TEXT,
          created REAL,expires REAL,instruction_id TEXT,fill_price REAL);
        CREATE TABLE IF NOT EXISTS positions(id TEXT PRIMARY KEY,owner TEXT,instrument TEXT,
          quantity INTEGER,cost TEXT,UNIQUE(owner,instrument));
        CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,ts REAL,
          kind TEXT,data TEXT,previous_hash TEXT,hash TEXT);
        CREATE TABLE IF NOT EXISTS fills(order_id TEXT PRIMARY KEY,position_id TEXT,
          quantity INTEGER,price TEXT,action TEXT,ts REAL);
        """)
        self._daily_schema()
        with self.transaction():
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('mode','DISARMED')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('estop','0')")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('realized','0')")
            existing=self.db.execute("SELECT v FROM meta WHERE k='limits'").fetchone()
            if existing and existing["v"]!=canonical(asdict(self.limits)):
                raise ValueError("limits differ from persisted state; use original limits")
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('limits',?)",(canonical(asdict(self.limits)),))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")
    def close(self): self.db.close()
    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try: yield
        except BaseException:
            self.db.rollback()
            raise
        else: self.db.commit()
    def _event(self,kind,data):
        last=self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev=last["hash"] if last else "0"*64
        ts=self.clock()
        body=canonical({"ts":ts,"kind":kind,"data":data,"previous_hash":prev})
        digest=hashlib.sha256(body.encode()).hexdigest()
        self.db.execute("INSERT INTO events(ts,kind,data,previous_hash,hash) VALUES(?,?,?,?,?)",
                        (ts,kind,canonical(data),prev,digest))
    def _meta(self,key):
        return self.db.execute("SELECT v FROM meta WHERE k=?",(key,)).fetchone()["v"]
    def _set(self,key,value):
        self.db.execute("UPDATE meta SET v=? WHERE k=?",(str(value),key))
    def _stop(self,latch=False):
        self._set("mode","DISARMED")
        if latch: self._set("estop","1")
        self.db.execute("UPDATE instructions SET status='CANCELED' WHERE status='ACTIVE'")
        self.db.execute("UPDATE orders SET status='CANCELED' WHERE status='PENDING'")
    def set_mode(self,mode):
        """Owner control-plane API, never exposed as a model tool."""
        if mode not in ("DISARMED","PAPER_ARMED"):
            raise Rejected("LIVE is unavailable in Phase 1")
        with self.transaction():
            if mode=="PAPER_ARMED" and self._meta("estop")=="1":
                raise Rejected("emergency stop latched; owner reset required")
            if mode=="DISARMED": self._stop()
            else: self._set("mode",mode)
            self._event("OWNER_MODE",{"mode":mode})
    def reset_emergency_stop(self):
        with self.transaction():
            self._set("estop","0")
            self._set("mode","DISARMED")
            self._event("OWNER_ESTOP_RESET",{})
    def emergency_stop(self):
        with self.transaction():
            self._stop(latch=True)
            self._event("OWNER_ESTOP",{})
    def register_instrument(self,ident,symbol=None,kind="stock",multiplier=1,details=None):
        from .schema import IDENTIFIER
        symbol=symbol or ident
        if not all(isinstance(x,str) and IDENTIFIER.fullmatch(x) for x in (ident,symbol)):
            raise ValueError("invalid instrument identifiers")
        if kind not in ("stock","option") or type(multiplier) is not int or multiplier<=0:
            raise ValueError("invalid instrument")
        if kind=="option" and (not details or not {"expiry","strike","right"}<=details.keys()):
            raise ValueError("option requires expiry, strike, right")
        values=(ident,symbol,kind,multiplier,canonical(details or {}))
        with self.transaction():
            row=self.db.execute("SELECT * FROM instruments WHERE id=?",(ident,)).fetchone()
            if row and tuple(row)!=values: raise Rejected("catalog entries are immutable")
            self.db.execute("INSERT OR IGNORE INTO instruments VALUES(?,?,?,?,?)",values)
            self._event("OWNER_INSTRUMENT",{"id":ident,"symbol":symbol,"kind":kind})
    def context(self,message):
        owner=message.owner
        return {"mode":self._meta("mode"),
            "instruments":[dict(x) for x in self.db.execute("SELECT * FROM instruments")],
            "positions":[dict(x) for x in self.db.execute(
                "SELECT * FROM positions WHERE owner=? AND quantity>0",(owner,))],
            "instructions":[dict(x)|{"plan":json.loads(x["plan"])} for x in self.db.execute(
                "SELECT * FROM instructions WHERE owner=? AND status='ACTIVE'",(owner,))],
            "orders":[dict(x)|{"plan":json.loads(x["plan"])} for x in self.db.execute(
                "SELECT * FROM orders WHERE owner=? AND status='PENDING'",(owner,))],
            "recent_messages":[dict(x) for x in self.db.execute(
                "SELECT text,plan,outcome FROM messages WHERE owner=? ORDER BY ts DESC,rowid DESC LIMIT 6",
                (owner,))][::-1]}
    def receive(self,message,interpreter):
        """Inference outside write lock; dispatch rechecks current state."""
        prior=self.db.execute("SELECT text,outcome FROM messages WHERE owner=? AND id=?",
                              (message.owner,message.message_id)).fetchone()
        if prior:
            if prior["text"]!=message.text: raise Rejected("message id reused with different content")
            return {"status":"DUPLICATE","original":json.loads(prior["outcome"])}
        try: plan=interpreter.interpret(message,self.context(message))
        except Exception as exc:
            return self.apply(message,None,interpretation_error=type(exc).__name__)
        return self.apply(message,plan)
    def apply(self,message,plan,interpretation_error=None):
        owner=message.owner
        fingerprint=hashlib.sha256(" ".join(message.text.casefold().split()).encode()).hexdigest()
        with self.transaction():
            by_id=self.db.execute("SELECT * FROM messages WHERE owner=? AND id=?",(owner,message.message_id)).fetchone()
            if by_id and by_id["text"]!=message.text: raise Rejected("message id reused with different content")
            row=by_id or self.db.execute("SELECT * FROM messages WHERE owner=? AND fingerprint=?",
                                        (owner,fingerprint)).fetchone()
            if row: return {"status":"DUPLICATE","original":json.loads(row["outcome"])}
            self.db.execute("SAVEPOINT proposal")
            try:
                if interpretation_error: raise Rejected("interpretation failed: "+interpretation_error)
                plan=validate_plan(plan)
                result=self._dispatch(message,plan)
            except (ValueError,KeyError) as exc:
                self.db.execute("ROLLBACK TO proposal")
                result={"status":"REJECTED","reason":str(exc)}
            self.db.execute("RELEASE proposal")
            try: saved=canonical(plan)
            except (ValueError,TypeError): saved=canonical({"invalid":repr(plan)[:2000]})
            self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                (owner,message.message_id,message.text,fingerprint,saved,canonical(result),self.clock()))
            self._event("AGENT_DECISION",{"message":message.as_dict(),"proposal":json.loads(saved),"result":result})
            return result
    def _dispatch(self,message,p):
        tool=p["tool"]
        if tool=="emergency_stop":
            self._stop(latch=True)
            return {"status":"STOPPED"}
        if tool=="disarm":
            self._stop()
            return {"status":"DISARMED"}
        if tool in ("record_commentary","request_clarification","maintain_position"):
            return {"status":{"record_commentary":"OBSERVED","request_clarification":"NEEDS_INFORMATION",
                              "maintain_position":"HELD"}[tool]}
        if tool=="read_market_snapshot":
            q=self.db.execute("SELECT * FROM quotes WHERE symbol=?",(p.get("symbol"),)).fetchone()
            return {"status":"SNAPSHOT","quote":dict(q) if q else None}
        if self._meta("mode")!="PAPER_ARMED" or self._meta("estop")=="1":
            return {"status":"OBSERVED_DISARMED"}
        if tool in ("cancel_instruction","cancel_simulated_order"):
            table="instructions" if tool=="cancel_instruction" else "orders"
            row=self.db.execute(f"SELECT * FROM {table} WHERE id=? AND owner=?",(p["target"],message.owner)).fetchone()
            if not row: raise Rejected("unknown or foreign target")
            if row["status"] not in ("ACTIVE","PENDING"):
                return {"status":"ALREADY_TERMINAL","target":p["target"]}
            if p["target"] not in message.text and not self._owns_context_target(message,p["target"],table):
                raise Rejected("ambiguous target")
            self.db.execute(f"UPDATE {table} SET status='CANCELED' WHERE id=?",(p["target"],))
            if table=="instructions":
                self.db.execute("UPDATE orders SET status='CANCELED' WHERE instruction_id=? AND status='PENDING'",(p["target"],))
            return {"status":"CANCELED","target":p["target"]}
        if tool=="close_simulated_position":
            pos=self._resolve_position(message,p)
            p=p|{"instrument":pos["instrument"],"action":"sell","quantity":p.get("quantity",pos["quantity"])}
            return self._new_order(message,p)
        if tool=="replace_instruction":
            old=self.db.execute("SELECT * FROM instructions WHERE id=? AND owner=? AND status='ACTIVE'",
                                (p["target"],message.owner)).fetchone()
            if not old: raise Rejected("active owned instruction required")
            self._ground(message,p)
            self.db.execute("UPDATE instructions SET status='SUPERSEDED' WHERE id=?",(p["target"],))
            self.db.execute("UPDATE orders SET status='CANCELED' WHERE instruction_id=? AND status='PENDING'",(p["target"],))
            return self._new_instruction(message,p) if "operator" in p else self._new_order(message,p)
        if tool=="monitor_price":
            self._ground(message,p)
            return self._new_instruction(message,p)
        if tool=="create_simulated_order": return self._new_order(message,p)
        raise Rejected("unsupported tool")
    def _owns_context_target(self,message,target,table):
        rows=list(self.db.execute(f"SELECT id FROM {table} WHERE owner=? AND status IN ('ACTIVE','PENDING')",(message.owner,)))
        return len(rows)==1 and rows[0]["id"]==target
    def _resolve_position(self,message,p):
        query="SELECT * FROM positions WHERE owner=? AND quantity>0"
        args=[message.owner]
        if p.get("target"):
            query+=" AND id=?"
            args.append(p["target"])
        if p.get("instrument"):
            query+=" AND instrument=?"
            args.append(p["instrument"])
        rows=list(self.db.execute(query,args))
        if len(rows)!=1: raise Rejected("one unambiguous owned position required")
        return rows[0]
    def _ground(self,message,p):
        if p["action"]=="sell":
            position_plan={k:v for k,v in p.items() if k!="target"} if p["tool"]=="replace_instruction" else p
            pos=self._resolve_position(message,position_plan)
            if p["quantity"]>pos["quantity"]: raise Rejected("cannot sell more than owned")
            if p["quantity"] != pos["quantity"] and not re.search(
                r"(?<![\w.\-])"+str(p["quantity"])+r"\s+(?:shares?\b|contracts?\b|"+re.escape(p["instrument"])+r"\b)",
                message.text,re.I):
                raise Rejected("partial exit quantity not explicit in current message")
        inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(p["instrument"],)).fetchone()
        if not inst: raise Rejected("unregistered instrument")
        if p.get("symbol") and p["symbol"]!=inst["symbol"]: raise Rejected("symbol disagrees with instrument")
        if p["action"]=="buy":
            if not re.search(r"(?<![\w.])"+re.escape(p["instrument"])+r"(?![\w.])",message.text,re.I):
                raise Rejected("instrument not explicit in current message")
            if not re.search(r"(?<![\w.\-])"+str(p["quantity"])+r"\s+(?:shares?\b|contracts?\b|"+re.escape(p["instrument"])+r"\b)",message.text):
                raise Rejected("quantity not explicit in current message")
        for key in ("price","limit_price"):
            if key in p:
                nums=re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?",message.text)
                if not any(money(x)==money(p[key]) for x in nums): raise Rejected(key+" not explicit in current message")
        if "condition_symbol" in p:
            known=self.db.execute("SELECT 1 FROM instruments WHERE symbol=? OR id=?",
                                  (p["condition_symbol"],p["condition_symbol"])).fetchone()
            if not known: raise Rejected("unregistered condition symbol")
        if "condition_symbol" in p and p["condition_symbol"]!=inst["symbol"] and p["condition_symbol"] not in message.text:
            raise Rejected("condition symbol is not grounded")
        return inst
    def _new_instruction(self,message,p):
        self._daily_refresh()
        if p["action"]=="buy" and self._daily_limit() is not None and not self.daily_risk_status()["entries_allowed"]:
            return {"status":"REJECTED","reason":"daily entry lock or unavailable daily valuation"}
        self._check_quantity(p)
        ident="i-"+hashlib.sha256(f"{message.owner}/{message.message_id}".encode()).hexdigest()[:20]
        now=self.clock()
        self.db.execute("INSERT INTO instructions VALUES(?,?,?,?,?,?)",
                        (ident,message.owner,canonical(p),"ACTIVE",now,now+self.limits.instruction_ttl_seconds))
        self._event("INSTRUCTION_CREATED",{"id":ident,"owner":message.owner,"plan":p})
        return {"status":"MONITORING","instruction_id":ident}
    def _new_order(self,message,p,instruction_id=None,prevalidated=False):
        self._daily_refresh()
        if p["action"]=="buy" and self._daily_limit() is not None and not self.daily_risk_status()["entries_allowed"]:
            return {"status":"REJECTED","reason":"daily entry lock or unavailable daily valuation"}
        if not prevalidated: self._ground(message,p)
        ident="o-"+hashlib.sha256((instruction_id or f"{message.owner}/{message.message_id}").encode()).hexdigest()[:20]
        self._check_quantity(p)
        now=self.clock()
        self.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,NULL)",
                        (ident,message.owner,canonical(p),"PENDING",now,now+self.limits.instruction_ttl_seconds,instruction_id))
        self._event("ORDER_CREATED",{"id":ident,"owner":message.owner,"plan":p})
        return {"status":self._try_fill(ident),"order_id":ident}
    def _check_quantity(self,p):
        if p["quantity"]>self.limits.max_order_quantity: raise Rejected("max order quantity")
    def _fresh_quote(self,symbol):
        q=self.db.execute("SELECT * FROM quotes WHERE symbol=?",(symbol,)).fetchone()
        if not q or not 0<=self.clock()-q["ts"]<=self.limits.max_quote_age_seconds: return None
        return q
    def _portfolio_loss(self):
        pnl=money(self._meta("realized"))
        for pos in self.db.execute("SELECT * FROM positions WHERE quantity>0"):
            inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(pos["instrument"],)).fetchone()
            q=self._fresh_quote(pos["instrument"])
            if q: pnl+=(money(q["bid"])-money(pos["cost"]))*pos["quantity"]*inst["multiplier"]
        return -pnl
    def _try_fill(self,ident):
        order=self.db.execute("SELECT * FROM orders WHERE id=?",(ident,)).fetchone()
        if order["status"]!="PENDING": return order["status"]
        if self._meta("mode")!="PAPER_ARMED" or self._meta("estop")=="1": return "PENDING"
        if self.clock()>=order["expires"]:
            self.db.execute("UPDATE orders SET status='EXPIRED' WHERE id=?",(ident,))
            return "EXPIRED"
        p=json.loads(order["plan"])
        q=self._fresh_quote(p["instrument"])
        if not q: return "PENDING"
        fill=money(q["ask"] if p["action"]=="buy" else q["bid"])
        if p.get("limit_price") is not None:
            limit=money(p["limit_price"])
            if (p["action"]=="buy" and fill>limit) or (p["action"]=="sell" and fill<limit): return "PENDING"
        self._daily_refresh()
        if p["action"]=="buy" and self._daily_limit() is not None:
            risk=self.daily_risk_status()
            if not risk["entries_allowed"]:
                self.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?",(ident,))
                self._event("ORDER_REJECTED",{"id":ident,"reason":"daily entry lock or unavailable valuation"})
                return "REJECTED"
        inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(p["instrument"],)).fetchone()
        pos=self.db.execute("SELECT * FROM positions WHERE owner=? AND instrument=?",(order["owner"],p["instrument"])).fetchone()
        qty=pos["quantity"] if pos else 0
        amount=fill*p["quantity"]*inst["multiplier"]
        reason=None
        if p["action"]=="buy":
            if self._daily_limit() is None and self._portfolio_loss()>=money(self.limits.max_session_loss):
                self._stop(latch=True)
                self._event("RISK_STOP",{"reason":"max session loss"})
                return "CANCELED"
            positions=list(self.db.execute("SELECT * FROM positions WHERE quantity>0"))
            gross=Decimal(0)
            for existing in positions:
                eq=self._fresh_quote(existing["instrument"])
                ei=self.db.execute("SELECT multiplier FROM instruments WHERE id=?",(existing["instrument"],)).fetchone()
                if not eq:
                    reason="open exposure has stale quote"
                    break
                gross+=max(money(eq["ask"]),money(existing["cost"]))*existing["quantity"]*ei["multiplier"]
            if qty+p["quantity"]>self.limits.max_position_quantity: reason="max position quantity"
            if not qty and len(positions)>=self.limits.max_positions: reason="max positions"
            if self._daily_limit() is not None:
                risk=self.daily_risk_status()
                spread=(money(q["ask"])-money(q["bid"]))*p["quantity"]*inst["multiplier"]
                if -money(risk["pnl_usd"])+spread>=self._daily_limit():reason="entry spread would reach daily loss limit"
            if amount>money(self.limits.max_order_notional): reason="max order notional"
            if gross+amount>money(self.limits.max_gross_notional): reason="max gross notional"
        elif p["quantity"]>qty: reason="insufficient owned position"
        if reason:
            self.db.execute("UPDATE orders SET status='REJECTED' WHERE id=?",(ident,))
            self._event("ORDER_REJECTED",{"id":ident,"reason":reason})
            return "REJECTED"
        pid=pos["id"] if pos else "p-"+hashlib.sha256(f"{order['owner']}/{p['instrument']}".encode()).hexdigest()[:20]
        if p["action"]=="buy":
            newqty=qty+p["quantity"]
            cost=((money(pos["cost"])*qty if pos else 0)+fill*p["quantity"])/newqty
        else:
            newqty=qty-p["quantity"]
            cost=money(pos["cost"])
            self._set("realized",money(self._meta("realized"))+(fill-cost)*p["quantity"]*inst["multiplier"])
        self.db.execute("INSERT INTO positions VALUES(?,?,?,?,?) ON CONFLICT(owner,instrument) "
                        "DO UPDATE SET quantity=excluded.quantity,cost=excluded.cost",
                        (pid,order["owner"],p["instrument"],newqty,str(cost)))
        self.db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?)",(ident,pid,p["quantity"],str(fill),p["action"],self.clock()))
        self.db.execute("UPDATE orders SET status='FILLED',fill_price=? WHERE id=?",(float(fill),ident))
        self._event("PAPER_FILL",{"order_id":ident,"position_id":pid,"quantity":p["quantity"],"action":p["action"],"price":str(fill)})
        self._daily_refresh()
        return "FILLED"
    def market_snapshot(self,symbol,bid,ask,last=None,timestamp=None):
        """Trusted synthetic feed only; models cannot write quotes."""
        bid,ask=number(bid,"bid"),number(ask,"ask")
        last=number(last if last is not None else (bid+ask)/2,"last")
        ts=self.clock() if timestamp is None else number(timestamp,"timestamp")
        if ask<bid or ts>self.clock()+1: raise ValueError("crossed or future quote")
        with self.transaction():
            prior=self.db.execute("SELECT * FROM quotes WHERE symbol=?",(symbol,)).fetchone()
            if prior and ts<=prior["ts"]:
                self._event("QUOTE_IGNORED",{"symbol":symbol,"reason":"non-increasing timestamp","ts":ts})
                return {"status":"IGNORED"}
            previous=prior["last"] if prior else None
            if prior and ts-prior["ts"]>self.limits.max_quote_age_seconds: previous=None
            self.db.execute("INSERT INTO quotes VALUES(?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET "
                "bid=excluded.bid,ask=excluded.ask,last=excluded.last,previous=excluded.previous,ts=excluded.ts",
                (symbol,bid,ask,last,previous,ts))
            self._event("SIMULATED_QUOTE",{"symbol":symbol,"bid":bid,"ask":ask,"last":last,"ts":ts})
            self._tick()
        return {"status":"RECORDED"}
    def tick(self):
        with self.transaction(): self._tick()
    def _tick(self):
        self._daily_refresh()
        now=self.clock()
        self.db.execute("UPDATE instructions SET status='EXPIRED' WHERE status='ACTIVE' AND expires<=?",(now,))
        self.db.execute("UPDATE orders SET status='EXPIRED' WHERE status='PENDING' AND expires<=?",(now,))
        if self._meta("mode")!="PAPER_ARMED" or self._meta("estop")=="1": return
        if self._daily_limit() is None and self._portfolio_loss()>=money(self.limits.max_session_loss):
            self._stop(latch=True)
            self._event("RISK_STOP",{"reason":"max session loss"})
            return
        for row in list(self.db.execute("SELECT * FROM instructions WHERE status='ACTIVE' ORDER BY created,id")):
            if self._meta("mode")!="PAPER_ARMED" or self._meta("estop")=="1": break
            p=json.loads(row["plan"])
            q=self._fresh_quote(p["condition_symbol"])
            if q and q["ts"]>row["created"] and self._condition(p,q):
                self.db.execute("UPDATE instructions SET status='TRIGGERED' WHERE id=?",(row["id"],))
                synthetic=Message(*row["owner"].split("/",1),row["id"],"registered instruction")
                try: self._new_order(synthetic,p,row["id"],prevalidated=True)
                except Rejected as exc: self._event("INSTRUCTION_REJECTED",{"id":row["id"],"reason":str(exc)})
        for row in list(self.db.execute("SELECT id FROM orders WHERE status='PENDING' ORDER BY created,id")):
            self._try_fill(row["id"])
    @staticmethod
    def _condition(p,q):
        current,threshold=money(q["last"]),money(p["price"])
        op=p["operator"]
        if op=="gt": return current>threshold
        if op=="gte": return current>=threshold
        if op=="lt": return current<threshold
        if op=="lte": return current<=threshold
        if q["previous"] is None: return False
        before=money(q["previous"])
        if op=="cross_above": return before<=threshold<current
        if op=="cross_below": return before>=threshold>current
        return False
    def status(self):
        return {"mode":self._meta("mode"),"emergency_stop":self._meta("estop")=="1",
            "realized_pnl":self._meta("realized"),"limits":asdict(self.limits),"daily_risk":self.daily_risk_status(),
            **{table:[dict(x) for x in self.db.execute(f"SELECT * FROM {table}")]
               for table in ("instruments","quotes","instructions","orders","positions","fills")},
            "messages":self.db.execute("SELECT count(*) FROM messages").fetchone()[0],
            "events":self.db.execute("SELECT count(*) FROM events").fetchone()[0]}
    def verify_journal(self):
        prev="0"*64
        seq=0
        exists=self.db.execute("SELECT 1 FROM sqlite_master WHERE name='event_retention'").fetchone()
        gaps={r['first_seq']:r for r in self.db.execute('SELECT * FROM event_retention')} if exists else {}
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            gap=gaps.pop(seq+1,None)
            if gap:
                if gap['previous_hash']!=prev or gap['last_seq']<gap['first_seq']:return False
                seq=gap['last_seq'];prev=gap['hash']
            if row['seq']!=seq+1:return False
            body=canonical({"ts":row["ts"],"kind":row["kind"],"data":json.loads(row["data"]),"previous_hash":prev})
            if row["previous_hash"]!=prev or hashlib.sha256(body.encode()).hexdigest()!=row["hash"]: return False
            prev=row["hash"];seq=row['seq']
        return not gaps and self.db.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
    def backup(self,path):
        target=Path(path)
        if target.exists(): raise FileExistsError(target)
        target.parent.mkdir(parents=True,exist_ok=True)
        dest=sqlite3.connect(target)
        try: self.db.backup(dest)
        finally: dest.close()
