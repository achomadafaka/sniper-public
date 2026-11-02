"""Direct transports for an already isolated sibling child; never creates a nested jail."""
import asyncio
import json
import math
import os
import re
import unicodedata
import sys
import time
from pathlib import Path
import stat
from types import SimpleNamespace

from scripts.research import judge_gpt_replay as transport
from scripts import memecoin_desk_checker as checker
from sniper.office import model_diagnostics as diagnostics,claude_compat

MAX_BYTES = 1_048_576


def sandbox_refusal(status):
    try:
        values = dict(line.split(':', 1) for line in status.splitlines() if ':' in line)
        return None if int(values['NoNewPrivs']) == 1 and int(values['CapEff'].strip(), 16) == 0 else 'sandbox_required'
    except (KeyError, ValueError, TypeError):
        return 'sandbox_required'


def execute(adapter, doc, *, dispatch=None):
    try:
        if (adapter not in ('claude_sdk', 'codex_exec') or type(doc) is not dict
                or set(doc)-{'debug_stderr'} != {'system', 'user', 'model', 'deadline_s', 'schema'}
                or ('debug_stderr' in doc and (adapter!='claude_sdk' or type(doc['debug_stderr']) is not bool))
                or any(type(doc[k]) is not str or not doc[k] for k in ('system', 'user', 'model'))
                or type(doc['deadline_s']) not in (int, float) or not math.isfinite(doc['deadline_s'])
                or not 0 < doc['deadline_s'] <= 120 or type(doc['schema']) is not dict
                or doc['schema'].get('type') != 'object'
                or len(json.dumps(doc, allow_nan=False).encode()) > MAX_BYTES):
            return {'call_error': 'invalid_request'}
        if adapter=='claude_sdk':
            reason=claude_compat.startup_refusal()
            if reason:return {'call_error':reason}
            if dispatch is None:
                reason=claude_compat.activate_vendor()
                if reason:return {'call_error':reason}
        return (dispatch or (claude if adapter == 'claude_sdk' else codex))(doc)
    except (ValueError, TypeError):
        return {'call_error': 'invalid_request'}
    except Exception:
        return {'call_error': 'transport_error'}


def codex(doc):
    # The launcher owns the shared provider slot and host auth preparation/writeback.
    # Existing parser/timeout/process-group cleanup is retained; only the nested jail is omitted.
    auth_home = Path.home() / '.codex'
    pinned=False
    def prepare(tmp):
        nonlocal pinned
        home,copy=checker.prepare_codex_home(tmp,real_home=auth_home)
        if home is not None:
            config=Path(home)/'config.toml'
            payload='model = '+json.dumps(doc['model'])+'\n'
            with config.open('x') as target:target.write(payload)
            config.chmod(0o400)
            pinned=config.read_text()==payload and config.stat().st_mode & 0o222==0
        return home,copy
    direct = SimpleNamespace(
        isolation=lambda *a: [],
        prepare_codex_home=prepare,
        sync_auth_back=lambda copy: checker.sync_auth_back(copy, real_home=auth_home, page=lambda _: None),
        minimal_env=checker.minimal_env)
    prompt = doc['system'] + '\n\nReturn only a JSON object matching the supplied schema. Do not use tools.\n' + doc['user']
    text, error, _, meta = transport._codex_json_locked(
        direct, prompt, timeout_s=doc['deadline_s'], schema_obj=doc['schema'], model=doc['model'],
        effort='low', codex_path='/opt/codex/codex', page=lambda _: None)
    return dict(text=text, call_error=error, usage=meta.get('usage'), model_echo=meta.get('model_echo'),
                served_model_basis='requested_pinned' if pinned and error is None and meta.get('model_echo') is None else 'unknown',
                session_id=meta.get('session_id'), result_id=meta.get('result_id'), billing_basis='subscription_flat',
                provider_error_kind=diagnostics.kind(meta.get('provider_error_kind')) if error else None)


_ESCAPE=re.compile(r'\\(?:u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|[\\"/nrtbf])')


def _canon(value):
    work=0
    def decode(match):
        token=match.group()[1:]
        if token[0] in 'uU':
            number=int(token[1:],16)
            return chr(number) if number<=0x10ffff else token
        return {'n':'\n','r':'\r','t':'\t','b':'\b','f':'\f'}.get(token,token)
    while True:
        work+=len(value)
        if work>8*MAX_BYTES:return None
        decoded=_ESCAPE.sub(decode,value)
        try:decoded=decoded.encode('utf-16','surrogatepass').decode('utf-16')
        except UnicodeError:return None
        if decoded==value:break
        value=decoded
    return ''.join(c for c in unicodedata.normalize('NFKC',value).casefold() if c.isalnum())

def _prompt_material(doc):
    pending=[doc['system'],doc['user']];seen=set();fragments=set();work=0
    decoder=json.JSONDecoder()
    while pending:
        value=pending.pop()
        if isinstance(value,dict):pending.extend(value.values());continue
        if isinstance(value,list):pending.extend(value);continue
        if not isinstance(value,str) or value in seen:continue
        seen.add(value);work+=len(value)
        if work>8*MAX_BYTES:return None  # Unknown material suppresses every debug line.
        for part in [value,*value.splitlines()]:
            part=_canon(part)
            if part is None:return None
            if len(part)>=8:
                fragments.add(part)
                if len(part)>32:fragments.update(part[i:i+32] for i in range(len(part)-31))
                if len(fragments)>32768:return None
        try:
            pending.append(json.loads(value));continue
        except json.JSONDecodeError:pass
        except RecursionError:return None
        # Host snapshots can be JSON embedded inside a quoted-data wrapper.
        at=0
        while at<len(value):
            if value[at] not in '{[':at+=1;continue
            try:parsed,end=decoder.raw_decode(value,at)
            except json.JSONDecodeError:at+=1;continue
            except RecursionError:return None
            pending.append(parsed);at=end
    return fragments


def _claude_stderr(doc):
    remaining=4096
    begun=time.monotonic()
    prompts=(doc['system'],doc['user'],json.dumps(doc['system']),json.dumps(doc['user']))
    material=_prompt_material(doc)
    def emit(line):
        nonlocal remaining
        # Indented payload/continuation lines can contain split prompts or folded headers.
        if remaining<=0 or not isinstance(line,str):return
        canonical=_canon(line)
        if material is None or canonical is None or any(fragment in canonical for fragment in material):
            line='[prompt line suppressed]'
        elif line[:1].isspace():return
        text=diagnostics.detail(line,prompts=prompts)
        if not text:return
        raw=('[claude-debug +%.3fs] %s\n'%(time.monotonic()-begun,text)).encode()
        marker=b'[claude-debug budget exhausted]\n'
        if len(raw)+len(marker)>remaining:
            raw=raw[:max(0,remaining-len(marker))].decode('utf-8',errors='ignore').encode()+marker
            remaining=0
        else:remaining-=len(raw)
        sys.stderr.write(raw.decode());sys.stderr.flush()
    return emit


def claude_options(doc):
    from claude_agent_sdk import ClaudeAgentOptions
    debug=doc.get('debug_stderr',True)
    extra={'no-session-persistence':None,'print':None}
    if debug:extra['debug-to-stderr']=None
    return ClaudeAgentOptions(cli_path=str(claude_compat.bundled_cli()),model=doc['model'], tools=[], setting_sources=[], mcp_servers={},
                              strict_mcp_config=True, max_turns=1, effort='low', system_prompt=doc['system'],
                              output_format={'type':'json_schema','schema':doc['schema']},
                              extra_args=extra,stderr=_claude_stderr(doc) if debug else None)


def claude(doc):
    from claude_agent_sdk import query, AssistantMessage, ResultMessage
    out = dict(text=None, model_echo=None, call_error='empty', billing_basis='subscription_flat')
    prompt = (doc['user'] + '\n\nReturn only a JSON object matching the supplied schema. Do not use tools. '
              'Represent host broker requests only in the JSON requests array; never invoke them as native tools.')
    async def run():
        async for message in query(prompt=prompt, options=claude_options(doc)):
            if isinstance(message, AssistantMessage):
                out['model_echo'] = message.model
                if any((getattr(block, 'type', None) == 'tool_use' or type(block).__name__ == 'ToolUseBlock')
                       and getattr(block,'name',None)!='StructuredOutput' for block in message.content):
                    raise ValueError('tool_event')
            elif isinstance(message, ResultMessage):
                structured = getattr(message, 'structured_output', None)
                text = json.dumps(structured, allow_nan=False) if isinstance(structured, dict) else None
                out.update(text=text if not message.is_error and isinstance(text, str) else None,
                           call_error='provider_error' if message.is_error else 'structured_output_missing' if not isinstance(structured,dict) else None,
                           session_id=message.session_id, result_id=getattr(message, 'uuid', None),
                           usage=message.usage)
                if message.is_error:
                    out['provider_error_detail']=diagnostics.detail(message.result,prompts=(doc['system'],doc['user']))
    try:
        asyncio.run(asyncio.wait_for(run(), timeout=doc['deadline_s']))
    except TimeoutError:
        return {'call_error': 'timeout'}
    except ValueError as exc:
        return {'call_error':'tool_event' if str(exc)=='tool_event' else 'invalid_result'}
    return out


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--adapter', choices=('claude_sdk', 'codex_exec'), required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if args.request != '/opt/scratch/request.json' or args.output != '/opt/scratch/result.json':
        return 2
    if sandbox_refusal(Path('/proc/self/status').read_text()) is not None:
        return 2
    fd = os.open(args.request, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return 2
        with os.fdopen(fd, 'rb', closefd=False) as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return 2
        doc = json.loads(raw)
    finally:
        os.close(fd)
    result = execute(args.adapter, doc)
    encoded = json.dumps(result, allow_nan=False).encode()
    if len(encoded) > MAX_BYTES:
        encoded = b'{"call_error":"output_too_large"}'
    import tempfile
    fd, stage = tempfile.mkstemp(dir='/opt/scratch', prefix='.result-')
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(encoded)
        os.replace(stage, args.output)
    finally:
        Path(stage).unlink(missing_ok=True)
    return 0
