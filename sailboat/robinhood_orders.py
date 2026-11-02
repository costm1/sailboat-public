"""Narrow Robinhood option writer, separate from the immutable read-only adapter.

Connected only through the verified risk/context bridge and owner runtime
authorization. Agent credentials cannot arm or bypass the permit.
"""
import asyncio, json
from decimal import Decimal
from jsonschema import validate
from .robinhood_readonly import ENDPOINT
from .order_execution import broker_arguments

LIVE_BLOCKERS=[
 'Live account bridge is not configured in this service.',
 'Live decision/context bridge and owner account/limits configuration are not installed.',
 'Broker review/place response and lost-response recovery need supported non-submitting fixtures or a separately authorized live test.',
 'Provider permission for the eventual real-money workflow remains to be confirmed.'
]

def data(result):
    if result.get('isError'):raise ValueError('broker returned an error; preserve uncertain order outcome')
    value=result.get('structuredContent')
    if value is None:
        text=[x['text'] for x in result.get('content',[]) if x.get('type')=='text']
        if len(text)!=1:raise ValueError('unsupported broker response')
        value=json.loads(text[0])
    if not isinstance(value,dict) or not isinstance(value.get('data'),dict):raise ValueError('missing broker data')
    return value['data']

class RobinhoodOptionTransport:
    """Actual official MCP argument transport; caller MUST supply a live permit.

    A permit is a synchronous host-owned callback, never a field in agent output.
    LiveBroker supplies it only for persisted owned attempts under the owner gate.
    """
    def __init__(self,readonly,permit):self.readonly=readonly;self.permit=permit
    async def call(self,name,args):
        if name not in {'review_option_order','place_option_order','cancel_option_order','decline_trade_approval'}:raise ValueError('unsupported order tool')
        # Independent from controller mode: both layers must authorize the operation.
        if self.permit(name,args) is not True:raise ValueError('DISARMED: brokerage mutation pathway blocked')
        return data(await self.readonly._call(name,args,permit=self.permit))
    async def review(self,p,a):
        args=broker_arguments(p,a);args.pop('ref_id')
        args.update(chain_symbol=p['symbol'],underlying_type='equity')
        # Review normalization is intentionally strict; never silently suppress alerts.
        v=await self.call('review_option_order',args)
        if 'order_checks' not in v or 'customer_approval_required' not in v:raise ValueError('unverified review response')
        expected=broker_arguments(p,a);expected.pop('ref_id')
        for field in ['account_number','type','legs']:
            if v.get(field)!=expected[field]:raise ValueError('review terms do not match requested order')
        if Decimal(v['quantity'])!=Decimal(expected['quantity']) or Decimal(v['price'])!=Decimal(expected['price']):raise ValueError('review quantity/price mismatch')
        for field in ['time_in_force','market_hours']:
            if field in v and v[field]!=expected[field]:raise ValueError('review session mismatch')
        if p['order_type']=='stop_limit' and Decimal(v.get('stop_price'))!=Decimal(p['stop_price']):raise ValueError('review stop mismatch')
        return {'approved':not v['order_checks'],'alerts':v['order_checks'],'customer_approval_required':v['customer_approval_required'],'fees':v['fees'].get('total_fee') if isinstance(v.get('fees'),dict) else None}
    async def place(self,p,a):
        v=await self.call('place_option_order',broker_arguments(p,a))
        if v.get('approval'):
            return {'approval_id':v['approval']['approval_id']}
        order=v.get('order')
        if not isinstance(order,dict) or not isinstance(order.get('id'),str):raise ValueError('missing placement order ID; retry only same ref_id')
        return {'order_id':order['id']}
    async def cancel(self,p,a):
        return await self.call('cancel_option_order',{'account_number':p['account_number'],'order_id':a['order_id']})

async def reconcile_known_order(readonly,p,a):
    """Actual read-only known-ID recovery. Missing ref_id is NOT guessed from terms."""
    if not a.get('order_id'):
        return {'complete':False,'order':None,'reason':'Broker list omits ref_id; unknown submission identity requires recovery.'}
    obs=await readonly.read('get_option_orders',{'account_number':p['account_number'],'order_id':a['order_id']},persist=False)
    v=data(obs['result'])
    orders=v.get('orders')
    if not isinstance(orders,list) or len(orders)!=1 or v.get('next'):return {'complete':False,'order':None}
    o=orders[0];legs=o['legs']
    if o['id']!=a['order_id'] or len(legs)!=1 or legs[0]['ratio_quantity']!=1 or legs[0]['position_effect']!=p['position_effect']:raise ValueError('order identity/leg mismatch')
    actual_type={('limit','immediate'):'limit',('limit','stop'):'stop_limit',('market','immediate'):'market',('market','stop'):'stop_market'}.get((o['type'],o['trigger']))
    if actual_type!=p['order_type'] or o['time_in_force']!='gfd' or o['market_hours']!='regular_hours':raise ValueError('order terms mismatch')
    if p['order_type']=='stop_limit' and Decimal(o['stop_price'])!=Decimal(p['stop_price']):raise ValueError('stop mismatch')
    quantity=Decimal(o['quantity']);filled=Decimal(o['processed_quantity'])
    if quantity!=quantity.to_integral_value() or filled!=filled.to_integral_value():raise ValueError('fractional contract quantity')
    return {'complete':True,'order':{'id':o['id'],'ref_id':a['ref_id'],'state':o['state'],
      'filled':int(filled),'quantity':int(quantity),'option_id':legs[0]['option_id'],'side':legs[0]['side'],'price':o['price']}}

class PreparedLiveControls:
    """Owner-visible readiness, never a switch that bypasses unfinished live checks."""
    def __init__(self,service):self.service=service
    def status(self):
        return {'mode':'DISARMED','live_ready':False,'order_types':['limit','stop_limit'],
          'broker_write_calls_enabled':False,'blockers':LIVE_BLOCKERS,
          'implementation':'durable order worker tested against simulated broker; production decision routing remains simulated'}
    def disarm(self):
        self.service.scheduler_transaction(lambda c,s:c.set_mode('DISARMED'))
        return self.status()
    async def arm(self):raise ValueError('LIVE_NOT_READY: '+ '; '.join(LIVE_BLOCKERS))
