"""Map sending-agent meaning blocks to the existing simulated controller.
No inference runtime, provider request, broker execution or owner control here.
"""
import copy,json
from datetime import datetime
from zoneinfo import ZoneInfo
from jsonschema import Draft202012Validator
from .controller import canonical, money
from .structured_schema import empty_body, finite, COND

INSTRUMENT={'type':'object','additionalProperties':False,'required':['kind'], 'properties':{
 'kind':{'enum':['stock','option']},'expiry':{'type':'string','pattern':r'^\d{4}-\d{2}-\d{2}$'},
 'strike':{'type':'number','exclusiveMinimum':0},'right':{'enum':['call','put']}}}
INTENT_SCHEMA={'type':'object','additionalProperties':False,'required':['meaning','action','evidence'], 'properties':{
 'meaning':{'enum':['observation','recommendation','instruction','clarification']},
 'action':{'enum':['observe','watch','enter','exit','hold','revise','cancel','clarify']},
 'symbol':{'type':'string','pattern':r'^[A-Z][A-Z0-9.]{0,14}$'},
 'plan_name':{'type':'string','minLength':1,'maxLength':100},
 'instrument':INSTRUMENT,'quantity':{'type':'integer','minimum':1},
 'limit_price':{'type':'number','exclusiveMinimum':0},
 'condition':COND,'invalidation':COND,'revision':{'enum':['replace','suspend','resume']},
 'direction':{'enum':['bullish','bearish','neutral']},
 'evidence':{'type':'array','minItems':1,'maxItems':12,'items':{'type':'string','minLength':1,'maxLength':1000}},
 'reasoning':{'type':'string','maxLength':1000}}}

class Clarification(ValueError):pass


def validate_intent(intent,text):
 finite(intent);Draft202012Validator(INTENT_SCHEMA).validate(intent)
 if any(piece not in text for piece in intent['evidence']):raise ValueError('evidence must quote the submitted text exactly')
 meaning,action=intent['meaning'],intent['action']
 if meaning=='observation' and action!='observe':raise ValueError('observations cannot change plans')
 if meaning=='clarification' and action!='clarify':raise ValueError('clarification cannot change plans')
 if meaning=='recommendation' and action not in ('watch','enter','exit','clarify'):raise ValueError('recommendation cannot revise or execute instructions')
 if action=='revise' and 'revision' not in intent:raise ValueError('revision operation required')
 if action!='revise' and 'revision' in intent:raise ValueError('revision belongs only to revise')
 if action in ('observe','hold','cancel','clarify') or intent.get('revision') in ('suspend','resume'):
  if set(intent)&{'quantity','limit_price','condition','invalidation'}:raise ValueError('order terms do not belong to this lifecycle action')
 if 'quantity' in intent and type(intent['quantity']) is not int:raise ValueError('quantity must be an integer')
 return intent


def names(c):
 c.db.execute('CREATE TABLE IF NOT EXISTS text_plan_names(owner TEXT,name TEXT,plan_id TEXT,PRIMARY KEY(owner,name),UNIQUE(owner,plan_id))')
 for row in c.db.execute('SELECT id,symbol FROM structured_plans WHERE owner=? ORDER BY rowid',(c.owner,)).fetchall():
  if c.db.execute('SELECT 1 FROM text_plan_names WHERE owner=? AND plan_id=?',(c.owner,row['id'])).fetchone():continue
  n=1
  while c.db.execute('SELECT 1 FROM text_plan_names WHERE owner=? AND name=?',(c.owner,f"{row['symbol']} plan {n}")).fetchone():n+=1
  c.db.execute('INSERT INTO text_plan_names VALUES(?,?,?)',(c.owner,f"{row['symbol']} plan {n}",row['id']))
 return {r['plan_id']:r['name'] for r in c.db.execute('SELECT name,plan_id FROM text_plan_names WHERE owner=?',(c.owner,))}


def instrument_description(i):
 d=json.loads(i['details']) if isinstance(i['details'],str) else i['details']
 return {'kind':i['kind'],**({k:d[k] for k in ('expiry','strike','right') if k in d} if i['kind']=='option' else {})}


def public_context(c,request):
 ctx=request['context'];labels=names(c);catalog={i['id']:i for i in ctx['instruments']}
 return {'scope':'simulated controller plans and positions; separate from brokerage holdings',
  'market_date':datetime.fromtimestamp(c.clock(),ZoneInfo('America/New_York')).date().isoformat(),
  'instruments':[{'symbol':i['symbol'],**instrument_description(i)} for i in catalog.values()],
  'plans':[{'plan_name':labels[p['id']],'symbol':p['symbol'],'status':p['status'],
    'instrument':instrument_description(catalog[p['content']['instrument_id']]) if p['content']['instrument_id'] in catalog else None,
    'quantity':p['content']['quantity'],'limit_price':p['content']['limit_price'],
    'condition':p['content']['condition'],'invalidation':p['content']['invalidation']} for p in ctx['plans']],
  'positions':[{'symbol':catalog[p['instrument']]['symbol'],'instrument':instrument_description(catalog[p['instrument']]),
    'quantity':p['quantity'],'entry_premium':p['cost']} for p in ctx['positions']],
  'reply_before':request['reply_before']}


def resolve_instrument(c,intent,required=False):
 spec=intent.get('instrument')
 if not spec:
  if required:raise Clarification('Specify stock or option; an option needs expiry, strike and call/put.')
  return None
 if spec['kind']=='option' and not {'expiry','strike','right'}<=spec.keys():raise Clarification('Exact option expiry, strike and call/put are required.')
 if spec['kind']=='stock' and set(spec)!={'kind'}:raise ValueError('stock cannot have option terms')
 matches=[]
 for i in c.db.execute('SELECT * FROM instruments WHERE symbol=?',(intent.get('symbol'),)):
  d=instrument_description(i)
  if d.get('kind')!=spec['kind']:continue
  if spec['kind']=='option':
   if d.get('expiry')!=spec['expiry'] or d.get('right')!=spec['right'] or money(d.get('strike',0))!=money(spec['strike']):continue
   if spec['expiry']<datetime.fromtimestamp(c.clock(),ZoneInfo('America/New_York')).date().isoformat():raise Clarification('Option is expired; specify a current contract.')
  matches.append(dict(i))
 if len(matches)!=1:raise Clarification('Contract is absent or ambiguous in the owner-approved simulated catalog. No contract was invented or registered.')
 return matches[0]


def translate(c,intent,request):
 a=intent['action'];mapped={'enter':'propose_entry','exit':'propose_exit','clarify':'request_clarification'}.get(a,a)
 b=empty_body(action=mapped,symbol=intent.get('symbol'),assessment=intent.get('direction'),reasoning=intent.get('reasoning'))
 if a in ('observe','clarify'):return b
 if not b['symbol']:raise Clarification('Market symbol is required.')
 labels=names(c);plans=request['context']['plans'];name=intent.get('plan_name')
 old=next((p for p in plans if labels[p['id']]==name),None) if name else None
 if a in ('revise','cancel'):
  candidates=[p for p in plans if p['symbol']==b['symbol'] and p['status'] not in ('FILLED','EXPIRED','INVALIDATED','CANCELED')]
  if not name:
   if len(candidates)!=1:raise Clarification('Specify the plan_name from the returned context; reference is ambiguous.')
   old=candidates[0]
  if not old or old['symbol']!=b['symbol']:raise Clarification('Referenced plan is not present for this symbol.')
  b.update(plan_id=old['id'],plan_version=old['version'],revision=intent.get('revision'))
 elif a in ('watch','enter','exit'):
  if old:raise Clarification('That plan already exists. Use revise or cancel instead of duplicating it.')
  b.update(plan_id=request['new_plan_id'],plan_version=0)
 if a=='cancel' or intent.get('revision') in ('suspend','resume'):return b
 inst=resolve_instrument(c,intent,required=a=='enter' or (a=='revise' and intent.get('revision')=='replace'))
 if a in ('hold','exit'):
  positions=[p for p in request['context']['positions'] if c.db.execute('SELECT symbol FROM instruments WHERE id=?',(p['instrument'],)).fetchone()[0]==b['symbol'] and (not inst or p['instrument']==inst['id'])]
  if len(positions)!=1:raise Clarification('Specify the held contract; position reference is absent or ambiguous.')
  pos=positions[0];inst=dict(c.db.execute('SELECT * FROM instruments WHERE id=?',(pos['instrument'],)).fetchone())
  b.update(position_id=pos['id'],position_quantity=pos['quantity'])
  if a=='hold':return b
 if a=='revise' and old['content'].get('order_side')=='sell':raise Clarification('Exit replacement requires a fresh position-specific exit instruction; do not reuse an entry revision.')
 b.update(instrument_id=inst['id'] if inst else None,quantity=intent.get('quantity'),
  limit_price=intent.get('limit_price'),order_type='limit' if 'limit_price' in intent else None,
  condition=intent.get('condition'),invalidation=intent.get('invalidation'))
 if a!='watch':b['order_side']='sell' if a=='exit' else 'buy'
 if a in ('enter','exit','revise') and intent['meaning']=='instruction':
  if not inst or b['quantity'] is None or b['limit_price'] is None:raise Clarification('Explicit quantity and maximum entry/minimum exit limit premium are required; no sizing or price was inferred.')
  if a=='exit' and money(b['limit_price'])<money(pos['cost']):raise Clarification('The requested exit is below recorded entry cost; no loss sale was queued.')
  b['execution']='paper'
 return b


def apply(c,intent,request):
 try:body=translate(c,intent,request)
 except Clarification as exc:
  body=empty_body(action='request_clarification',symbol=intent.get('symbol'),reasoning=str(exc))
 receipt=c.respond(canonical({'request_id':request['request_id'],'context_hash':request['context_hash'],'decision':body}))
 outcome=receipt.get('result',receipt)
 if body.get('plan_id') and outcome.get('plan_id') and intent.get('plan_name'):
  existing=c.db.execute('SELECT plan_id FROM text_plan_names WHERE owner=? AND name=?',(c.owner,intent['plan_name'])).fetchone()
  if not existing:c.db.execute('INSERT INTO text_plan_names VALUES(?,?,?)',(c.owner,intent['plan_name'],body['plan_id']))
 return body,receipt
