"""Phase 2 owner console. Legacy console remains available for Phase 1 reproduction."""
import argparse,json,time
from pathlib import Path
from .intent_controller import IntentController
from .intent_model import IntentInterpreter
from .schema import Message
from .monitoring import MonitorProcess,save_receipt
def main():
    p=argparse.ArgumentParser(description="SAILBOAT Phase 2 simulation only")
    p.add_argument("--db",default="state/phase2.sqlite")
    s=p.add_subparsers(dest="cmd",required=True)
    for name in ("status","stop","reset-stop","verify"): s.add_parser(name)
    mode=s.add_parser("mode"); mode.add_argument("mode",choices=["DISARMED","PAPER_ARMED"])
    reg=s.add_parser("register");reg.add_argument("instrument")
    quote=s.add_parser("quote");quote.add_argument("symbol");quote.add_argument("bid",type=float);quote.add_argument("ask",type=float);quote.add_argument("--last",type=float)
    msg=s.add_parser("message");msg.add_argument("text");msg.add_argument("--source",default="manual");msg.add_argument("--conversation",default="default");msg.add_argument("--id",required=True)
    work=s.add_parser("worker");work.add_argument("--inbox",default="state/phase2-inbox");work.add_argument("--quotes",default="state/phase2-quotes");work.add_argument("--once",action="store_true")
    a=p.parse_args()
    c=IntentController(a.db);monitor=None
    try:
        if a.cmd=="status": result=c.status()
        elif a.cmd=="stop": c.emergency_stop();result=c.status()
        elif a.cmd=="reset-stop":c.reset_emergency_stop();result=c.status()
        elif a.cmd=="verify":result={"journal_valid":c.verify_journal()}
        elif a.cmd=="mode":c.set_mode(a.mode);result={"mode":a.mode}
        elif a.cmd=="register":c.register_instrument(a.instrument);result={"registered":a.instrument}
        elif a.cmd=="quote":result=c.market_snapshot(a.symbol,a.bid,a.ask,a.last)
        else:
            if a.cmd=="worker": monitor=MonitorProcess(a.db,a.quotes,c.limits).start()
            root=Path(__file__).resolve().parents[1]
            lock=json.loads((root/"model-lock.json").read_text())
            model_id="Qwen/Qwen3.5-2B"
            model=IntentInterpreter(model_id,lock[model_id]["revision"],str(root/".cache/models/Qwen3.5-2B"))
            if a.cmd=="message":result=c.receive(Message(a.source,a.conversation,a.id,a.text),model)
            else:
                inbox=Path(a.inbox);inbox.mkdir(parents=True,exist_ok=True)
                receipts=inbox.parent/"phase2-message-receipts";receipts.mkdir(exist_ok=True)
                while True:
                    if not monitor.process.is_alive():c.emergency_stop();raise RuntimeError("monitor exited")
                    for f in sorted(inbox.glob("*.json")):
                        dest=receipts/f.name
                        if dest.exists():continue
                        try:out=c.receive(Message(**json.loads(f.read_text())),model)
                        except Exception as exc:out={"status":"FAILED","reason":str(exc)}
                        save_receipt(dest,out)
                    if a.once:break
                    time.sleep(.1)
                result={"status":"WORKER_EXIT"}
        print(json.dumps(result,indent=2))
    finally:
        if monitor:monitor.close()
        c.close()
if __name__=="__main__":main()
