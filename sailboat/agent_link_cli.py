"""Model-free local transport: JSON files or newline-delimited JSON over stdio."""
import argparse,json,sys
from pathlib import Path
from .agent_link import AgentLink
from .structured_schema import parse

def dispatch(link,message):
    if not isinstance(message,dict) or set(message)!={"method","params"} or not isinstance(message["params"],dict):
        raise ValueError("method and params object required")
    method,params=message["method"],message["params"]
    if method=="request" and set(params)<= {"operator_request","ttl","plan_ttl"} and "operator_request" in params:
        return link.request(**params)
    if method=="respond" and set(params)=={"response"}:return link.respond(json.dumps(params["response"],allow_nan=False))
    if method=="receipt" and set(params)=={"request_id"}:return link.receipt(params["request_id"])
    if method=="get_request" and set(params)=={"request_id"}:return link.get_request(params["request_id"])
    raise ValueError("unsupported method or parameters")

def main():
    p=argparse.ArgumentParser(description="SAILBOAT local agent connection; no model or network listener")
    p.add_argument("--db",default="state/structured.sqlite")
    p.add_argument("--source",default="external-agent");p.add_argument("--conversation",default="default")
    s=p.add_subparsers(dest="cmd",required=True)
    r=s.add_parser("request");r.add_argument("--brief",required=True);r.add_argument("--ttl",type=int,default=120);r.add_argument("--plan-ttl",type=int,default=900)
    for name in ("get-request","receipt"):
        q=s.add_parser(name);q.add_argument("request_id")
    r=s.add_parser("respond");r.add_argument("path")
    s.add_parser("stdio")
    a=p.parse_args();link=AgentLink(a.db,a.source,a.conversation)
    try:
        if a.cmd=="stdio":
            while True:
                line=sys.stdin.readline(20001)
                if not line:break
                if len(line)>20000:raise ValueError("oversized stdio frame")
                try:result=dispatch(link,parse(line))
                except (ValueError,TypeError) as exc:result={"status":"REJECTED","reason":str(exc)}
                print(json.dumps(result,allow_nan=False),flush=True)
            return
        if a.cmd=="request":result=link.request(a.brief,a.ttl,a.plan_ttl)
        elif a.cmd=="respond":result=link.respond(Path(a.path).read_text(encoding="utf-8"))
        elif a.cmd=="get-request":result=link.get_request(a.request_id)
        else:result=link.receipt(a.request_id)
        print(json.dumps(result,indent=2,allow_nan=False))
    finally:link.close()
if __name__=="__main__":main()
