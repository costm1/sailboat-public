"""Local agent request/response bridge. No model, provider SDK or network listener."""
from contextlib import contextmanager
from dataclasses import asdict
import hashlib,json,time,uuid
from .controller import canonical
from .structured_controller import StructuredController
from .structured_schema import BODY_SCHEMA,envelope,parse
from .schema import Message

PROTOCOL = """You are the outside agent. Sailboat performs no language interpretation.
Read operator_request and current context. Return exactly the response_template shape.
Fill every decision field, using null for unknown/inapplicable values. Do not invent critical values.
Use the supplied new_plan_id with version 0 only for a new plan. For updates, use a current
owned plan ID and its current version. Never create a new ID to replace an existing idea.
Use observe for commentary, watch for a conditional preference, request_clarification for
missing critical information, hold for an identified existing position.
A proposal executes only with execution=paper and owner PAPER_ARMED. Missing instrument,
quantity or order type must not execute. Market orders have null limit_price; limit orders
require limit_price. propose_entry buys; propose_exit sells an explicitly referenced position.
Exits and hold require position_id plus its current position_quantity.
revise requires revision=replace/suspend/resume. replace supplies a complete replacement.
suspend/cancel stop a pending plan. resume reconsiders it but never automatically trades.
observe/hold/cancel/request_clarification and suspend/resume must have null instrument_id,
quantity, order_side, order_type, limit_price, condition, invalidation.
Price conditions are explicit symbol/op/price, with op gt,gte,lt,lte,cross_above,cross_below.
Optional reasoning is inert metadata, not an instruction channel.
Do not request arming, quote changes, catalog changes, limit changes or live orders.
Use only supplied context; if it cannot support a decision, request clarification.
"""

class AgentLink(StructuredController):
    def __init__(self,path,source="external-agent",conversation="default",**kwargs):
        Message(source,conversation,"check","check")
        self.source,self.conversation=source,conversation
        super().__init__(path,**kwargs)
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_requests(
          owner TEXT,id TEXT,context_hash TEXT,payload TEXT,created REAL,deadline REAL,
          response_digest TEXT,response TEXT,receipt TEXT,PRIMARY KEY(owner,id))""")

    @contextmanager
    def transaction(self):
        # Submit + durable transport receipt share one SQLite commit.
        if not self.db.in_transaction:
            with super().transaction():yield
        else:
            name="link_"+uuid.uuid4().hex
            self.db.execute("SAVEPOINT "+name)
            try:yield
            except BaseException:
                self.db.execute("ROLLBACK TO "+name)
                self.db.execute("RELEASE "+name)
                raise
            else:self.db.execute("RELEASE "+name)

    @property
    def owner(self):return self.source+"/"+self.conversation

    def _context(self):
        m=Message(self.source,self.conversation,"context","context")
        c=self.context(m)
        c.pop("recent_messages",None)
        c["emergency_stop"]=self._meta("estop")=="1"
        c["limits"]=asdict(self.limits)
        c["daily_risk"]=self.daily_risk_status()
        c["position_valuations"]=self.position_valuations()
        c["quotes"]=[dict(r)|{"age_seconds":max(0,self.clock()-r["ts"]),
                     "fresh":0<=self.clock()-r["ts"]<=self.limits.max_quote_age_seconds}
                     for r in self.db.execute("SELECT * FROM quotes ORDER BY symbol")]
        c["market_data_origin"]="owner-supplied snapshots; not an authenticated live feed"
        c["portfolio_scope"]="owned simulated positions; no cash balance or brokerage account"
        return c

    @staticmethod
    def _state_hash(c):
        # Quote polling continues independently; execution checks freshness again.
        state={k:v for k,v in c.items() if k not in ("quotes","market_data_origin","portfolio_scope","position_valuations","daily_risk")}
        risk=c.get("daily_risk",{})
        state["daily_risk_controls"]={k:risk.get(k) for k in ("enabled","max_loss_usd","day","entry_locked")}
        return hashlib.sha256(canonical(state).encode()).hexdigest()

    def request(self,operator_request,ttl=120,plan_ttl=900):
        if not isinstance(operator_request,str) or not 1<=len(operator_request)<=6000:
            raise ValueError("operator_request must contain 1-6000 characters")
        if type(ttl) is not int or not 1<=ttl<=120:raise ValueError("request ttl must be 1-120 seconds")
        if type(plan_ttl) is not int or not ttl<=plan_ttl<=self.limits.instruction_ttl_seconds:
            raise ValueError("plan ttl must cover request ttl and respect owner instruction limit")
        self.tick()
        with self.transaction():
            now=self.clock();ident="req-"+uuid.uuid4().hex
            c=self._context();digest=self._state_hash(c)
            history=[{"request_id":r["id"],"receipt":json.loads(r["receipt"]),"decision":json.loads(r["response"])["decision"] if r["response"] else None} for r in self.db.execute(
                "SELECT id,receipt,response FROM agent_requests WHERE owner=? AND receipt IS NOT NULL ORDER BY created DESC,rowid DESC LIMIT 6",(self.owner,))]
            request={"protocol":"sailboat.agent-link.v1","request_id":ident,"context_hash":digest,
                "created_at":now,"reply_before":now+ttl,"decision_expires_at":now+plan_ttl,
                "new_plan_id":"plan-"+uuid.uuid4().hex,"instructions":PROTOCOL,
                "operator_request":operator_request,"context":c,"recent_receipts":history[::-1],
                "decision_schema":BODY_SCHEMA,
                "response_template":{"request_id":ident,"context_hash":digest,"decision":{k:None for k in BODY_SCHEMA["properties"]}}}
            self.db.execute("INSERT INTO agent_requests VALUES(?,?,?,?,?,?,NULL,NULL,NULL)",
                (self.owner,ident,digest,canonical(request),now,now+ttl))
            self._event("AGENT_REQUEST",{"owner":self.owner,"request_id":ident,"context_hash":digest})
            return request

    def get_request(self,ident):
        row=self.db.execute("SELECT payload FROM agent_requests WHERE owner=? AND id=?",(self.owner,ident)).fetchone()
        if not row:raise ValueError("unknown owned request")
        return json.loads(row["payload"])

    def receipt(self,ident):
        row=self.db.execute("SELECT receipt FROM agent_requests WHERE owner=? AND id=?",(self.owner,ident)).fetchone()
        if not row:raise ValueError("unknown owned request")
        return json.loads(row["receipt"]) if row["receipt"] else {"status":"AWAITING_RESPONSE","request_id":ident}

    def respond(self,raw):
        try:
            response=parse(raw)
            if not isinstance(response,dict) or set(response)!={"request_id","context_hash","decision"}:
                raise ValueError("response requires only request_id, context_hash, decision")
            if not isinstance(response["request_id"],str) or not isinstance(response["context_hash"],str):
                raise ValueError("request ID and context hash must be strings")
            text=canonical(response);digest=hashlib.sha256(text.encode()).hexdigest()
        except (ValueError,TypeError) as exc:return {"status":"REJECTED","reason":str(exc)}
        with self.transaction():
            row=self.db.execute("SELECT * FROM agent_requests WHERE owner=? AND id=?",(self.owner,response["request_id"])).fetchone()
            if not row:return {"status":"REJECTED","reason":"unknown owned request"}
            if row["receipt"]:
                if row["response_digest"]!=digest:return {"status":"REJECTED","reason":"request already answered with different content"}
                return {"status":"DUPLICATE","original":json.loads(row["receipt"])}
            if response["context_hash"]!=row["context_hash"]:
                result={"status":"REJECTED","reason":"context hash mismatch"}
            elif self.clock()>=row["deadline"]:
                result={"status":"REJECTED","reason":"request expired; obtain fresh context"}
            elif self._state_hash(self._context())!=row["context_hash"]:
                result={"status":"REJECTED","reason":"controller state changed; obtain fresh context"}
            else:
                request=json.loads(row["payload"])
                value=envelope(response["decision"],"decision-"+row["id"],self.clock(),request["decision_expires_at"])
                result=self.submit(self.source,self.conversation,canonical(value))
            receipt={"request_id":row["id"],"received_at":self.clock(),"result":result}
            self.db.execute("UPDATE agent_requests SET response_digest=?,response=?,receipt=? WHERE owner=? AND id=?",
                (digest,text,canonical(receipt),self.owner,row["id"]))
            self._event("AGENT_RESPONSE",{"owner":self.owner,"request_id":row["id"],"response_sha256":digest,"receipt":receipt})
            return receipt
