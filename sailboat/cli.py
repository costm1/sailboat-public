"""Local owner interface and provider-neutral mailbox worker."""
import argparse
import json
import os
from pathlib import Path
import time
from .controller import Controller, Limits
from .schema import Message
from .interpreters import ConservativeBaseline, TransformersInterpreter

def interpreter(args):
    if args.model=="baseline": return ConservativeBaseline()
    root=Path(__file__).resolve().parents[1]
    lock=json.loads((root/"model-lock.json").read_text())
    model=args.model
    if lock[model]["gated"] and not args.allow_gated:
        raise ValueError("Gated candidate skipped until owner has access; use --allow-gated only after access")
    local=root/".cache"/"models"/model.split("/")[-1]
    return TransformersInterpreter(model,lock[model]["revision"],
                                   model_path=str(local) if local.exists() else None,
                                   device=args.device,threads=args.threads)

def main():
    parser=argparse.ArgumentParser(description="SAILBOAT simulation-only owner console")
    parser.add_argument("--db",default="state/controller.sqlite")
    parser.add_argument("--limits",help="Owner JSON limits; must match when reopening state")
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("status")
    mode=commands.add_parser("mode")
    mode.add_argument("mode",choices=["DISARMED","PAPER_ARMED","LIVE"])
    commands.add_parser("stop")
    commands.add_parser("reset-stop")
    catalog=commands.add_parser("register")
    catalog.add_argument("instrument")
    catalog.add_argument("--symbol")
    catalog.add_argument("--option",help="JSON with expiry,strike,right")
    quote=commands.add_parser("quote")
    quote.add_argument("symbol")
    quote.add_argument("bid",type=float)
    quote.add_argument("ask",type=float)
    quote.add_argument("--last",type=float)
    commands.add_parser("tick")
    commands.add_parser("verify")
    backup=commands.add_parser("backup")
    backup.add_argument("destination")
    for name in ("message","worker"):
        sub=commands.add_parser(name)
        sub.add_argument("--model",default="Qwen/Qwen3.5-2B")
        sub.add_argument("--device",choices=["cpu","cuda"],default="cpu")
        sub.add_argument("--threads",type=int,default=2)
        sub.add_argument("--allow-gated",action="store_true")
        if name=="message":
            sub.add_argument("text")
            sub.add_argument("--source",default="manual")
            sub.add_argument("--conversation",default="default")
            sub.add_argument("--id",required=True)
        else:
            sub.add_argument("--inbox",default="state/inbox")
            sub.add_argument("--quotes",default="state/quotes")
            sub.add_argument("--once",action="store_true")
    args=parser.parse_args()
    limits=Limits(**json.loads(Path(args.limits).read_text())) if args.limits else None
    controller=Controller(args.db,limits=limits)
    try:
        if args.command=="status": result=controller.status()
        elif args.command=="mode":
            controller.set_mode(args.mode)
            result={"mode":args.mode}
        elif args.command=="stop":
            controller.emergency_stop()
            result={"status":"STOPPED","positions_retained":True}
        elif args.command=="reset-stop":
            controller.reset_emergency_stop()
            result={"mode":"DISARMED"}
        elif args.command=="register":
            details=json.loads(args.option) if args.option else None
            controller.register_instrument(args.instrument,args.symbol,"option" if details else "stock",
                                           100 if details else 1,details)
            result={"registered":args.instrument}
        elif args.command=="quote":
            result=controller.market_snapshot(args.symbol,args.bid,args.ask,args.last)
        elif args.command=="tick":
            controller.tick()
            result={"status":"TICKED"}
        elif args.command=="verify": result={"journal_valid":controller.verify_journal()}
        elif args.command=="backup":
            controller.backup(args.destination)
            result={"backup":args.destination}
        elif args.command=="message":
            result=controller.receive(Message(args.source,args.conversation,args.id,args.text),interpreter(args))
        elif args.command=="worker":
            worker(controller,interpreter(args),Path(args.inbox),Path(args.quotes),args.once)
            result={"status":"WORKER_EXIT"}
        print(json.dumps(result,indent=2))
    finally: controller.close()

def worker(controller,model,inbox,quotes,once=False):
    """One local worker. No network listener; quotes are a trusted synthetic feed directory.
    Immutable receipt files permit restart; DB idempotency covers commit-before-receipt crashes.
    Keep owner CLI/control access separate from message-file permissions in future deployments.
    """
    inbox.mkdir(parents=True,exist_ok=True)
    quotes.mkdir(parents=True,exist_ok=True)
    receipts=inbox.parent/"receipts"
    receipts.mkdir(parents=True,exist_ok=True)
    while True:
        for kind,directory in (("quote",quotes),("message",inbox)):
            for file in sorted(directory.glob("*.json")):
                receipt=receipts/(kind+"-"+file.name)
                if receipt.exists(): continue
                try:
                    obj=json.loads(file.read_text(encoding="utf-8"))
                    result=controller.market_snapshot(**obj) if kind=="quote" else controller.receive(Message(**obj),model)
                except Exception as exc:
                    result={"status":"FAILED","error":type(exc).__name__,"detail":str(exc)}
                temp=receipt.with_suffix(".tmp")
                with temp.open("w",encoding="utf-8") as f:
                    json.dump(result,f)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp,receipt)
                print(json.dumps({"input":str(file),"result":result}),flush=True)
        controller.tick()
        if once: return
        time.sleep(1)

if __name__=="__main__": main()
