"""Restartable model comparison; each finished case is a durable immutable JSON record."""
import argparse, hashlib, importlib.metadata, json, os, platform, statistics, sys
import threading, time, uuid
from pathlib import Path
from .schema import Message
from .interpreters import PROMPT, TransformersInterpreter, ConservativeBaseline
from .evaluation import semantic_score, compare_effects

ROOT=Path(__file__).resolve().parents[1]
def atomic_json(path,obj):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    with temporary.open("x",encoding="utf-8") as file:
        json.dump(obj,file,indent=2,allow_nan=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary,path)

class MemorySampler:
    def __init__(self):
        import psutil
        self.proc=psutil.Process()
        self.peak=0
        self.stop=threading.Event()
        self.thread=threading.Thread(target=self.sample,daemon=True)
    def sample(self):
        while not self.stop.wait(.05):
            self.peak=max(self.peak,self.proc.memory_info().rss)
    def __enter__(self):
        self.peak=self.proc.memory_info().rss
        self.thread.start()
        return self
    def __exit__(self,*args):
        self.stop.set()
        self.thread.join()
        self.peak=max(self.peak,self.proc.memory_info().rss)

def summarize(out):
    records=[json.loads(p.read_text()) for p in sorted((out/"cases").glob("*.json"))]
    manifest=json.loads((out/"manifest.json").read_text())
    groups={}
    for split in ("all","development","heldout"):
        rows=[r for r in records if split=="all" or r["split"]==split]
        n=len(rows)
        metrics={k:sum(bool(r.get(k)) for r in rows) for k in
                 ("valid_schema","correct_tool","semantic_success","incorrect_action",
                  "missed_action","commentary_to_action","correct_controller_effect")}
        metrics.update({"n":n,"semantic_accuracy":metrics["semantic_success"]/n if n else None,
                        "latency_p50_s":statistics.median(r["latency_s"] for r in rows) if n else None,
                        "latency_p95_s":sorted(r["latency_s"] for r in rows)[max(0,int(.95*n+.999)-1)] if n else None})
        groups[split]=metrics
    by_category={}
    for r in records:
        item=by_category.setdefault(r["category"],{"n":0,"semantic_success":0,"incorrect_action":0})
        item["n"]+=1
        item["semantic_success"]+=bool(r["semantic_success"])
        item["incorrect_action"]+=bool(r["incorrect_action"])
    summary={"run":manifest,"completed":len(records),"total":manifest["total"],
             "disposition":"COMPLETED" if len(records)==manifest["total"] else "INCOMPLETE",
             "groups":groups,"by_category":by_category,
             "peak_rss_gib":max((r["process_peak_rss_bytes"]/2**30 for r in records),default=None),
             "max_gpu_allocated_gib":max((r.get("gpu_peak_allocated_bytes",0)/2**30 for r in records),default=0)}
    startup=out/"runtime.json"
    if startup.exists(): summary["runtime"]=json.loads(startup.read_text())
    atomic_json(out/"summary.json",summary)
    return summary

def run(model_id,out,device="cpu",threads=2,quantization="none",limit=None,allow_gated=False,max_seconds=90):
    out=Path(out)
    out.mkdir(parents=True,exist_ok=True)
    data=(ROOT/"scenarios"/"sailboat_v1.jsonl").read_bytes()
    cases=[json.loads(x) for x in data.decode().splitlines()]
    if limit: cases=cases[:limit]
    lock=json.loads((ROOT/"model-lock.json").read_text())
    revision=lock[model_id]["revision"] if model_id!="baseline" else "rules-v1"
    source_digest=hashlib.sha256()
    for f in sorted((ROOT/"sailboat").glob("*.py")):
        source_digest.update(f.name.encode()+f.read_bytes())
    manifest={"model":model_id,"revision":revision,"device":device,"threads":threads,
              "quantization":quantization,"max_seconds":max_seconds,"max_new_tokens":192,
              "max_input_tokens":4096,"decoding":"greedy","seed":1729,"total":len(cases),
              "corpus_sha256":hashlib.sha256(data).hexdigest(),
              "prompt_sha256":hashlib.sha256(PROMPT.encode()).hexdigest(),
              "source_sha256":source_digest.hexdigest(),"precision":"bf16 CPU; bf16/fp16 GPU",
              "protocol":"v1"}
    mpath=out/"manifest.json"
    if mpath.exists():
        if json.loads(mpath.read_text())!=manifest:
            raise ValueError("Run manifest changed. New directory required; original evidence preserved.")
    else: atomic_json(mpath,manifest)
    if model_id!="baseline" and lock[model_id]["gated"] and not allow_gated:
        atomic_json(out/"status.json",{"status":"SKIPPED_GATED","reason":"No authorized access supplied. No gate bypass attempted."})
        return
    if len(list((out/"cases").glob("*.json")))==len(cases):
        print(json.dumps(summarize(out)["groups"]),flush=True)
        return
    # Constrain only this benchmark process; no changes to existing workloads.
    import psutil
    proc=psutil.Process()
    if os.name=="nt": proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    os.environ["TOKENIZERS_PARALLELISM"]="false"
    os.environ["OMP_NUM_THREADS"]=str(threads)
    started=time.perf_counter()
    with MemorySampler() as memory:
        try:
            if model_id=="baseline":
                model=ConservativeBaseline()
            else:
                local=ROOT/".cache"/"models"/model_id.split("/")[-1]
                model=TransformersInterpreter(model_id,revision,str(local) if local.exists() else None,
                    device,threads,quantization,max_seconds=max_seconds)
            runtime={"load_seconds":time.perf_counter()-started,"python":sys.version,
                     "platform":platform.platform(),"cpu":platform.processor(),
                     "metadata":getattr(model,"metadata",{"not_a_model":True}),
                     "packages":{n:importlib.metadata.version(n) for n in ("transformers","torch","psutil","huggingface_hub")}}
            # Warm-up excluded from scored cases; raw result and timing retained separately.
            warm=time.perf_counter()
            try:
                warm_plan=model.interpret(Message("agent","warmup","warmup","Read the QQQ market snapshot."),cases[0]["context"])
                warm_result={"plan":warm_plan}
            except Exception as exc: warm_result={"error":type(exc).__name__,"message":str(exc)}
            runtime["warmup"]={**warm_result,"seconds":time.perf_counter()-warm,"raw":getattr(model,"last_raw",None)}
            runtime["peak_load_rss_gib"]=memory.peak/2**30
            atomic_json(out/"runtime.json",runtime)
        except Exception as exc:
            atomic_json(out/("load-failure-"+uuid.uuid4().hex+".json"),
                        {"status":"FAILED","phase":"load","error":type(exc).__name__,"message":str(exc),
                         "seconds":time.perf_counter()-started,"peak_rss_gib":memory.peak/2**30})
            raise
        for case in cases:
            destination=out/"cases"/(case["id"]+".json")
            if destination.exists(): continue
            attempt=out/"attempts"/(case["id"]+"-"+uuid.uuid4().hex+".json")
            atomic_json(attempt,{"case":case["id"],"started_utc_epoch":time.time(),"status":"STARTED"})
            if device=="cuda": model.torch.cuda.reset_peak_memory_stats()
            begin=time.perf_counter()
            plan=None
            error=None
            try: plan=model.interpret(Message("agent","case",case["id"],case["text"]),case["context"])
            except Exception as exc: error=type(exc).__name__+": "+str(exc)
            latency=time.perf_counter()-begin
            score=semantic_score(plan,case,error)
            effects=compare_effects(case,plan)
            record={"id":case["id"],"category":case["category"],"split":case["split"],"text":case["text"],
                    "expected":case["expected"],"predicted":plan,"raw":getattr(model,"last_raw",None),
                    "tokens":getattr(model,"last_tokens",None),"latency_s":latency,
                    "process_peak_rss_bytes":memory.peak,
                    "gpu_peak_allocated_bytes":model.torch.cuda.max_memory_allocated() if device=="cuda" else 0,
                    **score,**effects}
            atomic_json(destination,record)
            atomic_json(attempt.with_suffix(".completed.json"),{"case":case["id"],"completed_utc_epoch":time.time(),
                       "record_sha256":hashlib.sha256(destination.read_bytes()).hexdigest()})
            if case==cases[0] or int(case["id"][1:])%10==0:
                print(json.dumps({"model":model_id,"case":case["id"],"semantic_success":score["semantic_success"],
                                  "latency_s":round(latency,2),"peak_rss_gib":round(memory.peak/2**30,2)}),flush=True)
            summarize(out)
    print(json.dumps(summarize(out)["groups"]),flush=True)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--model",required=True)
    parser.add_argument("--output",required=True)
    parser.add_argument("--device",choices=["cpu","cuda"],default="cpu")
    parser.add_argument("--threads",type=int,default=2)
    parser.add_argument("--quantization",choices=["none","bnb4"],default="none")
    parser.add_argument("--limit",type=int)
    parser.add_argument("--allow-gated",action="store_true")
    parser.add_argument("--max-seconds",type=float,default=90)
    args=parser.parse_args()
    run(args.model,args.output,args.device,args.threads,args.quantization,args.limit,args.allow_gated,args.max_seconds)
if __name__=="__main__": main()
