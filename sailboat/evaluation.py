"""Semantic scoring and independent controller effect comparison."""
import json, tempfile
from pathlib import Path
from .controller import Controller
from .schema import Message, validate_plan, MUTATING

class Clock:
    def __init__(self): self.now=1000.
    def __call__(self): return self.now
    def advance(self,n=1): self.now+=n

def seed_controller(path,case,clock):
    c=Controller(path,clock=clock)
    ctx=case["context"]
    for i in ctx["instruments"]:
        c.register_instrument(i["id"],i["symbol"],i["kind"],i["multiplier"],json.loads(i["details"]))
    # Explicit synthetic initial state for evaluation; never imported into operational databases.
    with c.transaction():
        for pos in ctx["positions"]:
            c.db.execute("INSERT INTO positions VALUES(?,?,?,?,?)",
                         (pos["id"],"agent/case",pos["instrument"],pos["quantity"],pos["cost"]))
        for i in ctx["instructions"]:
            c.db.execute("INSERT INTO instructions VALUES(?,?,?,?,?,?)",
                (i["id"],"agent/case",json.dumps(i["plan"]),"ACTIVE",i["created"],i["expires"]))
        for o in ctx["orders"]:
            c.db.execute("INSERT INTO orders VALUES(?,?,?,?,?,?,?,?)",
                (o["id"],"agent/case",json.dumps(o["plan"]),"PENDING",o["created"],o["expires"],None,None))
        c._event("SYNTHETIC_EVALUATION_FIXTURE",{"case":case["id"]})
    for i in ctx["instruments"]:
        c.market_snapshot(i["id"],1.,2.,1.5,timestamp=clock()) if i["kind"]=="option" else c.market_snapshot(i["id"],99.,101.,100.,timestamp=clock())
    if ctx["mode"]=="PAPER_ARMED": c.set_mode("PAPER_ARMED")
    return c

def normalize(plan,case):
    p={k:v for k,v in plan.items() if v is not None}
    ctx=case["context"]
    if p.get("tool")=="close_simulated_position":
        positions=[x for x in ctx["positions"]
                   if (not p.get("target") or x["id"]==p["target"])
                   and (not p.get("instrument") or x["instrument"]==p["instrument"])]
        if len(positions)==1:
            p["target"]=positions[0]["id"]
            p.pop("instrument",None)
            if p.get("quantity")==positions[0]["quantity"]: p.pop("quantity")
            if p.get("action")=="sell": p.pop("action")
    # Symbol may be redundant when an exact executable instrument or target identifies it.
    if p["tool"] in {"create_simulated_order","monitor_price","replace_instruction","close_simulated_position"}:
        instrument=next((x for x in ctx["instruments"] if x["id"]==plan.get("instrument")),None)
        if instrument and p.get("symbol")==instrument["symbol"]: p.pop("symbol",None)
    if "symbol" not in case["expected"] and p.get("symbol") in {x["symbol"] for x in ctx["instruments"]}:
        p.pop("symbol",None)
    if "direction" not in case["expected"]: p.pop("direction",None)
    return p

def semantic_score(plan,case,error=None):
    valid=False
    try:
        clean=validate_plan(plan)
        valid=True
    except (ValueError,TypeError): clean={}
    expected=case["expected"]
    correct=valid and normalize(clean,case)==normalize(expected,case)
    predicted_tool=clean.get("tool")
    exp_mut=expected["tool"] in MUTATING
    pred_mut=predicted_tool in MUTATING
    return {"valid_schema":valid,"correct_tool":predicted_tool==expected["tool"],
            "semantic_success":correct,"incorrect_action":bool(pred_mut and not correct),
            "missed_action":bool(exp_mut and not correct),
            "commentary_to_action":bool(expected["tool"]=="record_commentary" and pred_mut),
            "error":error}

def effect_signature(c):
    state=c.status()
    return {"mode":state["mode"],"emergency_stop":state["emergency_stop"],
            "realized_pnl":state["realized_pnl"],
            "positions":sorted((x["id"],x["instrument"],x["quantity"],x["cost"]) for x in state["positions"]),
            "orders":sorted((x["id"],x["status"],json.loads(x["plan"]).get("instrument"),
                json.loads(x["plan"]).get("action"),json.loads(x["plan"]).get("quantity"),x["fill_price"]) for x in state["orders"]),
            "instructions":sorted((x["id"],x["status"],
                tuple(sorted((k,v) for k,v in json.loads(x["plan"]).items() if k not in {"direction","symbol"}))) for x in state["instructions"]),
            "fill_count":len(state["fills"])}

def execute_case(case,plan,path):
    clock=Clock()
    c=seed_controller(path,case,clock)
    message=Message("agent","case",case["id"],case["text"])
    result=c.apply(message,plan)
    behavior=case.get("behavior","")
    if "restart" in behavior:
        c.close()
        c=Controller(path,clock=clock)
    if "replay" in behavior:
        new_id=case["id"]+"-retry" if "new_id" in behavior else case["id"]
        replay=c.apply(Message("agent","case",new_id,case["text"]),plan)
        assert replay["status"]=="DUPLICATE"
    if behavior=="restart_trigger":
        clock.advance()
        c.market_snapshot("SPY",102,104,103)
        c.tick()
    signature=effect_signature(c)
    signature["outcome"]=result["status"]
    assert c.verify_journal()
    c.close()
    return signature

def compare_effects(case,plan):
    with tempfile.TemporaryDirectory(prefix="sailboat-eval-") as tmp:
        actual=execute_case(case,plan,Path(tmp)/"actual.sqlite")
        expected=execute_case(case,case["expected"],Path(tmp)/"expected.sqlite")
    return {"correct_controller_effect":actual==expected,"actual_effect":actual,"expected_effect":expected}
