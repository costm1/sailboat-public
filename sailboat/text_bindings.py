"""Bounded source bindings shared by research capture and capability sorting.

No strategy, prices, authorization or brokerage actions are inferred here.
"""
import re

TICKER = r'(?:QQQ|SPY|SMH)'
PAIR = re.compile(r'\b(?:(?P<symbol>'+TICKER+r')\s+)?(?:0DTE\s+)?\$?(?P<strike>\d+(?:\.\d+)?)\s+(?:strike\s+)?(?P<right>calls?|puts?)\b', re.I)


def evidence(text, start, end):
    return {'start': start, 'end': end, 'text': text[start:end]}


def sentences(text):
    return list(re.finditer(r'.+?(?:[.!?](?=\s|$)|\n|$)', text, re.S))


def focus(text):
    """Explicit option subject wins over comparator mentions; conflicts stay unresolved."""
    matches = list(re.finditer(r'\b('+TICKER+r')\s+(?:0DTE\s+)?(?:options?|trade|calls?|puts?)\b', text, re.I))
    matches += [m for m in PAIR.finditer(text) if m['symbol']]
    symbols = {m['symbol'].upper() if 'symbol' in m.re.groupindex else m[1].upper() for m in matches}
    # A coordinated subject declares both underlyings, not just the last noun.
    coordinated=list(re.finditer(r'\b('+TICKER+r')\s+(?:and|or)\s+('+TICKER+r')\s+(?:0DTE\s+)?(?:options?|calls?|puts?)\b',text,re.I))
    for m in coordinated:symbols.update((m[1].upper(),m[2].upper()))
    matches+=coordinated
    if not symbols:
        matches = list(re.finditer(r'\b('+TICKER+r')\s+is\s+(?:at\s+|about\s+|approximately\s+)*\$?\d', text, re.I))
        symbols = {m[1].upper() for m in matches}
    if not symbols:
        matches = list(re.finditer(r'\b'+TICKER+r'\b', text, re.I))
        symbols = {m[0].upper() for m in matches}
    return (next(iter(symbols)) if len(symbols) == 1 else None,
            [evidence(text, m.start(), m.end()) for m in matches])


def local_symbol(text, start, default):
    """Only inherit when this clause supplies no conflicting explicit ticker."""
    clause = text[:start]
    boundary = max(clause.rfind('\n'), clause.rfind(';')) + 1
    for m in re.finditer(r'[.!?](?=\s|$)', clause):
        boundary = max(boundary, m.end())
    names = set(re.findall(r'\b'+TICKER+r'\b', clause[boundary:], re.I))
    return next(iter(names)).upper() if len(names) == 1 else None if names else default


def contracts(text):
    """Associate adjacent strike/right pairs. Preserve unpaired alternatives explicitly."""
    default, subject_evidence = focus(text)
    pairs = list(PAIR.finditer(text))
    rows = []
    for m in pairs:
        symbol = m['symbol'].upper() if m['symbol'] else local_symbol(text, m.start(), default)
        # A previous contract in the same clause may have its own symbol.
        # Do not let that symbol leak across an explicitly different local pair.
        rows.append({'symbol': symbol, 'right': m['right'].lower().rstrip('s'),
                     'strike': m['strike'], 'strike_alternatives': [],
                     'evidence': [evidence(text, m.start(), m.end())],
                     'identity_evidence': subject_evidence})
    alternatives = []
    alternative_evidence = []
    for m in re.finditer(r'\b(\d+(?:\.\d+)?)\s+or\s+(\d+(?:\.\d+)?)\s+strikes?\b', text, re.I):
        alternatives.extend([m[1], m[2]])
        alternative_evidence.append(evidence(text,m.start(),m.end()))
    declared = list(re.finditer(r'\b(?:strike\s*[:=]?\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)[- ]strike)\b', text, re.I))
    strikes = {m[1] or m[2] for m in declared}
    for side in ('call', 'put'):
        if any(r['right'] == side for r in rows):
            continue
        mentions = list(re.finditer(r'\b'+side+r's?\b', text, re.I))
        if not mentions:
            continue
        m = mentions[0]
        rows.append({'symbol': local_symbol(text, m.start(), default), 'right': side,
                     'strike': next(iter(strikes)) if len(strikes) == 1 and not alternatives else None,
                     'strike_alternatives': sorted(set(alternatives)),
                     'evidence': [evidence(text, m.start(), m.end())]+alternative_evidence+[evidence(text, d.start(), d.end()) for d in declared],
                     'identity_evidence': subject_evidence})
    return rows


def current_pass(text):
    """Recognize a present refusal; distinguish clearly conditional reconsideration."""
    found = None
    conflict = False
    for s in sentences(text):
        phrase = s[0].strip().lstrip('*').strip()
        conditional = bool(re.search(r'\b(?:if|unless|when|once|until|then|would change|reconsider|hypothetical|example|earlier|previously)\b', phrase, re.I))
        # Quoted opinions are not the author's present conclusion.
        if phrase.startswith(('"', '\u201c', '>')):
            continue
        refusal = re.search(r"\b(?:no worthwhile (?:(?:QQQ|0DTE)\s+)*(?:trade|opportunity)|no trade(?=\s*(?:[.!;]|currently\b|right now\b|now\b|recommended\b|$))|no (?:current |new )?candidate|preferred candidate\s*:\s*none|neither direction looks (?:especially )?attractive|I(?:['\u2019]d| would) pass|I would not buy|I(?:'m| am) not recommending a new QQQ call or put|I don't recommend a new QQQ call or put)\b", phrase, re.I)
        if refusal and not conditional:
            found = found or evidence(text, s.start(), s.end())
        choice = re.search(r"\b(?:I(?:['\u2019]d| would) (?:buy|consider|choose)|I (?:prefer|recommend))\b|preferred candidate\s*:\s*(?!none\b)\w|^\s*(?:buy|purchase)\b", phrase, re.I)
        if choice and not conditional:
            conflict = True
    return found, bool(found and conflict)
