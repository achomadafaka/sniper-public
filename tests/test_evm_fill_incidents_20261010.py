"""Offline regressions for FITCOIN 72879, 쿠우타 72902 and ORBLIVION 72843."""
import copy
import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_bsc_fast_lane as bsc
from sniper.services import memecoin_evm_post_trade as post
from sniper.services import memecoin_rh_fast_decide as rh
from sniper.services import memecoin_rh_fast_lane as lane
from sniper.services.memecoin_source_fill import source_fill_from_buy
from sniper.settings import memecoin as cfg

FRAME = json.loads((Path(__file__).parent / 'fixtures/memecoin/orblivion_72843_source.json').read_text())


def _orbl():
    f = copy.deepcopy(FRAME)
    decoded = post.source_post_trade('4663', f['receipt'], f['mint'], owner=f['wallet'],
                                    pool_key_fn=lambda chain, pool: f['keys'].get(pool))
    buy = dict(f['buy'], source_initiation=f['initiation'], source_post=decoded)
    sig = dict(chain='4663', tx_hash=f['receipt']['transactionHash'], wallet=f['wallet'],
               source_user_id=f['uid'], mint=f['mint'], seq=int(f['receipt']['blockNumber'], 16))
    return f, decoded, buy, sig


def test_orblivion_split_binds_all_outputs_without_inventing_one_pool_price():
    f, decoded, _, _ = _orbl()
    assert decoded['reason'] == 'split_route'
    assert len(decoded['pools']) == 2 and decoded['receipt_routes_complete'] is True
    assert int(decoded['pool_output_atomic']) == 20195298529345133749309093
    assert 'ratio' not in decoded and 'pool' not in decoded


@pytest.mark.parametrize('mutation', ['none', 'wrong_uid', 'wrong_atoms', 'missing_origin'])
def test_verified_deposit_initiation_survives_the_receipt_fill_builder(monkeypatch, mutation):
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS', ('4663', '56'))
    _, _, buy, sig = _orbl()
    if mutation == 'wrong_uid': buy['source_initiation']['origin_identity_user_id'] = 'other'
    if mutation == 'wrong_atoms': buy['source_initiation']['acquired_atomic'] = '1'
    if mutation == 'missing_origin': buy['source_initiation'].pop('origin_tx')
    proof = source_fill_from_buy(buy, sig)
    if mutation != 'none':
        assert proof is None
    else:
        assert proof is not None
        assert proof['source_initiation'] == FRAME['initiation']


@pytest.mark.parametrize('estimated_allowed', [False, True])
def test_orblivion_complete_split_uses_the_shared_executor_with_exact_wallet_net(monkeypatch, estimated_allowed):
    from test_memecoin_rh_fast_decide import CTX
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS', ('4663', '56'))
    f, _, buy, sig = _orbl()
    proof = source_fill_from_buy(buy, sig)
    assert proof is not None
    tape = rh.TapeState(lambda _: None)
    tape.learn_source_fills([proof])
    sig.update(payment_token=proof['payment_token'], paid=proof['payment_atomic'], permit_nonce=1,
               arrival_utc='2026-10-09T23:54:30+00:00')
    out = rh.decide(sig, tape=tape, **dict(CTX, allow_estimated_source=estimated_allowed,
                                        decimals={proof['payment_token']: 6}))
    assert out['reason'] == 'would_send' and out['route_kind'] == 'split_shared'
    assert out['src_out'] == int(f['initiation']['acquired_atomic'])
    assert out['pool'] is None and out['post_ratio'] is None and out.get('tx') is None
    payload = lane.lane_payload(sig, out, sent_epoch=time.time())
    assert lane.check_sender(payload, 12, os.getuid(), detector_pid=12) is None
    ev = lane.LaneEvent(payload, decimals=18, supply=None)
    assert ev.source_initiation == f['initiation']
    assert ev.their_price == pytest.approx(1298.775254 / (int(out['src_out']) / 10**18))
    assert 'ratio' not in ev.evm_chase_reference.payload()


@pytest.mark.parametrize('receipt_hash', [True, False])
def test_receipt_backed_relay_handoff_keeps_its_chain_authority(monkeypatch, receipt_hash):
    from sniper.services.memecoin_source_initiation import source_initiation_refusal
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS', ('4663',))
    monkeypatch.setattr(cfg, 'MEMECOIN_RH_FAST_DEPOSIT_PROOF', False)
    f, _, _, sig = _orbl()
    init = f['initiation']
    if not receipt_hash: init.pop('block_hash')
    ev = SimpleNamespace(signal_source='rh_fast_lane', source_wallet=f['wallet'], mint=f['mint'],
        source_user_id=f['uid'], source_block_number=sig['seq'], source_initiation=init,
        fomo_event_id='rhfast:4663:%s:%s' % (sig['tx_hash'], f['mint']))
    assert (source_initiation_refusal(ev, '4663') is None) is receipt_hash


@pytest.mark.parametrize('shared', [True, False])
def test_rh_early_quote_starts_only_for_shared_execution(monkeypatch, tmp_path, shared):
    import sqlite3
    import threading
    from sniper.services import memecoin_real_runner as runner
    c = sqlite3.connect(str(tmp_path / 'ledger.db'))
    started = threading.Event()
    def quote(*a, **kw): started.set(); return dict(quote=dict(buyAmount='123'))
    ctx = dict(prepare_entry_quote_fn=quote)
    monkeypatch.setattr(runner, '_early_evm_quote_clip', lambda *a: (25., None))
    ev = SimpleNamespace(mint='0x'+'aa'*20, signal_source='rh_fast_lane', shared_evm_route=shared)
    trace = SimpleNamespace(mark_substage=lambda *a, **kw: None)
    early = runner._start_early_evm_quote(c, ev, {'4663': ctx}, trace, object(), '4663')
    if shared:
        assert early is not None and started.wait(1)
        early.worker.join(1)
    else:
        assert early is None and not started.is_set()
    c.close()


def test_bsc_native_handoff_precedes_publication_and_wake(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watch
    calls, rows = [], []
    monkeypatch.setattr(cfg, 'memecoin_bsc_fast_lane_mode', lambda: 'live')
    monkeypatch.setattr(watch, '_signal_bsc_lane', lambda *a: calls.append('native') or
                        dict(sent=True, reason='would_send'))
    monkeypatch.setattr(watch, '_commit_detections', lambda c, rs: calls.append('commit+wake') or rows.extend(rs))
    row = dict(valuation={}, now='2026-10-10T03:48:42+00:00')
    watch._publish_detection(None, row, bsc_signal=object(), outcome={}, quote_usd=740, price_age=0)
    assert calls == ['native', 'commit+wake']
    assert rows[0]['valuation']['bsc_fast_lane_sent'] is True


@pytest.mark.parametrize('age,sent,chain,expect', [(0.1, True, '56', False),
    (2.1, True, '56', True), (0.1, False, '56', True), (0.1, True, '8453', True)])
def test_bsc_native_priority_is_bounded_and_never_blocks_a_lane_thread(age, sent, chain, expect):
    from test_memecoin_onchain_lane import NOW, ARMED_UID, approved, conn_with
    from sniper.services.memecoin_onchain_lane import sweep_onchain_detections
    c = conn_with([])
    at = (NOW - timedelta(seconds=age)).isoformat()
    c.execute('INSERT INTO onchain_wallet_detections '
              '(signature,source_user_id,handle,address,network_id,mint,detected_at,created_at,source_valuation_json) '
              'VALUES (?,?,?,?,?,?,?,?,?)',
              (chain+':0x'+'aa'*32+':0x'+'bb'*20, ARMED_UID, 'armed', '0x'+'cc'*20, chain,
               '0x'+'bb'*20, at, at, json.dumps(dict(bsc_fast_lane_sent=sent))))
    calls, held = [], {}
    cursor, result = sweep_onchain_detections(c, lambda c, ev: calls.append(ev) or dict(status='filled'),
        cursor=0, now_fn=lambda: NOW.isoformat(), max_age_s=45, approved_ids_fn=approved, deferred=held)
    assert bool(calls) is expect and cursor == 1
    if not expect:
        assert held and result[0]['result']['reason'] == 'bsc_fast_lane_handoff'
        sweep_onchain_detections(c, lambda c, ev: calls.append(ev) or dict(status='filled'),
            cursor=cursor, now_fn=lambda: (NOW+timedelta(seconds=3)).isoformat(), max_age_s=45,
            approved_ids_fn=approved, deferred=held)
        assert len(calls) == 1 and not held
    c.close()


def test_fitcoin_early_snapshot_is_bound_to_the_latest_send():
    snapshot = SimpleNamespace(current=lambda: dict(native_wei=10**18, weth_wei=2*10**18,
                                                    read_at=time.time()-1))
    obj = object.__new__(bsc.BscLive)
    obj.snapshot, obj.host_clock, obj.generation_fn = snapshot, time.time, lambda: 0
    obj.max_snapshot_age_s, obj.generation_margin_s = 5, 0
    assert obj.early_quote_balances(740) == (1480, 1)
    obj.generation_fn = lambda: time.time()
    assert obj.early_quote_balances(740) is None


@pytest.mark.parametrize('case', ['fresh', 'stale_price', 'missing_price', 'unavailable', 'knob_off'])
def test_fitcoin_early_sizing_overlaps_cold_balances_without_weakening_the_gate(monkeypatch, sizing, case):
    call, pre = sizing
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_EARLY_CACHED_SIZING', case != 'knob_off')
    used = []
    pre.context.update(early_quote_balances_fn=lambda price: used.append(price) or
                       (None if case == 'unavailable' else (1480, 1)),
                       quote_price_peek_fn=lambda: dict(px=None if case == 'missing_price' else 740,
                                                       age_ms=2000 if case == 'stale_price' else 10))
    (clip, refusal, facts), seen, fresh = call()
    if case == 'fresh':
        assert (clip, refusal, facts['balance_source']) == (25., None, 'lane_snapshot')
        assert seen['usdc'] == 1480 and fresh == [] and used == [740]
    else:
        assert facts['balance_source'] == 'fresh' and fresh == [1]


@pytest.mark.parametrize('price', [740, None, 0, float('nan')])
def test_fitcoin_cold_price_warms_before_waiting_for_balances(monkeypatch, sizing, price):
    call, pre = sizing
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_EARLY_CACHED_SIZING', True)
    warmed = []
    memo = {}
    def warm():
        warmed.append(1)
        memo.update(px=price, age_ms=0)
        return price
    pre.context.update(early_quote_balances_fn=lambda p: (2*p, 1),
                       quote_price_peek_fn=lambda: memo,
                       warm_quote_price_fn=warm)
    (clip, refusal, facts), seen, fresh = call()
    assert warmed == [1]
    assert (facts['balance_source'], fresh) == (('lane_snapshot', []) if price == 740 else ('fresh', [1]))
    if price == 740:
        assert seen['usdc'] == 1480 and clip == 25 and refusal is None


def test_concurrent_live_price_consumers_share_one_fetch_and_peek_never_waits():
    from concurrent.futures import ThreadPoolExecutor
    from sniper.execution.evm_real_seams import build_real_evm_executor
    entered, release = threading.Event(), threading.Event()
    calls = []
    def price():
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return 740
    ctx = build_real_evm_executor(acct=SimpleNamespace(address='0x'+'ab'*20), chain_id='56',
                                  rpc_url='offline', price_fn=price)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ctx['warm_quote_price_fn'])
        assert entered.wait(1)
        second = pool.submit(ctx['warm_quote_price_fn'])
        try:
            assert ctx['quote_price_peek_fn']() is None
            time.sleep(.02)
            assert calls == [1]
        finally:
            release.set()
        assert first.result(1) == second.result(1) == 740
    assert ctx['quote_price_peek_fn']()['px'] == 740


@pytest.mark.parametrize('failure', [None, RuntimeError('price transport')])
def test_failed_live_price_fetch_does_not_pin_the_next_read(failure):
    from sniper.execution.evm_real_seams import build_real_evm_executor
    answers = iter([failure, 740])
    def price():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer
    ctx = build_real_evm_executor(acct=SimpleNamespace(address='0x'+'ab'*20), chain_id='56',
                                  rpc_url='offline', price_fn=price)
    if failure is None:
        assert ctx['warm_quote_price_fn']() is None
    else:
        with pytest.raises(RuntimeError):
            ctx['warm_quote_price_fn']()
    assert ctx['warm_quote_price_fn']() == 740


def test_spend_during_price_warming_discards_the_speculative_snapshot(monkeypatch, sizing):
    from sniper.services import memecoin_real_runner as runner
    call, pre = sizing
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_EARLY_CACHED_SIZING', True)
    memo = {}
    def warm():
        memo.update(px=740, age_ms=0)
        runner._gate_balance_generation['8453'] += 1
    pre.context.update(early_quote_balances_fn=lambda p: (1480, 1),
                       quote_price_peek_fn=lambda: memo, warm_quote_price_fn=warm)
    (clip, refusal, facts), _, fresh = call()
    assert clip is None and refusal == 'balance_stale_generation' and fresh == [1]


@pytest.mark.parametrize('change', ['unknown_key', 'owner_forwarded', 'pool_sell'])
def test_partial_or_mixed_direction_orblivion_receipt_never_gets_split_handoff(change):
    f, _, _, _ = _orbl()
    if change == 'unknown_key': f['keys'].pop(next(iter(f['keys'])))
    if change == 'owner_forwarded':
        f['receipt']['logs'].append(dict(address=f['mint'], topics=[post.TRANSFER,
            '0x'+f['wallet'][2:].rjust(64, '0'), '0x'+'dd'*32], data=hex(5*10**24)))
    if change == 'pool_sell':
        swap = next(log for log in f['receipt']['logs']
                    if len(log.get('topics', [])) > 1 and log['topics'][1] in f['keys']
                    and f['keys'][log['topics'][1]]['currency0'] == f['mint'])
        swap['data'] = '0x'+hex(2**256-1)[2:]+swap['data'][66:]
    decoded = post.source_post_trade('4663', f['receipt'], f['mint'], owner=f['wallet'],
                                    pool_key_fn=lambda chain, pool: f['keys'].get(pool))
    assert not decoded.get('pools')


from test_evm_early_cached_sizing import sizing  # noqa: E402,F401


@pytest.mark.parametrize('proof_kind', ['bound_split', 'unbound_split', 'never'])
def test_orblivion_tape_split_waits_for_the_receipt_proof_instead_of_refusing(monkeypatch, proof_kind):
    """4663 fast-lane misses S3 (2026-10-14): the tape's two legs arrive before the watcher's proof.
    The lane refused source_swap_ambiguous 75-156 ms before a bound split proof landed (13 signals
    since 10-10). It now waits on the existing re-ask; a proof without pools still ends ambiguous."""
    from test_memecoin_rh_fast_decide import CTX, _row
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS', ('4663', '56'))
    f, decoded, buy, sig = _orbl()
    proof = source_fill_from_buy(buy, sig)
    assert proof is not None
    native = '0x' + '00' * 20
    meta = {p: dict(protocol='v4', mint=sig['mint'], token0=native, token1=sig['mint'], fee=0,
                    tick_spacing=200, hooks=native) for p in decoded['pools']}
    tape = rh.TapeState(meta.get)
    tape.ingest([_row(p, sig['seq'], sig['tx_hash'], 2**96, a0=100, a1=-out, log=i)
                 for i, (p, out) in enumerate(zip(decoded['pools'], (300, 700)), 1)])
    sig.update(payment_token=proof['payment_token'], paid=proof['payment_atomic'], permit_nonce=1,
               arrival_utc='2026-10-09T23:54:30+00:00')
    ctx = dict(CTX, allow_estimated_source=False, decimals={proof['payment_token']: 6})
    first = rh.decide(dict(sig), tape=tape, **ctx)
    assert first['reason'] == 'source_split_pending', first
    assert first.get('tx') is None and 'src_out' not in first
    assert first['reason'] in rh._SOURCE_REASK_REASONS
    pending = rh.PendingSourceSwaps(wait_s=2.0)
    assert pending.offer(sig, enqueued=1, now=10.0)
    assert pending.ready(tape, now=10.1) == []
    if proof_kind == 'bound_split':
        tape.learn_source_fills([proof])
        assert pending.ready(tape, now=10.2) == [(sig, 1)]
        out = rh.decide(dict(sig), tape=tape, **ctx)
        assert out['reason'] == 'would_send' and out['route_kind'] == 'split_shared'
    elif proof_kind == 'unbound_split':
        tape.learn_source_fills([dict(proof, source_post={'reason': 'split_route'})])
        assert pending.ready(tape, now=10.2) == [(sig, 1)]
        assert rh.decide(dict(sig), tape=tape, **ctx)['reason'] == 'source_swap_ambiguous'
    else:
        assert pending.ready(tape, now=12.1) == []
        (_rec, _enqueued, blocker), = pending.pop_expired()
        assert blocker == 'proof_absent'
