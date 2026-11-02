"""4663 display mark off our own swap tape (operator 2026-09-28, relayed by Gamma).

The 4663 "Market price" and chart used DexScreener marks written about every 15 s. On UNIPCS 66359
the exact route read -29.1% at 09:42:58Z while DexScreener read -12.1% eleven seconds later. The
tape sees each swap at sequencer-feed speed; the mark is our entry fill moved by the pool's own
price ratio since our entry transaction, so token decimals cancel. Unknown is absent, never zero.
"""
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes import api_memecoin as api
from sniper.services import memecoin_evm_tape_marks as T
from sniper.services import memecoin_volume_writer as V
from sniper.services.memecoin_position_pool import ensure_position_pool_table
from sniper.settings import memecoin as cfg

CHAIN = "4663"
MINT = "0x" + "cc" * 20
POOL = "0x" + "aa" * 32                    # a V4 pool id: its lag is volume_feed_lag.v4_lag_s
ENTRY_TX = "0x" + "e1" * 32
FILL = 5.0e-05
NOW = 1_790_000_000.0
Q96 = 2 ** 96


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _no_cached_anchor(monkeypatch):
    monkeypatch.setattr(T, "_ANCHORS", {})


def _ledger(path):
    c = sqlite3.connect(str(path), check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    pid = m.open_position(c, source_user_id="u", source_handle="alice", mint=MINT, ticker="T",
                          entry_price=FILL, entry_notional=100.0, entry_fill_source="chain",
                          entry_lag_s=0.5, their_size_usd=500.0, opened_at=_iso(NOW - 60),
                          strategy="alpha_b", execution_mode="real", network_id=CHAIN)
    c.execute("INSERT INTO memecoin_fills(position_id, kind, price, qty, notional, tx_sig,"
              " network_id, created_at) VALUES(?,?,?,?,?,?,?,?)",
              (pid, "entry", FILL, 2e6, 100.0, ENTRY_TX, CHAIN, _iso(NOW - 58)))
    c.commit()
    return c, pid


def _stores(tmp_path, pid, *, ticks=None, quote_side=1, bind=True, feed_age=0.5, lag=(0.5, 0.6),
            gap=False):
    """The real writer's schema. `ticks`: (tx, block, sqrt, observed ts, canonical hash?)."""
    tape_path, bind_path = tmp_path / "volume.db", tmp_path / "ticks.db"
    b = sqlite3.connect(str(bind_path))
    ensure_position_pool_table(b)
    if bind:
        b.execute("INSERT INTO evm_position_pools(position_id, chain_id, mint, pool, venue_kind,"
                  " resolved_at) VALUES(?,?,?,?,?,?)", (pid, CHAIN, MINT, POOL, "v4", _iso(NOW)))
    b.commit(); b.close()
    v = V.connect(str(tape_path))
    v.execute("INSERT INTO pool_meta(chain_id, pool, token0, token1, quote_side, token0_decimals,"
              " token1_decimals, resolved_at) VALUES(?,?,?,?,?,?,?,?)",
              (CHAIN, POOL, MINT, "0x" + "11" * 20, quote_side, None, 18, NOW))
    if ticks is None:
        ticks = [(ENTRY_TX, 100, Q96, NOW - 58, True), ("0x" + "22" * 32, 101, 2 * Q96, NOW - 1, True)]
    for i, (tx, block, sqrt_p, seen, canonical, *liq) in enumerate(ticks, start=1):
        v.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, tx_sig, log_index,"
                  " observed_at, sqrt_price_x96, block_hash, liquidity) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (CHAIN, POOL, "v3_swap", block, tx, i, _iso(seen),
                   None if sqrt_p is None else str(sqrt_p), "0xh%d" % block, liq[0] if liq else None))
        v.execute("INSERT OR REPLACE INTO block_times(chain_id, block_no, block_ts, fetched_at,"
                  " block_hash) VALUES(?,?,?,?,?)",
                  (CHAIN, block, _iso(seen), _iso(seen), "0xh%d" % block if canonical else "0xother"))
    if feed_age is not None:
        v.execute("INSERT INTO volume_feed VALUES(?,?,?,?,?)",
                  (CHAIN, NOW - 300, NOW - feed_age, NOW - feed_age, 10))
    if lag is not None:
        v.execute("INSERT INTO volume_feed_lag (chain_id,v4_lag_s,v3_lag_s,samples,observed) "
                  "VALUES(?,?,?,?,?)", (CHAIN, lag[0], lag[1], 50, NOW - 1))
    if gap:
        v.execute("INSERT INTO tick_gaps(chain_id, from_block, to_block, reason, detected_at)"
                  " VALUES(?,?,?,?,?)", (CHAIN, 90, 95, "test", _iso(NOW)))
    v.commit(); v.close()
    return str(tape_path), str(bind_path)


def _marks(tmp_path, now=NOW, **kw):
    c, pid = _ledger(tmp_path / "ledger.db")
    tape, bind = _stores(tmp_path, pid, **kw)
    return pid, T.tape_marks(c, [{"id": pid, "network_id": CHAIN}], now,
                             tape_path=tape, bind_path=bind)


def test_the_mark_is_our_fill_moved_by_the_pools_own_ratio(tmp_path):
    pid, marks = _marks(tmp_path)
    mark = marks[pid]
    assert mark["price"] == pytest.approx(FILL * 4)        # sqrt price doubled: ratio x4
    assert mark["source"] == T.SOURCE and mark["carried"] is False
    assert mark["observed_at"] == _iso(NOW - 1) and mark["checked_at"]


def test_the_quote_side_sets_the_direction(tmp_path):
    pid, marks = _marks(tmp_path, quote_side=0)
    assert marks[pid]["price"] == pytest.approx(FILL / 4)


@pytest.mark.parametrize("why, kw", [
    ("no pool binding", {"bind": False}),
    ("unknown quote side", {"quote_side": None}),
    ("our entry is not in the tape", {"ticks": [("0x" + "22" * 32, 101, 2 * Q96, NOW - 1, True)]}),
    ("the feed went quiet", {"feed_age": 10.0}),
    ("no feed row", {"feed_age": None}),
    ("no measured protocol lag", {"lag": None}),
    ("the V4 subscription lags", {"lag": (9.0, 0.5)}),
    ("an unresolved tick gap", {"gap": True}),
    ("no sqrt price (Pons, V2)", {"ticks": [(ENTRY_TX, 100, None, NOW - 58, True)]}),
])
def test_unknown_is_absent_never_zero(tmp_path, why, kw):
    pid, marks = _marks(tmp_path, **kw)
    assert pid not in marks, why


def test_an_orphaned_tick_never_moves_the_mark(tmp_path):
    pid, marks = _marks(tmp_path, ticks=[
        (ENTRY_TX, 100, Q96, NOW - 58, True), ("0x" + "22" * 32, 101, 2 * Q96, NOW - 2, True),
        ("0x" + "33" * 32, 102, 3 * Q96, NOW - 1, False)])     # hash not the canonical header
    assert marks[pid]["price"] == pytest.approx(FILL * 4)


def test_a_quiet_pool_keeps_its_last_price_and_says_so(tmp_path):
    pid, marks = _marks(tmp_path, now=NOW + 3, feed_age=-2.5)   # feed fresh, last swap 4 s ago
    assert marks[pid]["carried"] is False
    T._ANCHORS.clear()
    (tmp_path / "later").mkdir()
    pid, marks = _marks(tmp_path / "later", now=NOW + 30, feed_age=-29.5)
    assert marks[pid]["carried"] is True and marks[pid]["price"] == pytest.approx(FILL * 4)


def test_a_missing_store_is_empty_not_an_error(tmp_path):
    c, pid = _ledger(tmp_path / "ledger.db")
    assert T.tape_marks(c, [{"id": pid, "network_id": CHAIN}], NOW,
                        tape_path=str(tmp_path / "none.db"), bind_path=str(tmp_path / "none2.db")) == {}


def test_the_flag_turns_it_off(tmp_path):
    c, pid = _ledger(tmp_path / "ledger.db")
    tape, bind = _stores(tmp_path, pid)
    assert T.tape_marks(c, [{"id": pid, "network_id": CHAIN}], NOW, tape_path=tape, bind_path=bind,
                        env={"MEMECOIN_EVM_TAPE_DISPLAY": "0"}) == {}


def _client(monkeypatch, tmp_path, *, tape=True, exact_age=2):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_KEYPAIR", str(tmp_path / "nope_sol.json"))
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", str(tmp_path / "nope_evm.json"))
    c, pid = _ledger(tmp_path / "ledger.db")
    paths = _stores(tmp_path, pid, feed_age=0.5 if tape else 60.0)
    monkeypatch.setattr(T, "_paths", lambda *_a: paths)
    monkeypatch.setattr(api, "_now_iso", lambda: _iso(NOW))
    for ts, mid, source, quality in ((NOW - 20, FILL * 1.5, "fastlane_mid", "live"),
                                     (NOW - exact_age, FILL * 3.5, "fastlane_rpc", "exact_route")):
        c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality)"
                  " VALUES(?,?,?,?,?,?)", (CHAIN, MINT, _iso(ts), mid, source, quality))
    c.commit()
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    from _web_csrf import csrf_client
    return csrf_client(app)


def _ticker_row(cl):
    rows = cl.get("/api/memecoin/real/ticker").get_json()
    return (rows.get("positions", rows) if isinstance(rows, dict) else rows)[0]


def test_the_ticker_market_price_is_the_tape_not_the_provider(monkeypatch, tmp_path):
    row = _ticker_row(_client(monkeypatch, tmp_path))
    assert row["market_mid_source"] == T.SOURCE, row
    assert row["market_mid_price"] == pytest.approx(FILL * 4)
    assert row["market_mid_carried"] is False and row["market_mid_fetched_at"]


def test_without_a_healthy_tape_the_market_price_is_the_route_never_the_provider(monkeypatch, tmp_path):
    """2026-10-08 (operator via Alpha, Douyin 72229): without our tape a funded EVM card shows the
    exact route price, labelled, or nothing; the DexScreener mid (fastlane_mid) is never shown."""
    row = _ticker_row(_client(monkeypatch, tmp_path, tape=False))
    assert row["market_mid_source"] is None, row
    assert row["market_mid_basis"] in ("route", None), row
    assert row["market_mid_price"] != pytest.approx(FILL * 1.5)


def test_the_chart_draws_the_tape_beside_the_route_line(monkeypatch, tmp_path):
    d = _client(monkeypatch, tmp_path).get("/api/memecoin/marks/%s/%s" % (CHAIN, MINT)).get_json()
    assert {p["source"] for p in d["points"]} == {"fastlane_rpc", T.SOURCE}, d["points"]
    assert d["last"] == pytest.approx(FILL * 4) and d["source"] == T.SOURCE


def test_the_ui_names_the_source():
    app = Path("static/app.js").read_text()
    assert "onchain_tape_entry_anchor: 'on-chain tape'" in app


def test_a_long_busy_hold_is_thinned_across_the_whole_hold(tmp_path):
    ticks = [(ENTRY_TX, 100, Q96, NOW - 900, True)] + [
        ("0x%064x" % i, 100 + i, Q96 + i, NOW - 900 + i, True) for i in range(1, 50)]
    c, pid = _ledger(tmp_path / "ledger.db")
    tape, bind = _stores(tmp_path, pid, ticks=ticks)
    pts = T.tape_series(c, pid, max_points=10, tape_path=tape, bind_path=bind)
    assert 10 <= len(pts) <= 11 and pts[0]["t"] <= _iso(NOW - 890), [p["t"] for p in pts]
    assert pts[-1]["t"] == _iso(NOW - 900 + 49), "the newest swap is always kept"


@pytest.mark.parametrize("exact_age", [2, 400])      # fresh route; stale route (the mid fallback)
def test_the_tape_never_becomes_the_sell_value(monkeypatch, tmp_path, exact_age):
    """Alpha 2026-09-28: the tape is spot, ~6 points over the sell quote on 66384. Every sell-value,
    PnL and range field must be identical with and without it; only the market price moves."""
    (tmp_path / "on").mkdir(); (tmp_path / "off").mkdir()
    on = _ticker_row(_client(monkeypatch, tmp_path / "on", exact_age=exact_age))
    off = _ticker_row(_client(monkeypatch, tmp_path / "off", tape=False, exact_age=exact_age))
    # 2026-10-08: without the tape there is no provider mid; the card falls back to the route label.
    assert on["market_mid_source"] == T.SOURCE and off["market_mid_source"] is None
    moved = {k for k in on if on[k] != off[k]}
    assert moved <= {"market_mid_price", "market_mid_at", "market_mid_source", "market_mid_fetched_at",
                     "market_mid_carried", "market_mid_basis", "market_cap", "market_cap_fdv",
                     "last_known_price", "last_known_at", "last_known_source"}, sorted(moved)
    for field in ("selected_exit_price", "pnl_pct", "last_quote_price", "mark_basis"):
        assert on[field] == off[field], field


# ---------------------------------------------------------------- the exit dust guard's own mark

def _exit_mark(tmp_path, now=NOW, **kw):
    c, pid = _ledger(tmp_path / "ledger.db")
    tape, bind = _stores(tmp_path, pid, **kw)
    return T.exit_mark(c, pid, now, tape_path=tape, bind_path=bind)


LIVE = [(ENTRY_TX, 100, Q96, NOW - 58, True, "900"), ("0x" + "22" * 32, 101, 2 * Q96, NOW - 1, True, "700")]


def test_the_exit_mark_is_the_display_mark_on_a_pool_with_liquidity(tmp_path):
    mark = _exit_mark(tmp_path, ticks=LIVE)
    assert mark["price"] == pytest.approx(FILL * 4) and mark["source"] == T.SOURCE
    assert "liquidity" not in mark


def test_the_display_switch_never_turns_the_exit_mark_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_TAPE_DISPLAY", "0")
    assert _exit_mark(tmp_path, ticks=LIVE)["price"] == pytest.approx(FILL * 4)


@pytest.mark.parametrize("why, kw", [
    ("the newest tick printed L=0 (pulled pool)", {"ticks": LIVE[:1] + [LIVE[1][:5] + ("0",)]}),
    ("the newest tick has no liquidity", {"ticks": LIVE[:1] + [LIVE[1][:5]]}),
    ("an unreadable liquidity", {"ticks": LIVE[:1] + [LIVE[1][:5] + ("x",)]}),
    ("no pool binding", {"ticks": LIVE, "bind": False}),
    ("our entry aged out of the tape", {"ticks": LIVE[1:]}),
    ("the feed went quiet", {"ticks": LIVE, "feed_age": 10.0}),
    ("an unresolved tick gap", {"ticks": LIVE, "gap": True}),
])
def test_the_exit_mark_is_absent_whenever_anything_is_unknown(tmp_path, why, kw):
    assert _exit_mark(tmp_path, **kw) is None, why


def test_a_missing_store_is_no_mark_never_an_error(tmp_path):
    c, pid = _ledger(tmp_path / "ledger.db")
    assert T.exit_mark(c, pid, NOW, tape_path=tmp_path / "nope.db", bind_path=tmp_path / "nope2.db") is None
