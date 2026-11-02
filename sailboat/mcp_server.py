"""Small stdio MCP adapter over the existing model-free AgentLink."""
from pathlib import Path
import argparse,asyncio,json
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from .agent_link import AgentLink
from .structured_schema import BODY_SCHEMA,obj,ID
from .controller import canonical
ROOT=Path(__file__).resolve().parents[1]
SKILL=Path(__file__).with_name('controller_skill.md')
REQUEST_SCHEMA=obj({"operator_request":{"type":"string","minLength":1,"maxLength":6000}})
SUBMIT_SCHEMA=obj({"request_id":ID,"context_hash":{"type":"string","pattern":"^[a-f0-9]{64}$"},"decision":BODY_SCHEMA})
RECEIPT_SCHEMA=obj({"request_id":ID})

def create_server(db,source="external-agent",conversation="default",client_label="mcp"):
    server=Server("sailboat",version="1.0.0",instructions=(
      "Simulation-only Sailboat controller. Read sailboat://skill or follow the instructions returned by "
      "sailboat_get_context. Submit only an authorized structured decision. No live orders, arming, "
      "quote writing, shell or model inference tools. Same server-configured identity shares existing plans."))
    @server.list_tools()
    async def list_tools():
        return [
          types.Tool(name="sailboat_get_context",description="Get fresh owned simulated portfolio/plans, market snapshots, hard limits, full decision schema and a 120-second request. Evaluates existing conditions; no new decision is inferred.",
            inputSchema=REQUEST_SCHEMA,annotations=types.ToolAnnotations(readOnlyHint=False,destructiveHint=False,idempotentHint=False,openWorldHint=False)),
          types.Tool(name="sailboat_submit_decision",description="Validate and apply one structured decision to the simulated controller. Supply every decision field. Use current IDs/versions and explicit critical values. Exact retry returns the saved receipt.",
            inputSchema=SUBMIT_SCHEMA,annotations=types.ToolAnnotations(readOnlyHint=False,destructiveHint=True,idempotentHint=True,openWorldHint=False)),
          types.Tool(name="sailboat_get_receipt",description="Recover the durable result for an owned request. No trade or new decision.",
            inputSchema=RECEIPT_SCHEMA,annotations=types.ToolAnnotations(readOnlyHint=True,destructiveHint=False,idempotentHint=True,openWorldHint=False))]
    @server.list_resources()
    async def resources():
        return [types.Resource(uri="sailboat://skill",name="Sailboat Skill",description="Provider-independent workflow and decision rules.",mimeType="text/markdown")]
    @server.read_resource()
    async def read_resource(uri):
        if str(uri)!="sailboat://skill":raise ValueError("unknown resource")
        return SKILL.read_text(encoding="utf-8")
    @server.call_tool()
    async def call_tool(name,arguments):
        # Each synchronous call finishes its transaction before returning to the client.
        # Neither a connected client nor agent generation holds a SQLite write lock.
        link=AgentLink(db,source,conversation)
        try:
            if name=="sailboat_get_context":result=link.request(arguments["operator_request"],plan_ttl=3600)
            elif name=="sailboat_submit_decision":result=link.respond(canonical(arguments))
            elif name=="sailboat_get_receipt":result=link.receipt(arguments["request_id"])
            else:raise ValueError("unknown tool")
            with link.transaction():
                link._event("MCP_CALL",{"client_label":client_label,"tool":name,"request_id":result.get("request_id",arguments.get("request_id"))})
            rejected=result.get("status")=="REJECTED" or result.get("result",{}).get("status")=="REJECTED"
            return types.CallToolResult(content=[types.TextContent(type="text",text=canonical(result))],structuredContent=result,isError=rejected)
        finally:link.close()
    return server

async def run(db,source,conversation,client_label):
    server=create_server(db,source,conversation,client_label)
    async with stdio_server() as (read,write):
        await server.run(read,write,server.create_initialization_options())

def main():
    p=argparse.ArgumentParser(description="SAILBOAT simulation-only stdio MCP server")
    p.add_argument("--db",type=Path,default=ROOT/"state/structured.sqlite")
    p.add_argument("--source",default="external-agent")
    p.add_argument("--conversation",default="default")
    p.add_argument("--client-label",default="mcp")
    a=p.parse_args();asyncio.run(run(a.db.resolve(),a.source,a.conversation,a.client_label))
if __name__=="__main__":main()
