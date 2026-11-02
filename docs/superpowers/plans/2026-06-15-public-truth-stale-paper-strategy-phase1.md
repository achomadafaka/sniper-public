# Public-Truth Stale Paper Strategy Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the offline/manual paper loop for Public-Truth Stale Paper Strategy v0: manually captured source observations plus saved orderbook snapshots become lane-segmented paper entries and reports.

**Architecture:** No live scanner, no production DB writes, no migrations, no order logic. The first slice creates reusable pure-Python types, depth-weighted pricing math, manual CSV loading, and a CLI that scores manually captured observations from local files.

**Tech Stack:** Python 3.12, stdlib `csv/json/dataclasses/argparse`, pytest.

**Spec:** `docs/superpowers/specs/2026-06-15-public-truth-stale-paper-strategy-design.md`
**Research:** `docs/research/2026-06-15-public-truth-stale-source-research.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `docs/research/public_truth_stale/manual_signals_template.csv` | Manual paper log template for 5-10 initial signals |
| `docs/research/public_truth_stale/manual_signals.csv` | Active manual paper ledger, initialized empty |
| `docs/research/public_truth_stale/manual_report.md` | Generated lane-segmented paper deployment report |
| `docs/research/public_truth_stale/source_accounts_seed.json` | Manual seed registry for official/social/trusted accounts, no DB |
| `docs/research/public_truth_stale/examples/orderbook_signal_001.json` | Example Polymarket orderbook snapshot fixture |
| `sniper/analysis/public_truth_stale/__init__.py` | Package exports |
| `sniper/analysis/public_truth_stale/types.py` | Enums and dataclasses for lanes, observations, entries, confirmations |
| `sniper/analysis/public_truth_stale/pricing.py` | Pure depth-weighted entry-price and net-edge math |
| `sniper/analysis/public_truth_stale/manual_log.py` | CSV loader and validation for manually captured observations |
| `sniper/analysis/public_truth_stale/report.py` | Lane-segmented markdown report builder |
| `scripts/public_truth_stale_score_manual.py` | Offline CLI: CSV + local orderbook JSON -> paper report |
| `tests/test_public_truth_stale_pricing.py` | Pricing math tests |
| `tests/test_public_truth_stale_manual_log.py` | CSV parsing and validation tests |
| `tests/test_public_truth_stale_report.py` | Lane segmentation and claim-boundary tests |
| `tests/test_public_truth_stale_score_manual_cli.py` | CLI smoke test with local fixtures |

---

## Task 1: Manual Research Scaffolds

**Files:**
- Create: `docs/research/public_truth_stale/manual_signals_template.csv`
- Create: `docs/research/public_truth_stale/source_accounts_seed.json`
- Create: `docs/research/public_truth_stale/examples/orderbook_signal_001.json`

- [ ] **Step 1: Create directories**

Run:

```bash
mkdir -p docs/research/public_truth_stale/examples
```

- [ ] **Step 2: Add manual signal template**

Create `docs/research/public_truth_stale/manual_signals_template.csv`:

```csv
signal_id,market_url,market_id,condition_id,token_id,outcome,rule_text_hash,resolution_source_url,source_lane,source_family,source_account_id,source_url,source_timestamp,fetch_timestamp,observed_source_value,rule_match_status,finality_status,confirmation_status,stake_usdc,modeled_cost_per_share,orderbook_file,notes
signal_001,https://polymarket.com/event/example,example_market,example_condition,example_token,YES,sha256:example,https://official.example/final,resolution_authoritative,sports_official,,https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,home_team_won,exact_match,official_final,still_unconfirmed,20,0.005,examples/orderbook_signal_001.json,example row only
```

- [ ] **Step 3: Add source-account seed registry**

Create `docs/research/public_truth_stale/source_accounts_seed.json`:

```json
[
  {
    "account_id": "coinbase_assets_x",
    "handle": "CoinbaseAssets",
    "display_name": "Coinbase Assets",
    "source_tier": "official_social_source",
    "account_class": "official_exchange",
    "official_url_crosslink": "https://www.coinbase.com/blog/increasing-transparency-for-new-asset-listings-on-coinbase",
    "approved_market_categories": ["crypto_exchange_listing"],
    "reliability_notes": "Official social lead only. Settlement source depends on market rules.",
    "active": true,
    "created_at": "2026-06-15T00:00:00Z",
    "updated_at": "2026-06-15T00:00:00Z"
  },
  {
    "account_id": "krakenfx_x",
    "handle": "krakenfx",
    "display_name": "Kraken",
    "source_tier": "official_social_source",
    "account_class": "official_exchange",
    "official_url_crosslink": "https://www.kraken.com/listings",
    "approved_market_categories": ["crypto_exchange_listing"],
    "reliability_notes": "Kraken listings page points users to this X account for new listings.",
    "active": true,
    "created_at": "2026-06-15T00:00:00Z",
    "updated_at": "2026-06-15T00:00:00Z"
  }
]
```

- [ ] **Step 4: Add example orderbook fixture**

Create `docs/research/public_truth_stale/examples/orderbook_signal_001.json`:

```json
{
  "market": "example_condition",
  "asset_id": "example_token",
  "timestamp": "2026-06-15T21:00:12Z",
  "bids": [
    {"price": "0.62", "size": "100"}
  ],
  "asks": [
    {"price": "0.70", "size": "10"},
    {"price": "0.75", "size": "40"}
  ],
  "min_order_size": "5",
  "tick_size": "0.01",
  "neg_risk": false,
  "hash": "example_book_hash"
}
```

- [ ] **Step 5: Commit scaffold**

```bash
git add docs/research/public_truth_stale
git commit -m "docs: add public-truth stale manual paper scaffolds"
```

---

## Task 2: Depth-Weighted Pricing Math

**Files:**
- Create: `sniper/analysis/public_truth_stale/__init__.py`
- Create: `sniper/analysis/public_truth_stale/pricing.py`
- Create: `tests/test_public_truth_stale_pricing.py`

- [ ] **Step 1: Write failing pricing tests**

Create `tests/test_public_truth_stale_pricing.py`:

```python
import pytest

from sniper.analysis.public_truth_stale.pricing import (
    InsufficientDepthError,
    depth_weighted_buy_price,
    net_edge,
)


def test_depth_weighted_buy_price_walks_ask_depth_for_dollar_stake():
    asks = [
        {"price": "0.50", "size": "10"},
        {"price": "0.75", "size": "30"},
    ]

    fill = depth_weighted_buy_price(asks, stake_usdc=20.0)

    assert fill.cost_usdc == pytest.approx(20.0)
    assert fill.shares == pytest.approx(30.0)
    assert fill.avg_price == pytest.approx(20.0 / 30.0)
    assert fill.levels_consumed == 2
    assert fill.unfilled_usdc == pytest.approx(0.0)


def test_depth_weighted_buy_price_rejects_insufficient_depth():
    asks = [{"price": "0.50", "size": "10"}]

    with pytest.raises(InsufficientDepthError):
        depth_weighted_buy_price(asks, stake_usdc=20.0)


def test_net_edge_uses_expected_payout_minus_entry_and_costs():
    assert net_edge(expected_payout=1.0, entry_price=0.91, modeled_cost_per_share=0.01) == pytest.approx(0.08)
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_pricing.py -q
```

Expected: import failure because `sniper.analysis.public_truth_stale.pricing` does not exist.

- [ ] **Step 3: Add package init**

Create `sniper/analysis/public_truth_stale/__init__.py`:

```python
"""Offline research helpers for Public-Truth Stale Paper Strategy v0."""
```

- [ ] **Step 4: Add pricing implementation**

Create `sniper/analysis/public_truth_stale/pricing.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


class InsufficientDepthError(ValueError):
    """Raised when an ask book cannot fill the requested dollar stake."""


@dataclass(frozen=True)
class DepthWeightedFill:
    avg_price: float
    shares: float
    cost_usdc: float
    levels_consumed: int
    unfilled_usdc: float


def _as_positive_float(value: object, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be positive")
    return parsed


def depth_weighted_buy_price(
    asks: Sequence[Mapping[str, object]],
    *,
    stake_usdc: float,
) -> DepthWeightedFill:
    """Walk ask depth for a dollar-denominated paper buy."""
    target = _as_positive_float(stake_usdc, "stake_usdc")
    remaining = target
    shares = 0.0
    cost = 0.0
    levels_consumed = 0

    for level in asks:
        price = _as_positive_float(level.get("price"), "ask.price")
        size = _as_positive_float(level.get("size"), "ask.size")
        max_level_cost = price * size
        level_cost = min(remaining, max_level_cost)
        if level_cost <= 0:
            continue
        shares += level_cost / price
        cost += level_cost
        remaining -= level_cost
        levels_consumed += 1
        if remaining <= 1e-9:
            remaining = 0.0
            break

    if remaining > 1e-9 or shares <= 0:
        raise InsufficientDepthError(
            f"insufficient ask depth for ${target:.2f}; unfilled=${remaining:.2f}"
        )

    return DepthWeightedFill(
        avg_price=cost / shares,
        shares=shares,
        cost_usdc=cost,
        levels_consumed=levels_consumed,
        unfilled_usdc=remaining,
    )


def net_edge(
    *,
    expected_payout: float,
    entry_price: float,
    modeled_cost_per_share: float,
) -> float:
    payout = float(expected_payout)
    price = float(entry_price)
    costs = float(modeled_cost_per_share)
    return payout - price - costs
```

- [ ] **Step 5: Run pricing tests**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_pricing.py -q
```

Expected: `4 passed`.

- [ ] **Step 6: Commit pricing helper**

```bash
git add sniper/analysis/public_truth_stale tests/test_public_truth_stale_pricing.py
git commit -m "feat: add public-truth stale depth pricing"
```

---

## Task 3: Manual Observation Types and CSV Loader

**Files:**
- Create: `sniper/analysis/public_truth_stale/types.py`
- Create: `sniper/analysis/public_truth_stale/manual_log.py`
- Create: `tests/test_public_truth_stale_manual_log.py`

- [ ] **Step 1: Write failing manual-log tests**

Create `tests/test_public_truth_stale_manual_log.py`:

```python
from pathlib import Path

import pytest

from sniper.analysis.public_truth_stale.manual_log import load_manual_observations
from sniper.analysis.public_truth_stale.types import SourceLane


HEADER = (
    "signal_id,market_url,market_id,condition_id,token_id,outcome,rule_text_hash,"
    "resolution_source_url,source_lane,source_family,source_account_id,source_url,"
    "source_timestamp,fetch_timestamp,observed_source_value,rule_match_status,"
    "finality_status,confirmation_status,stake_usdc,modeled_cost_per_share,"
    "orderbook_file,notes\n"
)


def test_load_manual_observations_parses_valid_csv(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        HEADER
        + "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        + "https://official.example/final,resolution_authoritative,sports_official,,"
        + "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        + "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        + "examples/orderbook_signal_001.json,\n",
        encoding="utf-8",
    )

    observations = load_manual_observations(csv_path)

    assert len(observations) == 1
    assert observations[0].signal_id == "s1"
    assert observations[0].source_lane is SourceLane.RESOLUTION_AUTHORITATIVE
    assert observations[0].stake_usdc == pytest.approx(20.0)


def test_load_manual_observations_rejects_unknown_lane(tmp_path: Path):
    csv_path = tmp_path / "signals.csv"
    csv_path.write_text(
        HEADER
        + "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        + "https://official.example/final,bad_lane,sports_official,,"
        + "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        + "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        + "examples/orderbook_signal_001.json,\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source_lane"):
        load_manual_observations(csv_path)
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_manual_log.py -q
```

Expected: import failure because `manual_log` and `types` do not exist.

- [ ] **Step 3: Add domain types**

Create `sniper/analysis/public_truth_stale/types.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class SourceLane(str, Enum):
    RESOLUTION_AUTHORITATIVE = "resolution_authoritative"
    OFFICIAL_SOCIAL_SOURCE = "official_social_source"
    TRUSTED_NEWS_SOURCE = "trusted_news_source"
    SPECIALIST_ACCOUNT = "specialist_account"
    AMBIGUITY_DISPUTE = "ambiguity_dispute"


class ConfirmationStatus(str, Enum):
    CONFIRMED_BY_RESOLUTION_AUTHORITATIVE = "confirmed_by_resolution_authoritative"
    CONFIRMED_BY_MARKET_RESOLUTION = "confirmed_by_market_resolution"
    CONTRADICTED_BY_RESOLUTION_AUTHORITATIVE = "contradicted_by_resolution_authoritative"
    STILL_UNCONFIRMED = "still_unconfirmed"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ManualObservation:
    signal_id: str
    market_url: str
    market_id: str
    condition_id: str
    token_id: str
    outcome: str
    rule_text_hash: str
    resolution_source_url: str
    source_lane: SourceLane
    source_family: str
    source_account_id: str
    source_url: str
    source_timestamp: str
    fetch_timestamp: str
    observed_source_value: str
    rule_match_status: str
    finality_status: str
    confirmation_status: ConfirmationStatus
    stake_usdc: float
    modeled_cost_per_share: float
    orderbook_file: Path
    notes: str
```

- [ ] **Step 4: Add CSV loader**

Create `sniper/analysis/public_truth_stale/manual_log.py`:

```python
from __future__ import annotations

import csv
from pathlib import Path

from sniper.analysis.public_truth_stale.types import (
    ConfirmationStatus,
    ManualObservation,
    SourceLane,
)


REQUIRED_COLUMNS = {
    "signal_id",
    "market_url",
    "market_id",
    "condition_id",
    "token_id",
    "outcome",
    "rule_text_hash",
    "resolution_source_url",
    "source_lane",
    "source_family",
    "source_account_id",
    "source_url",
    "source_timestamp",
    "fetch_timestamp",
    "observed_source_value",
    "rule_match_status",
    "finality_status",
    "confirmation_status",
    "stake_usdc",
    "modeled_cost_per_share",
    "orderbook_file",
    "notes",
}


def _enum(enum_type, value: str, column: str):
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{column} has unsupported value {value!r}") from exc


def _float(row: dict[str, str], column: str) -> float:
    try:
        parsed = float(row[column])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{column} must be numeric") from exc
    if parsed < 0:
        raise ValueError(f"{column} must be non-negative")
    return parsed


def load_manual_observations(path: str | Path) -> list[ManualObservation]:
    csv_path = Path(path)
    with csv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manual log missing columns: {sorted(missing)}")

        observations: list[ManualObservation] = []
        for row in reader:
            observations.append(
                ManualObservation(
                    signal_id=row["signal_id"],
                    market_url=row["market_url"],
                    market_id=row["market_id"],
                    condition_id=row["condition_id"],
                    token_id=row["token_id"],
                    outcome=row["outcome"],
                    rule_text_hash=row["rule_text_hash"],
                    resolution_source_url=row["resolution_source_url"],
                    source_lane=_enum(SourceLane, row["source_lane"], "source_lane"),
                    source_family=row["source_family"],
                    source_account_id=row["source_account_id"],
                    source_url=row["source_url"],
                    source_timestamp=row["source_timestamp"],
                    fetch_timestamp=row["fetch_timestamp"],
                    observed_source_value=row["observed_source_value"],
                    rule_match_status=row["rule_match_status"],
                    finality_status=row["finality_status"],
                    confirmation_status=_enum(
                        ConfirmationStatus,
                        row["confirmation_status"],
                        "confirmation_status",
                    ),
                    stake_usdc=_float(row, "stake_usdc"),
                    modeled_cost_per_share=_float(row, "modeled_cost_per_share"),
                    orderbook_file=Path(row["orderbook_file"]),
                    notes=row["notes"],
                )
            )
    return observations
```

- [ ] **Step 5: Run manual-log tests**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_manual_log.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit loader**

```bash
git add sniper/analysis/public_truth_stale/types.py sniper/analysis/public_truth_stale/manual_log.py tests/test_public_truth_stale_manual_log.py
git commit -m "feat: load public-truth stale manual observations"
```

---

## Task 4: Lane-Segmented Report Builder

**Files:**
- Create: `sniper/analysis/public_truth_stale/report.py`
- Create: `tests/test_public_truth_stale_report.py`

- [ ] **Step 1: Write failing report tests**

Create `tests/test_public_truth_stale_report.py`:

```python
from pathlib import Path

from sniper.analysis.public_truth_stale.report import PaperEntry, build_markdown_report
from sniper.analysis.public_truth_stale.types import ConfirmationStatus, SourceLane


def test_report_keeps_lane_a_and_lane_b_claims_separate():
    entries = [
        PaperEntry(
            signal_id="a1",
            source_lane=SourceLane.RESOLUTION_AUTHORITATIVE,
            confirmation_status=ConfirmationStatus.CONFIRMED_BY_MARKET_RESOLUTION,
            stake_usdc=20.0,
            avg_entry_price=0.80,
            shares=25.0,
            net_edge=0.195,
        ),
        PaperEntry(
            signal_id="b1",
            source_lane=SourceLane.OFFICIAL_SOCIAL_SOURCE,
            confirmation_status=ConfirmationStatus.STILL_UNCONFIRMED,
            stake_usdc=20.0,
            avg_entry_price=0.70,
            shares=28.571428,
            net_edge=0.295,
        ),
    ]

    report = build_markdown_report(entries, source_path=Path("signals.csv"))

    assert "Lane A: resolution_authoritative" in report
    assert "Lane B: official_social_source" in report
    assert "combined PnL" not in report.lower()
    assert "strategy works" not in report.lower()
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_report.py -q
```

Expected: import failure because `report` does not exist.

- [ ] **Step 3: Add report builder**

Create `sniper/analysis/public_truth_stale/report.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sniper.analysis.public_truth_stale.types import ConfirmationStatus, SourceLane


LANE_LABELS = {
    SourceLane.RESOLUTION_AUTHORITATIVE: "Lane A: resolution_authoritative",
    SourceLane.OFFICIAL_SOCIAL_SOURCE: "Lane B: official_social_source",
    SourceLane.TRUSTED_NEWS_SOURCE: "Lane C: trusted_news_source",
    SourceLane.SPECIALIST_ACCOUNT: "Lane C: specialist_account",
    SourceLane.AMBIGUITY_DISPUTE: "Lane D: ambiguity_dispute",
}


@dataclass(frozen=True)
class PaperEntry:
    signal_id: str
    source_lane: SourceLane
    confirmation_status: ConfirmationStatus
    stake_usdc: float
    avg_entry_price: float
    shares: float
    net_edge: float


def build_markdown_report(entries: list[PaperEntry], *, source_path: Path) -> str:
    lines = [
        "# Public-Truth Stale Manual Paper Report",
        "",
        f"Source log: `{source_path}`",
        "",
        "Claims are lane-specific. Lane A, B, and C results are not combined.",
        "Lane D is excluded from v0 pass/fail.",
        "",
    ]

    for lane in SourceLane:
        lane_entries = [entry for entry in entries if entry.source_lane is lane]
        if not lane_entries:
            continue
        label = LANE_LABELS[lane]
        avg_edge = sum(entry.net_edge for entry in lane_entries) / len(lane_entries)
        confirmed = sum(
            1
            for entry in lane_entries
            if entry.confirmation_status
            in {
                ConfirmationStatus.CONFIRMED_BY_RESOLUTION_AUTHORITATIVE,
                ConfirmationStatus.CONFIRMED_BY_MARKET_RESOLUTION,
            }
        )
        lines.extend(
            [
                f"## {label}",
                "",
                f"- signals: {len(lane_entries)}",
                f"- confirmed: {confirmed}",
                f"- median/mean edge not final: mean_net_edge={avg_edge:.4f}",
                "",
                "| signal_id | status | stake_usdc | avg_entry_price | shares | net_edge |",
                "|---|---:|---:|---:|---:|---:|",
            ]
        )
        for entry in lane_entries:
            lines.append(
                "| "
                f"{entry.signal_id} | "
                f"{entry.confirmation_status.value} | "
                f"{entry.stake_usdc:.2f} | "
                f"{entry.avg_entry_price:.4f} | "
                f"{entry.shares:.4f} | "
                f"{entry.net_edge:.4f} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run report tests**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_report.py -q
```

Expected: `2 passed`.

- [ ] **Step 5: Commit report builder**

```bash
git add sniper/analysis/public_truth_stale/report.py tests/test_public_truth_stale_report.py
git commit -m "feat: report public-truth stale paper lanes"
```

---

## Task 5: Offline Manual Scoring CLI

**Files:**
- Create: `scripts/public_truth_stale_score_manual.py`
- Create: `tests/test_public_truth_stale_score_manual_cli.py`

- [ ] **Step 1: Write failing CLI smoke test**

Create `tests/test_public_truth_stale_score_manual_cli.py`:

```python
import subprocess
import sys
from pathlib import Path


def test_manual_scoring_cli_writes_lane_segmented_report(tmp_path: Path):
    signals = tmp_path / "signals.csv"
    book_dir = tmp_path / "books"
    book_dir.mkdir()
    out = tmp_path / "report.md"

    signals.write_text(
        "signal_id,market_url,market_id,condition_id,token_id,outcome,rule_text_hash,"
        "resolution_source_url,source_lane,source_family,source_account_id,source_url,"
        "source_timestamp,fetch_timestamp,observed_source_value,rule_match_status,"
        "finality_status,confirmation_status,stake_usdc,modeled_cost_per_share,"
        "orderbook_file,notes\n"
        "s1,https://polymarket.com/event/x,m1,c1,t1,YES,sha256:abc,"
        "https://official.example/final,resolution_authoritative,sports_official,,"
        "https://official.example/final,2026-06-15T21:00:00Z,2026-06-15T21:00:10Z,"
        "home_won,exact_match,official_final,still_unconfirmed,20,0.005,"
        "orderbook_signal_001.json,\n",
        encoding="utf-8",
    )
    (book_dir / "orderbook_signal_001.json").write_text(
        '{"asks":[{"price":"0.70","size":"10"},{"price":"0.75","size":"40"}]}',
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/public_truth_stale_score_manual.py",
            "--signals",
            str(signals),
            "--books-dir",
            str(book_dir),
            "--out",
            str(out),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    report = out.read_text(encoding="utf-8")
    assert "Lane A: resolution_authoritative" in report
    assert "s1" in report
```

- [ ] **Step 2: Run test and confirm failure**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_score_manual_cli.py -q
```

Expected: failure because `scripts/public_truth_stale_score_manual.py` does not exist.

- [ ] **Step 3: Add CLI**

Create `scripts/public_truth_stale_score_manual.py`:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sniper.analysis.public_truth_stale.manual_log import load_manual_observations
from sniper.analysis.public_truth_stale.pricing import depth_weighted_buy_price, net_edge
from sniper.analysis.public_truth_stale.report import PaperEntry, build_markdown_report


def _load_book(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _book_path(books_dir: Path, orderbook_file: Path) -> Path:
    if orderbook_file.is_absolute():
        return orderbook_file
    return books_dir / orderbook_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score manual Public-Truth Stale paper observations offline."
    )
    parser.add_argument("--signals", required=True, type=Path)
    parser.add_argument("--books-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    observations = load_manual_observations(args.signals)
    entries: list[PaperEntry] = []

    for observation in observations:
        book = _load_book(_book_path(args.books_dir, observation.orderbook_file))
        fill = depth_weighted_buy_price(book.get("asks", []), stake_usdc=observation.stake_usdc)
        edge = net_edge(
            expected_payout=1.0,
            entry_price=fill.avg_price,
            modeled_cost_per_share=observation.modeled_cost_per_share,
        )
        entries.append(
            PaperEntry(
                signal_id=observation.signal_id,
                source_lane=observation.source_lane,
                confirmation_status=observation.confirmation_status,
                stake_usdc=observation.stake_usdc,
                avg_entry_price=fill.avg_price,
                shares=fill.shares,
                net_edge=edge,
            )
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_markdown_report(entries, source_path=args.signals), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run CLI test**

Run:

```bash
python3 -m pytest tests/test_public_truth_stale_score_manual_cli.py -q
```

Expected: `5 passed`.

- [ ] **Step 5: Run focused suite**

Run:

```bash
python3 -m pytest \
  tests/test_public_truth_stale_pricing.py \
  tests/test_public_truth_stale_manual_log.py \
  tests/test_public_truth_stale_report.py \
  tests/test_public_truth_stale_score_manual_cli.py \
  -q
```

Expected: `13 passed`.

- [ ] **Step 6: Commit CLI**

```bash
git add scripts/public_truth_stale_score_manual.py tests/test_public_truth_stale_score_manual_cli.py
git commit -m "feat: score public-truth stale manual paper logs"
```

---

## Task 6: Manual Paper Run Procedure and Deployment

**Files:**
- Create: `docs/research/public_truth_stale/manual_runbook.md`
- Create: `docs/research/public_truth_stale/manual_signals.csv`
- Generate: `docs/research/public_truth_stale/manual_report.md`

- [ ] **Step 1: Add runbook**

Create `docs/research/public_truth_stale/manual_runbook.md`:

```markdown
# Public-Truth Stale Manual Paper Runbook

## Scope

Manual/offline only. No live orders, no production DB writes, no migrations, no live scanner.

## Capture Steps

1. Open candidate Polymarket market manually.
2. Copy market URL, market id if visible, condition id/token id if available, outcome, and rule text.
3. Hash the exact rule text with:

   ```bash
   printf '%s' '<PASTE_RULE_TEXT>' | sha256sum
   ```

4. Capture source evidence:
   - Lane A: official resolution-compatible source URL.
   - Lane B/C: source post/article URL plus account id from `source_accounts_seed.json`.
5. Save or paste the orderbook ask ladder into `docs/research/public_truth_stale/manual_books/<signal_id>.json`.
6. Add one row to a copy of `manual_signals_template.csv`.
7. Run:

   ```bash
   python3 scripts/public_truth_stale_score_manual.py \
     --signals docs/research/public_truth_stale/manual_signals.csv \
     --books-dir docs/research/public_truth_stale/manual_books \
     --out docs/research/public_truth_stale/manual_report.md
   ```

## Entry Rule

Paper only if:

- source lane is allowed;
- rule text is compatible;
- source observation says outcome appears determined;
- depth-weighted entry for $20 exists;
- `net_edge >= 0.04`.

## Review

Later update the row confirmation status to one of:

- `confirmed_by_resolution_authoritative`
- `confirmed_by_market_resolution`
- `contradicted_by_resolution_authoritative`
- `still_unconfirmed`
- `ambiguous`

Never combine lane results when claiming performance.
```

- [ ] **Step 2: Add active manual ledger**

Create `docs/research/public_truth_stale/manual_signals.csv` with the same header as `manual_signals_template.csv` and no candidate rows.

- [ ] **Step 3: Generate the initial paper deployment report**

Run:

```bash
python3 scripts/public_truth_stale_score_manual.py \
  --signals docs/research/public_truth_stale/manual_signals.csv \
  --books-dir docs/research/public_truth_stale/manual_books \
  --out docs/research/public_truth_stale/manual_report.md
```

Expected:

```text
wrote docs/research/public_truth_stale/manual_report.md
```

The report should show:

```text
candidates: 0
entered: 0
state: initialized, waiting for manually captured signals
```

- [ ] **Step 4: Commit runbook and paper deployment files**

```bash
git add \
  docs/research/public_truth_stale/manual_runbook.md \
  docs/research/public_truth_stale/manual_signals.csv \
  docs/research/public_truth_stale/manual_report.md
git commit -m "docs: deploy public-truth stale manual paper ledger"
```

---

## Verification

Run:

```bash
python3 -m pytest \
  tests/test_public_truth_stale_pricing.py \
  tests/test_public_truth_stale_manual_log.py \
  tests/test_public_truth_stale_report.py \
  tests/test_public_truth_stale_score_manual_cli.py \
  -q
```

Expected:

```text
13 passed
```

Run:

```bash
python3 scripts/validate_schema_refs.py --warn-unknown-tables
```

Expected:

```text
No schema violations.
```

Existing unknown-table warnings in older docs are acceptable if they are unchanged.

Run:

```bash
git status --short --branch
```

Expected:

```text
## <feature-branch>
```

Plus only intentional files from this plan unless the operator has unrelated local WIP.

## Scope Guards

- No live scans.
- No CLOB order placement.
- No production DB writes.
- No migrations.
- No service restarts.
- No X API calls.
- No LLM-decided finality.
- No combined lane performance claims.
