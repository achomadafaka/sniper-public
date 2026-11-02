"""Phase 1B review fix (Codex #7): the capture script's REST resnapshot must MEASURE book divergence
(compare reconstructed vs REST, persist crypto5m_book_reconciliation, quarantine on divergence) instead
of silently overwriting the reconstructed book."""
import importlib.util
import json
import os
import threading

from sniper.algos.polymarket_crypto5m import capture as C
from sniper.algos.polymarket_crypto5m import market_resolver as MR
from sniper.algos.polymarket_crypto5m import persistence as P

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def _cli():
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts",
                        "run_polymarket_crypto5m_capture.py")
    spec_mod = importlib.util.spec_from_file_location("c5m_cli_recon", path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    return mod


def _spec():
    with open(os.path.join(FX, "event_resolved_up.json")) as f:
        spec, _ = MR.parse_event(json.load(f))
    return spec


def _conn(tmp_path, name="recon.sqlite"):
    conn = P.connect(os.path.join(str(tmp_path), name))
    P.ensure_schema(conn)
    return conn


def _seed_coord(spec, tok, bids, asks):
    coord = C.CaptureCoordinator(spec, stale_book_ms=3000)
    coord.ingest_clob({"event_type": "book", "asset_id": tok, "timestamp": 1, "bids": bids,
                       "asks": asks}, recv_ts=1)
    return coord


def _recon_row(conn, tok):
    return conn.execute(
        "SELECT book_state_match, book_divergence_levels, quarantined_interval "
        "FROM crypto5m_book_reconciliation WHERE token_id=?", (tok,)).fetchone()


def test_rest_reconcile_records_divergence_and_quarantines(tmp_path, monkeypatch):
    cli = _cli()
    conn, spec = _conn(tmp_path), _spec()
    tok = spec.up_token_id
    coord = _seed_coord(spec, tok, [["0.40", "100"]], [["0.60", "100"]])
    monkeypatch.setattr(cli, "_get_json",
                        lambda url: {"bids": [["0.30", "100"]], "asks": [["0.70", "100"]], "timestamp": 2})
    monkeypatch.setattr(cli, "server_now_cached", lambda: 100)
    cli._rest_snapshot(conn, threading.Lock(), coord, spec, tok, source_event="rest_reconcile", flags=[])
    row = _recon_row(conn, tok)
    assert row is not None
    assert row[0] == 0           # not a match
    assert row[1] > 0            # divergence levels recorded
    assert row[2] == 1           # interval quarantined


def test_rest_reconcile_records_match_when_books_agree(tmp_path, monkeypatch):
    cli = _cli()
    conn, spec = _conn(tmp_path), _spec()
    tok = spec.up_token_id
    coord = _seed_coord(spec, tok, [["0.40", "100"]], [["0.60", "100"]])
    monkeypatch.setattr(cli, "_get_json",
                        lambda url: {"bids": [["0.40", "100"]], "asks": [["0.60", "100"]], "timestamp": 2})
    monkeypatch.setattr(cli, "server_now_cached", lambda: 100)
    cli._rest_snapshot(conn, threading.Lock(), coord, spec, tok, source_event="rest_reconcile", flags=[])
    row = _recon_row(conn, tok)
    assert row is not None
    assert row[0] == 1 and row[1] == 0 and row[2] == 0


def test_rest_init_does_not_record_reconciliation(tmp_path, monkeypatch):
    cli = _cli()
    conn, spec = _conn(tmp_path), _spec()
    tok = spec.up_token_id
    coord = C.CaptureCoordinator(spec, stale_book_ms=3000)   # empty, no prior reconstruction
    monkeypatch.setattr(cli, "_get_json",
                        lambda url: {"bids": [["0.40", "100"]], "asks": [["0.60", "100"]], "timestamp": 2})
    monkeypatch.setattr(cli, "server_now_cached", lambda: 100)
    cli._rest_snapshot(conn, threading.Lock(), coord, spec, tok, source_event="rest_init", flags=[])
    assert _recon_row(conn, tok) is None
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_book_reconciliation").fetchone()[0] == 0


# --- Codex #8: per-window RTDS delivery mode + staleness + clock recording -------------------------

def _tick(ws, i, *, snapshot):
    from sniper.algos.polymarket_crypto5m.models import ChainlinkTick
    return ChainlinkTick("btc/usd", (ws + i) * 1000, None, ws + i, 63456.0,
                         str(int(63456.0 * 10 ** 18)), snapshot)


def test_record_window_integrity_writes_delivery_and_staleness(tmp_path):
    cli = _cli()
    conn, spec = _conn(tmp_path), _spec()
    ws = spec.window_start_ts
    P.append_chainlink_tick(conn, _tick(ws, 0, snapshot=True), window_start_ts=ws)    # snapshot backfill
    P.append_chainlink_tick(conn, _tick(ws, 1, snapshot=False), window_start_ts=ws)   # live update
    cli._record_window_integrity(conn, threading.Lock(), spec, now=ws + 5)
    row = conn.execute("SELECT clock_source, delivery_mode, staleness_ms FROM crypto5m_clock").fetchone()
    assert row[0] == "binance_server"
    assert row[1] == "live_updates"          # a non-snapshot tick present -> live, never silently "live"
    assert row[2] == 4000                    # (ws+5 - (ws+1)) * 1000 ms


def test_record_window_integrity_snapshot_only_is_not_live(tmp_path):
    cli = _cli()
    conn, spec = _conn(tmp_path), _spec()
    ws = spec.window_start_ts
    P.append_chainlink_tick(conn, _tick(ws, 0, snapshot=True), window_start_ts=ws)
    cli._record_window_integrity(conn, threading.Lock(), spec, now=ws + 5)
    row = conn.execute("SELECT delivery_mode FROM crypto5m_clock").fetchone()
    assert row[0] == "snapshot_reconstruction"   # snapshot-only is NEVER reported as live
