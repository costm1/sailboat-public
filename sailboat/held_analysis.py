"""Bounded prose recognition; compile into the existing versioned profit manager.
No model, strategy, broker transport or independent execution authority.
"""
import asyncio,copy,re
from decimal import Decimal
from .live_broker import option_quote
from .order_execution import TERMINAL
from .shape_sorter import sort_text

VERSION='held-premium-analysis-v1'

def binding_text(text):
 # The entry sorter also recognizes bare "put"/"call". In held analysis those
 # words can be verbs, not competing contracts. Mask only explicit verb grammar,
 # preserving offsets and the immutable original evidence. Named options remain.
 pattern=r"\b(?:(?:I|we|you|they|he|she)(?:['’]d| would| could| might| may| can| will)?\s+|(?:would|could|might|may|can|should|will)\s+)(?P<verb>put|call)\b|\b(?P<idiom>put|call)(?=\s+(?:it|this|that)\b)"
 def mask(m):
  start,end=m.span('verb' if m.group('verb') else 'idiom')
  return m[0][:start-m.start()]+' '*(end-start)+m[0][end-m.start():]
 text=re.sub(pattern,mask,text,flags=re.I)
 # A judgment idiom is not an option right. Keep actual named calls untouched.
 return re.sub(r"\b(?:it|this|that) is a (?:marginal|close|tough|difficult) (?P<word>call)\b",
  lambda m:m[0][:-4]+' '*4,text,flags=re.I)

def entry_thesis(text,ins,at):
 """Select source paragraphs for the verified held contract before truncation."""
 selected=[]
 for paragraph in re.finditer(r'[^\n]+',text):
  terms=[b['terms'] for b in sort_text(binding_text(paragraph[0]),at)['blocks'] if b['kind']=='contract_reference']
  if not terms:continue
  if all(t['symbol']==ins['chain_symbol'] and t['right']==ins['type'] and
         t['strike'] is not None and Decimal(str(t['strike']))==Decimal(ins['strike_price']) and
         t['expiry']==ins['expiration_date'] for t in terms):selected.append(paragraph[0])
 return '\n'.join(selected) or None


async def enrich(ex,req,context):
 """Exact read-only identity/quote lookup. Keep internal position IDs out of the prompt."""
 facts={}
 for p in req['context']['positions']:
  if p['kind']!='option':continue
  # Independent reads for the same verified ID share one round trip window.
  # Neither result is used until both have passed the existing checks.
  rows,raw=await asyncio.gather(
   ex.broker.pages('get_option_instruments',{'ids':p['id']},'instruments'),
   ex.broker.read('get_option_quotes',{'instrument_ids':[p['id']]}))
  if len(rows)!=1:raise ValueError('Exact held contract unavailable')
  ins=rows[0]
  if (ins['id'],ins['chain_symbol'],ins['expiration_date'])!=(p['id'],p['symbol'],p['expiration_date']):raise ValueError('Held contract identity mismatch')
  if len(raw['results'])!=1:raise ValueError('Exact held quote unavailable')
  q=option_quote(raw['results'][0],ex.clock())
  if q['id']!=p['id']:raise ValueError('Held quote identity mismatch')
  facts[p['position_id']]={'contract':ins,'quote':q}
  label={'strike_price':ins['strike_price'],'option_type':ins['type'],'expiration_date':ins['expiration_date']}
  if ex.worker.status().get('position_labels',{}).get(p['id'])!=label:
   ex.journal.edit(lambda state:state.setdefault('position_labels',{}).__setitem__(p['id'],label))
  h=next(h for h in context['holdings'] if h.get('instrument_id')==p['id'])
  h.update(strike_price=ins['strike_price'],option_type=ins['type'],mark_bid=q['bid'],mark_ask=q['ask'],quote_at=q['asof'],
   market_value=str(Decimal(q['bid'])*100*p['quantity']),unrealized_pnl=str((Decimal(q['bid'])*100-Decimal(p['unit_cost']))*p['quantity']))
  old=req['context']['position_theses'].get(p['position_id'])
  notes=ex.worker.status().get('agent_assessments',[])
  safe_old=old and old.get('position_opened_at')==p.get('opened_at') and old['expected_position_quantity']==p['quantity'] and Decimal(old['expected_unit_cost'])==Decimal(p['unit_cost']) and any(n.get('held_analysis_version')==VERSION and n['request_id']==old['request_id'] for n in notes)
  h['previous_thesis']={'analysis':old['reasoning'],'expected_resale_premium':old['target_bid']} if safe_old else None
  if not safe_old:
   # Entry rationale from the same filled plan, not an unrelated account-wide opinion.
   for plan_id,row in reversed(list(req['context']['active_plans'].items())):
    if row['plan']['option_id']==p['id'] and row['plan']['side']=='buy' and row['filled']:
     from .scheduler import timestamp
     if p.get('opened_at') and row['plan']['created_at']+120<timestamp(p['opened_at']):continue
     shared=req['context'].get('shared_plans',{}).get(plan_id)
     if shared and any(n.get('analyst_protocol')=='qqq_options_analyst_v1' and n.get('reasoning')==shared.get('reasoning') for n in notes):
      selected=entry_thesis(shared['reasoning'],ins,req['created_at'])
      if selected:h['previous_thesis']={'analysis':selected,'expected_resale_premium':None}
     break
  if h['previous_thesis']:
   text=h['previous_thesis']['analysis'];short=text[:1600]
   h['previous_thesis']['analysis']=short+('…' if len(text)>1600 else '')
 return facts

# An explicit analytical conclusion is required. Mentions, hypotheticals and
# conditional future sales are not automatically rewritten into present instructions.
ACTION=re.compile(r"\b(?:hold|sell(?:[ -]for[ -]profit)?|take (?:the )?(?:(?:current|available) |\$\d+(?:\.\d+)? )bid|take (?:the |available |current )*profits?|taking (?:the |available |current )*profit is preferable)\b",re.I)
UNRESOLVED=re.compile(r"\b(?:if|unless|until|when|once|would|could|might|maybe|perhaps|may|can|potentially|suppose|assuming|not|never|avoid|instead|hypothetical(?:ly)?|example|quote[ds]?|previously|earlier|yesterday|partial|half|some|or|between|after|before|later|tomorrow)\b|(?:don|wouldn|shouldn|couldn|can|won)[’']t",re.I)
TARGET=re.compile(r"\b(?:(?:expected |profitable |resale |premium |bid )*target(?: (?:bid|premium|price))?(?: (?:is|of|at|around|near|to))?\s*[:=]?\s*\$?(\d+(?:\.\d+)?)|(?:expected |profitable )?(?:resale |bid )premium (?:of|at|near)\s*\$?(\d+(?:\.\d+)?)|hold(?: (?:these|the|this|my) (?:calls?|puts?|options?|position)| (?:it|them))? until \$(\d+(?:\.\d+)?)|(?:resale )?premium (?:could|may|might|should|can) (?:reach|rise to|appreciate to) \$?(\d+(?:\.\d+)?))",re.I)


def target_value(match):return next(v for v in match.groups() if v is not None)


def opinion_text(text):
 # First-person conditional grammar can express a present opinion. Other
 # hypotheticals and price/event conditions remain unresolved, with original evidence.
 text=re.sub(r"\bI(?:['’]d| would)\s+(?=(?:hold|sell|take (?:the )?(?:(?:current|available) |\$\d+(?:\.\d+)? )bid)\b)",'I ',text,flags=re.I)
 return TARGET.sub(lambda m:'premium target '+target_value(m),text)


def current_opinion(text):
 """Normalize present bid-taking opinions, never price/event prerequisites."""
 text=opinion_text(text)
 # Two synonymous current exit opinions are not a HOLD/SELL conflict.
 text=re.sub(r'\b(sell)(?: \(mild\))?,?\s+or at least take (?:the )?profits?\b',r'\1',text,flags=re.I)
 # "sell at or near the bid" is a present exit opinion. The host obtains a
 # new executable bid; source prices are evidence, never the execution quote.
 text=re.sub(r'\b(sell(?:[ -]for[ -]profit)?(?: now)?) at or near ((?:(?:the|current|available) )*(?:\$?\d+(?:\.\d+)? )?bid)\b',r'\1 at \2',text,flags=re.I)
 # These are two ways of expressing the SAME current preference. Do not strip
 # arbitrary "or"/"if" tails, which may contain a real action conflict/condition.
 text=re.sub(r",?\s+or at least (?:don['’]t|do not) rely on (?:further|more) (?:premium )?appreciation(?=[.!?;]|$)",'',text,flags=re.I)
 if re.search(r'\bsell(?:[ -]for[ -]profit)?(?: now)? at (?:(?:the|current|available) )*(?:\$?\d+(?:\.\d+)? )?bid\b',text,re.I):
  text=re.sub(r',?\s+or post \$?\d+(?:\.\d+)? and accept (?:the |current )?bid if there is no fill(?=[.!?;]|$)','',text,flags=re.I)
 return text


def scoped_conclusions(text,positions,facts,restricted):
 """Read the analyst's labeled current conclusion separately from discussion.

 This opt-in prose layout is shared by the prompt and interpreter. Legacy prose
 keeps its existing recognizer. No forecast, condition or score creates an order.
 """
 if not re.match(r'\s*Current conclusion:',text,re.I) or not re.search(r'^Reasoning:',text,re.M|re.I):return None
 held=[p for p in positions if p['kind']=='option' and p['symbol']=='QQQ']
 def blocked(reason):return [{'position_id':p['position_id'],'status':'NEEDS_CLARIFICATION','reason':reason} for p in held]
 if restricted:return blocked('Source restricts action')
 starts=list(re.finditer(r'^Current conclusion:[^\n]*',text,re.M|re.I))
 blocks={}
 for i,start in enumerate(starts):
  end=starts[i+1].start() if i+1<len(starts) else len(text)
  segment=text[start.start():end]
  conclusion=re.fullmatch(r'Current conclusion: (HOLD|SELL|UNCLEAR)\.',start[0].strip(),re.I)
  identity=re.search(r'^Position: QQQ \$(\d+(?:\.\d+)?) (call|put) expiring (\d{4}-\d{2}-\d{2})\.\s*$',segment,re.M|re.I)
  if not conclusion or not identity or len(re.findall(r'^Position:',segment,re.M|re.I))!=1:
   return blocked('One standalone current conclusion and exact held contract are required')
  matches=[p for p in held if Decimal(identity[1])==Decimal(facts[p['position_id']]['contract']['strike_price'])
   and identity[2].lower()==facts[p['position_id']]['contract']['type'] and identity[3]==p['expiration_date']]
  if len(matches)!=1:return blocked('Current conclusion does not uniquely match a broker-confirmed holding')
  p=matches[0];pid=p['position_id']
  if pid in blocks:return blocked('Repeated or conflicting current conclusions for the same holding')
  action=conclusion[1].upper()
  if action=='UNCLEAR':return blocked('Analyst did not provide a supported current conclusion')
  body=segment[len(start[0]):]
  # Explicit current opinions elsewhere remain conflicts, even under a future
  # heading. Conditional alternatives and analytical uses of action words do
  # not become current opinions merely because they mention HOLD or SELL.
  for part in re.split(r'\n|(?<=[.!?;])\s+',body):
   part=re.sub(r'^\s*(?:Reasoning|Resale outlook|Future conditions|(?:My )?current (?:preference|recommendation)|Recommendation|Decision|Action)(?::| is)\s*','',part,flags=re.I).strip()
   part=part.strip('* ')
   if re.match(r"^(?:I (?:would |do )?not|Do not|Don['’]t|Never) (?:hold|sell)\b",part,re.I):
    return blocked('Negated current instruction conflicts with the conclusion')
   if re.search(r'\b(?:but|however|actually|instead)\s+(?:I (?:would )?)?(?:hold|sell)\s+(?:now|regardless)\b',part,re.I):
    return blocked('Mixed current and conditional actions need clarification')
   opinion=re.match(r"^(?:(?:Actually|Instead|Now|However)[,:]?\s+)?(?:I(?: would|['’]d| prefer to| recommend)\s+)?(?P<action>hold|sell|take (?:the )?(?:current |available )?bid)\b(?P<tail>.*)",part,re.I)
   if not opinion:continue
   tail=opinion['tail'].strip()
   if re.match(r'^(?:only )?(?:if|when|unless|until|once)\b',tail,re.I):
    if len(list(ACTION.finditer(part)))!=1:return blocked('Mixed current and conditional actions need clarification')
    continue
   if re.match(r'^(?:vs\.?|versus|or) (?:hold|sell)\b',tail,re.I):continue
   other='HOLD' if opinion['action'].lower()=='hold' else 'SELL'
   if other!=action or len({a[0].lower() for a in ACTION.finditer(part)})>1:
    return blocked('Conflicting current hold/sell opinions')
  targets=list(re.finditer(r'^Expected resale target:\s*\$(\d+(?:\.\d+)?)\.\s*$',segment,re.M|re.I))
  labels=re.findall(r'^Expected resale target:',segment,re.M|re.I)
  if len(targets)!=len(labels) or len(targets)>1:return blocked('One unambiguous expected resale target is required')
  target=targets[0][1] if targets else None
  if target is not None and (action!='HOLD' or Decimal(target)<=0):return blocked('Invalid expected resale target for the current conclusion')
  evidence={'start':start.start(),'end':end,'text':segment}
  blocks[pid]={'position_id':pid,'status':'RECOGNIZED','action':'HOLD' if action=='HOLD' else 'SELL_FOR_PROFIT',
   'target_bid':target,'reason':None,'evidence':evidence,'supporting_evidence':[evidence],
   'hypothetical_alternatives':[{'start':start.end(),'end':end,'text':text[start.end():end],'kind':'analytical_discussion'}],
   'conclusion_evidence':{'start':start.start(),'end':start.end(),'text':start[0]},'recognition':'scoped_current_conclusion'}
 if set(blocks)!={p['position_id'] for p in held}:return blocked('A separate current conclusion is required for each holding')
 return [blocks[p['position_id']] for p in held]


def extract(text,positions,facts,at):
 """Recognize current opinions against broker holdings, not positions in prose."""
 results=[]
 restricted=bool(re.search(r"\b(?:no orders|do not (?:submit|trade|sell)|don[’']t (?:submit|trade|sell)|research.only|no (?:sale|selling|trade)|do not recommend|not recommended)\b",text,re.I))
 scoped=scoped_conclusions(text,positions,facts,restricted)
 if scoped is not None:return scoped
 paragraphs=[m for m in re.finditer(r'[^\n]+',text) if m[0].strip()]
 option_count=sum(p['kind']=='option' for p in positions)
 for p in positions:
  if p['kind']!='option' or p['symbol']!='QQQ':continue
  candidates=[];alternatives=[];unbound=['Source restricts action'] if restricted else []
  ins=facts[p['position_id']]['contract']
  for para in paragraphs:
   source=para[0]
   # A correction of the prior thesis is not a second current candidate. Mask
   # only the historical reference for binding; keep the actual holding and evidence.
   bound=re.sub(r'\((?:The )?(?:prior|previous) thesis (?:in the context )?describes a \$?\d+(?:\.\d+)? (?:call|put);(?= the actual holding is)',lambda m:' '*len(m[0]),source,flags=re.I)
   sorted_source=sort_text(binding_text(bound),at)
   terms=[b['terms'] for b in sorted_source['blocks'] if b['kind']=='contract_reference']
   def agrees(t):
    return (t['symbol'] in (None,p['symbol']) and t['right']==ins['type'] and
     (t['strike'] is None or Decimal(str(t['strike']))==Decimal(ins['strike_price'])) and
     t['expiry'] in (None,p['expiration_date']))
   if terms and (len(terms)!=1 or not agrees(terms[0])):continue
   if any('Multiple strikes' in w for w in sorted_source['warnings']):unbound.append(source);continue
   explicit_symbols=re.findall(r'\b([A-Z]{2,5})\s+(?:\$?\d+(?:\.\d+)?\s+)?(?:calls?|puts?)\b',source)
   if any(symbol!=p['symbol'] for symbol in explicit_symbols):continue
   if option_count!=1 and (not terms or any(v is None for v in terms[0].values())):
    if ACTION.search(source):unbound.append(source)
    continue
   for sentence in re.finditer(r'.+?(?:;|[.!?](?=\s|$)|$)',source):
    phrase=sentence[0]
    # A comparison heading is not two competing recommendations. Inspect its
    # body normally: "Hold or sell: sell now" must still conflict with HOLD.
    heading=re.match(r'^\s*Hold (?:or|vs\.?) sell(?: now)?\s*:\s*',phrase,re.I)
    if heading:
     phrase=phrase[heading.end():]
     if not ACTION.search(phrase):
      alternatives.append({'start':para.start()+sentence.start(),'end':para.start()+sentence.end(),
       'text':sentence[0],'kind':'comparison'})
      continue
    # A present HOLD followed by a future sell condition is one current choice,
    # not conflicting immediate orders. Preserve the condition as nonexecuting
    # evidence; do not strip other actions or conditional HOLD prerequisites.
    split=re.match(r'^(?P<hold>.+?),\s*but\s+(?P<exit>(?:I(?:[’\']d| would) |I )?sell (?:only )?(?:if|when)\b.+)$',phrase,re.I)
    if split:
     head=current_opinion(split['hold']);ha=list(ACTION.finditer(head));ea=list(ACTION.finditer(split['exit']))
     if len(ha)==1 and ha[0][0].lower()=='hold' and len(ea)==1 and not UNRESOLVED.search(head):
      alternatives.append({'start':para.start()+sentence.start()+split.start('exit'),
       'end':para.start()+sentence.end(),'text':split['exit'],'kind':'conditional_exit'})
      phrase=split['hold']
    # Labeled preference/forecast reporting is not a second HOLD/SELL command.
    if re.match(r'^\s*Forecast:',phrase,re.I) or re.match(r'^\s*Preference:\s*(?:HOLD|SELL)\s+\d+(?:\.\d+)?%',phrase,re.I):continue
    acts=list(ACTION.finditer(phrase))
    if not acts:continue
    # Event-led discussion has a future scope, independent of the present
    # conclusion. Retain exact evidence, never install its exits or targets.
    # Only one consequent action is supported; mixed/negated/quoted or unclear
    # clauses still require clarification. Semicolons above bound the scope so
    # an adjacent "sell now" cannot disappear into a preceding condition.
    event=re.match(r'^\s*(?:If|When|Once)\s+.+,\s*(?P<consequence>.+)$',phrase,re.I)
    consequence=current_opinion(event['consequence']) if event else ''
    conditional=(event and len(acts)==1 and ACTION.search(consequence) and
      not UNRESOLVED.search(consequence) and not re.search(r'\b(?:now|regardless|actually)\b',consequence,re.I) and
      not any(c in phrase for c in ('?','"','“','”')))
    # "Hold only while ..." describes a future monitoring condition; alone it
    # cannot establish a present HOLD. More ambiguous prerequisites stay blocked.
    ongoing=re.match(r'^\s*Hold only while\s+[^;.!?]+[.!?;]?$',phrase,re.I)
    if conditional or (ongoing and len(acts)==1 and not re.search(r'\b(?:not|never|now|regardless)\b',phrase,re.I)):
     alternatives.append({'start':para.start()+sentence.start(),'end':para.start()+sentence.end(),
      'text':phrase,'kind':'conditional_discussion'})
     continue
    # Comparison headings describe the cost of selling, not a second current choice.
    if re.match(r'^\s*Hold vs sell now:\s*selling now (?:locks|would lock)\b',phrase,re.I) and len(acts)==2:
     alternatives.append({'start':para.start()+sentence.start(),'end':para.start()+sentence.end(),'text':phrase,'kind':'comparison'})
     continue
    # Future exits are retained separately; they cannot install a loss sale.
    if re.match(r'^\s*Sell (?:only )?(?:if|when)\b',phrase,re.I) and len(acts)==1:
     alternatives.append({'start':para.start()+sentence.start(),'end':para.start()+sentence.end(),'text':phrase,'kind':'conditional_exit'})
     continue
    # A specifically marked alternative to the current recommendation is retained,
    # but does not veto an otherwise unambiguous SELL. Never discard mixed actions.
    if ((re.match(r'^\s*If you (?:hold anyway|(?:choose|decide) to hold (?:anyway|instead))\s*,',phrase,re.I) or
         re.match(r'^\s*(?:I(?:[’\']d| would) |Would )hold only if\b',phrase,re.I))
        and len(acts)==1 and acts[0][0].lower()=='hold'):
     start=para.start()+sentence.start()
     alternatives.append({'start':start,'end':para.start()+sentence.end(),'text':phrase})
     continue
    normalized=current_opinion(phrase)
    priced_sale=any(a[0].lower()!='hold' for a in acts) and re.search(r'\bat \$\d+(?:\.\d+)?(?!\d|\.\d)(?!\s+bid\b)',normalized,re.I)
    if UNRESOLVED.search(normalized) or priced_sale or re.search(r'\bin \d+ (?:seconds?|minutes?|hours?)\b',phrase,re.I) or '?' in phrase or '"' in phrase or '“' in phrase or '”' in phrase:
     unbound.append(phrase);continue
    kinds={'HOLD' if a[0].lower()=='hold' else 'SELL_FOR_PROFIT' for a in acts}
    candidates.append((kinds,para))
  row={'position_id':p['position_id'],'status':'NEEDS_CLARIFICATION','reason':'A clear current hold or sell opinion is needed for this holding.'}
  if unbound or not candidates:results.append(row);continue
  kinds=set().union(*(k for k,para in candidates))
  if len(kinds)!=1:results.append(row);continue
  paras={para.start():para for k,para in candidates};para=next(iter(paras.values()))
  action=next(iter(kinds))
  # This repair scopes current HOLD discussion only. Preserve the existing
  # stricter SELL prerequisite checks rather than expanding sale authority.
  if action!='HOLD' and any(a.get('kind')=='conditional_discussion' for a in alternatives):results.append(row);continue
  if any(a.get('kind') not in ('comparison','conditional_exit','conditional_discussion') for a in alternatives) and action!='SELL_FOR_PROFIT':results.append(row);continue
  target=None;source='\n'.join(m[0] for m in paras.values())
  if action=='HOLD':
   target_sentences=[m[0] for m in re.finditer(r'.+?(?:[.!?](?=\s|$)|$)',source) if TARGET.search(m[0])]
   ambiguous=any(UNRESOLVED.search(opinion_text(sentence)) or any(re.match(r'\s*[-–]\s*\$?\d',sentence[m.end():]) for m in TARGET.finditer(sentence)) for sentence in target_sentences)
   values={target_value(m) for m in TARGET.finditer(source)}
   unparsed_target=not values and re.search(r'\b(?:target|resale premium)\b[^.!?\n]*\$?\d',source,re.I)
   if len(values)>1 or ambiguous or unparsed_target:
    row['reason']='One unambiguous expected option resale premium is needed; none was invented.';results.append(row);continue
   target=next(iter(values)) if values else None
   if target is not None and (Decimal(target)<=0 or any(re.search(r'\b(?:percent|percentage|underlying target|QQQ target|QQQ price target|stock target)\b|%',s,re.I) for s in target_sentences)):
    row['reason']='The expected value must be an option premium, not an underlying price or percentage.';results.append(row);continue
  row.update(status='RECOGNIZED',action=action,target_bid=target,
   evidence={'start':para.start(),'end':para.end(),'text':para[0]},
   supporting_evidence=[{'start':m.start(),'end':m.end(),'text':m[0]} for m in paras.values()],
   hypothetical_alternatives=alternatives,reason=None)
  results.append(row)
 return results

async def compile_response(ex,req,text,facts):
 """Host fills identity/version/fee/order mechanics; analyst provides meaning only."""
 positions=req['context']['positions'];now=ex.clock()
 rows=extract(text,positions,facts,now)
 from .analyst_preferences import held_preferences
 scores=held_preferences(text) if len(rows)==1 else {'preference':None,'forecast':None}
 for row in rows:row['analyst_scores']=scores
 response={'request_id':req['request_id'],'context_hash':req['context_hash'],'action':'observe','plan':None,'plan_id':None,'plan_version':None}
 if not rows or any(r['status']!='RECOGNIZED' for r in rows):return response,rows
 if sum(r['action']=='SELL_FOR_PROFIT' for r in rows)>1:
  for r in rows:r.update(status='NEEDS_CLARIFICATION',reason='Only one immediate profit-sale conclusion can be applied per assessment.')
  return response,rows
 blocks=[];state=ex.worker.status()
 for row in rows:
  p=next(p for p in positions if p['position_id']==row['position_id']);fact=facts[p['position_id']]
  from .position_updates import management_block
  # Paused positions still receive analytical HOLD updates; no sell is queued.
  if state.get('manual_management_holds',{}).get(p['position_id'],{}).get('active'):
   row.update(status='BLOCKED',reason=management_block(state,p['position_id']));return response,rows
  old=req['context']['position_theses'].get(p['position_id']);version=old['target_version'] if old else 0
  plans=[(k,r) for k,r in req['context']['active_plans'].items() if r['plan']['option_id']==p['id']]
  working=[r for r in plans if r[1]['status'] not in TERMINAL]
  if len(working)>1:raise ValueError('Multiple working position orders need reconciliation')
  ref=(working or plans)[-1] if plans else None
  if row['action']=='HOLD' and row['target_bid'] is None:
   # A clear HOLD is useful even without a numeric forecast. Use the existing
   # HOLD directive (no sale/target invented); it cancels a managed fallback.
   same=old and old['expected_position_quantity']==p['quantity'] and Decimal(old['expected_unit_cost'])==Decimal(p['unit_cost'])
   price=old['target_bid'] if same else None
   blocks.append({'position_id':p['position_id'],'option_id':p['id'],'expected_position_quantity':p['quantity'],'expected_unit_cost':p['unit_cost'],
    'expected_target_version':version,'target_version':version+1,'plan_id':ref[0] if ref else None,'plan_version':ref[1]['plan']['plan_version'] if ref else None,
    'action':'HOLD','target_bid':price,'expected_upside_usd':None,'roundtrip_cost_allowance_usd':None,'reasoning':row['evidence']['text'][:2000]})
   row.update(host_action='HOLD',target_bid=price)
   continue
  details=await ex.profit_manager.port.exit_details(p,state,state.get('adaptive_profit',{}).get(p['position_id'],{}))
  qty=p['quantity']
  # Fees remain informational; unknown costs are not represented as zero.
  costs=(Decimal(details['entry_fee_per_contract'])*qty+Decimal(details['exit_fee_total'])
         if details.get('entry_fee_per_contract') is not None and details.get('exit_fee_total') is not None else None)
  action='REVISE_TARGET' if row['action']=='HOLD' else 'SELL_FOR_PROFIT'
  price=row['target_bid']
  if action=='SELL_FOR_PROFIT':
   raw=await ex.broker.read('get_option_quotes',{'instrument_ids':[p['id']]})
   if len(raw['results'])!=1:raise ValueError('Exact fresh exit quote unavailable')
   quote=option_quote(raw['results'][0],ex.clock())
   if quote['id']!=p['id']:raise ValueError('Exit quote identity mismatch')
   price=quote['bid'];row['execution_quote']=quote
  if (Decimal(price)*100-Decimal(p['unit_cost']))*qty<=0:raise ValueError('Target/bid does not exceed purchase premium; no loss sale')
  tick=Decimal(details['below_tick'] if Decimal(price)<Decimal(details['cutoff_price']) else details['above_tick'])
  if Decimal(price)%tick:raise ValueError('Target is not a valid option premium tick')
  block={'position_id':p['position_id'],'option_id':p['id'],'expected_position_quantity':qty,'expected_unit_cost':p['unit_cost'],
   'expected_target_version':version,'target_version':version+1,'plan_id':ref[0] if ref else None,'plan_version':ref[1]['plan']['plan_version'] if ref else None,
   'action':action,'target_bid':price,'expected_upside_usd':str((Decimal(price)*100-Decimal(p['unit_cost']))*qty),
   'roundtrip_cost_allowance_usd':str(costs) if costs is not None else None,'reasoning':row['evidence']['text'][:2000]}
  blocks.append(block)
  row.update(target_bid=price,host_action=action,profit_basis='premium_before_fees',closing_fee_status='Informational; Robinhood accounts for fees')
  if action=='SELL_FOR_PROFIT':
   response['plan']={'decision_id':req['decision_id'],'plan_id':req['new_plan_id'],'plan_version':1,'account_number':ex.broker.account_number,
    'option_id':p['id'],'symbol':p['symbol'],'side':'sell','position_effect':'close','quantity':qty,'order_type':'limit',
    'limit_prices':[price],'stop_price':None,'condition':None,'invalidation':None,'created_at':now,
    'expires_at':min(req['reply_before'],details['close_at']),'replace_after_seconds':60,'max_transport_attempts':3,
    'position_id':p['position_id'],'expected_position_quantity':qty,'time_in_force':'gfd','market_hours':'regular_hours'}
 response.update(action='execute' if response['plan'] else 'hold',position_blocks=blocks)
 return response,rows
