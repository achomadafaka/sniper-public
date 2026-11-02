import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_wallet_crud_and_curated_set():
    c = _conn()
    m.add_wallet(c, "u1", "alice", "sharp")
    m.add_wallet(c, "u2", "bob")
    assert {w["source_user_id"] for w in m.list_wallets(c)} == {"u1", "u2"}
    assert m.curated_user_ids(c) == {"u1", "u2"}
    m.set_wallet_paused(c, "u2", True)
    assert m.curated_user_ids(c) == {"u1"}          # paused excluded
    assert len(m.list_wallets(c, include_paused=False)) == 1
    m.remove_wallet(c, "u1")
    assert m.curated_user_ids(c) == set()


def test_event_dedup():
    c = _conn()
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "a",
          "mint": "M", "ticker": "T", "side": "buy", "their_price": 1.0,
          "their_market_cap": 100.0, "their_size_usd": 50.0, "is_first_buy": 1,
          "created_at": "2026-07-05T00:00:00Z", "received_at": "2026-07-05T00:00:00Z",
          "lag_s": 0.3}
    assert m.insert_event(c, ev) is True
    assert m.insert_event(c, ev) is False           # duplicate id ignored


def test_event_fills_only_a_blank_roster_handle():
    c = _conn()
    m.add_wallet(c, "u1", "", source="follow")
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "koy",
          "mint": "M", "ticker": "T", "side": "buy", "their_price": 1.0,
          "their_market_cap": 100.0, "their_size_usd": 50.0, "is_first_buy": 1,
          "created_at": "2026-08-31T00:00:00Z", "received_at": "2026-08-31T00:00:01Z",
          "lag_s": 1.0}

    m.insert_event(c, ev)
    assert m.list_wallets(c)[0]["handle"] == "koy"

    m.insert_event(c, dict(ev, fomo_event_id="e2", source_handle="new-name"))
    assert m.list_wallets(c)[0]["handle"] == "koy"


def test_evm_slot_proof_is_scoped_to_the_approved_spender():
    c = _conn()
    proof = ("0x" + "11" * 32, "0x" + "22" * 32)
    m.store_evm_slot_proof(
        c, network_id="4663", mint="0xToken", spender="0xSpender", proof=proof)

    assert m.load_evm_slot_proof(
        c, network_id="4663", mint="0xtoken", spender="0xspender") == proof
    assert m.load_evm_slot_proof(
        c, network_id="4663", mint="0xtoken", spender="0xother") is None


def test_position_lifecycle():
    c = _conn()
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="M",
                          ticker="T", entry_price=0.5, entry_notional=100.0,
                          entry_fill_source="jupiter", entry_lag_s=0.3,
                          their_size_usd=2000.0, opened_at="2026-07-05T00:00:00Z")
    assert m.get_open_position(c, "u1", "M")["id"] == pid
    m.insert_fill(c, position_id=pid, kind="entry", price=0.5, qty=200.0,
                  notional=100.0, fill_source="jupiter", fraction=1.0,
                  created_at="2026-07-05T00:00:00Z")
    m.close_position(c, pid, "2026-07-05T01:00:00Z", realized_pnl=25.0)
    assert m.get_open_position(c, "u1", "M") is None
    assert m.list_closed_positions(c)[0]["realized_pnl"] == 25.0


def test_persistent_old_schema_migrates_measurement_honesty_tables(tmp_path):
    path = tmp_path / "old_memecoin.sqlite"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE memecoin_wallets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT UNIQUE,
            handle TEXT,
            label TEXT,
            paused INTEGER DEFAULT 0,
            added_at TEXT
        );
        CREATE TABLE memecoin_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_event_id TEXT UNIQUE,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT, side TEXT,
            their_price REAL, their_market_cap REAL, their_size_usd REAL,
            is_first_buy INTEGER,
            created_at TEXT, received_at TEXT, lag_s REAL
        );
        CREATE TABLE memecoin_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_user_id TEXT, source_handle TEXT,
            mint TEXT, ticker TEXT,
            entry_price REAL, entry_notional REAL, entry_fill_source TEXT,
            entry_lag_s REAL, their_size_usd REAL,
            is_open INTEGER DEFAULT 1,
            opened_at TEXT, closed_at TEXT, realized_pnl REAL
        );
        CREATE TABLE memecoin_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position_id INTEGER,
            kind TEXT, price REAL, qty REAL, notional REAL,
            fill_source TEXT, fraction REAL, created_at TEXT
        );
    """)
    c.commit()
    c.close()

    migrated = sqlite3.connect(path)
    migrated.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(migrated)
    pos_cols = {r[1] for r in migrated.execute("PRAGMA table_info(memecoin_positions)")}
    assert {"exit_reason", "last_quote_price", "last_quote_ts", "origin_event_id",
            "hold_band", "hold_clock_normalized_at", "strategy"} <= pos_cols
    tables = {r[0] for r in migrated.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'memecoin_%'")}
    assert "memecoin_seen_sells" in tables
    assert "memecoin_alert_latency" in tables
    migrated.close()


def test_normalize_open_position_hold_stamps_shifts_legacy_host_rows_once():
    c = _conn()
    ev = {"fomo_event_id": "e1", "source_user_id": "u1", "source_handle": "a",
          "mint": "M1", "ticker": "T", "side": "buy", "their_price": 1.0,
          "their_market_cap": 100.0, "their_size_usd": 1000.0, "is_first_buy": 1,
          "created_at": "2026-07-08T08:00:00+00:00",
          "received_at": "2026-07-08T15:00:03+00:00", "lag_s": 3.0}
    m.insert_event(c, ev)
    legacy = m.open_position(c, source_user_id="u1", source_handle="a", mint="M1",
                             ticker="T", entry_price=1.0, entry_notional=100.0,
                             entry_fill_source="dexscreener", entry_lag_s=3.0,
                             their_size_usd=1000.0,
                             opened_at="2026-07-08T15:00:03+00:00",
                             strategy="alpha_b", origin_event_id="e1")
    c.execute("UPDATE memecoin_positions SET peak_ts=?, last_quote_ts=? WHERE id=?",
              ("2026-07-08T15:10:03+00:00", "2026-07-08T15:11:03+00:00", legacy))

    ev2 = dict(ev, fomo_event_id="e2", mint="M2",
               created_at="2026-07-08T10:00:00+00:00",
               received_at="2026-07-08T17:00:03+00:00")
    m.insert_event(c, ev2)
    real = m.open_position(c, source_user_id="u1", source_handle="a", mint="M2",
                           ticker="T", entry_price=1.0, entry_notional=100.0,
                           entry_fill_source="dexscreener", entry_lag_s=3.0,
                           their_size_usd=1000.0,
                           opened_at="2026-07-08T10:00:03+00:00",
                           strategy="alpha_b", origin_event_id="e2")
    no_origin_copy = m.open_position(
        c, source_user_id="u2", source_handle="b", mint="M3", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="dexscreener",
        entry_lag_s=4.0, their_size_usd=1000.0,
        opened_at="2026-07-08T15:00:04+00:00", strategy="alpha_b")
    manual = m.open_position(
        c, source_user_id="u3", source_handle="manual", mint="M4", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="manual",
        entry_lag_s=-1.0, their_size_usd=0.0,
        opened_at="2026-07-08T10:00:00+00:00", strategy="alpha_b")

    n = m.normalize_open_position_hold_stamps(
        c, offset_s=25200.0, now_iso="2026-07-08T22:00:00+00:00")
    assert n == 2
    p = m.get_position(c, legacy)
    assert p["opened_at"] == "2026-07-08T08:00:03+00:00"
    assert p["peak_ts"] == "2026-07-08T08:10:03+00:00"
    assert p["last_quote_ts"] == "2026-07-08T08:11:03+00:00"
    assert p["hold_clock_normalized_at"] == "2026-07-08T22:00:00+00:00"
    assert m.get_position(c, real)["opened_at"] == "2026-07-08T10:00:03+00:00"
    assert m.get_position(c, no_origin_copy)["opened_at"] == "2026-07-08T08:00:04+00:00"
    assert m.get_position(c, manual)["opened_at"] == "2026-07-08T10:00:00+00:00"

    assert m.normalize_open_position_hold_stamps(
        c, offset_s=25200.0, now_iso="2026-07-08T22:01:00+00:00") == 0
    assert m.get_position(c, legacy)["opened_at"] == "2026-07-08T08:00:03+00:00"


def _closed(c, *, mode="paper", handle="w", pnl=1.0, lag=5.0, closed_at="2026-07-29T00:00:00+00:00"):
    pid = m.open_position(
        c, source_user_id=handle, source_handle=handle, mint=f"M{closed_at}{pnl}", ticker="T",
        entry_price=1.0, entry_notional=100.0, entry_fill_source="dexscreener", entry_lag_s=lag,
        their_size_usd=1.0, opened_at="2026-07-28T00:00:00+00:00", strategy="control",
        execution_mode=mode,
    )
    m.close_position(c, pid, closed_at, realized_pnl=pnl, exit_reason="x")
    return pid


def test_closed_positions_page_is_bounded_but_totals_span_the_whole_ledger():
    """Regression: the endpoint used to materialise every closed row at 85 columns just to slice
    200 off the front (~980ms on the live ledger). The page shrank; the stats must not."""
    c = _conn()
    for i in range(5):
        _closed(c, handle="alice", pnl=10.0, closed_at=f"2026-07-29T00:00:0{i}+00:00")
    for i in range(3):
        _closed(c, handle="bob", pnl=-4.0, lag=1.0, closed_at=f"2026-07-28T00:00:0{i}+00:00")

    page = m.list_closed_positions(c, limit=2)
    assert len(page) == 2
    # closed_at DESC — the page is the newest rows, not an arbitrary two
    assert [r["closed_at"] for r in page] == [
        "2026-07-29T00:00:04+00:00", "2026-07-29T00:00:03+00:00"]
    assert m.list_closed_positions(c, limit=0) == []
    assert len(m.list_closed_positions(c)) == 8          # unbounded default preserved

    totals = m.closed_position_totals(c)
    assert totals["total_pnl"] == 5 * 10.0 + 3 * -4.0    # every row, not just the page
    assert totals["win_rate"] == 5 / 8
    assert totals["by_wallet"] == {"alice": 50.0, "bob": -12.0}
    assert totals["lag"]["min"] == 1.0 and totals["lag"]["median"] == 5.0


def test_closed_totals_are_mode_scoped_and_survive_an_empty_ledger():
    c = _conn()
    assert m.closed_position_totals(c) == {
        "win_rate": None, "total_pnl": 0, "by_wallet": {},
        "lag": {"median": None, "min": None},
    }
    _closed(c, mode="paper", handle="p", pnl=7.0)
    _closed(c, mode="real", handle="r", pnl=-99.0)
    assert m.closed_position_totals(c)["total_pnl"] == 7.0
    assert m.closed_position_totals(c, mode="real")["total_pnl"] == -99.0
    assert m.closed_position_totals(c, mode=None)["total_pnl"] == -92.0


def test_closed_totals_ignore_unknown_pnl_and_negative_lag_sentinels():
    """None PnL is unknown, not zero; entry_lag_s is -1 when the lag was never measured."""
    c = _conn()
    _closed(c, handle="a", pnl=6.0, lag=-1.0)
    pid = _closed(c, handle="a", pnl=0.0, lag=2.0, closed_at="2026-07-29T00:00:09+00:00")
    c.execute("UPDATE memecoin_positions SET realized_pnl=NULL WHERE id=?", (pid,))
    c.commit()
    totals = m.closed_position_totals(c)
    assert totals["win_rate"] == 1.0            # denominator is the ONE known outcome
    assert totals["total_pnl"] == 6.0
    assert totals["by_wallet"] == {"a": 6.0}    # unknown contributes 0 to the wallet sum
    assert totals["lag"]["min"] == 2.0 and totals["lag"]["median"] == 2.0


# --- position return after a partial de-risk -----------------------------------
# `last/entry - 1` measures the TOKEN. Once a de-risk lands, the position holds banked
# USDC plus a smaller bag and the two diverge permanently. Observed on 27035 (KIO,
# 2026-08-05): price read +53.1% against an actual +46.2%.

def _derisked_position(c, *, stake=50.0, entry_px=0.0008511655911466442,
                       entry_qty=58742.976126, sold_qty=40783.77368343506,
                       derisk_px=0.0012181987225027041, derisk_notional=49.68274099999999):
    c.execute("INSERT INTO memecoin_positions(mint,ticker,entry_price,entry_notional,is_open,"
              "opened_at) VALUES('mint-kio','KIO',?,?,1,'2026-08-05T14:30:31Z')",
              (entry_px, stake))
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute("INSERT INTO memecoin_fills(position_id,kind,price,qty,notional,fill_source,"
              "fraction,created_at) VALUES(?,'entry',?,?,?,'chain',1.0,'2026-08-05T14:30:31Z')",
              (pid, entry_px, entry_qty, stake))
    if sold_qty:
        c.execute("INSERT INTO memecoin_fills(position_id,kind,price,qty,notional,fill_source,"
                  "fraction,created_at) VALUES(?,'derisk',?,?,?,'chain',?,'2026-08-05T15:30:41Z')",
                  (pid, derisk_px, sold_qty, derisk_notional, sold_qty / entry_qty))
    return pid


def test_position_return_counts_recovered_stake_not_just_price():
    c = _conn()
    pid = _derisked_position(c)
    entry_px = 0.0008511655911466442
    pct = m.position_return_pct(c, pid, entry_notional=50.0, last_price=0.001303,
                                entry_price=entry_px)
    # $49.68 banked + 17,959.20 tokens * 0.001303 = $73.08 on a $50 stake.
    assert round(pct, 1) == 46.2
    # the token price alone says +53.1% — that is what the card used to show
    assert round((0.001303 / entry_px - 1) * 100, 1) == 53.1


def test_position_return_is_near_flat_when_a_derisked_coin_goes_to_zero():
    """The case the de-risk exists to create, and the one the price return got worst."""
    c = _conn()
    pid = _derisked_position(c)
    pct = m.position_return_pct(c, pid, entry_notional=50.0, last_price=1e-12,
                                entry_price=0.0008511655911466442)
    assert -1.0 < pct < 0.0            # ~ -0.6%, not -100%


def test_position_return_matches_price_return_without_a_derisk():
    c = _conn()
    pid = _derisked_position(c, sold_qty=0.0)
    entry_px = 0.0008511655911466442
    price_return = (0.001303 / entry_px - 1) * 100
    assert m.position_return_pct(c, pid, entry_notional=50.0, last_price=0.001303,
                                 entry_price=entry_px) == price_return


def test_position_return_falls_back_when_the_stake_is_unknown():
    c = _conn()
    pid = _derisked_position(c)
    entry_px = 0.0008511655911466442
    price_return = (0.001303 / entry_px - 1) * 100
    for missing in (None, 0.0):
        assert m.position_return_pct(c, pid, entry_notional=missing, last_price=0.001303,
                                     entry_price=entry_px) == price_return


def test_position_return_is_none_without_a_usable_price():
    c = _conn()
    pid = _derisked_position(c)
    assert m.position_return_pct(c, pid, entry_notional=50.0, last_price=None,
                                 entry_price=0.00085) is None
    assert m.position_return_pct(c, pid, entry_notional=50.0, last_price=0.001,
                                 entry_price=0) is None


def test_position_return_counts_the_final_exit_too_not_just_the_derisk():
    """A closed row's last leg is a bracket_exit. Summing only de-risk proceeds reported
    KIO 27035's true +36.8% as -0.6% — the recovered stake with the bag valued at nothing."""
    c = _conn()
    pid = _derisked_position(c)
    c.execute("INSERT INTO memecoin_fills(position_id,kind,price,qty,notional,fill_source,"
              "fraction,created_at) VALUES(?,'bracket_exit',0.00104343,17959.202442564943,"
              "18.74,'chain',1.0,'2026-08-05T15:57:44Z')", (pid,))
    c.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=?", (pid,))
    # bag fully sold -> open_qty 0, so the answer is the realized return regardless of price
    pct = m.position_return_pct(c, pid, entry_notional=50.0, last_price=0.00104343,
                                entry_price=0.0008511655911466442)
    assert round(pct, 1) == 36.8


# --- first-seen anchor window (2026-08-06) -------------------------------------
# Unbounded, the anchor drifted stale as history grew: the 104 signals this guard refused in one
# live window were anchored on an event a MEDIAN 26.7 hours old (max 23 days), so it was calling
# a day of appreciation a "chase" and refusing 64% of all eligible funded signals.

def _buy(c, mint, price, created_at, uid="u1"):
    c.execute("INSERT INTO memecoin_events(fomo_event_id,source_user_id,source_handle,mint,ticker,"
              "side,their_price,created_at,received_at) VALUES(?,?,'h',?,'T','buy',?,?,?)",
              (f"e-{mint}-{created_at}", uid, mint, price, created_at, created_at))
    c.commit()


def test_first_seen_anchor_ignores_events_outside_the_window():
    c = _conn()
    _buy(c, "M1", 0.001, "2026-08-05T00:00:00+00:00")      # 27h before -> stale
    _buy(c, "M1", 0.009, "2026-08-06T02:30:00+00:00")      # 30m before -> the real anchor
    now = "2026-08-06T03:00:00+00:00"
    assert m.first_seen_buy_price(c, "solana", "M1") == 0.001            # unbounded: stale anchor
    six_h = "2026-08-05T21:00:00+00:00"
    assert m.first_seen_buy_price(c, "solana", "M1", since_iso=six_h) == 0.009


def test_first_seen_anchor_is_none_when_every_event_is_stale():
    """No recent anchor means the move is not recent — nothing to chase, so the guard fails open
    exactly as it does for a never-seen coin."""
    c = _conn()
    _buy(c, "M2", 0.001, "2026-07-20T00:00:00+00:00")
    assert m.first_seen_buy_price(c, "solana", "M2",
                                  since_iso="2026-08-05T21:00:00+00:00") is None


def test_first_seen_anchor_unbounded_when_no_window_given():
    c = _conn()
    _buy(c, "M3", 0.002, "2026-07-01T00:00:00+00:00")
    assert m.first_seen_buy_price(c, "solana", "M3") == 0.002
    assert m.first_seen_buy_price(c, "solana", "M3", since_iso=None) == 0.002


def test_a_revival_poll_records_the_gap_beside_the_window_it_requested():
    """`covered_s` is the window REQUESTED and cannot fail (2026-09-23): the three attempts inside
    the V4 stall each stored 300.0 with zero swaps. `covered_gap_s` is how much of it the coin's own
    subscription had not delivered, so a later reader can tell a quiet tape from a blind one."""
    import sqlite3

    from sniper.persistence import db_memecoin as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.record_revival_watch_poll(
        conn, origin_event_id="e1", mint="0xm", network_id="4663", poll_index=1,
        polled_at="2026-09-23T16:20:03+00:00", usd=None, reason="protocol_lagging",
        coverage="blind_protocol_lagging", covered_s=300.0, covered_gap_s=162.4,
        measured_at=1790180403.0, allowed=True, decision_reason="onchain_blind_undecidable",
        source_age_s=None)
    row = conn.execute("SELECT covered_s, covered_gap_s FROM memecoin_revival_watch_polls").fetchone()
    assert row["covered_s"] == 300.0
    assert row["covered_gap_s"] == 162.4
    assert row["covered_s"] - row["covered_gap_s"] < row["covered_s"]
    conn.close()


def test_a_poll_recorded_without_a_gap_reads_unknown_not_zero():
    """Rows written before the column existed, and callers that pass nothing, must not claim the
    whole window was seen."""
    import sqlite3

    from sniper.persistence import db_memecoin as m

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.record_revival_watch_poll(
        conn, origin_event_id="e1", mint="0xm", network_id="4663", poll_index=1,
        polled_at="2026-09-23T16:20:03+00:00", usd=1.0, reason="ok", coverage="partial",
        covered_s=300.0, measured_at=1790180403.0, allowed=True, decision_reason="ok",
        source_age_s=None)
    assert conn.execute(
        "SELECT covered_gap_s FROM memecoin_revival_watch_polls").fetchone()["covered_gap_s"] is None
    conn.close()
