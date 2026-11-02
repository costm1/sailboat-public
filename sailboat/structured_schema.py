"""Provider-neutral decision data. No text interpretation, model imports or API calls."""
import json,math
from jsonschema import Draft202012Validator
from .schema import IDENTIFIER,OPERATORS
ACTIONS=("observe","watch","propose_entry","propose_exit","hold","revise","cancel","request_clarification")
def obj(properties):
    return {"type":"object","additionalProperties":False,"required":list(properties),"properties":properties}
def nullable(schema): return {"anyOf":[schema,{"type":"null"}]}
ID={"type":"string","pattern":IDENTIFIER.pattern}
COND=obj({"symbol":ID,"op":{"type":"string","enum":sorted(OPERATORS)},"price":{"type":"number","exclusiveMinimum":0}})
BODY_SCHEMA=obj({
 "symbol":nullable(ID),"assessment":nullable({"type":"string","enum":["bullish","bearish","neutral"]}),
 "action":{"type":"string","enum":list(ACTIONS)},
 "plan_id":nullable(ID),"plan_version":nullable({"type":"integer","minimum":0}),
 "position_id":nullable(ID),"position_quantity":nullable({"type":"integer","minimum":1}),
 "execution":{"type":"string","enum":["none","paper"]},
 "instrument_id":nullable(ID),"quantity":nullable({"type":"integer","minimum":1}),
 "order_side":nullable({"type":"string","enum":["buy","sell"]}),
 "order_type":nullable({"type":"string","enum":["market","limit"]}),
 "limit_price":nullable({"type":"number","exclusiveMinimum":0}),
 "condition":nullable(COND),"invalidation":nullable(COND),
 "revision":nullable({"type":"string","enum":["replace","suspend","resume"]}),
 "reasoning":nullable({"type":"string","maxLength":1000}),
 "uncertainty":nullable({"type":"number","minimum":0,"maximum":1})
})
ENVELOPE_SCHEMA=obj({
 "schema_version":{"type":"string","enum":["sailboat.decision.v1"]},
 "decision_id":ID,"timestamp":{"type":"number","exclusiveMinimum":0},
 "expires_at":{"type":"number","exclusiveMinimum":0},"decision":BODY_SCHEMA})
def empty_body(**fields):
    b={k:None for k in BODY_SCHEMA["properties"]};b.update(action="observe",execution="none");b.update(fields);return b
def envelope(body,decision_id,now,expires_at):
    return {"schema_version":"sailboat.decision.v1","decision_id":decision_id,
            "timestamp":now,"expires_at":expires_at,"decision":body}
def parse(raw):
    if not isinstance(raw,str) or len(raw)>20000:raise ValueError("one JSON block of at most 20000 characters required")
    def unique(pairs):
        out={}
        for k,v in pairs:
            if k in out:raise ValueError("duplicate JSON key")
            out[k]=v
        return out
    return json.loads(raw,object_pairs_hook=unique,parse_constant=lambda x:(_ for _ in ()).throw(ValueError("nonfinite JSON")))
def finite(value):
    if isinstance(value,float) and not math.isfinite(value):raise ValueError("nonfinite number")
    if isinstance(value,dict):
        for v in value.values():finite(v)
    if isinstance(value,list):
        for v in value:finite(v)
def validate(value,now):
    finite(value)
    Draft202012Validator(ENVELOPE_SCHEMA).validate(value)
    if not value["timestamp"]<=now+5:raise ValueError("future timestamp")
    if now-value["timestamp"]>120:raise ValueError("stale submission")
    if not value["timestamp"]<value["expires_at"] or now>=value["expires_at"]:raise ValueError("expired decision")
    b=value["decision"];a=b["action"]
    for key in ("quantity","position_quantity","plan_version"):
        if b[key] is not None and type(b[key]) is not int:raise ValueError(key+" must be an integer")
    trading=a in ("propose_entry","propose_exit") or (a=="revise" and b["revision"]=="replace")
    if b["execution"]=="paper" and not trading:raise ValueError("this action cannot execute")
    if a=="revise" and b["revision"] is None:raise ValueError("revision operation required")
    if a!="revise" and b["revision"] is not None:raise ValueError("revision field does not belong to action")
    if a in ("watch","propose_entry","propose_exit","revise","cancel"):
        if b["plan_id"] is None or b["plan_version"] is None or b["symbol"] is None:raise ValueError("plan ID, version and symbol required")
    if a in ("observe","hold","cancel","request_clarification") or b["revision"] in ("suspend","resume"):
        if any(b[k] is not None for k in ("instrument_id","quantity","order_side","order_type","limit_price","condition","invalidation")):
            raise ValueError("irrelevant order fields; do not mix commentary or lifecycle changes with orders")
    if b["order_type"]=="limit" and b["limit_price"] is None:raise ValueError("limit price required")
    if b["order_type"]!="limit" and b["limit_price"] is not None:raise ValueError("limit price requires limit order")
    if b["execution"]=="paper":
        if any(b[k] is None for k in ("instrument_id","quantity","order_side","order_type")):raise ValueError("explicit instrument, quantity and order type required")
    if a=="propose_entry" and b["order_side"] not in (None,"buy"):raise ValueError("entry must buy")
    if a=="propose_exit" and b["order_side"] not in (None,"sell"):raise ValueError("exit must sell")
    if b["order_side"]=="sell" or a=="propose_exit" or a=="hold":
        if b["position_id"] is None or b["position_quantity"] is None:raise ValueError("explicit position reference and expected quantity required")
    return b
def provider_schema(value=None):
    """Portable type/enum/required subset; the controller retains all local constraints."""
    value=BODY_SCHEMA if value is None else value
    if isinstance(value,list):return [provider_schema(v) for v in value]
    if not isinstance(value,dict):return value
    unsupported={"minimum","maximum","exclusiveMinimum","minLength","maxLength","pattern"}
    return {k:provider_schema(v) for k,v in value.items() if k not in unsupported}
def openai_format():
    return {"type":"json_schema","name":"sailboat_decision","strict":True,"schema":provider_schema()}
def anthropic_format():
    return {"type":"json_schema","schema":provider_schema()}
