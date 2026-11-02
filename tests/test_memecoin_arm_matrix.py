import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pos(c, uid, handle, strategy, realized, event, is_open=0, mode="paper"):
    c.execute(
        "INSERT INTO memecoin_positions(source_user_id, source_handle, mint, strategy, "
        "execution_mode, is_open, realized_pnl, network_id, origin_event_id, closed_at, "
        "entry_price, entry_notional, entry_fill_source) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, handle, "MINT" + event, strategy, mode, is_open, realized, "solana", event,
         "2026-07-28T12:00:00+00:00", 0.001, 100.0, "route_quote"))
    # A position ALWAYS has an entry fill in production. The matrix now requires positive entry
    # quantity because the report calls a zero/absent entry fill `invalid_missing_entry_fill`
    # (2026-08-04) — a fixture without one is not a shape that can occur.
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-07-28T10:00:00+00:00")
    if not is_open:
        m.insert_fill(c, position_id=pid, kind="bracket_exit", price=0.0011, qty=100_000.0,
                      notional=110.0, fill_source="dexscreener", fraction=1.0,
                      created_at="2026-07-28T12:00:00+00:00")
    return pid


def test_arm_matrix_groups_by_wallet_and_arm():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    m.add_wallet(c, "u2", "bob")
    _pos(c, "u1", "alice", "control", 10.0, "e1")
    _pos(c, "u1", "alice", "control", -4.0, "e2")
    _pos(c, "u1", "alice", "scalp_c2", -20.0, "e1")
    _pos(c, "u1", "alice", "scalp_c2", 5.0, "e2")
    _pos(c, "u2", "bob", "control", 2.0, "e1")
    _pos(c, "u2", "bob", "real_shadow", 3.0, "e1")
    _pos(c, "u2", "bob", "control", 3.0, "open", is_open=1)  # open -> excluded
    _pos(c, "u1", "alice", "control", 99.0, "real", mode="real")  # real -> excluded
    c.commit()
    r = m.trader_arm_matrix(c)
    assert "control" in r["arms"] and "scalp_c2" in r["arms"]
    w = {x["source_user_id"]: x for x in r["wallets"]}
    assert round(w["u1"]["by_arm"]["control"]["pnl"], 2) == 6.0      # 10 - 4
    assert w["u1"]["by_arm"]["control"]["closed"] == 2
    assert w["u1"]["by_arm"]["control"]["wins"] == 1
    assert round(w["u1"]["by_arm"]["scalp_c2"]["pnl"], 2) == -15.0
    assert round(w["u1"]["total_pnl"], 2) == -9.0                    # 6 - 15
    assert w["u2"]["by_arm"]["control"]["closed"] == 1              # open + real excluded
    assert r["exact_paired"] is True and r["honest_only"] is True
    # sorted by total_pnl desc -> u2 (+5) before u1 (-9)
    assert [x["source_user_id"] for x in r["wallets"]] == ["u2", "u1"]


def test_arm_matrix_never_writes():
    c = _conn()
    m.add_wallet(c, "u1", "alice")
    _pos(c, "u1", "alice", "control", 5.0, "e1")
    _pos(c, "u1", "alice", "real_shadow", 4.0, "e1")
    c.commit()
    before = c.total_changes
    m.trader_arm_matrix(c)
    assert c.total_changes == before


def test_partial_executable_exit_then_noquote_final_is_not_comparable_evidence():
    """REGRESSION (2026-08-04 review). A position can sell part of the bag at a real quote and
    then close the REMAINDER on a noquote leg. `observed_exit_sql` accepted it because an
    executable exit leg existed; the report censors the whole outcome because the reason ends
    `_noquote`. Live robin: ALL 16 matrix-input rows were exactly this shape, so the trader table
    showed +$217.88 of "evidence" the report had already censored — and it reached funded arm
    selection through select_copy_arm.

    The matrix must use the REPORT's measurable-outcome semantics, not a re-derivation of them.
    """
    import sqlite3

    from sniper.persistence import db_memecoin as m

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "w1", "alice")
    pid = m.open_position(
        c, source_user_id="w1", source_handle="alice", mint="MINT", ticker="T",
        entry_price=0.001, entry_notional=100.0, their_size_usd=10.0,
        opened_at="2026-08-04T00:00:00+00:00", strategy="control",
        network_id="4663", origin_event_id="ev1",
        entry_fill_source="route_quote", entry_lag_s=0.5,
    )
    m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=100_000.0,
                  notional=100.0, fill_source="route_quote", fraction=1.0,
                  created_at="2026-08-04T00:00:00+00:00")
    # Executable partial: real quote, real proceeds.
    m.insert_fill(c, position_id=pid, kind="partial_exit", price=0.0014, qty=70_000.0,
                  notional=98.0, fill_source="dexscreener", fraction=0.7,
                  created_at="2026-08-04T00:05:00+00:00")
    # Final close of the REMAINDER at a price nothing could transact.
    m.insert_fill(c, position_id=pid, kind="bracket_exit", price=0.001, qty=30_000.0,
                  notional=30.0, fill_source="noquote", fraction=1.0,
                  created_at="2026-08-04T00:09:00+00:00")
    c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=?, "
              "exit_reason='real_trail_noquote' WHERE id=?",
              ("2026-08-04T00:09:00+00:00", 28.0, pid))
    c.commit()

    assert m.position_outcome_is_measurable(c, pid) is False
    rows = [r["id"] for r in c.execute(
        "SELECT id FROM memecoin_positions WHERE " + m.measurable_outcome_sql())]
    assert pid not in rows, "a noquote-final close is not comparable evidence"


def test_matrix_predicate_agrees_with_the_report_row_by_row():
    """ANTI-DRIFT. The matrix filters in SQL; the report decides row-wise in Python. They encode
    the same question, and the last two defects here were both a partial re-derivation of the
    report's rules. This asserts agreement over a ledger containing every censor shape, so a
    future edit to either side fails loudly instead of silently publishing censored evidence.
    """
    import sqlite3
    from datetime import datetime, timezone

    from sniper.persistence import db_memecoin as m
    import sniper.services.memecoin_paper_report as rep

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "w1", "alice")

    def position(reason, legs, *, last_quote=None, closed=True, entry_qty=100_000.0,
                 entry_source="route_quote", entry_fill_source=None):
        pid = m.open_position(
            c, source_user_id="w1", source_handle="alice", mint=f"M{reason}{len(legs)}{entry_qty}{entry_source}{last_quote}",
            ticker="T", entry_price=0.001, entry_notional=100.0, their_size_usd=10.0,
            opened_at="2026-08-04T00:00:00+00:00", strategy="control", network_id="solana",
            origin_event_id=f"ev-{reason}-{len(legs)}-{entry_qty}-{entry_source}-{last_quote}",
            entry_fill_source=entry_source, entry_lag_s=0.5)
        m.insert_fill(c, position_id=pid, kind="entry", price=0.001, qty=entry_qty,
                      notional=100.0,
                      # The FILL source is independent of the POSITION column — the whole point of
                      # the position-only-fomo shape below.
                      fill_source=entry_fill_source or entry_source, fraction=1.0,
                      created_at="2026-08-04T00:00:00+00:00")
        for kind, source, qty in legs:
            m.insert_fill(c, position_id=pid, kind=kind, price=0.0011, qty=qty,
                          notional=qty * 0.0011, fill_source=source, fraction=1.0,
                          created_at="2026-08-04T00:05:00+00:00")
        if closed:
            c.execute("UPDATE memecoin_positions SET is_open=0, closed_at=?, realized_pnl=?, "
                      "exit_reason=?, last_quote_price=? WHERE id=?",
                      ("2026-08-04T00:09:00+00:00", 10.0, reason, last_quote, pid))
        c.commit()
        return pid

    shapes = {
        "clean": position("bracket_exit", [("bracket_exit", "dexscreener", 100_000.0)]),
        "all_noquote": position("a_rollover_noquote", [("bracket_exit", "noquote", 100_000.0)]),
        "partial_then_noquote": position(
            "real_trail_noquote",
            [("partial_exit", "dexscreener", 70_000.0), ("bracket_exit", "noquote", 30_000.0)]),
        "cleanup_only": position("unquotable", [("bracket_exit", "cleanup", 100_000.0)]),
        "fomo_fill": position("mirror", [("mirror_exit", "fomo", 100_000.0)]),
        # --- shapes the first version of this test FALSELY claimed to cover (2026-08-04 review).
        # Each one diverged: SQL said measurable, the report said None.
        # `unquotable` with a stored ZERO mark — Python's `not last_quote_price` is falsy for 0,
        # `IS NULL` was not.
        "unquotable_zero_mark": position(
            "unquotable", [("bracket_exit", "dexscreener", 100_000.0)], last_quote=0.0),
        # A zero-quantity entry fill: `missing_entry_fill = entered <= 0`, so EXISTS() was wrong.
        "zero_qty_entry": position(
            "bracket_exit", [("bracket_exit", "dexscreener", 100_000.0)], entry_qty=0.0),
        # Fomo provenance on the POSITION ONLY — a route_quote entry FILL, so `report_fomo_fills`
        # is 0 and only `entry_fill_source` reveals it. The first version of this test passed
        # entry_source to both, so it never exercised the case it was named for.
        "fomo_entry_source": position(
            "bracket_exit", [("bracket_exit", "dexscreener", 100_000.0)],
            entry_source="fomo", entry_fill_source="route_quote"),
        # Closed with NO exit legs at all. A stored realized_pnl with no fill to back it has no
        # provenance, and this is comparable-evidence gating for FUNDED rails — so it is now
        # rejected rather than fail-open (2026-08-04 review).
        "no_exit_legs": position("bracket_exit", []),
    }

    sql_ok = {r["id"] for r in c.execute(
        "SELECT id FROM memecoin_positions WHERE " + m.measurable_outcome_sql())}
    now = datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc)
    report_ok = set()
    for row in rep._project_rows(c, "solana"):
        row = rep._add_outcome(row, now)
        # The report's match layer rejects on EITHER fomo signal; checking only
        # `report_fomo_fills` let the position-only shape look measurable on the report side and
        # hid the divergence this test exists to catch.
        if (not row["is_open"] and row["report_pnl"] is not None
                and not row.get("report_fomo_fills")
                and row.get("entry_fill_source") != "fomo"):
            report_ok.add(row["id"])

    assert sql_ok == report_ok, (
        f"matrix SQL and report disagree: sql_only={sql_ok - report_ok} "
        f"report_only={report_ok - sql_ok}")
    assert shapes["clean"] in sql_ok
    for name in ("all_noquote", "partial_then_noquote", "cleanup_only", "fomo_fill",
                 "unquotable_zero_mark", "zero_qty_entry", "fomo_entry_source",
                 "no_exit_legs"):
        assert shapes[name] not in sql_ok, name
