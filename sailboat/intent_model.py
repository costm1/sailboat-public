"""Qwen intent recognizer. Grammar constraints restrict syntax, never determine meaning."""
import json
from .interpreters import TransformersInterpreter
from .intent_schema import SCHEMA,parse_intent
PROMPT2="""You recognize the meaning of another agent's latest message for a simulated controller.
You are NOT a market analyst. Do not assess whether a trade is wise. Return one JSON intent.
The CURRENT message updates prior context; history is not a list of commands to execute again.
Use the smallest object. Omit unknown fields. Never invent quantity, instrument, target or price.
Meanings:
observe = market commentary, including a change of direction with no change to a plan.
prefer = tentative entry wish, considering/would like calls or shares; NOT permission to buy.
propose = a suggested plan for review, explicitly not authorized for execution.
withdraw = no longer like/want an existing entry, suspend/stand down that plan. Never close a position.
resume = reconsider a suspended plan. Reopens discussion only; never reactivates execution.
enter = explicit buy instruction with an exact instrument and positive integer quantity.
exit = sell/reduce/close an EXISTING owned position. scope partial plus quantity for a partial sale;
       scope all for close/flatten/exit the entire position. Never omit a stated partial quantity.
hold = maintain an existing position; no new order.
cancel = cancel an existing instruction/order/plan. target is its exact ID if known.
revise = explicitly change an existing executable instruction; target and full updated entry required.
quote = read a named market snapshot. disarm and stop = those explicit safety requests.
clarify = missing critical details for an explicit order, ambiguous reference, contradiction, multiple
operations, unsupported short sale, unknown trigger, arming/LIVE/limit/credential changes.
Fields: kind, symbol, direction (bullish/bearish/neutral), instrument, quantity, scope,
target, limit_price, condition:{symbol,op,price}. condition.symbol is a market ticker, NEVER a price.
above/exceeds => gt; below/under => lt; at least/at or above => gte; at most/at or below => lte;
reclaim/cross above => cross_above; cross below => cross_below. Preserve boundary equality.
A condition on an enter or exit is NOT an immediate order. A limit price caps execution price.
Calls/puts without an exact contract ID are preferences or need clarification, never shares.
Only infer 'it' from one clear referent. Multiple positions and no explicit referent => clarify.
Select only the specified instrument; never select the first item in a catalog as a default.
'Buy QQQ now' lacks quantity: output {"kind":"clarify","symbol":"QQQ"}.
If two positions exist and the message says only 'Exit it', output {"kind":"clarify"}.
'Replace i1: buy 2 QQQ shares only above 115' means kind revise, target i1, price 115;
never copy the old condition's price. Only the latest message supplies revised values.
Always retain an explicitly stated bullish/bearish/neutral assessment as direction.
An explicit emergency stop always outputs {"kind":"stop"} with no other fields.
An explicit desire to exit the current position ('I'd prefer to exit') means exit, not an entry preference.
A bullish/bearish update alone does not cancel. 'I don't like the entry anymore' withdraws that plan.
Quotes or hypothetical instructions explicitly marked not to execute are observations.
Examples:
'Buy 2 XYZ shares if XYZ is at least 50' => {"kind":"enter","instrument":"XYZ","quantity":2,"condition":{"symbol":"XYZ","op":"gte","price":50}}
'Sell 1 XYZ share from my holding' => {"kind":"exit","symbol":"XYZ","scope":"partial","quantity":1}
'XYZ calls interest me after a reclaim of 50' => {"kind":"prefer","symbol":"XYZ","condition":{"symbol":"XYZ","op":"cross_above","price":50}}
'The earlier entry no longer appeals to me' => {"kind":"withdraw"}
'Reconsider the earlier entry' => {"kind":"resume"}
Do not emit explanations, alternative choices or extra fields."""
def compact_context(context):
    return {"mode":context.get("mode"),
        "catalog":[{"id":i["id"],"symbol":i["symbol"],"kind":i["kind"]} for i in context.get("instruments",[])],
        "positions":[{k:x[k] for k in ("id","instrument","quantity")} for x in context.get("positions",[])],
        "instructions":[{"id":x["id"],"plan":x["plan"]} for x in context.get("instructions",[])],
        "orders":[{"id":x["id"],"plan":x["plan"]} for x in context.get("orders",[])],
        "agent_intents":context.get("agent_intents",[]),
        "recent_messages":[{"text":x["text"]} for x in context.get("recent_messages",[])[-3:]]}
class IntentInterpreter(TransformersInterpreter):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        from .grammar_adapter import tokenizer_data
        self.enforcer_data=tokenizer_data(self.tokenizer)
    def interpret(self,message,context):
        from lmformatenforcer import JsonSchemaParser
        from .grammar_adapter import prefix_function
        self.last_raw=None
        text=self.tokenizer.apply_chat_template([
            {"role":"system","content":PROMPT2+"\nCURRENT CONTROLLER STATE:\n"+json.dumps(compact_context(context),separators=(",",":"))},
            {"role":"user","content":message.text}
        ],tokenize=False,add_generation_prompt=True,enable_thinking=False)
        encoded=self.tokenizer(text,return_tensors="pt",add_special_tokens=False)
        if encoded.input_ids.shape[1]>self.max_input_tokens: raise ValueError("context too large; no silent truncation")
        encoded=encoded.to(self.device)
        prefix=prefix_function(self.enforcer_data,JsonSchemaParser(SCHEMA))
        from transformers import StoppingCriteria,StoppingCriteriaList
        tokenizer=self.tokenizer
        offset=encoded.input_ids.shape[1]
        class CompleteJSON(StoppingCriteria):
            def __call__(self,input_ids,scores,**kwargs):
                raw=tokenizer.decode(input_ids[0][offset:],skip_special_tokens=True).strip()
                if not raw.endswith("}"):return False
                try: return isinstance(json.loads(raw),dict)
                except (ValueError,TypeError):return False
        with self.torch.inference_mode():
            output=self.model.generate(**encoded,do_sample=False,max_new_tokens=self.max_new_tokens,
                max_time=self.max_seconds,prefix_allowed_tokens_fn=prefix,stopping_criteria=StoppingCriteriaList([CompleteJSON()]),pad_token_id=self.tokenizer.eos_token_id)
        ids=output[0][encoded.input_ids.shape[1]:]
        self.last_tokens={"input":encoded.input_ids.shape[1],"output":len(ids)}
        self.last_raw=self.tokenizer.decode(ids,skip_special_tokens=True).strip()
        return parse_intent(self.last_raw)
