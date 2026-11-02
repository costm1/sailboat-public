"""Bounded, attributed prose fields. Scores never grant execution authority."""
import re
from decimal import Decimal, ROUND_FLOOR

MIN_ENTRY_PROBABILITY = Decimal('40')

def sizing_text(text,terms,at):
    """Bind bullet sizing only inside an explicit, uninterrupted contract section."""
    from .held_analysis import binding_text
    from .shape_sorter import sort_text
    from .text_bindings import PAIR
    selected=[];active=False
    for line in text.splitlines():
        if not line.strip():active=False;continue
        parsed=binding_text(line)
        refs=[b['terms'] for b in sort_text(parsed,at)['blocks'] if b['kind']=='contract_reference']
        exact=bool(refs) and all(t['symbol']==terms['symbol'] and t['right']==terms['right'] and
            t['strike'] is not None and Decimal(str(t['strike']))==Decimal(str(terms['strike'])) and
            t['expiry']==terms['expiry'] for t in refs)
        if exact:
            active=True;selected.append(line)
        elif PAIR.search(parsed):
            # Even an incomplete or conflicting next contract closes the section.
            active=False
        elif active and re.match(r'^\s*(?:[-*+]\s|\d+[.)]\s|\s{2,}\S)',line):
            selected.append(line)
        else:active=False
    return '\n'.join(selected)

def held_preferences(text):
    """Display only. A preference is not a calibrated probability or instruction."""
    pairs=list(re.finditer(r'\bPreference:\s*HOLD\s+(\d+(?:\.\d+)?)%\s*[,;/]\s*SELL\s+(\d+(?:\.\d+)?)%',text,re.I))
    result={'preference':None,'forecast':None}
    if len(pairs)==1:
        m=pairs[0];h,s=map(Decimal,m.groups())
        if 0<=h<=100 and 0<=s<=100 and h+s==100:
            result['preference']={'hold':float(h),'sell':float(s),'evidence':m[0]}
    forecasts=list(re.finditer(r'\bForecast:\s*(\d+(?:\.\d+)?)%\s+chance that waiting (?:produces|yields) a better executable (?:sale price|bid) (?:over|within) the next (\d+) minutes',text,re.I))
    if len(forecasts)==1:
        m=forecasts[0];p=Decimal(m[1]);minutes=int(m[2])
        if 0<=p<=100 and 0<minutes<=60:result['forecast']={'percent':float(p),'minutes':minutes,'evidence':m[0]}
    return result

def entry_size(text,terms,at,ask,buying_power,limits):
    """Only size text explicitly bound to the selected full contract. No other candidate's size."""
    bound=sizing_text(text,terms,at)
    # An explicit modest prose convention, not a brokerage protocol.
    pattern=r'\b(?:suggested|preferred|proposed) size:\s*(?:(\d+) contracts?|\$(\d+(?:\.\d+)?) budget)'
    matches=list(re.finditer(pattern,bound,re.I))
    all_sizes=list(re.finditer(pattern,text,re.I))
    if not matches and (all_sizes or re.search(r'\b(?:size|quantity|contracts? to buy)\s*:',text,re.I)):
        raise ValueError('Sizing is not unambiguously bound to the selected contract')
    if len(matches)>1:raise ValueError('Conflicting or repeated candidate sizing; fresh assessment required')
    unit=Decimal(ask)*100
    if not unit.is_finite() or unit<=0:raise ValueError('Invalid fresh entry premium')
    cap=min(int(limits['max_order_quantity']),int((min(Decimal(str(buying_power)),Decimal(str(limits['max_order_notional'])))/unit).to_integral_value(rounding=ROUND_FLOOR)))
    requested=1;budget=None
    if matches:
        m=matches[0]
        if m[1]:requested=int(m[1])
        else:
            budget=Decimal(m[2]);requested=int((budget/unit).to_integral_value(rounding=ROUND_FLOOR))
        # Do not quietly parse a range/alternative as its first number.
        tail=bound[m.end():].split('.')[0]
        if re.match(r'\s*(?:[-–]|or\b|to\b|if\b)',tail,re.I):raise ValueError('Ambiguous or conditional size')
    if requested<=0 or cap<=0:raise ValueError('Suggested size cannot afford one contract within owner limits')
    qty=min(requested,cap)
    return qty,{'requested_quantity':requested,'quantity':qty,'capped':qty<requested,
        'source':'analyst' if matches else 'existing_one_contract_default',
        'budget_usd':str(budget) if budget is not None else None,'evidence':matches[0][0] if matches else None}
