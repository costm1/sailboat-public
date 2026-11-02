"""Bounded clue extraction, not general natural-language understanding or authorization."""
import re,hashlib
VERSION='clues-v1'
def extract(text):
 if not isinstance(text,str) or not text.strip() or len(text)>16000:raise ValueError('bounded nonblank text required')
 fields=[];warnings=[]
 def add(field,value,m):
  fields.append({'field':field,'value':value,'evidence':{'start':m.start(),'end':m.end(),'text':text[m.start():m.end()]}})
 patterns=[('ticker',r'\b(?:QQQ|SPY|SMH)\b',lambda m:m[0]),
  ('option_side',r'\b(calls?|puts?)\b',lambda m:m[1].lower().rstrip('s')),
  ('expiry',r'\b20\d\d-\d\d-\d\d\b',lambda m:m[0]),
  ('expiry_reference',r'\b0DTE\b|\btoday(?:\x27s)?\b|\btomorrow\b',lambda m:m[0].lower()),
  ('strike',r'\b0DTE\s+\$?(\d+(?:\.\d+)?)\b',lambda m:float(m[1])),
  ('strike',r'\$?(\d+(?:\.\d+)?)\s+(?:strike\s+)?(?:calls?|puts?)\b',lambda m:float(m[1])),
  ('strike',r'\bstrike\s*(?:price\s*)?[:=]?\s*\$?(\d+(?:\.\d+)?)',lambda m:float(m[1])),
  ('threshold',r'\b(above|below|over|under)\s+(?:about\s+)?\$?(\d+(?:\.\d+)?)',lambda m:{'op':'gte' if m[1].lower() in ('above','over') else 'lte','price':float(m[2])}),
  ('observation',r'\b(?:bullish|bearish|momentum|drifting (?:lower|higher)|fading volume|mixed volume|volume[- ]backed|volume confirmation|no breakout|no directional edge|tight (?:range|chop)|stale|unverified|uncertain|missing)\b',lambda m:m[0].lower()),
  ('agent_decision',r"\b(?:I (?:wouldn['’]t|won['’]t|will not) (?:buy|sell|trade)|no trade(?: currently)?|observe|hold|I decline to trade|I would (?:buy|sell))\b",lambda m:m[0]),
  ('restriction',r"\b(?:don['’]t (?:buy|sell|trade)|do not (?:buy|sell|trade|submit)|never (?:buy|sell)|no orders|research[- ]only|hypothetical only)\b",lambda m:m[0]),
  ('reported_quote',r'\b(call|put)\s+(?:was\s+)?\$?(\d+(?:\.\d+)?)\s*/\s*\$?(\d+(?:\.\d+)?)',lambda m:{'right':m[1].lower(),'bid':float(m[2]),'ask':float(m[3])})]
 for field,pattern,fn in patterns:
  for m in re.finditer(pattern,text,re.I):add(field,fn(m),m)
 # Only directly bound side + price language is an extracted entry condition.
 for m in re.finditer(r'\b(calls?|puts?)\s+(?:if\s+(?:QQQ\s+)?(?:breaks?\s+)?|on\s+(?:a\s+)?(?:break\s+)?)?(above|below|over|under)\s+\$?(\d+(?:\.\d+)?)',text,re.I):
  prefix=text[max(0,m.start()-30):m.start()]
  if re.search(r'(?:not|never|avoid)\s+(?:buy\s+)?$',prefix,re.I):warnings.append('negated_condition');continue
  add('entry_condition',{'right':m[1].lower().rstrip('s'),'op':'gte' if m[2].lower() in ('above','over') else 'lte','price':float(m[3])},m)
 # Ambiguous targets are never guessed from a price alone.
 if not any(f['field']=='ticker' and f['value'].upper()=='QQQ' for f in fields):warnings.append('QQQ_not_explicit')
 if re.search(r'\b(?:SPY|SMH)\b[^.;\n]{0,25}\b(?:calls?|puts?)\b',text,re.I):warnings.append('option_underlying_ambiguous')
 if re.search(r'\b(?:SPY|SMH)\b[^.;\n]{0,50}\b(?:above|below|over|under)\b',text,re.I):warnings.append('threshold_underlying_ambiguous')
 if re.search(r'\b(?:unless|except|not until|either|not above|not below)\b',text,re.I):warnings.append('unsupported_conditional_language')
 if re.search(r'\b(?:tomorrow|next week)\b',text,re.I):warnings.append('non_0DTE_reference')
 if {'bullish','bearish'}<=set(f['value'] for f in fields if f['field']=='observation'):warnings.append('contradictory_directional_descriptions')
 for side in ('call','put'):
  cs={str(f['value']) for f in fields if f['field']=='entry_condition' and f['value']['right']==side}
  if len(cs)>1:warnings.append('conflicting_'+side+'_conditions')
 return {'version':VERSION,'original_text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
  'fields':fields,'warnings':sorted(set(warnings)),
  'agent_stated_decisions':[f for f in fields if f['field']=='agent_decision'],
  'limitations':'Pattern-supported clues only; absence of a matched refusal is not consent. Reported prices are never executable quotes.'}

def values(clues,field):return [f['value'] for f in clues['fields'] if f['field']==field]
