"""Native admission keeps first-buy quality without a source-slot deadline."""
import json
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_slot_lane as L, memecoin_entry_opportunity as policy
from sniper.settings import memecoin as cfg
from test_memecoin_entry_opportunity import ledger, UID, OWNER, MINT, SIG, SECOND, NOW, ARM


def buy(**changes):
    values = dict(venue='pump_curve', source_user_id=UID, owner=OWNER, mint=MINT,
        signature=SIG, slot=100, source_pre_atomic=0, recv_monotonic=10.,
        refusal=None, graduation=None, ondemand=None, rabbit=None, routed=None)
    values.update(changes)
    if 'source_initiation' not in changes:
        values['source_initiation'] = dict(schema_version=1, proof_version='direct-wallet-paid-v1',
            network_id='solana', tx_hash=values['signature'], block_number=values['slot'],
            source_user_id=values['source_user_id'], mint=values['mint'] or MINT,
            beneficiary=values['owner'], payer=values['owner'], acquired_atomic='100', payment_atomic='10')
    return SimpleNamespace(**values)


def native(conn, *, snapshot=None, head=None, **kwargs):
    return L.SlotLane(conn=conn, executor={}, build_fn=None,
        snapshot_fn=snapshot or (lambda: pytest.fail('policy refusal reached market selection')),
        armed_ids_fn=lambda: {UID}, kill_tripped_fn=lambda: False,
        now_iso_fn=lambda: NOW, lock_key='slot-first-buy-policy', mode='live',
        head_slot_fn=head or (lambda: (101, 10.)),
        clock=SimpleNamespace(monotonic=lambda: 10.1, perf_counter=lambda: 10.1), **kwargs)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON', '{}')


@pytest.mark.parametrize('gate', ['signal_age', 'stale', 'selection', 'decoder'])
def test_first_miss_at_each_early_gate_consumes_the_opportunity(monkeypatch, gate):
    c = ledger()
    calls = []
    snap = SimpleNamespace(sol_usd=100.)
    lane = native(c, snapshot=lambda: snap)
    monkeypatch.setattr(L, 'stale_input', lambda *_a: 'balances' if gate == 'stale' else None)
    monkeypatch.setattr(L, 'admission_refusal', lambda *_a, **_kw: ('source buy under floor', {}))
    first = buy(recv_monotonic=9. if gate == 'signal_age' else 10.,
                refusal='multi_own_buy:2xpump_curve' if gate == 'decoder' else None)
    assert lane._handle(first)['status'] == 'rejected'
    lane.snapshot_fn = lambda: calls.append(True) or snap
    second = buy(signature=SECOND, slot=102)
    assert lane._handle(second)['reason'] == 'first_source_buy_missed'
    assert not calls


def test_unknown_and_positive_source_holdings_refuse_before_snapshot():
    for pre, reason in [(None, 'source_first_buy_unknown'), (1, 'source_already_holding')]:
        c = ledger()
        assert native(c)._handle(buy(source_pre_atomic=pre))['reason'] == reason


@pytest.mark.parametrize('kind', ['routed', 'amm'])
def test_early_identity_uses_routed_or_cached_amm_mint_without_learning(kind):
    c = ledger()
    class Pools:
        def cached(self, pool):
            assert pool == 'pool'
            return {'base_mint': MINT}
        def get(self, _pool):
            pytest.fail('early identity queued a pool learn')
    lane = native(c, head=lambda: (103, 10.), amm_pools=Pools())
    routed = SimpleNamespace(mint=MINT, source_pre_atomic=0) if kind == 'routed' else None
    signal = buy(mint=None, venue='pump_amm', pool='pool', routed=routed,
                 source_pre_atomic=None if routed else 0)
    window, reason = lane._entry_opportunity(signal)
    assert reason is None and window['entry_max_slot'] is None
    assert c.execute('SELECT source_signature FROM memecoin_first_source_buys').fetchone()[0] == SIG


@pytest.mark.parametrize('reason', ['first_source_buy_missed', 'source_first_buy_unknown',
    'source_already_holding', 'source_buy_slot_mismatch', 'source_buy_claim_unknown',
    'entry_slot_expired', 'entry_slot_head_unknown', 'entry_slot_head_stale'])
def test_window_refusals_cannot_open_routed_or_learner_handoffs(monkeypatch, reason):
    c = ledger()
    lane = native(c, routed_handoff=SimpleNamespace(write=lambda *_a: pytest.fail('routed handoff')),
                  handoff=SimpleNamespace(offer=lambda *_a: pytest.fail('retry handoff')),
                  miss_learner=SimpleNamespace(is_retry=lambda _sig: True))
    signal = buy(routed=SimpleNamespace(mint=MINT))
    monkeypatch.setattr(lane, '_handle', lambda *_a, **_kw: {'status': 'rejected', 'reason': reason})
    assert L.policy_refusal(reason)
    assert lane._handle_buy(signal)['reason'] == reason
    signal.receipt_evidence = {'producer_run_id': 'unit', 'recv_monotonic': 10.}
    assert L._receipt_facts(signal, {'reason': reason})['policy_refused'] is True


def test_policy_disabled_and_shadow_leave_legacy_selection_unchanged(monkeypatch):
    for disabled, shadow in [(True, False), (False, True)]:
        monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_FIRST_BUY_WINDOW', not disabled)
        monkeypatch.setattr(policy, 'evaluate', lambda *_a, **_kw: pytest.fail('legacy claimed first'))
        lane = native(ledger(), snapshot=lambda: SimpleNamespace(sol_usd=100.))
        lane.mode = 'shadow' if shadow else 'live'
        monkeypatch.setattr(L, 'stale_input', lambda *_a: 'balances')
        assert lane._handle(buy())['reason'] == 'slot_lane_stale_balances'


def test_unproven_route_keeps_receipt_handoff_and_does_not_consume_verified_successor(monkeypatch):
    from test_memecoin_slot_lane import _snap
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner, '_start_momentum_read', lambda *_a: None)
    c, remembered = ledger(), []
    lane = native(c, snapshot=lambda: _snap(10.1),
        routed_handoff=SimpleNamespace(remember=remembered.append, write=lambda _buy: 'awaiting_receipt'))
    facts = dict(mint=MINT, their_usd=100., price_usd=.01, mcap_usd=100_000.)
    monkeypatch.setattr(L, 'admission_refusal', lambda *_a, **_kw: (None, dict(facts)))
    first = buy(source_initiation=None,
                routed=SimpleNamespace(mint=MINT, source_pre_atomic=0, requires_receipt_proof=True))
    result = lane.handle(first)
    assert result['status'] == 'source_initiation_unknown' and result['reason'] == 'missing_receipt_proof'
    assert remembered == [first]
    assert c.execute('SELECT COUNT(*) FROM memecoin_first_source_buys').fetchone()[0] == 0
    second = buy(signature=SECOND, slot=101)
    window, reason = lane._entry_opportunity(second)
    assert reason is None and window['source_signature'] == SECOND and window['entry_max_slot'] is None
    assert c.execute('SELECT source_signature FROM memecoin_first_source_buys').fetchone()[0] == SECOND


@pytest.mark.parametrize('delta,observed_head', [(0, 102), (1, 200)])
def test_admitted_native_passes_only_live_head_to_executor(monkeypatch, tmp_path, delta, observed_head):
    from test_memecoin_slot_lane import _buy, _snap
    from sniper.execution import exec_lock
    signal = _buy(slot=100, recv_monotonic=10., source_pre_atomic=0)
    signal.source_user_id = 'pump:' + signal.owner
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_SOLANA_ENTRY_EXCEPTIONS_JSON',
                        json.dumps({signal.source_user_id: dict(max_slot_delta=delta, allow_later_buys=False)}))
    c = ledger()
    m.add_wallet(c, signal.source_user_id, 'source-native')
    c.execute("UPDATE memecoin_wallets SET real_approved=1,real_chains='solana' WHERE source_user_id=?",
              (signal.source_user_id,))
    c.execute("INSERT INTO memecoin_wallet_approvals(source_user_id,action,actor,created_at,chain) VALUES(?,'chain_arm','test',?,'solana')",
              (signal.source_user_id, ARM))
    c.commit()
    monkeypatch.setattr(exec_lock, '_lock_path', lambda key, lock_dir=None: str(tmp_path / key))
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ENABLED', True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_MCAP_USD', 250_000.)
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_REGIME_RISK_SCALE', 1.)
    seen = []
    lane = native(c, snapshot=lambda: _snap(10.1), head=lambda: (observed_head, 10.))
    lane.armed_ids_fn = lambda: {signal.source_user_id}
    lane.build_fn = lambda *_a: ('message', {'blockhash': 'blockhash'})
    def execute(_conn, _pending, _message, **kwargs):
        seen.append(kwargs)
        return {'status': 'filled', 'position_id': 1}
    lane.executor['slot_execute_fn'] = execute
    result = lane.handle(signal)
    assert result['status'] == 'filled', result
    kw, = seen
    assert 'entry_min_slot' not in kw and 'entry_max_slot' not in kw
    assert kw['entry_head_fn'] is lane.head_slot_fn
    trace = json.loads(c.execute('SELECT trace_json FROM memecoin_entry_attempts').fetchone()[0])
    assert trace['source_evidence']['source_pre_atomic'] == 0
    assert any(e.get('name') == 'entry_opportunity' and e.get('entry_max_slot') is None
               and e.get('max_slot_delta') is None
               for e in trace['substage_events'])


def test_startup_status_exposes_effective_entry_policy(monkeypatch):
    from test_slot_lane_exotic_live import _start
    _lane, status = _start(monkeypatch, set(), entry_slot_guard_ready=True)
    assert status['entry_opportunity'] == dict(policy.snapshot(), head_callback_ready=True)
    assert status['entry_opportunity']['default_max_slot_delta'] is None
    assert status['entry_opportunity']['head_max_age_ms'] == 500
    assert status['entry_opportunity']['deadline_basis'] is None
    assert status['entry_opportunity']['landing_deadline_guaranteed'] is False
    json.dumps(status['entry_opportunity'])


def test_real_pumpswap_existing_account_events_carry_pre_trade_user_reserves():
    from pathlib import Path
    from solders.pubkey import Pubkey
    from sniper.execution.pumpswap_buy import AMM_PROGRAM, parse_buy_event
    from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
    from sniper.services.memecoin_opportunity_live import program_data_logs
    from sniper.services import memecoin_slot_signal as signal

    fixtures = Path(__file__).parent / 'fixtures' / 'memecoin'
    exact = json.loads((fixtures / 'pumpswap_v2_receipts_20261008.json').read_text())
    chain = json.loads((fixtures / 'pump_amm_chain_buys_20261016.json').read_text())
    matched, distinct_post = [], 0
    for tx in (exact['v2_second']['tx'], chain['apiANK']['transaction'],
               chain['starman']['transaction']):
        keys = [k['pubkey'] if isinstance(k, dict) else k
                for k in tx['transaction']['message']['accountKeys']]
        pre = {b['accountIndex']: b for b in tx['meta']['preTokenBalances']}
        post = {b['accountIndex']: b for b in tx['meta']['postTokenBalances']}
        for _, line in program_data_logs(tx['meta']['logMessages'], AMM_PROGRAM):
            raw = anchor_event_bytes(line)
            if raw[:8] != bytes.fromhex('67f4521f2cf57777'):
                continue
            event = parse_buy_event(line)
            index = keys.index(event['user_base_token_account'])
            if index not in pre:
                continue  # No absent-row inference: this checks exact existing-account balances.
            before, after = pre[index], post[index]
            amount = int(before['uiTokenAmount']['amount'])
            assert before['owner'] == after['owner'] == event['user']
            assert before['mint'] == after['mint']
            assert event['user_base_token_reserves'] == amount
            assert signal._amm(raw, bytes(Pubkey.from_string(event['user'])))['source_pre_atomic'] == amount
            matched.append(amount)
            distinct_post += amount != int(after['uiTokenAmount']['amount'])
    assert matched == [9_245_703_100_817, 35_117_714_595_287, 43_580_661_475_624, 7_214_728_526_571]
    assert distinct_post == 3
