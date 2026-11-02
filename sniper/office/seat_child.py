"""One wake of one seat, inside the harness sandbox: no native tools, one MCP server, the host socket. Stdlib plus the SDK.

Reads a JSON request on stdin, prints one JSON event per line. Everything printed is untrusted at the host."""
import asyncio
import json
import sys

sys.path.insert(0,'/opt/release')
from sniper.office import seat_forward,claude_seat_auth

CLI='/opt/runtime/lib/python3.%d/site-packages/claude_agent_sdk/_bundled/claude'%sys.version_info[1]
BRIDGE=r"""
import select,socket,sys,os
s=socket.socket(socket.AF_UNIX);s.connect('/opt/host.sock')
while True:
    ready,_,_=select.select([0,s],[],[])
    if 0 in ready:
        data=os.read(0,65536)
        if not data:break
        s.sendall(data)
    if s in ready:
        data=s.recv(65536)
        if not data:break
        os.write(1,data)
"""


def emit(kind,**fields):
    print(json.dumps(dict(kind=kind,**fields)),flush=True)


async def run(doc):
    from claude_agent_sdk import AssistantMessage,ClaudeAgentOptions,ResultMessage,__version__ as sdk_version,query
    options=ClaudeAgentOptions(cli_path=CLI,model=doc['model'],tools=[],setting_sources=[],strict_mcp_config=True,
        mcp_servers={'office':{'type':'stdio','command':sys.executable,'args':['-c',BRIDGE]}},allowed_tools=['mcp__office__*'],
        permission_mode='bypassPermissions',system_prompt=doc['system'],resume=doc.get('session_id'),effort=doc.get('effort','medium'),
        env=dict(seat_forward.start(),HOME='/opt/workspace'),max_turns=doc['max_turns'])
    async for message in query(prompt=doc['prompt'],options=options):
        if type(message).__name__=='SystemMessage' and getattr(message,'subtype',None)=='init':
            # First, so a wake killed partway can still be resumed.
            emit('session',session_id=(getattr(message,'data',None) or {}).get('session_id'))
        if isinstance(message,AssistantMessage):
            if claude_seat_auth.message_reason(message):emit('error',reason='claude_auth_failed')
            emit('request_usage',usage=getattr(message,'usage',None),message_id=getattr(message,'message_id',None))
            for block in message.content:
                name=type(block).__name__
                if name=='ThinkingBlock':emit('thought',text=block.thinking[:4000])
                elif name=='TextBlock':emit('thought',text=block.text[:4000])
        elif isinstance(message,ResultMessage):
            if claude_seat_auth.message_reason(message):emit('error',reason='claude_auth_failed')
            emit('result',sdk_version=sdk_version,session_id=message.session_id,result_id=getattr(message,'uuid',None),usage=message.usage,num_turns=getattr(message,'num_turns',None),is_error=message.is_error)


if __name__=='__main__':
    request=json.loads(sys.stdin.readline())
    try:asyncio.run(asyncio.wait_for(run(request),timeout=request['deadline_s']))
    except TimeoutError:
        emit('error',reason='timeout');sys.exit(3)
    except Exception as exc:
        emit('error',reason=claude_seat_auth.exception_reason(exc) or type(exc).__name__);sys.exit(4)
