"""Strict wire contract. Model output is data, never executable code."""
from dataclasses import dataclass, asdict
import json
import math
import re
TOOLS = (
    "record_commentary", "request_clarification", "read_market_snapshot",
    "create_simulated_order", "monitor_price", "close_simulated_position",
    "maintain_position", "cancel_instruction", "cancel_simulated_order",
    "replace_instruction", "disarm", "emergency_stop",
)
MUTATING = {"create_simulated_order", "monitor_price", "close_simulated_position",
            "cancel_instruction", "cancel_simulated_order", "replace_instruction",
            "disarm", "emergency_stop"}
FIELDS = {"tool", "symbol", "instrument", "quantity", "action", "target",
          "condition_symbol", "operator", "price", "limit_price", "direction"}
OPERATORS = {"gt", "gte", "lt", "lte", "cross_above", "cross_below"}
IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
def number(value, name, positive=True):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return value
@dataclass(frozen=True)
class Message:
    source: str
    conversation_id: str
    message_id: str
    text: str
    def __post_init__(self):
        for name in ("source", "conversation_id", "message_id"):
            if not isinstance(getattr(self, name), str) or not IDENTIFIER.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 12000:
            raise ValueError("text must contain 1-12000 characters")
    @property
    def owner(self):
        return f"{self.source}/{self.conversation_id}"
    def as_dict(self):
        return asdict(self)
def validate_plan(plan):
    if not isinstance(plan, dict) or not isinstance(plan.get("tool"), str):
        raise ValueError("one action object with tool required")
    if set(plan) - FIELDS:
        raise ValueError("unknown fields")
    if plan["tool"] not in TOOLS:
        raise ValueError("unknown tool")
    p = {k: v for k, v in plan.items() if v is not None}
    for name in ("symbol", "instrument", "target", "condition_symbol"):
        if name in p and (not isinstance(p[name], str) or not IDENTIFIER.fullmatch(p[name])):
            raise ValueError(f"invalid {name}")
    if "quantity" in p and (type(p["quantity"]) is not int or p["quantity"] <= 0):
        raise ValueError("quantity must be a positive integer")
    for name in ("price", "limit_price"):
        if name in p:
            number(p[name], name)
    if "action" in p and p["action"] not in ("buy", "sell"):
        raise ValueError("invalid action")
    if "direction" in p and p["direction"] not in ("bullish", "bearish", "neutral"):
        raise ValueError("invalid direction")
    if "operator" in p and p["operator"] not in OPERATORS:
        raise ValueError("invalid condition operator")
    conditional = {"condition_symbol", "operator", "price"}
    if conditional & p.keys() and not conditional <= p.keys():
        raise ValueError("incomplete price condition")
    if p["tool"] == "monitor_price" and not conditional <= p.keys():
        raise ValueError("monitor_price requires a complete price condition")
    if p["tool"] == "create_simulated_order" and conditional & p.keys():
        raise ValueError("conditional entries must use monitor_price")
    if p["tool"] in {"create_simulated_order", "monitor_price", "replace_instruction"}:
        if not {"instrument", "quantity", "action"} <= p.keys():
            raise ValueError("instrument, quantity and action required")
    if p["tool"] in {"cancel_instruction", "cancel_simulated_order", "replace_instruction"} and "target" not in p:
        raise ValueError("explicit target required")
    allowed = {
        "record_commentary": {"tool","symbol","direction"},
        "read_market_snapshot": {"tool","symbol"},
        "maintain_position": {"tool","symbol","instrument","target"},
        "disarm": {"tool"}, "emergency_stop": {"tool"},
        "cancel_instruction": {"tool","target","symbol"},
        "cancel_simulated_order": {"tool","target","symbol"},
        "close_simulated_position": {"tool","instrument","quantity","target","symbol","direction","action","limit_price"},
        "create_simulated_order": {"tool","instrument","quantity","action","symbol","direction","limit_price"},
    }
    if p["tool"] in allowed and set(p)-allowed[p["tool"]]:
        raise ValueError("fields do not belong to this tool")
    if p["tool"]=="close_simulated_position" and p.get("action","sell")!="sell":
        raise ValueError("close cannot buy")
    return p
def parse_plan(raw):
    if not isinstance(raw, str) or len(raw) > 30000:
        raise ValueError("invalid model response")
    s = raw.strip()
    if s.startswith("<tool_call>") and s.endswith("</tool_call>"):
        s = s[len("<tool_call>"):-len("</tool_call>")].strip()
    fence=chr(96)*3
    if s.startswith(fence+"json") and s.endswith(fence):
        s=s[7:-3].strip()
    def no_duplicates(pairs):
        result={}
        for k,v in pairs:
            if k in result: raise ValueError("duplicate JSON key")
            result[k]=v
        return result
    obj=json.loads(s,object_pairs_hook=no_duplicates,
                   parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if isinstance(obj,dict) and obj.get("name")=="propose_action" and set(obj)=={"name","arguments"}:
        obj=obj["arguments"]
    return validate_plan(obj)
PROPOSAL_SCHEMA = {
    "type":"object","additionalProperties":False,
    "properties":{
        "tool":{"type":"string","enum":list(TOOLS)},
        "symbol":{"type":"string"},"instrument":{"type":"string"},
        "quantity":{"type":"integer","minimum":1},
        "action":{"type":"string","enum":["buy","sell"]},"target":{"type":"string"},
        "condition_symbol":{"type":"string"},
        "operator":{"type":"string","enum":sorted(OPERATORS)},
        "price":{"type":"number"},"limit_price":{"type":"number"},
        "direction":{"type":"string","enum":["bullish","bearish","neutral"]},
    },"required":["tool"],
}
