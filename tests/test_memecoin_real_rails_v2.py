"""Operator rail changes 2026-07-10: bankroll sizing (min $30, clip = equity/slots), daily-loss
breaker 0=off, exposure cap 0=off, real rails decoupled from the paper alpha_b study
(stop / full-TP / one-shot de-risk 'sell back the entry, ride the rest' / timecap, each 0=off),
partial-exit executor. Paper arms must be untouched by all of this."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_real_runner as rr
from sniper.execution import solana_exit_executor as xx
from sniper.settings import memecoin as cfg
from sniper.services.memecoin_funded_policy import FundedExitPolicy


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, entry_notional=30.0, entry_price=0.02, mint="MINT"):
    pid = m.open_position(c, source_user_id="u1", source_handle="(real)", mint=mint, ticker="",
                          entry_price=entry_price, entry_notional=entry_notional,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-07-10T00:00:00+00:00", strategy="alpha_b",
                          execution_mode="real", network_id="solana")
    return m.get_position(c, pid)


def _frozen_pos(*, band="small", derisk=40.0, trail=30.0, trail_arm=15.0,
                requires_derisk=False, stop=50.0, catastrophe=80.0, debounce=3,
                max_hold=1000.0, unquotable=300.0, tp=60.0,
                derisk_mode=None, derisk_fraction=0.0):
    policy = FundedExitPolicy(
        policy_id=f"test-{band}-v1", policy_version=1, coin_band=band,
        tp_pct=tp, derisk_pct=derisk,
        derisk_mode=(derisk_mode if derisk_mode is not None
                     else "recover_entry" if derisk else "off"),
        derisk_fraction=derisk_fraction,
        trail_pct=trail, trail_arm_pct=trail_arm,
        trail_requires_derisk=requires_derisk,
        stop_pct=stop, catastrophe_pct=catastrophe,
        stop_debounce_sweeps=debounce, max_hold_s=max_hold,
        unquotable_exit_s=unquotable, mirror_on_sell=True,
    )
    contract = {
        "contract_version": 1,
        "identity": {"network_id": "solana", "mint": "MINT"},
        "policy": policy.to_dict(),
    }
    payload, digest = m.canonical_funded_json(contract)
    return {
        "id": 1, "entry_price": 1.0, "entry_notional": 50.0,
        "network_id": "solana", "mint": "MINT",
        "opened_at": "2026-07-18T12:00:00+00:00", "derisked_at": None,
        "executable_peak_price": 1.5, "peak_price": 1.5, "stuck_bag": 0,
        "last_quote_ts": "2026-07-18T12:00:00+00:00",
        "funded_contract_version": 1, "funded_contract_hash": digest,
        "funded_contract_json": payload,
    }


def test_frozen_contract_drives_nomination_and_confirmation_after_config_drift(monkeypatch):
    pos = _frozen_pos()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_ARM_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 10.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 20.0)

    now = "2026-07-18T12:01:00+00:00"
    assert rr.alpha_b_real_exit_reason(pos, 1.20, now) is None
    assert rr.alpha_b_real_exit_reason(pos, 1.41, now) == "derisk"
    assert rr.alpha_b_real_exit_reason(pos, 0.49, now) == "b_stop"

    c = _c()
    allowed, _detail = rr.executable_exit_allowed(
        c, pos, "real_tp", {"expected_usdc": 80.0})
    refused, _detail = rr.executable_exit_allowed(
        c, pos, "real_tp", {"expected_usdc": 60.0})
    assert allowed is True and refused is False
    assert rr.executable_exit_allowed(
        c, pos, "derisk", {"expected_usdc": 71.0})[0] is True
    assert rr.executable_exit_allowed(
        c, pos, "derisk", {"expected_usdc": 69.0})[0] is False
    assert rr.executable_exit_allowed(
        c, pos, "catastrophe", {"expected_usdc": 9.0})[0] is True
    assert rr.executable_exit_allowed(
        c, pos, "catastrophe", {"expected_usdc": 11.0})[0] is False


def test_stop_and_catastrophe_confirmation_use_remaining_bag_unit_price_after_derisk():
    c = _c()
    frozen = _frozen_pos(tp=0.0, stop=50.0, catastrophe=80.0)
    pid = _persist_frozen_position(c, frozen)
    m.insert_fill(
        c, position_id=pid, kind="derisk", price=2.0, qty=25.0, notional=50.0,
        fill_source="chain", fraction=0.5, created_at="2026-07-18T12:00:30+00:00",
    )
    pos = m.get_position(c, pid)

    stop_allowed, stop_detail = rr.executable_exit_allowed(
        c, pos, "b_stop", {"expected_usdc": 12.25})
    catastrophe_allowed, catastrophe_detail = rr.executable_exit_allowed(
        c, pos, "catastrophe", {"expected_usdc": 2.25})

    assert stop_allowed is True
    assert stop_detail["executable_unit_pnl_pct"] == -51.0
    assert catastrophe_allowed is True
    assert catastrophe_detail["executable_unit_pnl_pct"] == -91.0


@pytest.mark.parametrize(("band", "derisk", "trail"), [
    ("nano", 40.0, 40.0),
    ("micro", 50.0, 50.0),
    ("mid", 30.0, 35.0),
])
def test_frozen_ladder_band_arms_trail_only_after_derisk(band, derisk, trail):
    pos = _frozen_pos(
        band=band, derisk=derisk, trail=trail, trail_arm=0.0,
        requires_derisk=True, stop=60.0, tp=0.0,
    )
    now = "2026-07-18T12:01:00+00:00"
    assert rr.alpha_b_real_exit_reason(
        pos, 1.5 * (1.0 - trail / 100.0), now) is None
    pos["derisked_at"] = "2026-07-18T12:00:30+00:00"
    assert rr.alpha_b_real_exit_reason(
        pos, 1.5 * (1.0 - trail / 100.0), now) == "real_trail"


def test_frozen_timecap_and_unquotable_windows_ignore_process_config(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 999999.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", 999999.0)
    pos = _frozen_pos(max_hold=1000.0, unquotable=300.0)

    assert rr.alpha_b_real_exit_reason(
        pos, None, "2026-07-18T12:06:00+00:00") == "unquotable"
    pos["last_quote_ts"] = "2026-07-18T12:20:00+00:00"
    assert rr.alpha_b_real_exit_reason(
        pos, 1.2, "2026-07-18T12:20:01+00:00") == "b_timecap"


def _persist_frozen_position(c, frozen):
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=frozen["mint"], ticker="T",
        entry_price=frozen["entry_price"], entry_notional=frozen["entry_notional"],
        entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at=frozen["opened_at"], strategy=cfg.MEMECOIN_REAL_POLICY,
        execution_mode="real", network_id=frozen["network_id"],
        funded_contract_version=frozen["funded_contract_version"],
        funded_contract_hash=frozen["funded_contract_hash"],
        funded_contract_json=frozen["funded_contract_json"],
    )
    m.insert_fill(
        c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
        fill_source="chain", fraction=1.0, created_at=frozen["opened_at"],
    )
    return pid


def test_frozen_stop_debounce_survives_process_config_drift(monkeypatch):
    c = _c()
    frozen = _frozen_pos(
        derisk=0.0, trail=0.0, stop=50.0, catastrophe=80.0, debounce=3,
        max_hold=999999.0, tp=0.0,
    )
    pid = _persist_frozen_position(c, frozen)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    # This case breaches at -60% against a frozen stop of 50, which the 2026-09-03 depth rule
    # (MEMECOIN_REAL_STOP_INSTANT_PCT=50) sells on the FIRST read. That is a separate mechanism
    # from the one under test here -- whether the FROZEN sweep count survives process-config drift
    # -- so isolate it. The interaction itself is asserted in
    # tests/test_memecoin_stop_instant_depth.py.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0)
    exits = []
    contexts = {"solana": {
        "preview_exit_fn": lambda _pos: {"expected_usdc": 20.0},
        "exit_fn": lambda *_a: exits.append("exit") or {"status": "failed"},
    }}

    # One sweep per tick: a sweep count means "held about N ticks" (2026-09-27), so hits closer
    # together than a tick do not count.
    for second in ("00", "01"):
        rr.real_exit_sweep(
            c, contexts, price_fn=lambda _mints: {"MINT": {"price": 0.4}},
            now_fn=lambda _s=second: f"2026-07-18T12:01:{_s}+00:00",
        )
    assert exits == []
    assert m.get_position(c, pid)["stop_hits"] == 2

    rr.real_exit_sweep(
        c, contexts, price_fn=lambda _mints: {"MINT": {"price": 0.4}},
        now_fn=lambda: "2026-07-18T12:01:02+00:00",
    )
    assert exits == ["exit"]


def test_malformed_contract_holds_the_position_before_any_exit_action():
    c = _c()
    frozen = _frozen_pos()
    frozen["funded_contract_hash"] = "0" * 64
    _persist_frozen_position(c, frozen)
    calls = []
    contexts = {"solana": {
        "preview_exit_fn": lambda _pos: calls.append("preview"),
        "exit_fn": lambda *_a: calls.append("exit"),
    }}

    rr.real_exit_sweep(
        c, contexts, price_fn=lambda _mints: {"MINT": {"price": 2.0}},
        now_fn=lambda: "2026-07-18T12:01:00+00:00",
    )
    assert calls == []
    audit = c.execute(
        "SELECT allowed FROM memecoin_exit_trigger_audit "
        "WHERE reason='contract_unparseable'"
    ).fetchone()
    assert audit is not None and audit["allowed"] == 0


# --- sizing ---

def test_trade_size_fixed_mode_is_the_static_clip(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 10.0)
    assert risk.real_trade_size(usdc_balance=500.0, open_notional_usd=100.0) == 10.0


def test_trade_size_bankroll_splits_equity_into_slots(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    # $90 equity / 3 slots = $30; growing equity grows the clip
    assert risk.real_trade_size(usdc_balance=90.0, open_notional_usd=0.0) == 30.0
    assert risk.real_trade_size(usdc_balance=60.0, open_notional_usd=30.0) == 30.0   # deployed counts
    assert risk.real_trade_size(usdc_balance=300.0, open_notional_usd=0.0) == 100.0  # profits scale up


def test_trade_size_bankroll_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 3)
    assert risk.real_trade_size(usdc_balance=45.0, open_notional_usd=0.0) == 30.0    # never below $30


# --- breaker / exposure disable semantics ---

def test_daily_loss_breaker_disabled_at_zero_limit():
    c = _c()
    pos = _open_real(c, entry_notional=100.0)
    m.claim_close(c, pos["id"])
    m.finalize_close(c, pos["id"], "2026-07-10T01:00:00+00:00", -500.0, exit_reason="b_stop")
    assert m.check_and_latch_daily_loss(c, "2026-07-10", 0) is False       # 0 = off, huge loss ignored
    assert m.is_daily_loss_tripped(c, "2026-07-10") is False
    assert m.check_and_latch_daily_loss(c, "2026-07-10", 30.0) is True     # >0 still latches


def test_exposure_cap_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "bankroll")
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_USD", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 6)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 0.0)
    c = _c()
    for i in range(3):   # $90 deployed — over the old $60 cap
        _open_real(c, entry_notional=30.0, mint=f"MINT{i}")
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=500.0, sol_balance=1.0, day="2026-07-10")
    d = risk.memecoin_entry_allowed(ctx, mint="FRESH", trade_size=ctx.trade_size_usd)
    assert d.allowed, d.reason


# --- real rails (decoupled from paper alpha_b) ---

def _rails(monkeypatch, stop=50.0, tp=0.0, derisk=100.0, hold=0.0):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", stop)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", tp)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", derisk)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", hold)


def test_derisk_fires_once_at_threshold(monkeypatch):
    _rails(monkeypatch)
    pos = {"entry_price": 0.02, "opened_at": "2026-07-10T00:00:00+00:00", "derisked_at": None}
    now = "2026-07-10T01:00:00+00:00"
    assert rr.alpha_b_real_exit_reason(pos, 0.04, now) == "derisk"          # +100%
    assert rr.alpha_b_real_exit_reason(pos, 0.039, now) is None            # +95% -> hold
    pos["derisked_at"] = "t"
    assert rr.alpha_b_real_exit_reason(pos, 0.06, now) is None             # one-shot: never re-fires


def test_stop_still_fires_and_disabled_rails_do_not(monkeypatch):
    _rails(monkeypatch)   # stop=50, hold=0 (off), tp=0 (off)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 80.0)
    pos = {"entry_price": 0.02, "opened_at": "2026-07-01T00:00:00+00:00", "derisked_at": None}
    now = "2026-07-10T01:00:00+00:00"                                       # held 9 DAYS
    assert rr.alpha_b_real_exit_reason(pos, 0.009, now) == "b_stop"         # -55%
    assert rr.alpha_b_real_exit_reason(pos, 0.03, now) is None              # no timecap at hold=0
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 0.0)
    # primary stop OFF: a moderate loss above the -80% floor still holds (the 4663 recover-thesis)...
    assert rr.alpha_b_real_exit_reason(pos, 0.013, now) is None             # -35% holds
    # ...but the catastrophe floor is an absolute backstop even with the primary stop disabled.
    assert rr.alpha_b_real_exit_reason(pos, 0.001, now) == "catastrophe"    # -95% -> floor
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 0.0)          # floor off -> truly no stop
    assert rr.alpha_b_real_exit_reason(pos, 0.001, now) is None


def test_derisk_applies_after_derisk_stop_still_guards_remainder(monkeypatch):
    _rails(monkeypatch)
    pos = {"entry_price": 0.02, "opened_at": "2026-07-10T00:00:00+00:00", "derisked_at": "t"}
    assert rr.alpha_b_real_exit_reason(pos, 0.009, "2026-07-10T02:00:00+00:00") == "b_stop"


# --- partial-exit executor ---

class _Seams:
    def __init__(self, received=30.0, confirm="confirmed", sig="DSIG"):
        self._received = received
        self._confirm = confirm
        self._sig = sig
        self._sold = False
    def quote_fn(self, mint, tokens, slip):
        # Production-shaped partial quote: the seam owns fraction sizing and the executor requires
        # both exact atomic input and executable minimum output before it may sign.
        return {"inAmount": "500", "outAmount": "1", "otherAmountThreshold": "1"}
    def usdc_fn(self):
        return 100.0 + (self._received if self._sold else 0.0)
    def token_fn(self, mint):
        return 1000.0
    def sign_fn(self, quote):
        return (self._sig.encode(), self._sig)
    def send_fn(self, signed):
        pass
    def confirm_fn(self, sig):
        if self._confirm == "confirmed":
            self._sold = True
        return self._confirm


def _partial(c, pos, s, fraction=0.5):
    return xx.execute_partial_exit(
        c, pos, fraction, "2026-07-10T01:00:00+00:00",
        quote_fn=s.quote_fn, sign_fn=s.sign_fn, send_fn=s.send_fn, confirm_fn=s.confirm_fn,
        usdc_fn=s.usdc_fn, token_balance_fn=s.token_fn, slippage_ladder=[3.0],
        sleep_fn=lambda _s: None)


def test_partial_exit_records_derisk_fill_and_stays_open():
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    r = _partial(c, pos, _Seams(received=30.0))
    assert r["status"] == "derisked" and abs(r["received"] - 30.0) < 1e-9
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["derisked_at"]                          # OPEN, marked
    assert abs(m.derisk_proceeds(c, pos["id"]) - 30.0) < 1e-9
    f = c.execute("SELECT kind, tx_sig, fraction FROM memecoin_fills WHERE position_id=? "
                  "AND kind='derisk'", (pos["id"],)).fetchone()
    assert f["tx_sig"] == "DSIG" and abs(f["fraction"] - 0.5) < 1e-9


def test_partial_exit_unknown_marks_derisked_but_books_nothing():
    c = _c(); pos = _open_real(c)
    r = _partial(c, pos, _Seams(confirm="timeout"))
    assert r["status"] == "unknown"
    p = m.get_position(c, pos["id"])
    assert p["is_open"] == 1 and p["derisked_at"]                          # never re-fire a maybe-landed sell
    assert m.derisk_proceeds(c, pos["id"]) == 0.0


def test_final_close_realized_includes_derisk_proceeds():
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    assert _partial(c, pos, _Seams(received=30.0))["status"] == "derisked"  # entry recovered
    s = _Seams(received=45.0, sig="FINAL-SIG")                              # a distinct final sale
    r = xx.execute_exit(c, m.get_position(c, pos["id"]), "mirror", "2026-07-10T02:00:00+00:00",
                        quote_fn=s.quote_fn, sign_fn=s.sign_fn, send_fn=s.send_fn,
                        confirm_fn=s.confirm_fn, usdc_fn=s.usdc_fn, token_balance_fn=s.token_fn,
                        slippage_ladder=[3.0], sleep_fn=lambda _s: None)
    assert r["status"] == "closed"
    assert abs(r["realized"] - 45.0) < 1e-9                                # 30 + 45 - 30, NOT 45-30
    assert abs(m.get_position(c, pos["id"])["realized_pnl"] - 45.0) < 1e-9


def test_concurrent_double_derisk_books_exactly_one_fill():
    """P0-2 (Codex 2026-07-24 activation-blocker review): the one-shot used to stamp `derisked_at`
    only AFTER a confirmed/unknown result, so two callers racing for the SAME position (a duplicated
    sweep pass, a nomination-layer bug that re-proposes DERISK/TAKE_PROFIT before the mapper's own
    guard catches up) could both pass the balance/quote checks and both broadcast a partial sell —
    a real double-sell. The claim must be atomic and taken BEFORE any submission."""
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    s = _Seams(received=30.0)
    r1 = _partial(c, pos, s)
    r2 = _partial(c, pos, s)                       # second racer sees the one-shot already claimed
    assert r1["status"] == "derisked"
    assert r2["status"] != "derisked"               # refused without ever quoting/submitting
    fills = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind='derisk'",
                      (pos["id"],)).fetchone()[0]
    assert fills == 1                               # exactly one fill booked, never two


def test_derisk_failure_releases_the_claim_for_the_next_sweep():
    """A failed submission (no balance / ladder exhausted) must NOT burn the one-shot — the rail
    is healthy, it just could not route this sweep, and the NEXT sweep needs the claim available."""
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    s = _Seams(received=30.0)
    s.token_fn = lambda mint: 0.0                    # nothing to sell this sweep
    r = _partial(c, pos, s)
    assert r["status"] == "derisk_failed"
    assert m.get_position(c, pos["id"])["derisked_at"] is None   # released, not burned
    # a later sweep with a real balance can still fire the rail
    s2 = _Seams(received=30.0)
    r2 = _partial(c, pos, s2)
    assert r2["status"] == "derisked"


def test_zero_balance_after_derisk_stays_unrealized():
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    assert _partial(c, pos, _Seams(received=30.0))["status"] == "derisked"
    s = _Seams()
    s.token_fn = lambda mint: 0.0                                          # rest of the bag vanished
    r = xx.execute_exit(c, m.get_position(c, pos["id"]), "mirror", "2026-07-10T02:00:00+00:00",
                        quote_fn=s.quote_fn, sign_fn=s.sign_fn, send_fn=s.send_fn,
                        confirm_fn=s.confirm_fn, usdc_fn=s.usdc_fn, token_balance_fn=s.token_fn,
                        slippage_ladder=[3.0], sleep_fn=lambda _s: None)
    row = m.get_position(c, pos["id"])
    assert r["status"] == "stuck"
    assert row["is_open"] == 1 and row["stuck_bag"] == 1
    assert row["realized_pnl"] is None
    assert m.derisk_proceeds(c, pos["id"]) == 30.0


# --- sweep fraction math ---

def test_run_derisk_fraction_recovers_entry(monkeypatch):
    c = _c(); pos = _open_real(c, entry_notional=30.0, entry_price=0.02)
    seen = {}
    ctx = {"token_fn": lambda mint: 750.0,                                 # 750 tokens @ 0.08 = $60 value
           "derisk_fn": lambda conn, p, fraction, now: seen.update(f=fraction) or {"status": "derisked"}}
    r = rr._run_derisk(c, ctx, pos, 0.08, "2026-07-10T01:00:00+00:00",
                       preview={"expected_usdc": 60.0})
    assert r == {"status": "derisked"}
    assert abs(seen["f"] - 0.5) < 1e-9                                     # sell half -> $30 back


def test_run_derisk_skips_when_value_fell_back(monkeypatch):
    c = _c(); pos = _open_real(c, entry_notional=30.0)
    ctx = {"token_fn": lambda mint: 1000.0, "derisk_fn": lambda *a, **k: {"status": "derisked"}}
    assert rr._run_derisk(c, ctx, pos, 0.031, "t",
                          preview={"expected_usdc": 31.0}) is None         # value $31 vs entry $30 -> skip


# ── trail arming requires a CONFIRMED de-risk (2026-07-22, position 9003) ────────
#
# The executor sets `derisked_at` on an UNKNOWN confirm on purpose: the sell may have
# landed, and re-firing a maybe-landed sell risks a double-sell. That guard is correct.
# What was wrong is that `effective_trail` read the same flag as "principal recovered"
# and armed the trail on it. On 9003 the de-risk never landed (proven on-chain: the exit
# moved the FULL entry quantity), yet the trail armed and closed the position at -65%
# with prior_proceeds=0 — 371s after the stamp and 829s before the recovery pass could
# have re-armed the rail.
#
# `derisk_sig` is the discriminator already in the row: it is persisted ONLY while the
# de-risk is unverified, and cleared by settle (landed) or clear (proven not landed).

def _derisk_pos(*, derisked_at, derisk_sig, **kw):
    pos = _frozen_pos(requires_derisk=True, derisk=40.0, trail=30.0, trail_arm=0.0, **kw)
    pos["derisked_at"] = derisked_at
    pos["derisk_sig"] = derisk_sig
    return pos


def test_trail_does_not_arm_on_an_unverified_derisk():
    pos = _derisk_pos(derisked_at="2026-07-19T00:53:04+00:00", derisk_sig="3HDs2W9unverified")
    _pct, armed = rr.effective_trail(pos)
    assert armed is False


def test_trail_arms_on_a_settled_derisk():
    """Confirmed (or operator-settled) clears the sig and keeps the stamp -> principal
    really was recovered on-chain, so the trail is entitled to arm."""
    pos = _derisk_pos(derisked_at="2026-07-19T00:53:04+00:00", derisk_sig=None)
    _pct, armed = rr.effective_trail(pos)
    assert armed is True


def test_trail_does_not_arm_without_any_derisk():
    pos = _derisk_pos(derisked_at=None, derisk_sig=None)
    _pct, armed = rr.effective_trail(pos)
    assert armed is False


def test_unverified_derisk_still_blocks_a_second_derisk():
    """The double-sell guard must survive this change: `derisked_at` alone still means
    'do not re-fire the sell', it just no longer means 'principal recovered'."""
    pos = _derisk_pos(derisked_at="2026-07-19T00:53:04+00:00", derisk_sig="unverified")
    assert bool(pos["derisked_at"]) is True


def test_arm_pct_path_is_untouched_by_the_sig():
    """Bands whose trail does NOT require a de-risk (small: arm_pct=15) are unaffected —
    their arming reads the executable high-water, not the de-risk state at all."""
    pos = _frozen_pos(requires_derisk=False, trail_arm=15.0)
    pos["derisked_at"] = None
    pos["derisk_sig"] = "unverified"
    pos["executable_peak_price"] = 1.5           # +50% vs entry 1.0, well past +15%
    _pct, armed = rr.effective_trail(pos)
    assert armed is True


# --- v2 fraction-mode de-risk (2026-07-23): nano/micro sell a FIXED fraction at the rail, not
#     the recover-entry sizing (at +20% recover-entry would sell ~83% of the bag). ---

def _fraction_pos(**kw):
    return _frozen_pos(band="micro", derisk=20.0, trail=30.0, trail_arm=0.0,
                       requires_derisk=True, stop=60.0, tp=0.0,
                       derisk_mode="fraction", derisk_fraction=0.5, **kw)


def test_run_derisk_fraction_mode_sells_policy_fraction_not_recover_entry():
    c = _c()
    pos = _fraction_pos()
    seen = {}
    ctx = {"derisk_fn": lambda conn, p, fraction, now: seen.update(f=fraction) or {"status": "derisked"}}
    # entry_notional 50, expected 60: recover-entry would sell 50/60=0.833; fraction mode sells 0.5
    r = rr._run_derisk(c, ctx, pos, 1.20, "2026-07-18T12:01:00+00:00",
                       preview={"expected_usdc": 60.0})
    assert r == {"status": "derisked"}
    assert abs(seen["f"] - 0.5) < 1e-9


def test_fraction_mode_nomination_and_confirmation_at_20():
    c = _c()
    pos = _fraction_pos()
    now = "2026-07-18T12:01:00+00:00"
    assert rr.alpha_b_real_exit_reason(pos, 1.15, now) is None       # below +20
    assert rr.alpha_b_real_exit_reason(pos, 1.21, now) == "derisk"   # +21 crosses
    # confirmation: exact proceeds must confirm >= +20% on the whole bag (entry 50)
    assert rr.executable_exit_allowed(c, pos, "derisk", {"expected_usdc": 61.0})[0] is True
    assert rr.executable_exit_allowed(c, pos, "derisk", {"expected_usdc": 58.0})[0] is False


# --- a close already in flight needs nothing from the sweep -------------------------------------
# 267 `already_closing` rows on 4663 real_tp came from THREE positions. 46888 held a close for
# 16m58s and was re-fired 139 times, each attempt preceded by its own exact route preview. The
# executor bailed in ~0.12ms, which made it look free — but the executor is the LAST step and
# `total_ms` only measures the executor. The preview in front of it is the cost.

def _inflight_ctx(calls):
    return {"solana": {
        "preview_exit_fn": lambda _pos: calls.append("preview") or {"expected_usdc": 20.0},
        "exit_fn": lambda *_a: calls.append("exit") or {"status": "already_closing"},
    }}


def _sweep_once(c, calls):
    rr.real_exit_sweep(
        c, _inflight_ctx(calls), price_fn=lambda _mints: {"MINT": {"price": 0.4}},
        now_fn=lambda: "2026-07-18T12:01:00+00:00",
    )


def _stop_firing_position(c):
    frozen = _frozen_pos(derisk=0.0, trail=0.0, stop=50.0, catastrophe=80.0, debounce=1,
                         max_hold=999999.0, tp=0.0)
    return _persist_frozen_position(c, frozen)


def test_an_unclaimed_position_is_still_swept():
    """Control. Without this the skip could be swallowing every position and the others still pass."""
    c = _c(); _stop_firing_position(c); calls = []
    _sweep_once(c, calls)
    assert "preview" in calls, calls


def test_a_claimed_close_is_skipped_before_the_exact_preview():
    c = _c(); pid = _stop_firing_position(c)
    assert m.claim_position_close(c, pid, "2026-07-18T12:00:00+00:00", "real_tp")
    calls = []
    _sweep_once(c, calls)
    assert calls == [], "a position mid-close still bought a preview / re-fired"


def test_a_signed_close_is_skipped():
    c = _c(); pid = _stop_firing_position(c)
    c.execute("UPDATE memecoin_positions SET close_sig=? WHERE id=?", ("0xsig", pid)); c.commit()
    calls = []
    _sweep_once(c, calls)
    assert calls == []


def test_an_inflight_derisk_is_skipped_too():
    # the claim predicate refuses on derisk state as well, so a sweep past it could only ever
    # produce `already_closing` from the executor.
    c = _c(); pid = _stop_firing_position(c)
    c.execute("UPDATE memecoin_positions SET derisk_claim_id=? WHERE id=?", ("d1", pid)); c.commit()
    calls = []
    _sweep_once(c, calls)
    assert calls == []


# --- a preview that came back empty must say WHY (2026-09-02) -----------------------------------
#
# HOOD demanded an exact quote on ~74 fastlane ticks over its 74-second life and got 6 — an 8%
# success rate — and nothing recorded whether the 68 misses were the TOKEN, the ROUTE or the
# PROVIDER. Those are three different fixes. `PreviewResult` already carries a typed `failure` and
# the provider that produced it; the boundary discarded both and wrote only "preview_unavailable".


class _FailedPreview:
    """The shape `preview_exit_fn` actually returns on a miss: no expected_usdc, typed failure."""

    def __init__(self, failure, provider_id=None, provider_group=None):
        self.preview = None
        self.failure = failure
        self.provider_id = provider_id
        self.provider_group = provider_group


class _Enum:
    def __init__(self, value):
        self.value = value


def test_an_empty_preview_still_refuses():
    """The refusal is unchanged — this is diagnostic only and must not widen the gate."""
    allowed, detail = rr.executable_exit_allowed(_c(), _frozen_pos(), "real_tp", _FailedPreview(None))
    assert allowed is False and detail["code"] == "preview_unavailable"


def test_the_typed_failure_reaches_the_audit():
    _ok, detail = rr.executable_exit_allowed(
        _c(), _frozen_pos(), "real_tp",
        _FailedPreview(_Enum("no_route"), provider_id="4663:direct", provider_group="evm"))
    assert detail["preview_failure"] == "no_route", "token vs route vs provider must be readable"
    assert detail["preview_provider"] == "4663:direct"
    assert detail["preview_provider_group"] == "evm"


def test_a_failure_with_no_enum_value_still_records_something():
    """Never lose the cause to a shape we did not anticipate."""
    _ok, detail = rr.executable_exit_allowed(
        _c(), _frozen_pos(), "real_tp", _FailedPreview("rpc_transport"))
    assert detail["preview_failure"] == "rpc_transport"


def test_a_plain_dict_preview_is_unaffected():
    """The existing callers pass a bare dict. They must keep working and gain no phantom keys."""
    _ok, detail = rr.executable_exit_allowed(_c(), _frozen_pos(), "real_tp", {"expected_usdc": 0.0})
    assert detail["code"] == "preview_unavailable"
    assert "preview_failure" not in detail


def test_a_SUCCESSFUL_preview_is_untouched():
    allowed, _detail = rr.executable_exit_allowed(_c(), _frozen_pos(), "real_tp", {"expected_usdc": 80.0})
    assert allowed is True
