"""Real exit path honors per-position overrides (operator/template) — the actuation.

The safety-critical property is DEFAULT-INERT: with no override, alpha_b_real_exit_reason and
executable_exit_allowed behave byte-identically to before, so deploying this changes nothing for a
position until an override is set on it. Operator overrides may loosen; custom mcap targets
require a crossing on the exact sell quote.
"""
import json
import sqlite3

import pytest

from sniper.services.memecoin_real_runner import (
    alpha_b_real_exit_reason,
    effective_real_thresholds,
    executable_exit_allowed,
)

NOW = "2026-07-14T00:00:00+00:00"


def _pos(entry=0.001):
    # opened_at == NOW => held 0 (no timecap); solana; not derisked
    return {"entry_price": entry, "opened_at": NOW, "network_id": "solana",
            "id": 1, "entry_notional": 50.0, "derisked_at": None, "stop_hits": 0}


def test_default_inert_uses_cfg_thresholds():
    # override=None => the cfg thresholds verbatim (whatever they are in this env) and an identical
    # reason to omitting the kwarg. This is the safety property: deploying changes nothing until an
    # override is set. (End-to-end default-inertness is also covered by the 52 real-path regressions.)
    from sniper.settings import memecoin as cfg
    tp_pct, tp_mcap, stop_mag, sl_mcap = effective_real_thresholds(_pos(), None)
    assert tp_pct == cfg.MEMECOIN_REAL_TP_PCT
    assert stop_mag == cfg.real_stop_pct_for_chain("solana")
    assert tp_mcap is None and sl_mcap is None
    assert (alpha_b_real_exit_reason(_pos(), 0.0013, NOW)
            == alpha_b_real_exit_reason(_pos(), 0.0013, NOW, override=None))


def test_manual_contract_skips_volume_death(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.services import memecoin_risk as risk
    from sniper.services import memecoin_volume_death as volume_death
    from sniper.services.memecoin_funded_policy import _manual_unmanaged_policy
    from sniper.services.memecoin_market_state import EntryClassification
    from sniper.settings import memecoin as cfg

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.start_entry_attempt(
        c, attempt_id="manual-attempt", decision_started_at=NOW,
        network_id="solana", mint="MANUAL",
    )
    classification = EntryClassification(
        classification_version=1, network_id="solana", mint="MANUAL",
        signal_source="manual", decision_at=NOW, coin_band="small", lifecycle="young",
        liquidity_class="balanced", momentum_phase="early", market_regime="normal",
        data_quality="complete", risk_scale=1.0, reason_codes=(),
        facts={"network_id": "solana", "mint": "MANUAL"},
    )
    contract = risk.prepare_funded_contract(
        c, classification=classification, network_id="solana", mint="MANUAL",
        attempt_id="manual-attempt", origin_event_id=None, base_clip_usd=50.0,
        approved_clip_usd=50.0, source_user_id="u",
        funded_policy=_manual_unmanaged_policy(coin_band="small"),
    )
    m.open_position(
        c, source_user_id="u", source_handle="u", mint="MANUAL", ticker="MANUAL",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0.0, opened_at=NOW, strategy=cfg.MEMECOIN_REAL_POLICY,
        execution_mode="real", network_id="solana", signal_source="manual", **contract,
    )
    monkeypatch.setattr(cfg, "MEMECOIN_VOLDEATH_ARMED", 1)
    monkeypatch.setattr(
        volume_death, "armed_exit_decision",
        lambda *_args, **_kwargs: (True, 60.0, "measured_state_still"),
    )
    exits = []
    runner.real_exit_sweep(
        c,
        {"solana": {
            "exit_fn": lambda *_args: exits.append("vol_death") or {"status": "closed"},
            "preview_exit_fn": lambda _pos: None,
        }},
        price_fn=lambda _mints: {("solana", "MANUAL"): {"price": 1.0}},
        now_fn=lambda: NOW,
    )

    assert exits == []


def test_operator_tp_pct_override():
    ov = {"tp_pct": 25.0, "sl_pct": None, "tp_target_mcap": None, "sl_target_mcap": None}
    assert alpha_b_real_exit_reason(_pos(), 0.0013, NOW, override=ov) == "real_tp"   # +30% >= 25
    ov2 = {"tp_pct": 50.0, "sl_pct": None, "tp_target_mcap": None, "sl_target_mcap": None}
    assert alpha_b_real_exit_reason(_pos(), 0.0013, NOW, override=ov2) != "real_tp"  # +30% < 50


def test_operator_sl_pct_override_can_tighten():
    ov = {"tp_pct": None, "sl_pct": -20.0, "tp_target_mcap": None, "sl_target_mcap": None}
    assert alpha_b_real_exit_reason(_pos(), 0.00075, NOW, override=ov) == "b_stop"   # -25% <= -20


def test_operator_sl_may_loosen_below_default():
    # a WIDER stop than the -50/-60 default: -25% must NOT stop (operator gave it room)
    ov = {"tp_pct": None, "sl_pct": -80.0, "tp_target_mcap": None, "sl_target_mcap": None}
    assert alpha_b_real_exit_reason(_pos(), 0.00075, NOW, override=ov) != "b_stop"   # -25% > -80
    tp_pct, tp_mcap, stop_mag, sl_mcap = effective_real_thresholds(_pos(), ov)
    assert stop_mag == 80.0   # loosened, not clamped


def test_mcap_target_tp_and_sl():
    ov_tp = {"tp_pct": None, "sl_pct": None, "tp_target_mcap": 5_000_000, "sl_target_mcap": None}
    assert alpha_b_real_exit_reason(_pos(), 0.0011, NOW, override=ov_tp, current_mcap=6_000_000) == "real_tp"
    assert alpha_b_real_exit_reason(_pos(), 0.0011, NOW, override=ov_tp, current_mcap=4_000_000) != "real_tp"
    ov_sl = {"tp_pct": None, "sl_pct": None, "tp_target_mcap": None, "sl_target_mcap": 100_000}
    assert alpha_b_real_exit_reason(_pos(), 0.0011, NOW, override=ov_sl, current_mcap=90_000) == "b_stop"


def test_executable_confirmation_honors_operator_tp():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    from sniper.persistence.db_memecoin import ensure_memecoin_tables
    ensure_memecoin_tables(c)
    pos = _pos()
    # projected proceeds = +30% (expected 65 on entry 50). Default cfg TP (60) would REJECT;
    # an operator tp_pct=25 override must ACCEPT — proving the override flows to the executor gate.
    preview = {"expected_usdc": 65.0}
    ov = {"tp_pct": 25.0, "sl_pct": None, "tp_target_mcap": None, "sl_target_mcap": None}
    allowed_ov, _ = executable_exit_allowed(c, pos, "real_tp", preview, override=ov)
    allowed_default, _ = executable_exit_allowed(c, pos, "real_tp", preview)
    assert allowed_ov is True
    assert allowed_default is False   # default cfg gate still requires the higher cfg TP


def test_mcap_field_does_not_bypass_percent_executable_recheck_when_target_not_crossed():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    from sniper.persistence.db_memecoin import ensure_memecoin_tables
    ensure_memecoin_tables(c)
    pos = _pos()

    tp = {"tp_pct": 25.0, "tp_target_mcap": 5_000_000, "sl_pct": None,
          "sl_target_mcap": None}
    assert executable_exit_allowed(
        c, pos, "real_tp", {"expected_usdc": 55.0}, override=tp,
        current_mcap=4_000_000,
    )[0] is False

    sl = {"tp_pct": None, "tp_target_mcap": None, "sl_pct": -20.0,
          "sl_target_mcap": 100_000}
    assert executable_exit_allowed(
        c, pos, "b_stop", {"expected_usdc": 45.0}, override=sl,
        current_mcap=200_000,
    )[0] is False


def test_crossed_market_mcap_cannot_authorize_an_uncrossed_sell_quote():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    from sniper.persistence.db_memecoin import ensure_memecoin_tables
    ensure_memecoin_tables(c)
    pos = _pos()
    ov = {"tp_pct": 0.0, "tp_target_mcap": 5_000_000, "sl_pct": None,
          "sl_target_mcap": None}
    assert executable_exit_allowed(
        c, pos, "real_tp", {"expected_usdc": 45.0}, override=ov,
        current_mcap=6_000_000,
    )[0] is False


def test_auto_template_uses_frozen_entry_mcap_on_dexscreener_blind_chain(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="0xBig", ticker="BIG",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, entry_mcap=80_000_000, opened_at=NOW, strategy=cfg.MEMECOIN_REAL_POLICY,
        execution_mode="real", network_id="4663",
    )
    monkeypatch.setattr(cfg, "MEMECOIN_AUTO_TEMPLATES", "on")
    contexts = {"4663": {
        "exit_fn": lambda *args, **kwargs: pytest.fail("flat price must not exit"),
        "preview_exit_fn": lambda pos: {"expected_usdc": 50.0},
    }}

    runner.real_exit_sweep(
        c, contexts,
        price_fn=lambda mints: {("4663", "0xBig"): {"price": 1.0, "market_cap": None}},
        now_fn=lambda: NOW,
    )

    override = m.effective_exit_override(c, pid)
    assert override is not None and override["source"] == "scalp"


@pytest.mark.parametrize("expected,want", [(50.0, []), (65.0, ["real_tp"])])
def test_robinhood_mcap_target_uses_exact_sell_instead_of_latest_mark(monkeypatch, expected, want):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="0xTarget", ticker="T",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, entry_mcap=4_000_000, opened_at=NOW,
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="4663",
    )
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at=NOW)
    m.set_position_exit_override(c, pid, "operator", tp_target_mcap=5_000_000)
    c.execute(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,mcap,source,quality) "
        "VALUES('4663','0xTarget',?,1.0,6000000,'zerox','ok')",
        (NOW,),
    )
    c.commit()
    exits = []
    contexts = {"4663": {
        "exit_fn": lambda conn, pos, reason, now: exits.append(reason) or {"status": "closed"},
        "preview_exit_fn": lambda pos: {"expected_usdc": expected},
    }}

    runner.real_exit_sweep(
        c, contexts,
        price_fn=lambda mints: {("4663", "0xTarget"): {"price": 1.0}},
        now_fn=lambda: NOW,
    )

    assert exits == want


def test_exit_override_audit_defaults_to_corrected_real_utc(monkeypatch):
    from sniper.persistence import db_memecoin as m

    fixed = "2026-07-17T06:46:00+00:00"
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: fixed)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, opened_at=NOW, strategy="alpha_b", execution_mode="real",
        network_id="solana",
    )

    m.set_position_exit_override(c, pid, "operator", tp_pct=30.0)
    row = c.execute(
        "SELECT created_at FROM memecoin_position_exit_overrides WHERE position_id=?",
        (pid,),
    ).fetchone()
    assert row["created_at"] == fixed

    m.clear_position_exit_override(c, pid, "operator")
    row = c.execute(
        "SELECT resolved_at FROM memecoin_position_exit_overrides WHERE position_id=?",
        (pid,),
    ).fetchone()
    assert row["resolved_at"] == fixed

    explicit = "2026-01-02T03:04:05+00:00"
    m.set_position_exit_override(c, pid, "operator", tp_pct=40.0, now_iso=explicit)
    assert c.execute(
        "SELECT created_at FROM memecoin_position_exit_overrides WHERE active=1",
    ).fetchone()[0] == explicit


# ── RAILS-BLIND (2026-08-06 incident, position 29763) ────────────────────────────────
# The 4663 RPC returned HTTP 429, the exact whole-balance preview never resolved, and the loss
# rails silently fell back to the nominal mid. The card showed -62.8% (exact) while the -50% stop
# was measuring -28.4% (nominal); `stop_hits` stayed 0 and NOT ONE audit row was written, so the
# position looked protected and was not. A 429 is graded infra, so `exact_quote_misses` stays 0
# and the unsellable path never engages either. The blind state must be recorded, not invisible.

def _blind_case(monkeypatch, *, preview, mid=0.72):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="0xthin", ticker="THIN",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, entry_mcap=500_000, opened_at=NOW,
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="4663",
    )
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at=NOW)
    # trail/derisk present => exact_policy True => this position is supposed to decide on the
    # exact executable price, which is exactly when falling back to nominal is dangerous.
    m.set_position_exit_override(c, pid, "auto-classify", tp_pct=30.0, trail_pct=20.0,
                                 derisk_pct=40.0)
    contexts = {"4663": {
        "exit_fn": lambda conn, pos, reason, now: {"status": "closed"},
        "preview_exit_fn": preview,
    }}
    runner.real_exit_sweep(
        c, contexts,
        price_fn=lambda mints: {("4663", "0xthin"): {"price": mid}},   # default -28%, above -50%
        now_fn=lambda: NOW,
    )
    return c, pid


def test_missing_price_reports_unknown_proximity_in_the_actual_sweep(monkeypatch, caplog):
    from sniper.services import memecoin_real_runner as runner
    monkeypatch.setattr(runner, '_BLIND_IDLE_SINCE', {})
    monkeypatch.setattr('sniper.execution.geckoterminal_quote.gt_get_json', lambda *a, **k: None)
    asked = []
    c, pid = _blind_case(monkeypatch, preview=lambda pos: asked.append(pos['id']), mid=None)
    try:
        row = c.execute("SELECT allowed,detail FROM memecoin_exit_trigger_audit "
                        "WHERE position_id=? AND reason='rails_blind'", (pid,)).fetchone()
        detail = json.loads(row['detail'])
        assert row['allowed'] == 0 and detail['consequential'] is False
        assert detail['price_known'] is False
        assert detail['code'] == 'exact_price_unresolved_proximity_unknown'
        assert {'tp', 'derisk'}.issubset(detail['active_rails'])
        assert detail['nearest_rail_distance_pct'] is None
        assert 'rail proximity unknown' in caplog.text
        assert 'no rail within reach' not in caplog.text and 'active=none' not in caplog.text
        assert asked == [pid]
        assert c.execute('SELECT is_open FROM memecoin_positions WHERE id=?', (pid,)).fetchone()[0] == 1
    finally:
        c.close()


def test_unresolved_exact_price_records_a_rails_blind_row(monkeypatch):
    c, pid = _blind_case(monkeypatch, preview=lambda pos: None)   # 429 -> no preview
    row = c.execute(
        "SELECT reason, allowed, display_price, detail FROM memecoin_exit_trigger_audit "
        "WHERE position_id=? AND reason='rails_blind'", (pid,)).fetchone()
    assert row is not None, "a blind loss rail must never be silent"
    assert row["allowed"] == 0
    detail = json.loads(row["detail"])
    # Unknown proximity is distinct from a known near rail and a known distant rail.
    assert detail["code"] in ("exact_price_unresolved_near_rail",
                              "exact_price_unresolved_no_near_rail",
                              "exact_price_unresolved_proximity_unknown")
    assert detail["decision_basis"] == "nominal_mid"
    assert "consequential" in detail and "active_rails" in detail
    # exact equality: "no_near_rail" CONTAINS "near_rail", so a substring test passes
    # for both codes and asserts nothing.
    assert (detail["code"] == "exact_price_unresolved_near_rail") is detail["consequential"]
    # and the position is NOT force-sold: an unresolvable quote often means the exit cannot be
    # executed either, so dumping into it is how you realize the worst possible fill.
    assert c.execute("SELECT is_open FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0] == 1


def test_resolved_exact_price_writes_no_rails_blind_row(monkeypatch):
    c, pid = _blind_case(monkeypatch, preview=lambda pos: {"expected_usdc": 36.0})
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_exit_trigger_audit WHERE position_id=? AND reason='rails_blind'",
        (pid,)).fetchone()[0] == 0


# ── API rail-basis: the card must not show one price and rail on another ─────────────
def test_stop_blind_is_true_when_only_the_sellable_price_is_through_the_stop():
    """Position 29763's exact numbers. -62.8% sellable, -27.9% raw mid, -50% stop."""
    entry, mid, sellable_px, stop = 0.0006568837742759114, 0.00047396824905287746, 0.000244, 50.0
    nominal_pnl = (mid / entry - 1.0) * 100.0
    sellable_pnl = (sellable_px / entry - 1.0) * 100.0
    assert sellable_pnl <= -stop < nominal_pnl        # the exact shape of the incident
    assert bool(sellable_pnl <= -stop and nominal_pnl > -stop) is True


def test_stop_blind_is_false_when_both_prices_agree():
    entry, mid, sellable_px, stop = 0.001, 0.0004, 0.00039, 50.0
    nominal_pnl = (mid / entry - 1.0) * 100.0          # -60%, already through
    sellable_pnl = (sellable_px / entry - 1.0) * 100.0  # -61%
    assert bool(sellable_pnl <= -stop and nominal_pnl > -stop) is False


# ── Last-resolved exact fallback: an outage must not hand the rails the optimistic mark ─────
def _thin_pool_case(monkeypatch, *, previews, price, seed_last=None):
    """A funded 4663 position whose exact quote resolves, then stops resolving (RPC 429)."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    from sniper.settings import memecoin as cfg

    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint="0xthin2", ticker="THIN2",
        entry_price=1.0, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=0,
        their_size_usd=0, entry_mcap=500_000, opened_at=NOW,
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="4663")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at=NOW)
    # sl_pct explicit: 4663's CHAIN default stop is 0 (disabled), so without this the fixture has
    # no stop to arm and the assertions would pass vacuously. The incident position carried 50.0
    # from its frozen `copy-arm-alpha_b-v1` policy; an override is the same rail.
    m.set_position_exit_override(c, pid, "auto-classify", tp_pct=30.0, trail_pct=20.0,
                                 derisk_pct=40.0, sl_pct=-50.0)
    if seed_last is not None:
        m.set_position_last_executable(c, pid, seed_last, NOW)
    seq = list(previews)
    exits = []
    contexts = {"4663": {
        "exit_fn": lambda conn, pos, reason, now: exits.append(reason) or {"status": "closed"},
        "preview_exit_fn": lambda pos: seq.pop(0) if seq else None,
    }}
    runner.real_exit_sweep(
        c, contexts,
        price_fn=lambda mints: {("4663", "0xthin2"): {"price": price}},
        now_fn=lambda: NOW)
    return c, pid, exits


def test_a_resolved_exact_price_is_persisted_for_the_next_outage(monkeypatch):
    c, pid, _ = _thin_pool_case(monkeypatch, previews=[{"expected_usdc": 36.0}], price=0.72)
    row = c.execute("SELECT last_executable_price, last_executable_at FROM memecoin_positions "
                    "WHERE id=?", (pid,)).fetchone()
    assert row["last_executable_price"] == pytest.approx(36.0 / 50.0)   # expected_usdc / open qty
    assert row["last_executable_at"] == NOW


def test_outage_keeps_the_rails_on_the_worse_known_price_not_the_optimistic_mid(monkeypatch):
    """The incident: nominal mid -28% (above the -50% stop), true exact -63% (through it).

    With no fresh quote the rails must not switch to -28%. `stop_hits` proves the stop ARMED.
    """
    c, pid, exits = _thin_pool_case(
        monkeypatch, previews=[None], price=0.72, seed_last=0.37)   # 0.37 => -63%
    # ARMING EVIDENCE MOVED (2026-09-03). -63% is past MEMECOIN_REAL_STOP_INSTANT_PCT=50, so the
    # depth rule sells on the first read and the sweep-count branch is skipped -- stop_hits stays
    # 0. It would anyway: the executable-confirmation failure path resets stop_hits to 0 for
    # b_stop/catastrophe, so the counter was never durable evidence in THIS case, only in the
    # shallow-breach case that `continue`s before reaching it.
    # The audit row is the durable proof, and it is a stronger claim: the stop armed on the stale
    # exact price AND was refused for want of an executable quote, with the code saying so.
    row = c.execute("""SELECT reason, allowed, detail FROM memecoin_exit_trigger_audit
                       WHERE position_id=? AND reason='b_stop' ORDER BY id DESC LIMIT 1""",
                    (pid,)).fetchone()
    assert row is not None, "an armed stop that cannot sell must still leave a row"
    assert row[1] == 0, "it must be recorded as REFUSED, never as executed"
    # nothing sold: a stale price may arm a rail, it may never execute one
    assert exits == []
    assert c.execute("SELECT is_open FROM memecoin_positions WHERE id=?", (pid,)).fetchone()[0] == 1


def test_a_stale_exact_price_past_its_age_bound_is_not_evidence(monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    old = "2026-07-13T20:00:00+00:00"          # ~4h before NOW, past the 900s bound
    c, pid, _ = _thin_pool_case(monkeypatch, previews=[None], price=0.72, seed_last=0.37)
    m.set_position_last_executable(c, pid, 0.37, old)
    m.set_position_stop_hits(c, pid, 0)
    runner.real_exit_sweep(
        c, {"4663": {"exit_fn": lambda *a, **k: {"status": "closed"},
                     "preview_exit_fn": lambda pos: None}},
        price_fn=lambda mints: {("4663", "0xthin2"): {"price": 0.72}}, now_fn=lambda: NOW)
    assert c.execute("SELECT stop_hits FROM memecoin_positions WHERE id=?",
                     (pid,)).fetchone()[0] == 0


def test_the_nominal_mid_cannot_arm_a_rail_against_a_fresh_exact_price(monkeypatch):
    """SUPERSEDES `only the WORSE price may bind` (2026-09-03). That rule read the nominal mid as
    a live price the stale exact had to beat. On 4663 it is not one.

    Measured over 1,554 dexscreener marks paired to an executable mark within 15s since 09-01:
    |error| is 11.1% at the median, 37.8% at p90, 116.2% at p99, and 53.0% of marks disagree by
    more than 10%. A nominal reading -60% against an exact price of -5% resolved seconds earlier
    sits inside that noise band, so "the worse of the two" armed the stop off a coin flip.

    Position 49013 is the live case: 23 consecutive identical dexscreener marks at 1.986e-05 over
    32s while the curve's executable price was 3.99e-05, and b_stop banked a debounce hit at a
    fabricated -47%. The debounce is the bad-tick guard; pre-arming it from noise is what it exists
    to prevent. The old rule's own incident (29763) failed in the other direction — nominal -28.4%
    hid a real -62.8% bag and `stop_hits stayed 0` — so the nominal misled both ways.

    Nothing is lost by dropping it: `executable_exit_allowed` rechecks b_stop against the exact
    price before any sell, so a nominal-armed stop could never execute. It could only bank hits.
    """
    c, pid, _ = _thin_pool_case(
        monkeypatch, previews=[None], price=0.40, seed_last=0.95)   # nominal -60%, stale -5%
    assert c.execute("SELECT stop_hits FROM memecoin_positions WHERE id=?",
                     (pid,)).fetchone()[0] == 0


# ── The card must never present a saturated model as a quote ──────────────────────────
def test_modeled_fill_saturates_its_cap_and_is_not_a_quote():
    """Why the card read -67.6% while the real route quote for the same bag returned -35.2%.

    `slippage_frac` is min(cap, k*notional/liquidity). Once notional exceeds liquidity it pins to
    the cap, so the mark becomes exactly mid*(1-cap) no matter what the book actually looks like.
    Position 29763: $32.39 of bag against $17.30 of liquidity -> a flat 50% haircut, 32 points
    more pessimistic than the measured route. Pessimistic is the dangerous direction: it panics an
    operator into realizing a loss that is not there.
    """
    from sniper.execution.solana_quote import modeled_fill, slippage_frac
    from sniper.settings.memecoin import MEMECOIN_SLIPPAGE_CAP

    assert slippage_frac(32.39, 17.30) == MEMECOIN_SLIPPAGE_CAP        # saturated, carries no info
    mid = 0.000425523629
    assert modeled_fill(mid, 32.39, 17.30, "sell") == pytest.approx(mid * (1 - MEMECOIN_SLIPPAGE_CAP))
    measured = 0.000425523629                                          # the real whole-bag quote
    entry = 0.000656883774
    assert (measured / entry - 1) * 100 == pytest.approx(-35.22, abs=0.1)
    modeled = modeled_fill(mid, 32.39, 17.30, "sell")
    assert (modeled / entry - 1) * 100 == pytest.approx(-67.61, abs=0.1)


def test_a_fresh_last_exact_price_means_the_rails_are_not_blind(monkeypatch):
    """REGRESSION (pos 30226, 2026-08-07). The WO-4 demand gate skips mid-band positions on
    purpose — its None means "resolved: no exact price this tick" — and a healthy exact-policy
    position refreshes its exact price on the 60s demand heartbeat. Counting those skipped sweeps
    as blind paged once per minute per healthy position (276 rails_blind audit rows, miss
    counters 0/0, external whole-bag quote equal to the nominal mid). Blind means the exact
    price is STALE, not "not fetched this sweep"."""
    c, pid, _ = _thin_pool_case(monkeypatch, previews=[None], price=0.72, seed_last=0.70)
    assert c.execute("SELECT rails_blind_streak FROM memecoin_positions WHERE id=?",
                     (pid,)).fetchone()[0] == 0
    assert c.execute(
        "SELECT COUNT(*) FROM memecoin_exit_trigger_audit "
        "WHERE position_id=? AND reason='rails_blind'", (pid,)).fetchone()[0] == 0


def test_an_exact_price_older_than_the_blind_bound_is_blind_again(monkeypatch):
    """The outage detection survives: once nothing has resolved for 3 heartbeats, the streak
    accrues — while the 900s worse-of fallback still arms the rails on the stale price."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as runner
    c, pid, _ = _thin_pool_case(monkeypatch, previews=[None], price=0.72, seed_last=0.70)
    m.set_position_last_executable(c, pid, 0.70, "2026-07-13T23:50:00+00:00")   # 600s old
    runner.real_exit_sweep(
        c, {"4663": {"exit_fn": lambda *a, **k: {"status": "closed"},
                     "preview_exit_fn": lambda pos: None}},
        price_fn=lambda mints: {("4663", "0xthin2"): {"price": 0.72}}, now_fn=lambda: NOW)
    assert c.execute("SELECT rails_blind_streak FROM memecoin_positions WHERE id=?",
                     (pid,)).fetchone()[0] == 1


def test_a_sustained_blind_rail_pages_once_at_the_threshold(monkeypatch):
    """One missed exact quote is provider noise; a sustained blind window is a fault.

    Fires EXACTLY at the threshold sweep, not before and not on every sweep after.
    """
    from sniper.services import memecoin_real_runner as runner
    sent = []
    monkeypatch.setattr(runner.alerts, "alert_rails_blind",
                        lambda *a, **k: sent.append(a))
    c, pid, _ = _thin_pool_case(monkeypatch, previews=[None], price=0.72)
    assert sent == []                                            # streak 1 -> quiet
    assert c.execute("SELECT rails_blind_streak FROM memecoin_positions WHERE id=?",
                     (pid,)).fetchone()[0] == 1

    from sniper.persistence import db_memecoin as m
    contexts = {"4663": {"exit_fn": lambda *a, **k: {"status": "closed"},
                         "preview_exit_fn": lambda pos: None}}
    for expected in (2, 3, 4, 5, 6):
        runner.real_exit_sweep(
            c, contexts, price_fn=lambda mints: {("4663", "0xthin2"): {"price": 0.72}},
            now_fn=lambda: NOW)
        assert c.execute("SELECT rails_blind_streak FROM memecoin_positions WHERE id=?",
                         (pid,)).fetchone()[0] == expected
        assert len(sent) == (1 if expected >= 5 else 0), f"at streak {expected}"
    assert len(sent) == 1                                        # once, not every sweep after


def test_frozen_arm_name_reads_the_dict_the_contract_view_actually_returns():
    """`_position_contract_view` yields a plain dict, not the dataclass.

    Reading it with getattr returned None on every row — the field existed, shipped, and showed
    nothing, which is the failure mode it was added to remove.
    """
    from sniper.routes.api_memecoin import _frozen_arm_name
    assert _frozen_arm_name({"policy_id": "copy-arm-combo_f-v1"}) == "combo_f"
    assert _frozen_arm_name({"policy_id": "copy-arm-real_shadow-v1"}) == "real_shadow"
    assert _frozen_arm_name({"policy_id": "copy-arm-alpha_b-v1"}) == "alpha_b"
    assert _frozen_arm_name(None) is None
    assert _frozen_arm_name({}) is None
