"""Micro-cap ladder policy (2026-07-16).

Signal-outcome evidence: micro winners peak FAST (p25=15m, median <=1h), ~half of hit20 winners
round-trip to/below entry, yet 30% of the 250-500K band runs past +100% (avg MFE 118%). A fixed
TP caps the tail; pure ride round-trips half the winners. The ladder does neither:

  entry -> derisk at +50% (recover the entry stake, house money)
        -> remainder rides with NO TP, protected by a 50%-giveback trail from peak
        -> position-scoped -60% stop (covers 4663 micros without touching the chain-wide
           no-stop decision), 7d timecap unchanged (scalp_c lesson: tight timecaps kill).

Assigned automatically (override source='micro', outranks the tp-ab 'ride' row) when the entry
was assumed-micro or resolved mcap < MEMECOIN_REAL_MICRO_MCAP_USD. Hardening: at most
MEMECOIN_REAL_MICRO_MAX_CONCURRENT open micro positions. Pre-registered read: >=30 closes,
PnL + drop-top-1 vs the A/B arms."""
import sqlite3
import types

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# ---------- trailing rail on the real exit engine ---------------------------------------------


def _pos(mid=None, *, entry=1.0, peak=None, derisked=True):
    return {"id": 1, "entry_price": entry, "entry_notional": 50.0, "network_id": "solana",
            "opened_at": "2026-07-16T12:00:00Z",
            "derisked_at": "2026-07-16T12:30:00Z" if derisked else None,
            "peak_price": peak, "executable_peak_price": peak,
            "stuck_bag": 0, "last_quote_ts": None}


def test_trail_fires_after_derisk_when_giveback_exceeds_the_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    ov = {"tp_pct": 0.0, "derisk_pct": 50.0, "trail_pct": 50.0, "sl_pct": 60.0}
    now = "2026-07-16T13:00:00Z"
    # peak 3.0 (=+200%), floor = 1.5; mid 1.4 gave back >50% of peak -> trail exit
    assert R.alpha_b_real_exit_reason(_pos(1.4, peak=3.0), 1.4, now, override=ov) == "real_trail"
    # mid 1.6 still above the floor -> ride on
    assert R.alpha_b_real_exit_reason(_pos(1.6, peak=3.0), 1.6, now, override=ov) is None


def test_trail_is_armed_only_after_derisk(monkeypatch):
    """Before the entry stake is recovered the ladder's protection is the STOP, not the trail —
    a trail floor computed off an early small peak would sit below the stop and add nothing."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    ov = {"tp_pct": 0.0, "derisk_pct": 50.0, "trail_pct": 50.0, "sl_pct": 60.0}
    now = "2026-07-16T13:00:00Z"
    # not derisked yet: +20% peak, mid back to +2% -> NOT a trail exit (and not a stop either)
    assert R.alpha_b_real_exit_reason(_pos(1.02, peak=1.2, derisked=False), 1.02, now,
                                      override=ov) is None


def test_trail_needs_a_real_peak(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    ov = {"tp_pct": 0.0, "derisk_pct": 50.0, "trail_pct": 50.0}
    now = "2026-07-16T13:00:00Z"
    assert R.alpha_b_real_exit_reason(_pos(1.4, peak=None), 1.4, now, override=ov) is None


def test_real_trail_is_executable_confirmed_like_other_rails():
    c = _conn()
    pid = m.open_position(
        c, source_user_id="u", source_handle="h", mint="M", ticker="T",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at="2026-07-16T12:00:00Z", strategy="alpha_b",
        execution_mode="real", network_id="solana",
    )
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at="2026-07-16T12:00:00Z")
    pos = m.get_position(c, pid)
    pos["peak_price"] = 2.0
    pos["executable_peak_price"] = 2.0
    ov = {"tp_pct": 0.0, "derisk_pct": 0.0, "trail_pct": 30.0}
    allowed, detail = R.executable_exit_allowed(
        c, pos, "real_trail", {"expected_usdc": 65.0}, override=ov)
    assert allowed is True
    assert detail["code"] == "threshold_confirmed"
    # A phantom display tick below the 1.40 trail floor cannot force an exit when the exact
    # executable quote still values the remaining bag at 1.50/token.
    allowed, detail = R.executable_exit_allowed(
        c, pos, "real_trail", {"expected_usdc": 75.0}, override=ov)
    assert allowed is False
    assert detail["executable_price"] == pytest.approx(1.5)
    # and a dead preview still refuses (never a blind send)
    allowed, detail = R.executable_exit_allowed(c, pos, "real_trail", None, override=ov)
    assert allowed is False


# ---------- micro override assignment ----------------------------------------------------------


def test_assign_micro_policy_for_assumed_micro_fill(monkeypatch):
    """Assumed-micro (unknown mcap = minutes-old) lands in the NANO band since 2026-07-17."""
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=None, assumed_micro=True)
    ov = m.effective_exit_override(c, 7)
    assert ov["source"] == "micro"
    assert ov["tp_pct"] == pytest.approx(0.0)
    assert ov["derisk_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_DERISK_PCT)
    assert ov["trail_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_TRAIL_PCT)
    assert ov["sl_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_SL_PCT)


def test_assign_micro_policy_for_resolved_small_mcap(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=180_000.0, assumed_micro=False)
    assert m.effective_exit_override(c, 7)["source"] == "micro"


def test_assign_micro_policy_skips_non_micro_and_disabled(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=800_000.0, assumed_micro=False)
    assert m.effective_exit_override(c, 7) is None
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", False)
    R.assign_micro_policy(c, 8, eff_mc=None, assumed_micro=True)
    assert m.effective_exit_override(c, 8) is None


def test_micro_outranks_a_historical_ride_row(monkeypatch):
    """Historical tp-ab 'ride' rows survive in the audit trail (the A/B closed 2026-07-21);
    a micro assignment must still outrank one on a position that carries it."""
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    m.set_position_exit_override(c, 7, "ride", tp_pct=0.0, derisk_pct=60.0,
                                 actor="system", note="tp-vs-derisk-ride A/B v1 (2026-07-16)")
    R.assign_micro_policy(c, 7, eff_mc=None, assumed_micro=True)
    assert m.effective_exit_override(c, 7)["source"] == "micro"


# ---------- hardening: micro concurrency cap ---------------------------------------------------


def _open_micro(c, mint):
    pid = m.open_position(c, source_user_id="w", source_handle="w", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=50.0, entry_fill_source="quote",
                          entry_lag_s=1.0, their_size_usd=100.0,
                          opened_at="2026-07-16T12:00:00Z", execution_mode="real",
                          network_id="solana", strategy="alpha_b")
    m.set_position_exit_override(c, pid, "micro", tp_pct=0.0, derisk_pct=50.0,
                                 trail_pct=50.0, sl_pct=60.0, actor="system")
    return pid


def test_open_micro_position_count(monkeypatch):
    c = _conn()
    _open_micro(c, "M1"); _open_micro(c, "M2")
    closed = _open_micro(c, "M3")
    m.close_position(c, closed, "2026-07-16T13:00:00Z", 1.0, exit_reason="real_trail")
    assert m.open_micro_position_count(c) == 2


def test_mid_band_does_not_consume_nano_micro_concurrency_cap(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    pid = m.open_position(
        c, source_user_id="w", source_handle="w", mint="MID", ticker="MID",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="quote", entry_lag_s=1.0,
        their_size_usd=100.0, entry_mcap=1_500_000.0,
        opened_at="2026-07-17T12:00:00Z", execution_mode="real",
        network_id="solana", strategy="alpha_b",
    )
    assert R.assign_micro_policy(c, pid, eff_mc=1_500_000.0, assumed_micro=False) == "mid"

    assert m.open_micro_position_count(c) == 0


def test_micro_concurrency_cap_refuses_the_next_assumed_micro(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_MAX_CONCURRENT", 2)
    c = _conn()
    _open_micro(c, "M1"); _open_micro(c, "M2")
    assert R.micro_entry_capacity_ok(c) is False
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_MAX_CONCURRENT", 3)
    assert R.micro_entry_capacity_ok(c) is True
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_MAX_CONCURRENT", 0)   # 0 = cap off
    assert R.micro_entry_capacity_ok(c) is True


# ---------- band split: nano vs micro (2026-07-17) ---------------------------------------------
# Operator: "sub 50k, 50-150k … there's no one-fit-all strategy or safety gates." Nano coins
# (<$50K) have the thinnest pools and fastest round-trips -> earlier derisk, tighter trail, and
# a higher allowed entry impact (a 12% cap auto-blocked thin nano pools). LAUNCH GUESSES —
# recalibrated per band at >=30 closes each.


def test_nano_band_gets_its_own_rails(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=30_000.0, assumed_micro=False)
    ov = m.effective_exit_override(c, 7)
    assert ov["source"] == "micro"
    assert ov["derisk_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_DERISK_PCT)
    assert ov["trail_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_TRAIL_PCT)
    assert ov["sl_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_SL_PCT)


def test_assumed_micro_unknown_mcap_is_treated_as_nano(monkeypatch):
    """Triple-unresolvable = minutes-old = the smallest class. Most conservative rails apply."""
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=None, assumed_micro=True)
    ov = m.effective_exit_override(c, 7)
    assert ov["derisk_pct"] == pytest.approx(cfg.MEMECOIN_REAL_NANO_DERISK_PCT)


def test_micro_band_keeps_micro_rails(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=200_000.0, assumed_micro=False)
    ov = m.effective_exit_override(c, 7)
    assert ov["derisk_pct"] == pytest.approx(cfg.MEMECOIN_REAL_MICRO_DERISK_PCT)


def test_nano_impact_tier(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_IMPACT_CAP_PCT", 3.0)
    monkeypatch.setattr(
        cfg,
        "_IMPACT_CAP_TIERS",
        [(50_000.0, 15.0), (1_000_000.0, 12.0), (5_000_000.0, 8.0),
         (30_000_000.0, 6.0)],
    )
    assert cfg.entry_impact_cap_for_mcap(30_000) == pytest.approx(15.0)
    assert cfg.entry_impact_cap_for_mcap(200_000) == pytest.approx(12.0)


# ---------- mid band 1-2M: consensus-gated scalps (2026-07-17) ---------------------------------
# Operator thesis CONFIRMED on matured signals: 1-2M armed = 57% hit20 / 46% hit30 / MFE 43%
# (vs 2-5M armed: 20%/13%/0% hit50 — dead above 2M). Consensus is the driver: solo 20% hit20 vs
# 49% with >=1 prior buyer/60m. 58% of winners round-trip -> scalp ladder (derisk +30, trail 35).


def test_mid_band_gets_scalp_rails(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=1_500_000.0, assumed_micro=False)
    ov = m.effective_exit_override(c, 7)
    assert ov["source"] == "micro"
    assert ov["derisk_pct"] == pytest.approx(cfg.MEMECOIN_REAL_MID_DERISK_PCT)
    assert ov["trail_pct"] == pytest.approx(cfg.MEMECOIN_REAL_MID_TRAIL_PCT)


def test_ab_cohort_band_250k_to_1m_stays_unladdered(monkeypatch):
    """250K-1M keeps the running tp-vs-ride A/B — its pre-registered read must not be disturbed."""
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=600_000.0, assumed_micro=False)
    assert m.effective_exit_override(c, 7) is None


def test_above_mid_band_not_laddered(monkeypatch):
    c = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_POLICY_ENABLED", True)
    R.assign_micro_policy(c, 7, eff_mc=2_500_000.0, assumed_micro=False)
    assert m.effective_exit_override(c, 7) is None


def _consensus_hook(c, monkeypatch, prior, *, threshold=None):
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"], force=True)
    monkeypatch.setattr(R, "real_entry_hook", lambda *a, **k: {"status": "filled"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R.m, "prior_buyers_60m", lambda *a, **k: prior)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 2_000_000.0)
    if threshold is not None:
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", threshold)
    return R.make_real_hook(None, _VENUES := {"solana": object()},
                            now_fn=lambda: "2026-07-18T12:00:00+00:00",
                            trade_size=10.0, max_age_s=None,
                            blind_mcap_fn=lambda chain, mint: None)


def test_solo_entry_above_1m_proceeds_when_consensus_is_disabled(monkeypatch):
    c = _conn()
    hook = _consensus_hook(c, monkeypatch, prior=0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=1_500_000.0,
                               mint="SoMid", created_at="2026-07-17T00:00:00Z")
    assert hook(c, ev)["status"] == "filled"


def test_opt_in_consensus_refuses_solo_entry_above_1m(monkeypatch):
    c = _conn()
    hook = _consensus_hook(c, monkeypatch, prior=0, threshold=1_000_000.0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=1_500_000.0,
                               mint="SoMid", created_at="2026-07-17T00:00:00Z")
    res = hook(c, ev)
    assert res["status"] == "skipped_mcap"
    assert "consensus" in res["reason"]


def test_consensus_entry_above_1m_proceeds(monkeypatch):
    c = _conn()
    hook = _consensus_hook(c, monkeypatch, prior=1, threshold=1_000_000.0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=1_500_000.0,
                               mint="SoMid", created_at="2026-07-17T00:00:00Z")
    assert hook(c, ev)["status"] == "filled"


def test_consensus_mid_entry_passes_classification_context_to_funded_hook(monkeypatch):
    c = _conn()
    captured = {}
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["solana", "8453", "4663"], force=True)
    monkeypatch.setattr(
        R, "real_entry_hook",
        lambda *a, **k: (captured.update(k), {"status": "filled"})[1],
    )
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    monkeypatch.setattr(R.m, "prior_buyers_60m", lambda *a, **k: 1)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 2_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 1_000_000.0)
    hook = R.make_real_hook(
        None, {"solana": object()}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=10.0, max_age_s=None, blind_mcap_fn=lambda chain, mint: None,
    )
    ev = types.SimpleNamespace(
        side="buy", source_user_id="u", their_market_cap=1_500_000.0,
        mint="SoMid", created_at="2026-07-18T11:59:59+00:00",
    )

    assert hook(c, ev)["status"] == "filled"
    context = dict(captured["classification_context"])
    # E14 shadow on Solana (spec 2026-10-09 Solana pre-signal feed, step 0): the own-marks read holder,
    # peeked by the classifier and never waited on.
    holder = context.pop("momentum_own")
    assert set(holder) == {"future", "classified_mono", "peeked"}
    assert context == {
        "effective_mcap": 1_500_000.0,
        "assumed_micro": False,
        "consensus_n_60m": 1,
        "market_stats": None,
        "mcap_basis": "event",          # which stage priced the band (2026-09-27)
    }


def test_sub_1m_never_needs_consensus(monkeypatch):
    c = _conn()
    hook = _consensus_hook(c, monkeypatch, prior=0, threshold=1_000_000.0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=800_000.0,
                               mint="SoSmall", created_at="2026-07-17T00:00:00Z")
    assert hook(c, ev)["status"] == "filled"


def test_consensus_gate_fails_open_on_unreadable_prior_count(monkeypatch):
    """Data absence must never block (honeypot/mcap lesson) — a failed read is not zero buyers."""
    c = _conn()
    hook = _consensus_hook(c, monkeypatch, prior=None, threshold=1_000_000.0)
    ev = types.SimpleNamespace(side="buy", source_user_id="u", their_market_cap=1_500_000.0,
                               mint="SoMid", created_at="2026-07-17T00:00:00Z")
    assert hook(c, ev)["status"] == "filled"


# ---------- operator bare trail (2026-07-17: route no longer rejects trail_pct) ----------------


def test_bare_operator_trail_arms_immediately(monkeypatch):
    """A trail WITHOUT a derisk stage is a plain trailing stop — armed from entry. The ladder's
    staged trail (derisk_pct set) still arms only post-derisk (pinned above)."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    ov = {"trail_pct": 30.0}
    now = "2026-07-17T01:00:00Z"
    assert R.alpha_b_real_exit_reason(_pos(1.3, peak=2.0, derisked=False), 1.3, now,
                                      override=ov) == "real_trail"      # gave back >30% of peak
    assert R.alpha_b_real_exit_reason(_pos(1.5, peak=2.0, derisked=False), 1.5, now,
                                      override=ov) is None              # above the floor


def test_bare_operator_trail_does_not_inherit_future_default_tp_or_derisk(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 50.0)
    ov = {"trail_pct": 30.0}
    now = "2026-07-17T01:00:00Z"
    # +60% does not preempt the requested trail, and the trail is armed before a derisk fill.
    assert R.alpha_b_real_exit_reason(_pos(1.6, peak=2.0, derisked=False), 1.6, now,
                                      override=ov) is None
    assert R.alpha_b_real_exit_reason(_pos(1.3, peak=2.0, derisked=False), 1.3, now,
                                      override=ov) == "real_trail"
