"""Cross-wallet re-entry for full-cap wallets (operator order 2026-10-08, brain 30822fa2).

LILMET D75KtdgF on Solana: Degen (fb0c112f) position 71958 closed +$43.97 at 09:19:50Z, and
ResellCalendar (ff03cead) bought at 09:29:37Z. The on-chain lane refused it with "re-entry cooldown:
a real position on this coin closed within the window". The operator wants that buy taken. Degen
buying its own coin again stays blocked.

A wallet on MEMECOIN_REAL_FULL_CAP_WALLET_IDS for the entry's chain is refused only by its OWN close
inside the window (cooldown), or by the coin's last close when that close is its own and lost
(losing). The leg resolves the listing and passes `reentry_closes_by`; the risk modules stay blind.
Spec docs/superpowers/specs/2026-10-08-memecoin-cross-wallet-reentry-design.md.
"""
from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timedelta

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_wallet_caps as caps
from sniper.settings import memecoin as cfg

LILMET = "D75KtdgF7eouvFWjs2xDKRNPYJ5dRzUKg8pSuthMZUN1"
DEGEN = "fb0c112f-45ba-54f7-b5ac-910c4e1ec6bb"
RESELL = "ff03cead-0000-5000-8000-000000000000"
OTHER = "aaaaaaaa-0000-5000-8000-000000000000"
EVM_COIN = "0x00000000000000000000000000000000000000c0"


@pytest.fixture(autouse=True)
def _live_policy(monkeypatch):
    """The live re-entry configuration: 24 h cooldown, losing block on (24 h), winners cooled."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_REENTRY_COOLDOWN_S", 86400.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BLOCK_LOSING_REENTRY", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_LOSING_REENTRY_HOURS", 24.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REENTRY_WINNER_UNBLOCKED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_DAILY_LOSS_LIMIT_USD", 0.0)   # a test loss must not latch
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY", True, raising=False)
    monkeypatch.setattr(caps.CACHE, "operator_snapshot",
                        (frozenset({DEGEN, RESELL, "%s@4663" % OTHER}), "ready", 0.0))
    # The re-entry list unset: the full-cap list above decides (its own section below sets it).
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", "", raising=False)
    monkeypatch.setattr(caps.CACHE, "reentry_snapshot", None, raising=False)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _ago(seconds):
    from sniper.util.clock import real_utc_now_iso
    anchor = datetime.fromisoformat(real_utc_now_iso().replace("Z", "+00:00"))
    return (anchor - timedelta(seconds=seconds)).isoformat()


def _position(c, uid, *, pnl=None, closed_s_ago=None, mint=LILMET, network_id="solana"):
    """A real position by `uid` (None = a manual close), open when closed_s_ago is None."""
    is_open = closed_s_ago is None
    c.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, entry_notional, "
        "is_open, opened_at, closed_at, realized_pnl, execution_mode, network_id, strategy) "
        "VALUES (?,?,?,?,?,?,?,?,?, 'real', ?, 'alpha_b')",
        (uid, mint, "LILMET", 1.0, 50.0, int(is_open), _ago(7200),
         None if is_open else _ago(closed_s_ago), pnl, network_id))
    c.commit()


def _solana(c, closes_by):
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0,
                                           day=_ago(0)[:10], reentry_closes_by=closes_by)
    return risk.memecoin_entry_allowed(ctx, mint=LILMET, trade_size=ctx.trade_size_usd)


def _evm(c, closes_by, network_id="4663"):
    ctx = evm_risk.build_evm_risk_context(c, usdc_balance=300.0, native_balance=1.0,
                                          day=_ago(0)[:10], network_id=network_id,
                                          reentry_closes_by=closes_by)
    return evm_risk.evm_entry_allowed(ctx, mint=EVM_COIN, trade_size=ctx.trade_size_usd)


def _lilmet(c):
    """Degen's 71958 (+$43.97, 09:19:50Z) and the operator's manual 71961 (+$6.69, 09:32:08Z)."""
    _position(c, DEGEN, pnl=43.97, closed_s_ago=1800)
    _position(c, None, pnl=6.69, closed_s_ago=600)


def _not_reentry(decision):
    return "re-entry" not in (decision.reason or "")


# ------------------------------------------------------------------ the listing, resolved in the leg

def test_a_listed_wallet_gets_its_own_id_as_the_scope():
    assert caps.cross_wallet_reentry_closes_by(RESELL, "solana") == RESELL
    assert caps.cross_wallet_reentry_closes_by(DEGEN, "4663") == DEGEN


def test_a_wallet_that_is_not_listed_gets_no_scope():
    assert caps.cross_wallet_reentry_closes_by("not-on-the-list", "solana") is None


def test_a_chain_scoped_entry_lifts_only_on_its_own_chain():
    assert caps.cross_wallet_reentry_closes_by(OTHER, "solana") is None
    assert caps.cross_wallet_reentry_closes_by(OTHER, "4663") == OTHER


def test_no_source_wallet_gets_no_scope():
    assert caps.cross_wallet_reentry_closes_by(None, "solana") is None
    assert caps.cross_wallet_reentry_closes_by("", "solana") is None


def test_the_knob_off_gives_no_scope_to_anyone(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY", False)
    assert caps.cross_wallet_reentry_closes_by(RESELL, "solana") is None


@pytest.mark.parametrize("raw, on", [(None, True), ("1", True), ("0", False), ("false", False)])
def test_the_knob_defaults_on(raw, on):
    """Read in a fresh interpreter: this module's fixture patches the attribute in-process."""
    import os
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_REAL_CROSS_WALLET_REENTRY"}
    if raw is not None:
        env["MEMECOIN_REAL_CROSS_WALLET_REENTRY"] = raw
    out = subprocess.run(
        [sys.executable, "-c", "from sniper.settings import memecoin as c; "
                               "print(c.MEMECOIN_REAL_CROSS_WALLET_REENTRY)"],
        env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == str(on), out.stderr[-500:]


def test_the_listing_reads_the_full_cap_clips_live_list(monkeypatch):
    """One parser: the clip ceiling and the re-entry lift agree on who is listed."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", True)
    monkeypatch.setattr(caps.CACHE, "operator_snapshot", (frozenset({"%s@56" % RESELL}), "ready", 0.0))
    for chain in ("56", "solana"):
        listed = caps.cross_wallet_reentry_closes_by(RESELL, chain) is not None
        assert listed == (caps.entry_cap_usd(RESELL, chain) == cfg.MEMECOIN_REAL_MAX_ENTRY_USD)
    assert caps.cross_wallet_reentry_closes_by(RESELL, "56") == RESELL


# ------------------------------------------------------------------ the cooldown family (Solana)

def test_lilmet_a_listed_wallet_enters_a_coin_another_wallet_closed():
    c = _conn()
    _lilmet(c)
    d = _solana(c, RESELL)
    assert d.allowed, d.reason


def test_the_same_coin_without_a_scope_is_still_refused():
    """Today's behaviour, and what a wallet that is not listed gets."""
    c = _conn()
    _lilmet(c)
    d = _solana(c, None)
    assert not d.allowed and d.reason.startswith("re-entry cooldown"), d.reason


def test_degen_buying_its_own_coin_again_is_refused():
    c = _conn()
    _lilmet(c)
    d = _solana(c, DEGEN)
    assert not d.allowed and d.reason.startswith("re-entry cooldown"), d.reason


def test_a_manual_close_does_not_block_a_listed_wallet():
    c = _conn()
    _position(c, None, pnl=6.69, closed_s_ago=600)
    d = _solana(c, RESELL)
    assert d.allowed, d.reason


def test_an_own_close_among_others_still_refuses():
    c = _conn()
    _lilmet(c)
    _position(c, RESELL, pnl=5.0, closed_s_ago=3600)
    d = _solana(c, RESELL)
    assert not d.allowed and d.reason.startswith("re-entry cooldown"), d.reason


def test_an_own_close_outside_the_window_does_not_refuse():
    c = _conn()
    _position(c, RESELL, pnl=5.0, closed_s_ago=2 * 86400)
    _position(c, DEGEN, pnl=43.97, closed_s_ago=1800)
    assert _solana(c, RESELL).allowed


def test_an_open_position_still_blocks_a_second_wallet():
    c = _conn()
    _position(c, DEGEN)                               # Degen still holds LILMET
    d = _solana(c, RESELL)
    assert not d.allowed
    assert _not_reentry(d), d.reason                  # one-per-coin refuses, not the cooldown


def test_another_coins_close_by_the_wallet_does_not_block_this_coin():
    c = _conn()
    _position(c, RESELL, pnl=-3.0, closed_s_ago=600, mint="So1OtherMint")
    _position(c, DEGEN, pnl=43.97, closed_s_ago=1800)
    assert _solana(c, RESELL).allowed


# ------------------------------------------------------------------ the losing family (Solana)

def test_another_wallets_losing_last_close_does_not_block_a_listed_wallet():
    c = _conn()
    _position(c, DEGEN, pnl=-44.52, closed_s_ago=1800)
    d = _solana(c, RESELL)
    assert d.allowed, d.reason


def test_an_own_losing_last_close_refuses():
    c = _conn()
    _position(c, DEGEN, pnl=-44.52, closed_s_ago=1800)
    # The cooldown also names Degen's own close; take it out so the losing family alone decides.
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0, day=_ago(0)[:10],
                                           include_reentry_policy=True, reentry_closes_by=DEGEN)
    assert m.coin_identity("solana", LILMET) in ctx.losing_coin_ids


def test_losing_is_never_stricter_than_today():
    """W lost, then another wallet won: the coin's last close won, so nobody is blocked by it."""
    c = _conn()
    _position(c, RESELL, pnl=-5.0, closed_s_ago=3 * 3600)
    _position(c, DEGEN, pnl=10.0, closed_s_ago=3600)
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0, day=_ago(0)[:10],
                                           reentry_closes_by=RESELL)
    assert m.coin_identity("solana", LILMET) not in ctx.losing_coin_ids


def test_the_losing_set_without_a_scope_is_unchanged():
    c = _conn()
    _position(c, DEGEN, pnl=-44.52, closed_s_ago=1800)
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0, day=_ago(0)[:10])
    assert m.coin_identity("solana", LILMET) in ctx.losing_coin_ids


# ------------------------------------------------------------------ EVM

def test_evm_a_listed_wallet_enters_a_coin_another_wallet_closed():
    c = _conn()
    _position(c, DEGEN, pnl=12.0, closed_s_ago=1800, mint=EVM_COIN, network_id="4663")
    d = _evm(c, RESELL)
    assert d.allowed, d.reason


def test_evm_own_coin_is_refused():
    c = _conn()
    _position(c, DEGEN, pnl=12.0, closed_s_ago=1800, mint=EVM_COIN, network_id="4663")
    d = _evm(c, DEGEN)
    assert not d.allowed and d.reason.startswith("re-entry cooldown"), d.reason
    d = _evm(c, None)
    assert not d.allowed and d.reason.startswith("re-entry cooldown"), d.reason


def test_evm_57073_losing_follows_the_same_split():
    c = _conn()
    _position(c, DEGEN, pnl=-8.0, closed_s_ago=1800, mint=EVM_COIN, network_id="57073")
    for closes_by, blocked in ((RESELL, False), (DEGEN, True), (None, True)):
        ctx = evm_risk.build_evm_risk_context(c, usdc_balance=300.0, native_balance=1.0,
                                              day=_ago(0)[:10], network_id="57073",
                                              reentry_closes_by=closes_by)
        assert (m.coin_identity("57073", EVM_COIN) in ctx.losing_coin_ids) is blocked, closes_by


# ------------------------------------------------------------------ both hops, under the lock

def test_the_reservation_applies_the_scope_under_its_own_lock():
    c = _conn()
    _lilmet(c)
    kw = dict(mint=LILMET, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
              usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0))
    refused, pid = risk.reserve_entry(c, source_user_id=DEGEN, reentry_closes_by=DEGEN, **kw)
    assert pid is None and refused.reason.startswith("re-entry cooldown"), refused.reason
    allowed, pid = risk.reserve_entry(c, source_user_id=RESELL, reentry_closes_by=RESELL, **kw)
    assert allowed.allowed and pid is not None, allowed.reason


def test_preflight_takes_the_scope():
    c = _conn()
    _lilmet(c)
    kw = dict(mint=LILMET, day=_ago(0)[:10], usdc_balance=300.0, sol_balance=5.0)
    assert risk.preflight_entry(c, reentry_closes_by=RESELL, **kw).allowed
    assert not risk.preflight_entry(c, reentry_closes_by=DEGEN, **kw).allowed


# ------------------------------------------------------------------ every funded lane passes it

def _src(module):
    return inspect.getsource(module)


def test_the_solana_leg_passes_the_scope_to_both_hops():
    from sniper.services import memecoin_real_leg as leg
    src = _src(leg)
    assert "cross_wallet_reentry_closes_by(" in src
    assert src.count("reentry_closes_by=_reentry_closes_by") == 2, (
        "preflight and reserve_entry must BOTH get the scope")


def test_the_evm_leg_passes_the_scope_to_the_reservation():
    from sniper.services import memecoin_evm_leg as leg
    src = _src(leg.evm_real_entry)
    assert "cross_wallet_reentry_closes_by(" in src
    assert "reentry_closes_by=" in src


def test_the_slot_lane_passes_the_scope_to_its_reservation():
    from sniper.services import memecoin_slot_lane as lane
    src = _src(lane)
    assert "cross_wallet_reentry_closes_by(" in src
    assert "reentry_closes_by=" in src


def test_the_early_quote_previews_pass_the_scope():
    """An admitted entry must keep its early quote: the preview evaluates the same envelope."""
    from sniper.services import memecoin_real_runner as runner
    for fn in (runner._early_quote_clip, runner._early_evm_quote_clip):
        src = _src(fn)
        assert "cross_wallet_reentry_closes_by(" in src, fn.__name__
        assert src.count("reentry_closes_by=") >= (2 if fn is runner._early_quote_clip else 1), fn.__name__


def test_the_risk_modules_take_a_scope_and_never_resolve_the_listing():
    for module in (risk, evm_risk):
        src = _src(module)
        assert "reentry_closes_by" in src
        assert "cross_wallet_reentry_closes_by" not in src
        assert "FULL_CAP_WALLET_IDS" not in src


# ------------------------------------------------------------------ the lift marker (Gamma add 1)
# An entry admitted only because of the scope must say so: which family was lifted and the close
# that would have blocked it, so the cross-wallet cohort can be graded apart from ordinary entries.

def _ids(c):
    return {r["source_user_id"]: r["id"] for r in c.execute("SELECT id, source_user_id FROM memecoin_positions")}


def test_the_reservation_names_the_cooldown_close_it_lifted():
    c = _conn()
    _position(c, DEGEN, pnl=43.97, closed_s_ago=1800)
    decision, pid = risk.reserve_entry(
        c, mint=LILMET, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
        usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0), reentry_closes_by=RESELL)
    assert pid is not None, decision.reason
    lift = decision.reentry_lift
    assert set(lift) == {"cooldown"}
    assert lift["cooldown"]["position_id"] == _ids(c)[DEGEN]
    assert lift["cooldown"]["source_user_id"] == DEGEN
    assert lift["cooldown"]["realized_pnl"] == pytest.approx(43.97)


def test_the_marker_names_the_latest_blocking_close():
    """LILMET: the manual-buy position closed after Degen's, so it is the latest blocker."""
    c = _conn()
    _lilmet(c)
    decision, pid = risk.reserve_entry(
        c, mint=LILMET, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
        usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0), reentry_closes_by=RESELL)
    assert pid is not None, decision.reason
    assert decision.reentry_lift["cooldown"]["source_user_id"] is None
    assert decision.reentry_lift["cooldown"]["realized_pnl"] == pytest.approx(6.69)


def test_the_reservation_names_the_losing_close_it_lifted():
    c = _conn()
    _position(c, DEGEN, pnl=-44.52, closed_s_ago=1800)
    decision, pid = risk.reserve_entry(
        c, mint=LILMET, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
        usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0), reentry_closes_by=RESELL)
    assert pid is not None, decision.reason
    assert set(decision.reentry_lift) == {"cooldown", "losing"}
    assert decision.reentry_lift["losing"]["position_id"] == _ids(c)[DEGEN]
    assert decision.reentry_lift["losing"]["realized_pnl"] == pytest.approx(-44.52)


@pytest.mark.parametrize("closes_by", [None, RESELL])
def test_no_marker_when_nothing_was_lifted(closes_by):
    c = _conn()
    _position(c, DEGEN, pnl=5.0, closed_s_ago=1800, mint="So1OtherMint")
    decision, pid = risk.reserve_entry(
        c, mint=LILMET, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
        usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0), reentry_closes_by=closes_by)
    assert pid is not None, decision.reason
    assert decision.reentry_lift is None


def test_the_evm_reservation_names_the_close_it_lifted():
    c = _conn()
    _position(c, DEGEN, pnl=12.0, closed_s_ago=1800, mint=EVM_COIN, network_id="4663")
    decision, pid = evm_risk.reserve_entry_evm(
        c, mint=EVM_COIN, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY,
        network_id="4663", day=_ago(0)[:10], usdc_balance=300.0, native_balance=1.0,
        now_iso=_ago(0), reentry_closes_by=RESELL)
    assert pid is not None, decision.reason
    assert decision.reentry_lift["cooldown"]["source_user_id"] == DEGEN


def test_every_reserving_lane_writes_the_marker_to_the_trace():
    from sniper.services import memecoin_evm_leg as evm_leg
    from sniper.services import memecoin_real_leg as real_leg
    from sniper.services import memecoin_slot_lane as lane
    for module in (real_leg, evm_leg, lane):
        assert "mark_reentry_lift(" in _src(module), module.__name__


class _Trace:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def mark_substage(self, name, **fields):
        if self.fail:
            raise ValueError("trace closed")
        self.events.append((name, fields))


def test_the_marker_lands_on_the_trace_with_the_scope():
    c = _conn()
    _position(c, DEGEN, pnl=43.97, closed_s_ago=1800)
    decision, _ = risk.reserve_entry(
        c, mint=LILMET, source_user_id=RESELL, strategy=cfg.MEMECOIN_REAL_POLICY, day=_ago(0)[:10],
        usdc_balance=300.0, sol_balance=5.0, now_iso=_ago(0), reentry_closes_by=RESELL)
    trace = _Trace()
    caps.mark_reentry_lift(trace, decision, RESELL)
    (name, fields), = trace.events
    assert name == "reentry_cross_wallet"
    assert fields["closes_by"] == RESELL
    assert fields["cooldown"]["source_user_id"] == DEGEN


def test_no_lift_no_trace_event_and_a_trace_fault_never_raises():
    from sniper.risk.models import RiskDecision
    plain = RiskDecision(True, 10.0, "ok", False)
    trace = _Trace()
    caps.mark_reentry_lift(trace, plain, RESELL)
    caps.mark_reentry_lift(None, plain, RESELL)
    assert trace.events == []
    lifted = RiskDecision(True, 10.0, "ok", False, reentry_lift={"cooldown": {"position_id": 1}})
    caps.mark_reentry_lift(_Trace(fail=True), lifted, RESELL)     # evidence only: never blocks


# ------------------------------------------------------------------ real closes only (Gamma add 3)

def test_a_paper_close_neither_blocks_nor_counts():
    c = _conn()
    c.execute(
        "INSERT INTO memecoin_positions (source_user_id, mint, ticker, entry_price, entry_notional, "
        "is_open, opened_at, closed_at, realized_pnl, execution_mode, network_id, strategy) "
        "VALUES (?,?,?,?,?,0,?,?,?, 'paper', 'solana', 'funded_clone')",
        (RESELL, LILMET, "LILMET", 1.0, 50.0, _ago(7200), _ago(600), -9.0))
    c.commit()
    for closes_by in (RESELL, DEGEN, None):
        assert _solana(c, closes_by).allowed, closes_by


def test_ids_compare_exactly():
    """Roster ids are compared as stored: a pump:<base58> id is case-sensitive, never lowercased."""
    c = _conn()
    _position(c, "pump:AbCdEf", pnl=3.0, closed_s_ago=600)
    assert not _solana(c, "pump:AbCdEf").allowed
    assert _solana(c, "pump:abcdef").allowed


# ------------------------------------------------------------------ its own wallet list (operator 13:10Z)
# MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS: set and non-empty, it replaces the full-cap list
# for the lift; unset or empty, the full-cap list decides. Live through the 30 s operator refresh.

CHROLLO = "7069ef7a-7db3-5164-b3d3-b44237d2bb6a"
THOKANI = "e55a4d6f-dd26-5b56-9ded-9296ca8f199d"
FULL_CAP_LIVE = frozenset({CHROLLO, "%s@56" % DEGEN, RESELL})


def _lists(monkeypatch, reentry_raw):
    monkeypatch.setattr(caps.CACHE, "operator_snapshot", (FULL_CAP_LIVE, "ready", 0.0))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS", reentry_raw)
    monkeypatch.setattr(caps.CACHE, "reentry_snapshot", None)


OPERATOR_LIST = "%s, %s@solana, %s@4663" % (DEGEN, THOKANI, RESELL)


def test_with_the_list_set_a_full_cap_only_wallet_is_no_longer_lifted(monkeypatch):
    _lists(monkeypatch, OPERATOR_LIST)
    assert caps.cross_wallet_reentry_closes_by(CHROLLO, "solana") is None


def test_degen_bare_on_the_list_is_lifted_on_solana_and_every_chain(monkeypatch):
    _lists(monkeypatch, OPERATOR_LIST)
    for chain in ("solana", "4663", "56", "1"):
        assert caps.cross_wallet_reentry_closes_by(DEGEN, chain) == DEGEN, chain


def test_a_chain_scoped_list_entry_lifts_only_that_chain(monkeypatch):
    _lists(monkeypatch, OPERATOR_LIST)
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") == THOKANI
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "4663") is None
    assert caps.cross_wallet_reentry_closes_by(RESELL, "4663") == RESELL
    assert caps.cross_wallet_reentry_closes_by(RESELL, "solana") is None    # full-cap bare, not used


@pytest.mark.parametrize("raw", ["", "   ", " , ,"])
def test_an_empty_list_falls_back_to_the_full_cap_list(monkeypatch, raw):
    _lists(monkeypatch, raw)
    assert caps.cross_wallet_reentry_closes_by(CHROLLO, "solana") == CHROLLO
    assert caps.cross_wallet_reentry_closes_by(DEGEN, "solana") is None       # full-cap @56 only
    assert caps.cross_wallet_reentry_closes_by(DEGEN, "56") == DEGEN


@pytest.mark.parametrize("bad", ["%s@SOLANA" % THOKANI, "%s@" % THOKANI, "@solana", "%s @solana" % THOKANI,
                                 "%s@solana@4663" % THOKANI, "%s@sol ana" % THOKANI])
def test_a_malformed_entry_is_dropped_loudly(monkeypatch, caplog, bad):
    _lists(monkeypatch, "%s, %s" % (DEGEN, bad))
    with caplog.at_level("WARNING"):
        assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") is None
        assert caps.cross_wallet_reentry_closes_by(DEGEN, "solana") == DEGEN
    assert any(bad.strip() in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_a_list_of_only_malformed_entries_lifts_nobody_and_never_falls_back(monkeypatch):
    """Dropping only narrows: falling back to the full-cap list here would widen the lift."""
    _lists(monkeypatch, "%s@SOLANA, @56" % THOKANI)
    assert caps.cross_wallet_reentry_closes_by(CHROLLO, "solana") is None
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") is None


def test_the_list_is_live_through_the_operator_refresh(monkeypatch, tmp_path):
    _lists(monkeypatch, "")
    (tmp_path / ".env").write_text(
        "MEMECOIN_REAL_FULL_CAP_WALLET_IDS=%s\nMEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS=%s@solana\n"
        % (CHROLLO, THOKANI))
    assert caps.CACHE.refresh_operator_overrides(root=tmp_path)
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") == THOKANI
    assert caps.cross_wallet_reentry_closes_by(CHROLLO, "solana") is None
    (tmp_path / ".env").write_text("MEMECOIN_REAL_FULL_CAP_WALLET_IDS=%s\n" % CHROLLO)
    assert caps.CACHE.refresh_operator_overrides(root=tmp_path)            # knob removed: fallback
    assert caps.cross_wallet_reentry_closes_by(CHROLLO, "solana") == CHROLLO
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") is None


def test_a_failed_refresh_keeps_the_last_list(monkeypatch, tmp_path):
    _lists(monkeypatch, "")
    (tmp_path / ".env").write_text("MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS=%s@solana\n" % THOKANI)
    assert caps.CACHE.refresh_operator_overrides(root=tmp_path)
    (tmp_path / ".env").unlink()
    assert not caps.CACHE.refresh_operator_overrides(root=tmp_path)
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") == THOKANI


def test_clip_sizing_never_reads_the_reentry_list(monkeypatch):
    _lists(monkeypatch, "%s@solana" % THOKANI)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_WALLET_CAPS_ENABLED", True)
    assert caps.cross_wallet_reentry_closes_by(THOKANI, "solana") == THOKANI
    assert caps.entry_cap_usd(THOKANI, "solana") == cfg.MEMECOIN_REAL_UNPROVEN_MAX_ENTRY_USD
    assert caps.entry_cap_usd(CHROLLO, "solana") == cfg.MEMECOIN_REAL_MAX_ENTRY_USD


def test_the_status_shows_the_list_in_force_and_what_was_dropped(monkeypatch):
    _lists(monkeypatch, "%s@solana, %s@SOLANA" % (THOKANI, DEGEN))
    st = caps.CACHE.status()
    assert st["cross_wallet_reentry_wallet_ids"] == ["%s@solana" % THOKANI]
    assert st["cross_wallet_reentry_dropped"] == ["%s@SOLANA" % DEGEN]
    _lists(monkeypatch, "")
    st = caps.CACHE.status()
    assert st["cross_wallet_reentry_wallet_ids"] is None                 # the full-cap list decides


def test_the_knob_off_still_wins_over_the_list(monkeypatch):
    _lists(monkeypatch, OPERATOR_LIST)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CROSS_WALLET_REENTRY", False)
    assert caps.cross_wallet_reentry_closes_by(DEGEN, "solana") is None
