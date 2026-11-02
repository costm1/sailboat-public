"""Owner-controlled JSON mailbox. Simulation only; no model dependency."""
import argparse,json,time
from pathlib import Path
from .structured_controller import StructuredController
from .structured_schema import BODY_SCHEMA,ENVELOPE_SCHEMA
from .monitoring import save_receipt
def main():
    p=argparse.ArgumentParser(description="SAILBOAT structured simulation")
    p.add_argument("--db",default="state/structured.sqlite")
    s=p.add_subparsers(dest="cmd",required=True)
    for name in ("status","stop","verify","schema"):s.add_parser(name)
    r=s.add_parser("register");r.add_argument("symbol")
    a=s.add_parser("mode");a.add_argument("mode",choices=["DISARMED","PAPER_ARMED"])
    q=s.add_parser("quote");q.add_argument("symbol");q.add_argument("bid",type=float);q.add_argument("ask",type=float);q.add_argument("last",type=float)
    sub=s.add_parser("submit");sub.add_argument("path");sub.add_argument("--source",default="manual");sub.add_argument("--conversation",default="default")
    w=s.add_parser("worker");w.add_argument("--inbox",default="state/structured-inbox");w.add_argument("--quotes",default="state/structured-quotes");w.add_argument("--source",default="manual");w.add_argument("--conversation",default="default");w.add_argument("--once",action="store_true")
    a=p.parse_args()
    if a.cmd=="schema":print(json.dumps(ENVELOPE_SCHEMA,indent=2));return
    c=StructuredController(a.db)
    try:
        if a.cmd=="status":result=c.status()
        elif a.cmd=="verify":result={"journal_valid":c.verify_journal()}
        elif a.cmd=="stop":c.emergency_stop();result=c.status()
        elif a.cmd=="mode":c.set_mode(a.mode);result=c.status()
        elif a.cmd=="register":c.register_instrument(a.symbol);result={"registered":a.symbol}
        elif a.cmd=="quote":result=c.market_snapshot(a.symbol,a.bid,a.ask,a.last)
        elif a.cmd=="submit":result=c.submit(a.source,a.conversation,Path(a.path).read_text(encoding="utf-8"))
        else:
            inbox,quotes=Path(a.inbox),Path(a.quotes)
            for d in (inbox,quotes):d.mkdir(parents=True,exist_ok=True)
            receipts=inbox/"receipts";receipts.mkdir(exist_ok=True)
            qr=quotes/"receipts";qr.mkdir(exist_ok=True)
            while True:
                for f in [x for x in sorted(quotes.glob("*.json")) if not (qr/x.name).exists()][:100]:
                    dest=qr/f.name
                    if dest.exists():continue
                    try:r=c.market_snapshot(**json.loads(f.read_text()))
                    except (ValueError,TypeError) as exc:r={"status":"REJECTED","reason":str(exc)}
                    save_receipt(dest,r)
                c.tick()
                for f in [x for x in sorted(inbox.glob("*.json")) if not (receipts/x.name).exists()][:10]:
                    dest=receipts/f.name
                    if dest.exists():continue
                    save_receipt(dest,c.submit(a.source,a.conversation,f.read_text(encoding="utf-8")))
                    c.tick()
                if a.once:break
                time.sleep(.1)
            result={"status":"WORKER_EXIT"}
        print(json.dumps(result,indent=2))
    finally:c.close()
if __name__=="__main__":main()
