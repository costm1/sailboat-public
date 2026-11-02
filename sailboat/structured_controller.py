"""Direct deterministic structured ingress over the preserved simulation/risk core."""
import json,hashlib
from .controller import Controller,Rejected,canonical
from .schema import Message
from .structured_schema import parse,validate

class StructuredController(Controller):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS structured_receipts(owner TEXT,id TEXT,digest TEXT,payload TEXT,result TEXT,
          PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS structured_plans(owner TEXT,id TEXT,symbol TEXT,version INTEGER,status TEXT,
          content TEXT,updated REAL,expires REAL,linked_instruction TEXT,linked_order TEXT,
          PRIMARY KEY(owner,id));
        CREATE TABLE IF NOT EXISTS structured_assessments(owner TEXT,symbol TEXT,assessment TEXT,
          PRIMARY KEY(owner,symbol));
        """)
    def context(self,message):
        c=super().context(message)
        c["plans"]=[dict(r)|{"content":json.loads(r["content"])} for r in self.db.execute(
            "SELECT * FROM structured_plans WHERE owner=? ORDER BY id",(message.owner,))]
        c["assessments"]=[dict(r) for r in self.db.execute("SELECT * FROM structured_assessments WHERE owner=?",(message.owner,))]
        return c
    def submit(self,source,conversation,raw):
        # Identity comes from the owner-controlled ingress, never from the supplied block.
        try:
            v=parse(raw);text=canonical(v)
            m=Message(source,conversation,v.get("decision_id","invalid"),text)
        except (ValueError,TypeError,AttributeError) as exc:
            with self.transaction():self._event("STRUCTURED_REJECTED",{"reason":str(exc),"raw_sha256":hashlib.sha256(str(raw).encode()).hexdigest()})
            return {"status":"REJECTED","reason":str(exc)}
        digest=hashlib.sha256(text.encode()).hexdigest()
        with self.transaction():
            old=self.db.execute("SELECT * FROM structured_receipts WHERE owner=? AND id=?",(m.owner,m.message_id)).fetchone()
            if old:
                if old["digest"]!=digest:return {"status":"REJECTED","reason":"decision ID reused with changed content"}
                return {"status":"DUPLICATE","original":json.loads(old["result"])}
            self.db.execute("SAVEPOINT structured_submission")
            try:
                b=validate(v,self.clock())
                result=self._submit(m,v,b)
            except (ValueError,KeyError,TypeError) as exc:
                self.db.execute("ROLLBACK TO structured_submission")
                result={"status":"REJECTED","reason":str(exc)[:1500]}
            # jsonschema raises ValidationError, which is not a ValueError.
            except Exception as exc:
                from jsonschema import ValidationError
                if not isinstance(exc,ValidationError):raise
                self.db.execute("ROLLBACK TO structured_submission")
                result={"status":"REJECTED","reason":exc.message[:1500]}
            self.db.execute("RELEASE structured_submission")
            self.db.execute("INSERT INTO structured_receipts VALUES(?,?,?,?,?)",(m.owner,m.message_id,digest,text,canonical(result)))
            self._event("STRUCTURED_DECISION",{"source":source,"conversation":conversation,"block":v,"result":result})
            return result
    def _ground(self,message,p):
        # Direct data validation replaces ONLY natural-language lexical grounding.
        inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(p["instrument"],)).fetchone()
        if not inst:raise Rejected("unregistered exact instrument")
        if p.get("symbol") and p["symbol"]!=inst["symbol"]:raise Rejected("instrument/symbol mismatch")
        if p["action"]=="sell":
            pos=self._resolve_position(message,p)
            if p["quantity"]>pos["quantity"]:raise Rejected("cannot sell more than owned")
        if p.get("condition_symbol") and not self.db.execute("SELECT 1 FROM instruments WHERE symbol=? OR id=?",(p["condition_symbol"],p["condition_symbol"])).fetchone():
            raise Rejected("unregistered condition symbol")
        return inst
    def _cancel(self,row,status):
        if row["linked_instruction"]:
            self.db.execute("UPDATE instructions SET status=? WHERE id=? AND status='ACTIVE'",(status,row["linked_instruction"]))
            self.db.execute("UPDATE orders SET status=? WHERE instruction_id=? AND status='PENDING'",(status,row["linked_instruction"]))
        if row["linked_order"]:self.db.execute("UPDATE orders SET status=? WHERE id=? AND status='PENDING'",(status,row["linked_order"]))
    def _check_condition(self,c):
        if c and not self.db.execute("SELECT 1 FROM instruments WHERE symbol=? OR id=?",(c["symbol"],c["symbol"])).fetchone():
            raise Rejected("unregistered condition/invalidation symbol")
    def _submit(self,m,v,b):
        a=b["action"]
        old=self.db.execute("SELECT * FROM structured_plans WHERE owner=? AND id=?",(m.owner,b["plan_id"])).fetchone() if b["plan_id"] else None
        if b["plan_id"] and not old and a not in ("watch","propose_entry","propose_exit"):raise Rejected("unknown plan reference")
        if b["position_quantity"] is not None and b["position_id"] is None:raise Rejected("position quantity requires reference")
        if old and (b["plan_version"]!=old["version"] or b["symbol"]!=old["symbol"]):raise Rejected("stale plan version or mismatched symbol")
        if old and v["timestamp"]<old["updated"]:raise Rejected("out-of-order plan update")
        if a in ("revise","cancel") and not old:raise Rejected("existing owned plan required")
        if a in ("watch","propose_entry","propose_exit") and old:raise Rejected("existing plan requires explicit revise")
        if not old and a in ("watch","propose_entry","propose_exit") and b["plan_version"]!=0:raise Rejected("new plan must use version zero")
        if a in ("hold","propose_exit") or b["position_id"]:
            pos=self.db.execute("SELECT * FROM positions WHERE owner=? AND id=? AND quantity>0",(m.owner,b["position_id"])).fetchone()
            if not pos or pos["quantity"]!=b["position_quantity"]:raise Rejected("missing, foreign or changed position")
            inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(pos["instrument"],)).fetchone()
            if b["symbol"]!=inst["symbol"] or (b["instrument_id"] and b["instrument_id"]!=pos["instrument"]):raise Rejected("position/instrument/symbol mismatch")
        if b["instrument_id"]:
            inst=self.db.execute("SELECT * FROM instruments WHERE id=?",(b["instrument_id"],)).fetchone()
            if not inst or inst["symbol"]!=b["symbol"]:raise Rejected("unregistered instrument or symbol mismatch")
        self._check_condition(b["condition"]);self._check_condition(b["invalidation"])
        if a in ("observe","hold","request_clarification"):
            if a=="observe" and b["symbol"] and b["assessment"] is not None:
                self.db.execute("INSERT INTO structured_assessments VALUES(?,?,?) ON CONFLICT(owner,symbol) DO UPDATE SET assessment=excluded.assessment",(m.owner,b["symbol"],b["assessment"]))
            return {"status":{"observe":"OBSERVED","hold":"HELD","request_clarification":"NEEDS_INFORMATION"}[a]}
        expiry=min(v["expires_at"],self.clock()+self.limits.instruction_ttl_seconds)
        version=(old["version"]+1) if old else 1
        if a=="cancel" or b["revision"] in ("suspend","resume"):
            if b["revision"]=="resume" and old["status"] not in ("SUSPENDED","CANCELED","EXPIRED","INVALIDATED"):raise Rejected("only inactive plans can be reconsidered")
            status="CANCELED" if a=="cancel" else ("SUSPENDED" if b["revision"]=="suspend" else "WATCHING")
            self._cancel(old,"CANCELED")
            self.db.execute("UPDATE structured_plans SET status=?,version=?,updated=?,expires=? WHERE owner=? AND id=?",
                (status,version,v["timestamp"],expiry,m.owner,b["plan_id"]))
            return {"status":status,"plan_id":b["plan_id"],"plan_version":version}
        if old:
            if old["status"]=="FILLED" and b["execution"]=="paper":raise Rejected("filled plan cannot be re-executed; a new explicit plan is required")
            self._cancel(old,"SUPERSEDED")
        if b["execution"]=="paper" and (self._meta("mode")!="PAPER_ARMED" or self._meta("estop")=="1"):
            status="OBSERVED_DISARMED"
        else:status="WATCHING" if a=="watch" else "PROPOSED"
        execute=b["execution"]=="paper" and status!="OBSERVED_DISARMED"
        iid=oid=None
        if execute:
            key=hashlib.sha256(f"{m.owner}/{m.message_id}".encode()).hexdigest()[:20]
            iid="i-"+key if b["condition"] else None
            oid=None if iid else "o-"+key
            status="INSTRUCTED"
        self.db.execute("INSERT INTO structured_plans VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(owner,id) DO UPDATE SET version=excluded.version,status=excluded.status,content=excluded.content,updated=excluded.updated,expires=excluded.expires,linked_instruction=excluded.linked_instruction,linked_order=excluded.linked_order",
            (m.owner,b["plan_id"],b["symbol"],version,status,canonical(b),v["timestamp"],expiry,iid,oid))
        result={"status":status}
        if execute:
            is_exit=b["order_side"]=="sell"
            p={"tool":"monitor_price" if b["condition"] else "create_simulated_order","instrument":b["instrument_id"],
               "symbol":b["symbol"],"action":"sell" if is_exit else "buy","quantity":b["quantity"]}
            if b["position_id"]:p["target"]=b["position_id"]
            if b["limit_price"] is not None:p["limit_price"]=b["limit_price"]
            if b["condition"]:p.update(condition_symbol=b["condition"]["symbol"],operator=b["condition"]["op"],price=b["condition"]["price"])
            result=self._dispatch(m,p)
            if result.get("status")=="REJECTED":
                self.db.execute("UPDATE structured_plans SET status='REJECTED' WHERE owner=? AND id=?",(m.owner,b["plan_id"]))
            if iid:self.db.execute("UPDATE instructions SET expires=MIN(expires,?) WHERE id=?",(expiry,iid))
            if oid:self.db.execute("UPDATE orders SET expires=MIN(expires,?) WHERE id=?",(expiry,oid))
            self._sync()
        return result|{"plan_id":b["plan_id"],"plan_version":version}
    def _plan_for_order(self,order):
        return self.db.execute("SELECT * FROM structured_plans WHERE owner=? AND (linked_order=? OR linked_instruction=?)",
            (order["owner"],order["id"],order["instruction_id"])).fetchone()
    def _try_fill(self,ident):
        order=self.db.execute("SELECT * FROM orders WHERE id=?",(ident,)).fetchone()
        # During base construction no orders are dispatched; table exists by first use.
        plan=self._plan_for_order(order)
        if plan:
            self.db.execute("UPDATE orders SET expires=MIN(expires,?) WHERE id=?",(plan["expires"],ident))
            body=json.loads(plan["content"]);inv=body["invalidation"]
            if self.clock()>=plan["expires"]:
                self._cancel(plan,"EXPIRED");return "EXPIRED"
            if inv:
                q=self._fresh_quote(inv["symbol"])
                if not q:return "PENDING"
                if self._condition({"operator":inv["op"],"price":inv["price"]},q):
                    self._cancel(plan,"CANCELED")
                    self.db.execute("UPDATE structured_plans SET status='INVALIDATED' WHERE owner=? AND id=?",(plan["owner"],plan["id"]))
                    return "CANCELED"
        return super()._try_fill(ident)
    def _stop(self,latch=False):
        super()._stop(latch)
        self.db.execute("UPDATE structured_plans SET status='SUSPENDED' WHERE status IN ('INSTRUCTED','WATCHING','PROPOSED')")
    def _sync(self):
        for row in list(self.db.execute("SELECT * FROM structured_plans WHERE status='INSTRUCTED'")):
            filled=self.db.execute("SELECT 1 FROM orders WHERE owner=? AND status='FILLED' AND (id=? OR instruction_id=?)",(row["owner"],row["linked_order"],row["linked_instruction"])).fetchone()
            if filled:self.db.execute("UPDATE structured_plans SET status='FILLED' WHERE owner=? AND id=?",(row["owner"],row["id"]))
    def _tick(self):
        for row in list(self.db.execute("SELECT * FROM structured_plans WHERE status IN ('INSTRUCTED','WATCHING','PROPOSED','OBSERVED_DISARMED')")):
            reason=None
            if self.clock()>=row["expires"]:reason="EXPIRED"
            inv=json.loads(row["content"])["invalidation"]
            q=self._fresh_quote(inv["symbol"]) if inv else None
            if q and self._condition({"operator":inv["op"],"price":inv["price"]},q):reason="INVALIDATED"
            if reason:
                self._cancel(row,"CANCELED" if reason=="INVALIDATED" else "EXPIRED")
                self.db.execute("UPDATE structured_plans SET status=? WHERE owner=? AND id=?",(reason,row["owner"],row["id"]))
                self._event("STRUCTURED_PLAN_TERMINATED",{"plan_id":row["id"],"reason":reason})
        super()._tick();self._sync()
    def status(self):
        return super().status()|{"plans":[dict(x)|{"content":json.loads(x["content"])} for x in self.db.execute("SELECT * FROM structured_plans ORDER BY owner,id")]}
