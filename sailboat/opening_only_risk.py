"""Verify a zero-realized-PnL placeholder against a complete opening-only book."""
from datetime import datetime
from decimal import Decimal
from .market_hours import EASTERN
from .scheduler import timestamp


def verified_opening_only(positions, prior, option_orders, equity_orders, now):
    """No sales, overnight exposure, unknown fills, or inferred transaction costs.

    This proves there has been no realization, not that holdings have no losses.
    The existing risk ledger still marks every holding at its fresh bid.
    """
    def reject():
        raise ValueError('Missing realized gain: opening-only history does not reconcile')
    day=datetime.fromtimestamp(now,EASTERN).date()
    midnight=datetime.combine(day,datetime.min.time(),EASTERN).timestamp()
    if not positions or not prior or prior.get('day')!=day.isoformat():reject()
    if option_orders is None or equity_orders is None:reject()
    for p in positions+prior.get('positions',[]):
        if p.get('kind')!='option' or not p.get('opened_at') or not midnight<=timestamp(p['opened_at'])<=now:reject()
    fills={};seen=set();execution_ids=set()
    for order in option_orders:
        qty=Decimal(str(order['processed_quantity']))
        if not qty.is_finite() or qty<0:reject()
        if qty==0:continue
        updated=order.get('updated_at')
        legs=order.get('legs') or []
        executions=[x for leg in legs for x in leg.get('executions') or []]
        recent=not updated or timestamp(updated)>=midnight or any(timestamp(x['timestamp'])>=midnight for x in executions)
        if not recent:continue
        if not order.get('id') or order['id'] in seen:reject()
        seen.add(order['id'])
        if len(legs)!=1 or order.get('direction')!='debit' or Decimal(str(order.get('trade_value_multiplier',0)))!=100:reject()
        leg=legs[0]
        if leg.get('side')!='buy' or leg.get('position_effect')!='open' or leg.get('ratio_quantity')!=1:reject()
        if not executions or sum(Decimal(str(x['quantity'])) for x in executions)!=qty:reject()
        identity=leg.get('option_id');amount=Decimal(0)
        for x in executions:
            n=Decimal(str(x['quantity']));price=Decimal(str(x['price']))
            if not x.get('id') or x['id'] in execution_ids:reject()
            execution_ids.add(x['id'])
            if not n.is_finite() or n<=0 or n!=n.to_integral_value() or not price.is_finite() or price<=0:reject()
            if not midnight<=timestamp(x['timestamp'])<=now or x.get('trade_date')!=day.isoformat():reject()
            amount+=n*price*100
        if Decimal(str(order['processed_premium']))!=amount:reject()
        old=fills.setdefault(identity,[Decimal(0),Decimal(0)]);old[0]+=qty;old[1]+=amount
    # An equity execution could realize PnL independently of the option book.
    for order in equity_orders:
        qty=Decimal(str(order['cumulative_quantity']))
        if not qty.is_finite() or qty<0:reject()
        if qty and (not order.get('last_transaction_at') or timestamp(order['last_transaction_at'])>=midnight or
                    any(timestamp(x['timestamp'])>=midnight for x in order.get('executions') or [])):reject()
    if len({p['id'] for p in positions})!=len(positions) or set(fills)!={p['id'] for p in positions}:reject()
    for p in positions:
        qty,cost=fills[p['id']]
        if qty!=Decimal(str(p['quantity'])) or Decimal(str(p['unit_cost']))*qty!=cost:reject()
    return Decimal(0),'broker_zero_total_verified_opening_only'
