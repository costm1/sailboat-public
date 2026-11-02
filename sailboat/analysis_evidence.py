"""Attributed research only. No order authority, strategy, forecasts or broker calls."""
import hashlib,re
from datetime import datetime,timezone
from .market_hours import EASTERN
from .text_bindings import focus as bind_focus,contracts as bind_contracts,current_pass

VERSION='attributed-research-v3-local-forecasts'
NUMBER=r'-?\d+(?:\.\d+)?'

def extract_research(text,at):
 def ev(start,end):return {'start':start,'end':end,'text':text[start:end]}
 def span(m):return ev(m.start(),m.end())
 def item(kind,value,e,**extra):
  return {'kind':kind,'value':value,'evidence':e,'verification':'SOURCE_REPORTED','execution_authority':False,**extra}
 sentences=list(re.finditer(r'.+?(?:[.!?](?=\s|$)|\n|$)',text,re.S))
 # Bind a document focus only to an explicit instrument/trade phrase. Comparison
 # tickers in market commentary do not become competing option underlyings.
 symbol,focus_e=bind_focus(text)
 dates=list(re.finditer(r'\bexpir(?:ing|es|y|ation)\s*(?:today\s*\(?\s*)?(20\d\d-\d\d-\d\d)',text,re.I))
 zero=list(re.finditer(r'\b0DTE\b|\btoday[’\x27]s\s+(?:calls?|puts?|options?)\b|\bexpir(?:ing|es|ation)\s+(?:is\s+)?today\b',text,re.I));date_values={m[1] for m in dates}
 if zero:date_values.add(datetime.fromtimestamp(at,EASTERN).date().isoformat())
 expiry=next(iter(date_values)) if len(date_values)==1 else None
 if expiry:
  try:datetime.strptime(expiry,'%Y-%m-%d')
  except ValueError:expiry=None
 expiry_e=[span(m) for m in dates+zero]
 quote_times=list(re.finditer(r'\bquoted at\s+(\d{1,2}):(\d{2})Z\b',text,re.I))
 quote_time=None
 if len(quote_times)==1:
  m=quote_times[0]
  try:
   day=datetime.fromtimestamp(at,timezone.utc)
   quote_time={'reported':m[0],'utc':day.replace(hour=int(m[1]),minute=int(m[2]),second=0,microsecond=0).isoformat(),'evidence':[span(m)],'verification':'SOURCE_REPORTED','date_rule':'UTC date of the source message; not a refreshed broker timestamp'}
  except ValueError:pass
 contracts={};observations=[];forecasts=[];preferences=[];scenarios=[];proposals=[]
 for s in sentences:
  phrase=s[0];e=[span(s)]
  spot=re.search(r'\b([A-Z]{1,5}) is (?:trading )?(?:(?:at|near) )?(?:about )?\$?(\d+(?:\.\d+)?)',phrase)
  if spot:observations.append(item('reported_underlying_price',{'symbol':spot[1],'price':spot[2]},e))
  for direction in re.finditer(r'\b(?:bullish|bearish)\b',phrase,re.I):
   observations.append(item('reported_market_direction',direction[0].lower(),e))
  band=re.search(r'\brange of (?:roughly |about )?\$?(\d+(?:\.\d+)?) to \$?(\d+(?:\.\d+)?)',phrase,re.I)
  if band:observations.append(item('reported_underlying_range',{'symbol':symbol,'low':band[1],'high':band[2]},e))
  if re.search(r'\b(?:range|chop|momentum|SPY|SMH|no (?:clear )?(?:edge|breakout)|theta|gamma)\b',phrase,re.I):
   observations.append(item('market_commentary',phrase.strip(),e))
  if (re.search(r'\b(?:would|could|might|plausible|expected|chance of profit)\b',phrase,re.I)
      or (re.search(r'\b(?:probability|chance|likelihood|estimate[ds]?)\b',phrase,re.I)
          and re.search(r'\d\s*(?:%|percent\b)',phrase,re.I))):
   forecasts.append(item('agent_estimate',phrase.strip(),e))
 pass_e,pass_conflict=current_pass(text)
 if pass_e:preferences.append(item('current_preference','CONFLICTING' if pass_conflict else 'PASS',[pass_e]))
 refs=bind_contracts(text)
 for ref in refs:
  # Generic call/put mentions alone are observations, not resolved contracts.
  if ref['strike'] is None and not ref['strike_alternatives']:continue
  right=ref['right'];strike=ref['strike'];bound=ref['symbol'];key=(bound,right,strike,expiry)
  row=contracts.setdefault(key,{'symbol':bound,'right':right,'strike':strike,'expiry':expiry,'reported_bid':None,'reported_ask':None,'reported_delta':None,'expiration_breakeven':None,'evidence':[],'verification':'SOURCE_REPORTED','quote_freshness':'NOT_VERIFIED','quote_time':quote_time,'identity_status':'UNRESOLVED' if not bound or not expiry or not strike else 'NAMED_NOT_BROKER_RESOLVED','execution_authority':False})
  if ref['strike_alternatives']:row['strike_alternatives']=ref['strike_alternatives']
  row['evidence']+=ref['evidence'];row['identity_evidence']=ref['identity_evidence']+expiry_e
  pos=ref['evidence'][0]['start']
  line_start=text.rfind('\n',0,pos)+1;line_end=text.find('\n',pos)
  if line_end<0:line_end=len(text)
  line=text[line_start:line_end]
  # A mixed-contract sentence may give each exact contract its own immediate
  # parenthesized quote. Require all local references to have their own group;
  # "call and put (bid ... / ask ...)" remains shared and unresolved.
  if (len(set(v.lower().rstrip('s') for v in re.findall(r'\b(?:calls?|puts?)\b',line,re.I)))!=1
      or len({(v['right'],v['strike']) for v in refs if line_start<=v['evidence'][0]['start']<line_end})!=1):
   sentence=next((s for s in sentences if s.start()<=pos<s.end()),None)
   if sentence is None:continue
   local=[v for v in refs if sentence.start()<=v['evidence'][0]['start']<sentence.end()]
   groups=[]
   for v in local:
    end=v['evidence'][0]['end']
    m=re.match(r'\s*(?:expir(?:ing|es|y|ation)\s*[:=]?\s*20\d\d-\d\d-\d\d\s*)?\(([^()\n]*)\)',text[end:sentence.end()],re.I)
    if not m or re.search(r'\b(?:calls?|puts?)\b',m[1],re.I):break
    groups.append((v,end+m.start(1),end+m.end(1)))
   if len(groups)!=len(local):continue
   bound=next(((start,end) for v,start,end in groups if v is ref),None)
   if bound is None:continue
   line_start,line_end=bound;line=text[line_start:line_end]
  row['evidence'].append(ev(line_start,line_end));phrase=line
  for field,word in [('reported_bid','bid'),('reported_ask','ask')]:
   values=re.findall(r'\$?('+NUMBER+r')\s+'+word+r'\b',phrase,re.I)+re.findall(r'\b'+word+r'\s+\$?('+NUMBER+r')',phrase,re.I)
   vals=set(values)
   if len(vals)==1:
    value=next(iter(vals))
    if row[field] not in (None,value):row[field]=None;row['quote_freshness']='CONFLICTING_SOURCE_QUOTES'
    elif row['quote_freshness']!='CONFLICTING_SOURCE_QUOTES':row[field]=value
  delta=re.search(r'\bdelta(?: is)?(?: about)?\s+('+NUMBER+r')',phrase,re.I)
  if delta:row['reported_delta']=delta[1]
  be=re.search(r'\bbreak[- ]?even(?: is)?\s+\$?('+NUMBER+r')',phrase,re.I)
  if be:row['expiration_breakeven']=be[1]
 # Explicit premium expectations are source claims, never executable quotes.
 # Bind only a unique locally named contract, or the document's single contract.
 for s in sentences:
  phrase=s[0]
  targets=list(re.finditer(r'\b(?:target resale bid|resale (?:premium|bid)|premium target)(?:\s+(?:is|of|at|about|around|to))*\s*\$?(\d+(?:\.\d+)?)(?:\s*[-–]\s*\$?(\d+(?:\.\d+)?))?',phrase,re.I))
  reported=re.search(r'\b(?:its |option )?premium is\s*\$?(\d+(?:\.\d+)?)',phrase,re.I)
  if not targets and not reported:continue
  sides=set(v.lower().rstrip('s') for v in re.findall(r'\b(?:calls?|puts?)\b',phrase,re.I))
  local=[r for r in refs if any(s.start()<=e['start']<s.end() for e in r['evidence'])]
  matching=[r for r in contracts.values() if (len(local)==1 and r['symbol']==local[0]['symbol'] and r['right']==local[0]['right'] and r['strike']==local[0]['strike'])]
  if not matching and len(sides)==1:matching=[r for r in contracts.values() if r['right'] in sides]
  if not matching and len(contracts)==1:matching=list(contracts.values())
  binding={k:matching[0][k] for k in ('symbol','right','strike','expiry')} if len(matching)==1 else None
  if reported:observations.append(item('reported_option_premium',reported[1],[span(s)],contract=binding))
  for m in targets:forecasts.append(item('forecast_resale_premium',{'low':m[1],'high':m[2] or m[1]},[span(s)],contract=binding,confidence='SOURCE_UNSPECIFIED'))
 # Breakout thresholds with a locally explicit side, not expiration breakevens.
 for s in sentences:
  phrase=s[0]
  for m in re.finditer(r'\b(above|below)\s+(?:about\s+)?\$?(\d+(?:\.\d+)?)(?:\s*\((call|put)\))?',phrase,re.I):
   sides=set(v.lower().rstrip('s') for v in re.findall(r'\b(?:calls?|puts?)\b',phrase,re.I))
   side=m[3].lower() if m[3] else next(iter(sides)) if len(sides)==1 else None
   if not side:continue
   matching=[v for v in contracts.values() if v['right']==side]
   candidates=[{'symbol':v['symbol'],'right':v['right'],'strike':v['strike'],'expiry':v['expiry']} for v in matching]
   unresolved=[]
   if re.search(r'\bclean break\b',phrase,re.I):unresolved.append('Define clean break confirmation; no duration was invented.')
   if re.search(r'confirmed by SPY and SMH|SPY and SMH confirming',phrase,re.I):unresolved.append('Define the SPY/SMH confirmation criteria.')
   if re.search(r'rising volume',phrase,re.I):unresolved.append('Define the volume metric, comparison window and threshold.')
   if not symbol:unresolved.append('Identify the underlying for the condition.')
   if len(candidates)!=1:unresolved.append('Choose an unambiguous contract for this scenario.')
   scenarios.append(item('conditional_setup',{'symbol':symbol,'right':side,'operator':'gt' if m[1].lower()=='above' else 'lt','threshold':m[2]},[span(s)],contracts=candidates,confirmation_text=phrase.strip(),unresolved=unresolved,status='NOT_EVALUATED',monitor_installed=False))
 patterns=[('profit_target_percent',r'\btarget of\s+(?:about\s+)?\+?(\d+(?:\.\d+)?)%',lambda m:m[1]),
  ('loss_exit_percent',r'\bcut at\s+(?:about\s+)?-(\d+(?:\.\d+)?)%',lambda m:m[1]),
  ('max_hold_minutes',r'\bafter\s+(\d+)\s*(?:to|[-–])\s*(\d+)\s+minutes',lambda m:[int(m[1]),int(m[2])]),
  ('quantity',r'\b(\d+)\s+contracts?\b',lambda m:int(m[1])),
  ('sellout_time',r'\bsellout time is\s+(\d{1,2}:\d{2}Z)',lambda m:m[1])]
 for kind,pattern,fn in patterns:
  for m in re.finditer(pattern,text,re.I):proposals.append(item(kind,fn(m),[span(m)],status='ATTRIBUTED_ONLY',owner_policy_override=False))
 for name in ('theta','gamma'):
  for m in re.finditer(r'\b'+name+r'(?: is| of)?(?: about| approximately)?\s+('+NUMBER+r')(\s+per day)?',text,re.I):
   observations.append(item('reported_'+name,{'number':m[1],'units':'per day' if m[2] else 'UNSPECIFIED','contract_binding':'UNSPECIFIED'},[span(m)]))
 return {'version':VERSION,'status':'CAPTURED','source_sha256':hashlib.sha256(text.encode()).hexdigest(),'preferences':preferences,'contracts':list(contracts.values()),'observations':observations,'forecasts':forecasts,'scenarios':scenarios,'proposed_parameters':proposals,'execution_authority':False,'outcome_tracking':'NOT_STARTED','limitations':['Bounded supported language extraction, not general language understanding.','All prices, Greeks, forecasts and deadlines remain unverified source claims.','No trading conditions, monitoring jobs, targets, stops or orders are installed by this record.']}
