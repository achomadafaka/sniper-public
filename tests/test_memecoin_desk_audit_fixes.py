"""Codex's audit of desk step 1 (abd311423), five defects, one regression each.

1. The seat's rails are frozen into the funded contract BEFORE submission, so a fill booked by the
   unknown-outcome recovery carries exactly the same rails, with no second buy.
2. The desk cap is enforced inside the reservation statement (two connections cannot both pass).
3. The desk daily halt latches: a later winner, or a restart, cannot reopen the day.
4. One desk ticket = one executable intent: a replay returns the same intent, a conflicting
   payload is refused.
5. read_status keeps desk_rails_not_armed visible over a 'filled' entry projection.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as mi
from sniper.services import memecoin_origin as origin
import sniper.settings.memecoin as cfg

from test_memecoin_desk_origin import BOUNDS, GOOD, MINT, T0, _conn, _desk, _open_position, _metadata, _contract, _book_refusal


@pytest.fixture(autouse=True)
def _desk_knobs(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CHAINS", frozenset({"solana"}))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", 25.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 5)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_DAILY_LOSS_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS", cfg.parse_desk_rail_bounds(BOUNDS))


# --------------------------------------------------------------------- 1. rails in the contract

def _e2e(monkeypatch, outcome="filled"):
    return _e2e_on(monkeypatch, outcome=outcome)


def _e2e_on(monkeypatch, outcome="filled", pre=None):
    from sniper.services import memecoin_arming
    from sniper.services import memecoin_real_runner as runner
    from sniper.util.clock import real_utc_now_iso
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(runner, "_alert_entry_result", lambda *a, **k: None)
    clip = float(cfg.min_trade_floor_usd_for("solana"))
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_CLIP_USD", clip)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    seen = {"spent": [], "contract_at_submit": None, "pending_id": None}

    def execute(conn, pending_id, mint, size, now_iso, impact_cap_pct=None, **kw):
        row = dict(conn.execute("SELECT * FROM memecoin_pending_entries WHERE id=?",
                                (pending_id,)).fetchone())
        seen["spent"].append(size)
        seen["contract_at_submit"] = row["funded_contract_json"]
        seen["pending_id"] = pending_id
        if outcome != "filled":
            return {"status": "unknown", "reason": "confirmation unknown", "sig": "SIG"}
        pid = m.open_position(
            conn, source_user_id=None, source_handle="(real)", mint=mint, ticker="DSK",
            entry_price=0.0001, entry_notional=size, entry_fill_source="chain", entry_lag_s=0.0,
            their_size_usd=0.0, opened_at=now_iso, strategy=row["strategy"], execution_mode="real",
            network_id="solana", signal_source=row["signal_source"],
            funded_contract_version=row["funded_contract_version"],
            funded_contract_hash=row["funded_contract_hash"],
            funded_contract_json=row["funded_contract_json"])
        conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending_id,))
        return {"status": "filled", "position_id": pid}

    contexts = {"solana": {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
                           "token_safety_fn": lambda mint, **kw: (True, "ok"),
                           "execute_fn": execute, "kill_path": None}}
    stats = lambda mint, network_id=None: {"market_cap": 60_000.0, "price": 0.0001,
                                           "liquidity_usd": 20_000.0}
    iid = mi.enqueue(c, mint=MINT, network_id="solana", now_iso=real_utc_now_iso(), size_usd=clip,
                     rails=GOOD, rails_mode="custom", signal_source="desk", desk_ticket_id="r1", desk_metadata=_metadata(c))
    buy = runner.make_real_buy_fn(contexts, stats_fn=stats)
    if pre is not None:
        pre(c)
        c.commit()
    mi.drain(c, buy, now_fn=real_utc_now_iso)
    return c, iid, seen, buy


def _policy(contract_json):
    return json.loads(contract_json)["policy"]


def test_the_contract_carries_the_seat_rails_before_submission(monkeypatch):
    _, _, seen, _ = _e2e(monkeypatch)
    assert seen["contract_at_submit"], "the executor saw no frozen contract"
    policy = _policy(seen["contract_at_submit"])
    assert (policy["tp_pct"], policy["stop_pct"], policy["trail_pct"]) == (60.0, 25.0, 15.0)
    assert policy["derisk_pct"] == 0.0 and float(policy["catastrophe_pct"]) > 0
    desk = json.loads(seen["contract_at_submit"])["source"]["desk"]
    assert desk["ticket_id"] == "r1" and desk["rails"] == GOOD


def test_an_unknown_fill_recovered_later_has_the_same_rails_and_no_second_buy(monkeypatch):
    from sniper.services.memecoin_funded_policy import frozen_policy_from_position
    from sniper.util.clock import real_utc_now_iso
    c, iid, seen, buy = _e2e(monkeypatch, outcome="unknown")
    assert mi.read_status(c, iid)["operation_state"] == mi.UNRESOLVED
    mi.drain(c, buy, now_fn=real_utc_now_iso)            # a later pass never re-buys
    assert len(seen["spent"]) == 1
    # The recovery books the position from the reservation's own frozen bytes.
    row = dict(c.execute("SELECT * FROM memecoin_pending_entries WHERE id=?",
                         (seen["pending_id"],)).fetchone())
    pid = m.open_position(
        c, source_user_id=None, source_handle="(real)", mint=MINT, ticker="DSK", entry_price=0.0001,
        entry_notional=seen["spent"][0], entry_fill_source="chain", entry_lag_s=0.0,
        their_size_usd=0.0, opened_at=T0, strategy=row["strategy"], execution_mode="real",
        network_id="solana", signal_source=row["signal_source"],
        funded_contract_version=row["funded_contract_version"],
        funded_contract_hash=row["funded_contract_hash"],
        funded_contract_json=row["funded_contract_json"])
    pos = m.get_position(c, pid)
    policy = frozen_policy_from_position(pos)
    assert (policy.tp_pct, policy.stop_pct, policy.trail_pct) == (60.0, 25.0, 15.0)
    assert row["funded_contract_json"] == seen["contract_at_submit"]


# --------------------------------------------------------------------- 2. cap in the reservation

def test_the_desk_cap_holds_across_two_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    path = tmp_path / "ledger.db"
    a = sqlite3.connect(str(path), timeout=5)
    a.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(a)
    a.commit()
    b = sqlite3.connect(str(path), timeout=5)
    b.row_factory = sqlite3.Row
    kw = dict(source_user_id=None, strategy="control", notional=25.0, reserved_at=T0,
              network_id="solana", signal_source="desk", funded_contract_json=_contract(a))
    assert m.reserve_pending(a, mint="MintA", **kw) is not None
    assert m.reserve_pending(b, mint="MintB", **kw) is None        # the cap, inside the INSERT
    # Copy lanes are not counted against the desk cap.
    assert m.reserve_pending(b, mint="MintC", **dict(kw, signal_source="slot_lane",
                                                     source_user_id="w")) is not None


def test_an_open_desk_position_counts_against_the_cap(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    c = _conn()
    _open_position(c, pid=1, mint="Held")
    assert m.reserve_pending(c, mint="Other", source_user_id=None, strategy="control",
                             notional=25.0, reserved_at=T0, network_id="solana",
                             signal_source="desk", funded_contract_json=_contract(c)) is None


# --------------------------------------------------------------------- 3. latched daily halt

def _close(c, pid, pnl, at):
    c.execute("INSERT INTO memecoin_positions(id, mint, network_id, execution_mode, is_open, "
              "signal_source, realized_pnl, closed_at) VALUES (?, ?, 'solana', 'real', 0, 'desk', ?, ?)",
              (pid, f"m{pid}", pnl, at))
    c.commit()


def test_the_desk_halt_latches_through_a_later_winner_and_a_restart(tmp_path):
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    c.commit()
    _close(c, 1, -200.0, "2026-10-21T01:00:00+00:00")
    assert _book_refusal(c, now_iso=T0) == "desk_daily_loss"
    _close(c, 2, +500.0, "2026-10-21T02:00:00+00:00")
    assert _book_refusal(c, now_iso=T0) == "desk_daily_loss"
    c.close()
    again = sqlite3.connect(str(path))                       # a runner restart
    again.row_factory = sqlite3.Row
    assert _book_refusal(again, now_iso=T0) == "desk_daily_loss"
    assert not m.is_daily_loss_tripped(again, "2026-10-21"), "the book-wide latch is separate"
    assert _book_refusal(again, now_iso="2026-10-22T00:00:01+00:00") is None


# --------------------------------------------------------------------- 4. one ticket, one intent

def test_a_replayed_desk_ticket_returns_the_same_intent():
    c = _conn()
    first = _desk(c, ticket="dup")
    assert _desk(c, ticket="dup") == first
    assert c.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    {"rails": dict(GOOD, sl_pct=30.0)}, {"size_usd": 20.0}])
def test_a_conflicting_payload_on_one_ticket_is_refused(change):
    c = _conn()
    _desk(c, ticket="dup")
    with pytest.raises(ValueError, match="desk ticket dup already"):
        _desk(c, ticket="dup", **change)
    assert c.execute("SELECT COUNT(*) FROM memecoin_manual_intents").fetchone()[0] == 1


def test_a_conflicting_mint_on_one_ticket_is_refused():
    c = _conn()
    _desk(c, ticket="dup")
    with pytest.raises(ValueError, match="desk ticket dup already"):
        mi.enqueue(c, mint="OtherMint", network_id="solana", now_iso=T0, size_usd=25.0,
                   rails=GOOD, rails_mode="custom", signal_source="desk", desk_ticket_id="dup", desk_metadata=_metadata(c))


# --------------------------------------------------------------------- 5. read_status

def test_read_status_shows_an_unarmed_desk_fill_as_refused(monkeypatch):
    c = _conn()
    iid = _desk(c)
    _open_position(c)

    def boom(*a, **k):
        raise RuntimeError("override write failed")
    monkeypatch.setattr(mi, "apply_rails", boom)
    mi.drain(c, lambda *a, **k: {"status": "filled", "position_id": 4242}, now_fn=lambda: T0)
    monkeypatch.setattr(mi, "_project_intent", lambda conn, **kw: {
        "status": "filled", "label": "Filled", "reason": "", "position_id": 4242})
    status = mi.read_status(c, iid)
    assert status["state"] == mi.REFUSED and status["status"] == "desk_rails_not_armed"
    assert status["reason"].startswith("desk_rails_not_armed") and status["position_id"] == 4242


# --------------------------------------------------------------------- 3b. halt inside the reservation

@pytest.mark.parametrize("trip", ["close", "latch"])
def test_a_desk_day_tripped_after_prework_refuses_the_reservation(tmp_path, trip):
    """Codex review: a worker that passed desk_book_refusal must still be refused if another
    connection trips the desk day before its reservation lands."""
    path = tmp_path / "ledger.db"
    a = sqlite3.connect(str(path), timeout=5)
    a.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(a)
    a.commit()
    assert _book_refusal(a, now_iso=T0) is None          # prework passed
    b = sqlite3.connect(str(path), timeout=5)
    b.row_factory = sqlite3.Row
    if trip == "close":
        _close(b, 9, -200.0, "2026-10-21T03:00:00+00:00")
    else:
        m.latch_desk_loss(b, "2026-10-21", realized_usd=-200.0, limit_usd=200.0, tripped_at=T0)
    assert m.reserve_pending(a, mint="M", source_user_id=None, strategy="control", notional=25.0,
                             reserved_at=T0, network_id="solana", signal_source="desk", funded_contract_json=_contract(a)) is None
    assert a.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert m.desk_loss_latched(a, "2026-10-21"), "the refusal latches the day"
    # The copy lane is not touched by the desk halt.
    assert m.reserve_pending(a, mint="C", source_user_id="w", strategy="control", notional=25.0,
                             reserved_at=T0, network_id="solana", signal_source="slot_lane", funded_contract_json=_contract(a)) is not None


def test_the_latch_can_join_a_caller_transaction():
    c = _conn()
    c.execute("BEGIN")
    m.latch_desk_loss(c, "2026-10-21", realized_usd=-201.0, limit_usd=200.0, tripped_at=T0,
                      commit=False)
    assert c.in_transaction
    c.rollback()
    assert not m.desk_loss_latched(c, "2026-10-21")


def test_a_loss_then_a_winner_with_no_entry_check_between_stays_halted(tmp_path):
    """Codex review: the halt is the day's LOW of running desk PnL, so no entry check (and no
    writer-side latch) is needed between a breaching loss and a later winner."""
    path = tmp_path / "ledger.db"
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    for pid in (1, 2):
        _open_position(c, pid=pid, mint=f"m{pid}")
    m.close_position(c, 1, "2026-10-21T01:00:00+00:00", -200.0, "real_stop")
    m.close_position(c, 2, "2026-10-21T02:00:00+00:00", +500.0, "real_tp")
    c.close()
    again = sqlite3.connect(str(path))                       # restart
    again.row_factory = sqlite3.Row
    assert _book_refusal(again, now_iso=T0) == "desk_daily_loss"
    assert m.desk_loss_latched(again, "2026-10-21"), "the check latches what it found"


def _desk_pos(c, pid, *, mint, entry=400.0):
    c.execute("INSERT INTO memecoin_positions(id, source_user_id, mint, network_id, execution_mode, "
              "is_open, signal_source, entry_notional, opened_at, strategy) "
              "VALUES (?, NULL, ?, 'solana', 'real', 1, 'desk', ?, ?, 'alpha_b')",
              (pid, mint, entry, T0))
    c.commit()


def _unpriced_full_exit(c, pid, *, sig, closed_at, qty=1000.0):
    """A confirmed full exit booked with no proceeds yet (the recovery's unpriced path)."""
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, close_sig=?, realized_pnl=NULL "
              "WHERE id=?", (closed_at, sig, pid))
    c.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
              "fraction, created_at, tx_sig) VALUES (?, 'bracket_exit', NULL, ?, NULL, "
              "'chain_confirmed_unpriced', 1.0, ?, ?)", (pid, qty, closed_at, sig))
    c.commit()


def test_an_earlier_unpriced_winner_settling_after_a_breaching_loss_stays_halted():
    """Codex's case through the real writers: the winner closed FIRST but unpriced, the loser closed
    later and breached, then the winner's receipt settles it. The day stays halted."""
    c = _conn()
    _desk_pos(c, 1, mint="win")
    _desk_pos(c, 2, mint="loss")
    _unpriced_full_exit(c, 1, sig="W", closed_at="2026-10-21T01:00:00+00:00")
    m.close_position(c, 2, "2026-10-21T02:00:00+00:00", -250.0, "real_stop")
    assert m.desk_loss_latched(c, "2026-10-21")
    assert m.reconcile_unpriced_full_exit(c, position_id=1, sig="W", price=0.9, qty=1000.0,
                                          notional=900.0, fill_source="chain")
    assert _book_refusal(c, now_iso=T0) == "desk_daily_loss"


def test_receipt_reconciliation_of_a_full_exit_latches_the_breach_it_settles():
    c = _conn()
    _desk_pos(c, 1, mint="loss")
    _desk_pos(c, 2, mint="win")
    _unpriced_full_exit(c, 1, sig="L", closed_at="2026-10-21T01:00:00+00:00")
    m.close_position(c, 2, "2026-10-21T02:00:00+00:00", +100.0, "real_tp")
    assert not m.desk_loss_latched(c, "2026-10-21")
    assert m.reconcile_unpriced_full_exit(c, position_id=1, sig="L", price=0.05, qty=1000.0,
                                          notional=50.0, fill_source="chain")    # -350
    assert m.desk_loss_latched(c, "2026-10-21"), "the reconciler latched in its own transaction"
    _desk_pos(c, 3, mint="win2")
    m.close_position(c, 3, "2026-10-21T03:00:00+00:00", +500.0, "real_tp")
    assert _book_refusal(c, now_iso=T0) == "desk_daily_loss"


def test_closed_derisk_reconciliation_latches_the_breach_it_settles():
    c = _conn()
    _desk_pos(c, 1, mint="loss")
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at='2026-10-21T01:00:00+00:00', "
              "realized_pnl=NULL WHERE id=1")
    cur = c.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
                    "fraction, created_at, tx_sig) VALUES (1, 'derisk', NULL, 500.0, NULL, "
                    "'chain_confirmed_unpriced', 0.5, ?, 'D')", ("2026-10-21T00:30:00+00:00",))
    fill_id = cur.lastrowid
    c.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, fill_source, "
              "fraction, created_at, tx_sig) VALUES (1, 'bracket_exit', 0.08, 500.0, 40.0, 'chain', "
              "1.0, '2026-10-21T01:00:00+00:00', 'F')")
    c.commit()
    assert m.reconcile_unpriced_derisk(c, fill_id=fill_id, position_id=1, sig="D", notional=10.0,
                                       fill_source="chain")                   # 40 + 10 - 400
    assert c.execute("SELECT realized_pnl FROM memecoin_positions WHERE id=1").fetchone()[0] == -350.0
    assert m.desk_loss_latched(c, "2026-10-21")


def test_orphan_finalization_latches_the_breach_it_writes():
    c = _conn()
    _desk_pos(c, 1, mint="orphan")
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=NULL WHERE id=1")   # claimed, never finalized
    c.commit()
    m.finalize_close(c, 1, "2026-10-21T01:00:00+00:00", -300.0, exit_reason="recovered_orphan_close")
    assert m.desk_loss_latched(c, "2026-10-21")


def test_a_winner_first_then_a_smaller_loss_never_halts():
    c = _conn()
    for pid in (1, 2):
        _open_position(c, pid=pid, mint=f"m{pid}")
    m.close_position(c, 1, "2026-10-21T01:00:00+00:00", +500.0, "real_tp")
    m.close_position(c, 2, "2026-10-21T02:00:00+00:00", -300.0, "real_stop")
    assert not m.desk_loss_latched(c, "2026-10-21")
    assert _book_refusal(c, now_iso=T0) is None


def test_a_copy_close_never_latches_the_desk_day():
    c = _conn()
    _open_position(c, pid=1, signal_source="onchain_watch", uid="w")
    m.close_position(c, 1, "2026-10-21T01:00:00+00:00", -999.0, "real_stop")
    assert _book_refusal(c, now_iso=T0) is None
    assert not m.desk_loss_latched(c, "2026-10-21")


# --------------------------------------------------------------------- refusal labels

@pytest.mark.parametrize("gate, expected", [
    ("cap", "desk max concurrent"), ("halt", "desk daily loss halt"), ("copy", "one position per coin")])
def test_a_refused_reservation_names_its_real_gate(monkeypatch, gate, expected):
    """Codex review: the atomic refusal was labelled one-position-per-coin whatever refused it."""
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    c = _conn()
    signal = "desk"
    if gate == "cap":
        _open_position(c, pid=1, mint="Held")
    elif gate == "halt":
        _close(c, 1, -200.0, "2026-10-21T01:00:00+00:00")
    else:
        signal = "slot_lane"
    assert m.lost_reservation_reason(c, network_id="solana", mint="New", signal_source=signal,
                                     at_iso=T0, funded_contract_json=_contract(c)).startswith(expected)


def test_both_reservation_gates_use_the_truthful_label():
    import inspect
    from sniper.services import memecoin_evm_risk, memecoin_risk
    for mod in (memecoin_risk, memecoin_evm_risk):
        src = inspect.getsource(mod)
        assert "m.lost_reservation_reason(" in src, mod.__name__
        assert '_reject("one position per coin: lost reservation race")' not in src


def test_a_desk_refused_mid_entry_reports_its_gate_not_the_coin(monkeypatch):
    """Prework passes (desk_book_refusal patched to see an empty book), another writer filled the
    desk cap: the entry result names the cap, through the real reserve_entry."""
    from sniper.services import memecoin_origin
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_MAX_CONCURRENT", 1)
    monkeypatch.setattr(memecoin_origin, "desk_book_refusal", lambda conn, now_iso, desk_metadata=None: None)
    c, iid, seen, _ = _e2e_on(monkeypatch, pre=lambda conn: _open_position(conn, pid=77, mint="Held"))
    assert seen["spent"] == []
    assert "desk max concurrent" in str(mi.read_status(c, iid)["operation_reason"])


# --------------------------------------------------------------------- validator before freeze

def test_desk_rails_the_exit_reader_would_refuse_are_never_frozen():
    """Gamma gap on fef1066d6: apply_desk_rails must run the exit sweep's own validator. A stop above
    100% is accepted by the arithmetic and refused by the reader."""
    from sniper.services.memecoin_funded_policy import apply_desk_rails, resolve_funded_policy
    base = resolve_funded_policy(coin_band="micro", network_id="solana")
    with pytest.raises(ValueError):
        apply_desk_rails(base, {"ticket_id": "t", "rails": {"sl_pct": 150.0}})



# --------------------------------------------------------------------- rails at the runner boundary

def _boundary_buy(monkeypatch):
    from sniper.services import memecoin_real_runner as runner
    calls = []
    contexts = {"solana": {"usdc_fn": lambda: calls.append("usdc") or 300.0,
                           "sol_fn": lambda: 1.0, "token_safety_fn": lambda *a, **k: (True, "ok"),
                           "execute_fn": lambda *a, **k: calls.append("execute") or {},
                           "kill_path": None}}
    stats = lambda *a, **k: calls.append("stats") or {}
    return runner.make_real_buy_fn(contexts, stats_fn=stats), calls


@pytest.mark.parametrize("rails", [{"sl_pct": 1.0}, {"tp_pct": 60.0}, True, {"sl_pct": True},
                                   {"sl_pct": 25.0, "derisk_pct": 30.0}])
def test_the_runner_refuses_desk_rails_outside_the_operator_bounds(monkeypatch, rails):
    buy, calls = _boundary_buy(monkeypatch)
    out = buy(_conn(), MINT, None, network_id="solana", size_usd=25.0, signal_source="desk",
              desk_ticket_id="t", desk_rails=rails)
    assert out["status"] == "rejected" and out["reason"].startswith("desk_rails_invalid")
    assert calls == [], "a provider or executor was touched before the rails were checked"


def test_rails_queued_before_the_bounds_tightened_are_refused_at_the_runner(monkeypatch):
    c = _conn()
    iid = _desk(c)                                           # sl_pct 25 inside 10-40
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_RAIL_BOUNDS",
                        cfg.parse_desk_rail_bounds("tp_pct:15-150,sl_pct:30-40,trail_pct:8-30"))
    buy, calls = _boundary_buy(monkeypatch)
    mi.drain(c, buy, now_fn=lambda: T0)
    status = mi.read_status(c, iid)
    assert status["operation_state"] == mi.REFUSED
    assert str(status["operation_reason"]).startswith("desk_rails_invalid") and calls == []


def test_the_reservation_itself_refuses_a_day_latched_at_close():
    """No entry check ran: the close writer latched the day, and the INSERT's own predicate sees it."""
    c = _conn()
    _open_position(c, pid=1, mint="loss")
    _open_position(c, pid=2, mint="win")
    m.close_position(c, 1, "2026-10-21T01:00:00+00:00", -250.0, "real_stop")
    m.close_position(c, 2, "2026-10-21T02:00:00+00:00", +500.0, "real_tp")
    assert m.desk_loss_latched(c, "2026-10-21")
    assert m.reserve_pending(c, mint="M", source_user_id=None, strategy="control", notional=25.0,
                             reserved_at=T0, network_id="solana", signal_source="desk", funded_contract_json=_contract(c)) is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_the_refusal_label_is_read_before_the_reservation_commits():
    """Codex review: read after the commit, another connection can free the cap first and the label
    falls back to the coin race. Both gates compute it inside the reservation transaction."""
    import inspect
    from sniper.services import memecoin_evm_risk, memecoin_risk
    for mod in (memecoin_risk, memecoin_evm_risk):
        src = inspect.getsource(mod)
        read = src.index("lost = (m.lost_reservation_reason(")
        assert read < src.index("conn.commit()", read), mod.__name__
        assert src.index("return _reject(lost), None") > src.index("conn.commit()", read)
