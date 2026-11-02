"""Phase 2 meaning contract, separate from executable tool names."""
import json
from .schema import number,IDENTIFIER,OPERATORS
KINDS=("observe","prefer","propose","withdraw","resume","cancel","enter","exit","hold","quote","clarify","revise","disarm","stop")
FIELDS={"kind","symbol","direction","instrument","quantity","scope","condition","limit_price","target"}
SCHEMA={"type":"object","additionalProperties":False,"required":["kind"],"properties":{
    "kind":{"type":"string","enum":list(KINDS)},
    "symbol":{"type":"string","maxLength":100},
    "direction":{"type":"string","enum":["bullish","bearish","neutral"]},
    "instrument":{"type":"string","maxLength":100},
    "quantity":{"type":"integer","minimum":1,"maximum":100000},
    "scope":{"type":"string","enum":["all","partial"]},
    "target":{"type":"string","maxLength":100},
    "limit_price":{"type":"number","exclusiveMinimum":0},
    "condition":{"type":"object","additionalProperties":False,"required":["symbol","op","price"],"properties":{
        "symbol":{"type":"string","maxLength":100},"op":{"type":"string","enum":sorted(OPERATORS)},
        "price":{"type":"number","exclusiveMinimum":0}}}}}
def validate_intent(p):
    if not isinstance(p,dict) or set(p)-FIELDS or p.get("kind") not in KINDS: raise ValueError("invalid intent")
    for k in ("symbol","instrument","target"):
        if k in p and (not isinstance(p[k],str) or not IDENTIFIER.fullmatch(p[k])): raise ValueError("invalid "+k)
    if "quantity" in p and (type(p["quantity"]) is not int or p["quantity"]<=0): raise ValueError("invalid quantity")
    if "direction" in p and p["direction"] not in ("bullish","bearish","neutral"): raise ValueError("invalid direction")
    if "scope" in p and p["scope"] not in ("all","partial"): raise ValueError("invalid scope")
    if "limit_price" in p: number(p["limit_price"],"limit_price")
    if "condition" in p:
        c=p["condition"]
        if not isinstance(c,dict) or set(c)!={"symbol","op","price"} or c["op"] not in OPERATORS: raise ValueError("invalid condition")
        if not isinstance(c["symbol"],str) or not IDENTIFIER.fullmatch(c["symbol"]): raise ValueError("invalid condition symbol")
        number(c["price"],"condition price")
    if p["kind"]=="exit" and p.get("scope") not in ("all","partial"): raise ValueError("exit scope required")
    if p.get("scope")=="partial" and "quantity" not in p: raise ValueError("partial exit needs quantity")
    return p
def parse_intent(raw):
    def unique(pairs):
        result={}
        for k,v in pairs:
            if k in result: raise ValueError("duplicate key")
            result[k]=v
        return result
    return validate_intent(json.loads(raw,object_pairs_hook=unique,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError("nonfinite"))))
