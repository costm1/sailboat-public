"""Official SDK subprocess: subscription login with explicitly permitted research tools."""
import asyncio,json,sys,time
from dataclasses import asdict
from claude_agent_sdk import ClaudeAgentOptions,query,ResultMessage,RateLimitEvent,SystemMessage,HookMatcher
from .agent_contract import provider_contract,constrained_schema
from .research_tools import ResearchPolicy
from .controller import canonical

def research_prompt(request,native):
    from .market_analyst import PROTOCOL as ANALYST_PROTOCOL
    analyst=request.get('agent_protocol')==ANALYST_PROTOCOL
    ending='analysis' if analyst else 'decision'
    if analyst:
        # The base prompt and response schema are already supplied by the SDK.
        text=canonical({k:v for k,v in request.items() if k not in ('instructions','response_schema')})
        return text+'\nReturn your final analysis in the supplied response schema. '+('Use native research tools as needed; leave market_reads empty in the final response.' if native else 'Use market_reads for intermediate research requests if needed.')
    text=canonical(request)
    text+='\nUse read-only research tools in whatever sequence improves your market assessment. Read results before drawing conclusions; further research is optional. Distinguish evidence you have not researched from unavailable evidence. Explain the evidence supporting your final '+ending+'.'
    if native:text+='\nNative market-data tools and permitted web research are available. Use them directly as needed before your final answer. Leave market_reads empty in the final response. Structured output constrains only the final answer, not the research process.'
    return text

async def run():
    data=json.loads(sys.stdin.readline());started=time.perf_counter();rates=[];init={};result=None
    prompt,schema=provider_contract(data['request'])
    research=data.get('research',{'builtins':[],'servers':{},'schemas':{},'max_calls':2})
    native=data.get('native_market_tools',False)
    if native:
        from .market_research import sdk_market_server
        from .agent_contract import READS
        from .live_broker import CONTRACTS
        research['servers']['sailboat_market']=sdk_market_server()
        research['schemas'].update({f'mcp__sailboat_market__{n}':CONTRACTS[n]['input'] for n in READS})
    policy=ResearchPolicy(research)
    options=ClaudeAgentOptions(tools=research['builtins'],mcp_servers=research['servers'],
        allowed_tools=list(research['schemas']),strict_mcp_config=True,setting_sources=[],
        hooks={'PreToolUse':[HookMatcher(hooks=[policy.before])],
               'PostToolUse':[HookMatcher(hooks=[policy.after])],
               'PostToolUseFailure':[HookMatcher(hooks=[policy.after])]},
        permission_mode='dontAsk',max_turns=12,model=data['model'],
        output_format={'type':'json_schema','schema':constrained_schema(schema)},
        system_prompt=prompt+policy.instructions(compact=data['request'].get('agent_protocol')=='qqq_options_analyst_v1') if research['schemas'] else prompt,
        extra_args={'no-session-persistence':None})
    user_prompt=research_prompt(data['request'],native)
    async for message in query(prompt=user_prompt,options=options):
        if isinstance(message,SystemMessage) and message.subtype=='init':
            init={k:message.data.get(k) for k in ('model','apiKeySource','tools','mcp_servers')}
        if isinstance(message,RateLimitEvent):rates.append(asdict(message.rate_limit_info))
        if isinstance(message,ResultMessage):result=message
    if result is None or result.is_error or result.structured_output is None:
        print(canonical({'error':'no_valid_structured_result','subtype':getattr(result,'subtype',None)}));return
    if any(r.get('rate_limit_type')=='overage' for r in rates):
        print(canonical({'error':'unexpected_overage_signal'}));return
    print(canonical({'response':result.structured_output,'usage':{
        'provider':'claude_subscription','model':init.get('model'),'api_key_source':init.get('apiKeySource'),
        'tools':init.get('tools'),'mcp_servers':init.get('mcp_servers'),
        'research_allowlist':list(research['schemas']),'research_audit':policy.audit,'duration_seconds':time.perf_counter()-started,'api_duration_ms':result.duration_api_ms,
        'token_usage':result.usage,'estimated_api_equivalent_usd':result.total_cost_usd,
        'billing':'Claude subscription allowance; API-equivalent estimate is not an invoice',
        'rate_limits':rates,'num_turns':result.num_turns}}))
if __name__=='__main__':
    try:asyncio.run(asyncio.wait_for(run(),85))
    except Exception as exc:print(canonical({'error':type(exc).__name__}))
