import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as r
from sniper.services import memecoin_evm_risk as er


def _c(monkeypatch):
    monkeypatch.setattr(r.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def test_approved_entry_reserves_and_blocks_second_same_coin(monkeypatch):
    c = _c(monkeypatch)
    dec, pid = r.reserve_entry(c, mint="M1", source_user_id="u1", strategy="control",
                               day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t")
    assert dec.allowed is True and pid is not None
    assert m.pending_totals(c) == (1, 10.0)
    # a second signal for the SAME coin is rejected (one-position-per-coin, via held_mints)
    dec2, pid2 = r.reserve_entry(c, mint="M1", source_user_id="u2", strategy="control",
                                 day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t")
    assert dec2.allowed is False and pid2 is None
    assert m.pending_totals(c) == (1, 10.0)


def test_rejected_entry_reserves_nothing(monkeypatch):
    c = _c(monkeypatch)
    dec, pid = r.reserve_entry(c, mint="M1", source_user_id="u1", strategy="control",
                               day="2026-07-09", usdc_balance=1.0, sol_balance=1.0, now_iso="t")
    assert dec.allowed is False and pid is None          # insufficient USDC
    assert m.pending_totals(c) == (0, 0.0)


def test_solana_capacity_check_and_reservation_share_immediate_transaction(monkeypatch):
    """Different mints do not hit the mint UNIQUE index. Capacity reads must run after
    BEGIN IMMEDIATE, or two callers can both consume the same final slot."""
    c = _c(monkeypatch)
    sql = []
    c.set_trace_callback(sql.append)

    dec, pid = r.reserve_entry(
        c, mint="M1", source_user_id="u1", strategy="control",
        day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t",
    )

    assert dec.allowed and pid is not None
    begin = next(i for i, statement in enumerate(sql) if statement == "BEGIN IMMEDIATE")
    first_capacity_read = next(
        i for i, statement in enumerate(sql)
        if "FROM memecoin_positions" in statement or "FROM memecoin_pending_entries" in statement
    )
    assert begin < first_capacity_read


def test_evm_capacity_check_and_reservation_share_immediate_transaction(monkeypatch):
    monkeypatch.setattr(er.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    sql = []
    c.set_trace_callback(sql.append)

    dec, pid = er.reserve_entry_evm(
        c, mint="0xM1", source_user_id="u1", strategy="alpha_b", network_id="4663",
        day="2026-07-09", usdc_balance=300.0, native_balance=1.0, now_iso="t",
    )

    assert dec.allowed and pid is not None
    begin = next(i for i, statement in enumerate(sql) if statement == "BEGIN IMMEDIATE")
    first_capacity_read = next(
        i for i, statement in enumerate(sql)
        if "FROM memecoin_positions" in statement or "FROM memecoin_pending_entries" in statement
    )
    assert begin < first_capacity_read


def _Classification(age, mint="M", network_id="solana"):
    """A REAL EntryClassification, not a stand-in. The contract writer reads a dozen of its fields,
    so a duck-typed fake passes the gate and then fails the contract, which would have let this
    test pass for the wrong reason."""
    from sniper.services.memecoin_market_state import EntryClassification
    return EntryClassification(
        classification_version=1, network_id=network_id, mint=mint, signal_source="follow_activity",
        decision_at="t", coin_band="micro", lifecycle="fresh", liquidity_class="deep",
        momentum_phase="rising", market_regime="normal", data_quality="ok", risk_scale=1.0,
        reason_codes=(), facts={"pair_age_s": age})


def test_a_coin_under_the_age_floor_reserves_nothing(monkeypatch):
    """The refusal must happen BEFORE reserve_pending, or a coin we refuse still holds a slot and
    the notional. Reservation 1955 held the whole 4663 bankroll for two hours on 2026-09-03, so a
    gate that refuses AFTER reserving would reintroduce exactly that failure."""
    c = _c(monkeypatch)
    monkeypatch.setattr(r.cfg, "min_coin_age_s_for_chain", lambda chain, env=None: 600.0)
    m.start_entry_attempt(c, attempt_id="att-young", decision_started_at="t",
                          network_id="solana", mint="M_YOUNG")
    dec, pid = r.reserve_entry(c, mint="M_YOUNG", source_user_id="u1", strategy="control", attempt_id="att-young",
                               day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t",
                               classification=_Classification(30.0, mint="M_YOUNG"))
    assert dec.allowed is False and pid is None
    assert "coin_too_young" in dec.reason
    assert m.pending_totals(c) == (0, 0.0), "a refused coin must not hold a reservation"


def test_an_old_enough_coin_still_reserves(monkeypatch):
    c = _c(monkeypatch)
    monkeypatch.setattr(r.cfg, "min_coin_age_s_for_chain", lambda chain, env=None: 600.0)
    m.start_entry_attempt(c, attempt_id="att-old", decision_started_at="t",
                          network_id="solana", mint="M_OLD")
    dec, pid = r.reserve_entry(c, mint="M_OLD", source_user_id="u1", strategy="control", attempt_id="att-old",
                               day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t",
                               classification=_Classification(3600.0, mint="M_OLD"))
    assert dec.allowed is True and pid is not None
    assert m.pending_totals(c) == (1, 10.0)


def test_the_floor_off_by_default_changes_nothing(monkeypatch):
    """Shipping this must be a no-op until the operator sets the knob."""
    c = _c(monkeypatch)
    m.start_entry_attempt(c, attempt_id="att-any", decision_started_at="t",
                          network_id="solana", mint="M_ANY")
    dec, pid = r.reserve_entry(c, mint="M_ANY", source_user_id="u1", strategy="control", attempt_id="att-any",
                               day="2026-07-09", usdc_balance=300.0, sol_balance=1.0, now_iso="t",
                               classification=_Classification(1.0, mint="M_ANY"))
    assert dec.allowed is True and pid is not None
