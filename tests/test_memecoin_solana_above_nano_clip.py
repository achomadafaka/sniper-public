"""Solana clip ceiling 200 above nano (operator order 2026-10-08 ~14:00Z, via Alpha).

A Solana entry whose classification resolved the coin's mcap at $50K or more gets
MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO (200) as its Solana max clip, its global hard cap and its
per-wallet cap where that cap equals the global (full-cap wallets, proven cells). Nano, unknown and
unresolved keep 175. Unproven 50 and trial 20 stay. EVM is untouched. A ceiling, never a target.
Spec docs/superpowers/specs/2026-10-08-memecoin-solana-above-nano-clip-design.md.
"""
from __future__ import annotations

import inspect
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_wallet_caps as caps
from sniper.settings import memecoin as cfg

MINT = "So1AboveNanoMint"
FULL = "full-cap-wallet"
PROVEN = "proven-wallet"
UNPROVEN = "unproven-wallet"
NOW = "2026-10-08T14:00:00+00:00"


@pytest.fixture(autouse=True)
def _live_sizing(monkeypatch):
    """Live Solana sizing shape: bankroll, the 175 Solana ceiling, wallet caps on (global 175)."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda *a, **k: 1)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT_TOTAL", 6)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD", 175.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", 200.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", 50_000.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS", "", raising=False)
    monkeypatch.setattr(caps.CACHE, "raised_clip_snapshot", None, raising=False)
    monkeypatch.setattr(caps.CACHE, "operator_snapshot", (frozenset({FULL}), "ready", 0.0))
    monkeypatch.setattr(caps.CACHE, "clock", lambda: 100.0)
    monkeypatch.setattr(caps.CACHE, "snapshot", (
        100.0, {(PROVEN, "solana"): {"proven": True}}, "ready"))


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _classify(conn, mcap, *, network_id="solana", mint=MINT, assumed_micro=False):
    from sniper.services.memecoin_market_state import classify_funded_entry
    return classify_funded_entry(conn, network_id=network_id, mint=mint, decision_at=NOW,
                                 signal_source="onchain_watch", effective_mcap=mcap,
                                 assumed_micro=assumed_micro, market_stats={}, base_clip_usd=None,
                                 consensus_n_60m=None)


def _reserve(conn, uid, mcap, *, usdc=250.0, uid_for_ceiling=None, **kw):
    """A Solana reservation sized as the leg sizes it: the band's ceiling into both caps."""
    cls = _classify(conn, mcap)
    ceiling = risk.solana_clip_ceiling_usd(cls, "solana", uid_for_ceiling or uid)
    cap = caps.entry_cap_usd(uid, "solana", ceiling_usd=ceiling)
    decision, pid = risk.reserve_entry(
        conn, mint=MINT, source_user_id=uid, strategy=cfg.MEMECOIN_REAL_POLICY, day=NOW[:10],
        usdc_balance=usdc, sol_balance=5.0, now_iso=NOW, size_cap_usd=cap,
        clip_ceiling_usd=ceiling, **kw)
    assert decision.allowed and pid is not None, decision.reason
    return decision.approved_size_usdc


# ------------------------------------------------------------------ the band decides the ceiling

@pytest.mark.parametrize("mcap, band", [(50_000, "micro"), (56_632, "micro"), (300_000, "small"), (1_467_721, "mid"),
                                        (3_000_000, "out_of_scope")])
def test_a_coin_resolved_above_nano_gets_the_raised_ceiling(mcap, band):
    cls = _classify(_conn(), mcap)
    assert cls.coin_band == band
    assert risk.solana_clip_ceiling_usd(cls) == 200.0


@pytest.mark.parametrize("mcap, assumed", [(49_999, False), (35_851, False), (None, False),
                                           (None, True)])
def test_nano_unresolved_or_assumed_keeps_todays_ceiling(mcap, assumed):
    cls = _classify(_conn(), mcap, assumed_micro=assumed)
    assert cls.coin_band in ("nano", "out_of_scope", "assumed_nano")
    assert risk.solana_clip_ceiling_usd(cls) is None


def test_no_classification_and_an_evm_coin_keep_todays_ceiling():
    assert risk.solana_clip_ceiling_usd(None) is None
    cls = _classify(_conn(), 100_000, network_id="4663", mint="0x" + "ab" * 20)
    assert risk.solana_clip_ceiling_usd(cls) is None


@pytest.mark.parametrize("knob", [0.0, -5.0, float("inf"), float("nan")])
def test_the_knob_off_or_invalid_raises_nothing(monkeypatch, knob):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", knob)
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 100_000)) is None


@pytest.mark.parametrize("raw, value", [(None, 200.0), ("0", 0.0), ("250", 250.0)])
def test_the_knob_defaults_to_200(raw, value):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO"}
    if raw is not None:
        env["MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO"] = raw
    out = subprocess.run([sys.executable, "-c", "from sniper.settings import memecoin as c; "
                          "print(c.MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO)"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert float(out.stdout.strip()) == value, out.stderr[-400:]


# ------------------------------------------------------------------ the reservation sizes to it

def test_micro_coin_full_cap_wallet_bankroll_250_reserves_200():
    assert _reserve(_conn(), FULL, 56_632) == 200.0


def test_nano_coin_full_cap_wallet_bankroll_250_reserves_175():
    assert _reserve(_conn(), FULL, 35_851) == 175.0


def test_unknown_band_reserves_175():
    assert _reserve(_conn(), FULL, None) == 175.0


def test_a_proven_cell_follows_the_raised_global():
    assert _reserve(_conn(), PROVEN, 56_632) == 200.0


def test_an_unproven_wallet_on_a_micro_coin_stays_at_50():
    assert _reserve(_conn(), UNPROVEN, 56_632) == 50.0


def test_a_trial_clip_stays_at_its_clip():
    assert caps.entry_cap_usd(FULL, "solana", existing_cap=20.0, ceiling_usd=200.0) == 20.0


def test_the_ceiling_is_never_a_target():
    assert _reserve(_conn(), FULL, 56_632, usdc=180.0) == 180.0


def test_the_knob_off_reserves_175(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", 0.0)
    assert _reserve(_conn(), FULL, 56_632) == 175.0


def test_the_raise_never_lowers_a_larger_configured_ceiling(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD", 250.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", False)
    ctx = risk.build_memecoin_risk_context(_conn(), usdc_balance=300.0, sol_balance=5.0,
                                           day=NOW[:10], clip_ceiling_usd=200.0)
    assert ctx.trade_size_usd == 250.0


def test_a_disabled_ceiling_stays_disabled(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", False)
    ctx = risk.build_memecoin_risk_context(_conn(), usdc_balance=300.0, sol_balance=5.0,
                                           day=NOW[:10], clip_ceiling_usd=200.0)
    assert ctx.trade_size_usd == 300.0


def test_preflight_takes_the_ceiling():
    c = _conn()
    kw = dict(mint=MINT, day=NOW[:10], usdc_balance=250.0, sol_balance=5.0)
    assert risk.preflight_entry(c, clip_ceiling_usd=200.0, **kw).approved_size_usdc == 200.0
    assert risk.preflight_entry(c, **kw).approved_size_usdc == 175.0


def test_evm_sizing_is_unchanged_on_a_micro_coin(monkeypatch):
    from sniper.services import memecoin_evm_risk as evm_risk
    assert "clip_ceiling_usd" not in inspect.signature(evm_risk.build_evm_risk_context).parameters
    assert "clip_ceiling_usd" not in inspect.signature(evm_risk.reserve_entry_evm).parameters
    monkeypatch.setattr(cfg, "evm_max_clip_usd_for", lambda chain, **k: 175.0)
    ctx = evm_risk.build_evm_risk_context(_conn(), usdc_balance=250.0, native_balance=1.0,
                                          day=NOW[:10], network_id="4663")
    assert ctx.trade_size_usd <= 175.0


# ------------------------------------------------------------------ every Solana sizing caller

def _src(obj):
    return inspect.getsource(obj)


def test_the_solana_leg_passes_the_ceiling_to_both_hops():
    from sniper.services import memecoin_real_leg as leg
    src = _src(leg.real_entry)
    assert 'solana_clip_ceiling_usd(classification, "solana", getattr(ev, "source_user_id", None))' in src
    assert src.count("clip_ceiling_usd=_clip_ceiling") == 2


def test_the_hook_cap_passes_the_ceiling():
    from sniper.services import memecoin_real_runner as R
    src = _src(R.real_entry_hook)
    call = src.split("_size_cap = entry_cap_usd(", 1)[1].split("\n        if _size_cap", 1)[0]
    assert "ceiling_usd=solana_clip_ceiling_usd(classification, identity.network_id," in call
    assert 'getattr(ev, "source_user_id", None)))' in call


def test_the_slot_lane_raises_only_its_sol_funded_try():
    """The USDC leg is prefetched before the coin is known; a 200 reservation refuses a 175 leg."""
    from sniper.services import memecoin_slot_lane as lane
    src = _src(lane.SlotLane._reserve_and_execute)
    assert 'solana_clip_ceiling_usd(classification, "solana", buy.source_user_id)' in src
    sol_try = src.split('sol_args = (dict(funding_asset="sol"', 1)[1].split("if prefer_sol else {})", 1)[0]
    assert "clip_ceiling_usd=sol_raise" in sol_try and "ceiling_usd=sol_raise" in sol_try
    # the USDC try carries only the flat ceiling, the one the prefetched leg was sized with
    usdc_args = src.split("reserve_args = dict(", 1)[1].split("\n        # TRIAL", 1)[0]
    assert "clip_ceiling_usd=flat_ceiling" in usdc_args and "solana_clip_ceiling_usd" not in usdc_args
    # The SOL-short USDC fallback is exercised in test_memecoin_reservation_timing.
    assert "if trial is None and od is None else None)" in src
    # raised only when SOL and the buffer cap fund the raised clip; else today's SOL fill at 175
    assert "sol_raise = raise_wanted if prefer_sol and sol_funds(raise_wanted) else None" in src
    assert "prefer_sol = sol_venue and sol_funds(clip_guess)" in src


def test_the_early_quote_names_the_raised_clip_as_an_alternate(monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_arming as arming
    monkeypatch.setattr(arming, "real_entries_allowed", lambda: True)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    ev = SimpleNamespace(mint=MINT, signal_source="onchain_watch", source_user_id=FULL)
    clip, why, facts = R._early_quote_clip(ev, pre, path, with_alts=True)
    assert why is None and clip == 175.0
    alts = facts["alt_clips_fn"]()
    assert alts[0] == 200.0                    # first: the raised clip at market scale
    assert 131.25 in alts                      # kept: today's clip at the narrow regime scale


def test_the_early_quote_adds_no_alternate_when_the_ceiling_does_not_bind(monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_arming as arming
    monkeypatch.setattr(arming, "real_entries_allowed", lambda: True)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 0.75)
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    pre = SimpleNamespace(balances=lambda: (150.0, 1.0, False, 5.0))       # bankroll 150 < 175
    ev = SimpleNamespace(mint=MINT, signal_source="onchain_watch", source_user_id=FULL)
    clip, why, facts = R._early_quote_clip(ev, pre, path, with_alts=True)
    assert why is None and facts["alt_clips_fn"]() == []


# ------------------------------------------------------------------ displays

def test_the_wallet_map_shows_the_above_nano_ceiling():
    from sniper.services import memecoin_wallet_map as wm
    out = wm.funnel_clips(cfg)
    assert out["solana"]["full_max"] == 175.0
    assert out["solana"]["full_max_above_nano"] == 200.0
    assert out["4663"].get("full_max_above_nano") is None


def test_the_accounting_preview_shows_the_above_nano_clip(monkeypatch):
    """The dashboard's next-clip preview: today's base clip and, beside it, the above-nano one."""
    from test_memecoin_real_deploy_routes import _client
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)
    cl, _ = _client({"acct_stats_fn": lambda mints: {},
                     "acct_balances_fn": lambda: {"solana": {"cash_usd": 250.0, "native": 0.1}}})
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    assert (sol["clip_next"], sol["clip_next_above_nano"]) == (175.0, 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", 0.0)
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    assert (sol["clip_next"], sol["clip_next_above_nano"]) == (175.0, None)



# ------------------------------------------------------------------ threshold knob (operator ~15:00Z)
# MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD: default 50000 (the rule above); 0 = every Solana entry,
# so the slot lane's single prefetched USDC leg sizes at 200 and its reservations match it.

@pytest.fixture
def every_entry(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", 0.0)


@pytest.mark.parametrize("raw, value", [(None, 50_000.0), ("0", 0.0), ("100000", 100_000.0)])
def test_the_threshold_defaults_to_50000(raw, value):
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD"}
    if raw is not None:
        env["MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD"] = raw
    out = subprocess.run([sys.executable, "-c", "from sniper.settings import memecoin as c; "
                          "print(c.MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD)"],
                         env=env, capture_output=True, text=True, timeout=60)
    assert float(out.stdout.strip()) == value, out.stderr[-400:]


@pytest.mark.parametrize("mcap, assumed", [(35_851, False), (None, False), (None, True), (56_632, False)])
def test_at_threshold_0_every_solana_entry_gets_the_raise(every_entry, mcap, assumed):
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), mcap, assumed_micro=assumed)) == 200.0


def test_at_threshold_0_a_solana_path_without_a_classification_gets_it_and_evm_never(every_entry):
    assert risk.solana_clip_ceiling_usd(None, "solana") == 200.0
    assert risk.solana_clip_ceiling_usd(None, "4663") is None
    assert risk.solana_clip_ceiling_usd(None) is None
    evm = _classify(_conn(), 100_000, network_id="4663", mint="0x" + "ab" * 20)
    assert risk.solana_clip_ceiling_usd(evm, "4663") is None


def test_a_higher_threshold_moves_the_line(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", 100_000.0)
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 56_632)) is None
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 300_000)) == 200.0


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_an_invalid_threshold_is_the_default_never_wider(monkeypatch, bad):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", bad)
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 35_851)) is None
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 56_632)) == 200.0
    assert risk.solana_flat_clip_ceiling_usd() is None


def test_the_flat_ceiling_exists_only_at_threshold_0(monkeypatch):
    assert risk.solana_flat_clip_ceiling_usd() is None
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", 0.0)
    assert risk.solana_flat_clip_ceiling_usd() == 200.0
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO", 0.0)
    assert risk.solana_flat_clip_ceiling_usd() is None


def test_at_threshold_0_a_nano_coin_reserves_200(every_entry):
    assert _reserve(_conn(), FULL, 35_851) == 200.0
    assert _reserve(_conn(), FULL, None) == 200.0


def test_at_threshold_0_unproven_and_trial_stay_capped(every_entry):
    assert _reserve(_conn(), UNPROVEN, 35_851) == 50.0
    assert caps.entry_cap_usd(FULL, "solana", existing_cap=20.0,
                              ceiling_usd=risk.solana_flat_clip_ceiling_usd()) == 20.0


def test_at_threshold_0_the_slot_lane_leg_and_its_reservation_agree_at_200(every_entry):
    """leg_plan sizes the single prefetched USDC leg; the USDC reservation must land on the same
    clip, or the 90-100% leg check refuses it."""
    from sniper.services import memecoin_slot_lane as lane
    c = _conn()
    snap = SimpleNamespace(usdc_balance=250.0, sol_balance=5.0)
    roster = lane.expected_clip_usd(c, snap, NOW[:10])
    flat = risk.solana_flat_clip_ceiling_usd()
    decision, pid = risk.reserve_entry(
        c, mint=MINT, source_user_id=FULL, strategy=cfg.MEMECOIN_REAL_POLICY, day=NOW[:10],
        usdc_balance=250.0, sol_balance=5.0, now_iso=NOW, clip_ceiling_usd=flat,
        size_cap_usd=caps.entry_cap_usd(FULL, "solana", ceiling_usd=flat))
    assert roster == decision.approved_size_usdc == 200.0


def test_at_the_default_threshold_the_slot_lane_leg_stays_at_todays_clip():
    from sniper.services import memecoin_slot_lane as lane
    assert lane.expected_clip_usd(_conn(), SimpleNamespace(usdc_balance=250.0, sol_balance=5.0),
                                  NOW[:10]) == 175.0


def test_the_slot_lane_wallet_cap_and_ondemand_cap_take_the_flat_ceiling():
    from sniper.services import memecoin_slot_lane as lane
    src = _src(lane)
    assert 'wallet_cap = entry_cap_usd(buy.source_user_id, "solana", ceiling_usd=flat_ceiling)' in src
    assert 'cap = entry_cap_usd(buy.source_user_id, "solana",\n' in src or \
        'cap = entry_cap_usd(buy.source_user_id, "solana", ceiling_usd=solana_flat_clip_ceiling_usd())' in src


def test_at_threshold_0_the_early_quote_primary_is_200_and_no_raised_alternate(
        every_entry, monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_arming as arming
    monkeypatch.setattr(arming, "real_entries_allowed", lambda: True)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 0.75)
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    built = []
    real_build = risk.build_memecoin_risk_context
    monkeypatch.setattr(risk, "build_memecoin_risk_context",
                        lambda *a, **k: built.append(k.get("risk_scale")) or real_build(*a, **k))
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    ev = SimpleNamespace(mint=MINT, signal_source="onchain_watch", source_user_id=FULL)
    clip, why, facts = R._early_quote_clip(ev, pre, path, with_alts=True)
    assert why is None and clip == 200.0
    built.clear()
    assert facts["alt_clips_fn"]() == [150.0]          # only the regime alternate, at 200 x 0.75
    assert built == [0.75]                              # the raised envelope is not even built


def test_at_threshold_0_the_dashboard_shows_200_as_the_base_clip(every_entry, monkeypatch):
    from test_memecoin_real_deploy_routes import _client
    from sniper.services import memecoin_wallet_map as wm
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 5.0)
    cl, _ = _client({"acct_stats_fn": lambda mints: {},
                     "acct_balances_fn": lambda: {"solana": {"cash_usd": 250.0, "native": 0.1}}})
    sol = cl.get("/api/memecoin/real/accounting").get_json()["by_network"]["solana"]
    assert (sol["clip_next"], sol["clip_next_above_nano"]) == (200.0, None)
    out = wm.funnel_clips(cfg)["solana"]
    assert (out["full_max"], out["full_max_above_nano"]) == (200.0, None)



# ------------------------------------------------------------------ only listed wallets (operator ~14:20Z)
# MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS: unset = every wallet (per the threshold); set = only the
# listed wallets, still subject to the threshold. Its own list; sizing never reads the re-entry list.

def _listed(monkeypatch, raw, *, threshold=0.0):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD", threshold)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS", raw)
    monkeypatch.setattr(caps.CACHE, "raised_clip_snapshot", None)


def test_with_the_list_set_only_a_listed_wallet_gets_the_raise(monkeypatch):
    _listed(monkeypatch, FULL)
    assert risk.solana_clip_ceiling_usd(None, "solana", FULL) == 200.0
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) is None
    assert risk.solana_clip_ceiling_usd(None, "solana") is None              # no wallet: never widens
    assert _reserve(_conn(), FULL, 35_851, uid_for_ceiling=FULL) == 200.0
    assert _reserve(_conn(), PROVEN, 35_851, uid_for_ceiling=PROVEN) == 175.0


def test_the_list_still_obeys_the_threshold(monkeypatch):
    _listed(monkeypatch, FULL, threshold=50_000.0)
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 35_851), "solana", FULL) is None
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 56_632), "solana", FULL) == 200.0
    assert risk.solana_clip_ceiling_usd(_classify(_conn(), 56_632), "solana", PROVEN) is None


def test_an_unset_list_raises_every_wallet(monkeypatch):
    _listed(monkeypatch, "  ")
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) == 200.0
    assert risk.solana_flat_clip_ceiling_usd() == 200.0


def test_a_chain_scoped_entry_and_a_malformed_list(monkeypatch):
    _listed(monkeypatch, "%s@solana, %s@4663" % (FULL, PROVEN))
    assert risk.solana_clip_ceiling_usd(None, "solana", FULL) == 200.0
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) is None
    _listed(monkeypatch, "%s@SOLANA" % FULL)                                  # all malformed: nobody
    assert risk.solana_clip_ceiling_usd(None, "solana", FULL) is None
    assert risk.solana_flat_clip_ceiling_usd() is None


def test_with_the_list_set_the_flat_ceiling_and_the_slot_lane_leg_go_back_to_175(monkeypatch):
    from sniper.services import memecoin_slot_lane as lane
    _listed(monkeypatch, FULL)
    assert risk.solana_flat_clip_ceiling_usd() is None
    assert lane.expected_clip_usd(_conn(), SimpleNamespace(usdc_balance=250.0, sol_balance=5.0),
                                  NOW[:10]) == 175.0


def test_sizing_never_reads_the_reentry_list(monkeypatch):
    _listed(monkeypatch, FULL)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", PROVEN, raising=False)
    monkeypatch.setattr(caps.CACHE, "reentry_snapshot", None, raising=False)
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) is None


def test_the_raised_list_is_live_through_the_operator_refresh(monkeypatch, tmp_path):
    _listed(monkeypatch, "")
    (tmp_path / ".env").write_text("MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS=%s\n" % FULL)
    assert caps.CACHE.refresh_operator_overrides(root=tmp_path)
    assert risk.solana_clip_ceiling_usd(None, "solana", FULL) == 200.0
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) is None
    (tmp_path / ".env").unlink()
    assert not caps.CACHE.refresh_operator_overrides(root=tmp_path)          # failed: keeps the list
    assert risk.solana_clip_ceiling_usd(None, "solana", PROVEN) is None
    status = caps.CACHE.status()
    assert status["solana_raised_clip_wallet_ids"] == [FULL]


def test_every_caller_passes_the_wallet():
    from sniper.services import memecoin_real_leg as leg
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_slot_lane as lane
    assert 'solana_clip_ceiling_usd(classification, "solana", getattr(ev, "source_user_id", None))' in _src(leg)
    assert "solana_clip_ceiling_usd(classification, identity.network_id,\n" in _src(R.real_entry_hook) or \
        'solana_clip_ceiling_usd(classification, identity.network_id, getattr(ev, "source_user_id", None))' in _src(R.real_entry_hook)
    assert 'solana_clip_ceiling_usd(classification, "solana", buy.source_user_id)' in _src(lane)


def test_the_early_quote_primary_is_the_wallets_own_clip(monkeypatch, tmp_path):
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_arming as arming
    _listed(monkeypatch, FULL)
    monkeypatch.setattr(arming, "real_entries_allowed", lambda: True)
    monkeypatch.setattr(MS, "market_risk_scale", lambda *a, **k: 0.75)       # no regime alternate
    path = str(tmp_path / "ledger.db")
    c = sqlite3.connect(path)
    m.ensure_memecoin_tables(c)
    c.close()
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    clips = {}
    for uid in (FULL, PROVEN):
        ev = SimpleNamespace(mint=MINT, signal_source="onchain_watch", source_user_id=uid)
        clip, why, facts = R._early_quote_clip(ev, pre, path, with_alts=True)
        clips[uid] = (clip, facts["alt_clips_fn"]())
    assert clips[FULL] == (150.0, [])           # listed: 200 x 0.75, no raised alternate
    assert clips[PROVEN] == (131.25, [])        # unlisted: 175 x 0.75, no raised alternate either


# ------------------------------------------------------------------ slot lane: listed wallet, SOL short

def _slot(conn, tmp_path, monkeypatch, *, sol):
    """A live slot lane like test_slot_lane_scaled_sol's: a fixed $250 clip capped by wallet caps
    (175 global, raised 200), SOL at $100, the USDC leg prefetched at 175, the SOL buffer cap 200."""
    import contextlib
    import time
    from sniper.services import memecoin_slot_lane as lane
    from sniper.services import memecoin_source_initiation as initiation
    from sniper.execution import exec_lock
    monkeypatch.setattr(initiation, "source_initiation_refusal", lambda *_: None)
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    for name, value in (("MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0), ("MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0),
                        ("MEMECOIN_REAL_SIZING", "fixed"), ("MEMECOIN_REAL_SIZING_BY_CHAIN", {}),
                        ("MEMECOIN_TRADE_SIZE_USD", 250.0), ("MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 10_000.0)):
        monkeypatch.setattr(cfg, name, value, raising=False)
    record = {"reserve": [], "built": []}
    real_reserve = risk.reserve_entry

    def reserve(*a, **k):
        record["reserve"].append(k.get("funding_asset", "usdc"))
        return real_reserve(*a, **k)
    monkeypatch.setattr(risk, "reserve_entry", reserve)

    def build_fn(buy, clip, _snap, *, funding_lamports=None):
        record["built"].append((clip, funding_lamports))
        if funding_lamports is None:
            return "USDC_LEG_MESSAGE", {}
        return "NATIVE_MESSAGE", {"funding_asset": "sol", "funding_amount_atomic": funding_lamports,
                                  "target_mint": buy.mint}
    ln = lane.SlotLane(
        conn=conn, executor={"slot_execute_fn": lambda *_a, **_k: {"status": "filled"}}, build_fn=build_fn,
        snapshot_fn=lambda: lane.PrefetchSnapshot(
            sol_usd=100.0, usdc_balance=400.0, sol_balance=sol, blockhash="Hash1", last_valid_height=1,
            observed_mono={k: time.monotonic() for k in ("sol_usd", "balances", "blockhash")}),
        armed_ids_fn=lambda: {FULL, PROVEN}, kill_tripped_fn=lambda: False,
        now_iso_fn=lambda: "2026-09-26T20:30:00+00:00", lock_key="sol-test", mode="live",
        shadow_clip_fn=lambda: 175.0, sol_buffer_cap_usd=200.0, leg_scale_fn=lambda: 1.0)
    return ln, record


def _pump_buy(uid):
    import time
    return SimpleNamespace(source_user_id=uid, owner="Owner1", mint="MintPump1", signature="Sig1",
                           slot=450_000_000, sol_amount=2 * 10 ** 9, virtual_sol_reserves=30 * 10 ** 9,
                           virtual_token_reserves=10 ** 9 * 10 ** 6, recv_utc="2026-09-26T20:30:00+00:00",
                           recv_monotonic=time.monotonic())


def _trace(conn):
    import json
    return json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])["substage_events"]


@pytest.fixture
def ledger(tmp_path):
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    yield c
    c.close()


def test_a_listed_wallet_with_sol_for_200_reserves_200_in_sol(monkeypatch, tmp_path, ledger):
    _listed(monkeypatch, FULL)
    ln, rec = _slot(ledger, tmp_path, monkeypatch, sol=2.5)
    assert ln.handle(_pump_buy(FULL))["status"] == "filled"
    (clip, lamports), = rec["built"]
    assert rec["reserve"] == ["sol"] and clip == 200.0 and lamports > 0
    assert not any(e["name"] == "raised_clip_fallback" for e in _trace(ledger))


def test_a_listed_wallet_short_of_sol_for_200_takes_the_175_leg_and_the_trace_says_so(
        monkeypatch, tmp_path, ledger):
    _listed(monkeypatch, FULL)
    ln, rec = _slot(ledger, tmp_path, monkeypatch, sol=1.0)          # $100 of SOL: not even 175
    assert ln.handle(_pump_buy(FULL))["status"] == "filled"
    assert rec["reserve"] == ["usdc"] and rec["built"] == [(175.0, None)]   # the prefetched leg
    mark = next(e for e in _trace(ledger) if e["name"] == "raised_clip_fallback")
    assert mark["wanted_usd"] == 200.0 and mark["reason"] == "sol_short"


def test_a_listed_wallet_with_sol_for_175_only_falls_back_to_175_in_sol(monkeypatch, tmp_path, ledger):
    _listed(monkeypatch, FULL)
    ln, rec = _slot(ledger, tmp_path, monkeypatch, sol=1.9)          # $190: covers 175, not 200
    assert ln.handle(_pump_buy(FULL))["status"] == "filled"
    (clip, lamports), = rec["built"]
    assert rec["reserve"] == ["sol"] and clip == 175.0 and lamports > 0
    assert next(e for e in _trace(ledger) if e["name"] == "raised_clip_fallback")["reason"] == "sol_short"


def test_an_unlisted_wallet_takes_175_and_no_fallback_mark(monkeypatch, tmp_path, ledger):
    _listed(monkeypatch, FULL)
    ln, rec = _slot(ledger, tmp_path, monkeypatch, sol=2.5)
    assert ln.handle(_pump_buy(PROVEN))["status"] == "filled"
    (clip, _lamports), = rec["built"]
    assert clip == 175.0
    assert not any(e["name"] == "raised_clip_fallback" for e in _trace(ledger))


@pytest.mark.parametrize('source_mcap,scale,primary,alternates', [
    (60_000, 1.0, 200.0, [150.0]),
    (60_000, 0.75, 150.0, []),
    (49_999, 1.0, 175.0, [200.0, 131.25]),
    (None, 1.0, 175.0, [200.0, 131.25]),
    (float('nan'), 1.0, 175.0, [200.0, 131.25]),
])
def test_early_quote_covers_known_raised_and_reduced_clip_without_more_quotes(
        monkeypatch, tmp_path, source_mcap, scale, primary, alternates):
    from sniper.services import memecoin_market_state as MS
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_arming as arming
    monkeypatch.setattr(arming, 'real_entries_allowed', lambda: True)
    monkeypatch.setattr(MS, 'market_risk_scale', lambda *a, **k: scale)
    monkeypatch.setattr(cfg, 'MEMECOIN_FUNDED_REGIME_RISK_SCALE', 0.75)
    path = str(tmp_path / 'ledger.db')
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    pre = SimpleNamespace(balances=lambda: (300.0, 1.0, False, 5.0))
    ev = SimpleNamespace(mint=MINT, signal_source='onchain_watch', source_user_id=FULL,
                         their_market_cap=source_mcap)
    clip, why, facts = R._early_quote_clip(ev, pre, path, with_alts=True)
    assert why is None and clip == primary
    assert facts['alt_clips_fn']() == alternates
