"""Small persistent intent layer over the unchanged deterministic paper controller."""
import hashlib,json,re
from .controller import Controller,Rejected,canonical
from .intent_schema import validate_intent
from .schema import validate_plan
def mentioned(text,value):
    return bool(re.search(r"(?<![\w.])"+re.escape(value)+r"(?![\w.])",text,re.I))
def resolve(items,p,text):
    # IDs emitted by a model are not evidence that a reference was unambiguous.
    selected=[x for x in items if (not p.get("target") or x["id"]==p["target"])
              and (not p.get("instrument") or x.get("instrument",x.get("plan",{}).get("instrument"))==p["instrument"])
              and (not p.get("symbol") or x.get("instrument",x.get("plan",{}).get("instrument","")).split("_")[0]==p["symbol"])]
    if len(selected)!=1: raise Rejected("one owned unambiguous target required")
    x=selected[0]
    if len(items)>1 and not any(mentioned(text,v) for v in (x["id"],x.get("instrument",x.get("plan",{}).get("instrument","")) ) if v):
        raise Rejected("multiple targets require an explicit reference")
    return x

def project_tool(p,context,text):
    """Meaning-to-capability mapping. No model reasoning, market opinions or risk relaxation."""
    p=validate_intent(p)
    k=p["kind"]
    if k in ("observe","prefer","propose","withdraw","resume","clarify"):
        tool="record_commentary" if k in ("observe","withdraw") else "request_clarification"
        r={"tool":tool}
        if k not in ("withdraw","resume"):
            r.update({f:p[f] for f in ("symbol","direction") if f in p})
        if k in ("prefer","clarify") and p.get("condition"):
            c=p["condition"]
            r.update(condition_symbol=c["symbol"],operator=c["op"],price=c["price"])
        return r
    if k in ("disarm","stop","hold","quote"):
        r={"tool":{"disarm":"disarm","stop":"emergency_stop","hold":"maintain_position","quote":"read_market_snapshot"}[k]}
        if k=="quote":
            if "symbol" not in p: raise Rejected("snapshot symbol required")
            r["symbol"]=p["symbol"]
        return r
    if k=="cancel":
        candidates=context.get("instructions",[])+context.get("orders",[])
        x=resolve(candidates,p,text)
        return {"tool":"cancel_instruction" if any(i["id"]==x["id"] for i in context.get("instructions",[])) else "cancel_simulated_order","target":x["id"]}
    if k=="exit":
        x=resolve(context.get("positions",[]),p,text)
        q=x["quantity"] if p["scope"]=="all" else p["quantity"]
        if q>x["quantity"]: raise Rejected("exit exceeds position")
        explicit=re.findall(r"(?<![\w.\-])(\d+)\s+(?:shares?\b|contracts?\b|"+re.escape(x["instrument"])+r"\b)",text,re.I)
        if explicit and any(int(n)!=q for n in explicit): raise Rejected("exit size disagrees with explicit units")
        r={"tool":"close_simulated_position","target":x["id"]}
        if p["scope"]=="partial": r["quantity"]=q
        if p.get("condition"):
            r={"tool":"monitor_price","instrument":x["instrument"],"quantity":q,"action":"sell"}
    else:
        if "instrument" not in p or "quantity" not in p: raise Rejected("entry details missing")
        catalog=next((i for i in context.get("instruments",[]) if i["id"]==p["instrument"]),None)
        if re.search(r"\b(calls?|puts?|contracts?)\b",text,re.I) and catalog and catalog["kind"]!="option":
            raise Rejected("option language cannot be converted to a stock order")
        r={"tool":"monitor_price" if p.get("condition") else "create_simulated_order",
           "instrument":p["instrument"],"quantity":p["quantity"],"action":"buy"}
        if k=="revise":
            x=resolve(context.get("instructions",[]),p,text)
            r.update(tool="replace_instruction",target=x["id"])
    if k in ("enter","exit","revise"):
        conditional=re.search(r"\b(if|when|once|after|unless|until)\b|\bonly\s+(above|below|over|under)\b",text,re.I)
        if conditional and "condition" not in p: raise Rejected("conditional wording cannot execute immediately")
        if re.search(r"\b(do not|don't|never)\s+(buy|sell|execute|trade)\b",text,re.I):
            raise Rejected("negative execution language conflicts with action")
        if re.search(r"\blimit\b|maximum execution price",text,re.I) and "limit_price" not in p:
            raise Rejected("limit price missing")
    if p.get("condition"):
        c=p["condition"]
        r.update(condition_symbol=c["symbol"],operator=c["op"],price=c["price"])
    if "limit_price" in p: r["limit_price"]=p["limit_price"]
    return validate_plan(r)

class IntentController(Controller):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.db.execute("""CREATE TABLE IF NOT EXISTS agent_intents(
           owner TEXT,symbol TEXT,id TEXT UNIQUE,status TEXT,content TEXT,version INTEGER,
           linked_instruction TEXT,linked_order TEXT,updated REAL,PRIMARY KEY(owner,symbol))""")
    def context(self,message):
        ctx=super().context(message)
        ctx["agent_intents"]=[{"id":r["id"],"symbol":r["symbol"],"status":r["status"],
             "content":json.loads(r["content"]),"version":r["version"]}
             for r in self.db.execute("SELECT * FROM agent_intents WHERE owner=? ORDER BY symbol",(message.owner,))]
        return ctx
    @staticmethod
    def revision(ctx):
        return hashlib.sha256(canonical({k:ctx.get(k) for k in ("mode","positions","instructions","orders","agent_intents")}).encode()).hexdigest()
    def receive(self,message,interpreter):
        fingerprint=hashlib.sha256(" ".join(message.text.casefold().split()).encode()).hexdigest()
        row=self.db.execute("SELECT * FROM messages WHERE owner=? AND (id=? OR fingerprint=?)",
                            (message.owner,message.message_id,fingerprint)).fetchone()
        if row:
            if row["id"]==message.message_id and row["text"]!=message.text: raise Rejected("reused message id")
            return {"status":"DUPLICATE","original":json.loads(row["outcome"])}
        ctx=self.context(message)
        try: p=interpreter.interpret(message,ctx)
        except Exception as exc: return self.apply_intent(message,None,error=type(exc).__name__)
        return self.apply_intent(message,p,expected_revision=self.revision(ctx))
    def apply_intent(self,message,p,error=None,expected_revision=None):
        fp=hashlib.sha256(" ".join(message.text.casefold().split()).encode()).hexdigest()
        with self.transaction():
            row=self.db.execute("SELECT * FROM messages WHERE owner=? AND (id=? OR fingerprint=?)",
                                (message.owner,message.message_id,fp)).fetchone()
            if row:
                if row["id"]==message.message_id and row["text"]!=message.text: raise Rejected("reused message id")
                return {"status":"DUPLICATE","original":json.loads(row["outcome"])}
            self.db.execute("SAVEPOINT intent_proposal")
            try:
                if error: raise Rejected("interpretation failed: "+error)
                p=validate_intent(p)
                ctx=self.context(message)
                if expected_revision is not None and self.revision(ctx)!=expected_revision:
                    raise Rejected("state changed during inference; new message or explicit retry required")
                result=self._apply_intent(message,p,ctx)
            except (ValueError,KeyError) as exc:
                self.db.execute("ROLLBACK TO intent_proposal")
                result={"status":"REJECTED","reason":str(exc)}
            self.db.execute("RELEASE intent_proposal")
            saved=canonical(p)
            self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                (message.owner,message.message_id,message.text,fp,saved,canonical(result),self.clock()))
            self._event("AGENT_INTENT_DECISION",{"message":message.as_dict(),"intent":p,"result":result})
            return result
    def _apply_intent(self,message,p,ctx):
        kind=p["kind"]
        rows=list(self.db.execute("SELECT * FROM agent_intents WHERE owner=?",(message.owner,)))
        symbol=p.get("symbol") or (p.get("instrument") or "").split("_")[0] or None
        if kind in ("withdraw","resume") and not symbol:
            eligible=[r for r in rows if r["status"] not in ("OBSERVED","CANCELED")]
            if len(eligible)!=1: raise Rejected("one existing entry intent required")
            symbol=eligible[0]["symbol"]
        if kind=="cancel" and not (ctx["instructions"] or ctx["orders"]):
            eligible=[r for r in rows if (not symbol or r["symbol"]==symbol) and
                      (not p.get("target") or p["target"]==r["id"]) and r["status"]!="CANCELED"]
            if len(eligible)!=1: raise Rejected("one cancel target required")
            symbol=eligible[0]["symbol"]
            result={"status":"CANCELED"}
        elif kind in ("withdraw","resume"):
            result={"status":"SUSPENDED" if kind=="withdraw" else "NEEDS_INFORMATION"}
        else:
            tool=project_tool(p,ctx,message.text)
            result=self._dispatch(message,tool)
        old=next((r for r in rows if r["symbol"]==symbol),None)
        if kind in ("withdraw","resume","cancel") and old:
            eligible=[r for r in rows if r["status"] not in ("OBSERVED","CANCELED")]
            if len(eligible)>1 and not any(mentioned(message.text,v) for v in (old["symbol"],old["id"])):
                raise Rejected("multiple entry plans require an explicit intent reference")
        if kind=="resume" and not old:
            raise Rejected("no existing plan to reconsider")
        if kind=="withdraw":
            if not old: raise Rejected("no existing plan to withdraw")
            self._cancel_links(old)
        if kind=="cancel" and old: self._cancel_links(old)
        if kind in ("disarm","stop","quote","hold","exit") or not symbol:
            return result
        # Invalid controller execution never promotes an intent into active execution.
        if result["status"]=="REJECTED": return result
        if kind=="enter" and old and old["status"]=="INSTRUCTED":
            # New IDs must not duplicate an already pending plan.
            for col,table,status in (("linked_instruction","instructions","ACTIVE"),("linked_order","orders","PENDING")):
                linked=old[col]
                if linked and self.db.execute(f"SELECT 1 FROM {table} WHERE id=? AND status=?",(linked,status)).fetchone():
                    raise Rejected("existing executable plan requires explicit revision")
        content=json.loads(old["content"]) if old else {}
        # Lifecycle operations change status only; they cannot rewrite retained contract/price/size details.
        if kind not in ("withdraw","resume","cancel"):
            content.update({k:v for k,v in p.items() if k not in ("kind","target")})
        content["last_kind"]=kind
        content["last_message_id"]=message.message_id
        if kind=="observe":
            status=old["status"] if old else "OBSERVED"
        else:
            status={"prefer":"PREFERRED","propose":"PROPOSED","withdraw":"SUSPENDED","resume":"PREFERRED",
                    "cancel":"CANCELED","clarify":"NEEDS_INFORMATION","enter":"INSTRUCTED","revise":"INSTRUCTED"}[kind]
        ident=old["id"] if old else "intent-"+hashlib.sha256((message.owner+"/"+symbol).encode()).hexdigest()[:16]
        version=old["version"]+1 if old else 1
        self.db.execute("""INSERT INTO agent_intents VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(owner,symbol) DO UPDATE SET status=excluded.status,content=excluded.content,
            version=excluded.version,linked_instruction=excluded.linked_instruction,
            linked_order=excluded.linked_order,updated=excluded.updated""",
            (message.owner,symbol,ident,status,canonical(content),version,
             result.get("instruction_id",old["linked_instruction"] if old else None),
             result.get("order_id",old["linked_order"] if old else None),self.clock()))
        self._event("INTENT_UPDATED",{"id":ident,"version":version,"status":status,"kind":kind})
        return result|{"intent_id":ident,"intent_version":version,"intent_status":status}
    def _cancel_links(self,row):
        if row["linked_instruction"]:
            self.db.execute("UPDATE instructions SET status='CANCELED' WHERE id=? AND status='ACTIVE'",(row["linked_instruction"],))
            self.db.execute("UPDATE orders SET status='CANCELED' WHERE instruction_id=? AND status='PENDING'",(row["linked_instruction"],))
        if row["linked_order"]:
            self.db.execute("UPDATE orders SET status='CANCELED' WHERE id=? AND status='PENDING'",(row["linked_order"],))
    def status(self):
        s=super().status()
        s["agent_intents"]=[dict(x)|{"content":json.loads(x["content"])} for x in self.db.execute("SELECT * FROM agent_intents")]
        return s
