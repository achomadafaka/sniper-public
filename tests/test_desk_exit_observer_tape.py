"""The forward observer reads temporary, read-only source fixtures only."""
import importlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from sniper.util import clock

NOW = 1_800_000_000.0
OFFSET = 100.0
MINT = "AEPBdj3RViMJKCHM1xiVaM1VNuP7VbZRSN68gu5xpump"
SOL = "11111111111111111111111111111111"
FIELDS = "received_at REAL,signature TEXT,mint TEXT,curve TEXT,is_buy INTEGER,quote_atomic INTEGER,token_atomic INTEGER,actor TEXT,virtual_quote INTEGER,virtual_token INTEGER,real_quote INTEGER,quote_mint TEXT,fee_bps INTEGER,creator_fee_bps INTEGER,cashback_fee_bps INTEGER,slot INTEGER,tx_index INTEGER,ordinal INTEGER"


def at(host):
    return datetime.fromtimestamp(host - OFFSET, timezone.utc).isoformat()


def position(**changes):
    return dict(id=4, mint=MINT, network_id="solana", opened_at=at(NOW - 100),
                entry_price=0.5, open_qty=123.456789, open_qty_known=True, **changes)


def quote(**changes):
    row = dict(id=17, position_id=4, mint=MINT, network_id="solana", seam="exit_decision",
               observed_at=at(NOW - 1), quoted_at=at(NOW - 2), open_qty=123.456789,
               expected_usdc=246.913578, unit_price=2.0,
               quote_evidence_json=json.dumps({"input_atomic": "123456789", "venue": "pump_native"}))
    row.update(changes)
    return row


def swap(rel=-10, **changes):
    row = dict(received_at=NOW + rel, signature="swap" + str(rel), mint=MINT, curve="curve",
               is_buy=1, quote_atomic=1_000_000_000, token_atomic=2_000_000, actor="external",
               virtual_quote=31_000_000_000, virtual_token=1_000_000_000,
               real_quote=1_000_000_000, quote_mint=SOL, fee_bps=100,
               creator_fee_bps=50, cashback_fee_bps=0, slot=10, tx_index=0, ordinal=0)
    row.update(changes)
    return row


def source(desk, rows=None, *, missing=()):
    rows = [swap(-101), swap(-10), swap(-1)] if rows is None else rows
    groups = {}
    for row in rows:
        day = datetime.fromtimestamp(row["received_at"], timezone.utc).date().isoformat()
        groups.setdefault(day, []).append(row)
    for day, items in groups.items():
        p = desk / "swaps" / (day + ".db")
        p.parent.mkdir(parents=True, exist_ok=True)
        fields = [f for f in FIELDS.split(",") if f.split()[0] not in missing]
        names = [f.split()[0] for f in fields]
        with sqlite3.connect(p) as c:
            c.execute("CREATE TABLE swaps(" + ",".join(fields) + ")")
            c.execute("CREATE INDEX swaps_mint_slot ON swaps(mint,slot)")
            c.executemany("INSERT INTO swaps VALUES(" + ",".join("?" for _ in names) + ")",
                          [tuple(r[k] for k in names) for r in items])
    return desk


def launch(desk, rel=-110, **changes):
    p = desk / "launches" / (datetime.fromtimestamp(NOW + rel, timezone.utc).date().isoformat() + ".db")
    p.parent.mkdir(parents=True, exist_ok=True)
    row = dict(mint=MINT, curve="curve", creator="creator", slot=1,
               signature="launch", block_time=int(NOW + rel), received_at=NOW + rel)
    row.update(changes)
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY,curve TEXT,creator TEXT,slot INTEGER,signature TEXT,block_time INTEGER,received_at REAL)")
        c.execute("INSERT INTO launches VALUES(?,?,?,?,?,?,?)", tuple(row.values()))


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.setattr(clock, "offset_s", lambda: OFFSET)
    try:
        return importlib.import_module("sniper.services.memecoin_desk_exit_observer_tape")
    except ModuleNotFoundError:
        return None


def read(reader, desk, *, pos=None, q="default", proof=None, fills=lambda _: ["ours"]):
    assert reader is not None, "observer tape adapter is missing"
    return reader.read_snapshot(position() if pos is None else pos, quote() if q == "default" else q,
        desk_dir=desk, now=NOW, fill_sigs_of=fills,
        proof_fn=(lambda *a, **kw: {"from": NOW - 500, "through": NOW - 1}) if proof is None else proof)


def test_snapshot_preserves_closed_native_state_without_fx_and_keeps_quote_separate(reader, tmp_path):
    got = read(reader, source(tmp_path))
    assert got["source_reason"] is None and got["quantity_reason"] is None
    assert got["cut"] == NOW - 2 and got["opened_at"] == NOW - 100
    assert got["coverage_from"] == NOW - 500
    assert got["residual_atomic"] == "123456789"
    assert got["own_signatures"] == ["ours"] and got["own_complete"] is True
    assert [r["signature"] for r in got["rows"]] == ["swap-101", "swap-10"]
    assert got["rows"][-1]["virtual_quote"] == 31_000_000_000
    assert got["exact_quote"]["unit_price"] == 2.0
    assert got["launch_at"] is None


@pytest.mark.parametrize("proof", [None, {"from": NOW - 500, "through": NOW - 6},
    {"from": NOW - 500, "through": NOW + 1}, {"from": NOW + 1, "through": NOW - 1}])
def test_unknown_capture_never_looks_like_a_quiet_tape(reader, tmp_path, proof):
    got = read(reader, source(tmp_path), proof=lambda *a, **kw: proof)
    assert got["source_reason"] == "capture_unproven" and got["rows"] == []
    assert got["coverage_from"] is None


def test_continuous_start_clips_rows_and_leaves_peak_history_censored(reader, tmp_path):
    got = read(reader, source(tmp_path), proof=lambda *a, **kw: {"from": NOW - 30, "through": NOW - 1})
    assert [r["signature"] for r in got["rows"]] == ["swap-10"]
    assert got["coverage_from"] == NOW - 30 > got["opened_at"]


@pytest.mark.parametrize("missing", [("cashback_fee_bps",), ("fee_bps",), ("creator_fee_bps",)])
def test_historical_absent_fee_is_explicit_unknown_not_zero_or_dropped(reader, tmp_path, missing):
    got = read(reader, source(tmp_path, missing=missing))
    assert got["source_reason"] is None
    assert got["rows"][-1][missing[0]] is None


@pytest.mark.parametrize("missing", [("virtual_quote",), ("real_quote",), ("quote_mint",), ("signature",)])
def test_missing_required_state_or_identity_column_blinds_source(reader, tmp_path, missing):
    got = read(reader, source(tmp_path, missing=missing))
    assert got["source_reason"] is not None and got["rows"] == []


@pytest.mark.parametrize("label", [None, "unknown", "USDC"])
def test_native_identity_must_be_explicit_for_every_retained_swap(reader, tmp_path, label):
    got = read(reader, source(tmp_path, [swap(-100), swap(-10, quote_mint=label)]))
    assert got["source_reason"] == "curve_quote_unknown" and got["rows"] == []


def test_graduated_curve_never_reuses_pregraduate_states(reader, tmp_path):
    got = read(reader, source(tmp_path, [swap(-10, real_quote=PT.GRADUATION_REAL_SOL)]))
    assert got["source_reason"] == "graduated_unsupported" and got["venue"] == "ps"
    assert got["rows"] == []


def test_ps_graduation_receipt_is_authoritative_even_when_curve_remains_low(reader, tmp_path):
    source(tmp_path)
    p = PT.tape_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE ps_grads(pool TEXT,mint TEXT,received_at REAL)")
        c.execute("INSERT INTO ps_grads VALUES(?,?,?)", (PT.canonical_pool(MINT), MINT, NOW - 3))
    assert read(reader, tmp_path)["source_reason"] == "graduated_unsupported"


def test_aggregate_row_cap_across_midnight_is_enforced(reader, tmp_path, monkeypatch):
    monkeypatch.setitem(globals(), "NOW", NOW - NOW % 86400 + 100)
    monkeypatch.setattr(reader, "MAX_ROWS", 1)
    got = read(reader, source(tmp_path, [swap(-200), swap(-10)]))
    assert got["source_reason"] == "row_limit" and got["rows"] == []


@pytest.mark.parametrize("fills", [None, lambda _: (), lambda _: "ours", lambda _: [""],
                                   lambda _: (_ for _ in ()).throw(ValueError("unavailable"))])
def test_unknown_own_identity_preserves_rows_but_never_assumes_external(reader, tmp_path, fills):
    got = read(reader, source(tmp_path), fills=fills)
    assert got["source_reason"] is None and got["rows"]
    assert got["own_complete"] is False and got["own_signatures"] == []


@pytest.mark.parametrize("change", [{"position_id": 5}, {"mint": "other"}, {"network_id": "8453"},
    {"seam": "peak_preview"}, {"observed_at": at(NOW - 31)}, {"quoted_at": at(NOW - 31)},
    {"observed_at": at(NOW + 1)}, {"quoted_at": at(NOW + 1)},
    {"observed_at": "bad"}, {"quoted_at": "2026-10-23T00:00:00"}, {"unit_price": 0},
    {"unit_price": 3}, {"expected_usdc": float("inf")}, {"open_qty": 123.456788}])
def test_invalid_exact_quote_only_blinds_quantity_and_preserves_source(reader, tmp_path, change):
    got = read(reader, source(tmp_path), q=quote(**change))
    assert got["residual_atomic"] is None and got["quantity_reason"] is not None
    assert got["source_reason"] is None and got["rows"]


@pytest.mark.parametrize("atomic", [None, 123456789, 123456789.0, True, "123456789.0", "0123456789", "-1", "0", " 123456789", str(2**64)])
def test_quantity_needs_positive_canonical_u64_decimal_text(reader, tmp_path, atomic):
    got = read(reader, source(tmp_path), q=quote(quote_evidence_json=json.dumps({"input_atomic": atomic})))
    assert got["residual_atomic"] is None and got["quantity_reason"] is not None


@pytest.mark.parametrize("change", [{"open_qty_known": False}, {"open_qty": 120}, {"close_claim_id": "busy"},
                                    {"derisk_pending_at": "pending"}])
def test_partial_or_busy_position_never_binds_a_previous_quote(reader, tmp_path, change):
    p = position(); p.update(change)
    got = read(reader, source(tmp_path), pos=p)
    assert got["residual_atomic"] is None and got["rows"]


def test_exact_atoms_above_float_precision_survive_unchanged(reader, tmp_path):
    atomic = "9007199254740993"
    human = float(9007199254.740993)
    p = position(); p["open_qty"] = human
    q = quote(open_qty=human, expected_usdc=human * 2,
              quote_evidence_json=json.dumps({"input_atomic": atomic}))
    got = read(reader, source(tmp_path), pos=p, q=q)
    assert got["residual_atomic"] == atomic


def test_missing_or_incomplete_launch_is_unknown_and_never_first_retained_actor(reader, tmp_path):
    source(tmp_path)
    assert read(reader, tmp_path)["launch_at"] is None
    launch(tmp_path)
    got = read(reader, tmp_path, proof=lambda *a, **kw: {"from": NOW - 105, "through": NOW - 1})
    assert got["launch_at"] is None


def test_proven_native_launch_exposes_exact_first_captured_swap_receipt(reader, tmp_path):
    source(tmp_path)
    launch(tmp_path)
    assert read(reader, tmp_path)["launch_at"] == NOW - 101


def test_wrong_curve_launch_cannot_certify_inventory_history(reader, tmp_path):
    source(tmp_path)
    launch(tmp_path, curve="other")
    assert read(reader, tmp_path)["launch_at"] is None


def test_completed_partial_receipt_does_not_blind_a_fresh_residual_quote(reader, tmp_path):
    pos = position()
    pos.update(derisked_at=at(NOW - 5), derisk_sig="completed", derisk_pending_at=at(NOW - 7))
    got = read(reader, source(tmp_path), pos=pos)
    assert got["residual_atomic"] == "123456789" and got["quantity_reason"] is None


@pytest.mark.parametrize("invalid_time", [None, "bad", float("inf")])
def test_unplaceable_source_time_never_disappears_as_zero_activity(reader, tmp_path, invalid_time):
    source(tmp_path)
    day = datetime.fromtimestamp(NOW, timezone.utc).date().isoformat()
    with sqlite3.connect(tmp_path / "swaps" / (day + ".db")) as c:
        c.execute("UPDATE swaps SET received_at=? WHERE signature='swap-101'", (invalid_time,))
    got = read(reader, tmp_path)
    assert got["source_reason"] == "curve_time_unknown" and got["rows"] == []


@pytest.mark.parametrize("field", ["derisked_at", "_exit_observer_partial_at"])
@pytest.mark.parametrize("stale_clock", ["observed_at", "quoted_at", "both"])
def test_quote_must_follow_latest_completed_partial_even_when_float_quantity_looks_equal(reader, tmp_path, field, stale_clock):
    pos = position()
    pos[field] = at(NOW - 1)
    q = quote(observed_at=at(NOW - 0.1), quoted_at=at(NOW - 0.2))
    if stale_clock in ("observed_at", "both"):
        q["observed_at"] = at(NOW - 2)
    if stale_clock in ("quoted_at", "both"):
        q["quoted_at"] = at(NOW - 2)
    got = read(reader, source(tmp_path), pos=pos, q=q)
    assert got["residual_atomic"] is None and got["quantity_reason"] == "quote_before_partial"
    assert got["source_reason"] is None and got["rows"]


@pytest.mark.parametrize("field", ["derisked_at", "_exit_observer_partial_at"])
@pytest.mark.parametrize("value", ["bad", "", "2026-10-23T00:00:00", 123])
def test_malformed_completed_partial_clock_blinds_quantity(reader, tmp_path, field, value):
    pos = position()
    pos[field] = value
    got = read(reader, source(tmp_path), pos=pos)
    assert got["residual_atomic"] is None and got["quantity_reason"] == "partial_clock_unknown"


def test_fresh_quote_after_both_completed_partial_clocks_is_valid(reader, tmp_path):
    pos = position()
    pos.update(derisked_at=at(NOW - 10), _exit_observer_partial_at=at(NOW - 5))
    assert read(reader, source(tmp_path), pos=pos)["residual_atomic"] == "123456789"
