"""Owner-approved 5% entry ceiling; existing worker owns every order/replacement."""
from decimal import Decimal,ROUND_FLOOR

def ceiling(original,ticks):
    maximum=Decimal(original)*Decimal('1.05')
    tick=Decimal(ticks['below_tick'] if maximum<Decimal(ticks['cutoff_price']) else ticks['above_tick'])
    value=(maximum/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
    # Across a tick-size boundary, select a valid price on the lower side.
    tick=Decimal(ticks['below_tick'] if value<Decimal(ticks['cutoff_price']) else ticks['above_tick'])
    return format((value/tick).to_integral_value(rounding=ROUND_FLOOR)*tick,'f')

def fresh_price(policy,quote,option_id,now):
    if not quote or quote.get('id')!=option_id or not 0<=now-quote.get('asof',0)<=30:
        raise ValueError('Fresh exact option quote required for repricing')
    ask=Decimal(quote['ask']);bid=Decimal(quote['bid']);cap=Decimal(policy['ceiling'])
    if not ask.is_finite() or not bid.is_finite() or ask<=0 or bid<0 or bid>ask:raise ValueError('Invalid option quote')
    ticks=policy['ticks'];tick=Decimal(ticks['below_tick'] if ask<Decimal(ticks['cutoff_price']) else ticks['above_tick'])
    if ask%tick:raise ValueError('Ask is not a valid limit tick')
    return None if ask>cap else format(ask,'f')
