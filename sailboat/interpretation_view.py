"""Read-only source extraction and actual downstream outcomes for the owner UI."""
import re
from decimal import Decimal, InvalidOperation
from .shape_sorter import sort_text
from .preferred_candidate import candidate_pieces
from .analyst_preferences import sizing_text

def interpretation(state,item):
    rid=item['request_id'];key='scheduled:'+rid
    shaped=state.get('shape_recent',{}).get(key,{})
    entry=state.get('candidate_messages',{}).get(key,{}).get('result') or shaped.get('candidate_entry') or {}
    if item.get('source')=='Plugin submission':
        row=state.get('chat_messages',{}).get(rid,{})
        key=((row.get('result') or {}).get('shadow_sorter') or {}).get('receipt_id')
        shaped=state.get('shape_recent',{}).get(key,{})
        entry=state.get('candidate_messages',{}).get(key,{}).get('result') or shaped.get('candidate_entry') or {}
    text=item.get('reasoning') or '';at=item.get('received_at') or 0
    parsed=shaped.get('sorting')
    if not isinstance(parsed,dict) or not all(k in parsed for k in ('original_text','blocks','research_evidence')):parsed=None
    if not parsed and text.strip():
        try:parsed=sort_text(text,at)
        except (ValueError,TypeError,KeyError):parsed=None
    context=(state.get('shape_jobs',{}).get(key) or {}).get('domain_context')
    if context and parsed:parsed=dict(parsed,trading_domain_context=context)
    rows=[];seen=set()
    for piece in candidate_pieces(parsed) if parsed else []:
        terms=piece['terms'];identity=tuple(terms[k] for k in ('symbol','expiry','strike','right'))
        if identity in seen:continue
        seen.add(identity)
        probability=piece.get('probability') or {}
        source=(piece.get('economics') or {}).get('contract') or {}
        ask=source.get('reported_ask')
        if ask is None:
            # Only explicitly stated ask prices within the exact contract's
            # section; conflicting values remain unavailable, never guessed.
            bound=sizing_text(text,terms,at)
            asks=set(re.findall(r'\bask(?:\s+price)?\s*(?:is\s+|of\s+|[:=]\s*)?\$?(\d+(?:\.\d+)?)',bound,re.I))
            if len(asks)==1:ask=asks.pop()
        cost=None
        try:
            amount=Decimal(str(ask))
            if amount.is_finite() and amount>0:cost=str(amount*100)
        except (InvalidOperation,ValueError):pass
        rows.append(dict(terms,probability=probability.get('percent'),horizon_seconds=probability.get('horizon_seconds'),
            probability_error=probability.get('error'),source_ask=ask,source_cost_per_contract=cost,
            selected=terms==entry.get('candidate')))
    receipt=item.get('receipt') or {};note=state.get('received_analyses',{}).get(rid,{})
    processing=note.get('processing_result') or {}
    reason=receipt.get('reason') or receipt.get('blocker')
    if processing.get('error')=='TimeoutError' and not receipt.get('order_status'):
        reason=('Assessment processing timed out. Check the receipt for any downstream outcome.'
            if entry or state.get('shape_jobs',{}).get(key) or item.get('held_position_blocks') else
            'Assessment timed out before candidate processing. No entry was queued from this analysis.')
    return {'candidates':rows,'selection_confirmed':bool(entry.get('candidate')),
        'extraction_stage':'ASSEMBLY_PROCESSED' if entry else 'SOURCE_EXTRACTED',
        'quantity':entry.get('quantity'),'sizing':entry.get('sizing'),
        'broker_quote':entry.get('quote'),'entry_limit':entry.get('limit_price'),
        'status':receipt.get('order_status') or receipt.get('candidate_status') or receipt.get('status'),
        'reason':reason,'source_at':at,'processing_error':processing.get('error')}
