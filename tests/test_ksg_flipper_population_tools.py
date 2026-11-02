"""Acceptance tests for the FH4 peer-population-growth tools.

Covers three scripts:
  - scripts/ksg_flipper_population_check.py  (classify_population_gaps — new function)
  - scripts/ksg_flipper_candidate_score.py   (new file)
  - scripts/ksg_seed_flipper_population.py   (new file)

All tests are deliberately RED until the implementation tasks in
docs/superpowers/plans/2026-05-15-fh4-flip-roi-peer-population-growth.md
are completed.

Spec line → test mapping
------------------------
Plan §Task 1 (classify_population_gaps):
  test_classify_population_gaps_marks_ok_near_and_thin        — happy path
  test_classify_population_gaps_empty_input_returns_empty     — failure mode
  test_classify_population_gaps_custom_near_window_widens_band — edge case: wider band

Plan §Task 2 (score_candidate_wallets, load_wallet_tag_pnl_candidates):
  test_score_candidate_wallets_prioritizes_near_target_tags   — happy path
  test_load_wallet_tag_pnl_candidates_reads_sqlite_source     — happy path (SQLite)
  test_score_candidate_wallets_no_matched_tags_omits_wallet   — edge case: no match
  test_score_candidate_wallets_empty_gaps_returns_empty       — failure mode
  test_load_wallet_tag_pnl_candidates_empty_tags_returns_empty — edge case: empty filter

Plan §Task 3 (build_seed_commands, validate_execute_args):
  test_build_seed_commands_are_persist_flipper_grain_runs     — happy path
  test_execute_requires_explicit_write_acknowledgment         — failure mode
  test_validate_execute_args_max_wallets_cap_enforced         — edge case: cap >10
  test_validate_execute_args_dry_run_is_noop                  — edge case: execute=False
  test_build_seed_commands_max_exec_trades_none_omits_flag    — edge case: None flag

NOTE: wallet_flipper_tag_grain is a Supabase-only table.
<!-- schema:external=wallet_flipper_tag_grain -->
wallet_tag_pnl columns used below are verified against brain_index:
  wallet, tag, n_positions, realized_pnl, win_rate, conviction_concentration,
  computed_at, n_held_to_resolution, open_unrealized_pnl, n_open_positions,
  n_open_dead  (all confirmed in schema registry).
"""
from __future__ import annotations

import sqlite3


# ---------------------------------------------------------------------------
# Helpers — deferred imports so collection succeeds even before scripts exist.
# Each helper raises ImportError (FAIL, not ERROR) at test time.
# ---------------------------------------------------------------------------

def _import_classify_population_gaps():
    from scripts.ksg_flipper_population_check import classify_population_gaps  # noqa: PLC0415
    return classify_population_gaps


def _import_candidate_score():
    from scripts.ksg_flipper_candidate_score import (  # noqa: PLC0415
        load_wallet_tag_pnl_candidates,
        score_candidate_wallets,
    )
    return score_candidate_wallets, load_wallet_tag_pnl_candidates


def _import_seed_runner():
    from scripts.ksg_seed_flipper_population import (  # noqa: PLC0415
        build_seed_commands,
        validate_execute_args,
    )
    return build_seed_commands, validate_execute_args


def _import_population_fetch_helpers():
    """Phase 2 contract refinement: data-fetch + payload builder must be
    factored out so they can be tested without a live Supabase. Imports
    fail until the script is refactored."""
    from scripts.ksg_flipper_population_check import (  # noqa: PLC0415
        build_gaps_payload,
        fetch_active_grain_rows,
    )
    return fetch_active_grain_rows, build_gaps_payload


def _import_load_gap_rows_from_json():
    from scripts.ksg_flipper_candidate_score import load_gap_rows_from_json  # noqa: PLC0415
    return load_gap_rows_from_json


# ---------------------------------------------------------------------------
# Task 1 — classify_population_gaps
# ---------------------------------------------------------------------------


def test_classify_population_gaps_marks_ok_near_and_thin():
    """Plan Task 1 §Step 1 — happy path.

    30 wallets → ok, 27 wallets → near (within default window=5),
    4 wallets → thin.
    """
    classify_population_gaps = _import_classify_population_gaps()

    rows = [
        *[
            {
                "wallet": f"0xgeo{i}",
                "grain": "leaf",
                "tag": "geopolitics",
                "grain_roi": 0.1,
                "n_at_grain": 30,
                "protocol_version": "v2",
            }
            for i in range(30)
        ],
        *[
            {
                "wallet": f"0xpol{i}",
                "grain": "leaf",
                "tag": "politics",
                "grain_roi": 0.1,
                "n_at_grain": 30,
                "protocol_version": "v2",
            }
            for i in range(27)
        ],
        *[
            {
                "wallet": f"0xai{i}",
                "grain": "leaf",
                "tag": "ai",
                "grain_roi": 0.1,
                "n_at_grain": 30,
                "protocol_version": "v2",
            }
            for i in range(4)
        ],
    ]

    result = classify_population_gaps(rows, target_peers=30, near_window=5)

    by_tag = {(r["grain"], r["tag"]): r for r in result}
    assert by_tag[("leaf", "geopolitics")]["population_status"] == "ok"
    assert by_tag[("leaf", "geopolitics")]["peers_needed_to_ok"] == 0
    assert by_tag[("leaf", "politics")]["population_status"] == "near"
    assert by_tag[("leaf", "politics")]["peers_needed_to_ok"] == 3
    assert by_tag[("leaf", "ai")]["population_status"] == "thin"
    assert by_tag[("leaf", "ai")]["peers_needed_to_ok"] == 26


def test_classify_population_gaps_empty_input_returns_empty():
    """Failure mode: empty rows list must return an empty list, not crash."""
    classify_population_gaps = _import_classify_population_gaps()

    result = classify_population_gaps([], target_peers=30, near_window=5)
    assert result == []


def test_classify_population_gaps_custom_near_window_widens_band():
    """Edge case: near_window=10 means a gap of up to 10 peers is "near", not "thin".

    With 22 wallets and target=30, peers_needed=8.
    Default window=5 → thin.
    Custom window=10 → near.
    """
    classify_population_gaps = _import_classify_population_gaps()

    rows = [
        {
            "wallet": f"0xcrypto{i}",
            "grain": "leaf",
            "tag": "crypto",
            "grain_roi": 0.05,
            "n_at_grain": 30,
            "protocol_version": "v2",
        }
        for i in range(22)
    ]

    result_narrow = classify_population_gaps(rows, target_peers=30, near_window=5)
    result_wide = classify_population_gaps(rows, target_peers=30, near_window=10)

    assert result_narrow[0]["population_status"] == "thin"
    assert result_wide[0]["population_status"] == "near"
    assert result_wide[0]["peers_needed_to_ok"] == 8


# ---------------------------------------------------------------------------
# Task 2 — score_candidate_wallets + load_wallet_tag_pnl_candidates
# ---------------------------------------------------------------------------


def test_score_candidate_wallets_prioritizes_near_target_tags():
    """Plan Task 2 §Step 1 — happy path.

    The "near" wallet (geopolitics, peers_needed=2) has higher scarcity weight
    than the "thin" wallet (ai, peers_needed=20), so it must rank first.
    """
    score_candidate_wallets, _ = _import_candidate_score()

    gaps = [
        {
            "grain": "leaf",
            "tag": "geopolitics",
            "population_status": "near",
            "peers_needed_to_ok": 2,
        },
        {
            "grain": "leaf",
            "tag": "ai",
            "population_status": "thin",
            "peers_needed_to_ok": 20,
        },
    ]
    candidate_rows = [
        {"wallet": "0xaaa", "tag": "geopolitics", "n_positions": 50, "total_pnl": 1000},
        {"wallet": "0xbbb", "tag": "ai", "n_positions": 80, "total_pnl": 5000},
    ]

    scored = score_candidate_wallets(candidate_rows, gaps)

    assert scored[0]["wallet"] == "0xaaa"
    assert scored[0]["matched_target_tags"] == ["geopolitics"]
    assert scored[0]["score"] > scored[1]["score"]


def test_load_wallet_tag_pnl_candidates_reads_sqlite_source(tmp_path):
    """Plan Task 2 §Step 1 — SQLite source read, wallet normalized to lowercase."""
    _, load_wallet_tag_pnl_candidates = _import_candidate_score()

    db_path = tmp_path / "sniper.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE wallet_tag_pnl ("
        "wallet TEXT NOT NULL, tag TEXT NOT NULL, n_positions INTEGER NOT NULL, "
        "realized_pnl REAL NOT NULL, win_rate REAL NOT NULL, "
        "conviction_concentration REAL NOT NULL, computed_at INTEGER NOT NULL, "
        "n_held_to_resolution INTEGER NOT NULL DEFAULT 0, "
        "open_unrealized_pnl REAL NOT NULL DEFAULT 0, "
        "n_open_positions INTEGER NOT NULL DEFAULT 0, "
        "n_open_dead INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY(wallet, tag))"
    )
    conn.execute(
        "INSERT INTO wallet_tag_pnl"
        "(wallet, tag, n_positions, realized_pnl, win_rate, conviction_concentration, computed_at) "
        "VALUES ('0xAAA', 'geopolitics', 50, 1000.0, 0.7, 0.2, 1)"
    )
    conn.commit()
    conn.close()

    rows = load_wallet_tag_pnl_candidates(str(db_path), tags=["geopolitics"])

    assert rows == [
        {
            "wallet": "0xaaa",
            "tag": "geopolitics",
            "n_positions": 50,
            "realized_pnl": 1000.0,
        }
    ]


def test_score_candidate_wallets_no_matched_tags_omits_wallet():
    """Edge case: a wallet whose tag does not appear in any gap is excluded from output."""
    score_candidate_wallets, _ = _import_candidate_score()

    gaps = [
        {
            "grain": "leaf",
            "tag": "geopolitics",
            "population_status": "near",
            "peers_needed_to_ok": 3,
        },
    ]
    candidate_rows = [
        # tag "sports" is not in any gap — must be omitted entirely.
        {"wallet": "0xorphan", "tag": "sports", "n_positions": 100, "total_pnl": 9999},
    ]

    scored = score_candidate_wallets(candidate_rows, gaps)

    assert scored == []


def test_score_candidate_wallets_empty_gaps_returns_empty():
    """Failure mode: with no gaps there are no target tags, so the result is empty."""
    score_candidate_wallets, _ = _import_candidate_score()

    candidate_rows = [
        {"wallet": "0xaaa", "tag": "geopolitics", "n_positions": 50, "total_pnl": 1000},
    ]

    scored = score_candidate_wallets(candidate_rows, gaps=[])

    assert scored == []


def test_load_wallet_tag_pnl_candidates_empty_tags_returns_empty(tmp_path):
    """Edge case: empty tags list must return [] immediately without hitting the DB."""
    _, load_wallet_tag_pnl_candidates = _import_candidate_score()

    db_path = tmp_path / "sniper.db"
    # Intentionally not creating the table — if the function queries with an
    # empty IN clause it would raise; returning early is the only correct behaviour.
    rows = load_wallet_tag_pnl_candidates(str(db_path), tags=[])
    assert rows == []


# ---------------------------------------------------------------------------
# Task 3 — build_seed_commands + validate_execute_args
# ---------------------------------------------------------------------------


def test_build_seed_commands_are_persist_flipper_grain_runs():
    """Plan Task 3 §Step 1 — happy path.

    Each command must use --no-persist + --persist-flipper-grains, with
    --n-iter and --max-exec-trades forwarded as string args.
    """
    build_seed_commands, _ = _import_seed_runner()

    commands = build_seed_commands(
        wallets=["0xaaa", "0xbbb"],
        n_iter=500,
        max_exec_trades=100,
    )
    assert commands == [
        [
            "python3",
            "scripts/run_wallet_protocol.py",
            "0xaaa",
            "--no-persist",
            "--persist-flipper-grains",
            "--n-iter",
            "500",
            "--max-exec-trades",
            "100",
        ],
        [
            "python3",
            "scripts/run_wallet_protocol.py",
            "0xbbb",
            "--no-persist",
            "--persist-flipper-grains",
            "--n-iter",
            "500",
            "--max-exec-trades",
            "100",
        ],
    ]


def test_execute_requires_explicit_write_acknowledgment():
    """Plan Task 3 §Step 1 — failure mode: execute=True without acknowledgment → SystemExit(2)."""
    _, validate_execute_args = _import_seed_runner()

    try:
        validate_execute_args(execute=True, acknowledged=False, max_wallets=5)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("execute without acknowledgment should fail with SystemExit(2)")


def test_validate_execute_args_max_wallets_cap_enforced():
    """Edge case: max_wallets > 10 must be rejected even with acknowledgment.

    The plan spec §Task 3 Step 4 says the CLI hard-caps at 10.
    """
    _, validate_execute_args = _import_seed_runner()

    try:
        validate_execute_args(execute=True, acknowledged=True, max_wallets=11)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("max_wallets > 10 should fail with SystemExit(2) even with acknowledgment")


def test_validate_execute_args_dry_run_is_noop():
    """Edge case: execute=False must be a no-op — no exception raised regardless of acknowledged."""
    _, validate_execute_args = _import_seed_runner()

    # Both combinations of acknowledged must be silent in dry-run mode.
    validate_execute_args(execute=False, acknowledged=False, max_wallets=100)
    validate_execute_args(execute=False, acknowledged=True, max_wallets=100)


# ---------------------------------------------------------------------------
# Findings #1 + #5 — paginated adapter reads + full gaps emission
# ---------------------------------------------------------------------------
#
# Reviewer block from 2026-05-17:
#   #1 (HIGH): ksg_flipper_population_check.py constructs BotSupabaseAdapter,
#     then ignores it and calls raw `_request(... limit=10000)` — the exact
#     defect PR #56's pagination fix closed in a different file. Re-introduces
#     silent undercount once wallet_flipper_tag_grain exceeds the PostgREST
#     `db-max-rows` cap.
#   #5 (MEDIUM): population JSON writes only {ok, near, thin_top[:20]};
#     candidate scoring reads {near + thin_top} only. Long-tail thin gaps
#     are unreachable for downstream scoring.
#
# Fix shape (from docs/fh4-peer-population-plan-fix branch):
#   - Factor out fetch_active_grain_rows(supabase, *, n_at_grain_floor,
#     protocol_version=None) that uses BotSupabaseAdapter.select with
#     where_ops={"n_at_grain": ("gte", N)}.
#   - Factor out build_gaps_payload(rows, ...) that emits the full `gaps`
#     list plus convenience ok/near/thin_top views.
#   - Add load_gap_rows_from_json(path) in candidate score that reads
#     the new `gaps` key (or falls back to a raw list for older outputs).


def test_fetch_active_grain_rows_uses_paginated_adapter_with_where_ops():
    """`fetch_active_grain_rows` must call `supabase.select` with
    `where_ops={"n_at_grain": ("gte", n_at_grain_floor)}` so PostgREST
    applies the floor server-side, and pagination is honored via the
    adapter. Direct `_request(... limit=10000)` is forbidden."""
    fetch_active_grain_rows, _build = _import_population_fetch_helpers()

    calls = []

    class _FakeAdapter:
        def select(self, table, *, where, columns=None, max_rows=None, where_ops=None):
            calls.append({
                "table": table,
                "where": dict(where),
                "where_ops": dict(where_ops or {}),
                "columns": tuple(columns) if columns else None,
            })
            return [
                {"wallet": "0xa", "grain": "leaf", "tag": "geopolitics",
                 "grain_roi": 0.1, "n_at_grain": 50, "protocol_version": "v2"},
            ]

    rows = fetch_active_grain_rows(_FakeAdapter(), n_at_grain_floor=30)

    assert len(calls) == 1
    call = calls[0]
    assert call["table"] == "wallet_flipper_tag_grain"
    assert call["where"]["status"] == "active"
    assert call["where"]["walk_full_history"] is True
    assert call["where_ops"]["n_at_grain"] == ("gte", 30)
    assert rows[0]["wallet"] == "0xa"


def test_fetch_active_grain_rows_passes_protocol_version_when_set():
    """When `protocol_version` is provided, it must appear in `where` as
    an equality filter (PostgREST `eq.<value>`)."""
    fetch_active_grain_rows, _build = _import_population_fetch_helpers()
    captured = {}

    class _FakeAdapter:
        def select(self, table, *, where, columns=None, max_rows=None, where_ops=None):
            captured["where"] = dict(where)
            return []

    fetch_active_grain_rows(_FakeAdapter(), n_at_grain_floor=30, protocol_version="v2")
    assert captured["where"].get("protocol_version") == "v2"


def test_fetch_active_grain_rows_omits_protocol_version_when_none():
    """`protocol_version=None` must NOT add a filter (otherwise it would
    filter to literal None and return zero rows)."""
    fetch_active_grain_rows, _build = _import_population_fetch_helpers()
    captured = {}

    class _FakeAdapter:
        def select(self, table, *, where, columns=None, max_rows=None, where_ops=None):
            captured["where"] = dict(where)
            return []

    fetch_active_grain_rows(_FakeAdapter(), n_at_grain_floor=30)
    assert "protocol_version" not in captured["where"]


def test_build_gaps_payload_emits_full_gaps_list_plus_views():
    """The payload must include a full `gaps` list (no truncation) AND
    the convenience views `ok` / `near` / `thin_top`. Downstream tools
    read `gaps`; the views are operator readability only."""
    fetch_active_grain_rows, build_gaps_payload = _import_population_fetch_helpers()

    # 30 ok wallets for tag A, 27 near for tag B, 50 thin tags each with 4 wallets.
    rows = []
    for i in range(30):
        rows.append({"wallet": f"0xok{i}", "grain": "leaf", "tag": "tagA",
                     "grain_roi": 0.1, "n_at_grain": 50, "protocol_version": "v2"})
    for i in range(27):
        rows.append({"wallet": f"0xnear{i}", "grain": "leaf", "tag": "tagB",
                     "grain_roi": 0.1, "n_at_grain": 50, "protocol_version": "v2"})
    for t in range(50):
        for i in range(4):
            rows.append({"wallet": f"0xthin{t}_{i}", "grain": "leaf", "tag": f"thinTag{t}",
                         "grain_roi": 0.1, "n_at_grain": 50, "protocol_version": "v2"})

    payload = build_gaps_payload(
        rows,
        target_peers=30,
        near_window=5,
        n_at_grain_floor=30,
        protocol_version=None,
        n_active_rows_baseline=len(rows),
    )

    # Full gaps list — must not drop any thin tags.
    assert len(payload["gaps"]) == 52  # 1 ok + 1 near + 50 thin
    # Sanity: every status is represented in the full list.
    statuses = {g["population_status"] for g in payload["gaps"]}
    assert statuses == {"ok", "near", "thin"}

    # Convenience views — `thin_top` may truncate, but `gaps` must not.
    thin_in_gaps = [g for g in payload["gaps"] if g["population_status"] == "thin"]
    assert len(thin_in_gaps) == 50
    assert len(payload["ok"]) == 1
    assert len(payload["near"]) == 1


def test_build_gaps_payload_has_required_contract_keys():
    """Pin the JSON contract so downstream tools can rely on the keys
    naming the right thing."""
    _fetch, build_gaps_payload = _import_population_fetch_helpers()
    payload = build_gaps_payload(
        [],
        target_peers=30,
        near_window=5,
        n_at_grain_floor=30,
        protocol_version=None,
        n_active_rows_baseline=0,
    )
    for key in (
        "target_peers", "near_window", "n_at_grain_floor",
        "protocol_version_filter", "n_active_rows_baseline",
        "n_distinct_grain_tags", "gaps", "ok", "near", "thin_top",
    ):
        assert key in payload, f"missing contract key {key!r}"


def test_load_gap_rows_from_json_reads_gaps_key_from_full_payload(tmp_path):
    """Modern population summary writes `{gaps: [...], ok: [...], ...}`.
    The candidate scorer must read the full `gaps` list, not the
    convenience views."""
    import json as _json
    load_gap_rows_from_json = _import_load_gap_rows_from_json()
    path = tmp_path / "gaps.json"
    path.write_text(_json.dumps({
        "target_peers": 30,
        "gaps": [
            {"grain": "leaf", "tag": "geopolitics",
             "population_status": "near", "peers_needed_to_ok": 2},
            {"grain": "leaf", "tag": "ai",
             "population_status": "thin", "peers_needed_to_ok": 27},
        ],
        "near": [],
        "thin_top": [],
    }))
    rows = load_gap_rows_from_json(str(path))
    tags = {r["tag"] for r in rows}
    assert tags == {"geopolitics", "ai"}


def test_load_gap_rows_from_json_accepts_raw_list_for_back_compat(tmp_path):
    """Legacy callers may pass a raw list of gap rows. Accept that shape
    too so older operator workflows don't break."""
    import json as _json
    load_gap_rows_from_json = _import_load_gap_rows_from_json()
    path = tmp_path / "gaps.json"
    payload = [
        {"grain": "leaf", "tag": "x", "population_status": "near", "peers_needed_to_ok": 1},
    ]
    path.write_text(_json.dumps(payload))
    rows = load_gap_rows_from_json(str(path))
    assert rows == payload


# ---------------------------------------------------------------------------
# Finding #3 — seed runner must propagate persist failures, not trust
# subprocess exit code alone.
#
# The analyzer's _persist_flipper_grains_helper emits a structured
# `[PERSIST_FLIPPER_GRAINS_RESULT] {"status": ...}` marker on stdout.
# Seed runner parses each child's stdout, accumulates per-wallet
# outcomes, and exits non-zero if any wallet reports `failed`. Also
# prints a final summary so operators see exactly what happened.
# ---------------------------------------------------------------------------


def _make_marker(status: str, wallet: str, **extra) -> str:
    import json as _json
    payload = {"status": status, "wallet": wallet, **extra}
    return f"[PERSIST_FLIPPER_GRAINS_RESULT] {_json.dumps(payload)}"


def test_seed_runner_exits_nonzero_when_any_wallet_persist_fails(monkeypatch, capsys):
    """A child subprocess that exits 0 but emits a `failed` marker must
    cause the seed runner itself to exit non-zero. The pre-fix runner
    trusted subprocess exit code only and missed this."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    outputs = iter([
        _make_marker("wrote", "0xa", rows_written=3, rows_superseded=0),
        _make_marker("failed", "0xb", reason="env_missing", detail="SUPABASE_URL unset"),
    ])

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout=next(outputs) + "\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    rc = mod.main([
        "--wallet", "0xa", "--wallet", "0xb",
        "--max-wallets", "2",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    assert rc != 0, "seed runner must exit non-zero when any child reports persist failure"


def test_seed_runner_exits_zero_when_all_children_wrote_or_skipped(monkeypatch):
    """Mixed wrote + legitimate-skip outcomes still mean operational
    success: the wallets either persisted or hit an idempotent no-op."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    outputs = iter([
        _make_marker("wrote", "0xa", rows_written=3, rows_superseded=0),
        _make_marker("skipped", "0xb", skipped_reason="idempotent — 4 rows already exist"),
    ])

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout=next(outputs) + "\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    rc = mod.main([
        "--wallet", "0xa", "--wallet", "0xb",
        "--max-wallets", "2",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    assert rc == 0


def test_seed_runner_emits_per_wallet_outcome_summary(monkeypatch, capsys):
    """End-of-run summary must list each wallet with its outcome status
    so an operator scrolling back through stderr can tell exactly what
    happened without re-parsing the marker stream."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    outputs = iter([
        _make_marker("wrote", "0xa", rows_written=3, rows_superseded=0),
        _make_marker("skipped", "0xb", skipped_reason="no flipper grains"),
        _make_marker("failed", "0xc", reason="writer_error", detail="boom"),
    ])

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout=next(outputs) + "\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    mod.main([
        "--wallet", "0xa", "--wallet", "0xb", "--wallet", "0xc",
        "--max-wallets", "3",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    # Every wallet's outcome must be visible somewhere in the runner's output.
    assert "0xa" in combined and "wrote" in combined
    assert "0xb" in combined and "skipped" in combined
    assert "0xc" in combined and "failed" in combined


def test_seed_runner_rejects_wrote_marker_with_zero_rows_defensively(monkeypatch, capsys):
    """Defensive layer: even if the analyzer ever emits a malformed
    `wrote` marker with rows_written=0 (or missing), the seed runner
    must not count it as success. The helper SHOULD have caught this
    upstream (zero_rows_written → failed), but the runner doesn't
    trust upstream invariants."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    outputs = iter([
        # rows_written = 0 — must NOT count as success even though
        # status says "wrote".
        _make_marker("wrote", "0xa", rows_written=0, rows_superseded=0),
    ])

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout=next(outputs) + "\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    rc = mod.main([
        "--wallet", "0xa",
        "--max-wallets", "1",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    assert rc != 0, (
        "wrote marker with rows_written=0 must not be treated as success"
    )


def test_seed_runner_rejects_wrote_marker_missing_rows_written_key(monkeypatch):
    """A `wrote` marker with no `rows_written` key at all is also a
    contract violation. Reject defensively."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    outputs = iter([
        # No rows_written key at all.
        _make_marker("wrote", "0xa"),
    ])

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout=next(outputs) + "\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    rc = mod.main([
        "--wallet", "0xa",
        "--max-wallets", "1",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    assert rc != 0


def test_seed_runner_exits_nonzero_when_marker_missing(monkeypatch):
    """A child that exits 0 but emits no marker at all is also a
    failure mode — the analyzer either didn't run --persist-flipper-grains
    or crashed before emitting. Seed runner must not trust silent
    success."""
    from scripts import ksg_seed_flipper_population as mod  # noqa: PLC0415
    import subprocess as _sp

    def fake_run(cmd, *args, **kwargs):
        return _sp.CompletedProcess(
            args=cmd, returncode=0,
            stdout="no marker here\n", stderr="",
        )

    monkeypatch.setattr(_sp, "run", fake_run)

    rc = mod.main([
        "--wallet", "0xa",
        "--max-wallets", "1",
        "--execute",
        "--i-understand-this-writes-wallet-flipper-tag-grain",
    ])
    assert rc != 0


def test_main_stdout_emits_full_gaps_contract(monkeypatch, capsys):
    """`main()` stdout must be the new gaps payload (with `gaps` key)
    so downstream pipes work without --json-out. Operator-readability
    summary (old `min_peers_floor` shape) is fine on stderr or in the
    markdown report, but must not pollute the canonical stdout JSON."""
    import json as _json
    from scripts import ksg_flipper_population_check as mod  # noqa: PLC0415

    rows = [
        {"wallet": f"0xok{i}", "grain": "leaf", "tag": "tagA",
         "grain_roi": 0.1, "n_at_grain": 50, "protocol_version": "v2"}
        for i in range(30)
    ]

    class _FakeAdapter:
        def select(self, table, *, where, columns=None, max_rows=None, where_ops=None):
            return rows

    monkeypatch.setattr(
        mod.BotSupabaseAdapter,
        "from_env_or_dotenv",
        classmethod(lambda cls: _FakeAdapter()),
    )

    rc = mod.main(["--target-peers", "30", "--near-window", "5"])
    assert rc == 0
    captured = capsys.readouterr()

    payload = _json.loads(captured.out)
    for key in ("gaps", "ok", "near", "thin_top", "target_peers"):
        assert key in payload, (
            f"stdout JSON missing contract key {key!r}; got: {sorted(payload)}"
        )
    assert isinstance(payload["gaps"], list)
    assert payload["target_peers"] == 30


def test_load_gap_rows_from_json_rejects_unknown_shape(tmp_path):
    """A payload that's neither a list nor a dict-with-`gaps` must raise
    rather than silently producing zero rows."""
    import json as _json
    load_gap_rows_from_json = _import_load_gap_rows_from_json()
    path = tmp_path / "gaps.json"
    path.write_text(_json.dumps({"oops": "no-gaps-key-here"}))
    import pytest
    with pytest.raises(ValueError, match="gaps"):
        load_gap_rows_from_json(str(path))


def test_build_seed_commands_max_exec_trades_none_omits_flag():
    """Edge case: when max_exec_trades is None, --max-exec-trades must NOT appear in the command."""
    build_seed_commands, _ = _import_seed_runner()

    commands = build_seed_commands(
        wallets=["0xccc"],
        n_iter=200,
        max_exec_trades=None,
    )
    assert len(commands) == 1
    cmd = commands[0]
    assert "--max-exec-trades" not in cmd
    # Core flags must still be present.
    assert "--no-persist" in cmd
    assert "--persist-flipper-grains" in cmd
    assert "--n-iter" in cmd
    assert "200" in cmd
