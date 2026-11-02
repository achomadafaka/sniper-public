"""Codex audit #11 (35948d32..d7ef0f2c): scripts/research/pool_activity_backfill.py (untracked,
not authored in this session -- fixed and committed with the fix per Alpha's instruction, since no
git history exists to attribute it to).

Two bugs in `measure()`:

1. `sigs = rpc(...) or []` made a FAILED page (the RPC call itself never answered) read exactly
   like a genuinely short page (`len(sigs) < PAGE`), which the loop treats as "reached the end of
   history". A transient RPC failure therefore silently marked the 5-minute activity window
   covered and reported `txns_m5`/`txns_m1` as real, possibly on zero real data.

2. Pool age was only ever `entry_epoch - first_seen` or None -- `first_seen` is set exclusively by
   a genuine short page (proof of genesis). Any other case (RPC failure, or MAX_PAGES exhausted
   first) discarded the oldest signature actually observed and reported no age at all, even though
   that observation is a real lower bound.

Also renamed txns_m5/txns_m1 -> addr_txns_m5/addr_txns_m1: `getSignaturesForAddress` counts every
transaction touching the pool address, not proven swaps.
"""
import scripts.research.pool_activity_backfill as B
import sqlite3

ENTRY = 1_000_000


def _sig(block_time, sig_id):
    return {"blockTime": block_time, "signature": sig_id}


def test_a_first_page_rpc_failure_reports_unknown_not_a_covered_window(monkeypatch):
    monkeypatch.setattr(B, "rpc", lambda *_a, **_k: None)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)

    result = B.measure("pool", ENTRY, rate=1000.0)

    assert result is None, (
        "a first-page RPC failure has zero real data -- there is nothing to report, "
        "not a confidently-empty window")


def test_a_later_page_failure_does_not_fabricate_activity_counts(monkeypatch):
    """The first page covers the 5-minute window and is a full PAGE (so paging continues looking
    for genesis); the second page then fails. The already-covered 5-minute counts must survive
    (they were seen for real), but genesis/age must fall back to a bound, not a confident age."""
    calls = {"n": 0}
    full_page = [_sig(ENTRY - 100, "s%d" % i) for i in range(B.PAGE)]
    full_page[-1] = _sig(ENTRY - 400, "s_last")   # pushes min(bt) past entry-300, triggers coverage

    def fake_rpc(method, params):
        calls["n"] += 1
        if calls["n"] == 1:
            return full_page
        return None   # every subsequent page fails

    monkeypatch.setattr(B, "rpc", fake_rpc)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)

    result = B.measure("pool", ENTRY, rate=1000.0)

    assert result is not None
    assert result["addr_txns_m5"] is not None, (
        "the 5-minute window was genuinely covered by the first page -- "
        "a later page's failure must not erase that")
    assert result["pool_age_is_bound"] is True
    assert result["pool_age_s"] == ENTRY - (ENTRY - 400), "age falls back to the oldest signature actually seen"
    assert result["coverage_failed"] is True


def test_a_short_page_without_creation_evidence_reports_an_age_bound(monkeypatch):
    short_page = [_sig(ENTRY - 500, "s1"), _sig(ENTRY - 900, "s2")]
    monkeypatch.setattr(B, "rpc", lambda *_a, **_k: short_page)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)

    result = B.measure("pool", ENTRY, rate=1000.0)

    assert result["pool_age_is_bound"] is True
    assert result["pool_age_s"] == ENTRY - (ENTRY - 900)


def test_window_counts_only_include_the_requested_horizon(monkeypatch):
    short_page = [_sig(ENTRY - 30, "in_both"), _sig(ENTRY - 200, "in_m5_only"),
                  _sig(ENTRY - 1000, "outside")]
    monkeypatch.setattr(B, "rpc", lambda *_a, **_k: short_page)
    monkeypatch.setattr(B.time, "sleep", lambda _s: None)

    result = B.measure("pool", ENTRY, rate=1000.0)

    assert result["addr_txns_m1"] == 1     # only entry-30
    assert result["addr_txns_m5"] == 2     # entry-30 and entry-200


def test_missing_timestamp_preserves_unknown_window_counts(monkeypatch):
    monkeypatch.setattr(B, "rpc", lambda *_a: [
        _sig(ENTRY - 50, "known"), _sig(None, "unknown")])
    result = B.measure("pool", ENTRY, rate=1000)
    assert result["addr_txns_m5"] is None
    assert result["addr_txns_m1"] is None
    assert result["pool_age_is_bound"] is True
    assert result["sigs_seen"] == 2
    assert result["coverage_failed"] is True


def test_signatures_after_entry_do_not_create_a_negative_age(monkeypatch):
    monkeypatch.setattr(B, "rpc", lambda *_a: [_sig(ENTRY + 20, "future")])
    result = B.measure("pool", ENTRY, rate=1000)
    assert result["pool_age_s"] is None
    assert result["pool_age_is_bound"] is True


def test_short_history_without_the_window_start_is_incomplete(monkeypatch):
    monkeypatch.setattr(B, "rpc", lambda *_a: [_sig(ENTRY - 20, "recent")])
    result = B.measure("pool", ENTRY, rate=1000)
    assert result["addr_txns_m5"] is None
    assert result["addr_txns_m1"] is None


def test_a_full_page_at_the_included_boundary_does_not_prove_coverage(monkeypatch):
    monkeypatch.setattr(B, "PAGE", 1)
    monkeypatch.setattr(B, "MAX_PAGES", 1)
    monkeypatch.setattr(B.time, "sleep", lambda _: None)
    monkeypatch.setattr(B, "rpc", lambda *_a: [_sig(ENTRY - 300, "boundary")])
    result = B.measure("pool", ENTRY, rate=1000)
    assert result["addr_txns_m5"] is None


def test_main_retries_an_incomplete_stored_measurement(tmp_path, monkeypatch):
    ledger, output = tmp_path / "ledger.db", tmp_path / "activity.db"
    with sqlite3.connect(ledger) as conn:
        conn.executescript("""
            CREATE TABLE memecoin_positions(id,opened_at,realized_pnl,execution_mode,network_id,signal_source);
            CREATE TABLE memecoin_entry_attempts(position_id,attempt_id,quote_route_target,classification_json);
            INSERT INTO memecoin_positions VALUES(1,'2026-09-15T12:00:00Z',2,'real','solana','follow_activity');
            INSERT INTO memecoin_entry_attempts VALUES(1,'attempt','pool','{}');
        """)
    monkeypatch.setattr(B, "LEDGER", str(ledger))
    monkeypatch.setattr(B, "OUT", str(output))
    monkeypatch.setattr(B.sys, "argv", ["pool_activity_backfill.py"])
    monkeypatch.setattr(B.time, "sleep", lambda _: None)
    monkeypatch.setattr(B, "measure", lambda *args: None)
    B.main()
    monkeypatch.setattr(B, "measure", lambda *args: {
        "addr_txns_m5": 3, "addr_txns_m1": 1, "pool_age_s": 600,
        "pool_age_is_bound": True, "pages": 1, "sigs_seen": 4})
    B.main()
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT addr_txns_m5,addr_txns_m1 FROM pool_activity").fetchall() == [(3, 1)]

    # A previous version could mark incomplete timestamp coverage as complete.
    with sqlite3.connect(output) as conn:
        names = {row[1] for row in conn.execute("PRAGMA table_info(pool_activity)")}
        if "measurement_version" in names:
            conn.execute("ALTER TABLE pool_activity DROP COLUMN measurement_version")
        conn.execute("UPDATE pool_activity SET addr_txns_m5=99,pool_age_is_bound=1")
    B.main()
    with sqlite3.connect(output) as conn:
        assert conn.execute("SELECT addr_txns_m5 FROM pool_activity").fetchone()[0] == 3
