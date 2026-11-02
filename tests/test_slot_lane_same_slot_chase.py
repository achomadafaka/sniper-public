"""Case WALT (e0ee3456): a slot-lane buy landed at S+0 and reverted BuySlippageBelowMinBaseAmountOut, 1.08% short, because the signed min_out (expected x (1 - CHASE/100), expected from the source's own post-trade reserves)
did not allow for other buys in the same slot. MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT widens the OUTPUT haircut for a same-slot exposed send only. Edge cases are stated in the commit."""
import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.services import memecoin_slot_lane as lane
from sniper.settings import memecoin as cfg

import test_slot_lane_cpmm as cpmm
import test_slot_lane_damm_v2 as damm
import test_slot_lane_dbc as dbc
import test_slot_lane_launchlab as launchlab
import test_slot_lane_price_cap as pump

WALT_EXPECTED, WALT_MIN_AT_30, WALT_AVAILABLE = 132326053640, 92628237548, 91623752089


@pytest.fixture(autouse=True)
def _policy(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", 40.0, raising=False)


def test_the_knob_defaults_to_40_and_stays_a_raw_string_until_a_same_slot_send_parses_it():
    src = Path(cfg.__file__).read_text()
    assert 'os.getenv("MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", "40")' in src and "float(os.getenv(\"MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT\"" not in src
    assert pcb.output_haircut(True) == (40.0, "same_slot")


@pytest.mark.parametrize("bad", ["abc", "", "4o", "nan", "--"])
def test_an_unparsable_env_value_cannot_fail_the_settings_import_and_only_refuses_a_same_slot_send(bad):
    import os
    import subprocess
    import sys
    env = {**os.environ, "MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT": bad}
    code = ("import sniper.settings.memecoin as c, sniper.execution.pump_curve_buy as p\n"
            "print(repr(c.MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT))\n"
            "assert p.output_haircut(False)[1] == 'chase'\n"
            "try:\n    p.output_haircut(True); print('NO_REFUSAL')\nexcept ValueError as e:\n    print('refused', e)\n")
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, cwd=str(Path(cfg.__file__).resolve().parents[2]), timeout=120)
    assert out.returncode == 0, out.stderr[-400:]
    assert "refused invalid output slippage" in out.stdout and "NO_REFUSAL" not in out.stdout


def test_the_exact_walt_numbers_sign_at_the_same_slot_haircut_and_would_still_revert_at_the_chase_haircut():
    chase = pcb.capped_min_out(WALT_EXPECTED, 30, source_average_price_usd=None)[0]
    same = pcb.capped_min_out(WALT_EXPECTED, 30, source_average_price_usd=None, same_slot=True)[0]
    assert chase == WALT_MIN_AT_30 > WALT_AVAILABLE                      # reverted: min 92628237548 against 91623752089 available
    assert same == int(WALT_EXPECTED * 0.6) <= WALT_AVAILABLE            # fills at the 40 haircut


def test_without_a_same_slot_send_the_chase_haircut_is_unchanged():
    assert pcb.capped_min_out(1000, 30, source_average_price_usd=None) == (700, 700, 0)
    assert pcb.capped_min_out(1000, 30, source_average_price_usd=None, same_slot=False) == (700, 700, 0)


@pytest.mark.parametrize("bad", [-1.0, 100.0, 120.0, float("nan"), float("inf"), "x", None])
def test_an_invalid_same_slot_value_refuses_a_same_slot_send_and_never_touches_other_sends(bad, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", bad, raising=False)
    with pytest.raises(ValueError, match="output slippage"):
        pcb.capped_min_out(1000, 30, source_average_price_usd=None, same_slot=True)
    assert pcb.capped_min_out(1000, 30, source_average_price_usd=None)[0] == 700               # the chase path does not read the knob


def test_rollback_is_the_knob_at_the_chase_value(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", 30.0, raising=False)
    assert pcb.capped_min_out(1000, 30, source_average_price_usd=None, same_slot=True)[0] == 700


def test_legacy_mode_ignores_the_same_slot_flag(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    a = pcb.capped_min_out(100, 30, source_price_usd=2, source_cost_usdc_atomic=100_000_000, source_price_cap_pct=30, base_decimals=0, source_average_price_usd=1)
    b = pcb.capped_min_out(100, 30, source_price_usd=2, source_cost_usdc_atomic=100_000_000, source_price_cap_pct=30, base_decimals=0, source_average_price_usd=1, same_slot=True)
    assert a == b


def _buy(head, slot=500, seen=100.0, recv=100.03, routed=None):
    return SimpleNamespace(slot=slot, head_at_recv=head, head_at_recv_mono=seen, recv_monotonic=recv, routed=routed)


@pytest.mark.parametrize("head_minus_s,expected", [(-1, True), (0, True), (1, False), (-2, False), (-400, False), (3, False), (None, False)])
def test_buy_same_slot_needs_a_head_at_S_or_S_minus_one(head_minus_s, expected):
    assert pcb.buy_same_slot(_buy(None if head_minus_s is None else 500 + head_minus_s)) is expected


def test_a_stale_head_observation_is_not_a_same_slot_proof():
    assert pcb.buy_same_slot(_buy(499, seen=1.0, recv=100.0)) is False            # head 499 for slot 500 observed 99 s before the receipt (Zeta witness)
    assert pcb.buy_same_slot(_buy(499, seen=100.0, recv=100.9)) is False          # just past the two-slot bound
    assert pcb.buy_same_slot(_buy(499, seen=100.0, recv=100.8)) is True            # at the bound
    assert pcb.buy_same_slot(_buy(499, seen=100.5, recv=100.0)) is False          # a head stamped AFTER the receipt is not a measurement


def test_a_far_head_is_not_a_proof_even_when_fresh():
    assert pcb.buy_same_slot(_buy(1)) is False                                     # head 1 for slot 500, freshly observed (Zeta witness)


@pytest.mark.parametrize("seen,recv", [(None, 100.0), (100.0, None), (float("nan"), 100.0), (100.0, float("inf")), ("x", 100.0)])
def test_missing_or_malformed_provenance_stamps_are_false(seen, recv):
    assert pcb.buy_same_slot(_buy(499, seen=seen, recv=recv)) is False


def test_a_routed_pair_head_has_no_freshness_proof_so_it_keeps_the_chase_haircut():
    buy = SimpleNamespace(slot=500, head_at_recv=None, head_at_recv_mono=None, recv_monotonic=100.0, routed=SimpleNamespace(paired_head_slot=500))
    assert pcb.buy_same_slot(buy) is False


def test_the_helper_agrees_with_the_lanes_receipt_head_skew_when_the_head_is_fresh():
    buy = _buy(499)
    assert pcb.buy_same_slot(buy) is True and lane.receipt_head(buy)["head_at_recv_minus_s"] == -1 and lane.receipt_head(buy)["head_at_recv_age_ms"] == pytest.approx(30.0)


def _make(venue, head_minus_s):
    """(builder, buy, clip, snapshot) with the source slot and the receipt head set explicitly."""
    if venue == "curve":
        builder, buy = pump._curve_builder()
        snapshot, clip = SimpleNamespace(blockhash=pump.BLOCKHASH, last_valid_height=1, sol_usd=121.5), 175.0
    elif venue == "pumpswap":
        builder, buy, _event, _facts = pump._ps_builder(6)
        snapshot, clip = SimpleNamespace(blockhash=pump.BLOCKHASH, last_valid_height=1, sol_usd=175.0), 5.0
    elif venue == "cpmm":
        builder = cpmm.C.SlotLaneCpmmBuilder(user=cpmm.USER, rpc=cpmm._no_pool_reads, leg_fn=lambda: cpmm._leg(100.0), clock=lambda: 50.0)
        builder.pools = cpmm._cache(cpmm.SOL_EV)
        ev = cpmm.SOL_EV
        source = replace(cpmm._routed(ev), usdc_spent_atomic=round((ev.input_amount + ev.input_transfer_fee) * 0.1225), token_delta_atomic=ev.output_amount - ev.output_transfer_fee)
        buy, snapshot, clip = lane.cpmm_slot_buy(source), cpmm._snap(), 100.0
    else:
        builder = launchlab._builder(lambda _mint: launchlab._leg(100.0), launchlab.SOL_B)
        ev = launchlab._ev(launchlab.SOL_B)
        source = replace(launchlab._routed(launchlab.SOL_B), token_delta_atomic=ev.amount_out, usdc_spent_atomic=round(ev.amount_in * 0.1225))
        buy, snapshot, clip = lane.launchlab_slot_buy(source), launchlab._snap(), 100.0
    import dataclasses
    fields = {}
    if getattr(buy, "slot", None) is None:
        fields["slot"] = 500
    slot = fields.get("slot", getattr(buy, "slot", 500))
    fields["head_at_recv"] = None if head_minus_s is None else int(slot) + head_minus_s
    fields["recv_monotonic"] = 100.03                                              # a fresh head observation (30 ms before the receipt), as WALT's was (27 ms)
    fields["head_at_recv_mono"] = 100.0
    if head_minus_s is None and getattr(buy, "routed", None) is not None and dataclasses.is_dataclass(buy.routed):
        fields["routed"] = replace(buy.routed, paired_head_slot=None) if hasattr(buy.routed, "paired_head_slot") else buy.routed
    if dataclasses.is_dataclass(buy) and not isinstance(buy, type):
        buy = replace(buy, **fields)
    else:
        for k, v in fields.items():
            setattr(buy, k, v)
    return builder, buy, clip, snapshot


@pytest.mark.parametrize("venue", ["curve", "pumpswap", "cpmm", "launchlab"])
def test_each_native_builder_signs_the_wider_haircut_only_for_a_same_slot_send_and_traces_which_one(venue):
    b, buy, clip, snap = _make(venue, -1)
    msg, detail = b.build(buy, clip, snap)
    assert msg is not None, detail
    same = detail["pump_price_limit"]
    assert same["min_tokens_atomic"] == int(same["expected_tokens_atomic"] * 0.6) and same["output_slippage_pct"] == 40.0 and same["output_haircut"] == "same_slot"
    b, buy, clip, snap = _make(venue, 2)
    msg, detail = b.build(buy, clip, snap)
    assert msg is not None, detail
    later = detail["pump_price_limit"]
    assert later["min_tokens_atomic"] == int(later["expected_tokens_atomic"] * 0.7) and later["output_slippage_pct"] == 30.0 and later["output_haircut"] == "chase"
    assert same["expected_tokens_atomic"] == later["expected_tokens_atomic"]                           # only the haircut differs


@pytest.mark.parametrize("venue", ["curve", "pumpswap", "cpmm", "launchlab"])
def test_an_unknown_receipt_head_keeps_the_chase_haircut(venue):
    b, buy, clip, snap = _make(venue, None)
    msg, detail = b.build(buy, clip, snap)
    assert msg is not None, detail
    assert detail["pump_price_limit"]["output_haircut"] == "chase" and detail["pump_price_limit"]["output_slippage_pct"] == 30.0


@pytest.mark.parametrize("venue", ["curve", "pumpswap", "cpmm", "launchlab"])
def test_an_invalid_knob_refuses_a_same_slot_build_before_signing(venue, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SAME_SLOT_CHASE_MAX_PCT", float("nan"), raising=False)
    b, buy, clip, snap = _make(venue, 0)
    assert b.build(buy, clip, snap)[0] is None


@pytest.mark.parametrize("venue", ["damm", "dbc"])
def test_damm_and_dbc_have_no_expected_output_haircut_and_still_refuse_in_this_mode(venue, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_DBC", "live")
    if venue == "damm":
        builder, _leg = damm._builder(damm.CLASSIC)
        buy, why = damm.D.damm_slot_buy(damm._routed(damm.CLASSIC), builder.pools)
        snapshot = SimpleNamespace(blockhash=damm.BLOCKHASH, last_valid_height=1, sol_usd=150.0)
    else:
        builder = dbc._builder(dbc._leg(pcb.WSOL_MINT, out=600_000_000))
        buy, snapshot = dbc._buy(dbc.SOL), dbc.SNAP
    import dataclasses
    buy = replace(buy, head_at_recv=int(buy.slot) - 1) if dataclasses.is_dataclass(buy) else buy
    assert builder.build(buy, 100.0, snapshot) == (None, "output_quote_unavailable")


def test_every_capped_min_out_call_site_passes_same_slot_explicitly():
    root = Path(pcb.__file__).parent
    missing = []
    for name in ("pump_curve_buy.py", "pumpswap_buy.py", "raydium_cpmm_buy.py", "raydium_launchlab_buy.py"):
        for node in ast.walk(ast.parse((root / name).read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "capped_min_out":
                if "same_slot" not in {k.arg for k in node.keywords}:
                    missing.append((name, node.lineno))
    assert not missing, missing
