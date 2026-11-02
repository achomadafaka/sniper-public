"""Saved decoder history must not become a resident summary or scheduling queue."""
import json
import tracemalloc

import pytest


@pytest.mark.parametrize('phase', ['publish', 'summarize', 'collect'])
def test_saved_histories_do_not_accumulate_in_memory(tmp_path, monkeypatch, phase):
    from scripts.research import wallet_universe_metrics as worker
    from sniper.services import wallet_universe_flow as flow
    from sniper.execution.free_endpoint_registry import SOLANA_MAINNET_GENESIS

    at = '2026-10-04T16:00:00Z'
    address = 'Wallet'; owners = {('solana', address): ('A', 'Wallet')}
    db = worker.m.open_metrics(tmp_path / 'data/wallet_universe_metrics.db')
    units = []
    seen = {f'{n:088d}': [1000, n, 0] for n in range(5000)}
    for n in range(24):
        unit = dict(chain='solana', uid='A', address=address, mint=f'Mint{n}',
                    signature=f'Source{n}', t0=1000+n, proof_addresses=[address], own_signatures=[])
        identity = {k: unit[k] for k in ('chain', 'uid', 'address', 'mint', 'signature', 't0')}
        state = dict(version=flow.METHOD, binding=dict(identity=identity, exclusions=[address], own=[]),
                     seen=seen, records=[], cursor=f'cursor-{n}')
        result = dict(method=flow.METHOD, state=state, status='partial' if phase=='collect' else 'complete',
                      after_complete=True, quotes=[], last_collect=at, scope='fixture')
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',
                   (worker.unit_key(unit), json.dumps(unit), json.dumps(result), at))
        units.append(unit)
    for n, result in enumerate((None, json.dumps(dict(state={}, detail='provider_timeout')))):
        unit = {**units[0], 'mint': f'Unread{n}', 't0': 100+n}
        db.execute('INSERT INTO flow_units VALUES (?,?,?,?)',
                   (worker.unit_key(unit), json.dumps(unit), result, at))
        units.append(unit)
    db.commit()
    monkeypatch.setattr(worker, 'now_iso', lambda: at)
    resumed = []
    if phase == 'collect':
        class Rpc:
            used = 0
            def __call__(self, method, params):
                self.used += 1
                return SOLANA_MAINNET_GENESIS
            def close(self): pass
        monkeypatch.setattr(worker, 'research_reader', lambda *args: Rpc())
        monkeypatch.setattr(worker, 'paced', lambda *args: (lambda: None, None))
        monkeypatch.setattr(worker, 'flat', lambda *args: True)
        # Transport/decoder boundary only; real queue and owned DB stay in use.
        def collect(unit, rpc, *, state, **kwargs):
            assert len(state['seen']) == 5000
            assert state['cursor'] == 'cursor-' + unit['mint'][4:]
            assert state['binding']['identity']['mint'] == unit['mint']
            resumed.append(unit['mint'])
            rpc('getSlot', [])  # Consume the remaining attempt, preserving the hard budget.
            return dict(status='partial', detail='budget', state=state)
        monkeypatch.setattr(flow, 'collect_solana', collect)
    try:
        tracemalloc.start()
        if phase == 'collect':
            output = worker.collect_chain(tmp_path, 'solana', 'https://research.invalid',
                                          owners, units, {}, max_calls=2, capital_only=False)
        elif phase == 'publish':
            worker.publish(tmp_path, db, units, owners, {}, dict(state='ok'))
            output = json.loads((tmp_path / 'data/wallet_universe_metrics.json').read_text())['impact']
        else:
            output = worker.summarize(db, units, owners, {}, at)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 8 * 1024**2, f'{phase} retained saved histories: {peak / 1024**2:.1f} MiB'
        if phase == 'collect':
            assert resumed == ['Mint23'] and output['flow_reads'] == 1 and output['calls'] == 2
            assert db.execute('SELECT count(*) FROM flow_units').fetchone()[0] == 26
        else:
            assert output[0]['eligible'] == 26
            assert output[0]['complete'] == output[0]['priced'] == 24
            assert output[0]['states'] == {'complete': 24, 'pending': 2}
            assert output[0]['median_usd'] == 0
            assert len(output[0]['recent']) == 4
    finally:
        tracemalloc.stop()
        db.close()
