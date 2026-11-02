"""Capture coordinator: per-token CLOB dispatch, gap detection, snapshot persistence, Phase-0 report."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m import capture as C
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m import report as R

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")
CODE_V = "test-1"


def _spec(name="event_resolved_up.json"):
    with open(os.path.join(FX, name)) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


def _coord():
    return C.CaptureCoordinator(_spec(), stale_book_ms=3000)


def test_clob_message_routes_to_correct_token_book():
    co = _coord()
    spec = co.spec
    msg = {"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1000,
           "bids": [["0.30", "100"]], "asks": [["0.40", "100"]]}
    res = co.ingest_clob(msg, recv_ts=1001)
    assert res["token_id"] == spec.up_token_id and res["side"] == "Up"
    assert "token_unmapped" not in res["flags"]
    assert co.book_for(spec.up_token_id).best_bid() == 0.30
    # the Down book is untouched (no complement inference)
    assert co.book_for(spec.down_token_id).best_bid() is None


def test_unmapped_token_flagged_and_not_applied():
    co = _coord()
    res = co.ingest_clob({"event_type": "book", "asset_id": "99999", "timestamp": 1,
                          "bids": [["0.3", "1"]], "asks": []}, recv_ts=2)
    assert "token_unmapped" in res["flags"]


def test_timestamp_regression_flagged():
    co = _coord()
    spec = co.spec
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 5000,
                    "bids": [["0.3", "1"]], "asks": [["0.4", "1"]]}, recv_ts=1)
    res = co.ingest_clob({"event_type": "price_change", "market": "0xm",
                          "timestamp": 4000,  # older than the last applied
                          "price_changes": [{"asset_id": spec.up_token_id, "price": "0.31",
                                             "size": "5", "side": "BUY"}]},
                         recv_ts=2)
    assert "timestamp_regression" in res["flags"]


def test_price_change_routes_by_nested_asset_id():
    # Real Polymarket shape: price_change has NO top-level asset_id — each price_changes[] item
    # carries its own asset_id (docs.polymarket.com/market-data/websocket/market-channel).
    co = _coord()
    spec = co.spec
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1000,
                    "bids": [["0.30", "100"]], "asks": [["0.40", "100"]]}, recv_ts=1001)
    res = co.ingest_clob({"event_type": "price_change", "market": "0xm", "timestamp": 1002,
                          "price_changes": [{"asset_id": spec.up_token_id, "price": "0.31",
                                             "size": "7", "side": "BUY"}]}, recv_ts=1003)
    assert co.book_for(spec.up_token_id).best_bid() == 0.31        # applied to the Up book
    persist = [e for e in res["events"] if e["token_id"]]
    assert len(persist) == 1
    assert persist[0]["token_id"] == spec.up_token_id and persist[0]["side"] == "Up"
    assert persist[0]["event_type"] == "price_change"


def test_price_change_size_zero_removes_level():
    # size="0" means the level was removed (cancellation) — the missing cancel stream.
    co = _coord()
    spec = co.spec
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1000,
                    "bids": [["0.30", "100"], ["0.29", "50"]], "asks": [["0.40", "100"]]},
                   recv_ts=1001)
    co.ingest_clob({"event_type": "price_change", "market": "0xm", "timestamp": 1002,
                    "price_changes": [{"asset_id": spec.up_token_id, "price": "0.30",
                                       "size": "0", "side": "BUY"}]}, recv_ts=1003)
    assert co.book_for(spec.up_token_id).best_bid() == 0.29        # 0.30 level removed


def test_price_change_mixed_tokens_route_without_crosstalk():
    # One price_change frame can carry changes for BOTH tokens; each must hit its own book only.
    co = _coord()
    spec = co.spec
    for tid in (spec.up_token_id, spec.down_token_id):
        co.ingest_clob({"event_type": "book", "asset_id": tid, "timestamp": 1000,
                        "bids": [["0.10", "1"]], "asks": [["0.90", "1"]]}, recv_ts=1001)
    res = co.ingest_clob({"event_type": "price_change", "market": "0xm", "timestamp": 1002,
                          "price_changes": [
                              {"asset_id": spec.up_token_id, "price": "0.31", "size": "7", "side": "BUY"},
                              {"asset_id": spec.down_token_id, "price": "0.62", "size": "9", "side": "BUY"}]},
                         recv_ts=1003)
    assert co.book_for(spec.up_token_id).best_bid() == 0.31
    assert co.book_for(spec.down_token_id).best_bid() == 0.62
    persist = [e for e in res["events"] if e["token_id"]]
    assert {e["token_id"] for e in persist} == {spec.up_token_id, spec.down_token_id}
    # each persist record carries the per-level payload (one row per changed level)
    assert all("price" in e["payload"] and "asset_id" in e["payload"] for e in persist)


def _load_cli():
    import importlib.util
    cli_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                            "run_polymarket_crypto5m_capture.py")
    spec_mod = importlib.util.spec_from_file_location("c5m_cli_persist", cli_path)
    cli = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(cli)
    return cli


def test_persist_book_events_writes_one_row_per_price_change_level(tmp_path):
    cli = _load_cli()
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    co = _coord()
    spec = co.spec
    for tid in (spec.up_token_id, spec.down_token_id):
        co.ingest_clob({"event_type": "book", "asset_id": tid, "timestamp": 1000,
                        "bids": [["0.10", "1"]], "asks": [["0.90", "1"]]}, recv_ts=1001)
    res = co.ingest_clob({"event_type": "price_change", "market": "0xm", "timestamp": 1002,
                          "price_changes": [
                              {"asset_id": spec.up_token_id, "price": "0.31", "size": "7", "side": "BUY"},
                              {"asset_id": spec.down_token_id, "price": "0.62", "size": "9", "side": "BUY"}]},
                         recv_ts=1003)
    cli._persist_book_events(conn, co, spec, res, now_fn=lambda: 1003)
    rows = conn.execute("SELECT token_id, side, event_type, raw_payload FROM crypto5m_book_events "
                        "WHERE event_type='price_change' ORDER BY token_id").fetchall()
    assert len(rows) == 2                                   # one row per changed level
    assert {r["token_id"] for r in rows} == {spec.up_token_id, spec.down_token_id}
    payloads = [json.loads(r["raw_payload"]) for r in rows]
    assert all("asset_id" in p and "price" in p for p in payloads)   # per-level payload persisted


def test_persist_book_events_same_token_multi_level_no_key_collision(tmp_path):
    # Two level changes on the SAME token in one frame share (token, event_type, exchange_ts). They
    # must still produce two distinct rows — event_key hashes the per-level payload, so they don't
    # collide under INSERT OR IGNORE. Guards against a future "one payload per frame" regression.
    cli = _load_cli()
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    co = _coord()
    spec = co.spec
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1000,
                    "bids": [["0.10", "1"]], "asks": [["0.90", "1"]]}, recv_ts=1001)
    res = co.ingest_clob({"event_type": "price_change", "market": "0xm", "timestamp": 1002,
                          "price_changes": [
                              {"asset_id": spec.up_token_id, "price": "0.31", "size": "7", "side": "BUY"},
                              {"asset_id": spec.up_token_id, "price": "0.29", "size": "4", "side": "BUY"}]},
                         recv_ts=1003)
    cli._persist_book_events(conn, co, spec, res, now_fn=lambda: 1003)
    rows = conn.execute("SELECT event_key FROM crypto5m_book_events "
                        "WHERE event_type='price_change'").fetchall()
    assert len(rows) == 2                                   # both levels recorded
    assert len({r["event_key"] for r in rows}) == 2         # distinct keys, no collision


def test_persist_book_events_skips_unmapped(tmp_path):
    cli = _load_cli()
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    co = _coord()
    res = co.ingest_clob({"event_type": "book", "asset_id": "99999", "timestamp": 1,
                          "bids": [["0.3", "1"]], "asks": []}, recv_ts=2)
    # unmapped token has a truthy foreign id -> current behaviour still records it with the flag
    cli._persist_book_events(conn, co, co.spec, res, now_fn=lambda: 2)
    n = conn.execute("SELECT COUNT(*) FROM crypto5m_book_events WHERE token_id='99999'").fetchone()[0]
    assert n == 1


def test_reconnect_generation_increments():
    co = _coord()
    assert co.reconnect_generation == 0
    assert co.mark_reconnect() == 1
    assert co.reconnect_generation == 1


def test_is_stale_and_stale_tokens():
    co = _coord()
    spec = co.spec
    # never-seen book is stale by definition
    assert co.is_stale(spec.up_token_id, now=1000, max_gap=3) is True
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1,
                    "bids": [["0.3", "1"]], "asks": [["0.4", "1"]]}, recv_ts=1000)
    assert co.is_stale(spec.up_token_id, now=1002, max_gap=3) is False   # 2s <= 3s
    assert co.is_stale(spec.up_token_id, now=1010, max_gap=3) is True    # 10s > 3s
    # the un-updated Down book is also stale -> both surface
    stale = co.stale_tokens(now=1010, max_gap=3)
    assert spec.up_token_id in stale and spec.down_token_id in stale


def test_persist_snapshot_writes_best_and_levels(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    co = _coord()
    spec = co.spec
    co.ingest_clob({"event_type": "book", "asset_id": spec.up_token_id, "timestamp": 1000,
                    "bids": [["0.30", "100"], ["0.29", "50"]], "asks": [["0.40", "100"]]},
                   recv_ts=1001)
    sid = co.persist_snapshot(conn, spec.up_token_id, code_version=CODE_V, source_event="book",
                              recv_ts=1001, now_ts=1001)
    row = conn.execute("SELECT * FROM crypto5m_book_snapshots WHERE id=?", (sid,)).fetchone()
    assert row["best_bid"] == 0.30 and row["best_ask"] == 0.40
    assert json.loads(row["bids"])[0] == [0.30, 100.0]   # sorted high->low


def test_report_summarizes_a_populated_db(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    spec = _spec()
    P.upsert_market(conn, spec, code_version=CODE_V, source_lineage="gamma", now_ts=1)
    from sniper.algos.polymarket_crypto5m import settlement as S
    st = S.from_gamma(spec)   # event_resolved_up -> winner Up -> genuinely resolved
    P.record_settlement(conn, st, code_version=CODE_V, now_ts=2)
    rep = R.build_report(conn)
    assert rep["markets"]["total"] == 1
    assert rep["settlements"]["total"] == 1
    assert rep["settlements"]["resolved"] == 1
    assert "blockers" in rep
    text = R.format_report(rep)
    assert "Phase 0" in text and "markets" in text.lower()


def test_run_continuous_drives_capture_and_reconcile(tmp_path, monkeypatch):
    # regression for the deadlock: the driver must move a DISCOVERED window through
    # capturing -> awaiting -> reconciled (it previously only ever called start_capture on already-live
    # windows, so it captured nothing and looped forever). No network.
    import importlib.util
    cli_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                            "run_polymarket_crypto5m_capture.py")
    spec_mod = importlib.util.spec_from_file_location("c5m_cli2", cli_path)
    cli = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(cli)

    spec = _spec()  # window_end 1781947500
    captured = []
    raw = json.load(open(os.path.join(FX, "event_resolved_up.json")))
    monkeypatch.setattr(cli, "server_now", lambda: 1781947501)   # past window_end
    monkeypatch.setattr(cli, "discover_current", lambda now, **k: [spec])
    monkeypatch.setattr(cli, "capture_window", lambda *a, **k: captured.append(a[0].slug))
    monkeypatch.setattr(cli, "_get_json", lambda url: [raw])      # reconcile resolves to Up

    conn = P.connect(os.path.join(str(tmp_path), "cont.sqlite"))
    P.ensure_schema(conn)
    n = cli.run_continuous(conn, max_windows=1, discovery_interval_s=1, settlement_timeout_s=600,
                           capture_duration=1, stale_book_ms=3000, rest_reconcile_s=30)
    assert n == 1                                # terminated, not deadlocked
    assert captured == [spec.slug]               # the window was actually captured
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_settlements WHERE winning_outcome='Up'"
                        ).fetchone()[0] == 1     # and reconciled


def test_discover_current_gamma_fetch_failure_returns_no_markets(monkeypatch):
    # Gamma outages/refusals must not kill the long-running capture process; the watchdog should not
    # thrash just because one discovery poll failed.
    import importlib.util
    cli_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                            "run_polymarket_crypto5m_capture.py")
    spec_mod = importlib.util.spec_from_file_location("c5m_cli_gamma_fail", cli_path)
    cli = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(cli)

    def _refused(url):
        raise OSError("connection refused")

    monkeypatch.setattr(cli, "_get_json", _refused)

    assert cli.discover_current(1783247400, max_markets=2, fixture_mode=False) == []


def test_continuous_capture_covers_full_window_not_fixed_slice():
    # Codex #2: per-window capture must run until the window CLOSES, not a fixed 60s slice that misses
    # most of a 300s window.
    import importlib.util
    import types
    cli_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                            "run_polymarket_crypto5m_capture.py")
    spec_mod = importlib.util.spec_from_file_location("c5m_cli3", cli_path)
    cli = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(cli)
    win = types.SimpleNamespace(window_end_ts=1000)
    assert cli._window_capture_seconds(win, 700, cap=None) == 300    # full remaining window, NOT 60
    assert cli._window_capture_seconds(win, 990, cap=None) == 10
    assert cli._window_capture_seconds(win, 1000, cap=None) == 0     # closed
    assert cli._window_capture_seconds(win, 1100, cap=None) == 0     # past end -> no negative
    assert cli._window_capture_seconds(win, 700, cap=120) == 120     # explicit cap honored


def test_fixture_mode_implies_discover_only_and_touches_no_network(tmp_path, monkeypatch):
    import importlib.util
    cli_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                            "run_polymarket_crypto5m_capture.py")
    spec = importlib.util.spec_from_file_location("c5m_cli", cli_path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    # tripwires: any network path must explode -> proves fixture mode never runs the live loop
    def _boom(*a, **k):
        raise AssertionError("fixture mode hit the network")
    monkeypatch.setattr(cli, "server_now", _boom)
    monkeypatch.setattr(cli, "_get_json", _boom)
    monkeypatch.setattr(cli, "binance_btc_price", _boom)
    rc = cli.main(["--fixture-mode", "--db-path", os.path.join(str(tmp_path), "f.sqlite")])
    assert rc == 0   # ran discover/report off local fixtures, no capture_window, no network


def test_report_does_not_count_unresolved_or_conflict_as_resolved(tmp_path):
    # the CLI writes from_gamma() unconditionally, so an active window lands a row with winner=None;
    # that must NOT make the report stop warning "no resolved windows".
    conn = P.connect(os.path.join(str(tmp_path), "c.sqlite"))
    P.ensure_schema(conn)
    from sniper.algos.polymarket_crypto5m.models import Settlement
    P.record_settlement(conn, Settlement("0xun", None, None, None, None, (), "gamma", "unresolved",
                                         None), code_version=CODE_V, now_ts=1)
    P.record_settlement(conn, Settlement("0xcf", None, None, None, "resolved", (), "gamma+websocket",
                                         "conflict", 100.0), code_version=CODE_V, now_ts=2)
    rep = R.build_report(conn)
    assert rep["settlements"]["total"] == 2
    assert rep["settlements"]["resolved"] == 0          # neither counts as resolved
    assert rep["settlements"]["conflicts"] == 1         # the conflict is counted separately
    assert rep["settlements"]["unresolved"] == 1        # only the genuinely-unresolved row
    assert any("resolved" in b.lower() for b in rep["blockers"])
