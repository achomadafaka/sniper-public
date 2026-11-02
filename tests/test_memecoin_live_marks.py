"""Held Solana coins' market price reaches the UI without waiting for the mark tape (S27-05).

Codex review 2026-09-27 (docs/audits/2026-09-27-solana-and-system-review.md S27-05): the stream
cache holds a new price in memory, but the UI reads the mark tape, and the tape is written after the
batch snapshot, the exact previews and the exit sweep, with a one-second write floor per coin. A
changed price at +400 ms was dropped. The fix publishes a display-only snapshot of the cache on
tmpfs from its own thread, and the web ticker prefers a fresh one. The tape, the exit decisions and
the rail wake do not change.
"""
import ast
import json
import os
import sqlite3
import threading
import time

import pytest

import sniper.routes.api_memecoin as A
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_live_marks as L
from sniper.services.memecoin_solana_swap_marks import (
    CURVE_SOURCE, SOURCE, STATE_SOURCE, PoolBinding, SolanaSwapMarkCache, WSOL)

MINT = "MintLive1111111111111111111111111111111111"
BINDING = PoolBinding(mint=MINT, pool="CurvePool", coin_vault="cv", quote_vault="qv", quote_mint=WSOL)
T0 = "2026-09-27T10:00:00.000000+00:00"
T1 = "2026-09-27T10:00:00.400000+00:00"


def _curve(cache, vsol, slot, observed_at, now_ts):
    assert cache.record({"kind": "sol_curve_state", "pool": BINDING.pool, "virtual_sol": vsol, "virtual_token": 1_000_000_000,
                         "block_no": slot, "complete": False}, BINDING, now_ts=now_ts, observed_at=observed_at)


def _publisher(cache, path, *, quote_usd=None, quote_at=T0, now_iso=T1, enabled=True):
    return L.LiveMarkPublisher(
        cache, path=str(path), enabled=enabled,
        quote_usd_fn=lambda: ({m: (v, quote_at) for m, v in quote_usd.items()} if quote_usd is not None
                              else {WSOL: (100.0, quote_at)}),
        now_iso_fn=lambda: now_iso, now_ts_fn=lambda: 10.4)


@pytest.fixture()
def conn(tmp_path):
    from sniper.settings import memecoin as cfg
    c = sqlite3.connect(str(tmp_path / "lm.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.open_position(c, source_user_id="u1", source_handle="h", mint=MINT, ticker="L",
                    entry_price=0.001, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=-1.0,
                    their_size_usd=0.0, opened_at="2026-09-27T09:59:00+00:00",
                    strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    # The tape holds only the first price: the +400 ms change is inside its one-second floor.
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
              "VALUES('solana', ?, '2026-09-27T10:00:00.000000Z', 0.1, ?, 'live')", (MINT, CURVE_SOURCE))
    c.commit()
    return c


def _ticker(conn, path, monkeypatch, now=T1):
    monkeypatch.setattr(A, "LIVE_MARKS_PATH", str(path))
    A._LIVE_MARKS_CACHE = (None, {})
    return A._real_ticker_payload(conn, now)["positions"][0]


# --- the witness ----------------------------------------------------------------------------------

def test_a_price_changed_400ms_later_reaches_the_ticker_without_a_tape_row(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 1_000_000_000, 100, T0, now_ts=10.0)            # 1 SOL / 1,000 tokens x $100 = 0.1
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)            # +400 ms: the price doubles
    assert _publisher(cache, path).cycle() is True
    row = _ticker(conn, path, monkeypatch)
    assert row["market_mid_price"] == pytest.approx(0.2)
    assert row["market_mid_source"] == CURVE_SOURCE
    assert row["market_mid_at"] == T1                             # changed: the chain update
    assert row["market_mid_fetched_at"] is not None               # checked: the publish, labelled apart
    assert row["market_mid_carried"] is False                     # a live change, not a held price


# Without a live mark the Solana market price is the route price or unknown, never the tape row the
# fixture holds (DR-04: the tape's on-chain rows carry DexScreener's quote USD).

def test_rollback_flag_off_publishes_nothing_and_the_market_price_is_unknown(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    assert _publisher(cache, path, enabled=False).cycle() is False
    assert not path.exists()
    assert _ticker(conn, path, monkeypatch)["market_mid_price"] is None


# --- what the snapshot refuses to show ------------------------------------------------------------

def test_a_stale_snapshot_shows_no_market_price(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    _publisher(cache, path).cycle()
    row = _ticker(conn, path, monkeypatch, now="2026-09-27T10:00:03.000000+00:00")   # published 2.6 s ago
    assert row["market_mid_price"] is None


@pytest.mark.parametrize("content", [None, "", "{not json", json.dumps({"schema": 99, "marks": {}})])
def test_a_missing_torn_or_foreign_snapshot_shows_no_market_price(conn, tmp_path, monkeypatch, content):
    path = tmp_path / "live.json"
    if content is not None:
        path.write_text(content)
    assert _ticker(conn, path, monkeypatch)["market_mid_price"] is None


def test_a_stale_usd_conversion_is_not_shown_as_a_live_price(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    _publisher(cache, path, quote_at="2026-09-27T09:58:00.000000+00:00").cycle()     # 2 min old
    snap = json.loads(path.read_text())
    assert snap["marks"]["solana:%s" % MINT]["healthy"] is False
    assert _ticker(conn, path, monkeypatch)["market_mid_price"] is None


def test_no_usd_for_the_quote_asset_means_no_mark_never_zero(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    _publisher(cache, path, quote_usd={}).cycle()
    assert json.loads(path.read_text())["marks"] == {}


def test_state_marks_never_enter_the_display_series(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    cache._state_marks[("solana", MINT)] = cache._curve_marks.get(("solana", MINT))  # none yet
    from sniper.services.memecoin_solana_swap_marks import QuoteMark
    cache._state_marks[("solana", MINT)] = QuoteMark(
        mint=MINT, pool="CurvePool", quote_mint=WSOL, quote_per_token=9.0, slot=105,
        observed_ts=10.4, observed_at=T1, source=STATE_SOURCE)
    _publisher(cache, path).cycle()
    assert json.loads(path.read_text())["marks"] == {}


def test_an_older_slot_arriving_late_does_not_replace_the_published_price(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    pub = _publisher(cache, path)
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    pub.cycle()
    _curve(cache, 1_000_000_000, 100, T0, now_ts=10.4)            # the older slot arrives last
    pub.cycle()
    mark = json.loads(path.read_text())["marks"]["solana:%s" % MINT]
    assert mark["slot"] == 101 and mark["price_usd"] == pytest.approx(0.2)


def test_an_invalidated_cache_withdraws_the_mark(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    pub = _publisher(cache, path)
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    pub.cycle()
    cache.invalidate()                                            # disconnect / epoch rollover
    pub.cycle()
    assert json.loads(path.read_text())["marks"] == {}
    _curve(cache, 1_000_000_000, 100, T0, now_ts=10.4)            # a fresh epoch may start lower
    pub.cycle()
    assert json.loads(path.read_text())["marks"]["solana:%s" % MINT]["slot"] == 100


def test_a_swap_mark_older_than_five_seconds_is_withdrawn(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    from sniper.services.memecoin_solana_swap_marks import QuoteMark
    cache._marks[("solana", MINT)] = QuoteMark(
        mint=MINT, pool="Pool", quote_mint=WSOL, quote_per_token=0.001, slot=100,
        observed_ts=1.0, observed_at=T0, source=SOURCE)
    _publisher(cache, path).cycle()                               # now_ts 10.4: 9.4 s old
    assert json.loads(path.read_text())["marks"] == {}


# --- one writer, no disk, no stall -----------------------------------------------------------------

def test_a_second_publisher_on_the_same_file_is_refused(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    first, second = _publisher(cache, path), _publisher(cache, path)
    assert first.acquire() is True
    assert second.acquire() is False
    first.release()
    assert second.acquire() is True
    second.release()


def test_an_unchanged_book_rewrites_only_on_the_heartbeat(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    clock = {"t": 100.0}
    pub = L.LiveMarkPublisher(cache, path=str(path), quote_usd_fn=lambda: {WSOL: (100.0, T1)},
                              now_iso_fn=lambda: T1, now_ts_fn=lambda: 10.4,
                              mono_fn=lambda: clock["t"], heartbeat_s=1.0)
    assert pub.cycle() is True
    clock["t"] += 0.1
    assert pub.cycle() is False                                   # nothing changed, heartbeat not due
    clock["t"] += 1.0
    assert pub.cycle() is True                                    # heartbeat: "checked" advances


def test_the_default_path_is_tmpfs_and_follows_the_state_root(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMECOIN_LIVE_MARKS_PATH", raising=False)
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / "a"))
    a = L.default_path()
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path / "b"))
    b = L.default_path()
    assert a != b and os.path.dirname(a) == os.path.dirname(b)
    if os.path.isdir("/dev/shm"):
        assert a.startswith("/dev/shm/")


# --- part 2: instants, not text; the quote handoff; the runner and UI wiring ----------------------

def _tape_only(conn, sweep_ts, mid=0.3, source=CURVE_SOURCE):
    conn.execute("DELETE FROM memecoin_position_marks")
    conn.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
                 "VALUES('solana', ?, ?, ?, ?, 'live')", (MINT, sweep_ts, mid, source))
    conn.commit()


# The window bounds of _latest(), read through the exact-route mark the Solana card still uses. The
# naive spelling is the ledger's UTC: _mark_age_seconds used to raise on aware-minus-naive and read
# it as age unknown, so a naive route mark was never fresh (DR-03,
# docs/audits/2026-09-27-post-review-delta.md).
@pytest.mark.parametrize("spelling", ["2026-09-27T10:00:00Z", "2026-09-27T10:00:00+00:00",
                                      "2026-09-27T12:00:00.000000+02:00", "2026-09-27T10:00:00.000000"])
def test_a_route_mark_at_the_same_second_is_not_read_as_future_whatever_its_spelling(
        conn, tmp_path, monkeypatch, spelling):
    conn.execute("DELETE FROM memecoin_position_marks")
    _add(conn, spelling, 0.3, "fastlane_rpc", quality="exact_route")
    row = _ticker(conn, tmp_path / "none.json", monkeypatch)
    assert (row["market_mid_basis"], row["market_mid_price"]) == ("route", pytest.approx(0.3))


def test_the_newest_instant_wins_whatever_the_spellings(conn, tmp_path, monkeypatch):
    """DR-03a (docs/audits/2026-09-27-post-review-delta.md): ORDER BY sweep_ts is TEXT, and
    "...:00Z" sorts above "...:00.400000Z" although it is 400 ms older. The newest instant among
    several competing spellings must win."""
    conn.execute("DELETE FROM memecoin_position_marks")
    for ts, mid in [("2026-09-27T09:59:59.900000Z", 0.11), ("2026-09-27T10:00:00Z", 0.10),
                    ("2026-09-27T10:00:00.100000+00:00", 0.12), ("2026-09-27T10:00:00.300000", 0.13),
                    ("2026-09-27T10:00:00.400000Z", 0.20)]:
        _add(conn, ts, mid, "fastlane_rpc", quality="exact_route")
    row = _ticker(conn, tmp_path / "none.json", monkeypatch, now="2026-09-27T10:00:01Z")
    assert (row["market_mid_basis"], row["market_mid_price"]) == ("route", pytest.approx(0.20))
    assert row["market_mid_at"] == "2026-09-27T10:00:00.400000Z"


def test_a_route_mark_after_now_is_still_not_an_observation(conn, tmp_path, monkeypatch):
    conn.execute("DELETE FROM memecoin_position_marks")
    _add(conn, "2026-09-27T10:00:01Z", 0.3, "fastlane_rpc", quality="exact_route")
    assert _ticker(conn, tmp_path / "none.json", monkeypatch)["market_mid_price"] is None


def test_a_newer_tape_row_never_outranks_the_live_mark(conn, tmp_path, monkeypatch):
    _tape_only(conn, "2026-09-27T10:00:00.300000+00:00", mid=0.3)
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 100, T0, now_ts=10.4)             # observed at 10:00:00.000
    _publisher(cache, path).cycle()
    assert _ticker(conn, path, monkeypatch)["market_mid_price"] == pytest.approx(0.2)


def test_a_failed_quote_fetch_keeps_the_old_value_and_its_old_time():
    holder = L.QuoteUsdHolder()
    holder.update({WSOL: 100.0}, T0)
    holder.update({}, T1)                                         # a failed batch
    assert holder.get() == ({WSOL: 100.0}, T0)


def test_a_kept_late_slot_mark_goes_unhealthy_when_its_conversion_ages(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    quote = {"v": {WSOL: (100.0, T0)}}
    pub = L.LiveMarkPublisher(cache, path=str(path), quote_usd_fn=lambda: quote["v"],
                              now_iso_fn=lambda: T1, now_ts_fn=lambda: 10.4)
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    pub.cycle()
    quote["v"] = {WSOL: (100.0, "2026-09-27T09:58:00.000000+00:00")}
    _curve(cache, 1_000_000_000, 100, T0, now_ts=10.4)
    pub.cycle()
    assert json.loads(path.read_text())["marks"]["solana:%s" % MINT]["healthy"] is False


def test_a_disabled_publisher_thread_returns_without_writing(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    t = threading.Thread(target=_publisher(cache, path, enabled=False).run, args=(threading.Event(),))
    t.start()
    t.join(timeout=5)
    assert not t.is_alive() and not path.exists()


def test_the_runner_starts_the_publisher_behind_its_flag_with_the_onchain_quote():
    src = open(os.path.join(os.path.dirname(__file__), "..", "scripts", "memecoin_runner_loop.py")).read()
    start = src.index("if _live_marks.enabled():")
    block = src[start:start + 3000]
    assert "LiveMarkPublisher(" in block and "quote_usd_fn=_live_marks.onchain_quote_usd" in block
    assert "carry=_live_marks.carry_enabled()" in block
    # The pool owner reads run on their own thread, behind their own flag.
    assert "if _live_marks.liquidity_enabled():" in block and "owner_book=_pool_owners" in block
    assert 'name="memecoin-live-mark-pool-owners"' in block
    assert "ExoticQuoteRefresher(_swap_mark_cache" in block
    assert 'resource["live_quote"]' not in src, "the DexScreener batch no longer feeds the display"


def test_display_sol_usd_has_one_writer(monkeypatch):
    monkeypatch.setattr(L, "SOL_USD", L.SingleWriterHolder())
    monkeypatch.setattr(L, "EXOTIC_USD", L.QuoteBook())
    assert L.publish_sol_usd(150.0, T0) is True                    # the slot lane's role claims it
    assert L.publish_sol_usd(999.0, T1, writer="someone_else") is False
    assert L.publish_sol_usd(151.0, T1) is True                    # same role: a restarted lane
    assert L.onchain_quote_usd() == {WSOL: (151.0, T1), L.USDC: (1.0, None)}


def test_display_sol_usd_is_the_slot_lanes_jupiter_quote_never_dexscreener(monkeypatch):
    monkeypatch.setattr(L, "SOL_USD", L.SingleWriterHolder())
    monkeypatch.setattr(L, "EXOTIC_USD", L.QuoteBook())
    assert WSOL not in L.onchain_quote_usd()                       # no SOL price yet: no SOL marks
    L.publish_sol_usd(150.0, T0)
    L.publish_sol_usd(0, T1)                                       # a bad read keeps the good one
    assert L.onchain_quote_usd() == {WSOL: (150.0, T0), L.USDC: (1.0, None)}


def test_the_slot_lane_refresher_publishes_its_sol_price_for_display(monkeypatch):
    from sniper.services.memecoin_slot_lane import LanePrefetcher
    monkeypatch.setattr(L, "SOL_USD", L.SingleWriterHolder())
    r = LanePrefetcher(usdc_fn=lambda: 10.0, sol_fn=lambda: None, blockhash_fn=lambda: None,
                          sol_usd_fn=lambda: 151.5)
    r.refresh_once()
    usd, at = L.onchain_quote_usd()[WSOL]
    assert usd == 151.5 and at is not None


# --- no DexScreener on the Solana screen (operator, 2026-09-27) --------------------------------

def _add(conn, sweep_ts, mid, source, quality="live"):
    conn.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
                 "VALUES('solana', ?, ?, ?, ?, ?)", (MINT, sweep_ts, mid, source, quality))
    conn.commit()


def _live(tmp_path, observed_at=T0):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, observed_at, now_ts=10.4)    # 0.2
    _publisher(cache, path).cycle()
    return path


@pytest.mark.parametrize("dex", ["dexscreener", "fastlane_mid"])
def test_a_newer_dexscreener_row_never_becomes_the_solana_market_price(conn, tmp_path, monkeypatch, dex):
    _add(conn, "2026-09-27T10:00:00.300000+00:00", 9.9, dex)       # newer than the live mark
    row = _ticker(conn, _live(tmp_path), monkeypatch)
    assert row["market_mid_price"] == pytest.approx(0.2) and row["market_mid_source"] == CURVE_SOURCE


def test_with_only_dexscreener_the_route_price_is_shown_labelled_route(conn, tmp_path, monkeypatch):
    conn.execute("DELETE FROM memecoin_position_marks")
    _add(conn, "2026-09-27T10:00:00.300000+00:00", 9.9, "dexscreener")
    _add(conn, "2026-09-27T10:00:00.200000+00:00", 0.15, "fastlane_rpc", quality="exact_route")
    row = _ticker(conn, tmp_path / "none.json", monkeypatch)
    assert row["market_mid_basis"] == "route" and row["market_mid_price"] == pytest.approx(0.15)


def test_with_only_dexscreener_and_no_route_the_price_is_unknown(conn, tmp_path, monkeypatch):
    conn.execute("DELETE FROM memecoin_position_marks")
    _add(conn, "2026-09-27T10:00:00.300000+00:00", 9.9, "dexscreener")
    assert _ticker(conn, tmp_path / "none.json", monkeypatch)["market_mid_price"] is None


def test_a_dex_converted_onchain_tape_row_is_never_the_solana_market_price(conn, tmp_path, monkeypatch):
    """DR-04 (docs/audits/2026-09-27-post-review-delta.md). The fastlane values on-chain swap and
    curve marks in USD with DexScreener's quote-asset price (_batched_mid_snapshot), so a tape row
    named fastlane_sol_curve or fastlane_sol_swap can carry a DexScreener number. The tape row is
    built here by the real writer path from a DexScreener SOL/USD of $100."""
    from sniper.services.memecoin_fastlane import _batched_mid_snapshot
    _, quote_usd = _batched_mid_snapshot(
        [], quote_mints={WSOL}, batch_fn=lambda keys: {("solana", WSOL): {"price": 100.0}})
    cache = SolanaSwapMarkCache()
    _curve(cache, 2_000_000_000, 101, T1, now_ts=10.4)
    mark = cache.fresh_price_map(quote_usd, now_ts=10.4, max_age_s=5.0)[("solana", MINT)]
    assert mark["source"] == CURVE_SOURCE and mark["price"] == pytest.approx(0.2)
    _tape_only(conn, T1, mark["price"], source=mark["source"])
    row = _ticker(conn, tmp_path / "absent.json", monkeypatch)
    assert (row["market_mid_price"], row["market_mid_source"]) == (None, None)


@pytest.mark.parametrize("source", [SOURCE, CURVE_SOURCE])
def test_without_a_live_mark_a_fresh_onchain_tape_row_yields_to_the_route_price(
        conn, tmp_path, monkeypatch, source):
    _tape_only(conn, T1, 0.5, source=source)                      # fresh: the old 5 s window served it
    _add(conn, "2026-09-27T10:00:00.200000+00:00", 0.15, "fastlane_rpc", quality="exact_route")
    row = _ticker(conn, tmp_path / "absent.json", monkeypatch)
    assert (row["market_mid_basis"], row["market_mid_price"]) == ("route", pytest.approx(0.15))


def test_a_live_mark_dated_in_the_future_is_not_an_observation(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, "2026-09-27T10:00:02.000000+00:00", now_ts=10.4)
    _publisher(cache, path).cycle()                               # published at T1, 1.6 s before it
    assert _ticker(conn, path, monkeypatch)["market_mid_price"] is None


def test_the_solana_market_cap_uses_the_pinned_onchain_supply_not_a_dexscreener_row(conn, tmp_path, monkeypatch):
    conn.execute("UPDATE memecoin_positions SET fdv_supply=1000000000")
    conn.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, mcap, source, quality) "
                 "VALUES('solana', ?, '2026-09-27T10:00:00.200000+00:00', 0.1, 55, 'dexscreener', 'live')", (MINT,))
    conn.commit()
    row = _ticker(conn, _live(tmp_path), monkeypatch)
    assert row["market_cap"] == pytest.approx(0.2 * 1e9)


def test_the_ui_labels_a_live_curve_mark_and_shows_changed_and_checked():
    from test_price_freshness import APP, _method, _node
    assert "fastlane_sol_curve: 'curve'" in APP
    body = (_method("_marketMidCell", "_mergeLastKnown") + _method("_realTickerRows", "_cardSig")
            + _method("_operatorQuote", "_marketSourceLabel") + _method("_marketSourceLabel", "_marketMidCell")
            + _method("_supplyOf", "_fmtAgo"))
    p = {"market_mid_price": 0.2, "market_mid_basis": "market", "market_mid_source": CURVE_SOURCE,
         "market_mid_at": "2026-09-27T10:00:00.4Z", "market_mid_fetched_at": "2026-09-27T10:00:01Z"}
    script = ("const esc = s => String(s); const view = { _operatorPrice(v) { return String(v); },"
              " _operatorTime(v) { return v; }, _quoteAge(v) { return 'AGE(' + v + ')'; },"
              " _operatorValueRows() { return ''; }, _realRangeBar() { return ''; }, _realFactsLine() { return ''; }, "
              + body + "}; const p = " + json.dumps(p) + ";"
              " console.log(JSON.stringify([view._marketMidCell(p.market_mid_price, p.market_mid_basis,"
              " p.market_mid_source, p.market_mid_at, p.market_mid_fetched_at), view._realTickerRows(p)]));")
    cell, card = _node(script)
    for html in (cell, card):
        assert "curve" in html
        assert "changed" in html and "AGE(2026-09-27T10:00:00.4Z)" in html, html
        assert "checked" in html and "AGE(2026-09-27T10:00:01Z)" in html, html


# --- step 2: quiet pools carry the last trade; odd quote tokens through Jupiter --------------------

BPXX = "BPxxfRCXkUVhig4HS1Lh7kZqV6SPJhzfEk4x6fVBjPCy"
AMM = PoolBinding(mint=MINT, pool="AmmPool", coin_vault="cv", quote_vault="qv", quote_mint=WSOL)


POST = (9 * 10**11, 11 * 10**11)            # the reserves a test swap leaves behind


def _swap(cache, slot, now_ts, binding=AMM, amount1=-1_000_000_000, post=POST):
    """A buy of 1,000 tokens (6 dec) for 1 quote unit (9 dec): 0.001 quote per token. Carries its
    post-trade reserves, as every VaultTape swap row does since 2d770bd0; post=None for an older row."""
    row = {"kind": "sol_swap", "pool": binding.pool, "amount0": -1_000_000_000,
           "amount1": -amount1, "block_no": slot, "decimals0": 6, "decimals1": 9}
    if post is not None:
        row.update(balance0=str(post[0]), balance1=str(post[1]))
    assert cache.record(row, binding, now_ts=now_ts, observed_at=T0)


def _sync(cache, slot, now_ts, coin, quote, binding=AMM):
    assert cache.record({"kind": "sol_sync", "pool": binding.pool, "amount0": coin, "amount1": quote,
                         "block_no": slot, "decimals0": 6, "decimals1": 9},
                        binding, now_ts=now_ts, observed_at=T0)


def _carry_pub(cache, path, now_ts):
    return L.LiveMarkPublisher(cache, path=str(path), carry=True, now_iso_fn=lambda: T1,
                               quote_usd_fn=lambda: {WSOL: (100.0, T1)}, now_ts_fn=lambda: now_ts)


def test_a_quiet_pool_keeps_its_last_trade_labelled_carried(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    assert _carry_pub(cache, path, now_ts=20.0).cycle()               # 19 s since the trade
    mark = json.loads(path.read_text())["marks"]["solana:%s" % MINT]
    assert mark["carried"] is True and mark["price_usd"] == pytest.approx(0.1)


def test_a_reserve_change_after_the_trade_withdraws_the_carry_after_the_grace(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    _sync(cache, 99, now_ts=0.9, coin=10**12, quote=10**12)          # before the trade: no effect
    _sync(cache, 105, now_ts=10.0, coin=2 * 10**12, quote=10**12)    # moved after it, no new trade
    _carry_pub(cache, path, now_ts=10.5).cycle()                      # inside the 1 s grace: kept
    assert "solana:%s" % MINT in json.loads(path.read_text())["marks"]
    _carry_pub(cache, path, now_ts=11.5).cycle()                      # past it: withdrawn
    assert json.loads(path.read_text())["marks"] == {}


def test_a_newer_trade_after_the_change_is_shown(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    _sync(cache, 105, now_ts=10.0, coin=2 * 10**12, quote=10**12)    # the first half of trade 2
    _swap(cache, 105, now_ts=10.01, amount1=-2_000_000_000)          # its swap row, same slot
    _carry_pub(cache, path, now_ts=30.0).cycle()
    mark = json.loads(path.read_text())["marks"]["solana:%s" % MINT]
    assert mark["slot"] == 105 and mark["price_usd"] == pytest.approx(0.2)


def test_an_unconfirmed_carry_is_dropped_two_seconds_after_a_rollover(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    cache.invalidate_prices(now_ts=10.0)                              # the 30 s resubscription
    _carry_pub(cache, path, now_ts=11.5).cycle()                      # seed pending: kept
    assert "solana:%s" % MINT in json.loads(path.read_text())["marks"]
    _carry_pub(cache, path, now_ts=20.0).cycle()                      # no seed ever came: dropped
    assert json.loads(path.read_text())["marks"] == {}


# --- DR-06: carry by reserve identity, not slot order ---------------------------------------------

def test_a_later_change_in_the_same_slot_withdraws_the_carry_after_the_grace(tmp_path):
    """DR-06 (docs/audits/2026-09-27-post-review-delta.md). Slot order could not see a change in
    the swap's own slot: the carry stayed on forever."""
    cache = SolanaSwapMarkCache()
    _swap(cache, 100, now_ts=1.0)
    _sync(cache, 100, now_ts=10.0, coin=2 * 10**12, quote=10**12)    # same slot, after the swap row
    kept = cache.display_marks({WSOL: 100.0}, now_ts=10.5, max_age_s=5.0, carry=True)
    assert kept[("solana", MINT)]["carried"] is True                 # inside the 1 s grace
    assert cache.display_marks({WSOL: 100.0}, now_ts=20.0, max_age_s=5.0, carry=True) == {}


@pytest.mark.parametrize("first_half", [(9 * 10**11, 10**12), (10**12, 11 * 10**11)])
def test_the_two_vault_frames_of_one_swap_keep_its_carry(first_half):
    # Either vault can arrive first; its one-sided sync precedes the swap row in the same slot.
    cache = SolanaSwapMarkCache()
    _sync(cache, 99, now_ts=0.5, coin=10**12, quote=10**12)
    _sync(cache, 100, now_ts=0.99, coin=first_half[0], quote=first_half[1])
    _swap(cache, 100, now_ts=1.0)
    marks = cache.display_marks({WSOL: 100.0}, now_ts=40.0, max_age_s=5.0, carry=True)
    assert marks[("solana", MINT)]["carried"] is True


def test_a_swap_without_post_trade_reserves_is_never_carried():
    cache = SolanaSwapMarkCache()
    _swap(cache, 100, now_ts=1.0, post=None)
    assert ("solana", MINT) in cache.display_marks({WSOL: 100.0}, now_ts=5.9, max_age_s=5.0, carry=True)
    assert cache.display_marks({WSOL: 100.0}, now_ts=6.1, max_age_s=5.0, carry=True) == {}


CURVE_MINT = "CurveMint111111111111111111111111111111111"
CURVE = PoolBinding(mint=CURVE_MINT, pool="CurvePda", coin_vault="cv2", quote_vault="CurvePda",
                    quote_mint=WSOL)


def test_the_carry_bookkeeping_never_moves_the_exit_map_or_the_silence_witness():
    """The same frames give the same fresh_price_map and reserve_witness as before DR-06 and the
    on-chain liquidity (values captured on c45f96ef and 2fc06c68). A same-slot change, a lower-slot
    sync, a seed and a curve with real reserves are all in the sequence."""
    cache = SolanaSwapMarkCache()

    def rec(row, now_ts, at):
        assert cache.record({**row, "pool": AMM.pool, "decimals0": 6, "decimals1": 9}, AMM,
                            now_ts=now_ts, observed_at="2026-09-27T10:00:%s+00:00" % at)
    rec({"kind": "sol_sync", "amount0": 10**12, "amount1": 10**12, "block_no": 99}, 0.9, "00.900000")
    rec({"kind": "sol_sync", "amount0": 9 * 10**11, "amount1": 10**12, "block_no": 100}, 0.95, "00.950000")
    rec({"kind": "sol_swap", "amount0": -10**11, "amount1": 10**11, "block_no": 100,
         "balance0": str(9 * 10**11), "balance1": str(11 * 10**11)}, 1.0, "01.000000")
    rec({"kind": "sol_sync", "amount0": 2 * 10**12, "amount1": 10**12, "block_no": 100}, 1.2, "01.200000")
    rec({"kind": "sol_seed", "amount0": "2000000000000", "amount1": "1000000000000", "block_no": 130},
        1.3, "01.300000")
    # A curve with its real reserves (the displayed liquidity reads real_sol, 2026-09-27).
    assert cache.record({"kind": "sol_curve_state", "pool": CURVE.pool, "virtual_sol": 2_000_000_000,
                         "virtual_token": 1_000_000_000, "real_sol": 1_500_000_000,
                         "real_token": 800_000_000, "block_no": 131, "complete": False}, CURVE,
                        now_ts=1.3, observed_at="2026-09-27T10:00:01.300000+00:00")
    assert cache.fresh_price_map({WSOL: 100.0}, now_ts=1.3, max_age_s=5.0) == {
        ("solana", MINT): {"price": pytest.approx(0.1), "liquidity": None, "mcap": None,
                           "source": SOURCE, "quality": "live",
                           "observed_at": "2026-09-27T10:00:01.000000+00:00"},
        ("solana", CURVE_MINT): {"price": pytest.approx(0.2), "liquidity": None, "mcap": None,
                                 "source": CURVE_SOURCE, "quality": "live",
                                 "observed_at": "2026-09-27T10:00:01.300000+00:00"}}
    witness = [cache.reserve_witness(mint=MINT, since_iso="2026-09-27T10:00:00.800000+00:00",
                                     window_start_iso="2026-09-27T10:00:%s+00:00" % start,
                                     now_iso="2026-09-27T10:00:01.300000+00:00")
               for start in ("00.800000", "01.100000", "01.250000")]
    assert witness == ["moving", "moving", "still"]
    assert cache.reserve_witness(mint=CURVE_MINT, since_iso="2026-09-27T10:00:00.800000+00:00",
                                 window_start_iso="2026-09-27T10:00:01.250000+00:00",
                                 now_iso="2026-09-27T10:00:01.300000+00:00") == "uncovered_transient"


def test_a_carried_price_reaches_the_ticker_flagged_carried(conn, tmp_path, monkeypatch):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    _carry_pub(cache, path, now_ts=20.0).cycle()                      # 19 s quiet: held
    row = _ticker(conn, path, monkeypatch)
    assert (row["market_mid_price"], row["market_mid_source"]) == (pytest.approx(0.1), SOURCE)
    assert row["market_mid_carried"] is True


@pytest.mark.parametrize("carried", [True, False])
def test_the_ui_says_carried_only_for_a_held_price(carried):
    from test_price_freshness import APP, _method, _node
    assert "'market_mid_carried'" in APP[APP.index("    _mergePosition("):APP.index("    _mergePositionBook(")]
    body = (_method("_marketMidCell", "_mergeLastKnown") + _method("_realTickerRows", "_cardSig")
            + _method("_operatorQuote", "_marketSourceLabel") + _method("_marketSourceLabel", "_marketMidCell")
            + _method("_supplyOf", "_fmtAgo"))
    p = {"market_mid_price": 0.1, "market_mid_basis": "market", "market_mid_source": SOURCE,
         "market_mid_at": "2026-09-27T10:00:00Z", "market_mid_fetched_at": "2026-09-27T10:00:19Z",
         "market_mid_carried": carried}
    script = ("const esc = s => String(s); const view = { _operatorPrice(v) { return String(v); },"
              " _operatorTime(v) { return v; }, _quoteAge(v) { return 'AGE(' + v + ')'; },"
              " _operatorValueRows() { return ''; }, _realRangeBar() { return ''; }, _realFactsLine() { return ''; }, "
              + body + "}; const p = " + json.dumps(p) + ";"
              " console.log(JSON.stringify([view._marketMidCell(p.market_mid_price, p.market_mid_basis,"
              " p.market_mid_source, p.market_mid_at, p.market_mid_fetched_at, p.market_mid_carried),"
              " view._realTickerRows(p)]));")
    for html in _node(script):
        assert ("carried" in html) is carried, html


# --- on-chain liquidity (operator 2026-09-27 via Alpha: replace DexScreener where better exists) ---

PUMPSWAP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CLMM = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"
METEORA_POOLS = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"


def _owners(program):
    book = L.PoolOwnerBook(lambda pool: program)
    book.want([AMM.pool])
    book.refresh_once()
    return book


def _liq_pub(cache, path, book, now_ts=1.5):
    return L.LiveMarkPublisher(cache, path=str(path), carry=True, now_iso_fn=lambda: T1,
                               quote_usd_fn=lambda: {WSOL: (100.0, T1)}, now_ts_fn=lambda: now_ts,
                               owner_book=book)


def _published(path, mint=MINT):
    return json.loads(path.read_text())["marks"]["solana:%s" % mint]


def test_a_constant_product_pool_publishes_twice_its_real_quote_deposit(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)                 # post reserves: 9e11 coin, 11e11 quote (9 dec)
    _liq_pub(cache, path, _owners(PUMPSWAP)).cycle()
    mark = _published(path)
    assert mark["liquidity_usd"] == pytest.approx(2 * 1100 * 100.0)
    assert (mark["liquidity_basis"], mark["liquidity_slot"], mark["liquidity_at"]) == ("onchain", 100, T0)


@pytest.mark.parametrize("program", [CLMM, METEORA_POOLS, None])
def test_a_pool_whose_vault_is_not_its_depth_publishes_no_liquidity(tmp_path, program):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    _liq_pub(cache, path, _owners(program)).cycle()
    assert "liquidity_usd" not in _published(path)


def test_a_curve_publishes_twice_its_real_sol(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    assert cache.record({"kind": "sol_curve_state", "pool": BINDING.pool, "virtual_sol": 2_000_000_000,
                         "virtual_token": 1_000_000_000, "real_sol": 1_500_000_000,
                         "block_no": 101, "complete": False}, BINDING, now_ts=1.0, observed_at=T1)
    _liq_pub(cache, path, L.PoolOwnerBook(lambda pool: None)).cycle()
    mark = _published(path)
    assert mark["liquidity_usd"] == pytest.approx(2 * 1.5 * 100.0)
    assert (mark["liquidity_slot"], mark["liquidity_at"]) == (101, T1)


def test_an_amm_pair_not_seen_since_the_rollover_publishes_no_liquidity(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    cache.invalidate_prices(now_ts=10.0)
    _liq_pub(cache, path, _owners(PUMPSWAP), now_ts=11.0).cycle()   # carried, seed pending
    assert "liquidity_usd" not in _published(path)
    _seed(cache, 130, now_ts=10.5, pair=POST)
    _liq_pub(cache, path, _owners(PUMPSWAP), now_ts=11.0).cycle()
    assert _published(path)["liquidity_usd"] == pytest.approx(2 * 1100 * 100.0)


def test_an_owner_read_past_its_deadline_is_a_counted_failure_retried_later():
    calls = []

    def slow(pool):
        calls.append(pool)
        time.sleep(0.3)
        return PUMPSWAP
    clock = [0.0]
    book = L.PoolOwnerBook(slow, deadline_s=0.05, retry_s=30.0, mono_fn=lambda: clock[0])
    book.want(["P1"])
    book.refresh_once()
    assert (book.owner("P1"), book.counts()) == (None, {"known": 0, "reads": 1, "failures": 1})
    book.refresh_once()                                              # inside the retry wait
    assert len(calls) == 1
    clock[0] = 31.0
    book.deadline_s = 1.0
    book.refresh_once()
    assert book.owner("P1") == PUMPSWAP and book.counts()["failures"] == 1


def test_an_owner_read_that_raises_is_a_counted_failure():
    def boom(pool):
        raise TimeoutError("read timed out")
    book = L.PoolOwnerBook(boom)
    book.want(["P1"])
    book.refresh_once()
    assert book.owner("P1") is None and book.counts()["failures"] == 1


def test_the_pool_owner_reader_reads_the_account_owner_and_refuses_junk():
    from sniper.services.memecoin_solana_swap_marks import pool_owner_fn
    seen = []

    def rpc(method, params):
        seen.append((method, params))
        return {"context": {"slot": 5}, "value": {"owner": PUMPSWAP, "data": ["", "base64"]}}
    assert pool_owner_fn("u", rpc_call=rpc)("P1") == PUMPSWAP
    assert seen[0][0] == "getAccountInfo" and seen[0][1][1]["encoding"] == "jsonParsed"
    assert len(seen) == 1                        # a non-token account names its program directly
    assert pool_owner_fn("u", rpc_call=lambda m, p: {"value": None})("P1") is None
    assert pool_owner_fn("u", rpc_call=lambda m, p: None)("P1") is None


# REAL FRAMES, 2026-09-27 (position 66034). The tape binds an AMM mark to its COIN VAULT: 7PHy3q...
# is a Token-2022 account (space 170) whose SPL authority CrZLtWV8... is the PumpSwap pool, owned
# by pAMMBay6... (research key read). Before this fix the reader returned the token program, so
# on-chain liquidity never showed for any AMM mark.
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SYSTEM = "11111111111111111111111111111111"
CPMM = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def _chain(accounts):
    """getAccountInfo over a dict {address: (owner, spl_authority_or_None)}."""
    calls = []

    def rpc(method, params):
        calls.append(params[0])
        owner, authority = accounts.get(params[0], (None, None))
        if owner is None:
            return {"value": None}
        data = ({"parsed": {"info": {"owner": authority}, "type": "account"}, "program": "spl-token"}
                if authority else ["", "base64"])
        return {"value": {"owner": owner, "data": data}}
    return rpc, calls


@pytest.mark.parametrize("vault_authority,authority_owner,expected", [
    ("CrZLtWV8oEGRXyKxAm2fyr5Bjz94kGDpbqnoZL3rP44N", PUMPSWAP, PUMPSWAP),   # 66034, pool owns its vaults
    ("GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL", SYSTEM, CPMM),        # shared CPMM authority PDA
    ("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", SYSTEM, V4),          # shared v4 authority PDA
    ("HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC", SYSTEM, SYSTEM),      # Meteora DAMM v2: not CP
])
def test_a_vault_resolves_through_its_authority_to_the_pool_program(vault_authority, authority_owner,
                                                                     expected):
    from sniper.services.memecoin_solana_swap_marks import pool_owner_fn
    vault = "7PHy3qaCCy1qsVFCk5FnSuuuMabuG1udhwK8gJReVm8q"
    rpc, calls = _chain({vault: (TOKEN_2022, vault_authority), vault_authority: (authority_owner, None)})
    assert pool_owner_fn("u", rpc_call=rpc)(vault) == expected
    assert (expected in L.CONSTANT_PRODUCT_PROGRAMS) == (expected in (PUMPSWAP, CPMM, V4))
    assert calls[0] == vault and len(calls) == (1 if expected in (CPMM, V4) else 2)


def test_an_unreadable_authority_is_unknown_not_a_program():
    from sniper.services.memecoin_solana_swap_marks import pool_owner_fn
    vault = "7PHy3qaCCy1qsVFCk5FnSuuuMabuG1udhwK8gJReVm8q"
    rpc, _calls = _chain({vault: (TOKEN_2022, "Gone1111111111111111111111111111111111111111")})
    assert pool_owner_fn("u", rpc_call=rpc)(vault) is None
    rpc, _calls = _chain({vault: (TOKEN_2022, None)})          # token account without a parsed owner
    assert pool_owner_fn("u", rpc_call=rpc)(vault) is None


def test_the_owner_book_gives_the_66034_vault_its_pumpswap_program():
    from sniper.services.memecoin_solana_swap_marks import pool_owner_fn
    vault, pool = "7PHy3qaCCy1qsVFCk5FnSuuuMabuG1udhwK8gJReVm8q", "CrZLtWV8oEGRXyKxAm2fyr5Bjz94kGDpbqnoZL3rP44N"
    rpc, _calls = _chain({vault: (TOKEN_2022, pool), pool: (PUMPSWAP, None)})
    book = L.PoolOwnerBook(pool_owner_fn("u", rpc_call=rpc))
    assert book.deadline_s == 3.0
    book.want([vault])
    book.refresh_once()
    assert L.CONSTANT_PRODUCT_PROGRAMS.get(book.owner(vault)) == "pumpswap"


def test_no_owner_book_publishes_no_liquidity(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    _liq_pub(cache, path, None).cycle()
    assert "liquidity_usd" not in _published(path)


def _liq_live(tmp_path, liquidity_usd=1000.0):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _curve(cache, 2_000_000_000, 101, T0, now_ts=10.4)
    _publisher(cache, path).cycle()
    snap = json.loads(path.read_text())
    snap["marks"]["solana:%s" % MINT].update(liquidity_usd=liquidity_usd, liquidity_at=T0)
    path.write_text(json.dumps(snap))
    return path


def test_the_ticker_prefers_onchain_liquidity_over_a_wash_inflated_dexscreener_row(conn, tmp_path, monkeypatch):
    conn.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, liquidity, source, quality) "
                 "VALUES('solana', ?, '2026-09-27T10:00:00.200000+00:00', 0.1, 92800000, 'fastlane_mid', 'live')",
                 (MINT,))
    conn.commit()
    row = _ticker(conn, _liq_live(tmp_path), monkeypatch)
    assert (row["liquidity"], row["liquidity_source"], row["liquidity_at"]) == (1000.0, "onchain", T0)
    row = _ticker(conn, tmp_path / "none.json", monkeypatch)                  # no live mark
    assert (row["liquidity"], row["liquidity_source"]) == (92800000, "dexscreener")


def test_the_ticker_names_stored_liquidity_and_never_invents_one(conn, tmp_path, monkeypatch):
    row = _ticker(conn, tmp_path / "none.json", monkeypatch)
    assert (row["liquidity"], row["liquidity_source"]) == (None, None)
    m.set_token_liquidity(conn, "solana", MINT, 5000.0, "2026-09-27T10:00:00Z")
    row = _ticker(conn, tmp_path / "none.json", monkeypatch)
    assert (row["liquidity"], row["liquidity_source"]) == (5000.0, "stored")


@pytest.mark.parametrize("source,label", [("onchain", "on-chain"), ("dexscreener", "DexScreener"),
                                          ("stored", "stored"), (None, "")])
def test_the_ui_names_the_liquidity_source(source, label):
    from test_price_freshness import APP, _node
    body = APP[APP.index("    _liquiditySourceLabel("):APP.index("    _mergeLastKnown(")]
    script = ("const esc = s => String(s); const view = { _usd(v) { return '$' + v; }, " + body + "};"
              " console.log(JSON.stringify([view._liquidityCell(1000, " + json.dumps(source) + "),"
              " view._liquidityCell(null, " + json.dumps(source) + ")]));")
    cell, empty = _node(script)
    assert cell.startswith("$1000") and (label in cell if label else "<small" not in cell), cell
    assert "—" in empty


# --- the reconnect (operator 2026-09-27: "fix the reconnect") ----------------------------------

def _swap_with_post(cache, slot, now_ts, post=(9 * 10**11, 11 * 10**11)):
    assert cache.record({"kind": "sol_swap", "pool": AMM.pool, "amount0": -1_000_000_000,
                         "amount1": 1_000_000_000, "block_no": slot, "decimals0": 6, "decimals1": 9,
                         "balance0": str(post[0]), "balance1": str(post[1])},
                        AMM, now_ts=now_ts, observed_at=T0)


def _seed(cache, slot, now_ts, pair):
    assert cache.record({"kind": "sol_seed", "pool": AMM.pool, "amount0": str(pair[0]),
                         "amount1": str(pair[1]), "block_no": slot, "decimals0": 6, "decimals1": 9},
                        AMM, now_ts=now_ts, observed_at=T0)


def test_a_seed_showing_the_same_reserves_carries_the_trade_across_the_rollover(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap_with_post(cache, 100, now_ts=1.0)
    cache.invalidate_prices(now_ts=10.0)
    _seed(cache, 130, now_ts=10.3, pair=(9 * 10**11, 11 * 10**11))    # nothing traded in the gap
    _carry_pub(cache, path, now_ts=40.0).cycle()
    mark = json.loads(path.read_text())["marks"]["solana:%s" % MINT]
    assert mark["carried"] is True and mark["slot"] == 100


def test_a_seed_showing_different_reserves_withdraws_the_carry(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap_with_post(cache, 100, now_ts=1.0)
    cache.invalidate_prices(now_ts=10.0)
    _seed(cache, 130, now_ts=10.3, pair=(8 * 10**11, 12 * 10**11))    # a trade happened in the gap
    _carry_pub(cache, path, now_ts=40.0).cycle()
    assert json.loads(path.read_text())["marks"] == {}


def test_a_seed_is_display_only_never_a_state_mark_or_silence_evidence():
    cache = SolanaSwapMarkCache()
    _seed(cache, 130, now_ts=10.3, pair=(9 * 10**11, 11 * 10**11))
    assert cache._state_marks == {} and cache._reserve_times == {} and cache._reserve_last == {}
    assert cache.fresh_price_map({WSOL: 100.0}, now_ts=10.3, max_age_s=5.0) == {}


def test_a_disconnect_clears_every_carry():
    cache = SolanaSwapMarkCache()
    _swap_with_post(cache, 100, now_ts=1.0)
    cache.invalidate()
    assert cache.display_marks({WSOL: 100.0}, now_ts=40.0, max_age_s=5.0, carry=True) == {}


def test_another_pool_for_the_mint_drops_the_old_pools_carry():
    cache = SolanaSwapMarkCache()
    _swap_with_post(cache, 100, now_ts=1.0)
    other = PoolBinding(mint=MINT, pool="NewPool", coin_vault="c2", quote_vault="q2", quote_mint=WSOL)
    cache.invalidate_prices(now_ts=10.0)
    assert cache.record({"kind": "sol_seed", "pool": "NewPool", "amount0": "5", "amount1": "5",
                         "block_no": 131, "decimals0": 6, "decimals1": 9}, other, now_ts=10.3, observed_at=T0)
    assert cache.display_marks({WSOL: 100.0}, now_ts=40.0, max_age_s=5.0, carry=True) == {}


def test_the_vault_tape_takes_a_seed_as_baseline_so_the_first_trade_is_a_swap():
    from sniper.services.memecoin_solana_ws import VaultTape
    tape = VaultTape("cv", "qv", mint="P")
    assert tape.seed_balance("cv", 130, 1_000, decimals=6) is None      # one side known
    seed_row = tape.seed_balance("qv", 130, 2_000, decimals=9)
    assert seed_row["kind"] == "sol_seed" and (seed_row["amount0"], seed_row["amount1"]) == ("1000", "2000")
    assert tape.on_balance("cv", 131, 900, decimals=6)["kind"] == "sol_sync"   # first half of a trade
    row = tape.on_balance("qv", 131, 2_100, decimals=9)
    assert row["kind"] == "sol_swap" and (row["amount0"], row["amount1"]) == ("-100", "100")
    assert (row["balance0"], row["balance1"]) == ("900", "2100")


def test_before_the_fix_the_first_trade_after_a_rollover_was_invisible():
    from sniper.services.memecoin_solana_ws import VaultTape
    tape = VaultTape("cv", "qv", mint="P")                               # no seed
    assert tape.on_balance("cv", 131, 900) is None and tape.on_balance("qv", 131, 2_100) is None


def test_a_notification_that_arrived_first_wins_over_the_seed():
    from sniper.services.memecoin_solana_ws import VaultTape
    tape = VaultTape("cv", "qv", mint="P")
    tape.on_balance("cv", 131, 900)                                      # the frame beat the seed
    assert tape.seed_balance("cv", 130, 1_000) is None
    assert tape._last["cv"] == 900


def test_the_vault_seed_reader_parses_a_token_account_and_refuses_junk():
    from sniper.services.memecoin_solana_swap_marks import vault_seed_fn
    good = {"context": {"slot": 130}, "value": {"data": {"parsed": {"info": {
        "tokenAmount": {"amount": "1234", "decimals": 6}}}}}}
    assert vault_seed_fn("u", rpc_call=lambda m, p: good)("V") == (130, 1234, 6)
    for bad in (None, {}, {"context": {"slot": 130}, "value": None},
                {"context": {"slot": 130}, "value": {"data": ["x", "base64"]}},
                {"context": {"slot": 130}, "value": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "x"}}}}}}):
        assert vault_seed_fn("u", rpc_call=lambda m, p, b=bad: b)("V") is None


def test_the_stream_seeds_a_vault_after_its_ack_and_emits_the_seed_row():
    import asyncio
    from sniper.services import memecoin_solana_ws as W
    from test_memecoin_solana_curve_marks import _Socket
    sock, rows, seeded = _Socket(), [], []

    def seed(vault):
        seeded.append(vault)
        return 130, {"cv": 1_000, "qv": 2_000}[vault], 6

    tape = W.VaultTape("cv", "qv", mint="P")
    asyncio.run(W.run_vault_stream({"cv": tape, "qv": tape}, rows.append, ws_url="wss://x.invalid/",
                                   connect=lambda url: sock, vault_seed_fn=seed))
    assert sorted(seeded) == ["cv", "qv"]
    assert [r["kind"] for r in rows] == ["sol_seed"]


def _assert_vault_seed_guard(src):
    from types import SimpleNamespace
    worker = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == '_solana_swap_mark_worker')
    seed = next(k.value for n in ast.walk(worker)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'run_price_stream_epoch'
                for k in n.keywords if k.arg == 'vault_seed_fn')
    assert isinstance(seed, ast.IfExp), 'vault seed must remain conditional'
    assert isinstance(seed.body, ast.Call) and seed.body.func.id == 'vault_seed_fn'
    assert isinstance(seed.orelse, ast.Constant) and seed.orelse.value is None
    reads = [n for n in ast.walk(seed.test) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute) and n.func.attr == 'getenv']
    assert len(reads) == 1 and reads[0].args[0].value == 'MEMECOIN_SOL_VAULT_SEED'
    predicate = compile(ast.Expression(seed.test), '<vault seed flag>', 'eval')
    for flag, enabled in [('0', False), ('false', False), ('NO', False), (' off ', False),
                          ('1', True), ('true', True), (None, True)]:
        env = SimpleNamespace(getenv=lambda name, default: default if flag is None else flag)
        assert eval(predicate, {'os': env, '__builtins__': {}}) is enabled


def test_the_runner_seeds_vaults_behind_its_flag():
    src = open(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'memecoin_runner_loop.py')).read()
    _assert_vault_seed_guard(src)


def test_vault_seed_guard_check_rejects_removed_flag():
    src = open(os.path.join(os.path.dirname(__file__), '..', 'scripts', 'memecoin_runner_loop.py')).read()
    tree = ast.parse(src)
    seed = next(k for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == 'run_price_stream_epoch'
                for k in n.keywords if k.arg == 'vault_seed_fn')
    seed.value = seed.value.body
    with pytest.raises(AssertionError, match='must remain conditional'):
        _assert_vault_seed_guard(ast.unparse(tree))


def test_carry_off_shows_only_fresh_trades(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_LIVE_MARKS_CARRY", "0")
    assert L.carry_enabled() is False
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=1.0)
    L.LiveMarkPublisher(cache, path=str(path), carry=L.carry_enabled(), now_iso_fn=lambda: T1,
                        quote_usd_fn=lambda: {WSOL: (100.0, T1)}, now_ts_fn=lambda: 20.0).cycle()
    assert json.loads(path.read_text())["marks"] == {}


def test_an_odd_quote_token_is_priced_from_its_own_jupiter_usd(tmp_path):
    cache, path = SolanaSwapMarkCache(), tmp_path / "live.json"
    _swap(cache, 100, now_ts=10.4, binding=PoolBinding(MINT, "AmmPool", "cv", "qv", BPXX))
    L.LiveMarkPublisher(cache, path=str(path), now_iso_fn=lambda: T1, now_ts_fn=lambda: 10.4,
                        quote_usd_fn=lambda: {BPXX: (1.5, T0)}).cycle()
    mark = json.loads(path.read_text())["marks"]["solana:%s" % MINT]
    assert mark["price_usd"] == pytest.approx(0.0015) and mark["healthy"] is True
    L.LiveMarkPublisher(cache, path=str(path), now_iso_fn=lambda: T1, now_ts_fn=lambda: 10.4,
                        quote_usd_fn=lambda: {BPXX: (1.5, "2026-09-27T09:58:00+00:00")}).cycle()
    assert json.loads(path.read_text())["marks"]["solana:%s" % MINT]["healthy"] is False


def test_the_refresher_quotes_only_odd_tokens_with_known_decimals_every_period():
    cache = SolanaSwapMarkCache()
    _swap(cache, 100, now_ts=1.0, binding=PoolBinding(MINT, "AmmPool", "cv", "qv", BPXX))
    _swap(cache, 101, now_ts=1.0, binding=PoolBinding("Other", "P2", "cv2", "qv2", WSOL))
    calls, clock, book = [], {"t": 0.0}, L.QuoteBook()
    r = L.ExoticQuoteRefresher(cache, now_iso_fn=lambda: T0, book=book, mono_fn=lambda: clock["t"],
                               price_fn=lambda mint, dec: calls.append((mint, dec)) or (1.53, "ok"))
    assert r.refresh_once() == [BPXX] and calls == [(BPXX, 9)]       # decimals from the stream row
    clock["t"] = 5.0
    assert r.refresh_once() == [] and len(calls) == 1                 # not due for 10 s
    clock["t"] = 10.0
    r.refresh_once()
    assert len(calls) == 2 and book.get()[BPXX] == (1.53, T0)
    assert book.publish("someone_else", BPXX, 9.9, T1) is False       # one writer


def test_a_failed_odd_token_quote_keeps_the_old_value_and_time():
    cache = SolanaSwapMarkCache()
    _swap(cache, 100, now_ts=1.0, binding=PoolBinding(MINT, "AmmPool", "cv", "qv", BPXX))
    book, clock, answers = L.QuoteBook(), {"t": 0.0}, iter([(1.53, "ok"), (None, "error")])
    r = L.ExoticQuoteRefresher(cache, now_iso_fn=lambda: T0 if clock["t"] == 0 else T1, book=book,
                               mono_fn=lambda: clock["t"], price_fn=lambda m, d: next(answers))
    r.refresh_once()
    clock["t"] = 10.0
    r.refresh_once()
    assert book.get()[BPXX] == (1.53, T0)



# --- Alpha review of 1d7c8873: a display quote never touches the funded Jupiter state ----------

def _funded_jupiter_state():
    import copy
    from sniper.execution import solana_real_seams as S
    return (copy.deepcopy(S._jup_host_cooldown), copy.deepcopy(S.JUPITER_LOAD), dict(S._FUNDED_INFLIGHT))


@pytest.mark.parametrize("code", [429, 401])
def test_a_refused_display_quote_leaves_every_funded_jupiter_state_untouched(monkeypatch, code):
    import io
    import urllib.error
    from sniper.execution import solana_real_seams as S
    from sniper.services import memecoin_jupiter_load_sink as sink
    notes = []
    monkeypatch.setattr(sink, "note", lambda *a, **k: notes.append((a, k)))
    monkeypatch.setenv("MEMECOIN_CAPTURE_JUPITER_API_KEY", "test-capture-key")
    # The old path (the funded seam) and the new one (a plain GET) both get the same refusal.
    monkeypatch.setattr(S, "_http_status", lambda *a, **k: (code, {}, None))

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "refused", {}, io.BytesIO(b""))
    monkeypatch.setattr("urllib.request.urlopen", refuse)
    cache = SolanaSwapMarkCache()
    _swap(cache, 100, now_ts=1.0, binding=PoolBinding(MINT, "AmmPool", "cv", "qv", BPXX))
    before = _funded_jupiter_state()
    L.ExoticQuoteRefresher(cache, now_iso_fn=lambda: T0, book=L.QuoteBook()).refresh_once()
    assert _funded_jupiter_state() == before
    assert notes == [], "a display quote must never write the funded load sink"


def test_a_rate_limited_mint_backs_off_a_full_minute():
    cache, clock, calls = SolanaSwapMarkCache(), {"t": 0.0}, []
    _swap(cache, 100, now_ts=1.0, binding=PoolBinding(MINT, "AmmPool", "cv", "qv", BPXX))
    r = L.ExoticQuoteRefresher(cache, now_iso_fn=lambda: T0, book=L.QuoteBook(), mono_fn=lambda: clock["t"],
                               price_fn=lambda m, d: calls.append(m) or (None, "rate_limited"))
    r.refresh_once()
    clock["t"] = 59.0
    r.refresh_once()
    assert len(calls) == 1
    clock["t"] = 60.0
    r.refresh_once()
    assert len(calls) == 2


def test_the_display_quote_needs_the_capture_key_and_never_goes_keyless():
    seen = []
    assert L.jupiter_display_quote(BPXX, 9, env={}, urlopen=lambda *a, **k: seen.append(a)) == (None, "no_key")
    assert seen == []


def test_the_display_quote_sends_the_key_as_a_header_on_the_keyed_host():
    import io
    import json as _json
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=None):
        seen["url"], seen["key"] = req.full_url, req.get_header("X-api-key")
        return Resp(_json.dumps({"outAmount": "1530000"}).encode())

    usd, status = L.jupiter_display_quote(BPXX, 9, env={"MEMECOIN_CAPTURE_JUPITER_API_KEY": "SECRET-TEST-KEY-9"},
                                          urlopen=opener)
    assert (usd, status) == (pytest.approx(1.53), "ok")
    assert seen["url"].startswith("https://api.jup.ag/swap/v1/quote?") and "amount=1000000000" in seen["url"]
    assert seen["key"] == "SECRET-TEST-KEY-9" and "SECRET-TEST-KEY-9" not in seen["url"]


def test_the_display_quote_deadline_is_wall_clock():
    import time as _time

    def hang(req, timeout=None):
        _time.sleep(2.0)
    started = _time.monotonic()
    assert L.jupiter_display_quote(BPXX, 9, env={"MEMECOIN_CAPTURE_JUPITER_API_KEY": "k"}, urlopen=hang,
                                   deadline_s=0.2) == (None, "timeout")
    assert _time.monotonic() - started < 1.0
