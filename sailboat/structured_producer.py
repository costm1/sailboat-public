"""Evaluation-only local external-agent producer; NOT imported by structured runtime."""
import json
from .intent_model import IntentInterpreter
from .structured_schema import BODY_SCHEMA,parse
PRODUCER_PROMPT="""You are a participating external agent submitting one SAILBOAT decision.
The user message is the intended simulated instruction/assessment. Encode it faithfully;
do not invent a market opinion or missing details. Return only the decision JSON schema.
All properties are required; use null for absent information. reasoning and uncertainty may be null.
observe means commentary only. watch means a tentative preference, not an order.
propose_entry/propose_exit are review-only unless the latest message explicitly authorizes execution:
then set execution=paper, explicit instrument_id, integer quantity, order_side and order_type.
No quantity, contract, price, or target may be guessed from the catalog. Missing details => request_clarification.
Calls without an exact contract are NOT stock orders.
hold requires position_id and current position_quantity; no order fields.
All exits require exact position_id, current position_quantity and explicit numeric quantity, even for a full exit.
A full exit uses the current explicitly identified position's quantity. Ambiguous references => request_clarification.
New watch/proposal uses new_plan_id and plan_version=0. Existing plans can change only through revise or cancel.
For every reference copy the current exact plan_id/version or position ID/quantity from state.
revise + revision=suspend withdraws the idea and cancels pending execution; no order fields.
revise + revision=resume reopens discussion only, execution=none and no order fields.
revise + revision=replace supplies the complete new order/preference. Do not copy stale thresholds.
cancel requires the existing plan ID/version and symbol, no order fields.
A changed bullish/bearish observation alone NEVER cancels or suspends a plan.
Do not repeat an earlier order. Reasoning text never controls execution.
above=>gt; at least=>gte; below=>lt; at most=>lte; reclaim=>cross_above.
A price condition is not a limit order. Set order_type=limit only for an explicit execution-price limit.
Use one price condition and optional price invalidation. Unsupported compound conditions => request_clarification.
Set assessment only if explicitly stated bullish/bearish/neutral.
Do not produce timestamps or decision IDs: the publisher supplies the transport envelope.
"""
class StructuredProducer(IntentInterpreter):
    def interpret(self,message,context):
        from lmformatenforcer import JsonSchemaParser
        from .grammar_adapter import prefix_function
        from transformers import StoppingCriteria,StoppingCriteriaList
        self.last_raw=None
        # Deliberately expose actual state, not answer labels or future turns.
        state={k:context.get(k) for k in ("mode","instruments","positions","plans","assessments","new_plan_id")}
        text=self.tokenizer.apply_chat_template([
          {"role":"system","content":PRODUCER_PROMPT+"\nSTATE:\n"+json.dumps(state,separators=(",",":"))},
          {"role":"user","content":message.text}],tokenize=False,add_generation_prompt=True,enable_thinking=False)
        encoded=self.tokenizer(text,return_tensors="pt",add_special_tokens=False).to(self.device)
        if encoded.input_ids.shape[1]>self.max_input_tokens:raise ValueError("context too large; no truncation")
        offset=encoded.input_ids.shape[1];tokenizer=self.tokenizer
        class Complete(StoppingCriteria):
            def __call__(self,ids,scores,**kwargs):
                raw=tokenizer.decode(ids[0][offset:],skip_special_tokens=True).strip()
                if not raw.endswith("}"):return False
                try:return isinstance(json.loads(raw),dict)
                except ValueError:return False
        with self.torch.inference_mode():
            output=self.model.generate(**encoded,do_sample=False,max_new_tokens=self.max_new_tokens,
              max_time=self.max_seconds,prefix_allowed_tokens_fn=prefix_function(self.enforcer_data,JsonSchemaParser(BODY_SCHEMA)),
              stopping_criteria=StoppingCriteriaList([Complete()]),pad_token_id=self.tokenizer.eos_token_id)
        self.last_raw=tokenizer.decode(output[0][offset:],skip_special_tokens=True).strip()
        self.last_tokens={"input":offset,"output":len(output[0])-offset}
        return parse(self.last_raw)
