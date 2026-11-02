"""A clear must identify the exact pause that the caller observed."""
from concurrent.futures import ThreadPoolExecutor
import pytest

from sniper.services import memecoin_arming as a
from test_memecoin_real_status import _client


@pytest.mark.parametrize('source', ['arc-to-robinhood:receipt', 'runtime-deploy', 'web:other'])
def test_dashboard_retrip_preserves_current_owner(monkeypatch, tmp_path, source):
    client, conn = _client(monkeypatch, tmp_path)
    path = str(tmp_path / 'kill')
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', path)
    token = a.trip_kill_switch(path, source=source, only_if_clear=True)
    for address in ('127.0.0.1', '127.0.0.2'):
        response = client.post('/api/memecoin/real/kill', json={'action': 'trip'},
                               environ_overrides={'REMOTE_ADDR': address})
        assert response.status_code == 200
        status = client.get('/api/memecoin/real/status').get_json()
        assert (status['kill_source'], status['kill_token']) == (source, token)
    if source != 'web:other':
        assert client.post('/api/memecoin/real/kill', json={
            'action': 'clear', 'expected_token': status['kill_token']}).status_code == 409
    conn.close()


def test_dashboard_normal_pause_clear(monkeypatch, tmp_path):
    client, conn = _client(monkeypatch, tmp_path)
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', str(tmp_path / 'kill'))
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, 'send_alert', lambda *args: None)
    assert client.post('/api/memecoin/real/kill', json={'action': 'trip'}).status_code == 200
    status = client.get('/api/memecoin/real/status').get_json()
    assert status['kill_source'] == 'web:127.0.0.1'
    response = client.post('/api/memecoin/real/kill', json={
        'action': 'clear', 'expected_token': status['kill_token']})
    assert response.status_code == 200 and not response.get_json()['tripped']
    conn.close()


def test_stale_clear_cannot_remove_a_new_pause(tmp_path):
    path = str(tmp_path / 'kill')
    old = a.trip_kill_switch(path, source='runtime-deploy')
    new = a.trip_kill_switch(path, source='operator')
    assert old and new and old != new
    assert a.clear_kill_switch(path, source='runtime-deploy', expected_token=old) is False
    assert a.kill_switch_status(path)['kill_token'] == new
    assert a.clear_kill_switch(path, source='operator', expected_token=new) is True


def test_missing_token_and_unconfirmed_deploy_override_fail(tmp_path):
    path = str(tmp_path / 'kill')
    token = a.trip_kill_switch(path, source='runtime-deploy')
    assert a.clear_kill_switch(path, source='runtime-deploy') is False
    assert a.clear_kill_switch(path, source='web:127.0.0.1', expected_token=token) is False
    assert a.clear_kill_switch(path, source='web:127.0.0.1', expected_token=token,
                               override_deploy=True) is True


def test_only_one_deploy_acquires_the_switch(tmp_path):
    path = str(tmp_path / 'kill')
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _: a.trip_kill_switch(
            path, source='runtime-deploy', only_if_clear=True), range(20)))
    assert len([t for t in tokens if t]) == 1
    assert a.kill_switch_source(path) == 'runtime-deploy'
    from sniper.services.memecoin_real_runner import _kill_trip_source
    assert _kill_trip_source(path) == 'runtime-deploy'


def test_legacy_file_can_be_cleared_only_from_its_snapshot(tmp_path):
    path = tmp_path / 'kill'
    path.write_text('tripped by alpha-deploy:old')
    state = a.kill_switch_status(str(path))
    assert state['kill_tripped'] and state['kill_deploy_held']
    assert a.clear_kill_switch(str(path), source='web:127.0.0.1',
        expected_token=state['kill_token'], override_deploy=True)


def test_api_rejects_stale_clear_and_requires_explicit_override(monkeypatch, tmp_path):
    client, conn = _client(monkeypatch, tmp_path)
    path = str(tmp_path / 'kill')
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', path)
    from sniper.services import memecoin_alerts
    alerts = []
    monkeypatch.setattr(memecoin_alerts, 'send_alert', lambda *args: alerts.append(args))
    old = a.trip_kill_switch(path, source='runtime-deploy')
    new = a.trip_kill_switch(path, source='runtime-deploy')
    status = client.get('/api/memecoin/real/status').get_json()
    assert status['kill_token'] == new and status['kill_deploy_held']
    for payload in ({'action':'clear'}, {'action':'clear','expected_token':old,'override_deploy':True},
                    {'action':'clear','expected_token':new},
                    {'action':'clear','expected_token':new,'override_deploy':'true'}):
        assert client.post('/api/memecoin/real/kill', json=payload).status_code == 409
        assert a.is_kill_tripped(path) and not alerts
    response = client.post('/api/memecoin/real/kill', json={
        'action':'clear','expected_token':new,'override_deploy':True})
    assert response.status_code == 200 and response.get_json()['tripped'] is False
    assert len(alerts) == 1
    conn.close()


def test_deploy_cleanup_preserves_a_replacement_pause(tmp_path):
    """Run the shell cleanup against a temporary switch, with an older target release."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts/deploy_memecoin_runtime.sh').read_text()
    function = script[script.index('clear_deploy_entry_gate() {'):script.index('pre_switch_rollover_rollback() {')]
    path = str(tmp_path / 'kill')
    old = a.trip_kill_switch(path, source='runtime-deploy')
    new = a.trip_kill_switch(path, source='operator')
    env = dict(os.environ, DEFAULT_SOURCE=str(root), RELEASE=str(tmp_path/'old-release'),
        STATE_ROOT=str(tmp_path), PY=sys.executable, MEMECOIN_KILL_SWITCH_FILE=path,
        DEPLOY_KILL_PATH=path, DEPLOY_KILL_TOKEN=old, DEPLOY_KILL_OWNED='1')
    result = subprocess.run(['bash','-eu','-c',function+'\nclear_deploy_entry_gate'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert a.kill_switch_status(path)['kill_token'] == new
    token = a.trip_kill_switch(path, source='runtime-deploy')
    env['DEPLOY_KILL_TOKEN'] = token
    result = subprocess.run(['bash','-eu','-c',function+'\nclear_deploy_entry_gate'],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert not a.is_kill_tripped(path)


@pytest.mark.parametrize('foreign_hold', [False, True])
@pytest.mark.parametrize('late_open', [False, True])
def test_flat_cutover_checks_under_hold_before_stop(tmp_path, foreign_hold, late_open):
    """Execute the real admission/drain block; all process control is replaced by a marker."""
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts/deploy_memecoin_runtime.sh').read_text()
    check = script[script.index('require_flat_book() {'):script.index('# Validation overrides')]
    cleanup = script[script.index('clear_deploy_entry_gate() {'):script.index('pre_switch_rollover_rollback() {')]
    gate = script[script.index('# Stop new entry admission,'):script.index('  # DECLARE THE INTENT')]
    db = tmp_path / 'book.db'
    with sqlite3.connect(db) as conn:
        conn.executescript('CREATE TABLE memecoin_positions(execution_mode TEXT,is_open INTEGER);'
                           'CREATE TABLE memecoin_pending_entries(status TEXT);')
        if late_open:
            conn.execute("INSERT INTO memecoin_positions VALUES('real',1)")
    path = str(tmp_path / 'kill')
    token = a.trip_kill_switch(path, source='arc-to-base:test') if foreign_hold else None
    command = ('fail() { echo "$*" >&2; exit 1; }\n'
               'pending_reservations() { echo 0; }\n' + check + cleanup +
               '\ntrap clear_deploy_entry_gate EXIT\n' + gate + '\nfi\necho STOP_ALLOWED\n')
    env = dict(os.environ, DEFAULT_SOURCE=str(root), STATE_ROOT=str(tmp_path), PY=sys.executable,
               DB=str(db), REQUIRE_FLAT='1', DEPLOY_KILL_OWNED='0', DEPLOY_KILL_PATH='',
               DEPLOY_KILL_TOKEN='', MEMECOIN_KILL_SWITCH_FILE=path, SNIPER_DEPLOY_NO_PROCESS='0')
    result = subprocess.run(['bash', '-eu', '-c', command], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=10)
    assert ('STOP_ALLOWED' in result.stdout) is not late_open
    assert (result.returncode == 0) is not late_open
    assert a.kill_switch_status(path)['kill_token'] == token


def test_unreadable_switch_cannot_be_cleared(tmp_path):
    path = tmp_path / 'kill'
    path.mkdir()
    state = a.kill_switch_status(str(path))
    assert state['kill_tripped'] is True and state['kill_token'] is None
    assert a.clear_kill_switch(str(path), source='operator',
                               expected_token='unavailable', override_deploy=True) is False
    assert path.is_dir()


def test_the_dashboard_cannot_release_a_transfer_hold(monkeypatch, tmp_path):
    """logs/kill_switch_audit.log:3147-3148, 2026-09-22: arc-to-base tripped its hold at 17:58:58Z
    and web:127.0.0.1 cleared it with the same token 9.5 s later, mid-transfer. The web must refuse
    and say which receipt to resume; the script itself and a deliberate CLI clear still work."""
    client, conn = _client(monkeypatch, tmp_path)
    path = str(tmp_path / 'kill')
    monkeypatch.setenv('MEMECOIN_KILL_SWITCH_FILE', path)
    from sniper.services import memecoin_alerts
    monkeypatch.setattr(memecoin_alerts, 'send_alert', lambda *args: None)
    source = 'arc-to-base:c2b6e1ed-b122-4a37-8189-37ea877b7d03'
    token = a.trip_kill_switch(path, source=source, only_if_clear=True)
    response = client.post('/api/memecoin/real/kill', json={
        'action': 'clear', 'expected_token': token, 'override_deploy': True})
    assert response.status_code == 409
    error = response.get_json()['error']
    assert 'data/arc-to-base-c2b6e1ed-b122-4a37-8189-37ea877b7d03.json' in error
    assert 'scripts/memecoin_bridge_arc_to_base.py --resume' in error
    assert a.kill_switch_status(path)['kill_token'] == token
    # The atomic check holds even for a caller that skips the route's pre-check.
    assert a.clear_kill_switch(path, source='web:127.0.0.1', expected_token=token,
                               refuse_transfer=True) is False
    assert a.clear_kill_switch(path, source=source, expected_token=token) is True
    token = a.trip_kill_switch(path, source=source, only_if_clear=True)
    assert a.clear_kill_switch(path, source='operator', expected_token=token) is True
    conn.close()


def test_every_script_that_takes_a_hold_is_a_registered_transfer():
    """A new transfer script that trips the switch must be registered, or the web can release it."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    names = set()
    for script in (root / 'scripts').glob('*.py'):
        names |= set(re.findall(r"trip_kill_switch\(.*?source=[\"']([\w-]+):[\"']", script.read_text()))
    assert names == set(a.TRANSFER_HOLDS), names
    for name, script in a.TRANSFER_HOLDS.items():
        assert (root / script).is_file(), script
        assert "source=%r" % (name + ":") in (root / script).read_text().replace('"', "'")
