"""Keeping route quotes must not be able to change what a rail decides.

This is the only route-space price series that will exist — `executable_exit_value` is an alias for
`mid` on 100% of 3.2M mark rows — so it is worth having, and worth being unable to cost anything.
"""
from __future__ import annotations

import pathlib
import sqlite3
import json

import pytest

from sniper.services import memecoin_route_series as rs

NOW = "2026-08-26T12:00:00+00:00"
RUNNER = pathlib.Path(__file__).resolve().parents[1] / "sniper" / "services" / "memecoin_real_runner.py"


@pytest.fixture(autouse=True)
def _clean():
    rs.reset_state()
    yield
    rs.reset_state()


def _pos(pid=1, qty=1000.0):
    return {"id": pid, "mint": "MINT", "network_id": "solana", "open_qty": qty}


def _preview(expected=50.0, quoted_at=NOW):
    return {"expected_usdc": expected, "quoted_at": quoted_at}


def _vf(preview, key):
    return (preview or {}).get(key)


# ------------------------------------------------------------------ recording

def test_a_route_quote_is_kept_with_its_seam():
    assert rs.record(_pos(), _preview(), seam=rs.SEAM_PEAK_PREVIEW, now_iso=NOW, value_fn=_vf)
    row = rs._BUFFER[0]
    assert row["seam"] == "peak_preview" and row["expected_usdc"] == 50.0
    assert row["position_id"] == 1 and row["mint"] == "MINT"


def test_the_unit_price_is_derived_so_it_compares_to_entry_price():
    rs.record(_pos(qty=1000.0), _preview(expected=50.0), seam=rs.SEAM_EXIT_DECISION,
              now_iso=NOW, value_fn=_vf)
    assert rs._BUFFER[0]["unit_price"] == pytest.approx(0.05)


def test_an_unknown_quantity_leaves_the_unit_price_NULL_rather_than_faking_it():
    """Faking a per-unit price from a nominal number is the mistake this series exists to escape."""
    rs.record(_pos(qty=0), _preview(), seam=rs.SEAM_EQUITY_MARK, now_iso=NOW, value_fn=_vf)
    assert rs._BUFFER[0]["unit_price"] is None
    assert rs._BUFFER[0]["expected_usdc"] == 50.0, "the bag value is still real and still kept"


@pytest.mark.parametrize("preview", [
    None, {}, {"expected_usdc": None}, {"expected_usdc": 0}, {"expected_usdc": -5},
    {"expected_usdc": float("nan")},
])
def test_a_preview_without_a_usable_price_is_dropped_not_stored_as_null(preview):
    """A null row would be indistinguishable from a quote that priced the bag at nothing."""
    assert rs.record(_pos(), preview, seam=rs.SEAM_PEAK_PREVIEW, now_iso=NOW, value_fn=_vf) is False
    assert rs._BUFFER == []
    # the hole is named instead, unless nothing was asked for (a None preview from a non-asking site)
    assert [g["kind"] for g in rs._GAPS] == ([] if preview is None else ["quote_miss"])


def test_recording_never_raises_and_counts_the_failure():
    def boom(_p, _k):
        raise RuntimeError("preview exploded")
    assert rs.record(_pos(), _preview(), seam="x", now_iso=NOW, value_fn=boom) is False
    assert rs.snapshot()["errors"] == 1


def test_the_buffer_is_bounded_and_drops_the_oldest():
    for i in range(rs._MAX_BUFFER + 5):
        rs.record(_pos(pid=i), _preview(), seam=rs.SEAM_EQUITY_MARK, now_iso=NOW, value_fn=_vf)
    assert len(rs._BUFFER) == rs._MAX_BUFFER
    assert rs.snapshot()["dropped_full"] == 5
    assert rs._BUFFER[0]["position_id"] == 5, "oldest went first"


# ------------------------------------------------------------------ flushing

def test_flush_writes_and_empties():
    for i in range(3):
        rs.record(_pos(pid=i), _preview(), seam=rs.SEAM_EXIT_AUDIT, now_iso=NOW, value_fn=_vf)
    written = []
    assert rs.flush(None, write_fn=lambda _c, rows, _g, **_k: written.extend(rows)) == 3
    assert len(written) == 3 and rs._BUFFER == []
    assert rs.snapshot()["flushed"] == 3


def test_quote_evidence_survives_the_buffer_and_database():
    from sniper.execution.exit_preview import ExitPreview, PreviewResult
    from sniper.persistence import db_memecoin as m
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    evidence = {"version": 1, "venue": "relay_same_chain", "input_atomic": "99999999999999999999",
                "expected_output_atomic": "90000000000000000", "minimum_output_atomic": "63000000000000000",
                "quote_asset_usd": 1000, "quote_asset_decimals": 18, "slippage_bps": 3000}
    preview = PreviewResult.succeeded(ExitPreview(90, 99999999999999999999, "router", NOW,
                                                  quote_evidence=evidence))
    assert rs.record(_pos(), preview, seam=rs.SEAM_EXIT_DECISION, now_iso=NOW)
    evidence["venue"] = "mutated"
    assert rs.flush(conn) == 1
    row = conn.execute("SELECT * FROM memecoin_route_series").fetchone()
    stored = json.loads(row["quote_evidence_json"])
    assert stored["venue"] == "relay_same_chain"
    assert stored["input_atomic"] == "99999999999999999999"
    assert row["expected_usdc"] == 90
    m.ensure_memecoin_tables(conn)
    conn.close()


def test_evidence_excludes_transaction_bytes_and_preserves_native_amounts():
    from sniper.execution.exit_preview import quote_evidence
    payload = {"_venue": "pump_native", "inAmount": "999", "outAmount": "9000000",
               "otherAmountThreshold": "8000000", "slippageBps": 1000,
               "_native_swap": {"swapTransaction": "SECRET"},
               "_curve_leg": {"quote_mint": "Q", "expected_out": 100, "min_out": 70, "curve": "C"},
               "routePlan": [{"swapInfo": {"ammKey": "P", "inputMint": "Q", "outputMint": "USDC",
                                            "inAmount": "70", "outAmount": "9000000", "unknown": "SECRET"}}]}
    evidence = quote_evidence(payload, quote_asset="USDC", quote_asset_decimals=6, quote_asset_usd=1)
    assert evidence["expected_output_atomic"] == "9000000"
    assert evidence["curve_leg"]["min_out"] == "70"
    assert evidence["route_plan"][0]["inAmount"] == "70"
    assert "SECRET" not in json.dumps(evidence)


def test_flush_is_bounded_per_pass():
    for i in range(20):
        rs.record(_pos(pid=i), _preview(), seam=rs.SEAM_EXIT_AUDIT, now_iso=NOW, value_fn=_vf)
    assert rs.flush(None, write_fn=lambda _c, rows, _g, **_k: None, limit=5) == 5
    assert len(rs._BUFFER) == 15


def test_a_failed_write_loses_the_batch_rather_than_retrying_forever():
    """Growing without bound against a broken table would be worse than losing observations."""
    rs.record(_pos(), _preview(), seam=rs.SEAM_EXIT_AUDIT, now_iso=NOW, value_fn=_vf)

    def boom(_c, _rows, _gaps, **_k):
        raise sqlite3.OperationalError("no such table")
    assert rs.flush(None, write_fn=boom) == 0
    assert rs._BUFFER == [], "the batch is gone, not queued forever"
    assert rs.snapshot()["errors"] == 1


def test_an_empty_buffer_flushes_nothing():
    assert rs.flush(None, write_fn=lambda _c, _r: pytest.fail("should not write")) == 0


def test_the_writer_round_trips_through_a_real_table():
    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_route_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, mint TEXT, network_id TEXT,
        seam TEXT NOT NULL, observed_at TEXT NOT NULL, quoted_at TEXT,
        expected_usdc REAL NOT NULL, open_qty REAL, unit_price REAL)""")
    m.ensure_memecoin_tables(c)
    rs.record(_pos(), _preview(), seam=rs.SEAM_PEAK_PREVIEW, now_iso=NOW, value_fn=_vf)
    assert rs.flush(c, write_fn=m.record_route_series) == 1
    got = c.execute("SELECT seam, expected_usdc, unit_price FROM memecoin_route_series").fetchone()
    assert got["seam"] == "peak_preview" and got["expected_usdc"] == 50.0
    assert got["unit_price"] == pytest.approx(0.05)


# ------------------------------------------------------------------ isolation

def test_all_four_seams_are_recorded_and_distinctly_named():
    """A consumer that pools seams holds a mixture: they price different quantities at different
    moments."""
    body = RUNNER.read_text()
    for seam in ("peak_preview", "exit_decision", "exit_audit", "equity_mark"):
        assert f'_keep_route_quote(pos, preview, "{seam}"' in body \
            or f'_keep_route_quote(pos, _mk_preview, "{seam}"' in body, f"{seam} not wired"
    assert len({rs.SEAM_PEAK_PREVIEW, rs.SEAM_EXIT_DECISION,
                rs.SEAM_EXIT_AUDIT, rs.SEAM_EQUITY_MARK}) == 4


def test_the_runner_helper_swallows_everything():
    """It sits inside the exit path. It may not raise, ever."""
    body = RUNNER.read_text()
    start = body.index("def _keep_route_quote")
    block = body[start:body.index("def _executable_unit_price", start)]
    assert "except Exception:" in block and "pass" in block


def test_the_service_writes_no_decision_state():
    src = pathlib.Path(rs.__file__).read_text()
    for forbidden in ("UPDATE memecoin_positions", "exit_fn", "execute_fn", "release_pending",
                      "wallet_exec_lock", "reserve_entry"):
        assert forbidden not in src, f"an observer must not reference {forbidden}"


def test_the_heartbeat_field_survives_to_the_written_file(tmp_path):
    from sniper.services import memecoin_runner as run
    path = str(tmp_path / "hb.json")
    rs.record(_pos(), _preview(), seam=rs.SEAM_PEAK_PREVIEW, now_iso=NOW, value_fn=_vf)
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path,
                        route_series=rs.snapshot())
    got = run.read_heartbeat(path)
    assert got["route_series"]["buffered"] == 1 and got["route_series"]["recorded"] == 1


# ------------------------------------------------------------------ the starved-dict defect

def test_a_known_quantity_reaches_the_recorder_rather_than_reading_as_unknown():
    """REGRESSION 2026-08-26 (Alpha, on the first live row). `open_qty` is not a column on
    memecoin_positions — it is computed inside _executable_unit_price — so sweep dicts never carried
    it and every peak_preview / exit_decision / exit_audit row wrote unit_price NULL while the
    system knew the quantity. The 'never fake it' guard was correct and was being fed a starved
    dict, which is worse than a broken guard: a right rule on a lying input."""
    import sniper.services.memecoin_real_runner as R
    body = pathlib.Path(R.__file__).read_text()
    start = body.index("def _executable_unit_price")
    block = body[start:body.index("def executable_exit_allowed", start)]
    assert 'pos["open_qty"] = qty' in block, "the computed quantity must be stashed on the position"

    # and the recorder turns that into a real unit price
    rs.record({"id": 1, "mint": "M", "network_id": "solana", "open_qty": 1000.0},
              _preview(expected=50.0), seam=rs.SEAM_EXIT_DECISION, now_iso=NOW, value_fn=_vf)
    assert rs._BUFFER[0]["unit_price"] == pytest.approx(0.05)


def test_invalid_evidence_does_not_drop_the_quote_value():
    preview = dict(_preview(), quote_evidence={"bad": float("nan")})
    assert rs.record(_pos(), preview, seam=rs.SEAM_EXIT_DECISION, now_iso=NOW)
    assert rs._BUFFER[0]["expected_usdc"] == 50
    assert rs._BUFFER[0]["quote_evidence_json"] is None
