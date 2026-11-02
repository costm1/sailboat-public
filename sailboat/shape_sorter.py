"""Evidence-preserving text -> capability blocks. No strategy or order authority."""
import copy,hashlib,re
from datetime import datetime
from .market_hours import EASTERN
from .shadow_clues import extract
from .text_bindings import focus,contracts,current_pass,local_symbol
from .controller import canonical
VERSION='shape-sorter-v3-preferred-candidate'
CAPABILITIES={
 'observation':'record_agent_observation','prediction':'record_agent_prediction',
 'price_condition':'watch_price','volume_condition':'request_clarification',
 'contract_reference':'research_option_contract','instruction':'controller_preview',
 'restriction':'preserve_source_restriction','clarification':'request_clarification'}

def sort_text(text,at,intent=None):
 clues=extract(text);blocks=[];missing=[]
 def evidence(start,end):return {'start':start,'end':end,'text':text[start:end]}
 def add(kind,terms,e,needed=None):
  blocks.append({'kind':kind,'capability':CAPABILITIES[kind],'terms':terms,'evidence':[e],
    'missing':needed or [],'status':'NEEDS_CLARIFICATION' if needed else 'RECOGNIZED','authority':'SOURCE_MEANING_ONLY'})
 # Retain every sentence; unmatched language is never silently converted to a command.
 for m in re.finditer(r'.+?(?:[.!?](?=\s|$)|\n|$)',text,re.S):
  if m[0].strip():add('observation',{'text':m[0]},evidence(m.start(),m.end()))
 tickers={f['value'].upper() for f in clues['fields'] if f['field']=='ticker'}
 symbol,subject_evidence=focus(text)
 conditions=[f for f in clues['fields'] if f['field']=='threshold']
 for f in conditions:
  # Keep strict above/below semantics, rather than silently relaxing them to >=/<=.
  v=copy.deepcopy(f['value']);v['op']='gt' if v['op']=='gte' else 'lt';v['symbol']=local_symbol(text,f['evidence']['start'],symbol)
  bound=[x for x in clues['fields'] if x['field']=='entry_condition' and x['evidence']['start']<=f['evidence']['start'] and x['evidence']['end']>=f['evidence']['end']]
  if bound:v['option_side']=bound[0]['value']['right']
  needed=[] if v['symbol'] else ['Identify the underlying for this threshold.']
  add('price_condition',v,f['evidence'],needed)
 for m in re.finditer(r'\b(?:strong volume|rising volume|volume confirmation|volume[- ]backed|high volume|heavy volume)\b',text,re.I):
  add('volume_condition',{'description':m[0]},evidence(m.start(),m.end()),['Define the volume metric, comparison window and threshold. No multiplier was invented.'])
 for m in re.finditer(r'\b(?:could|might|may|expect(?:ed)?|predict(?:ed)?)\b[^.!?\n]*',text,re.I):
  add('prediction',{'text':m[0]},evidence(m.start(),m.end()))
 for f in clues['fields']:
  if f['field']=='restriction':add('restriction',{'text':f['value']},f['evidence'])
 rights=sorted({f['value'] for f in clues['fields'] if f['field']=='option_side'})
 strikes=sorted({f['value'] for f in clues['fields'] if f['field']=='strike'})
 # Expiration must be labeled, or explicitly 0DTE/today. Do not treat quote dates as expiries.
 expiries=list(re.finditer(r'\b(?:expir(?:y|ation|ing|es)|expiry date)\s*[:=]?\s*(?:today,?\s*)?(20\d\d-\d\d-\d\d)',text,re.I))
 zero=re.search(r'\b0DTE\b|\btoday[’\x27]s\s+(?:calls?|puts?|options?)\b|\bexpir(?:ing|es|ation)\s+(?:is\s+)?today\b',text,re.I)
 expiry=expiries[0][1] if len({m[1] for m in expiries})==1 else datetime.fromtimestamp(at,EASTERN).date().isoformat() if zero and not expiries else None
 # Natural month-name expirations are facts, not trading preferences.
 named=list(re.finditer(r'\bexpir(?:y|ation|ing|es)\s*[:=]?\s*([A-Za-z]+ \d{1,2}(?:(?:,\s*|\s+)20\d\d)?)',text,re.I))
 named_values=[]
 for m in named:
  raw=' '.join(m[1].replace(',',' ').split());raw=raw if re.search(r'20\d\d$',raw) else raw+' '+str(datetime.fromtimestamp(at,EASTERN).year)
  parsed=None
  for fmt in ('%B %d %Y','%b %d %Y'):
   try:parsed=datetime.strptime(raw,fmt).date().isoformat();break
   except ValueError:pass
  named_values.append(parsed)
 if named or expiries or zero:
  all_dates=set(named_values+[m[1] for m in expiries]+([datetime.fromtimestamp(at,EASTERN).date().isoformat()] if zero else []))
  expiry=next(iter(all_dates)) if len(all_dates)==1 and None not in all_dates else None
 refs=contracts(text)
 seen=set()
 for ref in refs:
  side=ref['right'];strike=float(ref['strike']) if ref['strike'] is not None else None
  terms={'symbol':ref['symbol'],'right':side,'strike':strike,'expiry':expiry}
  identity=tuple(terms.values())
  if identity in seen:continue
  seen.add(identity)
  needed=[k+' is unspecified or ambiguous' for k,v in terms.items() if v is None]
  add('contract_reference',terms,ref['evidence'][0],needed)
  b=blocks[-1];b['evidence']=ref['evidence']+ref['identity_evidence']
  if ref['strike_alternatives']:b['strike_alternatives']=ref['strike_alternatives']
  if expiry:
   match=(expiries or named or [zero])[0];b['evidence'].append(evidence(match.start(),match.end()))
  b['normalizations']=[{'rule':'0DTE means the message date in America/New_York','value':expiry}] if zero and not expiries and not named else []
  if named and any(not re.search(r'20\d\d$',m[1]) for m in named):b['normalizations'].append({'rule':'Omitted expiration year uses the message year in America/New_York; no rollover to a later year','value':expiry})
 # Identify stated action words without assuming the whole sentence authorizes that action.
 for m in re.finditer(r'\b(buy|purchase|sell|exit|hold|cancel|suspend|resume|revise|withdraw)\b',text,re.I):
  add('instruction',{'mentioned_action':m[1].lower(),'classification':'ACTION_MENTION_NOT_AUTHORIZATION'},evidence(m.start(),m.end()),['Confirm action scope, conditions and referenced plan/position; an action word alone is not an instruction.'])
 warnings=[w for w in clues['warnings'] if w not in ('non_0DTE_reference','QQQ_not_explicit')]
 if any(b['kind'] in ('contract_reference','price_condition') and not b['terms'].get('symbol') for b in blocks) and len(tickers)>1:warnings.append('Multiple underlyings: bindings require clarification.')
 if any(b['kind']=='contract_reference' and b['terms']['strike'] is None for b in blocks) and len(strikes)>1:warnings.append('Multiple strikes: no side-to-strike pairing was guessed.')
 accepted=None
 # A narrow supported natural-language form; every order term must occur explicitly.
 if intent is None and symbol and not warnings:
  start=re.match(r'^\s*(?:Please\s+)?(buy|purchase|hold|cancel)\b',text,re.I)
  complex_text=re.search(r'\b(?:if|unless|except|not|never|could|might|maybe|hypothetical|after|before|while|until|and|or)\b',text,re.I)
  if start and not complex_text and len(re.findall(r'\b(?:buy|purchase|sell|exit|hold|cancel|suspend|resume|revise)\b',text,re.I))==1:
   action={'buy':'enter','purchase':'enter','hold':'hold','cancel':'cancel'}[start[1].lower()]
   annotation={'meaning':'instruction','action':action,'symbol':symbol,'evidence':[text[:1000]]}
   if len(text)>1000:annotation=None
   if annotation and action=='enter':
    quantity=re.search(r'\b(?:buy|purchase)\s+(\d+)\b',text,re.I)
    price=re.search(r'\blimit(?: price| premium)?\s*\$?(\d+(?:\.\d+)?)',text,re.I)
    duration=re.search(r'\bvalid for\s+(\d+)\s+seconds\b',text,re.I)
    if len(rights)==1 and len(strikes)==1 and expiry and quantity and price and duration:
     annotation.update(instrument={'kind':'option','right':rights[0],'strike':strikes[0],'expiry':expiry},quantity=int(quantity[1]),limit_price=float(price[1]),valid_for_seconds=int(duration[1]))
    else:annotation=None
   if annotation:
    intent=annotation
    for b in blocks:
     if b['kind']=='instruction':b.update(status='RECOGNIZED_EXPLICIT_INSTRUCTION',missing=[])
 if intent is not None:
  # Optional sending-agent semantic annotation uses the existing interface. It is not required to receive text.
  from .shared_chat import check
  try:
   check({'text':text,'intent':intent});accepted=copy.deepcopy(intent)
   quoted=[{'start':text.index(e),'end':text.index(e)+len(e),'text':e} for e in intent['evidence']]
   blocks.append({'kind':'sending_agent_annotation','capability':{'observe':'record_agent_observation','watch':'watch_price','clarify':'request_clarification'}.get(intent['action'],'controller_preview'),
      'terms':accepted,'evidence':quoted,'missing':[],'status':'SENDING_AGENT_ANNOTATION','authority':'NOT_INDEPENDENT_EXECUTION_AUTHORIZATION'})
  except Exception as exc:
   warnings.append('Sending-agent annotation invalid: '+type(exc).__name__)
 for b in blocks:
  for item in b['missing']:
   if item not in missing:missing.append(item)
 # A clear absence of an entry candidate does not ask for action clarification
 # about incidental levels, old positions or contracts mentioned in commentary.
 no_candidate,conflicting=current_pass(text)
 none=bool(no_candidate and not conflicting and intent is None)
 ambiguous_candidate=bool(no_candidate and conflicting and intent is None)
 if ambiguous_candidate:warnings.append('Conflicting no-candidate and candidate opinions require clarification.')
 if none:
  missing=[];warnings=[]
  for block in blocks:
   block['research_missing']=block['missing'];block['missing']=[]
   if block['kind']!='restriction':
    block.update(status='RECORDED_ONLY',capability='record_agent_observation')
  accepted=None
 from .analysis_evidence import extract_research
 research_evidence=extract_research(text,at)
 result={'research_evidence':research_evidence,'version':VERSION,'candidate_disposition':'NONE' if none else 'AMBIGUOUS' if ambiguous_candidate else 'UNSPECIFIED','no_candidate_evidence':no_candidate if none else None,'original_text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
   'blocks':blocks,'missing_information':missing,'warnings':warnings,'agent_stated_decisions':clues['agent_stated_decisions'],
   'sending_agent_intent':accepted,'inferences':[], 'strategy_rules':[],
   'status':'NEEDS_CLARIFICATION' if missing or warnings else 'SORTED',
   'limitations':'Bounded text recognition, not general language understanding. Unsupported meaning remains visible and requires clarification. No missing trading preference is invented.'}

 from .preferred_candidate import select_preferred
 result['preferred_candidate']=select_preferred(result)
 return result

async def research(ex,sorted_text):
 """Resolve only exact supplied identities. Never pick a strike, expiry, quantity or price."""
 from .live_broker import option_quote
 findings=[]
 if sorted_text.get('candidate_disposition')=='NONE':return findings
 for b in sorted_text['blocks']:
  if b['kind']!='contract_reference' or b['missing']:continue
  terms=b['terms'];item={'supplied_terms':terms,'status':'UNAVAILABLE'};findings.append(item)
  try:
   raw=await ex.broker.read('get_option_instruments',{'chain_symbol':terms['symbol'],'expiration_dates':terms['expiry'],'strike_price':str(terms['strike']),'type':terms['right'],'state':'active'})
   item['contract_response']=raw
   rows=[r for r in raw.get('instruments',[]) if r['chain_symbol']==terms['symbol'] and r['expiration_date']==terms['expiry'] and float(r['strike_price'])==terms['strike'] and r['type']==terms['right']]
   if raw.get('next') or len(rows)!=1:raise ValueError('Exact contract absent, ambiguous or chain response incomplete')
   item['contract']=rows[0];quotes=await ex.broker.read('get_option_quotes',{'instrument_ids':[rows[0]['id']]});item['quote_response']=quotes
   matched=[q for q in quotes['results'] if q['quote']['instrument_id']==rows[0]['id']]
   if len(matched)!=1:raise ValueError('Exact quote absent or ambiguous')
   item['quote']=option_quote(matched[0],ex.clock());item['status']='RESOLVED'
   item['note']='Market facts only. This lookup selects no trade and sets no entry price or size.'
  except Exception as exc:item['reason']=type(exc).__name__+': '+str(exc)[:200]
 return findings
