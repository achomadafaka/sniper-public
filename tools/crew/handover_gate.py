"""Pure decision for a trusted PreToolUse adapter; no hooks are installed here.

The host owns state and the exact pinned writer/status/ACK allowlist. Generic
shell and code execution must never be entries in that allowlist.
"""
import json

GENERIC_TOOLS={'shell','bash','exec','functions.exec','exec_command','python','code','code_mode'}


def allow_tool(state, *, session_id, incarnation, tool, arguments, allowlist):
    try:
        if (not isinstance(state,dict) or state.get('session_id')!=session_id
                or state.get('incarnation')!=incarnation or not session_id or not incarnation):return False
        if state.get('phase')!='note_pending':return False
        if type(state.get('failed_note_turns')) is not int or not 0<=state['failed_note_turns']<2:return False
        if not isinstance(tool,str) or tool in GENERIC_TOOLS or not isinstance(arguments,dict) or not isinstance(allowlist,list):return False
        packet=dict(tool=tool,arguments=arguments)
        raw=json.dumps(packet,sort_keys=True,allow_nan=False)
        if len(raw.encode())>16384:return False
        return any(packet==entry for entry in allowlist)
    except (ValueError,TypeError):return False


def note_turn_finished(state, *, valid):
    result=dict(state)
    if state.get('phase')!='note_pending':return result
    count=state.get('failed_note_turns')
    if type(count) is not int or count<0 or type(valid) is not bool:
        return dict(result,phase='parked',needs_page=True,reason='note_state_unknown')
    if valid:return dict(result,phase='note_ready')
    count+=1
    return dict(result,failed_note_turns=count,phase='parked' if count>=2 else 'note_pending',needs_page=count>=2)
