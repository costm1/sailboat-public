"""Separate OS process: model load/generation cannot block quote ingestion or tick."""
import json,os,time,multiprocessing
from pathlib import Path
from .controller import Controller

def save_receipt(path,value):
    tmp=path.with_suffix(".tmp")
    with tmp.open("w",encoding="utf-8") as f:
        json.dump(value,f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)

def monitor_loop(db_path,quotes_path,stop_event,ready_event=None,limits=None,interval=.1):
    c=Controller(db_path,limits=limits)
    quotes=Path(quotes_path); quotes.mkdir(parents=True,exist_ok=True)
    receipts=quotes.parent/"quote_receipts"; receipts.mkdir(exist_ok=True)
    try:
        if ready_event: ready_event.set()
        while not stop_event.is_set():
            for path in sorted(quotes.glob("*.json")):
                dest=receipts/path.name
                if dest.exists(): continue
                try: result=c.market_snapshot(**json.loads(path.read_text(encoding="utf-8")))
                except (ValueError,TypeError) as exc: result={"status":"REJECTED","reason":str(exc)}
                save_receipt(dest,result)
            c.tick()
            stop_event.wait(interval)
    except BaseException:
        c.emergency_stop()
        raise
    finally:
        c.close()

class MonitorProcess:
    def __init__(self,db_path,quotes_path,limits=None):
        ctx=multiprocessing.get_context("spawn")
        self.stop_event=ctx.Event(); self.ready=ctx.Event()
        self.process=ctx.Process(target=monitor_loop,args=(str(db_path),str(quotes_path),self.stop_event,self.ready,limits))
    def start(self):
        self.process.start()
        if not self.ready.wait(15): self.close(); raise RuntimeError("monitor startup failed")
        return self
    def close(self):
        self.stop_event.set()
        self.process.join(15)
        if self.process.is_alive(): raise RuntimeError("monitor did not stop; inspect process")
