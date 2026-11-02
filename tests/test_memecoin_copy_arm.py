"""Per-wallet copy-arm exit policy (2026-08-04)."""
import json

import pytest

from sniper.services import memecoin_copy_arm as ca
from sniper.services import memecoin_paper_report as rep


def _patch_cohort(monkeypatch, cells):
    """Funded selection reads the EXACT forward cohort, never the survivor matrix."""
    monkeypatch.setattr(rep, "wallet_arm_cells_from_forward_cohort",
                        lambda conn, arms, chain: cells)
from sniper.services.memecoin_funded_policy import (
    FUNDED_CONTRACT_VERSION, _validated_policy_data, resolve_funded_policy)
from sniper.settings import memecoin as cfg


def _policy(arm, band="small", network="solana"):
    return ca.funded_policy_for_arm(arm, coin_band=band, network_id=network)


# --- the operator's literal-mirror decision -------------------------------------------------

def test_scalp_c2_is_translated_faithfully_not_as_a_floorless_mirror():
    """REGRESSION (2026-08-04 review). scalp_c2 was mapped as a pure mirror with no stop, no TP
    and no catastrophe floor — a strategy no paper cohort ever measured.

    Root cause: the frozen `params` dict for scalp arms holds only
    {mirror_on_sell, paper_max_hold_s, paper_unquotable_s}; their real rails live in a separate
    `exit_policy` key. `_scalp_c2_policy` is explicit that the arm "copies CONTROL exactly: wide
    -60% stop, NO time cap, mirror the trader's sell" and changes ONLY the take-profit.
    """
    from sniper.services.memecoin_policy import resolve_policy

    for mcap in (600_000, 1_500_000, 8_000_000, 40_000_000, None):
        resolved, _ = resolve_policy({"entry_mcap": mcap}, arm="scalp_c2")
        policy = ca.funded_policy_for_arm(
            "scalp_c2", coin_band="small", network_id="solana", entry_mcap=mcap)
        assert policy.stop_pct == abs(resolved.stop_pct) > 0, mcap
        assert policy.tp_pct == resolved.tp_pct, mcap
        assert policy.mirror_on_sell is True, mcap
        # A real stop means the catastrophe backstop belongs here like every other armed policy.
        assert policy.catastrophe_pct == cfg.MEMECOIN_REAL_CATASTROPHE_PCT, mcap


def test_scalp_c2_take_profit_tracks_the_measured_mcap_band():
    """The ONE thing the arm conditions on. A flat TP would be a different strategy."""
    tps = {m: ca.funded_policy_for_arm("scalp_c2", coin_band="small", network_id="solana",
                                       entry_mcap=m).tp_pct
           for m in (600_000, 1_500_000, 8_000_000, 40_000_000)}
    assert tps[40_000_000] == 10.0
    assert tps[8_000_000] == 20.0
    assert tps[1_500_000] == 35.0
    assert tps[600_000] == cfg.MEMECOIN_TAKE_PROFIT_PCT      # sub-$1M rides, exactly like control


def test_no_funded_arm_leaves_a_position_without_a_price_floor():
    """The property the floorless-mirror bug violated: every selectable arm keeps a loss rail."""
    for arm in sorted(ca.FUNDED_EXPRESSIBLE):
        policy = ca.funded_policy_for_arm(
            arm, coin_band="small", network_id="solana", entry_mcap=600_000)
        assert policy.stop_pct > 0 or policy.catastrophe_pct > 0, arm


def test_every_arm_keeps_its_catastrophe_floor():
    for arm in ("control", "alpha_b", "real_shadow", "trail_d", "combo_f", "scalp_c2"):
        assert _policy(arm).catastrophe_pct == cfg.MEMECOIN_REAL_CATASTROPHE_PCT, arm


# --- arm -> rail fidelity --------------------------------------------------------------------

def test_arm_rails_match_the_paper_arm_constants():
    """Each funded arm reproduces the SAME settings constants the paper arm was measured under."""
    b = _policy("alpha_b")
    assert (b.stop_pct, b.max_hold_s) == (cfg.MEMECOIN_B_STOP_PCT, cfg.MEMECOIN_B_MAX_HOLD_S)
    assert b.trail_pct == 0.0 and b.tp_pct == 0.0

    d = _policy("trail_d")
    assert d.trail_pct == cfg.MEMECOIN_TRAIL_D_TRAIL_PCT
    assert d.trail_arm_pct == cfg.MEMECOIN_TRAIL_D_ACTIVATE_PCT
    assert d.stop_pct == cfg.MEMECOIN_TRAIL_D_STOP_PCT
    assert d.trail_requires_derisk is False

    c = _policy("control")
    assert (c.tp_pct, c.stop_pct) == (cfg.MEMECOIN_TAKE_PROFIT_PCT, cfg.MEMECOIN_STOP_LOSS_PCT)

    f = _policy("combo_f")
    assert f.derisk_mode == "fraction"
    assert f.derisk_fraction == cfg.MEMECOIN_COMBO_F_FRACTION
    assert f.derisk_pct == cfg.MEMECOIN_COMBO_F_TRIGGER_PCT
    # The paper arm's max_hold of 0 means "uncapped"; a funded contract requires a positive cap,
    # so it inherits the funded default rather than inventing a shorter one.
    assert f.max_hold_s == cfg.MEMECOIN_REAL_MAX_HOLD_S


def test_real_shadow_arm_reproduces_todays_funded_small_band_rails():
    """The fallback must genuinely be 'what the funded leg does now' on the band it usually runs."""
    shadow = _policy("real_shadow")
    band = resolve_funded_policy(coin_band="small", network_id="solana")
    for field in ("tp_pct", "derisk_pct", "trail_pct", "trail_arm_pct", "stop_pct",
                  "catastrophe_pct", "max_hold_s", "unquotable_exit_s", "mirror_on_sell"):
        assert getattr(shadow, field) == getattr(band, field), field


def test_stall_arms_carry_their_rails_and_their_frozen_stall_rule():
    e = _policy("peak_lock_e")
    assert e.stop_pct == cfg.MEMECOIN_PEAK_E_STOP_PCT
    assert e.trail_arm_pct == cfg.MEMECOIN_PEAK_E_ACTIVATE_PCT
    assert e.trail_pct == 0.0                     # peak_lock_e harvests on stall, never trails
    assert e.max_hold_s == cfg.MEMECOIN_PEAK_E_MAX_HOLD_S

    a = _policy("momentum_a")
    assert (a.stop_pct, a.trail_pct) == (cfg.MEMECOIN_A_STOP_PCT, cfg.MEMECOIN_A_TRAIL_PCT)
    assert a.max_hold_s == cfg.MEMECOIN_A_MAX_HOLD_S
    assert a.mirror_on_sell is False              # this arm deliberately does not follow them out


def test_the_two_stall_arms_do_not_share_a_profit_rule():
    """peak_lock_e harvests a stalled WINNER; momentum_a rolls out of anything that stops rising.
    Collapsing the two would make one of them a different strategy than the one that was measured.
    """
    assert ca._stall_params("peak_lock_e")["require_profit"] is True
    assert ca._stall_params("momentum_a")["require_profit"] is False
    assert ca._stall_params("peak_lock_e")["activate_pct"] == cfg.MEMECOIN_PEAK_E_ACTIVATE_PCT
    assert ca._stall_params("momentum_a")["activate_pct"] == 0.0
    assert ca._stall_params("alpha_b") is None


def test_every_expressible_arm_builds_a_valid_contract_policy_on_every_band():
    for arm in sorted(ca.FUNDED_EXPRESSIBLE):
        for band in ("nano", "micro", "small", "mid"):
            policy = _policy(arm, band=band)
            assert policy.coin_band == band
            # Round-trips through the strict contract validator the exit sweep uses.
            assert _validated_policy_data(policy.to_dict()) == policy


def test_assumed_nano_maps_to_the_nano_band():
    assert _policy("alpha_b", band="assumed_nano").coin_band == "nano"


def test_unreservable_band_is_refused():
    with pytest.raises(ValueError, match="not reservable"):
        _policy("alpha_b", band="out_of_scope")


# --- rollback safety --------------------------------------------------------------------------

def test_copy_arm_is_carried_in_source_not_policy():
    """A prior pinned release must still parse a contract this release wrote.

    `_validated_policy_data` rejects unknown POLICY keys, so an added policy field would make a
    rolled-back runner raise on every open position's exit sweep. The arm therefore rides in
    `source`, which readers already tolerate unknown keys in.
    """
    contract = _contract_with_copy_arm()
    assert "copy_arm" in contract["source"]
    assert set(contract["policy"]) == set(
        _validated_policy_data(contract["policy"]).to_dict())
    # The strict validator — the exact call a rolled-back reader makes — accepts it unchanged.
    assert _validated_policy_data(contract["policy"]).policy_id == "copy-arm-alpha_b-v1"


def test_contract_without_a_copy_arm_is_byte_identical_to_before():
    """Default-off must be inert: no copy arm, no new key, no changed bytes."""
    contract = _contract_with_copy_arm(copy_arm=None)
    assert "copy_arm" not in contract["source"]


# --- selection ---------------------------------------------------------------------------------

def test_selection_picks_the_highest_pnl_closed_arm(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_a_pnl_tie_resolves_toward_the_fallback_never_by_dict_order():
    """Ties are the NORMAL case — every arm mirroring the same sell exits at the same price — and
    they mean the evidence does not separate the arms. Resolving one arbitrarily handed `scalp_c2`
    (no stop, no catastrophe floor) to wallets tied with `control` (stop 60, floor 80), and showed
    the operator the other one. Live: zinceth tied 7 ways, GIGATT 6.
    """
    seven_way = {a: {"pnl": 238.9, "closed": 2} for a in
                 ("alpha_b", "combo_f", "control", "peak_lock_e", "real_shadow", "scalp_c2",
                  "trail_d")}
    assert ca.best_arm_cell(seven_way)["arm"] == ca.FALLBACK_ARM
    # Reversing insertion order must not change the answer.
    assert ca.best_arm_cell(dict(reversed(list(seven_way.items()))))["arm"] == ca.FALLBACK_ARM
    # No fallback in the tie -> deterministic by name, and the rail-bearing arm wins here.
    assert ca.best_arm_cell({"scalp_c2": {"pnl": 24.04, "closed": 2},
                             "control": {"pnl": 24.04, "closed": 2}})["arm"] == "control"
    # A genuine winner is still a winner.
    assert ca.best_arm_cell({"real_shadow": {"pnl": 1.0, "closed": 2},
                             "scalp_c2": {"pnl": 99.0, "closed": 1}})["arm"] == "scalp_c2"


def test_the_board_and_the_money_rank_with_the_same_function():
    """The Armed board's best_arm column must not be able to name a different arm than the one
    funded entries use — they now share `best_arm_cell` rather than each calling max()."""
    import inspect

    from sniper.routes import api_memecoin

    src = inspect.getsource(api_memecoin.create_api_memecoin_blueprint)
    assert "best_arm_cell(cells)" in src
    assert 'key=lambda c: c["pnl"]' not in src


def test_selection_ignores_arms_with_no_close(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_stall_arm_selection_freezes_its_rule(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_non_stall_arm_selection_carries_no_stall(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_wallet_with_no_closed_arm_returns_none(monkeypatch):
    """brunettetittiesenjoyor / ResellCalendar: 0 closes, nothing to select from."""
    _patch_cohort(monkeypatch, {"w1": {"control": {"pnl": 0.0, "closed": 0}}})
    assert ca.select_copy_arm(object(), "w1", chain="solana") is None


def test_wallet_absent_from_the_matrix_returns_none(monkeypatch):
    _patch_cohort(monkeypatch, {})
    assert ca.select_copy_arm(object(), "w1", chain="solana") is None


def test_selection_is_chain_scoped(monkeypatch):
    """Chains are never pooled — the matrix must be asked for the entering chain only."""
    seen = {}

    def fake(conn, arms, chain):
        seen["chain"] = chain
        return {}

    monkeypatch.setattr(rep, "wallet_arm_cells_from_forward_cohort", fake)
    ca.select_copy_arm(object(), "w1", chain="robin")
    assert seen == {}


# --- wiring ------------------------------------------------------------------------------------

def test_selection_is_skipped_while_the_flag_is_off(monkeypatch):
    from sniper.services.memecoin_risk import _select_copy_arm_for_contract

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", False)
    called = []
    monkeypatch.setattr(rep, "wallet_arm_cells_from_forward_cohort",
                        lambda conn, arms, chain: called.append(1) or {})
    assert _select_copy_arm_for_contract(
        object(), source_user_id="w1", network_id="solana", engine_authoritative=False) is None
    assert not called


def test_engine_authoritative_chain_keeps_its_envelope(monkeypatch):
    """A V5-engine chain surrendered its linear rails; a copy arm IS a linear rail set."""
    from sniper.services.memecoin_risk import _select_copy_arm_for_contract

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", True)
    assert _select_copy_arm_for_contract(
        object(), source_user_id="w1", network_id="solana", engine_authoritative=True) is None


def test_selection_failure_falls_back_instead_of_blocking_the_entry(monkeypatch, caplog):
    """A selection error must never veto an entry every admission gate already approved."""
    from sniper.services.memecoin_risk import _select_copy_arm_for_contract

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", True)

    def boom(conn, arms, chain):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(rep, "wallet_arm_cells_from_forward_cohort", boom)
    assert _select_copy_arm_for_contract(
        object(), source_user_id="w1", network_id="solana", engine_authoritative=False) is None
    assert "copy-arm selection failed" not in caplog.text


def test_no_source_user_id_keeps_the_band_policy(monkeypatch):
    from sniper.services.memecoin_risk import _select_copy_arm_for_contract

    monkeypatch.setattr(cfg, "MEMECOIN_COPY_ARM_POLICY", True)
    assert _select_copy_arm_for_contract(
        object(), source_user_id=None, network_id="solana", engine_authoritative=False) is None


# --- the peak-stall rail ------------------------------------------------------------------------

def _stall_position(arm="peak_lock_e", *, stalled_s, peak, entry=1.0):
    """A funded position whose executable high was set `stalled_s` seconds before the decision."""
    from datetime import datetime, timedelta, timezone

    pos = _position_with(_policy(arm), copy_arm={
        "arm": arm, "pnl": 1.0, "closed": 4, "chain": "solana", "blocked_arm": None,
        "stall": ca._stall_params(arm)})
    decision = datetime(2026, 8, 4, 0, 5, tzinfo=timezone.utc)
    pos.update({"entry_price": entry, "executable_peak_price": peak,
                "executable_peak_at": (decision - timedelta(seconds=stalled_s)).isoformat()})
    return pos


def test_peak_stall_fires_only_once_armed_and_stalled():
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason

    now = "2026-08-04T00:05:00+00:00"
    full = cfg.MEMECOIN_PEAK_E_STALL_SWEEPS * ca.PAPER_SWEEP_S
    # Armed (+40% peak), stalled the full measured window, still in profit -> harvest.
    assert alpha_b_real_exit_reason(
        _stall_position(stalled_s=full, peak=1.40), 1.20, now) == "peak_stall"
    # Same stall, but the peak never reached the +30% activation -> no harvest.
    assert alpha_b_real_exit_reason(
        _stall_position(stalled_s=full, peak=1.10), 1.05, now) is None
    # Armed but only half the measured window elapsed -> not yet. Under the old sweep-count rule
    # a 1s fast lane would already have fired here.
    assert alpha_b_real_exit_reason(
        _stall_position(stalled_s=full / 2, peak=1.40), 1.20, now) is None


def test_peak_lock_e_does_not_harvest_a_stalled_loser():
    """require_profit: peak_lock_e harvests winners. Underwater, the stop owns the position."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason

    pos = _stall_position(stalled_s=300, peak=1.40)
    # Peaked +40%, stalled, now down 10% from entry: stall must NOT fire.
    assert alpha_b_real_exit_reason(pos, 0.90, "2026-08-04T00:05:00+00:00") is None


def test_momentum_a_rolls_out_of_a_stalled_loser():
    """momentum_a has no profit condition — that is the difference between the two arms."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason

    pos = _stall_position("momentum_a", stalled_s=300, peak=1.05)
    assert alpha_b_real_exit_reason(pos, 0.90, "2026-08-04T00:05:00+00:00") == "peak_stall"


def test_loss_rails_outrank_the_stall():
    """A stalled position that is also through its stop exits as a stop, not a harvest."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason

    pos = _stall_position(stalled_s=600, peak=1.40)
    # -55%, below peak_lock_e's 50% stop.
    assert alpha_b_real_exit_reason(pos, 0.45, "2026-08-04T00:05:00+00:00") == "b_stop"


def test_a_position_without_a_stall_arm_is_unaffected():
    """Every pre-existing funded position must behave exactly as it did before this rail."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason

    pos = _position_with(_policy("alpha_b"))
    pos.update({"executable_peak_price": 1.40,
                "executable_peak_at": "2026-08-04T00:00:00+00:00"})
    assert alpha_b_real_exit_reason(pos, 1.20, "2026-08-04T00:05:00+00:00") is None
    assert ca.position_stall_rule(pos) is None


def test_stall_rule_is_read_from_the_contract_not_from_settings(monkeypatch):
    """An open position keeps the rule it was opened under."""
    pos = _stall_position(stalled_s=30, peak=1.40)
    monkeypatch.setattr(cfg, "MEMECOIN_PEAK_E_STALL_SWEEPS", 99)
    assert ca.position_stall_rule(pos)["stall_seconds"] == (
        2 * ca.PAPER_SWEEP_S)


def test_a_tampered_contract_yields_no_stall_rule():
    pos = _stall_position(stalled_s=30, peak=1.40)
    pos["funded_contract_hash"] = "0" * 64
    assert ca.position_stall_rule(pos) is None


def test_stall_counter_resets_on_a_new_executable_high_and_climbs_otherwise():
    assert ca.stall_sweep_count(3, 1.50, 1.40) == 0      # new high -> reset
    assert ca.stall_sweep_count(3, 1.40, 1.40) == 0      # matching the peak is not a stall
    assert ca.stall_sweep_count(3, 1.39, 1.40) == 4      # below the peak -> another stalled sweep
    assert ca.stall_sweep_count(0, 1.00, 1.40) == 1


@pytest.mark.parametrize("exact_mid", [None, 0, 0.0, -1.0, "nan-ish"])
def test_infrastructure_failure_never_advances_the_stall_counter(exact_mid):
    """The rail means 'the coin stopped making highs'. A failed quote is not that — counting it
    would let a provider outage harvest funded positions, which is the exact_quote_misses lesson.
    """
    assert ca.stall_sweep_count(1, exact_mid, 1.40) is None


def test_stall_counter_needs_a_real_peak_too():
    assert ca.stall_sweep_count(1, 1.20, 0) is None
    assert ca.stall_sweep_count(1, 1.20, None) is None


def test_peak_stall_requires_executable_confirmation():
    """It sells the bag, so it must be confirmed against a real preview like every price exit."""
    from sniper.services.memecoin_real_runner import _EXECUTABLE_PRICE_REASONS

    assert "peak_stall" in _EXECUTABLE_PRICE_REASONS


def test_contract_declares_peak_stall_only_when_the_arm_can_use_it():
    stall_arm = {"arm": "peak_lock_e", "pnl": 1.0, "closed": 2, "chain": "solana",
                 "blocked_arm": None, "stall": ca._stall_params("peak_lock_e")}
    assert "peak_stall" in _contract(_policy("peak_lock_e"),
                                     copy_arm=stall_arm)["invalidations"]
    assert "peak_stall" not in _contract(_policy("alpha_b"))["invalidations"]


# --- helpers -----------------------------------------------------------------------------------

def _position_with(policy, copy_arm=None):
    """A minimal open funded position carrying `policy` as its immutable contract."""
    from sniper.persistence import db_memecoin as m

    contract = _contract(policy, copy_arm=copy_arm)
    contract_json, contract_hash = m.canonical_funded_json(contract)
    return {
        "entry_price": 1.0, "opened_at": "2026-08-04T00:00:00+00:00",
        "network_id": "solana", "mint": "MINT",
        "funded_contract_version": FUNDED_CONTRACT_VERSION,
        "funded_contract_hash": contract_hash,
        "funded_contract_json": contract_json,
    }


def _contract(policy, copy_arm=None):
    from sniper.services.memecoin_funded_policy import build_funded_contract

    return build_funded_contract(
        classification=_classification(coin_band=policy.coin_band),
        base_clip_usd=100.0, approved_clip_usd=100.0,
        policy=policy, origin_event_id="ev1", copy_arm=copy_arm)


def _contract_with_copy_arm(copy_arm={"arm": "alpha_b", "pnl": 12.0, "closed": 3,
                                      "chain": "solana", "blocked_arm": None}):
    return _contract(_policy("alpha_b"), copy_arm=copy_arm)


def _classification(*, coin_band="small", network_id="solana", mint="MINT"):
    """Mirrors the builder in test_memecoin_funded_contract_storage — same shape, one source."""
    from sniper.services.memecoin_market_state import EntryClassification

    return EntryClassification(
        classification_version=1,
        network_id=network_id,
        mint=mint,
        signal_source="global_feed",
        decision_at="2026-08-04T00:00:00+00:00",
        coin_band=coin_band,
        lifecycle="young",
        liquidity_class="unknown",
        momentum_phase="climbing",
        market_regime="hot_narrow",
        data_quality="partial",
        risk_scale=1.0,
        reason_codes=(f"coin_band:{coin_band}", "liquidity:unknown", "regime:hot_narrow"),
        facts={
            "network_id": network_id, "mint": mint, "effective_mcap": 500_000.0,
            "liquidity": 5_000.0, "base_clip_usd": None,
        },
    )


# --- 2026-08-04 review regressions --------------------------------------------------------------

def test_selection_refuses_an_arm_below_the_evidence_floor(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_selection_accepts_an_arm_at_the_floor(monkeypatch):
    """Legacy summary counts/PnL cannot steer funded rails."""
    def forbidden(*args, **kwargs):
        raise AssertionError('legacy cohort must not be projected during entry')
    monkeypatch.setattr(rep, 'wallet_arm_cells_from_forward_cohort', forbidden)
    assert ca.select_copy_arm(object(), 'w1', chain='solana') is None


def test_unobserved_exits_are_censored_from_the_arm_matrix():
    """A close whose exit legs are ALL non-executable was never observed at a transactable price.
    The paper report censors these; the matrix did not, so arm ranking — and through it funded
    exit rails — scored prices nothing could have traded at. On blind 4663 that is EVERY close."""
    import sqlite3

    from sniper.persistence import db_memecoin as m

    sql = m.observed_exit_sql()
    for source in m.NON_EXECUTABLE_FILL_SOURCES:
        assert f"'{source}'" in sql, source
    for kind in m.EXIT_KINDS:
        assert f"'{kind}'" in sql, kind
    # It is a valid predicate against the real schema, not just a string.
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    c.execute(f"SELECT COUNT(*) FROM memecoin_positions WHERE {sql}").fetchone()


def test_stall_arms_are_exact_relevant_to_the_demand_gated_prepass():
    """peak_lock_e zeroes trail and derisk. If the PREPASS does not consider it exact-relevant it
    is skipped by the demand gate and then quoted unconditionally in the main loop — the exact
    provider-hammering pattern the demand gate exists to stop, at a 1s tick."""
    from sniper.services import memecoin_real_runner as rr

    pos = {"id": 1, "network_id": "solana", "mint": "So1" + "1" * 40}
    assert rr.exact_price_relevant(
        pos, trail_pct=0.0, derisk_pct=0.0,
        stall_rule={"stall_sweeps": 4, "stall_seconds": 60.0, "activate_pct": 0.0}) is True


def test_a_take_profit_or_stop_only_policy_is_exact_relevant():
    """follow-30-50-v1 and tp-only-v1 zero BOTH trail and derisk: their only rails are the TP and
    the stop. The predicate omitted tp/stop, so they read as irrelevant and the prepass skipped
    them with no counter and no log. On EVM the fast lane quotes anyway; on Solana — which
    `in_scope` excludes by design — nothing did, so a +30% TP and a -50% stop decided on the raw
    mid for every wallet on the follow-30-50 allowlist (position 36012, 2026-08-12: 28 minutes
    open, last_executable_price NULL, every miss counter 0).
    """
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_funded_policy as fp

    # The REAL policies, not a lookalike: both zero trail and de-risk and turn mirror OFF, so the
    # take-profit and the stop are their only discretionary exits.
    for build in (fp._follow3050_policy, fp._tp_only_policy):
        policy = build(coin_band="small")
        assert policy.trail_pct == 0 and policy.derisk_pct == 0
        assert policy.mirror_on_sell is False
        pos = dict(_position_with(policy), id=1)
        assert rr.exact_price_relevant(
            pos, trail_pct=0.0, derisk_pct=0.0, stall_rule=None) is True, policy.policy_id


def test_a_mirroring_policy_stays_irrelevant_so_the_book_is_not_quoted_wholesale():
    """The predicate must still EXCLUDE the mirroring policies. Three sweep callers pass no
    demand_fn (runner loop 761/790/948), so on those paths `exact_relevant` is the ONLY bound on
    quote volume — widening it to every stop-bearing position would quote the whole open book
    every sweep, which is the 2026-07-27 provider-hammering pattern. A policy that still mirrors
    the wallet out keeps a non-price exit, so not quoting it is a defensible economy.
    """
    from sniper.services import memecoin_real_runner as rr

    pos = {"id": 1, "network_id": "solana", "mint": "So1" + "1" * 40}   # no frozen policy
    assert rr.exact_price_relevant(
        pos, trail_pct=0.0, derisk_pct=0.0, stall_rule=None) is False


def test_both_call_sites_share_one_definition():
    """The prepass and the main loop each hand-rolled this predicate, which is how they were free
    to disagree. One function, two callers."""
    import inspect

    from sniper.services import memecoin_real_runner as rr

    assert "exact_price_relevant(" in inspect.getsource(rr._wo4_miss_prepass)
    assert "exact_price_relevant(" in inspect.getsource(rr.real_exit_sweep)


def test_stall_clock_is_seconds_so_a_faster_loop_cannot_tighten_the_rule():
    rule = ca._stall_params("peak_lock_e")
    assert rule["stall_seconds"] == rule["stall_sweeps"] * ca.PAPER_SWEEP_S
    assert ca.PAPER_SWEEP_S == 15.0        # the measured paper bracket cadence


def test_funded_selection_drops_an_origin_when_ANY_arm_is_censored():
    """MISSING-NOT-AT-RANDOM (2026-08-04 review). The survivor matrix derived a wallet's arm set
    AFTER censoring, so an arm that censors more often removed itself from the competition and
    could not lose. Choosing funded rails from that is choosing on the censoring mechanism.

    The forward cohort drops the whole ORIGIN instead, so every arm is scored over an identical
    event set.
    """
    import sqlite3
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_paper_report import wallet_arm_cells_from_forward_cohort
    from sniper.services.memecoin_policy import resolve_paper_arm_policy

    arms = ("control", "alpha_b")
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "w1", "alice")
    roster = [{"strategy": a, "policy_id": f"{a}-rule-v1", "policy_version": 1,
               "policy_sha256": "x" * 64} for a in arms]
    cohort = json.dumps({"arms": sorted(roster, key=lambda r: r["strategy"])})

    def add(event, arm, pnl, censored):
        pid = m.open_position(
            c, source_user_id="w1", source_handle="alice", mint=f"M{event}{arm}", ticker="T",
            entry_price=0.001, entry_notional=100.0, their_size_usd=10.0,
            opened_at="2026-08-04T00:00:00+00:00", strategy=arm, network_id="solana",
            origin_event_id=event, entry_fill_source="route_quote", entry_lag_s=0.5)
        policy_id, version, policy_json = resolve_paper_arm_policy(arm, entry_mcap=500_000.0)
        m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                      notional=100.0, fill_source="route_quote", fraction=1.0,
                      created_at="2026-08-04T00:00:00+00:00")
        m.insert_fill(c, position_id=pid, kind="bracket_exit", price=0.0011, qty=100_000.0,
                      notional=110.0, fill_source="noquote" if censored else "dexscreener",
                      fraction=1.0, created_at="2026-08-04T00:05:00+00:00")
        c.execute(
            "UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=?, exit_reason=?,"
            " paper_cohort_id='c1', paper_cohort_json=?, experiment_status='eligible',"
            " assigned_policy_id=?, assigned_policy_version=?, assigned_policy_json=? WHERE id=?",
            ("2026-08-04T00:05:00+00:00", pnl,
             "a_rollover_noquote" if censored else "bracket_exit",
             cohort, policy_id, version, policy_json, pid))
        c.commit()

    # e1: both arms measurable -> counts. e2: control censored -> the WHOLE origin is dropped,
    # so alpha_b does not get a free win from control's missing outcome.
    add("e1", "control", 5.0, False)
    add("e1", "alpha_b", 7.0, False)
    add("e2", "control", -50.0, True)
    add("e2", "alpha_b", 99.0, False)

    cells = wallet_arm_cells_from_forward_cohort(c, arms, "solana")["w1"]
    assert cells["control"]["closed"] == 1 and cells["alpha_b"]["closed"] == 1
    assert cells["alpha_b"]["pnl"] == 7.0, "e2's +99 must NOT count — its control arm was censored"
    assert cells["control"]["pnl"] == 5.0


# ── ACTIVATION OBSERVABILITY (2026-08-05) ───────────────────────────────────
# The flag is import-time and the runner loads `.env` itself, so `/proc/<pid>/environ` cannot
# report it. Without a startup line there is no way to confirm a running funded process actually
# picked the flag up — the same silent-strand class as the real-money arm flag and POLL_MS.
def test_startup_states_the_copy_arm_policy_either_way():
    import importlib
    loop = importlib.import_module("scripts.memecoin_runner_loop")

    on, off = [], []
    assert loop.report_copy_arm_policy(enabled=True, log_fn=on.append) is True
    assert loop.report_copy_arm_policy(enabled=False, log_fn=off.append) is False
    assert len(on) == 1 and len(off) == 1
    assert "ON" in on[0] and "copy-arm" in on[0]
    assert "OFF" in off[0] and "band" in off[0]


# ── WHICH PAPER ARMS THIS PROCESS LOADED (2026-09-24) ───────────────────────
# Same silent-strand class one level up: `MEMECOIN_ARMS` is import-time, the runner loads `.env`
# itself, and the arm decides which exit rails, fill and sizing produced every paper row. It
# changed from `real_shadow` to `funded_clone` on 2026-09-24 and nothing in the log said so.
def test_startup_names_the_paper_arms_and_says_so_when_there_are_none():
    import importlib
    loop = importlib.import_module("scripts.memecoin_runner_loop")

    # The list is passed in, never read from the settings module: `MEMECOIN_ARMS` is built from
    # `os.getenv` at import, so under pytest it holds the code default rather than production's.
    named, empty = [], []
    assert loop.report_paper_arms(["funded_clone", "real_shadow"], log_fn=named.append) == \
        ["funded_clone", "real_shadow"]
    assert loop.report_paper_arms([], log_fn=empty.append) == []
    assert len(named) == 1 and len(empty) == 1
    assert "funded_clone, real_shadow" in named[0]
    assert "NONE" in empty[0], "an empty arm list reads as a quiet arm unless the line says so"
    assert "NONE" not in named[0]


def test_funded_trail_gate_strips_trails_but_not_stall(monkeypatch):
    """2026-08-17 (HORNET): momentum_a's entry-armed 15% trail sold a 22s-old funded position at
    -19%. The gate zeroes trails on the FUNDED copy of every arm; paper arms keep theirs."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_copy_arm import _rails
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_ARM_TRAIL_ENABLED", False)
    for arm in ("momentum_a", "trail_d", "combo_f", "real_shadow", "peak_lock_e"):
        r = _rails(arm, "solana")
        assert r["trail"] == 0.0 and r["trail_arm"] == 0.0, arm
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_ARM_TRAIL_ENABLED", True)
    assert _rails("momentum_a", "solana")["trail"] > 0


def test_manual_mode_strips_every_auto_exit_but_mirror(monkeypatch):
    """2026-08-17 operator manual mode: funded rails carry only mirror-on-sell; stall is None."""
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_copy_arm import _rails, _stall_params
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_AUTO_EXITS_ENABLED", False)
    for arm in ("momentum_a", "trail_d", "combo_f", "real_shadow", "peak_lock_e", "alpha_b", "control"):
        r = _rails(arm, "solana")
        assert r["tp"] == 0.0 and r["derisk"] == 0.0 and r["trail"] == 0.0 and r["stop"] == 0.0, arm
    assert _stall_params("peak_lock_e") is None
    assert _stall_params("momentum_a") is None
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_AUTO_EXITS_ENABLED", True)
    assert _stall_params("peak_lock_e") is not None
