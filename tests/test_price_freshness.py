"""A market price says when it last CHANGED and when it was last CHECKED; a stream says it is alive.

docs/audits/2026-09-26-price-freshness.md (Codex, 23:22Z): position 65860's Market price sat at
0.000004103 stamped 23:21:21Z for two minutes while the provider was being read every second. The
stamp is the first observation of unchanged values (MidObservationState), so the screen could not
tell a quiet market from a dead provider. And the dashboard trusted an EventSource that was merely
OPEN: a stream that stops delivering suppressed the fallback poll forever.

Items 2-4 only. The mark tape's own stamp (memecoin_position_marks.sweep_ts, read by vol_death and
the rails) must not change; fetch health travels separately through the runner heartbeat.
"""
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

import sniper.routes.api_memecoin as A
import sniper.services.memecoin_fastlane as fl
from sniper.persistence import db_memecoin as m
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)

APP = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


# --- (2) fetch health beside value change ----------------------------------------------------

def test_an_unchanged_fetch_advances_the_check_and_not_the_change():
    st = fl.MidObservationState()
    key = ("solana", "M1")
    stats = {"price": 1.0, "liquidity": 2.0, "mcap": 3.0}
    assert st.observe(key, stats, "2026-09-26T23:00:00+00:00") == "2026-09-26T23:00:00+00:00"
    assert st.observe(key, stats, "2026-09-26T23:00:01+00:00") == "2026-09-26T23:00:00+00:00"
    snap = st.snapshot(now_iso="2026-09-26T23:00:02+00:00")
    assert snap["solana:M1"] == {"changed_at": "2026-09-26T23:00:00+00:00",
                                 "fetched_at": "2026-09-26T23:00:01+00:00"}


def test_a_key_not_fetched_recently_leaves_the_snapshot():
    """A failed batch never calls observe, so its key simply stops being checked; after ten
    minutes it is not reported as checked at all, and after an hour it is forgotten."""
    st = fl.MidObservationState()
    st.observe(("solana", "OLD"), {"price": 1.0}, "2026-09-26T22:00:00+00:00")
    st.observe(("solana", "NEW"), {"price": 1.0}, "2026-09-26T23:00:00+00:00")
    snap = st.snapshot(now_iso="2026-09-26T23:05:00+00:00")
    assert set(snap) == {"solana:NEW"}
    assert ("solana", "OLD") not in st._fetched


def _open_real(c, mint="MintF"):
    from sniper.settings import memecoin as cfg
    return m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="F",
        entry_price=0.001, entry_notional=50.0, entry_fill_source="chain", entry_lag_s=-1.0,
        their_size_usd=0.0, opened_at="2026-09-26T23:00:00+00:00",
        strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "pf.db"))
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, quality) "
              "VALUES('solana','MintF','2026-09-26T23:00:00Z',0.002,'fastlane_mid','live')")
    c.commit()
    return c


def test_the_ticker_carries_the_check_time_beside_the_change_time(conn, tmp_path, monkeypatch):
    # On Base: a Solana market price never comes from DexScreener since 2026-09-27 (operator), see
    # tests/test_memecoin_live_marks.py. The changed/checked pair still labels DexScreener elsewhere.
    _open_real(conn)
    conn.execute("UPDATE memecoin_positions SET network_id='8453'")
    conn.execute("UPDATE memecoin_position_marks SET chain_id='8453'")
    beat = tmp_path / "hb.json"
    beat.write_text(json.dumps({"fastlane": {"mid_observations": {"8453:MintF": {
        "changed_at": "2026-09-26T23:00:00+00:00", "fetched_at": "2026-09-26T23:01:59+00:00"}}}}))
    monkeypatch.setattr(A, "HEARTBEAT_PATH", str(beat))
    row = A._real_ticker_payload(conn, "2026-09-26T23:02:00+00:00")["positions"][0]
    # 2026-10-08 (operator via Alpha): a DexScreener mid (fastlane_mid) no longer prices a funded
    # EVM card at all, so there is no change/check pair to show for it.
    assert row["market_mid_price"] is None and row["market_mid_source"] is None
    assert row["market_mid_fetched_at"] is None
    assert row["market_mid_source_at"] is None


def test_no_heartbeat_means_the_check_time_is_unknown_never_now(conn, tmp_path, monkeypatch):
    _open_real(conn)
    monkeypatch.setattr(A, "HEARTBEAT_PATH", str(tmp_path / "missing.json"))
    row = A._real_ticker_payload(conn, "2026-09-26T23:02:00+00:00")["positions"][0]
    assert row["market_mid_fetched_at"] is None


# --- (3) cards and table show both ages ------------------------------------------------------

def _node(script):
    node = shutil.which("node")
    assert node
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                     check=True).stdout)


def _method(name, end):
    return APP[APP.index(f"    {name}("):APP.index(f"    {end}(")]


def test_the_table_cell_and_the_card_line_show_changed_and_checked_ages():
    body = (_method("_marketMidCell", "_mergeLastKnown") + _method("_realTickerRows", "_cardSig")
            + _method("_operatorQuote", "_marketSourceLabel") + _method("_marketSourceLabel", "_marketMidCell")
            + _method("_supplyOf", "_fmtAgo"))
    p = {"market_mid_price": 0.000004103, "market_mid_basis": "market", "market_mid_source": "fastlane_mid",
         "market_mid_at": "2026-09-26T23:21:21Z", "market_mid_fetched_at": "2026-09-26T23:23:20Z"}
    script = ("const esc = s => String(s); const view = { _operatorPrice(v) { return String(v); },"
              " _operatorTime(v) { return v; }, _quoteAge(v) { return 'AGE(' + v + ')'; },"
              " _operatorValueRows() { return ''; }, _realRangeBar() { return ''; }, _realFactsLine() { return ''; }, "
              + body + "}; const p = " + json.dumps(p) + ";"
              " console.log(JSON.stringify([view._marketMidCell(p.market_mid_price, p.market_mid_basis,"
              " p.market_mid_source, p.market_mid_at, p.market_mid_fetched_at), view._realTickerRows(p)]));")
    cell, card = _node(script)
    for html in (cell, card):
        assert "changed" in html and "AGE(2026-09-26T23:21:21Z)" in html, html
        assert "checked" in html and "AGE(2026-09-26T23:23:20Z)" in html, html
        assert 'data-observation-at="2026-09-26T23:23:20Z"' in html        # the age ticker updates it


def test_the_card_signature_ignores_the_check_stamp():
    """It moves every second; if the signature saw it, every card would rebuild every second."""
    body = _method("_cardSig", "_realCard")
    script = ("const view = {" + body + "}; const a = {id: 1, market_mid_fetched_at: 't1'};"
              " const b = {id: 1, market_mid_fetched_at: 't2'};"
              " console.log(JSON.stringify(view._cardSig(a) === view._cardSig(b)));")
    assert _node(script) is True


# --- (4) the stream proves it is alive -------------------------------------------------------

def _stream_chunks(monkeypatch, tmp_path, n, fail=False):
    from flask import Flask
    import sniper.persistence.db_core as db_core
    c = sqlite3.connect(str(tmp_path / "s.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: c)
    clock = {"t": 0.0}

    def _mono():
        clock["t"] += 0.5
        return clock["t"]

    patch_local(monkeypatch, "monotonic", _mono)
    patch_local(monkeypatch, "sleep", lambda s: None)
    monkeypatch.setattr(A, "_real_ticker_payload", (lambda *a: (_ for _ in ()).throw(
        sqlite3.OperationalError("locked"))) if fail else (lambda *a: {"positions": []}))
    monkeypatch.setattr(A, "_cockpit_change_signature", lambda sc: ((0, 0, 0, 0),))
    app = Flask(__name__)
    app.register_blueprint(A.create_api_memecoin_blueprint({"conn": c}))
    resp = app.test_client().get("/api/memecoin/real/stream", buffered=False)
    out = []
    for chunk in resp.response:
        out.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        if len(out) >= n:
            break
    resp.close()
    return out


def test_a_healthy_stream_sends_heartbeat_events(monkeypatch, tmp_path):
    chunks = _stream_chunks(monkeypatch, tmp_path, 12)
    beats = [c for c in chunks if c.startswith("event: heartbeat\n")]
    assert beats, chunks
    assert "server_time" in json.loads(beats[0].split("data: ", 1)[1])


def test_a_stream_whose_ticks_fail_sends_no_heartbeat(monkeypatch, tmp_path):
    """The server catches a SQLite error and keeps the socket open; the heartbeat is what tells
    the browser the data behind it stopped. A comment keepalive never reaches JavaScript."""
    chunks = _stream_chunks(monkeypatch, tmp_path, 4, fail=True)
    assert chunks and not any(c.startswith("event: heartbeat") for c in chunks), chunks


def test_an_open_stream_with_no_recent_message_is_not_healthy():
    body = _method("_realStreamHealthy", "_ensureRealStream")
    script = ("const view = {" + body + "}; const now = performance.now();"
              " view._realStream = {readyState: 1};"
              " view._realStreamLastMsgAt = now; const fresh = view._realStreamHealthy();"
              " view._realStreamLastMsgAt = now - 20000; const stale = view._realStreamHealthy();"
              " view._realStreamLastMsgAt = undefined; const never = view._realStreamHealthy();"
              " view._realStream = {readyState: 0}; view._realStreamLastMsgAt = now;"
              " const connecting = view._realStreamHealthy();"
              " console.log(JSON.stringify([fresh, stale, never, connecting]));")
    assert _node(script) == [True, False, False, False]


def test_every_stream_event_including_heartbeat_marks_the_stream_alive():
    body = _method("_ensureRealStream", "_notifyFill")
    script = ("const listeners = {}; global.EventSource = function () { this.readyState = 1;"
              " this.addEventListener = (n, f) => { listeners[n] = f; }; this.close = () => {}; };"
              " global.document = {hidden: false};"
              " const view = { _applyRealTicker() {}, _loadEntries() {}, _loadEvents() {}, _notifyFill() {}, "
              + body + "}; view._ensureRealStream(); const out = {};"
              " for (const name of ['heartbeat', 'ticker', 'cockpit', 'activity', 'fill']) {"
              "   view._realStreamLastMsgAt = -1;"
              "   try { listeners[name]({data: name === 'ticker' ? '{}' : '{}'}); } catch (e) {}"
              "   out[name] = view._realStreamLastMsgAt > 0; }"
              " console.log(JSON.stringify(out));")
    assert _node(script) == {"heartbeat": True, "ticker": True, "cockpit": True,
                             "activity": True, "fill": True}
