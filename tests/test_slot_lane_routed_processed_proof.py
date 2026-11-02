"""Routed Solana buys on processed proof (operator 2026-10-14: "whatever would get us the fastest
execution"). The Shyft processed transaction carries the same signer, payment and acquisition the
confirmed receipt does; with MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF on, the lane signs at processed
instead of waiting ~0.3 s for confirmation. Off (default) keeps the confirmed-only authority."""
import dataclasses
import time

import pytest

from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_slot_signal as S
from sniper.settings import memecoin as cfg


def _routed(monkeypatch, on):
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER, FIX
    from sniper.services.memecoin_shyft_signal import routed_buys
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF', on, raising=False)
    buys = routed_buys(wire_receipt(), {OWNER: ('uid', 'h')}, recv_monotonic=5.0, recv_utc='t',
                       head_slot=FIX['slot'], head_mono=5.0)
    assert len(buys) == 1
    return buys[0], OWNER, FIX


def test_off_keeps_processed_routed_buys_without_authority(monkeypatch):
    buy, _, _ = _routed(monkeypatch, False)
    assert buy.source_initiation is None


def test_on_attaches_the_receipt_proof_from_the_processed_transaction(monkeypatch):
    buy, owner, fix = _routed(monkeypatch, True)
    proof = buy.source_initiation
    assert proof['proof_version'] == 'direct-wallet-paid-v1'
    assert proof['payer'] == owner and proof['beneficiary'] == owner
    assert proof['payment_atomic'] == '500000000' and proof['route_payment_atomic'] == '500000000'
    assert proof['acquired_atomic'] == str(buy.token_delta_atomic)
    assert proof['block_number'] == fix['slot'] and proof['source_user_id'] == 'uid'
    assert proof['tx_hash'] == buy.signature


def _slot_buy(routed, **over):
    fields = dict(venue='pump_amm', source_user_id=routed.source_user_id, handle='h',
                  owner=routed.owner, signature=routed.signature, slot=routed.slot,
                  mint=routed.mint, pool='POOL', sol_amount=0,
                  token_amount=routed.token_delta_atomic, virtual_sol_reserves=0,
                  virtual_token_reserves=0, real_sol_reserves=0, real_token_reserves=0,
                  token_program=None, trade_event_log='', recv_monotonic=time.monotonic(),
                  recv_utc='t', routed=routed)
    fields.update(over)
    return S.SlotBuy(**fields)


def test_lane_admits_a_matching_processed_proof_and_labels_it_processed(monkeypatch):
    routed, _, _ = _routed(monkeypatch, True)
    out = L.with_processed_routed_proof(_slot_buy(routed))
    assert out.source_initiation == routed.source_initiation
    assert out.proof_provenance == 'processed_receipt'
    assert out.routed.usdc_spent_atomic == int(routed.source_initiation['route_payment_atomic'])
    assert L.proof_lane(out) == ('slot_lane_processed', 'processed_receipt')
    # A confirmed resume keeps its own lane.
    assert L.proof_lane(dataclasses.replace(out, proof_provenance=None)) == (
        'slot_lane_confirmed_receipt', 'confirmed_receipt')


@pytest.mark.parametrize('change', [
    dict(signature='x' * 88), dict(owner='OTHER'), dict(mint='OTHERMINT'), dict(slot=1),
    dict(token_amount=10 ** 30)])
def test_lane_refuses_a_processed_proof_that_does_not_match_the_buy(monkeypatch, change):
    routed, _, _ = _routed(monkeypatch, True)
    buy = _slot_buy(routed, **change)
    assert L.with_processed_routed_proof(buy) is buy


def test_lane_refuses_when_payment_differs_and_when_the_knob_is_off(monkeypatch):
    routed, _, _ = _routed(monkeypatch, True)
    wrong = dataclasses.replace(routed, usdc_spent_atomic=routed.usdc_spent_atomic + 1)
    buy = _slot_buy(wrong)
    assert L.with_processed_routed_proof(buy) is buy
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF', False)
    buy = _slot_buy(routed)
    assert L.with_processed_routed_proof(buy) is buy


def test_processed_proof_cost_is_bounded(monkeypatch):
    """Spec rule: no new provider call on the path; measured local CPU only."""
    from test_memecoin_shyft_confirmed import wire_receipt, OWNER, FIX
    from sniper.services.memecoin_shyft_signal import routed_buys
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_ROUTED_PROCESSED_PROOF', True, raising=False)
    u = wire_receipt()
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        routed_buys(u, {OWNER: ('uid', 'h')}, recv_monotonic=5.0, recv_utc='t',
                    head_slot=FIX['slot'], head_mono=5.0)
        times.append((time.perf_counter() - t0) * 1000)
    times.sort()
    print('routed_buys with proof ms p50=%.2f p90=%.2f' % (times[25], times[45]))
    assert times[45] < 50
