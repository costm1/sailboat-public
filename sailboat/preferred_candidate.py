"""Source-supported preference selection, independent of the source's PASS opinion.

No ranking model: only explicit, uniquely named preferences are selected. This
module cannot authorize orders or supply missing contract identity or prices.
"""
import copy
import re
from decimal import Decimal
from .text_bindings import PAIR, sentences, evidence, local_symbol, focus


def select_preferred(sorted_text):
    text = sorted_text['original_text']
    default, _ = focus(text)
    selected = {}
    blocked = []
    for sentence in sentences(text):
        phrase = sentence[0]
        if phrase.strip().startswith(('"', '\u201c', '>')):
            continue
        if re.search(r'\b(?:no longer|withdraw|retract)\b.*\b(?:prefer|preferred|preference|candidate)\b',phrase,re.I):
            blocked.append(evidence(text,sentence.start(),sentence.end()))
            continue
        pairs = list(PAIR.finditer(phrase))
        for pair in pairs:
            before, after = phrase[:pair.start()], phrase[pair.end():]
            prefix = re.search(r"(?:preferred candidate|(?:the |my )?(?:preferred|best|top)[ -](?:option|contract|candidate)|(?:I |my )?(?:prefer|preference is))\s*(?:is|:)?\s*(?:the\s+)?$", before, re.I)
            suffix = re.match(r"\s+is\s+(?:the |my )?(?:most interesting|preferred|best|top)(?:\s+(?:bullish|bearish))?\s+(?:candidate|contract|option)\b", after, re.I)
            if not (prefix or suffix):
                continue
            span = evidence(text, sentence.start(), sentence.end())
            if len(pairs)>1:
                blocked.append(span)
                continue
            # A preference only under a future/forced scenario is not current.
            if re.search(r"\b(?:if|unless|when|once|until|after|before|hypothetical|example|earlier|previously|no longer|not|never)\b|\b(?:would|might|could)\s+prefer\b", phrase, re.I):
                blocked.append(span)
                continue
            symbol = pair['symbol'].upper() if pair['symbol'] else local_symbol(text, sentence.start()+pair.start(), default)
            right = pair['right'].lower().rstrip('s')
            strike = float(pair['strike'])
            matches = [b for b in sorted_text['blocks'] if b['kind']=='contract_reference'
                       and b['terms']['symbol']==symbol and b['terms']['right']==right and b['terms']['strike']==strike]
            if len(matches)!=1:
                blocked.append(span)
                continue
            ref = matches[0]
            identity = tuple(ref['terms'][k] for k in ('symbol','right','strike','expiry'))
            selected.setdefault(identity, {'terms':copy.deepcopy(ref['terms']),
                'evidence':copy.deepcopy(ref['evidence']), 'preference_evidence':[]})['preference_evidence'].append(span)
    if len(selected)>1 or (selected and blocked):
        return {'status':'AMBIGUOUS', 'reason':'Conflicting or qualified preferred candidates.', 'alternatives':list(selected.values()), 'qualified_evidence':blocked}
    if not selected:
        return {'status':'QUALIFIED' if blocked else 'NONE', 'qualified_evidence':blocked}
    return {'status':'SELECTED', **next(iter(selected.values()))}


def entry_conditions(text):
    """Keep stated entry prerequisites; risk commentary is not an entry trigger."""
    matches = []
    for s in sentences(text):
        p = re.sub(r'after[- ]cost','net cost',s[0],flags=re.I)
        condition = re.search(r'\b(?:if|unless|when|once|until|after|before|only|requires?|must|wait|reconsider)\b', p, re.I)
        entry = re.search(r'\b(?:enter|entry|buy|buying|purchase|consider|choose|prefer|reconsider|wait|watch)\b', p, re.I)
        # "requires a resale bid" describes exit economics, not an entry gate.
        economics = (re.search(r'\b(?:would require|requires?)\s+(?:a\s+)?resale bid\b', p, re.I)
                     and not re.search(r'\b(?:if|unless|when|once|until|after|before|only|must|wait)\b',p,re.I))
        if condition and entry and not economics:
            matches.append(evidence(text,s.start(),s.end()))
        elif re.search(r'\b(?:calls?|puts?)\s+(?:above|below)|\b(?:requires?|must have)\s+(?:rising|strong|high)\s+volume',p,re.I):
            matches.append(evidence(text,s.start(),s.end()))
        elif condition and re.search(r'\b(?:break|breaks|breakout|breakdown|hold|holds|confirmation|above|below)\b',p,re.I):
            matches.append(evidence(text,s.start(),s.end()))
    return matches


def candidate_pieces(sorted_text):
    """Adapt existing extraction to Assembly evidence, never select an order here."""
    text=sorted_text['original_text'];preferred=select_preferred(sorted_text)
    context=sorted_text.get('trading_domain_context',{})
    refs=[]
    for block in sorted_text['blocks']:
        if block['kind']!='contract_reference':continue
        ref=copy.deepcopy(block)
        # Only the authenticated host's existing 0DTE research task supplies an
        # omitted date. Analyst text cannot assert this context or override dates.
        if ref['terms']['expiry'] is None and context.get('symbol')==ref['terms']['symbol'] and context.get('expiry'):
            if not re.search(r'\b(?:expir\w*|0DTE|today|tomorrow|yesterday|next|20\d\d|Monday|Tuesday|Wednesday|Thursday|Friday|January|February|March|April|May|June|July|August|September|October|November|December)\b',text,re.I):
                ref['terms']['expiry']=context['expiry'];ref['host_identity_context']=copy.deepcopy(context)
        if not any(v is None for v in ref['terms'].values()):refs.append(ref)
    research=sorted_text['research_evidence'];rows=[]
    probabilities=resale_probabilities(text,refs)
    # First-person execution disclaimers describe the analyst's role. Imperative
    # restrictions remain source restrictions; neither changes owner permissions.
    restricted=bool(re.search(r'\bresearch.only\b|\bsimulation test\b|\bexample only\b',text,re.I))
    for m in re.finditer(r"\b(?:do not|don't|don’t)\s+(?:submit|buy|trade)\b",text,re.I):
        before=re.split(r'[.!?\n]',text[:m.start()])[-1]
        # A personal disclaimer describes the contributor. An imperative limits
        # that source's support, never the owner's permissions or other sources.
        if not re.search(r'\b(?:I|we|Claude|analyst)\b.{0,50}$',before,re.I):restricted=True
    for ref in refs:
        terms=copy.deepcopy(ref['terms']);conditions=[];unresolved=[]
        probability=probabilities.get(tuple(terms.values()))
        preferred_terms=copy.deepcopy(preferred.get('terms'))
        if preferred_terms and preferred_terms['expiry'] is None and ref.get('host_identity_context'):preferred_terms['expiry']=terms['expiry']
        preference=preferred.get('status')=='SELECTED' and preferred_terms==terms
        for scenario in research['scenarios']:
            matches=[c for c in scenario['contracts'] if c.get('symbol')==terms['symbol'] and c.get('right')==terms['right'] and c.get('strike') is not None and float(c['strike'])==terms['strike']]
            if matches:
                v=scenario['value'];conditions.append({'symbol':v['symbol'],'op':v['operator'],'price':v['threshold'],'evidence':scenario['evidence']})
                unresolved.extend(scenario['unresolved'])
        # Keep unsupported condition semantics unresolved. Do not turn strong
        # momentum, rising volume, completed bars or confirmation into a number.
        for e in entry_conditions(text):
            phrase=e['text']
            if probability and re.fullmatch(r'\s*(?:PASS|WAIT|NO TRADE)[.!]?\s*',phrase,re.I):continue
            if re.search(r"\bI (?:would )?(?:personally )?wait\b|\bI(?:'d|’d) wait\b",phrase,re.I) and not re.search(r'\b(?:until|for|after|above|below|confirmation)\b',phrase,re.I):continue
            if re.search(r"\bSailboat.*\bpolicy\b",phrase,re.I):continue
            bound=[b for b in refs if any(e['start']<=v['start']<e['end'] for v in b['evidence'])]
            if bound and terms not in [b['terms'] for b in bound]:continue
            sides=set(v.lower().rstrip('s') for v in re.findall(r'\b(?:calls?|puts?)\b',phrase,re.I))
            if len(sides)==1 and terms['right'] not in sides:continue
            measured=any(any(v['start']==e['start'] and v['end']==e['end'] for v in c['evidence']) for c in conditions)
            if not measured or re.search(r'\b(?:clean|decisive|holds?|volume|momentum|confirm(?:ed|ing|ation)?|completed|closes|bars|fails? to bounce)\b|\d+\s+or\s+\d+',phrase,re.I):
                unresolved.append('Unresolved source prerequisite: '+phrase.strip())
        if not preference and not probability:
            if sorted_text.get('candidate_disposition')=='AMBIGUOUS' or preferred['status'] in ('AMBIGUOUS','QUALIFIED'):
                unresolved.append('Source candidate preference is conflicting or qualified')
            if re.search(r"\b(?:hypothetically|hypothetical|suppose|assuming|maybe|perhaps)\b|\b(?:might|could) consider\b|\b(?:wouldn[’']t|would not) (?:buy|consider|choose)\b",text,re.I):
                unresolved.append('Tentative, hypothetical or negated candidate opinion')
            if not conditions and re.search(r'\b(?:if|unless)\b',text,re.I):
                unresolved.append('Conditional source lacks a measurable bound prerequisite')
        withdrawn=False
        for e in sentences(text):
            if re.search(r'\b(?:no longer|withdraw|retract)\b',e[0],re.I):
                pairs=list(PAIR.finditer(e[0]))
                if any(p['right'].lower().rstrip('s')==terms['right'] and float(p['strike'])==terms['strike'] for p in pairs):withdrawn=True
        source_contract=next((c for c in research['contracts'] if c['symbol']==terms['symbol'] and c['right']==terms['right'] and c['strike'] is not None and float(c['strike'])==terms['strike']),{})
        # An unconditional numeric estimate replaces analyst entry preferences.
        # Preserve their prose as evidence, but do not add a WAIT/confirmation gate.
        # Conditional probability claims themselves remain explicitly unresolved.
        if probability and 'percent' in probability:conditions=[];unresolved=[]
        rows.append({'terms':terms,'probability':probability,'preferred':preference,'supported':bool(probability) or preference or bool(conditions) or (len(refs)==1 and sorted_text.get('candidate_disposition')!='NONE'),
                     'withdrawn':withdrawn,'restricted':restricted,'conditions':conditions,'unresolved':list(dict.fromkeys(unresolved)),
                     'evidence':copy.deepcopy(ref['evidence']),'analyst_preferences':research['preferences'],
                     'confidence':'SOURCE_UNSPECIFIED','host_identity_context':ref.get('host_identity_context'),'economics':{'contract':source_contract,'forecasts':research['forecasts'],
                     'market_conditions':research['observations'],'proposed_parameters':research['proposed_parameters']}})
    return rows


def resale_probabilities(text,refs):
    """Bind explicit percentages to existing exact contracts; never rank or impute."""
    found={};previous=[]
    # A source may define the common metric once, then label CALL/PUT estimates.
    metric=lambda s: bool(re.search(r'\bprofit(?:able)?\b',s,re.I) and re.search(r'\b(?:premium|resale|resell)\b',s,re.I))
    def horizons(s):
        values={int(v)*60 for v in re.findall(r'\b(\d+)[ -]min(?:ute)?s?\b',s,re.I)}
        for low,high in re.findall(r'\b(\d+)\s*(?:[-–]|to)\s*(\d+)\s*min(?:ute)?s?\b',s,re.I):values.update((int(low)*60,int(high)*60))
        if re.search(r'\b(?:half[- ](?:an?[- ])?hour|thirty[ -]minutes?)\b',s,re.I):values.add(1800)
        return values
    shared=set().union(*(horizons(s[0]) for s in sentences(text) if metric(s[0]) and re.search(r'\b(?:both|these|following|estimates|probabilities)\b',s[0],re.I)))
    for clause in re.finditer(r'[^;\n]+(?:;|\n|$)',text):
        for sentence in sentences(clause[0]):
            phrase=sentence[0];start=clause.start()+sentence.start();end=clause.start()+sentence.end()
            pairs=list(PAIR.finditer(phrase))
            bound=[r for r in refs if any(r['terms']['right']==p['right'].lower().rstrip('s')
                   and r['terms']['strike']==float(p['strike'])
                   and r['terms']['symbol']==(p['symbol'].upper() if p['symbol'] else local_symbol(text,start+p.start(),focus(text)[0])) for p in pairs)]
            if pairs:previous=bound
            if '%' not in phrase and not re.search(r'\bpercent\b',phrase,re.I):continue
            if phrase.strip().startswith(('"','\u201c','>')) or re.search(r'\b(?:earlier|previously|yesterday|historical|example|hypothetical)\b',phrase,re.I):continue
            if not re.search(r'\b(?:probability|chance|likelihood|estimate[ds]?)\b',phrase,re.I):continue
            local=horizons(phrase);periods=local or shared
            if not (metric(phrase) and local or shared) or len(periods)!=1:continue
            sides={v.lower().rstrip('s') for v in re.findall(r'\b(?:calls?|puts?)\b',phrase,re.I)}
            if not pairs:
                bound=[r for r in refs if r['terms']['right'] in sides] if sides else previous
            identities={tuple(r['terms'].values()) for r in bound}
            if len(identities)!=1:continue
            identity=next(iter(identities))
            values=re.findall(r'(?<![\w.])([+-]?\d+(?:\.\d+)?)\s*(?:%|percent\b)',phrase,re.I)
            claim={'evidence':evidence(text,start,end),'horizon_seconds':next(iter(periods))}
            if re.search(r'\b(?:if|unless|provided|assuming|conditional|once|when)\b|\bnot\s+(?:be\s+)?profitable\b',phrase,re.I):
                claim['error']='Unconditional profitable-resale probability not established'
            elif len(values)!=1 or re.search(r'\d+(?:\.\d+)?\s*[-–]\s*\d+(?:\.\d+)?\s*(?:%|percent)|\b(?:between|at least|at most|greater than|less than|over|under)\s+\d',phrase,re.I):
                claim['error']='Probability is not one exact percentage'
            else:
                value=Decimal(values[0])
                if not 0<=value<=100:claim['error']='Probability outside 0..100 percent'
                else:claim['percent']=str(value)
            found.setdefault(identity,[]).append(claim)
    result={}
    for identity,claims in found.items():
        values={(Decimal(c['percent']),c['horizon_seconds']) for c in claims if 'percent' in c}
        if len(values)==1 and all('percent' in c for c in claims):
            percent,period=next(iter(values))
            result[identity]={'percent':str(percent),'horizon_seconds':period,'evidence':[c['evidence'] for c in claims]}
        else:
            result[identity]={'error':'Missing, qualified, or conflicting probability/horizon','horizon_seconds':None,'claims':claims,'evidence':[c['evidence'] for c in claims]}
    return result
