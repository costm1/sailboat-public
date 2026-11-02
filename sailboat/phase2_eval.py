"""Frozen-label conversation evaluation. No labels, categories or future turns enter prompts."""
import json,hashlib,time,statistics,os,sys,zipfile
from pathlib import Path
from collections import Counter
from .intent_controller import IntentController,project_tool,resolve
from .controller import Controller
from .intent_schema import validate_intent
from .intent_model import IntentInterpreter,PROMPT2
from .interpreters import TransformersInterpreter
from .schema import Message,MUTATING
from .evaluation import Clock,semantic_score,compare_effects
from .benchmark import atomic_json,MemorySampler
ROOT=Path(__file__).resolve().parents[1]
EXEC={"enter","exit","cancel","revise","withdraw","disarm","stop"}
UPDATES={"prefer","propose","withdraw","resume","revise","cancel"}

def legacy_intent(p,ctx):
    if not p:return None
    kind={"record_commentary":"observe","request_clarification":"clarify","read_market_snapshot":"quote",
          "create_simulated_order":"enter","monitor_price":"enter","close_simulated_position":"exit",
          "maintain_position":"hold","cancel_instruction":"cancel","cancel_simulated_order":"cancel",
          "replace_instruction":"revise","disarm":"disarm","emergency_stop":"stop"}[p["tool"]]
    if p["tool"] in ("monitor_price","create_simulated_order") and p.get("action")=="sell":kind="exit"
    r={"kind":kind}|{k:v for k,v in p.items() if k in ("symbol","instrument","quantity","target","direction","limit_price")}
    if "operator" in p:r["condition"]={"symbol":p["condition_symbol"],"op":p["operator"],"price":p["price"]}
    if kind=="exit":r["scope"]="partial" if "quantity" in p else "all"
    return r

def normalize(p,ctx):
    p=validate_intent(p);k=p["kind"]
    out={"kind":k}
    if k in ("enter","revise"):
        out.update({f:p[f] for f in ("instrument","quantity","condition","limit_price") if f in p})
        if k=="revise":out["target"]=resolve(ctx["instructions"],p," ".join([p.get("target",""),p.get("instrument","")]))["id"]
    elif k=="exit":
        x=resolve(ctx["positions"],p," ".join([p.get("target",""),p.get("instrument",""),p.get("symbol","")]))
        out.update(symbol=x["instrument"],quantity=x["quantity"] if p["scope"]=="all" else p["quantity"])
        out.update({f:p[f] for f in ("condition","limit_price") if f in p})
    elif k=="cancel":
        items=ctx["instructions"]+ctx["orders"]
        if items:out["target"]=resolve(items,p," ".join([p.get("target",""),p.get("instrument",""),p.get("symbol","")]))["id"]
        else:
            intents=ctx.get("agent_intents",[])
            selected=[x for x in intents if not p.get("symbol") or x["symbol"]==p["symbol"]]
            out["symbol"]=selected[0]["symbol"] if len(selected)==1 else p.get("symbol")
    elif k in ("withdraw","resume"):
        out["symbol"]=p.get("symbol")
        if not out["symbol"]:
            intents=[x for x in ctx.get("agent_intents",[]) if x["status"] not in ("OBSERVED","CANCELED")]
            if len(intents)==1:out["symbol"]=intents[0]["symbol"]
    elif k in ("observe","prefer","propose","clarify","quote"):
        out.update({f:p[f] for f in ("symbol","direction","instrument","quantity","condition","limit_price") if f in p})
    return out
def score(p,expected,ctx):
    try:
        actual=normalize(p,ctx);correct=actual==normalize(expected,ctx);valid=True
    except (ValueError,KeyError,TypeError):actual=None;correct=False;valid=False
    kind=p.get("kind") if isinstance(p,dict) else None
    return {"semantic_correct":correct,"valid_intent":valid,
        "wrong_actions":bool(kind in EXEC and not correct),
        "missed_actions":bool(expected["kind"] in EXEC and not correct),
        "wrong_order_proposals":bool(kind in ("enter","exit","revise") and not correct),
        "missed_order_instructions":bool(expected["kind"] in ("enter","exit","revise") and not correct),
        "normalized_actual":actual}

def seed(path,conversation,legacy=False):
    clock=Clock();c=(Controller if legacy else IntentController)(path,clock=clock)
    for symbol in conversation.get("catalog",["QQQ","SPY","IWM","AAPL","MSFT"]):c.register_instrument(symbol)
    for symbol in conversation.get("catalog",["QQQ","SPY","IWM","AAPL","MSFT"]):c.market_snapshot(symbol,99,101,100)
    c.set_mode(conversation.get("mode","PAPER_ARMED"))
    with c.transaction():
        for symbol,quantity in conversation.get("positions",[]):
            pid="seed-"+symbol
            c.db.execute("INSERT INTO positions VALUES(?,?,?,?,?)",(pid,"agent/case",symbol,quantity,"100"))
        c._event("SYNTHETIC_CONVERSATION_START",{"conversation":conversation["id"]})
    return c,clock

def fills(c):
    return [dict(x) for x in c.db.execute("""SELECT p.instrument,f.quantity,f.action,f.price FROM fills f
          JOIN positions p ON p.id=f.position_id ORDER BY f.ts,f.order_id""")]
def intents(c):
    try:rows=list(c.db.execute("SELECT symbol,status,content,version FROM agent_intents ORDER BY symbol"))
    except Exception:return []
    return [{"symbol":r["symbol"],"status":r["status"],
       "content":{k:v for k,v in json.loads(r["content"]).items() if k not in ("last_message_id","last_kind","symbol","scope")},
       "version":r["version"]} for r in rows]
def effect(c):
    s=c.status()
    return {"positions":sorted((x["instrument"],x["quantity"]) for x in s["positions"] if x["quantity"]),
            "fills":fills(c),"mode":s["mode"],"emergency_stop":s["emergency_stop"],
            "active_instructions":sorted(json.dumps(json.loads(x["plan"]),sort_keys=True) for x in s["instructions"] if x["status"]=="ACTIVE"),
            "pending_orders":sorted(json.dumps(json.loads(x["plan"]),sort_keys=True) for x in s["orders"] if x["status"]=="PENDING")}
def summarize(out):
    rows=[json.loads(p.read_text()) for p in sorted((out/"turns").glob("*.json"))]
    n=len(rows)
    metrics={k:sum(bool(x.get(k)) for x in rows) for k in (
        "semantic_correct","valid_intent","wrong_actions","missed_actions","wrong_order_proposals",
        "missed_order_instructions","incorrect_fill_case","correct_effect","correct_intent_state")}
    metrics["n"]=n;metrics["accuracy"]=metrics["semantic_correct"]/n if n else None
    updates=[r for r in rows if r["expected"]["kind"] in ("withdraw","resume","revise","cancel")]
    metrics["plan_change_turns"]=len(updates)
    metrics["correct_plan_changes"]=sum(r["correct_intent_state"] and r["semantic_correct"] for r in updates)
    metrics["incorrect_fills"]=sum(r["incorrect_fills"] for r in rows)
    metrics["missed_fills"]=sum(r["missed_fills"] for r in rows)
    metrics["latency_median_s"]=statistics.median(r["latency_s"] for r in rows) if rows else None
    metrics["latency_p95_s"]=sorted(r["latency_s"] for r in rows)[max(0,int(n*.95+.999)-1)] if rows else None
    metrics["peak_ram_gib"]=max((r["peak_ram_gib"] for r in rows),default=0)
    metrics["peak_gpu_allocated_gib"]=max((r.get("peak_gpu_allocated_gib",0) for r in rows),default=0)
    metrics["conversations"]=len({r["conversation"] for r in rows})
    metrics["all_correct_conversations"]=sum(all(r["semantic_correct"] and r["correct_effect"] for r in rows if r["conversation"]==cid) for cid in {r["conversation"] for r in rows})
    metrics["context_drift_turns"]=sum(r["context_drift"] for r in rows)
    metrics["wrong_actions_definition"]="Incorrect mutating intent: enter,exit,cancel,revise,withdraw,disarm,stop. May overlap missed actions."
    atomic_json(out/"summary.json",metrics)
    return metrics

def conversation_run(model,out,variant,corpus,memory):
    conversations=[json.loads(line) for line in corpus.read_text().splitlines()]
    for conv in conversations:
        work=out/"recovery"/(conv["id"]+"-"+str(time.time_ns()))
        work.mkdir(parents=True)
        actual,aclock=seed(work/"actual.sqlite",conv,legacy=variant=="phase1")
        reference,rclock=seed(work/"reference.sqlite",conv)
        try:
            for index,t in enumerate(conv["turns"]):
                ident=conv["id"]+"-"+str(index+1).zfill(2)
                path=out/"turns"/(ident+".json")
                before_a=fills(actual);before_r=fills(reference)
                for c,clock in ((actual,aclock),(reference,rclock)):
                    clock.advance()
                    for symbol,bid,ask,last in t.get("quotes",[]):c.market_snapshot(symbol,bid,ask,last)
                    if t.get("restart"):
                        c.close()
                if t.get("restart"):
                    actual=(Controller if variant=="phase1" else IntentController)(work/"actual.sqlite",clock=aclock)
                    reference=IntentController(work/"reference.sqlite",clock=rclock)
                message=Message("agent","case",ident,t["text"])
                ctx=actual.context(message);refctx=reference.context(message)
                drift=effect(actual)!=effect(reference)
                if path.exists():
                    receipt=json.loads((out/"receipts"/(ident+".json")).read_text())
                    assert receipt["sha256"]==hashlib.sha256(path.read_bytes()).hexdigest(),"saved record modified"
                    old=json.loads(path.read_text());wire=old["wire"];elapsed=old["latency_s"];raw=old["raw"];error=old["error"]
                else:
                    atomic_json(out/"attempts"/(ident+"-"+str(time.time_ns())+".json"),{"status":"STARTED","turn":ident,"utc_epoch":time.time()})
                    start=time.perf_counter();wire=None;error=None
                    if model.device=="cuda":model.torch.cuda.reset_peak_memory_stats()
                    try:wire=model.interpret(message,ctx)
                    except Exception as exc:error=type(exc).__name__+": "+str(exc)
                    elapsed=time.perf_counter()-start;raw=model.last_raw
                if variant=="phase1":
                    result=actual.apply(message,wire);proposal=legacy_intent(wire,ctx) if wire else None
                else:
                    result=actual.apply_intent(message,wire,error=error);proposal=wire
                refresult=reference.apply_intent(message,t["expected"])
                if refresult["status"]=="REJECTED":raise AssertionError("Invalid frozen label "+ident+str(refresult))
                # Follow-up quote events measure trigger behavior, including boundaries and gaps.
                for c,clock in ((actual,aclock),(reference,rclock)):
                    for symbol,bid,ask,last in t.get("after_quotes",[]):
                        clock.advance();c.market_snapshot(symbol,bid,ask,last)
                af=fills(actual)[len(before_a):];rf=fills(reference)[len(before_r):]
                aset=Counter(json.dumps(x,sort_keys=True) for x in af)
                rset=Counter(json.dumps(x,sort_keys=True) for x in rf)
                incorrect=sum((aset-rset).values());missed=sum((rset-aset).values())
                if t.get("replay"):
                    replay=actual.apply(message,wire) if variant=="phase1" else actual.apply_intent(message,wire,error=error)
                    assert replay["status"]=="DUPLICATE"
                scored=score(proposal,t["expected"],refctx)
                row={"id":ident,"conversation":conv["id"],"style":conv["style"],"text":t["text"],
                     "expected":t["expected"],"wire":wire,"intent":proposal,"raw":raw,"error":error,
                     "context":ctx,"reference_context":refctx,"context_drift":drift,
                     "result":result,"expected_result":refresult,**scored,
                     "actual_effect":effect(actual),"expected_effect":effect(reference),
                     "correct_effect":effect(actual)==effect(reference),
                     "actual_intents":intents(actual),"expected_intents":intents(reference),
                     "correct_intent_state":intents(actual)==intents(reference),
                     "incorrect_fills":incorrect,"missed_fills":missed,"incorrect_fill_case":incorrect>0,
                     "latency_s":elapsed,"peak_ram_gib":memory.peak/2**30,
                     "peak_gpu_allocated_gib":model.torch.cuda.max_memory_allocated()/2**30 if model.device=="cuda" else 0}
                if not path.exists():
                    atomic_json(path,row)
                    atomic_json(out/"receipts"/(ident+".json"),{"sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
                assert actual.verify_journal() and reference.verify_journal()
                print(json.dumps({"id":ident,"correct":row["semantic_correct"],"wrong_fill":incorrect,"seconds":round(elapsed,2)}),flush=True)
        finally:actual.close();reference.close()
    return summarize(out)

def regression(model,out,memory):
    rows=[]
    for case in map(json.loads,(ROOT/"scenarios/sailboat_v1.jsonl").read_text().splitlines()):
        path=out/"cases"/(case["id"]+".json")
        if path.exists():rows.append(json.loads(path.read_text()));continue
        p=None;tool=None;error=None;start=time.perf_counter()
        try:
            p=model.interpret(Message("agent","case",case["id"],case["text"]),case["context"])
            tool=project_tool(p,case["context"],case["text"])
        except Exception as exc:error=repr(exc)
        scoring=semantic_score(tool,case,error)
        effects=compare_effects(case,tool)
        row={"id":case["id"],"intent":p,"predicted":tool,"raw":model.last_raw,"expected":case["expected"],
             **scoring,**effects,"latency_s":time.perf_counter()-start,"peak_ram_gib":memory.peak/2**30}
        atomic_json(path,row);rows.append(row)
        print(json.dumps({"regression":case["id"],"correct":row["semantic_success"]}),flush=True)
    result={"n":len(rows),"semantic_correct":sum(r["semantic_success"] for r in rows),
            "wrong_actions":sum(r["incorrect_action"] for r in rows),"missed_actions":sum(r["missed_action"] for r in rows),
            "correct_effect":sum(r["correct_controller_effect"] for r in rows),
            "note":"Unchanged Phase 1 labels and deterministic core, new intent output projected to legacy tools. Exposed regression, not fresh holdout."}
    atomic_json(out/"summary.json",result);return result

def main():
    import argparse,psutil
    p=argparse.ArgumentParser();p.add_argument("--variant",choices=["phase1","phase2"],default="phase2")
    p.add_argument("--suite",choices=["fresh","regression"],default="fresh");p.add_argument("--device",default="cpu",choices=["cpu","cuda"])
    p.add_argument("--output",required=True);p.add_argument("--corpus",default="scenarios/phase2_fresh.jsonl")
    args=p.parse_args()
    if os.name=="nt":psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    source=hashlib.sha256(b"".join(f.name.encode()+f.read_bytes() for f in sorted((ROOT/"sailboat").glob("*.py")))).hexdigest()
    manifest={"variant":args.variant,"suite":args.suite,"source_sha256":source,
              "prompt_sha256":hashlib.sha256(PROMPT2.encode()).hexdigest(),"device":args.device,
              "corpus_sha256":hashlib.sha256((ROOT/args.corpus).read_bytes()).hexdigest(),
              "generation":"greedy, seed1729, 192 new tokens, 4096 input tokens, two threads, 90 seconds"}
    if (out/"manifest.json").exists():assert json.loads((out/"manifest.json").read_text())==manifest
    else:atomic_json(out/"manifest.json",manifest)
    lock=json.loads((ROOT/"model-lock.json").read_text());mid="Qwen/Qwen3.5-2B"
    klass=IntentInterpreter if args.variant=="phase2" else TransformersInterpreter
    with MemorySampler() as memory:
        model=klass(mid,lock[mid]["revision"],str(ROOT/".cache/models/Qwen3.5-2B") if (ROOT/".cache/models/Qwen3.5-2B").exists() else None,device=args.device)
        # One excluded warm-up, retained. No heldout item is a warm-up.
        start=time.perf_counter()
        try: warm=model.interpret(Message("manual","warm","warm","Read the XYZ snapshot."),
              {"mode":"DISARMED","instruments":[],"positions":[],"instructions":[],"orders":[],"recent_messages":[]})
        except Exception as exc:warm={"error":repr(exc)}
        atomic_json(out/("warmup-"+str(time.time_ns())+".json"),{"answer":warm,"raw":model.last_raw,"seconds":time.perf_counter()-start,"metadata":model.metadata})
        result=conversation_run(model,out,args.variant,ROOT/args.corpus,memory) if args.suite=="fresh" else regression(model,out,memory)
        print(json.dumps(result,indent=2))
if __name__=="__main__":main()
