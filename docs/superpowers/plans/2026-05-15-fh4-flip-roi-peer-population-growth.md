# FH4 Flip-ROI Peer Population Growth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Grow `wallet_flipper_tag_grain` peer coverage so more FH4 flip-ROI tag grains clear the `peer_count >= 30` confidence threshold without lowering inference standards.

**Architecture:** Keep Phase 2 inference unchanged. Add read-only population-gap and candidate-scoring tools first, then add an operator-gated seed runner that only writes when explicitly passed `--execute`. Use existing likely-wallet sources, especially `wallet_tag_pnl` and prior analyzer candidates, only as address queues; every contributing row must come from the current wallet protocol and the `wallet_flipper_tag_grain` writer.

**Tech Stack:** Python scripts, existing `BotSupabaseAdapter`, `scripts/run_wallet_protocol.py --persist-flipper-grains`, Supabase table `wallet_flipper_tag_grain`, schema validator.

---

## Scope

This is Path A: grow the peer population. Do not change FH4 inference math, BH pooling, `peer_count` thresholds, verdict mapping, or `specialist_evidence` doctrine.

No live analyzer batch runs happen while implementing this plan. The shipped code should default to read-only dry-run behavior. Actual population seeding is a separate operator action using an explicit `--execute` flag and a small `--max-wallets` cap.

## Files

- Modify: `scripts/ksg_flipper_population_check.py`
  - Add target-threshold reporting for `peer_count >= 30`.
  - Emit `peers_needed_to_ok`, not only `min_peers` qualification.
  - Add JSON output path support for downstream candidate scoring.
  - Replace the direct `_request(...&limit=10000)` read with paginated
    `BotSupabaseAdapter.select(...)` so the report cannot silently undercount
    once `wallet_flipper_tag_grain` grows past a single PostgREST page.

- Create: `scripts/ksg_flipper_candidate_score.py`
  - Read candidate wallet addresses from `wallet_tag_pnl`, a newline file, or explicit CLI args.
  - Score candidates against near-target `(grain, tag)` gaps using existing local/DB metadata only.
  - Produce an ordered seed queue. No analyzer runs. No DB writes.

- Create: `scripts/ksg_seed_flipper_population.py`
  - Dry-run by default: print the exact per-wallet `run_wallet_protocol.py` commands.
  - With `--execute`, run at most `--max-wallets` current-protocol analyses with `--persist-flipper-grains`.
  - Hard require `--i-understand-this-writes-wallet-flipper-tag-grain` when `--execute` is used.

- Test: `tests/test_ksg_flipper_population_tools.py`
  - Unit tests for gap calculation, candidate scoring, dry-run command generation, and execute guardrails.

- Docs: `docs/research/wallet_intelligence_workflow/2026-05-15-fh4-peer-population-growth-path-a.md`
  - Operational runbook for Task #83.

## Invariants

- Do not lower `peer_count >= 30` for `confidence_flag == "ok"`.
- Do not treat `peer_count 10-29` as promotable specialist evidence.
- Do not write to Supabase unless the operator passes both `--execute` and the write acknowledgment flag.
- Do not run broad analyzer batches from tests.
- Do not use legacy low-buy, insider-score, or copy-trading framing.
- Do not mutate `overall_verdict`, `gate_result`, `primary_edge_type`, or `copyability_note`.

---

### Task 1: Population Gap Report

**Files:**
- Modify: `scripts/ksg_flipper_population_check.py`
- Test: `tests/test_ksg_flipper_population_tools.py`

- [ ] **Step 1: Write failing tests for gap classification**

Add helpers to the new test file:

```python
from scripts.ksg_flipper_population_check import classify_population_gaps


def test_classify_population_gaps_marks_ok_near_and_thin():
    rows = [
        *[{"wallet": f"0xgeo{i}", "grain": "leaf", "tag": "geopolitics", "grain_roi": 0.1, "n_at_grain": 30, "protocol_version": "v2"} for i in range(30)],
        *[{"wallet": f"0xpol{i}", "grain": "leaf", "tag": "politics", "grain_roi": 0.1, "n_at_grain": 30, "protocol_version": "v2"} for i in range(27)],
        *[{"wallet": f"0xai{i}", "grain": "leaf", "tag": "ai", "grain_roi": 0.1, "n_at_grain": 30, "protocol_version": "v2"} for i in range(4)],
    ]

    result = classify_population_gaps(rows, target_peers=30, near_window=5)

    by_tag = {(r["grain"], r["tag"]): r for r in result}
    assert by_tag[("leaf", "geopolitics")]["population_status"] == "ok"
    assert by_tag[("leaf", "geopolitics")]["peers_needed_to_ok"] == 0
    assert by_tag[("leaf", "politics")]["population_status"] == "near"
    assert by_tag[("leaf", "politics")]["peers_needed_to_ok"] == 3
    assert by_tag[("leaf", "ai")]["population_status"] == "thin"
    assert by_tag[("leaf", "ai")]["peers_needed_to_ok"] == 26
```

- [ ] **Step 2: Run the failing test**

Run:

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py::test_classify_population_gaps_marks_ok_near_and_thin -q
```

Expected: fail because `classify_population_gaps` does not exist.

- [ ] **Step 3: Implement `classify_population_gaps`**

Move the existing local aggregation in `scripts/ksg_flipper_population_check.py` behind a pure helper:

```python
def classify_population_gaps(
    rows: list[dict],
    *,
    target_peers: int = 30,
    near_window: int = 5,
) -> list[dict]:
    by_grain_tag: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        key = (r["grain"], r["tag"])
        by_grain_tag.setdefault(key, []).append(r)

    out: list[dict] = []
    for (grain, tag), wallet_rows in by_grain_tag.items():
        wallets = sorted({r["wallet"] for r in wallet_rows})
        n_peers = len(wallets)
        mean_roi = sum(float(r["grain_roi"]) for r in wallet_rows) / len(wallet_rows)
        peers_needed = max(0, target_peers - n_peers)
        if n_peers >= target_peers:
            status = "ok"
        elif peers_needed <= near_window:
            status = "near"
        else:
            status = "thin"
        out.append({
            "grain": grain,
            "tag": tag,
            "n_peers": n_peers,
            "target_peers": target_peers,
            "peers_needed_to_ok": peers_needed,
            "population_status": status,
            "mean_flip_roi": round(mean_roi, 6),
            "wallets": wallets,
        })
    return sorted(out, key=lambda r: (r["peers_needed_to_ok"], -r["n_peers"], r["grain"], r["tag"]))
```

- [ ] **Step 4: Replace the direct PostgREST read with paginated adapter reads**

The current script uses `bot.supabase_helper._request` with `limit=10000`.
Do not keep that path. It bypasses the `BotSupabaseAdapter.select(...)`
Range-header pagination added for Codex deep-review finding #5 and can
silently undercount peer rows as the table grows.

Current `BotSupabaseAdapter.select(...)` is equality-filter only. Do not fake
operator filters by passing values like `"gte.30"` through `where`; that would
produce an equality predicate, not a `gte` predicate.

Preferred implementation: extend `BotSupabaseAdapter.select(...)` with a tested
operator-filter argument and keep the Range-header pagination centralized:

```python
supabase = BotSupabaseAdapter.from_env_or_dotenv()
rows = supabase.select(
    "wallet_flipper_tag_grain",
    where={
        "status": "active",
        "walk_full_history": True,
        **({"protocol_version": args.protocol_version} if args.protocol_version else {}),
    },
    where_ops={"n_at_grain": ("gte", args.n_at_grain_floor)},
    columns=("wallet", "grain", "tag", "grain_roi", "n_at_grain", "protocol_version"),
)
```

Acceptable fallback: add a small local helper in
`ksg_flipper_population_check.py` that builds the operator-filter read path and
calls the adapter's helper with Range headers. Do not fall back to a one-shot
`_request` with a large `limit`.

- [ ] **Step 5: Wire CLI flags and JSON contract**

Add:

```python
p.add_argument("--target-peers", type=int, default=30)
p.add_argument("--near-window", type=int, default=5)
p.add_argument("--json-out", default=None)
```

Use `classify_population_gaps(rows, target_peers=args.target_peers, near_window=args.near_window)`.

The stdout and `--json-out` payload must use this contract:

```json
{
  "target_peers": 30,
  "near_window": 5,
  "n_at_grain_floor": 30,
  "protocol_version_filter": null,
  "n_active_rows_baseline": 0,
  "n_distinct_grain_tags": 0,
  "gaps": [],
  "ok": [],
  "near": [],
  "thin_top": []
}
```

`gaps` is the full flat list from `classify_population_gaps`. `ok`, `near`,
and `thin_top` are derived views. Downstream tools must read `gaps`; the
summary sections are operator convenience only.

- [ ] **Step 6: Verify**

Run:

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py -q
python3 scripts/validate_schema_refs.py
```

Expected: tests pass; schema validator reports no violations.

- [ ] **Step 7: Commit**

```bash
git add scripts/ksg_flipper_population_check.py tests/test_ksg_flipper_population_tools.py
git commit -m "feat(wallet-protocol): report FH4 peer population gaps"
```

---

### Task 2: Candidate Queue Scorer

**Files:**
- Create: `scripts/ksg_flipper_candidate_score.py`
- Modify: `tests/test_ksg_flipper_population_tools.py`

- [ ] **Step 1: Write failing tests for candidate scoring**

Add:

```python
import json
import sqlite3

from scripts.ksg_flipper_candidate_score import (
    load_gap_rows_from_json,
    load_wallet_tag_pnl_candidates,
    score_candidate_wallets,
)


def test_score_candidate_wallets_prioritizes_near_target_tags():
    gaps = [
        {"grain": "leaf", "tag": "geopolitics", "population_status": "near", "peers_needed_to_ok": 2},
        {"grain": "leaf", "tag": "ai", "population_status": "thin", "peers_needed_to_ok": 20},
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
        "INSERT INTO wallet_tag_pnl(wallet, tag, n_positions, realized_pnl, win_rate, conviction_concentration, computed_at) "
        "VALUES ('0xAAA', 'geopolitics', 50, 1000.0, 0.7, 0.2, 1)"
    )
    conn.commit()
    conn.close()

    rows = load_wallet_tag_pnl_candidates(str(db_path), tags=["geopolitics"])

    assert rows == [{
        "wallet": "0xaaa",
        "tag": "geopolitics",
        "n_positions": 50,
        "realized_pnl": 1000.0,
    }]


def test_load_gap_rows_from_json_accepts_population_summary_contract(tmp_path):
    path = tmp_path / "gaps.json"
    path.write_text(json.dumps({
        "target_peers": 30,
        "gaps": [
            {"grain": "leaf", "tag": "geopolitics", "population_status": "near", "peers_needed_to_ok": 2}
        ],
        "near": [],
    }))

    rows = load_gap_rows_from_json(str(path))

    assert rows == [
        {"grain": "leaf", "tag": "geopolitics", "population_status": "near", "peers_needed_to_ok": 2}
    ]
```

- [ ] **Step 2: Run the failing test**

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py::test_score_candidate_wallets_prioritizes_near_target_tags -q
```

Expected: fail because the module does not exist.

- [ ] **Step 3: Implement the pure scorer**

Create `scripts/ksg_flipper_candidate_score.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict


def score_candidate_wallets(candidate_rows: list[dict], gaps: list[dict]) -> list[dict]:
    gap_by_tag = {g["tag"]: g for g in gaps if g.get("population_status") in {"near", "thin"}}
    by_wallet: dict[str, dict] = defaultdict(lambda: {"wallet": "", "score": 0.0, "matched_target_tags": []})
    for row in candidate_rows:
        tag = row.get("tag")
        gap = gap_by_tag.get(tag)
        if gap is None:
            continue
        wallet = str(row["wallet"]).lower()
        entry = by_wallet[wallet]
        entry["wallet"] = wallet
        n_positions = float(row.get("n_positions") or row.get("n_held") or 0)
        total_pnl = abs(float(row.get("total_pnl") or row.get("realized_pnl") or 0))
        scarcity = 1.0 / max(1, int(gap["peers_needed_to_ok"]))
        entry["score"] += scarcity * (1.0 + min(n_positions, 100.0) / 100.0) * (1.0 + min(total_pnl, 100000.0) / 100000.0)
        entry["matched_target_tags"].append(tag)
    scored = list(by_wallet.values())
    for entry in scored:
        entry["matched_target_tags"] = sorted(set(entry["matched_target_tags"]))
        entry["score"] = round(float(entry["score"]), 6)
    return sorted(scored, key=lambda r: (-r["score"], r["wallet"]))


def load_gap_rows_from_json(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("gaps"), list):
        return payload["gaps"]
    raise ValueError("--gaps-json must be a raw gap list or a population summary with a 'gaps' list")


def load_wallet_tag_pnl_candidates(db_path: str, *, tags: list[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in tags)
    sql = (
        "SELECT wallet, tag, n_positions, realized_pnl "
        "FROM wallet_tag_pnl "
        f"WHERE tag IN ({placeholders}) "
        "ORDER BY ABS(realized_pnl) DESC, n_positions DESC"
    )
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(sql, tags).fetchall()
    finally:
        conn.close()
    return [
        {
            "wallet": str(r["wallet"]).lower(),
            "tag": r["tag"],
            "n_positions": int(r["n_positions"]),
            "realized_pnl": float(r["realized_pnl"]),
        }
        for r in rows
    ]
```

- [ ] **Step 4: Add CLI**

The CLI accepts:

```text
--gaps-json <path>
--candidate-json <path>
--wallet-tag-pnl-db sniper_tracker.db
--out <path>
--limit 50
```

It reads JSON arrays and/or `wallet_tag_pnl`, calls `score_candidate_wallets`, prints JSON, and writes `--out` when provided. It does not import analyzer code and does not call Supabase.

`--gaps-json` must use `load_gap_rows_from_json(...)` so it accepts both the
Task 1 population summary contract and a raw gap list in tests.

- [ ] **Step 5: Verify**

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py -q
python3 scripts/validate_schema_refs.py
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ksg_flipper_candidate_score.py tests/test_ksg_flipper_population_tools.py
git commit -m "feat(wallet-protocol): score FH4 peer population seed candidates"
```

---

### Task 3: Operator-Gated Seed Runner

**Files:**
- Create: `scripts/ksg_seed_flipper_population.py`
- Modify: `tests/test_ksg_flipper_population_tools.py`

- [ ] **Step 1: Write failing tests for dry-run and execute guard**

Add:

```python
from scripts.ksg_seed_flipper_population import build_seed_commands, validate_execute_args


def test_build_seed_commands_are_persist_flipper_grain_runs():
    commands = build_seed_commands(
        wallets=["0xaaa", "0xbbb"],
        n_iter=500,
        max_exec_trades=100,
    )
    assert commands == [
        ["python3", "scripts/run_wallet_protocol.py", "0xaaa", "--no-persist", "--persist-flipper-grains", "--n-iter", "500", "--max-exec-trades", "100"],
        ["python3", "scripts/run_wallet_protocol.py", "0xbbb", "--no-persist", "--persist-flipper-grains", "--n-iter", "500", "--max-exec-trades", "100"],
    ]


def test_execute_requires_explicit_write_acknowledgment():
    try:
        validate_execute_args(execute=True, acknowledged=False, max_wallets=5)
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("execute without acknowledgment should fail")
```

- [ ] **Step 2: Run the failing tests**

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py::test_build_seed_commands_are_persist_flipper_grain_runs tests/test_ksg_flipper_population_tools.py::test_execute_requires_explicit_write_acknowledgment -q
```

Expected: fail because the module does not exist.

- [ ] **Step 3: Implement dry-run command builder**

Create `scripts/ksg_seed_flipper_population.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def build_seed_commands(
    *,
    wallets: list[str],
    n_iter: int,
    max_exec_trades: int | None,
) -> list[list[str]]:
    commands: list[list[str]] = []
    for wallet in wallets:
        cmd = [
            "python3", "scripts/run_wallet_protocol.py", wallet.lower(),
            "--no-persist",
            "--persist-flipper-grains",
            "--n-iter", str(n_iter),
        ]
        if max_exec_trades is not None:
            cmd.extend(["--max-exec-trades", str(max_exec_trades)])
        commands.append(cmd)
    return commands


def validate_execute_args(*, execute: bool, acknowledged: bool, max_wallets: int) -> None:
    if execute and not acknowledged:
        raise SystemExit(2)
    if execute and max_wallets > 10:
        raise SystemExit(2)
```

The `--no-persist` flag here means "do not write the broad legacy run result"; `--persist-flipper-grains` is the narrow explicit write surface.

- [ ] **Step 4: Add CLI**

CLI arguments:

```text
--queue-json <path>          # list of {"wallet": "..."} objects from candidate scorer
--wallet <address>           # repeatable manual wallet
--max-wallets 3              # default 3, hard cap 10
--n-iter 500
--max-exec-trades 100
--execute                    # default false
--i-understand-this-writes-wallet-flipper-tag-grain
```

Dry-run prints commands and exits 0. Execute mode runs each command with `subprocess.run(cmd, check=False)` and stops on first non-zero exit.

- [ ] **Step 5: Verify**

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py -q
python3 scripts/validate_schema_refs.py
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ksg_seed_flipper_population.py tests/test_ksg_flipper_population_tools.py
git commit -m "feat(wallet-protocol): add gated FH4 peer population seeding runner"
```

---

### Task 4: Runbook

**Files:**
- Create: `docs/research/wallet_intelligence_workflow/2026-05-15-fh4-peer-population-growth-path-a.md`

- [ ] **Step 1: Write the runbook**

Include:

```markdown
# FH4 Peer Population Growth — Path A

Status: operational plan; no threshold change.

Goal: grow `wallet_flipper_tag_grain` so FH4 flip-ROI inference clears `peer_count >= 30` for more `(grain, tag)` pairs.

Doctrine:
- This is evidence infrastructure, not copy trading.
- Do not lower the confidence threshold to manufacture evidence.
- `peer_count 10-29` can run but remains low-population and cannot become specialist_evidence alone.
- No surface alone creates a trade recommendation.

Operator flow:
1. Run the population gap report at the 30-peer target.
2. Score seed candidates against near-target tags.
3. Review the first 3-10 wallets manually.
4. Run the seed runner in dry-run mode.
5. Execute only the reviewed queue with the write acknowledgment flag.
6. Re-run the population gap report.
7. Only then re-run targeted fixture/card review for wallets blocked on FH4 population.

Stop rules:
- Any wallet reports `walk_full_history=false`.
- The writer skips for schema or fixture-hash reasons.
- A target tag does not gain peer count after two reviewed wallets.
- Runs hit API/Goldsky rate limits.
- The candidate wallet is obviously correlated with an existing peer cluster.
```

- [ ] **Step 2: Verify docs**

```bash
python3 scripts/validate_schema_refs.py
```

Expected: schema clean.

- [ ] **Step 3: Commit**

```bash
git add docs/research/wallet_intelligence_workflow/2026-05-15-fh4-peer-population-growth-path-a.md
git commit -m "docs: document FH4 peer population growth path"
```

---

### Task 5: Final Verification

- [ ] **Step 1: Run focused tests**

```bash
python3 -m pytest tests/test_ksg_flipper_population_tools.py tests/test_wallet_protocol_fh4_flip_roi_inference.py tests/test_wallet_flipper_tag_grain_writer.py -q
```

Expected: pass.

- [ ] **Step 2: Run schema validation**

```bash
python3 scripts/validate_schema_refs.py
```

Expected: `No schema violations found.`

- [ ] **Step 3: Check status**

```bash
git status --short --branch
git log --oneline -5
```

Expected: clean branch, three or four commits ahead of `main`.

## Acceptance Criteria

- Population-gap tool distinguishes `ok`, `near`, and `thin` using a configurable `target_peers`, default 30.
- Candidate scorer ranks wallets by ability to help near-target tags, without running analyzer or writing DB.
- Seed runner is dry-run by default and cannot write without explicit `--execute` plus acknowledgment.
- No confidence threshold is lowered.
- Existing FH4 inference tests still pass.
- Documentation states this is evidence infrastructure, not a trade/recommendation surface.

## Follow-Up After This Plan Lands

After merge, an operator can run the flow manually:

```bash
python3 scripts/ksg_flipper_population_check.py --target-peers 30 --json-out /tmp/fh4_gaps.json
python3 scripts/ksg_flipper_candidate_score.py --gaps-json /tmp/fh4_gaps.json --candidate-json /tmp/candidate_wallets.json --out /tmp/fh4_seed_queue.json
python3 scripts/ksg_seed_flipper_population.py --queue-json /tmp/fh4_seed_queue.json --max-wallets 3
python3 scripts/ksg_seed_flipper_population.py --queue-json /tmp/fh4_seed_queue.json --max-wallets 3 --execute --i-understand-this-writes-wallet-flipper-tag-grain
python3 scripts/ksg_flipper_population_check.py --target-peers 30
```

The execute command is operator-gated and writes only through `wallet_flipper_tag_grain`.
