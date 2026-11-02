import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_risk import build_memecoin_risk_context


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_context_folds_confirmed_and_pending_into_effective():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M1", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET execution_mode='real' WHERE id=?", (pid,)); c.commit()
    m.reserve_pending(c, mint="M2", source_user_id="u", strategy="control",
                      notional=10.0, reserved_at="t")
    ctx = build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=1.0, day="2026-07-09")
    assert ctx.effective_concurrent == 2                 # 1 confirmed real + 1 pending
    assert abs(ctx.effective_exposure_usd - 20.0) < 1e-9
    assert ctx.held_coin_ids == frozenset({("solana", "M1"), ("solana", "M2")})
    assert ctx.daily_tripped is False
    assert ctx.max_concurrent == 6 and ctx.trade_size_usd == 10.0


def test_manual_reservation_does_not_consume_an_automatic_slot():
    c = _c()
    m.reserve_pending(
        c,
        mint="ManualMint",
        source_user_id="u",
        strategy="alpha_b",
        notional=20.0,
        reserved_at="2026-07-09T00:00:00Z",
        signal_source="manual",
    )

    ctx = build_memecoin_risk_context(
        c, usdc_balance=300.0, sol_balance=1.0, day="2026-07-09")

    assert ctx.effective_concurrent == 0
    assert ctx.effective_exposure_usd == 20.0


def test_funded_book_snapshot_counts_all_chains_and_only_unresolved_reservations():
    c = _c()
    for mint, network, notional in (("SOL", "solana", 10.0), ("0xB", "8453", 20.0),
                                     ("0xR", "4663", 30.0)):
        m.open_position(
            c, source_user_id=f"u-{mint}", source_handle="h", mint=mint, ticker="T",
            entry_price=1.0, entry_notional=notional, entry_fill_source="chain",
            entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-07-18T00:00:00Z",
            execution_mode="real", network_id=network,
        )
    active = m.reserve_pending(c, mint="P", source_user_id="u", strategy="alpha_b",
                               notional=40.0, reserved_at="2026-07-18T00:00:00Z",
                               network_id="solana")
    confirmed = m.reserve_pending(c, mint="0xPC", source_user_id="u", strategy="alpha_b",
                                  notional=50.0, reserved_at="2026-07-18T00:00:00Z",
                                  network_id="8453")
    failed = m.reserve_pending(c, mint="0xPF", source_user_id="u", strategy="alpha_b",
                               notional=60.0, reserved_at="2026-07-18T00:00:00Z",
                               network_id="4663")
    m.confirm_pending(c, confirmed)
    m.release_pending(c, failed, "test")

    # `mark_notional` is the SIZING view and these positions carry no executable mark, so each is
    # worth zero there — only the reservation, which is cash already committed, survives into it.
    # The COST view is what the exposure cap reads and it is unchanged.
    assert m.funded_book_snapshot(c) == {
        "count": 4,
        "notional": 100.0,
        "mark_notional": 40.0,
        "open_count": 3,
        "open_notional": 60.0,
        "open_mark_notional": 0.0,
        "pending_count": 1,
        "pending_notional": 40.0,
    }
    assert m.pending_row(c, active)["status"] == "reserved"


def test_funded_book_does_not_double_count_open_position_and_stale_reserved_attempt():
    c = _c()
    m.open_position(
        c, source_user_id="u", source_handle="h", mint="M", ticker="T",
        entry_price=1.0, entry_notional=75.0, entry_fill_source="chain",
        entry_lag_s=0.0, their_size_usd=0.0, opened_at="2026-07-18T00:00:00Z",
        execution_mode="real", network_id="solana", attempt_id="attempt-1",
    )
    m.reserve_pending(
        c, mint="M", source_user_id="u", strategy="alpha_b", notional=75.0,
        reserved_at="2026-07-18T00:00:00Z", network_id="solana",
        attempt_id="attempt-1",
    )

    assert m.funded_book_snapshot(c)["count"] == 1
    assert m.funded_book_snapshot(c)["notional"] == 75.0
    assert m.pending_totals(c) == (0, 0.0)


# ------------------------------ unknown balance is not zero (2026-08-26, live incident)

def _fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


def test_an_unreadable_usdc_balance_refuses_honestly_not_as_insufficient_bankroll():
    """REGRESSION: token_balance collapses every failure to 0.0, so a transient RPC fault during
    a restart read a funded wallet as $0 and the gate said 'insufficient bankroll' — the operator
    was refused a manual entry at 14:57Z with $108 in the wallet. Unknown is not zero."""
    from sniper.services.memecoin_risk import memecoin_entry_allowed
    c = _fresh()
    ctx = build_memecoin_risk_context(c, usdc_balance=None, sol_balance=1.0, day="2026-08-26")
    assert ctx.usdc_balance is None                      # preserved, not floated to a crash or 0.0
    from dataclasses import replace
    ctx = replace(ctx, real_enabled=True)
    d = memecoin_entry_allowed(ctx, mint="M", trade_size=ctx.trade_size_usd)
    assert d.allowed is False
    assert "USDC balance unavailable" in d.reason


def test_a_genuine_zero_balance_still_reads_insufficient_bankroll():
    from sniper.services.memecoin_risk import memecoin_entry_allowed
    c = _fresh()
    ctx = build_memecoin_risk_context(c, usdc_balance=0.0, sol_balance=1.0, day="2026-08-26")
    from dataclasses import replace
    ctx = replace(ctx, real_enabled=True)
    d = memecoin_entry_allowed(ctx, mint="M", trade_size=ctx.trade_size_usd)
    assert d.allowed is False
    assert "USDC balance unavailable" not in d.reason    # a real zero is not an unknown


def _closed_real_position(c, mint, closed_at, pnl=-1.0):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET execution_mode='real', is_open=0, closed_at=?, "
              "realized_pnl=? WHERE id=?", (closed_at, pnl, pid))
    c.commit()
    return pid


def test_reentry_policy_sets_are_caller_controlled():
    """A manual leg passes include_reentry_policy=False: both POLICY sets come back empty, so a
    deliberate operator re-buy is not refused for a reason that only means something while
    copying somebody. Live 2026-08-27 12:56Z: two manual re-buys refused by the cooldown."""
    from sniper.util.clock import real_utc_now_iso
    c = _c()
    _closed_real_position(c, "MJUSTCLOSED", real_utc_now_iso())
    ctx_default = build_memecoin_risk_context(
        c, usdc_balance=300.0, sol_balance=1.0, day="2026-08-27")
    assert ("solana", "MJUSTCLOSED") in ctx_default.cooldown_coin_ids
    ctx_manual = build_memecoin_risk_context(
        c, usdc_balance=300.0, sol_balance=1.0, day="2026-08-27",
        include_reentry_policy=False)
    assert ctx_manual.cooldown_coin_ids == frozenset()
    assert ctx_manual.losing_coin_ids == frozenset()
    # safety stays unconditional: the held-coin (one-per-coin) set is NOT emptied
    assert ctx_manual.held_coin_ids == ctx_default.held_coin_ids


def test_evm_reentry_policy_set_is_caller_controlled():
    from sniper.util.clock import real_utc_now_iso
    from sniper.services.memecoin_evm_risk import build_evm_risk_context
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="0xJUSTCLOSED",
                          ticker="T", entry_price=1.0, entry_notional=10.0,
                          entry_fill_source="chain", entry_lag_s=0.1, their_size_usd=0.0,
                          opened_at="t", execution_mode="real", network_id="8453")
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=-1.0 "
              "WHERE id=?", (real_utc_now_iso(), pid))
    c.commit()
    ctx_default = build_evm_risk_context(
        c, usdc_balance=300.0, native_balance=1.0, day="2026-08-27", network_id="8453")
    assert ("8453", "0xJUSTCLOSED") in ctx_default.cooldown_coin_ids
    ctx_manual = build_evm_risk_context(
        c, usdc_balance=300.0, native_balance=1.0, day="2026-08-27", network_id="8453",
        include_reentry_policy=False)
    assert ctx_manual.cooldown_coin_ids == frozenset()


def test_preflight_threads_the_reentry_policy_like_the_reservation(monkeypatch):
    """The 08-27 gap: preflight runs BEFORE reserve_entry in the Solana leg and returns on
    refusal, so fixing the reservation alone never ran. Preflight must honour the same
    include_reentry_policy the caller passes to the reservation."""
    from sniper.util.clock import real_utc_now_iso
    from sniper.services.memecoin_risk import preflight_entry
    import sniper.settings.memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    c = _c()
    _closed_real_position(c, "MJUSTCLOSED", real_utc_now_iso())
    default = preflight_entry(c, mint="MJUSTCLOSED", day="2026-08-27",
                              usdc_balance=300.0, sol_balance=1.0)
    assert not default.allowed and "re-entry cooldown" in default.reason
    manual = preflight_entry(c, mint="MJUSTCLOSED", day="2026-08-27",
                             usdc_balance=300.0, sol_balance=1.0,
                             include_reentry_policy=False)
    assert "re-entry cooldown" not in (manual.reason or "")


def test_the_solana_leg_passes_manual_reentry_policy_to_both_hops():
    """Source-level pin at the LEG altitude: every call the leg makes into the risk gate carries
    the manual expression, so a future hop added without it fails this count."""
    import pathlib, re
    src = pathlib.Path("sniper/services/memecoin_real_leg.py").read_text()
    # UPDATED 2026-08-28: the leg now resolves the policy ONCE into named switches rather than
    # repeating the manual expression at each hop, because a per-wallet exemption names ONE
    # re-entry family and the two must move independently. The count still binds: every gate call
    # must carry BOTH switches, so a hop added without them fails here.
    gate_calls = len(re.findall(r"preflight_entry\(|reserve_entry\(", src))
    cooldown = src.count("include_reentry_policy=_apply_cooldown")
    losing = src.count("include_losing_reentry=_apply_losing")
    assert gate_calls == cooldown == losing == 2, (
        f"{gate_calls} risk-gate calls in the leg but {cooldown} carry the cooldown switch "
        f"and {losing} the losing-re-entry switch")
    # And the switches must still be DERIVED from the manual signal plus the wallet's exemptions,
    # or the rename would have quietly dropped the manual bypass this test was written for.
    # 2026-10-21: the manual split now lives in memecoin_origin (the desk keeps capacity gates).
    assert '_enforce_concurrency = enforces_capacity(getattr(ev, "signal_source", None))' in src
    assert '"reentry_cooldown" not in _wallet_exempt' in src
    assert '"losing_reentry" not in _wallet_exempt' in src
