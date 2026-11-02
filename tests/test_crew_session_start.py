"""Hooks resolve exact registered identity, never another role's inbox."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/crew'))
import crew_session_bridge as cb
import crew_session_start as hook
from test_crew_session_bridge import ipc, local, native_peer


def prepare(monkeypatch, root, bridge):
    monkeypatch.setattr(hook, 'in_project', lambda *_: True)
    monkeypatch.setattr(hook, 'SESSIONS_DIR', bridge.sessions_dir)
    monkeypatch.setattr(hook, 'codex_full_access', lambda: False)
    monkeypatch.setattr(hook, 'subprocess', SimpleNamespace(run=lambda *a, **k: SimpleNamespace(returncode=0),
                                                        SubprocessError=RuntimeError))


@pytest.mark.parametrize('role', cb.CLAUDE_ROLES)
def test_uppercase_claude_registry_resolves_canonical_inbox(local, monkeypatch, role):
    root, bridge = local
    prepare(monkeypatch, root, bridge)
    with ipc(root / 'native', native=True):
        native_peer(bridge, root / 'native', role.upper())
        peer = cb.read_json(bridge.sessions_dir / f'{os.getpid()}.json')
        assert hook.claude_role(bridge, {'session_id':peer['sessionId'], 'session_title':role.upper()}) == role
        with pytest.raises(cb.BridgeError, match='claude_role_conflict'):
            hook.claude_role(bridge, {'session_id':peer['sessionId'], 'session_title':'Beta' if role != 'Beta' else 'Alpha'})


def test_terminal_hook_keeps_exact_registered_identity(local, monkeypatch):
    root, bridge = local
    prepare(monkeypatch, root, bridge)
    thread = str(uuid.uuid4())
    bridge.register('Delta', None, thread)
    bridge.store('terminal-message', 'Alpha', 'Delta', 'terminal inbox')
    result = hook.session_start('codex', json.dumps({'cwd':str(root), 'session_id':thread}),
        state_dir=bridge.state_dir, environ={'CODEX_THREAD_ID':thread})
    text = result['hookSpecificOutput']['additionalContext']
    assert 'Role: Delta.' in text
    assert 'terminal inbox' in text


@pytest.mark.parametrize('role', ['Codex', 'Delta', 'Epsilon', 'Zeta'])
def test_hook_preview_only_registered_role(local, monkeypatch, role):
    root, bridge = local
    prepare(monkeypatch, root, bridge)
    thread = str(uuid.uuid4())
    with ipc(root / 'app'):
        bridge.register(role, str(root / 'app'), thread)
        for recipient in cb.ROLES:
            bridge.store(str(uuid.uuid4()), 'Alpha', recipient, f'private-{recipient}')
        result = hook.session_start('codex', json.dumps({'cwd':str(root), 'session_id':thread}),
            state_dir=bridge.state_dir, environ={'CODEX_THREAD_ID':thread, 'CODEX_APP_TOOLS_PIPE_PATH':str(root/'app')})
    text = result['hookSpecificOutput']['additionalContext']
    assert f'Role: {role}.' in text
    assert f'private-{role}' in text
    for other in cb.ROLES:
        if other != role:
            assert f'private-{other}' not in text
    assert 'delegated operator authority' in text
    assert 'Historical' in text


def test_unregistered_hook_reads_no_inbox_and_cannot_retarget(local, monkeypatch):
    root, bridge = local
    prepare(monkeypatch, root, bridge)
    owner = str(uuid.uuid4())
    with ipc(root / 'app'), ipc(root / 'other'):
        bridge.register('Codex', str(root / 'app'), owner)
        before = cb.read_json(bridge.state_dir / 'routes.json')
        bridge.store('secret', 'Alpha', 'Codex', 'do-not-leak')
        unknown = str(uuid.uuid4())
        result = hook.session_start('codex', json.dumps({'cwd':str(root), 'session_id':unknown}),
            state_dir=bridge.state_dir, environ={'CODEX_THREAD_ID':unknown,'CODEX_APP_TOOLS_PIPE_PATH':str(root/'other')})
        assert cb.read_json(bridge.state_dir / 'routes.json') == before
    text = result['hookSpecificOutput']['additionalContext']
    assert 'register --role' in text
    assert 'do-not-leak' not in text


def test_stale_refresh_generation_cannot_overwrite_takeover(local, monkeypatch):
    root, bridge = local
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    with ipc(root/'app'), ipc(root/'new'):
        bridge.register('Delta', str(root/'app'), old)
        def takeover(_):
            bridge.register('Delta', str(root/'new'), new, replace_thread=old)
        monkeypatch.setattr(hook, 'live_socket', takeover)
        with pytest.raises(cb.BridgeError):
            hook.refresh_codex(bridge, {'session_id':old}, {'CODEX_THREAD_ID':old, 'CODEX_APP_TOOLS_PIPE_PATH':str(root/'app')})
        assert bridge.context('Delta')['thread'] == new


def test_hook_payload_environment_thread_mismatch_refuses(local, monkeypatch):
    root, bridge = local
    with ipc(root/'app'):
        thread = str(uuid.uuid4())
        bridge.register('Zeta', str(root/'app'), thread)
        with pytest.raises(cb.BridgeError, match='thread_identity_mismatch'):
            hook.refresh_codex(bridge, {'session_id':thread}, {'CODEX_THREAD_ID':str(uuid.uuid4()),
                'CODEX_APP_TOOLS_PIPE_PATH':str(root/'app')})
