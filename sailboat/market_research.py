"""Read-only research bridge. Credentials and broker execution stay in the host."""
import asyncio,json,time
from .agent_contract import READS
from .live_broker import CONTRACTS
from .controller import canonical
from jsonschema import validate

class MarketResearch:
 def __init__(self,broker):self.broker=broker;self.calls=0;self.results=[]
 async def read(self,name,args):
  if name not in READS:raise ValueError('outside market research allowlist')
  if self.calls>=6:raise ValueError('market research call budget exhausted')
  self.calls+=1
  validate(args,CONTRACTS[name]['input'])
  if len(canonical(args))>4000:raise ValueError('oversized research arguments')
  if name=='get_option_quotes' and (not args['instrument_ids'] or len(args['instrument_ids'])>20):raise ValueError('request 1 to 20 option IDs')
  try:
   observation=await self.broker.read(name,args,persist=False)
   if observation['result'].get('isError'):raise ValueError('broker read failed')
   from .robinhood_orders import data
   raw=data(observation['result']);truncated=False
   for key in ('instruments','results','chains','historicals'):
    if isinstance(raw.get(key),list) and len(raw[key])>25:raw[key]=raw[key][:25];truncated=True
   item={'tool':name,'arguments':args,'retrieved_at':observation['retrieved_at'],'source':observation['source'],
    'data':raw,'truncated':truncated,'complete':not truncated and not bool(raw.get('next'))}
   if len(canonical(item))>60000:raise ValueError('result too large; narrow research request')
  except Exception as exc:
   item={'tool':name,'arguments':args,'retrieved_at':time.time(),'data':{},'error':type(exc).__name__,
    'complete':False,'truncated':False,'note':'Research unavailable; narrow or correct the request if useful. Never infer absent data.'}
  self.results.append(item)
  return item

async def exchange(process,payload,reader):
 """Private parent/worker pipes carry bounded tool reads; never broker credentials."""
 process.stdin.write((canonical(payload)+'\n').encode());await process.stdin.drain()
 while True:
  line=await process.stdout.readline()
  if not line or len(line)>262144:raise ValueError('missing or oversized worker result')
  message=json.loads(line)
  if message.get('type')!='market_read':return message
  try:
   if reader is None:raise ValueError('market research unavailable')
   result=await reader.read(message['tool'],message['arguments'])
  except Exception as exc:result={'error':type(exc).__name__,'note':'Research request rejected; no action performed.'}
  process.stdin.write((canonical({'call_id':message['call_id'],'result':result})+'\n').encode());await process.stdin.drain()

async def drain_stderr(stream):
 while await stream.read(8192):pass


def sdk_market_server():
 from claude_agent_sdk import SdkMcpTool,create_sdk_mcp_server
 from mcp.types import ToolAnnotations
 import sys,uuid
 lock=asyncio.Lock()
 def make(name):
  async def handler(args):
   async with lock:
    call_id=uuid.uuid4().hex
    print(canonical({'type':'market_read','call_id':call_id,'tool':name,'arguments':args}),flush=True)
    line=await asyncio.to_thread(sys.stdin.readline);answer=json.loads(line)
    if answer.get('call_id')!=call_id:raise ValueError('research response mismatch')
    item=answer['result']
    return {'content':[{'type':'text','text':canonical(item)}],'isError':bool(item.get('error'))}
  descriptions={
   'get_equity_quotes':'Read current underlying bid/ask and last prices.',
   'get_equity_historicals':'Read historical underlying OHLCV bars for a requested date range.',
   'get_option_chains':'Discover an underlying option chain and its real available expirations.',
   'get_option_instruments':'Find exact option contract UUIDs using returned chain/expiry and narrow strike/type filters. No order is created.',
   'get_option_quotes':'Read current option bid/ask, quote timestamp, IV and Greeks for observed contract UUIDs.',
   'get_option_historicals':'Read historical option bars for observed contract UUIDs; not an implied-volatility history feed.'}
  return SdkMcpTool(name,descriptions[name],CONTRACTS[name]['input'],handler,ToolAnnotations(readOnlyHint=True,destructiveHint=False))
 return create_sdk_mcp_server('sailboat_market',tools=[make(n) for n in READS])
