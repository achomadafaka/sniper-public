"""Phase 1.2 tests — flipper_tag_grain_writer.

Covers:
  * compute_wallet_fixture_hash: deterministic for both fixture and
    live modes; analyzed_at NOT in the hash; missing data raises.
  * persist_flipper_tag_grains: all spec §3.3 skip cases; insert
    → supersede sequence; idempotent re-run; correct row content.

Uses the StubSupabaseHttp from sniper.analysis.wallet_protocol.ledger.
supabase_io for offline, deterministic tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sniper.analysis.wallet_protocol.ledger.flipper_tag_grain_writer import (
    compute_wallet_fixture_hash,
    compute_wallet_fixture_hash_from_data,
    persist_flipper_tag_grains,
)
from sniper.analysis.wallet_protocol.ledger.supabase_io import StubSupabaseHttp


# ----------------------------------------------------------------------
# Minimal report fakes (we don't import ProtocolReport because we only
# need the duck-typed surface the writer touches)
# ----------------------------------------------------------------------

@dataclass
class _FakeGrain:
    grain: str
    tag: str
    n_at_grain: int
    grain_roi: float
    grain_win_rate: float
    grain_total_pnl: float
    grain_max_realized_loss: float | None = None


@dataclass
class _FakeFlipperBranch:
    is_active: bool = True
    tag_grains: list[_FakeGrain] = field(default_factory=list)
    flips: list = field(default_factory=list)


@dataclass
class _FakeReport:
    wallet: str = "0xabc"
    walk_full_history: bool = True
    flip_rate: float = 0.5
    analyzed_at: int = 1715000000
    protocol_version: str = "v2"
    flipper_branch: _FakeFlipperBranch = field(default_factory=_FakeFlipperBranch)
    closed_positions: list = field(default_factory=list)
    open_positions: list = field(default_factory=list)
    activity_rows: list = field(default_factory=list)
    # New: matches ProtocolReport.wallet_fixture_hash. Tests stamp this
    # manually via _stamp_fixture_hash() so the writer doesn't need a
    # real analyze_wallet round-trip.
    wallet_fixture_hash: str | None = None


@dataclass
class _FakeWalletData:
    """Tiny stand-in for `WalletData`. Carries only the fields
    `compute_wallet_fixture_hash_from_data` reads."""
    wallet: str
    closed_positions: list
    open_positions: list
    activity_rows: list


def _data_from_report(r: _FakeReport) -> _FakeWalletData:
    return _FakeWalletData(
        wallet=r.wallet,
        closed_positions=list(r.closed_positions),
        open_positions=list(r.open_positions),
        activity_rows=list(r.activity_rows),
    )


def _stamp_fixture_hash(r: _FakeReport) -> _FakeReport:
    """Mirror what analyze_wallet does — stamp the hash from the
    matching WalletData. Tests that exercise persist_flipper_tag_grains
    need this so the writer can read report.wallet_fixture_hash."""
    try:
        r.wallet_fixture_hash = compute_wallet_fixture_hash_from_data(
            _data_from_report(r),
        )
    except ValueError:
        r.wallet_fixture_hash = None
    return r


def _grain(tag: str, n: int = 50, roi: float = 0.2, wr: float = 0.7) -> _FakeGrain:
    return _FakeGrain(
        grain="leaf", tag=tag, n_at_grain=n, grain_roi=roi,
        grain_win_rate=wr, grain_total_pnl=n * roi * 100, grain_max_realized_loss=-50.0,
    )


def _report_with_grains(
    *,
    wallet: str = "0xabc",
    grains: list[_FakeGrain] | None = None,
    walk_full_history: bool = True,
    flipper_active: bool = True,
    activity_rows: list | None = None,
    closed_positions: list | None = None,
    flips: list | None = None,
) -> _FakeReport:
    if grains is None:
        grains = [_grain("geopolitics"), _grain("politics", n=80, roi=0.15)]
    r = _FakeReport(
        wallet=wallet,
        walk_full_history=walk_full_history,
        flipper_branch=_FakeFlipperBranch(
            is_active=flipper_active,
            tag_grains=grains,
            flips=flips or [object(), object(), object()],
        ),
        activity_rows=activity_rows or [{"id": "tx1:1"}, {"id": "tx2:0"}],
        closed_positions=closed_positions or [{"x": 1}],
    )
    return _stamp_fixture_hash(r)


# ======================================================================
# compute_wallet_fixture_hash
# ======================================================================

def test_fixture_hash_from_file_is_sha256_truncated(tmp_path):
    p = tmp_path / "fixture.json"
    p.write_bytes(b'{"hello":"world"}')
    h = compute_wallet_fixture_hash(_FakeReport(), fixture_path=p)
    assert len(h) == 16
    # Identical content → identical hash
    p2 = tmp_path / "copy.json"
    p2.write_bytes(b'{"hello":"world"}')
    h2 = compute_wallet_fixture_hash(_FakeReport(), fixture_path=p2)
    assert h == h2


def test_fixture_hash_changes_when_bytes_change(tmp_path):
    p = tmp_path / "a.json"
    p.write_bytes(b'{"x":1}')
    h1 = compute_wallet_fixture_hash(_FakeReport(), fixture_path=p)
    p.write_bytes(b'{"x":2}')
    h2 = compute_wallet_fixture_hash(_FakeReport(), fixture_path=p)
    assert h1 != h2


def test_fixture_hash_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        compute_wallet_fixture_hash(_FakeReport(), fixture_path=tmp_path / "nope.json")


def test_live_hash_deterministic_excludes_wall_clock():
    """Two WalletData with same raw rows but different fetch times
    must produce the SAME hash. (Hash derivation doesn't read
    fetched_at / analyzed_at.)"""
    d1 = _data_from_report(_report_with_grains())
    d2 = _data_from_report(_report_with_grains())
    assert (
        compute_wallet_fixture_hash_from_data(d1)
        == compute_wallet_fixture_hash_from_data(d2)
    )


def test_live_hash_changes_when_activity_changes():
    d1 = _data_from_report(_report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}],
    ))
    d2 = _data_from_report(_report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx3:0"}],
    ))
    assert (
        compute_wallet_fixture_hash_from_data(d1)
        != compute_wallet_fixture_hash_from_data(d2)
    )


def test_live_hash_independent_of_activity_row_order():
    """Sorting activity row IDs makes the hash order-independent."""
    d1 = _data_from_report(_report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}],
    ))
    d2 = _data_from_report(_report_with_grains(
        activity_rows=[{"id": "tx2:0"}, {"id": "tx1:0"}],
    ))
    assert (
        compute_wallet_fixture_hash_from_data(d1)
        == compute_wallet_fixture_hash_from_data(d2)
    )


def test_live_hash_changes_when_wallet_changes():
    """Wallet identity is in the fingerprint — two identical fixtures
    for different wallets must hash differently."""
    d1 = _data_from_report(_report_with_grains(wallet="0xabc"))
    d2 = _data_from_report(_report_with_grains(wallet="0xdef"))
    assert (
        compute_wallet_fixture_hash_from_data(d1)
        != compute_wallet_fixture_hash_from_data(d2)
    )


def test_live_hash_empty_data_raises():
    d = _FakeWalletData(
        wallet="0xabc", closed_positions=[], open_positions=[], activity_rows=[],
    )
    with pytest.raises(ValueError, match="cannot derive"):
        compute_wallet_fixture_hash_from_data(d)


def test_compute_wallet_fixture_hash_reads_stamped_report_field():
    """`compute_wallet_fixture_hash(report)` returns the stamped value
    when present — does not re-derive."""
    r = _report_with_grains()
    assert r.wallet_fixture_hash is not None
    assert compute_wallet_fixture_hash(r) == r.wallet_fixture_hash


def test_compute_wallet_fixture_hash_raises_when_unstamped():
    """A report without a stamp and no fixture_path is an error —
    explicit guard against silent live-mode failure modes."""
    r = _FakeReport()
    r.wallet_fixture_hash = None
    with pytest.raises(ValueError, match="no wallet_fixture_hash stamp"):
        compute_wallet_fixture_hash(r)


# ======================================================================
# persist_flipper_tag_grains — skip cases
# ======================================================================

def test_skip_when_wallet_empty():
    r = _report_with_grains(wallet="")
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert res.skipped_reason == "report.wallet is empty"
    assert res.rows_written == 0
    assert len(db.rows_by_table.get("wallet_flipper_tag_grain", [])) == 0


def test_skip_when_flipper_inactive():
    r = _report_with_grains(flipper_active=False)
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert "inactive" in res.skipped_reason
    assert res.rows_written == 0


def test_skip_when_walk_full_history_false():
    r = _report_with_grains(walk_full_history=False)
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert "walk_full_history" in res.skipped_reason
    assert res.rows_written == 0


def test_skip_when_all_grains_have_zero_n():
    r = _report_with_grains(grains=[_grain("foo", n=0), _grain("bar", n=0)])
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert "n_at_grain >= 1" in res.skipped_reason
    assert res.rows_written == 0


def test_per_grain_n_zero_is_skipped_but_others_written():
    """Mix of zero and non-zero grains — only non-zero land."""
    r = _report_with_grains(grains=[
        _grain("zero_one", n=0),
        _grain("good_one", n=50),
        _grain("zero_two", n=0),
        _grain("another_good", n=80),
    ])
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert res.rows_written == 2
    written_tags = {r["tag"] for r in db.rows_by_table["wallet_flipper_tag_grain"]}
    assert written_tags == {"good_one", "another_good"}


# ======================================================================
# persist_flipper_tag_grains — happy path + idempotency
# ======================================================================

def test_writes_rows_with_correct_content():
    r = _report_with_grains(wallet="0xWALLET")
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db, code_version="test-1.0")
    assert res.rows_written == 2
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert all(row["wallet"] == "0xwallet" for row in rows)  # lowercased
    assert all(row["status"] == "active" for row in rows)
    assert all(row["walk_full_history"] is True for row in rows)
    assert all(row["protocol_version"] == "v2" for row in rows)
    assert all(row["code_version"] == "test-1.0" for row in rows)
    assert all(row["wallet_fixture_hash"] == res.wallet_fixture_hash for row in rows)
    assert all(len(row["wallet_fixture_hash"]) == 16 for row in rows)


def test_idempotent_when_same_hash():
    """Running the writer twice on identical content writes once."""
    r = _report_with_grains()
    db = StubSupabaseHttp()
    res1 = persist_flipper_tag_grains(r, supabase=db)
    res2 = persist_flipper_tag_grains(r, supabase=db)
    assert res1.rows_written == 2
    assert res2.rows_written == 0
    assert res2.skipped_reason is not None
    assert "idempotent" in res2.skipped_reason


def test_partial_same_hash_recovery_inserts_missing_rows_only():
    """Reviewer finding #4 (2026-05-17): if a prior run inserted only
    a subset of the expected rows at the current fixture hash (writer
    interrupted, partial unique conflict swallowed half, etc.), the
    re-run currently returns `idempotent — N rows already exist` and
    leaves the gap forever. Coverage must be by (grain, tag) key set,
    not by row presence."""
    r = _report_with_grains(
        wallet="0xabc",
        grains=[_grain("geopolitics"), _grain("politics", n=80, roi=0.15)],
    )
    db = StubSupabaseHttp()

    # First run: write both rows.
    res1 = persist_flipper_tag_grains(r, supabase=db)
    assert res1.rows_written == 2

    # Simulate a partial-write state: manually delete one of the two
    # active rows so only (leaf, geopolitics) remains at this hash.
    store = db.rows_by_table["wallet_flipper_tag_grain"]
    store[:] = [row for row in store if row["tag"] != "politics"]
    assert len(store) == 1 and store[0]["tag"] == "geopolitics"

    # Re-run on identical content. The writer must NOT no-op — it must
    # heal the missing (leaf, politics) row.
    res2 = persist_flipper_tag_grains(r, supabase=db)
    assert res2.rows_written == 1, (
        f"partial same-hash state must insert only the missing row, "
        f"got rows_written={res2.rows_written}"
    )
    assert res2.skipped_reason is None, (
        f"partial recovery is not a skip; got skipped_reason="
        f"{res2.skipped_reason!r}"
    )

    final_rows = db.rows_by_table["wallet_flipper_tag_grain"]
    final_keys = {(r["grain"], r["tag"]) for r in final_rows if r["status"] == "active"}
    assert final_keys == {("leaf", "geopolitics"), ("leaf", "politics")}


def test_full_same_hash_coverage_still_no_ops():
    """Regression preserve: when every candidate (grain, tag) is already
    present at the current hash, the writer is still a pure no-op.
    `test_idempotent_when_same_hash` covers this happy path; this test
    additionally pins that the skipped_reason mentions full coverage so
    operators can distinguish full-coverage idempotency from
    partial-recovery skips that might appear in future code."""
    r = _report_with_grains(
        wallet="0xabc",
        grains=[_grain("geopolitics"), _grain("politics", n=80)],
    )
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)
    res2 = persist_flipper_tag_grains(r, supabase=db)
    assert res2.rows_written == 0
    assert res2.skipped_reason is not None
    assert "idempotent" in res2.skipped_reason


def test_partial_same_hash_recovery_does_not_duplicate_present_row():
    """During partial recovery the present same-hash row must NOT be
    re-inserted — only the missing rows. We assert by counting
    insert_ignore calls: should be exactly 1 (for the missing row),
    not 2 (which would risk hitting partial unique on the present
    row)."""
    r = _report_with_grains(
        wallet="0xabc",
        grains=[_grain("geopolitics"), _grain("politics", n=80, roi=0.15)],
    )
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)

    # Delete one row to simulate partial state.
    store = db.rows_by_table["wallet_flipper_tag_grain"]
    store[:] = [row for row in store if row["tag"] != "politics"]

    db.calls.clear()  # focus on the second run only
    persist_flipper_tag_grains(r, supabase=db)

    insert_calls = [c for c in db.calls if c[0] == "insert_ignore"]
    assert len(insert_calls) == 1, (
        f"expected 1 insert_ignore call (only the missing row), got "
        f"{len(insert_calls)}: {[c[2].get('tag') for c in insert_calls]}"
    )
    assert insert_calls[0][2]["tag"] == "politics", (
        "the single insert must be the missing row, not the already-present one"
    )


def test_partial_same_hash_recovery_with_pending_different_hash_supersession():
    """Edge case: existing state has same-hash partial coverage AND a
    different-hash active row for the missing tag. Healing must
    supersede the different-hash row first (otherwise the partial unique
    index blocks the new insert), then insert the missing same-hash
    row. This test pins both halves in one scenario."""
    db = StubSupabaseHttp()

    # First run with old activity → produces v1 hash, two rows.
    r1 = _report_with_grains(
        wallet="0xabc",
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}],
        grains=[_grain("geopolitics"), _grain("politics", n=80)],
    )
    persist_flipper_tag_grains(r1, supabase=db)
    v1_hash = r1.wallet_fixture_hash

    # Second run with new activity → new v2 hash, supersedes both v1 rows
    # and inserts two new active rows.
    r2 = _report_with_grains(
        wallet="0xabc",
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}, {"id": "tx3:0"}],
        grains=[_grain("geopolitics", n=60), _grain("politics", n=90)],
    )
    persist_flipper_tag_grains(r2, supabase=db)
    v2_hash = r2.wallet_fixture_hash
    assert v1_hash != v2_hash

    # Simulate a partial-write state on v2: only (leaf, geopolitics)
    # active at v2; (leaf, politics) was lost. Both v1 rows still
    # superseded.
    store = db.rows_by_table["wallet_flipper_tag_grain"]
    store[:] = [
        row for row in store
        if not (row["status"] == "active" and row["tag"] == "politics")
    ]

    # Hand-craft a stray different-hash active politics row to simulate
    # a corrupted state where the supersession didn't cover everything.
    store.append({
        "wallet": "0xabc",
        "grain": "leaf",
        "tag": "politics",
        "status": "active",
        "wallet_fixture_hash": v1_hash,  # different from v2_hash
        "walk_full_history": True,
    })

    # Re-run on r2: writer must heal politics by inserting at v2_hash,
    # AND supersede the stray v1_hash politics row first.
    res = persist_flipper_tag_grains(r2, supabase=db)

    assert res.rows_written == 1
    assert res.rows_superseded == 1

    final_active = [
        row for row in db.rows_by_table["wallet_flipper_tag_grain"]
        if row["status"] == "active"
    ]
    active_keys_at_v2 = {
        (row["grain"], row["tag"]) for row in final_active
        if row["wallet_fixture_hash"] == v2_hash
    }
    assert active_keys_at_v2 == {("leaf", "geopolitics"), ("leaf", "politics")}


def test_supersession_when_data_changes():
    """New activity → new hash → old rows superseded, new rows active."""
    r1 = _report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}],
        grains=[_grain("geopolitics", n=50)],
    )
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r1, supabase=db)
    rows_active_v1 = [x for x in db.rows_by_table["wallet_flipper_tag_grain"] if x["status"] == "active"]
    assert len(rows_active_v1) == 1
    old_hash = rows_active_v1[0]["wallet_fixture_hash"]

    # New activity → new hash → supersession
    r2 = _report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}, {"id": "tx3:0"}],
        grains=[_grain("geopolitics", n=80), _grain("politics", n=40)],
    )
    res2 = persist_flipper_tag_grains(r2, supabase=db)

    assert res2.rows_written == 2  # geopolitics + politics under new hash
    assert res2.rows_superseded == 1  # the prior geopolitics row

    all_rows = db.rows_by_table["wallet_flipper_tag_grain"]
    active_rows = [r for r in all_rows if r["status"] == "active"]
    superseded_rows = [r for r in all_rows if r["status"] == "superseded"]

    assert len(active_rows) == 2
    assert len(superseded_rows) == 1
    assert superseded_rows[0]["wallet_fixture_hash"] == old_hash
    assert superseded_rows[0]["superseded_by_fixture_hash"] == res2.wallet_fixture_hash


def test_supersession_only_touches_same_wallet():
    """A different wallet's rows must not be superseded."""
    db = StubSupabaseHttp()
    r_other = _report_with_grains(
        wallet="0xother",
        activity_rows=[{"id": "txA:0"}],
        grains=[_grain("foreign", n=50)],
    )
    persist_flipper_tag_grains(r_other, supabase=db)

    r_target = _report_with_grains(
        wallet="0xtarget",
        activity_rows=[{"id": "txZ:0"}],
        grains=[_grain("geopolitics", n=50)],
    )
    persist_flipper_tag_grains(r_target, supabase=db)

    # Update target wallet — other wallet must be untouched
    r_target_v2 = _report_with_grains(
        wallet="0xtarget",
        activity_rows=[{"id": "txZ:0"}, {"id": "txZZ:0"}],
        grains=[_grain("geopolitics", n=80)],
    )
    res = persist_flipper_tag_grains(r_target_v2, supabase=db)

    all_rows = db.rows_by_table["wallet_flipper_tag_grain"]
    other_rows = [r for r in all_rows if r["wallet"] == "0xother"]
    assert len(other_rows) == 1
    assert other_rows[0]["status"] == "active"  # untouched
    assert res.rows_superseded == 1


def test_uses_fixture_path_when_provided(tmp_path):
    p = tmp_path / "fixture.json"
    p.write_bytes(b'{"data":"yes"}')
    r = _report_with_grains()
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db, fixture_path=p)
    # Hash should be from the file, not from the live fingerprint
    expected = compute_wallet_fixture_hash(r, fixture_path=p)
    assert res.wallet_fixture_hash == expected


def test_grain_row_includes_max_realized_loss():
    g = _grain("geopolitics")
    g.grain_max_realized_loss = -200.0
    r = _report_with_grains(grains=[g])
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert rows[0]["grain_max_realized_loss"] == -200.0


def test_grain_row_handles_none_max_realized_loss():
    g = _grain("geopolitics")
    g.grain_max_realized_loss = None
    r = _report_with_grains(grains=[g])
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert rows[0]["grain_max_realized_loss"] is None


def test_grain_row_carries_flip_rate_and_protocol_version():
    r = _report_with_grains()
    r.flip_rate = 0.6534
    r.protocol_version = "v2.1"
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert all(row["flip_rate"] == 0.6534 for row in rows)
    assert all(row["protocol_version"] == "v2.1" for row in rows)


def test_supersession_order_patch_before_insert():
    """Patch-then-insert order: all PATCH (supersede) calls must
    precede any INSERT of new active rows.

    The migration's partial unique index `idx_wftg_one_active_per_wgt`
    forbids two active rows for the same (wallet, grain, tag). Inserting
    new active rows first would race that index — `INSERT ... ON CONFLICT
    DO NOTHING` silently drops them, the wallet stays frozen at the old
    fixture_hash, and the writer reports rows_written=0 with no error.
    Patch-then-insert avoids the conflict by flipping the prior rows to
    `status='superseded'` before the new ones land.

    The brief window where the wallet has zero active rows is
    acceptable: readers degrade to "no peer data" which is also the
    failure mode for new wallets.

    Previously this test (`test_supersession_order_insert_before_patch`)
    pinned the wrong order — it locked in the very bug the deep-review
    flagged.
    """
    r1 = _report_with_grains(activity_rows=[{"id": "tx1:0"}])
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r1, supabase=db)

    db.calls.clear()  # focus on the second run

    r2 = _report_with_grains(activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}])
    persist_flipper_tag_grains(r2, supabase=db)

    insert_idxs = [i for i, c in enumerate(db.calls) if c[0] == "insert_ignore"]
    patch_idxs = [i for i, c in enumerate(db.calls) if c[0] == "patch"]
    assert insert_idxs, "expected at least one insert"
    assert patch_idxs, "expected at least one patch"
    assert max(patch_idxs) < min(insert_idxs), (
        "all patches (status→superseded) must precede any insert; "
        "otherwise the partial unique index drops the new active rows"
    )


def test_supersession_with_partial_unique_enforcement_persists_new_rows():
    """Regression for Codex deep-review finding #3.

    Migration `supabase_021_wallet_flipper_tag_grain.sql:156` creates a
    partial unique index on (wallet, grain, tag) WHERE status='active'.
    Under the OLD insert-then-patch order, inserting a new active row
    for a wallet that already has an active row at the same (grain, tag)
    fires `ON CONFLICT DO NOTHING` and `rows_written` is silently 0.
    This test exercises the stub's partial-unique enforcement to make
    sure the writer's patch-then-insert order produces a non-zero
    rows_written when the fixture_hash changes.

    StubSupabaseHttp(partial_unique_indexes=...) mirrors the migration
    constraint so future order regressions cannot pass silently.
    """
    db = StubSupabaseHttp(partial_unique_indexes=[
        ("wallet_flipper_tag_grain",
         ("wallet", "grain", "tag"),
         {"status": "active"}),
    ])

    r1 = _report_with_grains(
        activity_rows=[{"id": "tx1:0"}],
        grains=[_grain("geopolitics", n=50)],
    )
    res1 = persist_flipper_tag_grains(r1, supabase=db)
    assert res1.rows_written == 1
    assert res1.rows_superseded == 0

    # New activity → new fixture_hash → must supersede + insert.
    r2 = _report_with_grains(
        activity_rows=[{"id": "tx1:0"}, {"id": "tx2:0"}],
        grains=[_grain("geopolitics", n=80)],
    )
    res2 = persist_flipper_tag_grains(r2, supabase=db)

    # The key assertion: the new active row LANDS. Under the wrong order
    # this would be 0 because the partial unique index would block the
    # insert.
    assert res2.rows_written == 1, (
        "new active row was silently dropped — likely insert-then-patch order"
    )
    assert res2.rows_superseded == 1

    active_rows = [
        r for r in db.rows_by_table["wallet_flipper_tag_grain"]
        if r["status"] == "active"
    ]
    assert len(active_rows) == 1
    assert active_rows[0]["wallet_fixture_hash"] == res2.wallet_fixture_hash
    assert active_rows[0]["n_at_grain"] == 80  # the new value, not the old 50


def test_writer_uses_lowercased_wallet():
    r = _report_with_grains(wallet="0xABCdef0123")
    db = StubSupabaseHttp()
    res = persist_flipper_tag_grains(r, supabase=db)
    assert res.wallet == "0xabcdef0123"
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert all(row["wallet"] == "0xabcdef0123" for row in rows)


def test_analyzed_at_serialized_as_iso():
    r = _report_with_grains()
    r.analyzed_at = 1715000000
    db = StubSupabaseHttp()
    persist_flipper_tag_grains(r, supabase=db)
    rows = db.rows_by_table["wallet_flipper_tag_grain"]
    assert all("T" in row["analyzed_at"] for row in rows)
    assert all(row["analyzed_at"].endswith("+00:00") for row in rows)
