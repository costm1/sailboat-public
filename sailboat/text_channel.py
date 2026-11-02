"""Natural text plus sending-agent intent; deterministic simulated plan handling."""
import hashlib,json,time,uuid
from jsonschema import Draft202012Validator
from .controller import canonical
from .text_intent import INTENT_SCHEMA,validate_intent,public_context,apply

SEND_SCHEMA={'type':'object','additionalProperties':False,'required':['text'],'properties':{
 'text':{'type':'string','minLength':1,'maxLength':16000},
 'message_id':{'type':'string','minLength':1,'maxLength':100},
 'context_receipt':{'type':'string','minLength':1,'maxLength':100},
 'intent':INTENT_SCHEMA}}
RECEIPT_SCHEMA={'type':'object','additionalProperties':False,'required':['receipt_id'],'properties':{
 'receipt_id':{'type':'string','minLength':1,'maxLength':100}}}
TOOLS=(('sailboat_send_message','Send natural-language text and a small meaning block. First call returns current simulated context and a receipt. Then send the unchanged text/message_id with intent and context_receipt set to that receipt_id. Sailboat resolves contracts and plan versions and validates through the simulated controller. No second model or live brokerage execution. Recommendations cannot execute; owner permissions remain separate. Reuse identical arguments for uncertain retries.',SEND_SCHEMA),
 ('sailboat_message_receipt','Read this client message context, interpretation or simulated controller result. Acknowledgment is not a fill or live authorization.',RECEIPT_SCHEMA))
SKILL="""Sailboat uses the sending agent's interpretation, with no second model.
Send text first to get context. Then, without asking the user to construct a schema,
send unchanged text/message_id plus context_receipt=receipt_id and intent. Include meaning
(observation/recommendation/instruction/clarification), action, exact supporting excerpts
in evidence, and the applicable natural terms: symbol, option expiry/strike/call-or-put,
quantity, limit_price, explicit condition/invalidation and a human plan_name. No broker
UUID, account number, internal plan ID, plan version or controller order schema is required.
Actions are observe/watch/enter/exit/hold/revise/cancel/clarify. Revisions use
replace/suspend/resume. Resolve 0DTE to context.market_date; don't guess missing terms.
Do not upgrade mentions, hypotheticals, refusals or recommendations into instructions.
If text contains multiple independent actions, ask to split it; don't drop one.
Preserve research-only/do-not-submit/no-orders restrictions. No text can change owner
controls. This route manages simulated plans only; it cannot submit live brokerage orders.
Read actual controller_receipt/result. NEEDS_INTENT is incomplete, NEEDS_INFORMATION
requires clarification, OBSERVED_DISARMED is recorded but not executed, PENDING is not
FILLED. If REJECTED/stale, report it and obtain current user intent before resubmission;
do not use a fresh message ID to bypass a rejection. Identical retries recover receipts.
"""


def validate(operation,args):
 schema=SEND_SCHEMA if operation=='sailboat_send_message' else RECEIPT_SCHEMA
 errors=list(Draft202012Validator(schema).iter_errors(args))
 if errors:raise ValueError('Invalid text/intent arguments: '+errors[0].message[:300])
 if operation=='sailboat_send_message':
  if not args['text'].strip() or len(args['text'].encode('utf-8'))>32000:raise ValueError('Text must be nonblank and at most 32000 UTF-8 bytes')
  if 'intent' in args:validate_intent(args['intent'],args['text'])


def exchange(c,principal,operation,args):
 validate(operation,args)
 owner=hashlib.sha256(canonical({k:principal[k] for k in ('source','conversation','label')}).encode()).hexdigest()
 with c.transaction():
  c.db.execute('CREATE TABLE IF NOT EXISTS text_inbox(id TEXT PRIMARY KEY,owner TEXT NOT NULL,message_key TEXT NOT NULL,text TEXT NOT NULL,receipt TEXT NOT NULL,UNIQUE(owner,message_key))')
  c.db.execute('CREATE TABLE IF NOT EXISTS text_contexts(id TEXT PRIMARY KEY,request TEXT NOT NULL,intent_digest TEXT)')
  if operation=='sailboat_message_receipt':
   row=c.db.execute('SELECT receipt FROM text_inbox WHERE id=? AND owner=?',(args['receipt_id'],owner)).fetchone()
   if not row:raise ValueError('Message receipt not found for this client')
   return json.loads(row[0])
  content=args['text'];key=args.get('message_id') or 'sha256:'+hashlib.sha256(content.encode()).hexdigest()
  row=c.db.execute('SELECT id,text,receipt FROM text_inbox WHERE owner=? AND message_key=?',(owner,key)).fetchone()
  if row:
   if row['text']!=content:raise ValueError('message_id already belongs to different text')
   result=json.loads(row['receipt']);ident=row['id']
   if 'intent' not in args:return result
   ctx=c.db.execute('SELECT * FROM text_contexts WHERE id=?',(ident,)).fetchone()
   if not ctx:raise ValueError('Legacy transport receipt is preserved without replay. Send a fresh instruction with a new message_id.')
   digest=hashlib.sha256(canonical(args['intent']).encode()).hexdigest()
   if ctx['intent_digest']:
    if ctx['intent_digest']!=digest:raise ValueError('Message already interpreted with different intent; cannot overwrite its receipt')
    return result
   if args.get('context_receipt')!=ident:raise ValueError('Return this message receipt_id as context_receipt before applying its intent')
   request=json.loads(ctx['request'])
   body,receipt=apply(c,args['intent'],request)
   outcome=receipt.get('result',receipt)
   result.update(interpretation_status='REJECTED' if outcome.get('status')=='REJECTED' else 'COMPLETE',
    response=body.get('reasoning') or 'Intent resolved and checked by the simulated controller. Read controller_receipt for the actual outcome.',
    meaning=args['intent']['meaning'],controller_receipt=receipt,order_submitted=bool(outcome.get('order_id') or outcome.get('instruction_id')),
    simulated_order_submitted=bool(outcome.get('order_id')),simulated_condition_created=bool(outcome.get('instruction_id')))
   c.db.execute('UPDATE text_contexts SET intent_digest=? WHERE id=?',(digest,ident))
   c.db.execute('UPDATE text_inbox SET receipt=? WHERE id=?',(canonical(result),ident))
   c._event('TEXT_INTENT_APPLIED',{'receipt_id':ident,'intent':args['intent'],'controller_receipt':receipt})
   return result
  if args.get('context_receipt'):raise ValueError('Unknown message context; send text first')
  count,size=c.db.execute('SELECT COUNT(*),COALESCE(SUM(length(CAST(text AS BLOB))),0) FROM text_inbox').fetchone()
  if count>=1024 or size+len(content.encode())>4*1024*1024:raise ValueError('Text inbox capacity reached; no message was accepted')
  request=c.request('Resolve the incoming text intent using the current simulated controller state.',ttl=120,plan_ttl=900)
  result={'receipt_id':'message-'+uuid.uuid4().hex,'status':'RECEIVED','received_at':c.clock(),
   'interpretation_status':'NEEDS_INTENT','execution_authorized':False,'order_submitted':False,
   'broker_order_submitted':False,'execution_scope':'simulated',
   'context':public_context(c,request),
   'response':'Text received. Use this context to supply a faithful intent block with unchanged text/message_id and context_receipt=receipt_id. No second model is called; no plan changed yet.'}
  c.db.execute('INSERT INTO text_inbox VALUES(?,?,?,?,?)',(result['receipt_id'],owner,key,content,canonical(result)))
  c.db.execute('INSERT INTO text_contexts VALUES(?,?,NULL)',(result['receipt_id'],canonical(request)))
  c._event('TEXT_MESSAGE_RECEIVED',{'receipt_id':result['receipt_id'],'client':principal['label'],'text_sha256':hashlib.sha256(content.encode()).hexdigest()})
  return result
