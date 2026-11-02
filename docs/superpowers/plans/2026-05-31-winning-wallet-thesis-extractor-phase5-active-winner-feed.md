# Winning-Wallet Thesis Extractor - Phase 5 Active Winner Feed Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a controlled active-winner intake loop after the extractor is built, so the analyzer can process many current winners without reverting to Becker-first null mining or copy-bot framing.

**Architecture:** Intake is separate from thesis extraction. The feed supplies candidate wallets with a transparent reason (`curated`, `active_winner`, `operator_watchlist`) and enough activity metadata to avoid leaderboard-only survivorship whenever possible. The extractor runs one wallet at a time, writes profiles/artifacts, and optionally queues validation plans; it does not trade.

**Tech Stack:** Python 3, JSONL checkpoints, existing wallet fetchers, `sniper.analysis.winning_wallet_thesis` CLI, optional Supabase brain write only with explicit operator flag.

**Depends on:** Phase 3 complete. Phase 4 improves signal coverage but is not required for this feed.

---

## Scope

Build:

1. candidate source contract;
2. curated/watchlist candidate source;
3. active-winner source wrapper with explicit survivorship label;
4. batch runner for `run_winning_wallet_thesis.py`;
5. profile artifact store;
6. counts-only summary;
7. optional brain persistence behind `--persist-brain`.

No production DB mutation by default. No live trading. No broad random-wallet claims.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/winning_wallet_thesis/intake.py` | `CandidateWallet`, source protocols, curated JSON source. |
| `sniper/analysis/winning_wallet_thesis/batch.py` | Run extractor over candidates with checkpointing. |
| `scripts/run_winning_wallet_thesis_batch.py` | CLI wrapper. |
| `tests/winning_wallet_thesis/test_intake.py` | Candidate source tests. |
| `tests/winning_wallet_thesis/test_batch.py` | Batch/checkpoint tests. |

---

## Task 1: Candidate source contract

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/intake.py`
- Test: `tests/winning_wallet_thesis/test_intake.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet, load_curated_candidates


def test_candidate_wallet_carries_source_and_survivorship_label():
    c = CandidateWallet(wallet="0xabc", source="curated", selection_note="operator supplied", survivorship_risk="high")
    assert c.wallet == "0xabc"
    assert c.survivorship_risk == "high"


def test_load_curated_candidates_from_json(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text('[{"wallet": "0xabc", "selection_note": "SwissMiss style"}]')
    out = load_curated_candidates(str(path))
    assert out[0].source == "curated"
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_intake.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement intake**

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CandidateWallet:
    wallet: str
    source: str
    selection_note: str
    survivorship_risk: str = "unknown"


class CandidateSource(Protocol):
    def candidates(self) -> list[CandidateWallet]:
        ...


def load_curated_candidates(path: str) -> list[CandidateWallet]:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return [
        CandidateWallet(
            wallet=str(row["wallet"]),
            source=str(row.get("source", "curated")),
            selection_note=str(row.get("selection_note", "")),
            survivorship_risk=str(row.get("survivorship_risk", "high")),
        )
        for row in raw
    ]
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_intake.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/intake.py tests/winning_wallet_thesis/test_intake.py
git commit -m "feat(thesis): add active-winner intake contract"
```

---

## Task 2: Batch checkpoint runner

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/batch.py`
- Test: `tests/winning_wallet_thesis/test_batch.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.batch import InMemoryProfileStore, run_wallet_batch
from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet


def test_batch_skips_done_wallets_on_rerun():
    calls = []
    def run(wallet):
        calls.append(wallet)
        return {"wallet": wallet, "headline": "ok"}
    store = InMemoryProfileStore()
    candidates = [CandidateWallet("0x1", "curated", ""), CandidateWallet("0x2", "curated", "")]
    run_wallet_batch(candidates, run_profile=run, store=store)
    run_wallet_batch(candidates, run_profile=run, store=store)
    assert calls == ["0x1", "0x2"]
```

- [ ] **Step 2: Run failing tests**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_batch.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement batch**

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from sniper.analysis.winning_wallet_thesis.intake import CandidateWallet


@dataclass
class InMemoryProfileStore:
    profiles: dict[str, dict] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)

    def done_wallets(self) -> set[str]:
        return {wallet for wallet, status in self.statuses.items() if status == "done"}

    def record_done(self, wallet: str, profile: dict) -> None:
        self.profiles[wallet] = profile
        self.statuses[wallet] = "done"

    def record_failed(self, wallet: str, error: str) -> None:
        self.statuses[wallet] = f"failed:{error}"


def run_wallet_batch(
    candidates: list[CandidateWallet],
    *,
    run_profile: Callable[[str], dict[str, Any]],
    store: InMemoryProfileStore,
    max_wallets: int | None = None,
) -> dict[str, int]:
    counts = {"seen": 0, "ran": 0, "done": 0, "failed": 0, "skipped": 0}
    done = store.done_wallets()
    selected = candidates if max_wallets is None else candidates[:max_wallets]
    for candidate in selected:
        counts["seen"] += 1
        if candidate.wallet in done:
            counts["skipped"] += 1
            continue
        try:
            counts["ran"] += 1
            profile = run_profile(candidate.wallet)
            profile["intake"] = {
                "source": candidate.source,
                "selection_note": candidate.selection_note,
                "survivorship_risk": candidate.survivorship_risk,
            }
            store.record_done(candidate.wallet, profile)
            counts["done"] += 1
        except Exception as exc:  # noqa: BLE001
            store.record_failed(candidate.wallet, str(exc))
            counts["failed"] += 1
    return counts
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_batch.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/batch.py tests/winning_wallet_thesis/test_batch.py
git commit -m "feat(thesis): add wallet thesis batch runner"
```

---

## Task 3: Batch CLI

**Files:**
- Create: `scripts/run_winning_wallet_thesis_batch.py`
- Modify: `sniper/analysis/winning_wallet_thesis/batch.py`
- Test: `tests/winning_wallet_thesis/test_batch_cli.py`

- [ ] **Step 1: Write failing CLI smoke test**

```python
import subprocess
import sys


def test_batch_cli_help():
    out = subprocess.run(
        [sys.executable, "scripts/run_winning_wallet_thesis_batch.py", "--help"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--candidates" in out.stdout
    assert "--max-wallets" in out.stdout
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_batch_cli.py -q`

Expected: script missing failure.

- [ ] **Step 3: Implement script**

```python
#!/usr/bin/env python3
from sniper.analysis.winning_wallet_thesis.batch_cli import main

if __name__ == "__main__":
    main()
```

Create `sniper/analysis/winning_wallet_thesis/batch_cli.py`:

```python
from __future__ import annotations

import argparse
import json

from sniper.analysis.winning_wallet_thesis.batch import InMemoryProfileStore, run_wallet_batch
from sniper.analysis.winning_wallet_thesis.intake import load_curated_candidates


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--max-wallets", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", default=True)
    args = parser.parse_args()
    candidates = load_curated_candidates(args.candidates)
    store = InMemoryProfileStore()
    counts = run_wallet_batch(
        candidates,
        run_profile=lambda wallet: {"wallet": wallet, "headline": "dry_run_placeholder"},
        store=store,
        max_wallets=args.max_wallets,
    )
    print(json.dumps(counts, sort_keys=True))
```

Keep the first CLI dry-run only. Wire real profile execution after Phase 3 profile serialization stabilizes.

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_batch_cli.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_winning_wallet_thesis_batch.py sniper/analysis/winning_wallet_thesis/batch_cli.py tests/winning_wallet_thesis/test_batch_cli.py
git commit -m "feat(thesis): add active-winner batch CLI"
```

## Phase 5 Exit Criteria

- Intake source is explicit and tagged with survivorship risk.
- Batch runner is checkpointed and idempotent.
- Dry-run is default.
- Persistence requires explicit flags in a later patch.
- No trade advice, no live execution, no implicit DB mutation.
