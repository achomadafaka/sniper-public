"""The rollout verifier must reject a proof that expired after its heartbeat."""
import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).resolve().parents[1] / 'docs/audits/2026-10-11-rh-entry-speed/verify_runtime.py'
spec = importlib.util.spec_from_file_location('rh_runtime_check', path)
V = importlib.util.module_from_spec(spec)
spec.loader.exec_module(V)


@pytest.mark.parametrize('changes,valid', [({}, True), ({'expires_at': 99}, False),
    ({'boot_id': 'other'}, False), ({'completion_epoch': 101}, False),
    ({'completion_epoch': float('nan')}, False), ({'cache_valid': False}, False)])
def test_current_completion_rejects_stale_or_foreign_heartbeat_proof(changes, valid):
    proof = dict(scope='discovery_only', chain_id='4663', proof_pool_ids=['pool'],
                 boot_id='boot', completion_epoch=95, expires_at=105, cache_valid=True)
    assert V.current_completion(dict(proof, **changes), now=100, boot_id='boot') is valid


@pytest.mark.parametrize('change,valid', [({}, True), ({'script': 'other.py'}, False),
    ({'cwd': '/tmp'}, False), ({'import_root': '/tmp'}, False),
    ({'state_root': '/tmp'}, False), ({'db_path': '/tmp/ledger.db'}, False)])
def test_standalone_source_requires_exact_pinned_code_and_primary_state(change, valid):
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    values = dict(script=str(release / 'scripts/memecoin_rh_feed_shadow_loop.py'),
                  cwd=str(V.ROOT), import_root=str(release), state_root=str(V.ROOT),
                  db_path=str(V.ROOT / 'sniper_tracker.db')) | change
    env = dict(PYTHONPATH=values['import_root'], SNIPER_STATE_ROOT=values['state_root'],
               DB_PATH=values['db_path'])
    assert V.source_owner_ok(release, ['python3', '-u', values['script']],
                             Path(values['cwd']), env) is valid


@pytest.mark.parametrize('prefix', [['other.py'], ['-c', 'pass'], ['-m', 'other'],
                                  ['-u', 'other.py']])
def test_source_script_must_be_executed_not_an_argument(prefix):
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    env = dict(PYTHONPATH=str(release), SNIPER_STATE_ROOT=str(V.ROOT))
    argv = ['python3', *prefix, str(release / 'scripts/memecoin_rh_feed_shadow_loop.py')]
    assert not V.source_owner_ok(release, argv, V.ROOT, env)


@pytest.mark.parametrize('db_path,chain,valid', [
    (None, '4663', True), (str(V.ROOT / 'sniper_tracker.db'), '4663', True),
    ('sniper_tracker.db', '4663', False), (None, '56', False)])
def test_watcher_explicit_relative_ledger_resolves_under_its_pinned_cwd(db_path, chain, valid):
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    env = dict(SNIPER_STATE_ROOT=str(V.ROOT))
    if db_path is not None:
        env['DB_PATH'] = db_path
    argv = ['python3', str(release / 'scripts/research/onchain_evm_wallet_watch.py'),
            '--chain', chain]
    assert V.source_owner_ok(release, argv, release, env) is valid


def test_standalone_relative_ledger_uses_primary_state():
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    env = dict(PYTHONPATH=str(release), SNIPER_STATE_ROOT=str(V.ROOT),
               DB_PATH='sniper_tracker.db')
    assert V.source_owner_ok(release,
        ['python3', str(release / 'scripts/memecoin_rh_feed_shadow_loop.py')], V.ROOT, env)


@pytest.mark.parametrize('standalone,valid', [(True, True), (False, False)])
def test_relative_state_root_resolves_under_source_process_cwd(standalone, valid, monkeypatch):
    monkeypatch.chdir(V.ROOT)
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    env = dict(SNIPER_STATE_ROOT='.', PYTHONPATH=str(release))
    script = 'memecoin_rh_feed_shadow_loop.py' if standalone else 'research/onchain_evm_wallet_watch.py'
    argv = ['python3', str(release / 'scripts' / script), '--chain', '4663']
    assert V.source_owner_ok(release, argv, V.ROOT if standalone else release, env) is valid


def test_source_script_argument_requires_a_python_interpreter():
    release = V.ROOT / '.runtime/memecoin/releases' / ('a' * 40)
    env = dict(SNIPER_STATE_ROOT=str(V.ROOT), PYTHONPATH=str(release))
    assert not V.source_owner_ok(release,
        ['/bin/cat', str(release / 'scripts/memecoin_rh_feed_shadow_loop.py')], V.ROOT, env)
