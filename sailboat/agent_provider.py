"""Selectable outside-agent runtimes. No controller tools or host secrets reach inference."""
import asyncio,json,os,sys,tempfile,time
from pathlib import Path
from cryptography.fernet import Fernet
from .controller import canonical

class AgentUnavailable(RuntimeError):pass

class ClaudeSubscription:
    name='claude_subscription'
    def __init__(self,service,encryption_key,model='sonnet',broker=None):
        self.service=service;self.cipher=Fernet(encryption_key.encode());self.model=model;self.broker=broker
    async def decide(self,request):
        import anyio
        from .research_tools import load_config
        research=load_config()
        encrypted=await anyio.to_thread.run_sync(self.service.private_read,'claude_subscription')
        if not encrypted:raise AgentUnavailable('official Claude subscription sign-in required')
        profile=json.loads(self.cipher.decrypt(encrypted.encode()))
        if profile['credentials'].get('claudeAiOauth',{}).get('subscriptionType') not in ('pro','max','team','enterprise'):
            raise AgentUnavailable('subscription authentication required')
        with tempfile.TemporaryDirectory(prefix='sailboat-agent-') as scratch:
            root=Path(scratch);conf=root/'config';conf.mkdir(mode=0o700);work=root/'work';work.mkdir()
            cred=conf/'.credentials.json';cred.write_text(canonical(profile['credentials']));cred.chmod(0o600)
            if profile.get('config'):(conf/'.claude.json').write_text(canonical(profile['config']))
            # An allowlist avoids inheriting API keys, Robinhood/HF secrets, proxies, or other CLI profiles.
            env={k:v for k,v in os.environ.items() if k in ('PATH','SystemRoot','WINDIR','TEMP','TMP','LANG','SSL_CERT_FILE')}
            env.update(HOME=str(root),USERPROFILE=str(root),CLAUDE_CONFIG_DIR=str(conf),
                CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',CLAUDE_CODE_DISABLE_AUTO_MEMORY='1',
                PYTHONPATH=str(Path(__file__).resolve().parents[1]),PYTHONIOENCODING='utf-8')
            p=await asyncio.create_subprocess_exec(sys.executable,'-m','sailboat.claude_worker',cwd=work,env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,limit=262144)
            from .market_research import MarketResearch,exchange,drain_stderr
            reader=MarketResearch(self.broker) if self.broker is not None else None
            stderr_task=asyncio.create_task(drain_stderr(p.stderr))
            try:
                result=await exchange(p,{'request':request,'model':self.model,'research':research,'native_market_tools':reader is not None},reader)
                p.stdin.close();await p.wait()
            finally:
                if p.returncode is None:
                    import psutil
                    try:children=psutil.Process(p.pid).children(recursive=True)
                    except psutil.NoSuchProcess:children=[]
                    for child in children:
                        try:child.terminate()
                        except psutil.NoSuchProcess:pass
                    p.terminate()
                    try:await asyncio.wait_for(p.wait(),5)
                    except asyncio.TimeoutError:p.kill();await p.wait()
                stderr_task.cancel()
                await asyncio.gather(stderr_task,return_exceptions=True)
                # Persist CLI-managed refresh rotation before any controller decision acknowledgment.
                fresh=json.loads(cred.read_text())
                refreshed=fresh!=profile['credentials']
                if refreshed:
                    profile['credentials']=fresh
                    sealed=self.cipher.encrypt(canonical(profile).encode()).decode()
                    await anyio.to_thread.run_sync(self.service.private_write,'claude_subscription',sealed)
            if p.returncode:raise AgentUnavailable('Claude worker unavailable')
            if result.get('error'):raise AgentUnavailable(result['error'])
            result['usage']['credential_refresh_persisted']=refreshed
            result['usage']['market_results']=reader.results if reader else []
            return result['response'],result['usage']

class HTTPSAgent:
    """Owner-supplied independent endpoint; never enabled or billed by default."""
    name='https_agent'
    def __init__(self,url,token=None):
        from urllib.parse import urlparse
        p=urlparse(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password:raise ValueError('HTTPS agent URL required')
        self.url,self.token=url,token
    async def decide(self,request):
        import httpx
        headers={'Authorization':'Bearer '+self.token} if self.token else {}
        async with httpx.AsyncClient(timeout=90,follow_redirects=False) as client:
            r=await client.post(self.url,json=request,headers=headers);r.raise_for_status()
            if len(r.content)>20000:raise AgentUnavailable('oversized response')
            return r.json(),{'provider':self.name,'billing':'owner endpoint; not inferred'}
