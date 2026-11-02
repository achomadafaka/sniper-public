"""Real-deploy feature (2026-07-10 spec): per-wallet real allowlist, auto-demote guardrail,
unquotable-24h zombie rail, and the accounting rollup (cash/deployed/unrealized/realized)."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open_real(c, uid="u1", mint="MINT", entry_notional=50.0, entry_price=0.02,
               network_id="solana", opened_at="2026-07-10T00:00:00+00:00"):
    pid = m.open_position(c, source_user_id=uid, source_handle="h", mint=mint, ticker="TKR",
                          entry_price=entry_price, entry_notional=entry_notional,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at=opened_at, strategy="alpha_b",
                          execution_mode="real", network_id=network_id)
    m.insert_fill(c, position_id=pid, kind="entry", price=entry_price,
                  qty=entry_notional / entry_price, notional=entry_notional,
                  fill_source="chain", fraction=1.0, created_at=opened_at)
    return pid


def _close_real(c, pid, realized, closed_at="2026-07-10T01:00:00+00:00"):
    m.claim_close(c, pid)
    m.finalize_close(c, pid, closed_at, realized, exit_reason="b_stop")


# --- allowlist column + helpers ---

def test_real_approved_defaults_off_and_toggles():
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.add_wallet(c, "u2", "bob", "")
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert any(w["source_user_id"] == "u1" and w["real_approved"] == 1
               for w in m.list_wallets(c))
    m.set_wallet_real_approved(c, "u1", False)
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_wallet_approval_audit_defaults_to_corrected_real_utc(monkeypatch):
    fixed = "2026-07-17T06:45:00+00:00"
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: fixed)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")

    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    m.record_wallet_real_follow_refusal(c, "u1", reason="test")

    rows = c.execute(
        "SELECT action, created_at FROM memecoin_wallet_approvals ORDER BY id",
    ).fetchall()
    # `set_chains` is audited too: since 2026-07-30 arming is approve + chain list, and the
    # authority assignment must leave the same append-only record as the flag.
    assert [(row["action"], row["created_at"]) for row in rows] == [
        ("approve", fixed),
        ("set_chains", fixed),
        ("approve_refused_fomo_follow", fixed),
    ]
    explicit = "2026-01-02T03:04:05+00:00"
    m.set_wallet_real_approved(c, "u1", False, now_iso=explicit)
    assert c.execute(
        "SELECT created_at FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1",
    ).fetchone()[0] == explicit


def test_paused_or_excluded_wallet_is_not_real_approved():
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    m.set_wallet_paused(c, "u1", True)
    assert m.real_approved_user_ids(c, network_id="solana") == set()   # paused wallets never real-trade


def test_curated_stats_armed_never_overstates_real():
    # The Traders-tab badge must equal the funded runner authority set.
    c = _c()
    for u, h in (("u1", "alice"), ("u2", "bob"), ("u3", "carol"), ("u4", "dave")):
        m.add_wallet(c, u, h, "")
    for u in ("u1", "u2", "u3"):
        m.set_wallet_real_approved(c, u, True, chains=["solana"], force=True)
    m.set_wallet_paused(c, "u2", True)
    m.set_wallet_excluded(c, "u3", True)
    rows = {r["source_user_id"]: r for r in m.list_curated_with_stats(c)}
    armed = m.real_approved_user_ids(c, network_id="solana")
    assert armed == {"u1"}
    assert rows["u1"]["armed"] == 1 and rows["u1"]["real_approved"] == 1
    assert rows["u2"]["armed"] == 0 and rows["u2"]["real_approved"] == 0
    assert rows["u3"]["armed"] == 0 and rows["u3"]["real_approved"] == 0
    assert rows["u3"]["excluded"] == 1
    assert rows["u4"]["armed"] == 0 and (rows["u4"].get("real_approved") or 0) == 0
    # invariant: armed flag == membership in the runner's effective arm set, for every row
    for uid, r in rows.items():
        assert bool(r["armed"]) == (uid in armed)


# --- wallet real record (auto-demote inputs) ---

def test_wallet_real_record_cumulative_and_consecutive():
    c = _c()
    for i, pnl in enumerate([+10.0, -5.0, -8.0, -2.0]):
        pid = _open_real(c, uid="u1", mint=f"M{i}")
        _close_real(c, pid, pnl, closed_at=f"2026-07-10T0{i}:30:00+00:00")
    rec = m.wallet_real_record(c, "u1")
    assert rec["realized_total"] == -5.0
    assert rec["consecutive_losses"] == 3
    assert rec["closed_count"] == 4
    # a win resets the streak
    pid = _open_real(c, uid="u1", mint="M9")
    _close_real(c, pid, +1.0, closed_at="2026-07-10T09:00:00+00:00")
    assert m.wallet_real_record(c, "u1")["consecutive_losses"] == 0


def test_wallet_real_record_restarts_cohort_after_reapproval(monkeypatch):
    """jester 2026-07-22: demoted at -$101.23, re-approved, and the lifetime sum still sat under
    the -$100 floor -> the NEXT close re-demoted it whatever the outcome."""
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-10T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    for i, pnl in enumerate([-60.0, -41.23]):
        _close_real(c, _open_real(c, uid="u1", mint=f"M{i}"), pnl,
                    closed_at=f"2026-07-1{i}T12:00:00+00:00")
    assert m.wallet_real_record(c, "u1")["realized_total"] == -101.23      # lifetime, pre-revoke

    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-20T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", False, actor="auto_demote")
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-22T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)

    rec = m.wallet_real_record(c, "u1")
    assert rec == {"realized_total": 0.0, "consecutive_losses": 0, "closed_count": 0}
    _close_real(c, _open_real(c, uid="u1", mint="M9"), -3.0,
                closed_at="2026-07-23T12:00:00+00:00")
    rec = m.wallet_real_record(c, "u1")
    assert rec["realized_total"] == -3.0 and rec["closed_count"] == 1      # cohort only


def test_wallet_real_record_ignores_redundant_approve_without_revoke(monkeypatch):
    """Re-clicking approve on a never-demoted wallet must NOT zero the loss counter."""
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-10T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    _close_real(c, _open_real(c, uid="u1", mint="M0"), -80.0,
                closed_at="2026-07-11T12:00:00+00:00")
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-12T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)                  # redundant approve
    assert m.wallet_real_record(c, "u1")["realized_total"] == -80.0


def test_demote_does_not_refire_on_first_close_after_reapproval(monkeypatch):
    """End-to-end of the same bug through the guardrail itself."""
    from sniper.services import memecoin_real_runner as rr
    _demote_cfg(monkeypatch)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-10T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    for i, pnl in enumerate([-60.0, -55.0]):
        _close_real(c, _open_real(c, uid="u1", mint=f"M{i}"), pnl,
                    closed_at=f"2026-07-1{i}T12:00:00+00:00")
    assert rr.check_wallet_demotion(c, "u1", network_id="solana", alert_fn=lambda *a: None) is True

    monkeypatch.setattr(m, "real_utc_now_iso", lambda: "2026-07-22T00:00:00+00:00")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    _close_real(c, _open_real(c, uid="u1", mint="M9"), +2.0,
                closed_at="2026-07-23T12:00:00+00:00")
    assert rr.check_wallet_demotion(c, "u1", network_id="solana", alert_fn=lambda *a: None) is False
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}


# --- accounting rollup ---

def test_real_accounting_math():
    c = _c()
    p1 = _open_real(c, mint="SOL1", entry_notional=50.0, entry_price=0.02)      # 2500 tokens
    _open_real(c, mint="0xBASE", entry_notional=37.0, entry_price=0.10, network_id="8453")
    m.reserve_pending(c, mint="SOLP", source_user_id="u1", strategy="alpha_b",
                      notional=50.0, reserved_at="t")
    pid = _open_real(c, mint="SOLDONE", entry_notional=40.0)
    _close_real(c, pid, +12.5)
    acct = m.real_accounting(c, price_map={"SOL1": 0.03}, day="2026-07-10")
    # deployed = open cost basis + reserved, per venue
    assert acct["by_network"]["solana"]["deployed"] == 100.0        # 50 open + 50 reserved
    assert acct["by_network"]["8453"]["deployed"] == 37.0
    # unrealized: SOL1 marked 0.03 vs 0.02 entry on 2500 tokens = +25; 0xBASE unquotable
    assert acct["by_network"]["solana"]["unrealized"] == 25.0
    assert acct["by_network"]["8453"]["unrealized"] == 0.0
    assert acct["by_network"]["8453"]["unquotable_n"] == 1
    assert acct["realized_total"] == 12.5
    assert acct["realized_today"] == 12.5
    assert acct["open_count"] == 2
    assert acct["reserved_count"] == 1
    # reserved notional is exposed separately so equity can exclude it (it is still in cash)
    assert acct["by_network"]["solana"]["reserved"] == 50.0
    assert acct["by_network"]["8453"]["reserved"] == 0.0


def test_real_accounting_marks_same_address_independently_by_chain():
    c = _c()
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="8453")
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="4663")

    acct = m.real_accounting(
        c,
        price_map={("8453", "0xabc"): 0.2, ("4663", "0xabc"): 0.3},
        day="2026-07-10",
    )

    assert acct["by_network"]["8453"]["unrealized"] == 50.0
    assert acct["by_network"]["4663"]["unrealized"] == 100.0
    assert acct["by_network"]["8453"]["unquotable_n"] == 0
    assert acct["by_network"]["4663"]["unquotable_n"] == 0


def test_real_accounting_never_reuses_bare_evm_mark_across_chains():
    c = _c()
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="8453")
    _open_real(c, mint="0xabc", entry_notional=50.0, entry_price=0.1, network_id="4663")

    acct = m.real_accounting(c, price_map={"0xabc": 0.2}, day="2026-07-10")

    assert acct["by_network"]["8453"]["unrealized"] == 0.0
    assert acct["by_network"]["4663"]["unrealized"] == 0.0
    assert acct["by_network"]["8453"]["unquotable_n"] == 1
    assert acct["by_network"]["4663"]["unquotable_n"] == 1


def test_real_accounting_route_haircut_and_equity(monkeypatch):
    # the accounting endpoint must (1) mark open bags NET of modeled exit slippage, not at raw mid,
    # and (2) compute equity = cash + (deployed - reserved) + unrealized, never double-counting the
    # reserved USDC that is still sitting in the wallet's cash balance.
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from sniper.execution.solana_quote import norm_mint
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SLIPPAGE_K", 1.0)
    monkeypatch.setattr(cfg, "MEMECOIN_SLIPPAGE_CAP", 0.5)

    c = _c()
    _open_real(c, mint="SOL1", entry_notional=50.0, entry_price=0.02)   # 2500 tokens on solana
    m.reserve_pending(c, mint="SOLP", source_user_id="u1", strategy="alpha_b",
                      notional=50.0, reserved_at="t")

    def stats_fn(mints):
        # mid 0.03, liquidity 750 -> slippage_frac = 1.0*(2500*0.03)/750 = 0.10 -> mark 0.027
        return {norm_mint(mnt): {"price": 0.03, "liquidity": 750.0} for mnt in mints}

    def balances_fn():
        return {"solana": {"cash_usd": 200.0, "native": 1.0}}

    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(
        {"conn": c, "acct_stats_fn": stats_fn, "acct_balances_fn": balances_fn}))
    a = app.test_client().get("/api/memecoin/real/accounting").get_json()
    sol = a["by_network"]["solana"]
    # unrealized is HAIRCUT: 2500*(0.027-0.02)=17.5, NOT the raw-mid 25.0
    assert sol["unrealized"] == 17.5
    assert sol["reserved"] == 50.0 and sol["deployed"] == 100.0   # 50 open cost + 50 reserved
    # equity = cash 200 + (deployed 100 - reserved 50) + unrealized 17.5 = 267.5 (reserved NOT doubled)
    assert sol["equity"] == 267.5


def test_real_accounting_route_uses_the_entry_gas_reserve(monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint

    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"4663"})
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_ENTRY_NATIVE", 0.00005)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_APPROVAL_NATIVE", 0.00002)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_EXIT_NATIVE", 0.00005)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2.0)

    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": _c(),
        "acct_balances_fn": lambda: {"4663": {"cash_usd": 234.83, "native": 0.002015}},
    }))
    gas = app.test_client().get("/api/memecoin/real/accounting").get_json()["by_network"]["4663"]

    assert gas["gas_required"] == 0.00021
    assert gas["gas_headroom"] == pytest.approx(0.001805)
    assert gas["gas_ok"] is True


def test_real_accounting_empty_book():
    c = _c()
    acct = m.real_accounting(c, price_map={}, day="2026-07-10")
    assert acct["realized_total"] == 0.0
    assert acct["open_count"] == 0
    assert acct["by_network"] == {}


# --- allowlist gate in the real hook ---

class _Ev:
    their_market_cap = 500e3   # sub-ceiling default so the mcap risk limiter (fail-closed
                               # on missing) doesn't shadow what each test actually targets

    def __init__(self, side, uid="u1", mint="MINT"):
        self.side = side
        self.source_user_id = uid
        self.mint = mint
        self.created_at = "2026-07-10T00:00:00+00:00"


# venue contexts keyed by resolved chain; make_real_hook's guard checks MEMBERSHIP only (an
# unroutable chain is refused before arming), so placeholder values suffice for these unit tests.
_VENUES = {"solana": object(), "4663": object(), "8453": object()}


def test_real_hook_blocks_buys_from_unapproved_wallets(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.add_wallet(c, "u2", "bob", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    calls = []
    monkeypatch.setattr(rr, "real_entry_hook",
                        lambda conn, ev, ctxs, **kw: calls.append(ev.source_user_id) or {"status": "filled"})
    hook = rr.make_real_hook(None, _VENUES, now_fn=lambda: "2026-08-31T12:00:00+00:00", trade_size=10.0, max_age_s=120)
    assert hook(c, _Ev("buy", "u2"))["status"] == "not_approved"
    assert hook(c, _Ev("buy", "u1")) == {"status": "filled"}
    assert calls == ["u1"]


def test_real_hook_mcap_ceiling(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 30e6)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    calls = []
    monkeypatch.setattr(rr, "real_entry_hook",
                        lambda conn, ev, ctxs, **kw: calls.append(1) or {"status": "filled"})
    hook = rr.make_real_hook(None, _VENUES, now_fn=lambda: "2026-08-31T12:00:00+00:00", trade_size=10.0, max_age_s=120)
    big = _Ev("buy", "u1"); big.their_market_cap = 32.2e6
    small = _Ev("buy", "u1"); small.their_market_cap = 800e3
    unknown = _Ev("buy", "u1"); unknown.their_market_cap = None
    assert hook(c, big)["status"] == "skipped_mcap"
    assert hook(c, small)["status"] == "filled"
    # 2026-07-16: unknown-on-Solana = assume-micro (triple-unresolvable is a minutes-old micro
    # by construction; the old "0% of armed feed buys lack it" fact died with the follow lane —
    # 163 armed buys/10d arrive mcap-less). Fail-closed survives behind the flag.
    assert hook(c, unknown)["status"] == "filled"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MCAP_ASSUME_MICRO", False)
    assert hook(c, unknown)["status"] == "skipped_mcap"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0.0)   # 0 = limiter off entirely
    # the consensus gate (2026-07-17) is a separate limiter — off here so this test stays
    # about the CEILING knob only (consensus has its own tests in test_memecoin_micro_policy)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP", 0.0)
    assert hook(c, big)["status"] == "filled"
    assert hook(c, unknown)["status"] == "filled"
    assert len(calls) == 4


def test_real_hook_sells_bypass_allowlist(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    c = _c()
    calls = []
    monkeypatch.setattr(rr, "real_mirror_on_sell",
                        lambda conn, ev, ctxs, **kw: calls.append(ev.source_user_id) or {"status": "closed"})
    hook = rr.make_real_hook(None, _VENUES, now_fn=lambda: "2026-08-31T12:00:00+00:00", trade_size=10.0, max_age_s=120)
    assert hook(c, _Ev("sell", "u_not_approved")) == {"status": "closed"}
    assert calls == ["u_not_approved"]


# --- unquotable zombie rail ---

def _rails(monkeypatch, unquotable=86400.0):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", unquotable)


def test_unquotable_rail_fires_after_24h_without_quote(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _rails(monkeypatch)
    now = "2026-07-11T01:00:00+00:00"
    pos = {"entry_price": 0.02, "opened_at": "2026-07-10T00:00:00+00:00",
           "derisked_at": None, "last_quote_ts": None, "stuck_bag": 0}
    assert rr.alpha_b_real_exit_reason(pos, None, now) == "unquotable"      # never quoted, >24h
    pos["last_quote_ts"] = "2026-07-10T23:30:00+00:00"
    assert rr.alpha_b_real_exit_reason(pos, None, now) is None              # quoted 1.5h ago
    pos["last_quote_ts"] = "2026-07-10T00:30:00+00:00"
    assert rr.alpha_b_real_exit_reason(pos, None, now) == "unquotable"      # quote stale >24h


def test_unquotable_rail_respects_off_and_stuck_and_live_quote(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _rails(monkeypatch, unquotable=0.0)                                     # 0 = off
    now = "2026-07-12T00:00:00+00:00"
    pos = {"entry_price": 0.02, "opened_at": "2026-07-10T00:00:00+00:00",
           "derisked_at": None, "last_quote_ts": None, "stuck_bag": 0}
    assert rr.alpha_b_real_exit_reason(pos, None, now) is None
    _rails(monkeypatch)                                                     # on again
    pos["stuck_bag"] = 1
    assert rr.alpha_b_real_exit_reason(pos, None, now) is None              # stuck: no retry spam
    pos["stuck_bag"] = 0
    assert rr.alpha_b_real_exit_reason(pos, 0.019, now) is None             # live quote -> normal rails


def test_exit_sweep_stamps_last_quote_on_real_positions(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _rails(monkeypatch)
    c = _c()
    pid = _open_real(c, mint="SOLQ")
    ctx = {"exit_fn": lambda *a, **k: {"status": "closed"}, "token_fn": lambda mint: 0.0}
    rr.real_exit_sweep(c, {"solana": ctx},
                       price_fn=lambda mints: {"SOLQ": {"price": 0.021}},
                       now_fn=lambda: "2026-07-10T02:00:00+00:00")
    p = m.get_position(c, pid)
    assert p["last_quote_price"] == 0.021
    assert p["last_quote_ts"] == "2026-07-10T02:00:00+00:00"


# --- auto-demote guardrail ---

def _demote_cfg(monkeypatch, loss=100.0, consec=4):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DEMOTE_LOSS_USD", loss, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DEMOTE_CONSEC", consec, raising=False)


def test_demote_on_cumulative_loss(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _demote_cfg(monkeypatch)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    alerts_fired = []
    for i, pnl in enumerate([-60.0, +5.0, -55.0]):                          # cum -110 <= -100
        pid = _open_real(c, uid="u1", mint=f"M{i}")
        _close_real(c, pid, pnl, closed_at=f"2026-07-10T0{i}:30:00+00:00")
    demoted = rr.check_wallet_demotion(c, "u1", network_id="solana", alert_fn=lambda *a: alerts_fired.append(a))
    assert demoted is True
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    assert len(alerts_fired) == 1


def test_demote_on_consecutive_losses(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _demote_cfg(monkeypatch, loss=1000.0, consec=3)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    for i in range(3):                                                      # 3 straight small losses
        pid = _open_real(c, uid="u1", mint=f"M{i}")
        _close_real(c, pid, -1.0, closed_at=f"2026-07-10T0{i}:30:00+00:00")
    assert rr.check_wallet_demotion(c, "u1", network_id="solana", alert_fn=lambda *a: None) is True
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_no_demote_below_thresholds_or_unapproved(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    _demote_cfg(monkeypatch)
    c = _c()
    m.add_wallet(c, "u1", "alice", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    pid = _open_real(c, uid="u1", mint="M0")
    _close_real(c, pid, -50.0)                                              # under both thresholds
    assert rr.check_wallet_demotion(c, "u1", network_id="solana", alert_fn=lambda *a: None) is False
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert rr.check_wallet_demotion(c, "u_unknown", network_id="solana", alert_fn=lambda *a: None) is False


# --- Telegram enrichment: watch everything, trade few ---

class _FeedEv:
    fomo_event_id = "e1"
    source_user_id = "u1"
    source_handle = "zinceth"
    mint = "So1MintXYZ"
    ticker = "PEPE"
    side = "buy"
    their_price = 0.02
    their_market_cap = 500000.0
    their_size_usd = 2500.0
    is_first_buy = 1
    created_at = "2026-07-10T00:00:00+00:00"
    received_at = "2026-07-10T00:00:01+00:00"
    lag_s = 0.4
    signal_source = "global_feed"
    network_id = "solana"


def test_alert_message_carries_wallet_context():
    from sniper.services.memecoin_telegram import build_alert_message
    # record is the HONEST alpha_b metric (fomo-excluded), NOT the inflated all-arms total; gate_ok
    # renders a ✓/✗ so an armed-but-failing wallet is visible.
    msg = build_alert_message(_FeedEv(), context={"real_armed": True, "gate_ok": True,
                                                  "authority_status": "armed",
                                                  "gate_applicable": True,
                                                  "paper_pnl": 461.10, "paper_n": 14})
    assert "ARMED ⚡" in msg
    assert "REAL-ELIGIBLE" not in msg
    assert "honest αb +$461 · 14 closes" in msg
    assert "gate ✓" in msg
    msg2 = build_alert_message(_FeedEv(), context={"real_armed": False, "gate_ok": False,
                                                   "authority_status": "watch_only",
                                                   "gate_applicable": True,
                                                   "paper_pnl": -34.48, "paper_n": 20})
    assert "watch-only" in msg2
    assert "honest αb -$34 · 20 closes" in msg2
    assert "gate ✗" in msg2
    assert build_alert_message(_FeedEv())    # no context -> still builds (back-compat)


def test_alert_badge_is_exact_chain_or_unfunded_watch_only():
    from sniper.services.memecoin_telegram import build_alert_message
    armed = build_alert_message(_FeedEv(), context={"real_armed": True,
                                                    "authority_status": "armed",
                                                    "gate_applicable": True,
                                                    "paper_pnl": 80.0, "paper_n": 54})
    assert "ARMED ⚡" in armed
    watch = build_alert_message(_FeedEv(), context={"real_armed": False,
                                                    "authority_status": "watch_only",
                                                    "gate_applicable": True,
                                                    "paper_pnl": 1.0, "paper_n": 3})
    assert "watch-only" in watch
    unfunded = build_alert_message(_FeedEv(), context={
        "real_armed": False,
        "authority_status": "unfunded_chain",
        "gate_applicable": False,
    })
    assert "unfunded chain - watch-only" in unfunded
    assert "paper gate: not applicable" in unfunded


def test_prepare_alert_builds_wallet_context():
    from sniper.services.memecoin_runner import _prepare_alert
    c = _c()
    m.add_wallet(c, "u1", "zinceth", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    pid = m.open_position(c, source_user_id="u1", source_handle="zinceth", mint="X", ticker="T",
                          entry_price=1.0, entry_notional=100.0, entry_fill_source="dexscreener",
                          entry_lag_s=0.5, their_size_usd=0.0,
                          opened_at="2026-07-09T00:00:00+00:00", strategy="alpha_b",
                          network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="dexscreener", fraction=1.0,
                  created_at="2026-07-09T00:00:00+00:00")
    m.insert_fill(c, position_id=pid, kind="mirror_exit", price=1.25, qty=100.0, notional=125.0,
                  fill_source="dexscreener", fraction=1.0,
                  created_at="2026-07-09T01:00:00+00:00")
    m.claim_close(c, pid)
    m.finalize_close(c, pid, "2026-07-09T01:00:00+00:00", 25.0, exit_reason="b_mirror")
    job = _prepare_alert(c, _FeedEv())
    assert job and "ARMED ⚡" in job["message"]
    # 1 honest alpha_b close / +$25 -> below the 5-close gate -> ✗ (the wiring end-to-end)
    assert "honest αb +$25 · 1 close" in job["message"]
    assert "gate ✗" in job["message"]
    assert "Source buy received. This notice does not report our entry result." in job["message"]


def test_prepare_alert_uses_signal_chain_for_authority_and_evidence():
    from types import SimpleNamespace
    from sniper.services.memecoin_runner import _prepare_alert

    c = _c()
    m.add_wallet(c, "u1", "zinceth", "")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)
    pid = m.open_position(
        c, source_user_id="u1", source_handle="zinceth", mint="R", ticker="R",
        entry_price=1.0, entry_notional=10.0, entry_fill_source="route_quote",
        entry_lag_s=0.1, their_size_usd=1.0, opened_at="2026-07-09T00:00:00+00:00",
        strategy="alpha_b", network_id="4663")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=10.0, notional=10.0,
                  fill_source="route_quote", fraction=1.0,
                  created_at="2026-07-09T00:00:00+00:00")
    m.insert_fill(c, position_id=pid, kind="mirror_exit", price=0.5, qty=10.0, notional=5.0,
                  fill_source="route_quote", fraction=1.0,
                  created_at="2026-07-09T01:00:00+00:00")
    m.close_position(c, pid, "2026-07-09T01:00:00+00:00", -5.0, exit_reason="b_mirror")

    robin = SimpleNamespace(**{
        name: getattr(_FeedEv, name) for name in (
            "source_user_id", "source_handle", "mint", "ticker", "side", "their_price",
            "their_market_cap", "their_size_usd", "is_first_buy", "created_at", "received_at",
            "lag_s", "signal_source")})
    robin.fomo_event_id = "e-robin"
    robin.network_id = "4663"
    job = _prepare_alert(c, robin)

    assert "watch-only" in job["message"]
    assert "ARMED ⚡" not in job["message"]
    assert "honest αb -$5 · 1 close" in job["message"]

    robin.fomo_event_id = "e-bsc"
    robin.network_id = "56"
    job = _prepare_alert(c, robin)
    assert "unfunded chain - watch-only" in job["message"]
    assert "paper gate: not applicable" in job["message"]


# --- P0 regression: paper and real must coexist on the same (uid, mint, strategy) ---

def test_paper_and_real_same_key_coexist():
    c = _c()
    kw = dict(source_user_id="u1", source_handle="h", mint="MINT", ticker="T", entry_price=0.02,
              entry_notional=50.0, entry_fill_source="x", entry_lag_s=0.1, their_size_usd=0.0,
              opened_at="t", strategy="alpha_b")
    m.open_position(c, **kw)                                        # paper alpha_b
    pid = m.open_position(c, **{**kw, "execution_mode": "real", "network_id": "solana"})
    assert pid                                                      # real fill must ledger, not raise
    # dup within the SAME mode is still rejected (the race guard)
    import pytest, sqlite3 as _sq
    with pytest.raises(_sq.IntegrityError):
        m.open_position(c, **{**kw, "execution_mode": "real", "network_id": "solana"})
    with pytest.raises(_sq.IntegrityError):
        m.open_position(c, **kw)


def test_dedup_sweep_is_mode_scoped():
    c = _c()
    kw = dict(source_user_id="u1", source_handle="h", mint="MINT", ticker="T", entry_price=0.02,
              entry_notional=50.0, entry_fill_source="x", entry_lag_s=0.1, their_size_usd=0.0,
              opened_at="t", strategy="alpha_b")
    m.open_position(c, **kw)
    m.open_position(c, **{**kw, "execution_mode": "real", "network_id": "solana"})
    m.ensure_memecoin_tables(c)                                     # re-run migrations (boot path)
    stays = c.execute("SELECT execution_mode FROM memecoin_positions WHERE is_open=1").fetchall()
    assert sorted(r[0] for r in stays) == ["paper", "real"]         # dedup must NOT retire the real row


# --- stop debounce: a one-sweep wick must not fire the real stop ---

def _sweep(c, price, exits_log, now="2026-07-10T02:00:00+00:00"):
    from sniper.services import memecoin_real_runner as rr
    ctx = {"exit_fn": lambda conn, pos, reason, now: exits_log.append(reason) or {"status": "closed"},
           "token_fn": lambda mint: 0.0,
           "preview_exit_fn": lambda pos: {
               "expected_usdc": price / pos["entry_price"] * pos["entry_notional"],
           }}
    rr.real_exit_sweep(c, {"solana": ctx},
                       price_fn=lambda mints: {"SOLQ": {"price": price}},
                       now_fn=lambda: now)


def test_stop_requires_consecutive_sweeps(monkeypatch):
    _rails(monkeypatch)
    # These three assert the DEBOUNCE contract, and _rails sets stop=50 while the fixture breaches
    # at -55%, which is past MEMECOIN_REAL_STOP_INSTANT_PCT=50 (2026-09-03) -- so the depth bypass
    # would sell on the first read and this file would be testing the wrong mechanism. Pin it off.
    # The interaction itself, including the stop_pct >= instant_pct consequence, is asserted in
    # tests/test_memecoin_stop_instant_depth.py.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2, raising=False)
    c = _c()
    pid = _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []
    _sweep(c, 0.009, exits)                                         # −55%: first sighting
    assert exits == []                                              # NOT fired — unconfirmed wick
    assert m.get_position(c, pid)["stop_hits"] == 1
    _sweep(c, 0.009, exits, now="2026-07-10T02:00:01+00:00")        # still −55% a tick later
    assert exits == ["b_stop"]


def test_a_rail_wake_sweep_does_not_shorten_the_stop_debounce(monkeypatch):
    """A sweep count means "held about N ticks" (2026-09-27, Alpha). Rail wakes run extra sweeps
    between ticks, so a confirmation 0.3 s after the first must not satisfy a 2-sweep debounce;
    one a tick later must."""
    _rails(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_FASTLANE_TICK_S", 1.0, raising=False)
    c = _c()
    pid = _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []
    _sweep(c, 0.009, exits, now="2026-07-10T02:00:00+00:00")
    _sweep(c, 0.009, exits, now="2026-07-10T02:00:00.300000+00:00")   # a wake, 0.3 s later
    assert exits == [] and m.get_position(c, pid)["stop_hits"] == 1
    _sweep(c, 0.009, exits, now="2026-07-10T02:00:01+00:00")          # the next tick
    assert exits == ["b_stop"]


def test_first_stop_debounce_sweep_writes_pending_audit(monkeypatch):
    _rails(monkeypatch)
    # These three assert the DEBOUNCE contract, and _rails sets stop=50 while the fixture breaches
    # at -55%, which is past MEMECOIN_REAL_STOP_INSTANT_PCT=50 (2026-09-03) -- so the depth bypass
    # would sell on the first read and this file would be testing the wrong mechanism. Pin it off.
    # The interaction itself, including the stop_pct >= instant_pct consequence, is asserted in
    # tests/test_memecoin_stop_instant_depth.py.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2, raising=False)
    c = _c()
    pid = _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []

    _sweep(c, 0.009, exits)

    row = c.execute(
        "SELECT allowed, detail FROM memecoin_exit_trigger_audit "
        "WHERE position_id=? AND reason='b_stop' ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
    assert exits == []
    assert row is not None and row["allowed"] == 0
    detail = __import__("json").loads(row["detail"])
    assert detail == {"code": "debounce_pending", "hits": 1, "required_hits": 2}


def test_wick_resets_stop_counter(monkeypatch):
    _rails(monkeypatch)
    # These three assert the DEBOUNCE contract, and _rails sets stop=50 while the fixture breaches
    # at -55%, which is past MEMECOIN_REAL_STOP_INSTANT_PCT=50 (2026-09-03) -- so the depth bypass
    # would sell on the first read and this file would be testing the wrong mechanism. Pin it off.
    # The interaction itself, including the stop_pct >= instant_pct consequence, is asserted in
    # tests/test_memecoin_stop_instant_depth.py.
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 0.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2, raising=False)
    c = _c()
    pid = _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []
    _sweep(c, 0.009, exits)                                         # wick to −55%
    _sweep(c, 0.019, exits)                                         # next sweep back to −5%
    assert exits == [] and m.get_position(c, pid)["stop_hits"] == 0  # counter released
    _sweep(c, 0.009, exits)                                         # a fresh drop starts over
    assert exits == []


def test_confirm_sweeps_1_is_legacy_immediate(monkeypatch):
    _rails(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1, raising=False)
    c = _c()
    _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []
    _sweep(c, 0.009, exits)
    assert exits == ["b_stop"]


def test_tp_is_never_debounced(monkeypatch):
    # selling into a spike is favorable — the TP must fire on the first sweep it sees
    _rails(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2, raising=False)
    c = _c()
    _open_real(c, mint="SOLQ", entry_price=0.02)
    exits = []
    _sweep(c, 0.029, exits)                                         # +45% >= TP 40
    assert exits == ["real_tp"]


# --- alert truth: ENTRY prints the true chain spend, never the static config clip ---

def test_entry_alert_uses_executor_spend_not_config_clip(monkeypatch):
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_alerts as alerts
    seen = []
    monkeypatch.setattr(alerts, "alert_entry",
                        lambda mint, price, notional, sig, **k: seen.append(notional))
    rr._alert_entry_result("MINT", 10.0,   # caller's static fallback clip
                           {"status": "filled", "fill_price": 0.000963,
                            "notional": 50.69, "sig": "S"})
    assert seen == [50.69]                                       # the REAL spend, not $10
    rr._alert_entry_result("MINT", 10.0, {"status": "filled", "fill_price": 1.0, "sig": "S"})
    assert seen[1] == 10.0                                       # legacy result -> fallback only


def test_solana_executor_returns_true_spend():
    from sniper.execution.solana_swap_executor import execute_entry
    import inspect
    c = _c()
    pid_res = m.reserve_pending(c, mint="MINT", source_user_id="u1", strategy="alpha_b",
                                notional=50.69, reserved_at="t")
    kw = dict(conn=c, pending_id=pid_res, mint="MINT", usd_amount=50.69, strategy="alpha_b",
              source_user_id="u1", now_iso="t",
              quote_fn=lambda mint, usd: {
                  "inAmount": "50690000", "outAmount": "52637000000",
                  "otherAmountThreshold": "50000000000", "priceImpactPct": 0.001},
              reverse_quote_fn=lambda mint, amount: {
                  # price-neutral for this $50.69 clip: 1,000 token atomic per USDC atomic.
                  # `outAmount: "1"` was one atomic back on the whole bag, and the entry
                  # round-trip cap reads this same quote as a total loss.
                  "inAmount": str(amount), "outAmount": str(int(amount) // 1000),
                  "otherAmountThreshold": str(int(amount) // 1000)},
              sign_fn=lambda q: ("RAW", "SIG"), send_fn=lambda r: None,
              confirm_fn=lambda s: "confirmed",
              usdc_fn=(lambda it: (lambda *a: next(it)))(iter([500.0, 449.31, 449.31])),
              token_fn=(lambda it: (lambda *a: next(it)))(iter([0.0, 52637.0, 52637.0])),
              sleep_fn=lambda s: None)
    sig = inspect.signature(execute_entry)
    kw = {k: v for k, v in kw.items() if k in sig.parameters}
    r = execute_entry(**kw)
    assert r["status"] == "filled"
    assert abs(r["notional"] - 50.69) < 1e-9


def test_real_fill_backfills_ticker_and_handle(monkeypatch, tmp_path):
    from sniper.services import memecoin_real_runner as rr
    from sniper.services import memecoin_arming
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    seen = {}

    def execute_fn(conn, pid, mint, size, now, impact_cap_pct=None,
                   entry_min_slot=None, entry_max_slot=None, entry_head_fn=None):
        seen["entry_window"] = (entry_min_slot, entry_max_slot, entry_head_fn)
        rowid = m.open_position(conn, source_user_id="u1", source_handle="(real)", mint=mint,
                                ticker="", entry_price=1.0, entry_notional=size,
                                entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                                opened_at=now, strategy="alpha_b", execution_mode="real",
                                network_id="solana")
        return {"status": "filled", "position_id": rowid, "fill_price": 1.0, "notional": size,
                "sig": "S"}

    class _Ev:
        mint = "So1MintReal"; source_user_id = "u1"; source_handle = "zinceth"
        ticker = "PEPE"; side = "buy"; network_id = None
        created_at = "2026-07-10T00:00:00+00:00"
        source_block_number = 100
    rc = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
          "token_safety_fn": lambda m_: (True, "ok"), "execute_fn": execute_fn,
          "kill_path": str(tmp_path / "nokill")}
    res = rr.real_entry_hook(c, _Ev(), {"solana": rc}, now_fn=lambda: "2026-07-10T00:00:01+00:00",
                             trade_size=10.0, max_age_s=120)
    assert res["status"] == "filled"
    p = m.get_position(c, res["position_id"])
    assert p["ticker"] == "PEPE" and p["source_handle"] == "zinceth"
    assert seen["entry_window"][:2] == (None, None)
    assert callable(seen["entry_window"][2])


# --- expired-unfound reservation recovery (Solana only) ---

def test_recovery_expires_ancient_solana_only_after_repeated_not_found():
    from sniper.services import memecoin_real_runner as rr
    c = _c()
    pid_sol = m.reserve_pending(c, mint="SOLDEAD", source_user_id="u1", strategy="alpha_b",
                                notional=50.0, reserved_at="2026-07-11T10:00:00+00:00")
    m.set_pending_sig(c, pid_sol, "SIGDEAD", "2026-07-11T10:00:01+00:00")
    pid_evm = m.reserve_pending(c, mint="0xEVMWAIT", source_user_id="u1", strategy="alpha_b",
                                notional=37.0, reserved_at="2026-07-11T10:00:00+00:00",
                                network_id="8453")
    m.set_pending_sig(c, pid_evm, "0xHASHWAIT", "2026-07-11T10:00:01+00:00")
    out = []
    for now in (
        "2026-07-11T11:00:00+00:00",
        "2026-07-11T11:01:00+00:00",
        "2026-07-11T11:02:00+00:00",
    ):
        out += rr.recover_stale_reservations(
            c, confirm_for=lambda net, sig: "not_found", now_iso=now,
        )
    assert (pid_sol, "released_expired") in out          # solana blockhash long dead -> release
    assert (pid_evm, "released_expired") not in out      # EVM nonce may still land -> KEEP
    stats = {r[0]: r[1] for r in c.execute("SELECT id, status FROM memecoin_pending_entries")}
    assert stats[pid_sol] == "failed"                    # released-for-audit status
    assert stats[pid_evm] == "reserved"


# --- firehose retention: every event kept once, both sides, no size floor ---

def test_firehose_retains_all_events_once():
    c = _c()
    evs = [
        {"fomo_event_id": "f1", "source_user_id": "u1", "source_handle": "a", "mint": "M1",
         "ticker": "T1", "side": "buy", "their_price": 0.01, "their_market_cap": 5e5,
         "their_size_usd": 50.0, "is_first_buy": 1, "created_at": "t1", "received_at": "r1",
         "network_id": "solana"},
        {"fomo_event_id": "f2", "source_user_id": "u2", "source_handle": "b", "mint": "M1",
         "ticker": "T1", "side": "sell", "their_price": 0.02, "their_market_cap": 5e5,
         "their_size_usd": 3.0, "is_first_buy": 0, "created_at": "t2", "received_at": "r2",
         "network_id": "solana"},
    ]
    assert m.insert_firehose_events(c, evs, curated_ids={"u1"}, armed_ids=set()) == 2
    assert m.insert_firehose_events(c, evs) == 0          # re-delivered feed -> deduped
    rows = c.execute("SELECT side, their_size_usd, was_curated, was_real_approved "
                     "FROM memecoin_firehose ORDER BY id").fetchall()
    # point-in-time cohort stamped at ingest: u1 curated-not-armed, u2 neither
    assert [tuple(r) for r in rows] == [("buy", 50.0, 1, 0), ("sell", 3.0, 0, 0)]
    assert m.insert_firehose_events(c, []) == 0


def test_quote_ledger_minute_dedup():
    c = _c()
    pm = {"M1": {"price": 0.01, "liquidity": 5e4}, "M2": {"price": 2.0}, "BAD": {"price": None}}
    assert m.record_quote_observations(c, pm, "2026-07-11T20:00:05+00:00") == 2
    # same minute -> deduped; next minute -> new rows
    assert m.record_quote_observations(c, pm, "2026-07-11T20:00:35+00:00") == 0
    assert m.record_quote_observations(c, pm, "2026-07-11T20:01:05+00:00") == 2
    n = c.execute("SELECT COUNT(*) FROM memecoin_quote_ledger").fetchone()[0]
    assert n == 4


# --- paper unquotable cleanup: dark >24h -> closed, quoted stays open ---

def test_paper_unquotable_cleanup():
    from sniper.services.memecoin_exits import paper_unquotable_cleanup
    c = _c()
    def _paper(mint, opened, lq=None, lq_ts=None):
        pid = m.open_position(c, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
                              entry_price=0.01, entry_notional=100.0, entry_fill_source="d",
                              entry_lag_s=0.1, their_size_usd=0.0, opened_at=opened,
                              strategy="control")
        m.insert_fill(c, position_id=pid, kind="entry", price=0.01, qty=10000.0, notional=100.0,
                      fill_source="d", fraction=1.0, created_at=opened)
        if lq:
            m.update_position_last_quote(c, pid, lq, lq_ts)
        return pid
    now = "2026-07-11T12:00:00+00:00"
    dark_never = _paper("DARK1", "2026-07-10T10:00:00+00:00")                      # 26h, never quoted
    dark_stale = _paper("DARK2", "2026-07-09T00:00:00+00:00", 0.005,
                        "2026-07-10T09:00:00+00:00")                               # quote 27h old
    fresh = _paper("LIVE1", "2026-07-10T10:00:00+00:00", 0.02,
                   "2026-07-11T11:59:00+00:00")                                    # quoted 1m ago
    young = _paper("NEW1", "2026-07-11T11:00:00+00:00")                            # 1h old, no quote yet
    closed = dict(paper_unquotable_cleanup(c, now))
    assert set(closed) == {dark_never, dark_stale}
    assert m.get_position(c, dark_never)["exit_reason"] == "unquotable"
    # UPDATED 2026-08-29, and these two lines are the whole distinction side by side.
    # DARK1 never had a quote, so cleanup fell back to the ENTRY price and the -0.0 that came out
    # was a fabrication, not a flat outcome. It books NULL now.
    assert m.get_position(c, dark_never)["realized_pnl"] is None
    # DARK2 had a real (27h stale) quote. A poor observation is still an observation and keeps its
    # number — erasing it would be the mirror of the bug this change fixes.
    assert abs(m.get_position(c, dark_stale)["realized_pnl"] - (-50.0)) < 1e-9     # at last quote 0.005
    assert m.get_position(c, fresh)["is_open"] == 1
    assert m.get_position(c, young)["is_open"] == 1


def test_kill_path_is_absolute_and_cwd_proof(tmp_path, monkeypatch):
    from sniper.services import memecoin_arming as arm
    import os
    monkeypatch.chdir(tmp_path)                       # hostile CWD
    p = arm._kill_path()
    assert os.path.isabs(p)
    assert not str(p).startswith(str(tmp_path))       # never lands in the stray CWD


# --- evm-unknown chain resolution: recover Base signals the feed delivered without a chain id ---

class _RouteEv:
    def __init__(self, mint, network_id=""):
        self.mint = mint
        self.network_id = network_id


def test_unknown_evm_chain_resolves_before_routing():
    from sniper.services.memecoin_real_leg import route_real_entry
    calls = []

    def resolver(mint):
        calls.append(mint)
        return "8453"
    # feed omitted the chain -> resolve -> Base -> route
    assert route_real_entry(_RouteEv("0xABC", ""), {"8453"}, resolve_fn=resolver) == "evm:8453"
    assert route_real_entry(_RouteEv("0xABC", "evm-unknown"), {"8453"}, resolve_fn=resolver) == "evm:8453"
    assert calls == ["0xABC", "0xABC"]


def test_resolved_non_allowlisted_and_unresolvable_still_drop():
    from sniper.services.memecoin_real_leg import route_real_entry
    assert route_real_entry(_RouteEv("0xABC", ""), {"8453"}, resolve_fn=lambda m: "56") == "drop"
    assert route_real_entry(_RouteEv("0xABC", ""), {"8453"}, resolve_fn=lambda m: None) == "drop"


def test_feed_authoritative_chain_is_never_second_guessed():
    from sniper.services.memecoin_real_leg import route_real_entry

    def resolver(mint):
        raise AssertionError("resolver must not run when the feed named the chain")
    assert route_real_entry(_RouteEv("0xABC", "56"), {"8453"}, resolve_fn=resolver) == "drop"
    assert route_real_entry(_RouteEv("0xABC", "8453"), {"8453"}, resolve_fn=resolver) == "evm:8453"
    assert route_real_entry(_RouteEv("So1Mint", ""), {"8453"}, resolve_fn=resolver) == "solana"


def test_norm_network_canonicalizes_fomo_solana_id():
    from sniper.feeds.fomo_feed import _norm_network
    assert _norm_network("1399811149") == "solana"    # fomo's numeric Solana id
    assert _norm_network(1399811149) == "solana"
    assert _norm_network("8453") == "8453"
    assert _norm_network(None) == ""
    assert _norm_network("") == ""


def test_pump_recompute_rejects_unit_mismatch_prints():
    from sniper.services.memecoin_pump import recompute
    # the HYPE bad print: ref ~67, mark ~330k (4,916x) — must be dropped, not booked as +491,553%
    g, peak_h, last, n = recompute(67.0994, {"60": {"price": 329896.33}, "300": {"price": 70.0}})
    assert g is not None and g < 1000                 # only the sane +4.3% mark counts
    assert abs(g - (70.0 / 67.0994 - 1) * 100) < 0.1
    from sniper.execution.solana_quote import resolve_evm_chain

    def http(method, url):
        return 200, {"pairs": [{"chainId": "base", "pairAddress": "base-pair",
                                "priceUsd": "1.0", "liquidity": {"usd": 1},
                                "baseToken": {"address": "0xabc", "symbol": "X"}}]}, None
    assert resolve_evm_chain("0xabc", http=http) == "8453"

    def http56(method, url):
        return 200, {"pairs": [{"chainId": "bsc", "pairAddress": "bsc-pair",
                                "priceUsd": "1.0", "liquidity": {"usd": 1},
                                "baseToken": {"address": "0xabc", "symbol": "X"}}]}, None
    assert resolve_evm_chain("0xabc", http=http56) == "56"
    assert resolve_evm_chain("0xabc", http=lambda m, u: (200, {"pairs": []}, None)) is None


# --- cash/base-asset guard: WETH/cbBTC routing legs are never copyable signals ---

def test_classifier_treats_weth_cbbtc_as_cash_legs():
    from sniper.analysis.fomo_account.classify import classify_swap, is_cash_asset
    assert is_cash_asset("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")   # checksummed WETH
    assert is_cash_asset("0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf")   # cbBTC
    assert not is_cash_asset("CizcDa22HJjXybKLriibqP167GcJjqiDHDPtuiMmpump")
    # memecoin -> WETH is a SELL, not a "buy of WETH"
    kind, mint, usd, qty = classify_swap({
        "inTokenAddress": "SomeMemeMint111", "outTokenAddress": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        "humanUsdAmountIn": 0.0, "humanUsdAmountOut": 500.0, "inHumanAmount": 1000.0})
    assert kind == "sell" and mint == "SomeMemeMint111"


def test_copy_engine_skips_base_asset_buys():
    from sniper.services.memecoin_copy_trader import copy_event_to_arms

    class _Buy:
        side = "buy"
        mint = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"   # cbBTC
        their_price = 63939.0
        fomo_event_id = "e-cb"
        source_user_id = "u1"
    c = _c()
    out = copy_event_to_arms(c, _Buy(), 100.0, "t", arms=["control", "alpha_b"],
                             fill_fn=lambda *a: (_ for _ in ()).throw(AssertionError("must not quote")))
    assert out == {"control": "skipped_base_asset", "alpha_b": "skipped_base_asset"}
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_real_entry_drops_base_asset(monkeypatch, tmp_path):
    from sniper.services.memecoin_real_leg import real_entry
    from sniper.services import memecoin_arming
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)

    class _Ev:
        mint = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"   # WETH
        source_user_id = "u1"
        network_id = "1"
    c = _c()
    r = real_entry(c, _Ev(), day="2026-07-10", usdc_balance=500.0, sol_balance=1.0,
                   now_iso="t", trade_size=50.0, token_safety_fn=lambda m: (True, "ok"),
                   execute_fn=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute")),
                   kill_path=str(tmp_path / "nokill"))
    assert r == {"status": "dropped_base_asset"}


def test_entry_exit_alerts_carry_handle_and_ticker():
    from sniper.services import memecoin_alerts as alerts
    sent = []
    alerts.alert_entry("So1MintXYZ", 0.02, 50.70, "SIGSIGSIG",
                       handle="zinceth", ticker="PEPE", send_fn=lambda t: sent.append(t) or (True, 1.0))
    assert "PEPE" in sent[0] and "zinceth" in sent[0] and "$50.70" in sent[0]
    alerts.alert_exit("So1MintXYZ", 25.35, "mirror", "SIG", handle="zinceth", ticker="PEPE",
                      entry_notional=50.70, send_fn=lambda t: sent.append(t) or (True, 1.0))
    assert "PEPE" in sent[1] and "+50.0%" in sent[1] and "$+25.35" in sent[1]


# --- pre-gate balance reads run concurrently (2026-07-28) ---

def test_concurrent_reads_preserves_order_and_runs_in_parallel():
    """These were positional arguments, so the gate paid their SUM: on 4663 a Binance ETH/USD read
    plus two chain reads at ~160ms, all before the reservation was attempted."""
    import time
    from sniper.services.memecoin_real_runner import _concurrent_reads

    def slow(value):
        def fn():
            time.sleep(0.20)
            return value
        return fn

    started = time.monotonic()
    assert _concurrent_reads(slow("a"), slow("b"), slow("c")) == ["a", "b", "c"]
    elapsed = time.monotonic() - started
    assert elapsed < 0.45, f"reads did not overlap (took {elapsed:.2f}s of a 0.60s serial sum)"


def test_concurrent_reads_reraises_exactly_like_inline_evaluation():
    """A failing balance read must still refuse at the gate, never become a zero balance."""
    from sniper.services.memecoin_real_runner import _concurrent_reads

    def boom():
        raise RuntimeError("rpc down")

    try:
        _concurrent_reads(lambda: 1.0, boom)
    except RuntimeError as exc:
        assert str(exc) == "rpc down"
    else:
        raise AssertionError("exception was swallowed — the gate would size on a partial read")


def test_concurrent_reads_passes_none_through():
    """native_fn returns None on a failed read; the gate refuses with an honest reason. None must
    survive, not become 0.0."""
    from sniper.services.memecoin_real_runner import _concurrent_reads
    assert _concurrent_reads(lambda: None, lambda: 3.0) == [None, 3.0]


@pytest.mark.parametrize("handover,retire_ready,watcher_ready,graph_ready,shyft,code,retained,phases", [
    (True, True, True, True, True, 0, False, ["retire", "watcher", "graph"]),
    (True, False, True, True, True, 1, True, ["retire"]),
    (True, True, False, True, True, 1, True, ["retire", "watcher"]),
    (True, True, True, False, True, 1, False, ["retire", "watcher", "graph"]),
    (False, True, True, True, True, 0, False, ["graph"]),
    (False, True, True, False, True, 1, False, ["graph"]),
    (True, True, True, True, 2, 0, False, ["retire", "watcher", "graph"]),
    (True, False, False, False, False, 0, True, []),
])
def test_deploy_advances_watcher_handover_under_held_watchdog_lock(
        tmp_path, handover, retire_ready, watcher_ready, graph_ready, shyft, code, retained, phases):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    state = tmp_path / "state"
    release = state / ".runtime/memecoin/releases/newsha"
    (release / "scripts").mkdir(parents=True)
    (state / "data").mkdir(parents=True)
    record = state / "data/onchain_watch.handover.json"
    if handover:
        record.write_text(json.dumps({"version": 1,
            "candidate": [4242, str(release / "scripts/research/onchain_wallet_watch.py"), "77"],
            "old": [[3131, str(state / ".runtime/memecoin/releases/oldsha/scripts/research/onchain_wallet_watch.py"), "66"]]}))
        (state / "old_alive").touch()
    (state / "ready.json").write_text(json.dumps(
        dict(retire=retire_ready, watcher=watcher_ready, graph=graph_ready)))
    verifier = release / "scripts/memecoin_runtime_verify.py"
    verifier.write_text('''import fcntl, json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(args[args.index('--state-root') + 1])
ready = json.loads((state / 'ready.json').read_text())
fd = os.open(state / 'data/watchdog.lock', os.O_RDWR)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    pass
else:
    raise SystemExit(88)
finally:
    os.close(fd)
if '--retire-watcher-record' in args:
    phase = 'retire'
elif '--watcher-only' in args:
    phase = 'watcher'
else:
    phase = 'graph'
with (state / 'calls.jsonl').open('a') as out:
    out.write(json.dumps(phase) + '\\n')
if not ready[phase]:
    raise SystemExit(1)
if phase == 'retire':
    assert Path(args[args.index('--retire-watcher-record') + 1]) == state / 'data/onchain_watch.handover.json'
    (state / 'old_alive').unlink(missing_ok=True)
elif phase == 'watcher':
    assert args[args.index('--watcher-candidate-pid') + 1] == '4242'
    assert args[args.index('--watcher-candidate-birth') + 1] == '77'
    assert '--allow-watcher-overlap' not in args and '--watcher-primary-only' not in args
    assert not (state / 'old_alive').exists()
    (state / 'full_watcher_proof').touch()
else:
    assert '--shyft-runtime-only' in args
    assert not any(arg.startswith('--allow-') for arg in args)
    assert not (state / 'old_alive').exists()
    assert not (state / 'data/onchain_watch.handover.json').exists()
''')
    source = (Path(__file__).resolve().parents[1] / "scripts/deploy_memecoin_runtime.sh").read_text()
    start = source.index("provider_runtime_ready() {")
    function = source[start:source.index("\n}\n", start) + 3]
    gate = source.index("shyft_runtime_gate() {")          # its strict gate (no baseline here)
    function = source[gate:source.index("\n}\n", gate) + 3] + function
    result = subprocess.run(["bash"], input=function + '''
exec 8>"$STATE_ROOT/data/watchdog.lock"
flock -n 8 || exit 89
provider_runtime_ready
''', text=True, capture_output=True, timeout=5, env=dict(os.environ,
        PY=sys.executable, RELEASE=str(release), STATE_ROOT=str(state), SHYFT_RUNTIME=str(int(shyft))))
    assert result.returncode == code, result.stderr
    assert record.exists() is retained
    calls = state / "calls.jsonl"
    observed = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    assert observed == phases
    if handover and shyft and retire_ready:
        assert not (state / "old_alive").exists()
    if handover and watcher_ready and retire_ready and shyft:
        assert (state / "full_watcher_proof").exists()
