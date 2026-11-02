"""Offline transport checks; every socket belongs to this test process."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import uuid

import pytest

CREW = Path(__file__).resolve().parents[1] / 'tools/crew'
sys.path.insert(0, str(CREW))
import crew_session_bridge as cb

GPT = ('Codex', 'Delta', 'Epsilon', 'Zeta')
CLAUDE = ('Alpha', 'Beta', 'Lambda', 'Gamma')
ROLES = CLAUDE + GPT


@contextmanager
def ipc(path, *, native=False, ambiguous=False):
    """Real local Unix IPC with bounded accept/read and visible received requests."""
    calls = []
    errors = []
    stop = threading.Event()
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(16)
    listener.settimeout(.1)
    def run():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with conn:
                    conn.settimeout(1)
                    if native:
                        data = b''
                        while True:
                            chunk = conn.recv(131072)
                            if not chunk:
                                break
                            data += chunk
                        if data:
                            calls.append([json.loads(line) for line in data.splitlines()])
                            conn.sendall(b'{}\n')
                    else:
                        head = conn.recv(4)
                        if not head:  # registration's live credential probe
                            continue
                        size = struct.unpack('<I', head)[0]
                        request = json.loads(cb.exact(conn, size))
                        calls.append(request)
                        if ambiguous:
                            continue
                        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {
                            'success': True, 'contentItems': []}}).encode()
                        conn.sendall(struct.pack('<I', len(body)) + body)
            except Exception as exc:
                errors.append(exc)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield calls
    finally:
        stop.set()
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert not errors


@pytest.fixture
def local():
    with tempfile.TemporaryDirectory(prefix='crew-') as directory:
        root = Path(directory)
        root.chmod(0o700)
        bridge = cb.Bridge(root / 'state', sessions_dir=root / 'sessions')
        cb.private_dir(bridge.sessions_dir)
        yield root, bridge


def native_peer(bridge, path, name='Alpha'):
    pid = os.getpid()
    cb.write_json(bridge.sessions_dir / f'{pid}.json', {
        'name': name, 'pid': pid, 'procStart': cb.process_start(pid), 'peerProtocol': 1,
        'sessionId': str(uuid.uuid4()), 'messagingSocketPath': str(path)})
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
    cb.write_json(bridge.sessions_dir / f'{pid}.{digest}.key', {'peerToken': 'a' * 32})


def daemon(bridge, paths):
    cb.write_json(bridge.state_dir / 'daemon.json', {'pid': os.getpid(),
        'procStart': cb.process_start(os.getpid()), 'replies': {
            role: f'uds:{path}' for role, path in paths.items()}})


def test_exact_roster():
    assert cb.ROLES == ROLES


@pytest.mark.parametrize('role', CLAUDE)
def test_uppercase_native_role_is_canonical(local, role):
    root, bridge = local
    with ipc(root / 'native', native=True):
        native_peer(bridge, root / 'native', role.upper())
        assert bridge.peer(name=role)['name'] == role
        assert bridge.peer(pid=os.getpid())['name'] == role


def test_case_variants_do_not_hide_duplicate_peers(local):
    root, bridge = local
    with ipc(root / 'native', native=True):
        native_peer(bridge, root / 'native', 'ALPHA')
        other_pid = os.getppid()
        cb.write_json(bridge.sessions_dir / f'{other_pid}.json', {
            'name': 'Alpha', 'pid': other_pid, 'procStart': cb.process_start(other_pid), 'peerProtocol': 1,
            'sessionId': str(uuid.uuid4()), 'messagingSocketPath': str(root / 'native')})
        with pytest.raises(cb.BridgeError, match='ambiguous_peer'):
            bridge.peer(name='Alpha')


@pytest.mark.parametrize('role', GPT)
def test_terminal_registration_and_queue(local, monkeypatch, role):
    root, bridge = local
    thread = str(uuid.uuid4())
    route = bridge.register(role, None, thread)
    assert route['pipe'] is None
    assert bridge.register(role, None, thread) == route
    with pytest.raises(cb.BridgeError, match='role_already_registered'):
        bridge.register(role, None, str(uuid.uuid4()))
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(cb.subprocess, 'run', run)
    row = bridge.send('Alpha', role, 'terminal round trip')
    assert row['delivery_status'] == 'codex_queued'
    assert calls[0][0][:4] == ['codex', 'queue', '--thread', thread]
    assert 'terminal round trip' in calls[0][0][-1]
    assert calls[0][1]['timeout'] > 0
    assert bridge.read(role)[0]['body'] == 'terminal round trip'


@pytest.mark.parametrize('failure', ['rejected', 'timeout', 'missing'])
def test_terminal_queue_never_fabricates_success(local, monkeypatch, failure):
    _, bridge = local
    bridge.register('Delta', None, str(uuid.uuid4()))
    def run(*args, **kwargs):
        if failure == 'timeout':
            raise cb.subprocess.TimeoutExpired('codex', 5)
        if failure == 'missing':
            raise FileNotFoundError('codex')
        return type('Result', (), {'returncode': 1})()
    monkeypatch.setattr(cb.subprocess, 'run', run)
    row = bridge.send('Alpha', 'Delta', 'keep this message')
    assert row['delivery_status'] == ('delivery_unknown' if failure == 'timeout' else 'delivery_failed')
    assert bridge.read('Delta')[0]['body'] == 'keep this message'


def test_terminal_register_command_without_app_environment(local, monkeypatch, capsys):
    _, bridge = local
    thread = str(uuid.uuid4())
    monkeypatch.setenv('CODEX_THREAD_ID', thread)
    monkeypatch.delenv('CODEX_APP_TOOLS_PIPE_PATH', raising=False)
    monkeypatch.setattr(sys, 'argv', ['sniper-crew', '--state-dir', str(bridge.state_dir),
                                     'register', '--role', 'Codex'])
    assert cb.main() == 0
    assert json.loads(capsys.readouterr().out)['thread'] == thread
    assert bridge.context()['pipe'] is None


@pytest.mark.parametrize('role', ['Codex', 'Zeta'])
def test_terminal_daemon_context_accepts_registration(local, role):
    _, bridge = local
    thread = str(uuid.uuid4())
    cb.write_json(bridge.state_dir / 'context.json', {
        'pipe': None, 'thread': thread, 'reply': 'uds:/fixture', 'from_mode': 'prompting'})
    assert bridge.context()['pipe'] is None
    route = bridge.register(role, None, thread if role == 'Codex' else str(uuid.uuid4()))
    assert route['pipe'] is None


@pytest.mark.parametrize('change', [
    {'pipe': ''}, {'pipe': 0}, {'thread': ''}, {'thread': 'invalid'}, {'reply': ''}, {'reply': None},
])
def test_terminal_daemon_context_rejects_invalid_fields(local, change):
    _, bridge = local
    value = {'pipe': None, 'thread': str(uuid.uuid4()), 'reply': 'uds:/fixture'}
    cb.write_json(bridge.state_dir / 'context.json', dict(value, **change))
    with pytest.raises(cb.BridgeError):
        bridge.context()


def test_terminal_daemon_context_requires_pipe_key(local):
    _, bridge = local
    cb.write_json(bridge.state_dir / 'context.json', {'thread': str(uuid.uuid4()), 'reply': 'uds:/fixture'})
    with pytest.raises(cb.BridgeError, match='invalid_codex_context'):
        bridge.context()


def test_registration_cas_and_one_to_one(local):
    root, bridge = local
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with ipc(root / 'app'):
        route = bridge.register('Delta', str(root / 'app'), a)
        assert route['role'] == 'Delta'
        assert bridge.register('Delta', str(root / 'app'), a) == route
        with pytest.raises(cb.BridgeError, match='role_already_registered'):
            bridge.register('Delta', str(root / 'app'), b)
        with pytest.raises(cb.BridgeError, match='thread_already_registered'):
            bridge.register('Zeta', str(root / 'app'), a)
        replaced = bridge.register('Delta', str(root / 'app'), b, replace_thread=a)
        assert replaced['generation'] == route['generation'] + 1
        with pytest.raises(cb.BridgeError, match='registration_changed'):
            bridge.register('Delta', str(root / 'app'), a, replace_thread=a)
        assert bridge.context('Delta')['thread'] == b


def test_simultaneous_register_has_one_winner(local):
    root, bridge = local
    with ipc(root / 'app'):
        def register(thread):
            try:
                return bridge.register('Zeta', str(root / 'app'), thread)['thread']
            except cb.BridgeError as exc:
                return str(exc)
        threads = [str(uuid.uuid4()), str(uuid.uuid4())]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, threads))
        assert results.count('role_already_registered') == 1
        assert bridge.context('Zeta')['thread'] in threads


@pytest.mark.parametrize('role,thread', [('Alpha', '11111111-1111-4111-8111-111111111111'), ('Delta', 'not-uuid')])
def test_register_refuses_bad_identity(local, role, thread):
    root, bridge = local
    with ipc(root / 'app'), pytest.raises(cb.BridgeError):
        bridge.register(role, str(root / 'app'), thread)


def test_register_refuses_offline_or_unsafe_socket(local):
    root, bridge = local
    with pytest.raises((cb.BridgeError, OSError)):
        bridge.register('Delta', str(root / 'absent'), str(uuid.uuid4()))
    with ipc(root / 'app'):
        (root / 'app').chmod(0o666)
        with pytest.raises(cb.BridgeError, match='unsafe_permissions'):
            bridge.register('Delta', str(root / 'app'), str(uuid.uuid4()))
    assert not (bridge.state_dir / 'routes.json').exists()


def test_legacy_codex_migration_is_explicit(local):
    root, bridge = local
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    legacy = {'thread': old, 'pipe': str(root / 'app'), 'reply': 'uds:/old', 'from_mode': 'bypass'}
    cb.write_json(bridge.state_dir / 'context.json', legacy)
    assert bridge.context()['thread'] == old
    with ipc(root / 'app'):
        with pytest.raises(cb.BridgeError, match='role_already_registered'):
            bridge.register('Codex', str(root / 'app'), new)
        bridge.register('Codex', str(root / 'app'), new, replace_thread=old)
    assert cb.read_json(bridge.state_dir / 'context.json') == legacy
    assert bridge.context()['thread'] == new
    with pytest.raises(cb.BridgeError):
        bridge.context('Delta')


@pytest.mark.parametrize('sender,recipient', [(a,b) for a in ROLES for b in ROLES if a != b])
def test_all_56_directed_pairs(local, sender, recipient):
    root, bridge = local
    from contextlib import ExitStack
    with ExitStack() as stack:
        calls = {}
        threads = {}
        replies = {}
        for role in GPT:
            calls[role] = stack.enter_context(ipc(root / role))
            threads[role] = str(uuid.uuid4())
            bridge.register(role, str(root / role), threads[role])
            path = root / f'reply-{role}'
            stack.enter_context(ipc(path, native=True))
            replies[role] = path
        daemon(bridge, replies)
        native = stack.enter_context(ipc(root / 'native', native=True))
        if recipient in CLAUDE:
            native_peer(bridge, root / 'native', recipient)
        row = bridge.send(sender, recipient, f'{sender} -> {recipient}', message_id=str(uuid.uuid4()))
        assert row['delivery_status'] in ('codex_queued', 'transport_sent'), row
        assert [r['body'] for r in bridge.read(recipient)] == [f'{sender} -> {recipient}']
        for role in ROLES:
            if role != recipient:
                assert bridge.read(role) == []
        if recipient in GPT:
            assert calls[recipient][0]['params']['arguments']['threadId'] == threads[recipient]
            assert all(not items for role, items in calls.items() if role != recipient)
        else:
            assert native[0][1]['from'] == f'uds:{replies[sender if sender in GPT else "Codex"]}'


@pytest.mark.parametrize('role', GPT)
def test_native_reply_endpoint_is_authority_and_status_is_preserved(local, role):
    root, bridge = local
    with ipc(root / 'app') as calls, ipc(root / 'native', native=True):
        thread = str(uuid.uuid4())
        bridge.register(role, str(root / 'app'), thread)
        native_peer(bridge, root / 'native')
        original = str(uuid.uuid4())
        bridge.store(original, role, 'Alpha', 'hello')
        left, right = socket.socketpair()
        frames = [{'type':'auth','token':'secret'}, {'type':'control','action':'peer_message_status',
            'status':'expired', 'status_detail':'refused', 'orig_msg_id':original},
            {'type':'user','msgV':1,'msg_id':str(uuid.uuid4()),
             'message':{'content':'To Codex: prose does not set recipient'}}]
        right.sendall(b''.join(json.dumps(f).encode()+b'\n' for f in frames))
        right.shutdown(socket.SHUT_WR)
        try:
            bridge.handle(left, 'secret', role=role)
        finally:
            left.close()
            right.close()
        assert bridge.read('Alpha')[0]['native_status'] == 'refused'
        assert len(bridge.read(role)) == 1
        assert calls[0]['params']['threadId'] == thread


def test_ambiguous_send_is_durable_and_not_resent(local):
    root, bridge = local
    with ipc(root / 'app', ambiguous=True) as calls:
        bridge.register('Delta', str(root / 'app'), str(uuid.uuid4()))
        row = bridge.send('Alpha', 'Delta', 'once', message_id='same')
        assert row['delivery_status'] == 'delivery_unknown'
        assert bridge.send('Alpha', 'Delta', 'once', message_id='same') == row
        assert len(calls) == 1
        with pytest.raises(cb.BridgeError, match='message_id_conflict'):
            bridge.send('Alpha', 'Delta', 'different', message_id='same')


def test_offline_gpt_never_falls_back_to_codex(local):
    root, bridge = local
    with ipc(root / 'app') as calls:
        bridge.register('Codex', str(root / 'app'), str(uuid.uuid4()))
        row = bridge.send('Alpha', 'Zeta', 'offline')
        assert row['delivery_status'] == 'delivery_failed'
        assert not calls


def test_legacy_codex_thread_cannot_be_borrowed(local):
    root, bridge = local
    thread = str(uuid.uuid4())
    cb.write_json(bridge.state_dir / 'context.json', {'thread':thread, 'pipe':str(root/'app'),
        'reply':'uds:/legacy', 'from_mode':'prompting'})
    with ipc(root/'app'), pytest.raises(cb.BridgeError, match='thread_already_registered'):
        bridge.register('Delta', str(root/'app'), thread)


def test_role_reply_endpoints_are_not_aliased(local):
    root, bridge = local
    with ipc(root/'app'), ipc(root/'reply', native=True):
        for role in GPT:
            bridge.register(role, str(root/'app'), str(uuid.uuid4()))
        daemon(bridge, {role:root/'reply' for role in GPT})
        with pytest.raises(cb.BridgeError, match='bridge_not_serving'):
            bridge.reply_context('Delta')


def test_native_offline_peer_is_distinct_from_empty_inbox(local):
    root, bridge = local
    with ipc(root/'app'), ipc(root/'reply', native=True):
        bridge.register('Delta', str(root/'app'), str(uuid.uuid4()))
        daemon(bridge, {'Delta':root/'reply'})
        row = bridge.send('Delta', 'Alpha', 'offline')
        assert row['delivery_status'] == 'delivery_failed'
        assert row['error'] == 'peer_missing'
        assert bridge.read('Alpha')[0]['body'] == 'offline'


def test_one_daemon_dispatches_and_receives_all_gpt_replies(local):
    """Run only an isolated test daemon, never systemctl or the installed service."""
    from contextlib import ExitStack
    import subprocess
    root, bridge = local
    with ExitStack() as stack:
        calls = {}
        threads = {}
        for role in GPT:
            calls[role] = stack.enter_context(ipc(root/role))
            threads[role] = str(uuid.uuid4())
            bridge.register(role, str(root/role), threads[role])
        cb.write_json(bridge.state_dir/'context.json', dict(bridge.context(), reply='uds:/legacy'))
        native = stack.enter_context(ipc(root/'native', native=True))
        native_peer(bridge, root/'native')
        program = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import crew_session_bridge as cb
original = cb.private_dir
cb.private_dir = lambda path: original(Path(sys.argv[4]) if str(path).startswith('/run/user/') else path)
cb.Bridge(sys.argv[2], sessions_dir=sys.argv[3]).serve(saved_context=True)
'''
        process = subprocess.Popen([sys.executable, '-u', '-c', program, str(CREW), str(bridge.state_dir),
            str(bridge.sessions_dir), str(root/'socks')], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            import select
            assert select.select([process.stdout], [], [], 4)[0], 'test daemon did not start'
            ready = json.loads(process.stdout.readline())
            assert ready['roles'] == list(GPT)
            metadata = cb.read_json(bridge.state_dir/'daemon.json')
            assert len(set(metadata['replies'].values())) == 4
            for role in GPT:
                message_id = str(uuid.uuid4())
                row = bridge.send(role, 'Alpha', f'from {role}', message_id=message_id)
                assert row['delivery_status'] == 'transport_sent', row
                assert native[-1][1]['from'] == metadata['replies'][role]
                path = Path(metadata['replies'][role][4:])
                digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()
                token = cb.read_json(bridge.sessions_dir/f'{process.pid}.{digest}.key')['peerToken']
                reply_id = str(uuid.uuid4())
                frames = [{'type':'auth','token':token},
                    {'type':'control','action':'peer_message_status','status':'delivered','orig_msg_id':message_id},
                    {'type':'user','msgV':1,'msg_id':reply_id,'message':{'content':f'reply {role}'}}]
                with socket.socket(socket.AF_UNIX) as conn:
                    conn.settimeout(3)
                    conn.connect(str(path))
                    conn.sendall(b''.join(json.dumps(f).encode()+b'\n' for f in frames))
                    conn.shutdown(socket.SHUT_WR)
                    assert conn.recv(16) == b''
                assert bridge.read(role)[0]['id'] == reply_id
                assert calls[role][0]['params']['threadId'] == threads[role]
                assert [r for r in bridge.read('Alpha') if r['id'] == message_id][0]['native_status'] == 'delivered'
        finally:
            process.terminate()
            process.wait(timeout=5)
            stderr = process.stderr.read()
            process.stdout.close()
            process.stderr.close()
        assert process.returncode == 0, stderr
        assert not stderr


def test_native_status_for_legacy_claude_sender_is_retained(local):
    root, bridge = local
    with ipc(root/'native', native=True):
        native_peer(bridge, root/'native', 'Beta')
        bridge.store('legacy-status', 'Alpha', 'Beta', 'existing CLI behavior')
        left, right = socket.socketpair()
        right.sendall(json.dumps({'type':'control','action':'peer_message_status',
            'status':'held','orig_msg_id':'legacy-status'}).encode()+b'\n')
        right.shutdown(socket.SHUT_WR)
        try:
            bridge.handle(left, role='Codex')
        finally:
            left.close()
            right.close()
        assert bridge.read('Beta')[0]['native_status'] == 'held'


def test_two_roles_racing_for_one_thread_have_one_winner(local):
    root, bridge = local
    thread = str(uuid.uuid4())
    with ipc(root/'app'):
        def register(role):
            try:
                return bridge.register(role, str(root/'app'), thread)['role']
            except cb.BridgeError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(register, ('Delta','Zeta')))
        assert results.count('thread_already_registered') == 1
        assert len(bridge.routes()) == 1


def test_public_cli_register_reports_identity_without_pipe(local):
    import subprocess
    root, bridge = local
    thread = str(uuid.uuid4())
    with ipc(root/'app'):
        result = subprocess.run([sys.executable, str(CREW/'crew_session_bridge.py'), '--state-dir',
            str(bridge.state_dir), 'register', '--role', 'Delta'],
            env=dict(os.environ, CODEX_THREAD_ID=thread, CODEX_APP_TOOLS_PIPE_PATH=str(root/'app')),
            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {'role':'Delta','thread':thread,'generation':1}
    assert str(root/'app') not in result.stdout + result.stderr
    assert bridge.context('Delta')['thread'] == thread


def test_send_cli_stable_message_id_survives_replay(local, tmp_path, monkeypatch, capsys):
    root, bridge = local
    body = tmp_path/'ticket.txt'
    body.write_text('untrusted Office infrastructure ticket')
    monkeypatch.setattr(cb, 'Bridge', lambda path: bridge)
    with ipc(root/'app') as calls:
        bridge.register('Delta', str(root/'app'), str(uuid.uuid4()))
        monkeypatch.setattr(sys, 'argv', ['sniper-crew','send','--from','Codex','--to','Delta',
                           '--message-file',str(body),'--message-id','office-ticket-123'])
        assert cb.main() == 0
        first = json.loads(capsys.readouterr().out)
        assert cb.main() == 0
        second = json.loads(capsys.readouterr().out)
        assert first['id'] == second['id'] == 'office-ticket-123'
        assert len(calls) == 1 and len(bridge.read('Delta')) == 1
        body.write_text('different payload under same ticket')
        assert cb.main() == 1
        assert json.loads(capsys.readouterr().out)['error'] == 'message_id_conflict'
        assert len(calls) == 1
