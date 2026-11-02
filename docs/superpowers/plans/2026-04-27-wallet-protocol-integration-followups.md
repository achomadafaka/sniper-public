# Wallet Protocol Integration Follow-ups (C1 → Report, C2 Fallback, Accounting Tie)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the CapitalLedger onto ProtocolReport (Follow-up 1), add subgraph fallback in fetch.py when the REST cap is hit (Follow-up 2), and add the ±2% accounting tie check (Follow-up 4, depends on Follow-up 1).

**Architecture:**
- Follow-up 1: additive changes to `types.py` + `fetch.py` + `analyze_wallet.py` + `report.py` + CLI. `CapitalLedger` flows through `WalletData → ProtocolReport → render_report`.
- Follow-up 2: normalization layer in `fetch.py`. When `activity_cap_hit`, replace REST rows with subgraph fills normalized via a token-ID lookup built from the REST rows themselves (free, no extra API calls). Unmapped tokens are dropped and flagged, not passed through.
- Follow-up 4: `WalletData` carries `portfolio_value_usdc` (from `fetch_user_value()`). `analyze_wallet.py` computes the tie and adds a data-quality flag when discrepancy > 2%.

**Tech Stack:** Python 3.11, `requests`, existing `subgraph_reader.py`, `capital_ledger.py`, `polymarket_api.fetch_user_value()`, pytest.

## Execution Status

Taken over and verified on 2026-04-27.

- Implemented in commits `26a6282`, `799f021`, `1c4f976` on `feat/wallet-protocol-integration-followups`.
- Rebased cleanly onto `origin/main` after `29ed8c1`.
- Wallet protocol regression: `317 passed`.
- SwissMiss live smoke: capital ledger renders `net_capital_funded: $33,555.52 USDC`; subgraph fallback returned `n_fills=28280`, `n_mapped=3385`, `n_unmapped=24895`.
- Accounting tie emitted `ACCOUNTING_TIE_FAILED`; per Task 3 caveats this is valid signal, not a code failure, because the tie intentionally exposes discrepancy against `fetch_user_value()`.

---

## Prerequisites

- [ ] **Branch / worktree isolation**
  ```bash
  cd /home/bigpapi/Desktop/Sniper
  git checkout -b feat/wallet-protocol-integration-followups
  ```
  Do NOT write to `main` directly. Three tasks, three commits.

---

## Task 1: Wire CapitalLedger onto ProtocolReport

**Files:**
- Modify: `sniper/analysis/wallet_protocol/types.py` — add `capital_ledger` to `WalletData` + `ProtocolReport`
- Modify: `sniper/analysis/wallet_protocol/fetch.py` — optionally call `compute_capital_ledger`
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — pass `data.capital_ledger` → `report.capital_ledger`
- Modify: `sniper/analysis/wallet_protocol/report.py` — render ledger in Capital efficiency section
- Modify: `scripts/run_wallet_protocol.py` — add `--capital-ledger` flag
- Test: `tests/test_wallet_protocol_capital_ledger_integration.py`

### Step 1.1: Write the failing tests

- [ ] Create `tests/test_wallet_protocol_capital_ledger_integration.py`:

```python
"""Tests: CapitalLedger flows WalletData → ProtocolReport → render_report.

Tests are hermetic — mocked ledger, no live HTTP.
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock
from dataclasses import replace

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.report import render_report
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _minimal_wallet_data(wallet="0xtest") -> WalletData:
    """WalletData with enough closed positions to not fail R2 gate."""
    closed = [
        {"conditionId": f"cid_{i}", "outcome": "Yes",
         "realizedPnl": "10.0", "avgPrice": "0.5", "size": "20",
         "totalBought": "100", "endDate": "2025-01-01T00:00:00Z",
         "curPrice": "1.0"}
        for i in range(30)
    ]
    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
    )


def _sample_ledger(wallet="0xtest") -> CapitalLedger:
    return CapitalLedger(
        wallet=wallet,
        n_splits=10,
        n_merges=3,
        n_redemptions=5,
        total_deposits_usdc=10_000.0,
        total_withdrawals_usdc=2_000.0,
        total_redemption_payout_usdc=1_500.0,
        net_capital_funded_usdc=8_000.0,
        first_event_ts=1_600_000_000,
        last_event_ts=1_700_000_000,
        events=[],
    )


# ---------------------------------------------------------------------------
# Test 1: capital_ledger passes through WalletData → ProtocolReport
# ---------------------------------------------------------------------------

def test_capital_ledger_stored_on_protocol_report():
    data = _minimal_wallet_data()
    ledger = _sample_ledger()
    data = replace(data, capital_ledger=ledger)

    with patch(
        "sniper.analysis.wallet_protocol.analyze_wallet.fetch_wallet_data",
        return_value=data,
    ):
        report = analyze_wallet("0xtest", pre_fetched_data=data)

    assert report.capital_ledger is ledger
    assert report.capital_ledger.net_capital_funded_usdc == 8_000.0


def test_capital_ledger_none_when_not_provided():
    data = _minimal_wallet_data()
    with patch(
        "sniper.analysis.wallet_protocol.analyze_wallet.fetch_wallet_data",
        return_value=data,
    ):
        report = analyze_wallet("0xtest", pre_fetched_data=data)

    assert report.capital_ledger is None


# ---------------------------------------------------------------------------
# Test 2: render_report includes ledger section when capital_ledger present
# ---------------------------------------------------------------------------

def test_render_report_shows_capital_ledger():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    report.capital_ledger = _sample_ledger()

    md = render_report(report)
    assert "net_capital_funded" in md
    assert "8000" in md or "8,000" in md


def test_render_report_omits_ledger_section_when_none():
    report = ProtocolReport(wallet="0xtest", analyzed_at=1_700_000_000)
    assert report.capital_ledger is None

    md = render_report(report)
    assert "net_capital_funded" not in md


# ---------------------------------------------------------------------------
# Test 3: fetch_wallet_data computes ledger when with_capital_ledger=True
# ---------------------------------------------------------------------------

def test_fetch_wallet_data_with_capital_ledger_calls_compute():
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    mock_ledger = _sample_ledger()
    with patch("polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_activity", return_value=[]), \
         patch("polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.compute_capital_ledger",
             return_value=mock_ledger,
         ) as mock_cl:
        data = fetch_wallet_data(
            "0xtest", requests.Session(), with_capital_ledger=True,
        )

    mock_cl.assert_called_once()
    assert data.capital_ledger is mock_ledger


def test_fetch_wallet_data_no_ledger_by_default():
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data

    with patch("polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_activity", return_value=[]), \
         patch("polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.compute_capital_ledger",
         ) as mock_cl:
        data = fetch_wallet_data("0xtest", requests.Session())

    mock_cl.assert_not_called()
    assert data.capital_ledger is None
```

- [ ] Run the tests to confirm they fail:
  ```bash
  cd /home/bigpapi/Desktop/Sniper
  python3 -m pytest tests/test_wallet_protocol_capital_ledger_integration.py -q 2>&1 | head -30
  ```
  Expected: FAIL (AttributeError — `capital_ledger` not yet on WalletData/ProtocolReport)

### Step 1.2: Add `capital_ledger` + `portfolio_value_usdc` + `fetch_quality_flags` to `WalletData` and `ProtocolReport`

- [ ] Edit `sniper/analysis/wallet_protocol/types.py`:

In `WalletData` (after `fetched_at`), add:
```python
capital_ledger: "CapitalLedger | None" = None        # optional; populated when --capital-ledger
portfolio_value_usdc: float | None = None             # from fetch_user_value(); used for acctg tie
fetch_quality_flags: list[str] = field(default_factory=list)  # flags set during data fetch
```

Add the import at top (TYPE_CHECKING guard to avoid circular):
```python
from __future__ import annotations
# existing imports...
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
```

Actually, `CapitalLedger` is a simple dataclass with no circular dependency, so direct import is fine:
```python
from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
```

In `ProtocolReport` (after `applicable_limitations`), add:
```python
# C1 — Capital ledger: onchain SPLIT/MERGE/REDEMPTION history.
# None when not computed (--capital-ledger not passed). When present,
# net_capital_funded_usdc is the honest divisor for portfolio-level ROI.
capital_ledger: CapitalLedger | None = None
```

Update `__all__` to include `CapitalLedger` (re-export from types):
```python
# No change needed — CapitalLedger stays in capital_ledger.py
```

- [ ] Run tests again — expect new failures on `WalletData` instantiation (missing kwargs):
  ```bash
  python3 -m pytest tests/test_wallet_protocol_capital_ledger_integration.py -q 2>&1 | head -30
  ```

### Step 1.3: Update `fetch.py` to optionally compute the capital ledger and portfolio value

- [ ] Edit `sniper/analysis/wallet_protocol/fetch.py`:

Add import at top:
```python
import polymarket_api
from sniper.analysis.wallet_protocol.capital_ledger import compute_capital_ledger
```

Change `fetch_wallet_data` signature:
```python
def fetch_wallet_data(
    wallet: str,
    session: requests.Session | None = None,
    archetype_extractor=None,
    walk_full_history: bool = False,
    with_capital_ledger: bool = False,
) -> WalletData:
```

After the existing fetch block (just before the `return WalletData(...)`), add:
```python
    # Optional: capital ledger (3 subgraph queries, ~4s for large wallets).
    capital_ledger = None
    if with_capital_ledger:
        try:
            capital_ledger = compute_capital_ledger(wallet, session=sess)
            logger.info(
                "wallet_protocol capital_ledger wallet=%s net_funded=%.2f",
                wallet[:14], capital_ledger.net_capital_funded_usdc,
            )
        except Exception:
            logger.exception(
                "wallet_protocol capital_ledger failed wallet=%s", wallet[:14],
            )

    # Portfolio value — used for ±2% accounting tie (Follow-up 4).
    portfolio_value_usdc: float | None = None
    if with_capital_ledger:
        try:
            portfolio_value_usdc = polymarket_api.fetch_user_value(sess, wallet)
            logger.info(
                "wallet_protocol portfolio_value wallet=%s value=%.2f",
                wallet[:14], portfolio_value_usdc,
            )
        except Exception:
            logger.exception(
                "wallet_protocol portfolio_value failed wallet=%s", wallet[:14],
            )
```

Update the `return WalletData(...)` to include new fields:
```python
    return WalletData(
        wallet=wallet,
        closed_positions=closed,
        open_positions=open_positions,
        activity_rows=activity,
        activity_cap_hit=activity_cap_hit,
        archetype_layer1=archetype_layer1,
        execution_profile=execution_profile,
        fetched_at=int(time.time()),
        capital_ledger=capital_ledger,
        portfolio_value_usdc=portfolio_value_usdc,
        fetch_quality_flags=[],
    )
```

### Step 1.4: Update `analyze_wallet.py` to pass capital_ledger through

- [ ] Edit `sniper/analysis/wallet_protocol/analyze_wallet.py`:

After the DW/EW section (around line 325), add:
```python
    # C1 — Capital ledger passthrough: always carry from data if present.
    if data.capital_ledger is not None:
        report.capital_ledger = data.capital_ledger
```

Also extend `report.data_quality_flags` with any flags from the fetch layer:
```python
    # Merge fetch-layer quality flags (subgraph fallback, unmapped tokens, etc.)
    report.data_quality_flags.extend(data.fetch_quality_flags)
```

Add this just after the `report = ProtocolReport(...)` block is populated.

### Step 1.5: Update `report.py` to render the capital ledger

- [ ] Edit `sniper/analysis/wallet_protocol/report.py`:

Add import at top:
```python
from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
```

In `render_report`, inside the Capital efficiency section (after the DW/EW block), add a capital ledger sub-section:

```python
    # C1 — Capital ledger: show honest net-funded divisor when available.
    if report.capital_ledger is not None:
        cl = report.capital_ledger
        lines.append("")
        lines.append("## Capital ledger (onchain SPLIT/MERGE/REDEMPTION)")
        lines.append(f"- net_capital_funded: ${cl.net_capital_funded_usdc:,.2f} USDC")
        lines.append(f"- total_deposits: ${cl.total_deposits_usdc:,.2f} (via SPLIT events)")
        lines.append(f"- total_withdrawals: ${cl.total_withdrawals_usdc:,.2f} (via MERGE events)")
        lines.append(f"- total_redemption_payout: ${cl.total_redemption_payout_usdc:,.2f} (settlement)")
        lines.append(f"- n_splits: {cl.n_splits}, n_merges: {cl.n_merges}, n_redemptions: {cl.n_redemptions}")
        if cl.first_event_ts:
            lines.append(f"- first_event: {_fmt_ts(cl.first_event_ts)}")
        if cl.last_event_ts:
            lines.append(f"- last_event: {_fmt_ts(cl.last_event_ts)}")
        # Portfolio-level ROI: total realized P&L / net capital deployed.
        # This is the "honest divisor" the I1 section previously lacked.
        if cl.net_capital_funded_usdc > 0 and report.net_pnl is not None:
            portfolio_roi = report.net_pnl / cl.net_capital_funded_usdc
            lines.append(
                f"- portfolio_roi (realized_pnl / net_funded): {portfolio_roi*100:+.2f}%"
            )
```

Place this block right after the DW/EW section, before `### Stage 1 result`.

### Step 1.6: Update CLI to add `--capital-ledger` flag

- [ ] Edit `scripts/run_wallet_protocol.py`:

Add argument:
```python
ap.add_argument(
    "--capital-ledger", action="store_true",
    help="Fetch onchain capital ledger (SPLIT/MERGE/REDEMPTION via Goldsky subgraph). "
         "Adds ~4s for large wallets. Required for ±2%% accounting tie.",
)
```

Update the `fetch_wallet_data` call to pass the new flag:
```python
data = fetch_wallet_data(
    wallet, session, archetype_extractor=None,
    walk_full_history=args.full_history,
    with_capital_ledger=args.capital_ledger,
)
```

### Step 1.7: Run tests and verify

- [ ] Run:
  ```bash
  python3 -m pytest tests/test_wallet_protocol_capital_ledger_integration.py -q
  ```
  Expected: all 5 tests PASS.

- [ ] Run the full wallet-protocol test suite (regression check):
  ```bash
  python3 -m pytest tests/test_wallet_protocol*.py -q
  ```
  Expected: all existing tests still pass.

- [ ] **Live verification on SwissMiss** — confirm I1 section shows the honest divisor:
  ```bash
  python3 scripts/run_wallet_protocol.py \
      0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
      --capital-ledger \
      --no-exec --no-history --no-tags --no-peers --no-archetype \
      --n-iter 500 2>&1 | grep -A 20 "Capital ledger"
  ```
  Expected output must include:
  - `net_capital_funded: $33,555.xx USDC` (matches C1 closure verification bar)
  - `portfolio_roi` line populated

### Step 1.8: Commit

- [ ] Commit:
  ```bash
  git add \
    sniper/analysis/wallet_protocol/types.py \
    sniper/analysis/wallet_protocol/fetch.py \
    sniper/analysis/wallet_protocol/analyze_wallet.py \
    sniper/analysis/wallet_protocol/report.py \
    scripts/run_wallet_protocol.py \
    tests/test_wallet_protocol_capital_ledger_integration.py
  git commit -m "feat(wallet-protocol): wire CapitalLedger onto ProtocolReport (C1 integration)"
  ```

- [ ] Write Supabase brain entry documenting closure:
  ```python
  # Record in Supabase brain
  # memory_name: "wallet-protocol/C1-report-integration"
  # memory_type: "finding"
  # body: "C1 CapitalLedger wired onto ProtocolReport. fetch.py: --capital-ledger flag calls compute_capital_ledger + fetch_user_value. analyze_wallet.py: data.capital_ledger → report.capital_ledger. report.py: Capital ledger section shows net_capital_funded, portfolio_roi. Verified SwissMiss live: net_capital_funded=$33,555. Commit: <SHA>."
  ```

---

## Task 2: Wire subgraph_reader into fetch.py as fallback

**Files:**
- Modify: `sniper/analysis/wallet_protocol/fetch.py` — add `_build_token_lookup`, `_normalize_order_fill`, subgraph fallback logic
- Test: `tests/test_wallet_protocol_fetch_subgraph_fallback.py`

**Key design decisions (document in code):**
1. USDC in the CLOB is token ID `"0"`. Assumption based on test fixtures; verified assumption in docstring.
2. Token lookup is built from the REST activity rows already in hand — no extra API calls.
3. Fills with unmapped token IDs (not in REST rows) are dropped. Count emitted as `ACTIVITY_SUBGRAPH_UNMAPPED: n=K`.
4. "Transparent" is a goal: `timestamp`, `transactionHash`, `asset` pass through unchanged. `conditionId`, `outcome`, `side` require the lookup; rows with unknown tokens are dropped.
5. `activity_cap_hit` is set to `False` after successful subgraph fetch (cap is gone).

### Step 2.1: Write the failing tests

- [ ] Create `tests/test_wallet_protocol_fetch_subgraph_fallback.py`:

```python
"""Tests: fetch.py subgraph fallback when activity_cap_hit=True.

Covers:
- _build_token_lookup extracts asset → (conditionId, outcome) from REST rows
- _normalize_order_fill maps OrderFilledEvent → activity row shape
- fetch_wallet_data triggers subgraph when activity_cap_hit
- Unmapped tokens are counted and flagged, not passed through
"""
from __future__ import annotations
from unittest.mock import patch, MagicMock

import pytest

from sniper.analysis.wallet_protocol.fetch import (
    _build_token_lookup,
    _normalize_order_fill,
    _CLOB_USDC_TOKEN_ID,
)


# ---------------------------------------------------------------------------
# _build_token_lookup
# ---------------------------------------------------------------------------

def test_build_token_lookup_extracts_asset_conditionid_outcome():
    rows = [
        {"asset": "tok_yes_a", "conditionId": "cid_a", "outcome": "Yes"},
        {"asset": "tok_no_a", "conditionId": "cid_a", "outcome": "No"},
        {"asset": "tok_yes_b", "conditionId": "cid_b", "outcome": "Yes"},
    ]
    lookup = _build_token_lookup(rows)
    assert lookup["tok_yes_a"] == ("cid_a", "Yes")
    assert lookup["tok_no_a"] == ("cid_a", "No")
    assert lookup["tok_yes_b"] == ("cid_b", "Yes")


def test_build_token_lookup_skips_rows_without_asset():
    rows = [
        {"conditionId": "cid_a", "outcome": "Yes"},  # no asset
        {"asset": "", "conditionId": "cid_a", "outcome": "Yes"},  # empty asset
    ]
    lookup = _build_token_lookup(rows)
    assert len(lookup) == 0


def test_build_token_lookup_skips_usdc_token():
    rows = [
        {"asset": _CLOB_USDC_TOKEN_ID, "conditionId": "cid_a", "outcome": "Yes"},
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes"},
    ]
    lookup = _build_token_lookup(rows)
    assert _CLOB_USDC_TOKEN_ID not in lookup
    assert "tok_yes" in lookup


# ---------------------------------------------------------------------------
# _normalize_order_fill — maker perspective (wallet == maker)
# ---------------------------------------------------------------------------

WALLET = "0xwallet"
TOKEN_LOOKUP = {
    "tok_yes": ("cid_a", "Yes"),
    "tok_no": ("cid_a", "No"),
}


def _fill(maker_asset, taker_asset, maker_amt, taker_amt, ts="1700000000", tx="0xabc"):
    """Helper: build an OrderFilledEvent dict."""
    return {
        "id": f"{tx}_0x00",
        "timestamp": ts,
        "maker": WALLET,
        "taker": "0xother",
        "makerAssetId": maker_asset,
        "takerAssetId": taker_asset,
        "makerAmountFilled": str(maker_amt),
        "takerAmountFilled": str(taker_amt),
        "fee": "0",
        "transactionHash": tx,
    }


def test_normalize_maker_buys_yes():
    """Maker gives USDC, receives YES tokens → BUY YES."""
    # Maker pays 600000 USDC ($0.60) for 1000000 YES tokens (1.0 share)
    fill = _fill(
        maker_asset=_CLOB_USDC_TOKEN_ID,
        taker_asset="tok_yes",
        maker_amt=600_000,   # $0.60 USDC
        taker_amt=1_000_000, # 1.0 YES share
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "BUY"
    assert row["conditionId"] == "cid_a"
    assert row["outcome"] == "Yes"
    assert row["asset"] == "tok_yes"
    assert abs(row["price"] - 0.6) < 1e-9
    assert abs(row["size"] - 1.0) < 1e-9
    assert row["timestamp"] == 1_700_000_000
    assert row["transactionHash"] == "0xabc"


def test_normalize_maker_sells_yes():
    """Maker gives YES tokens, receives USDC → SELL YES."""
    # Maker sells 2000000 YES tokens (2.0 shares) for 1400000 USDC ($1.40 → $0.70/share)
    fill = _fill(
        maker_asset="tok_yes",
        taker_asset=_CLOB_USDC_TOKEN_ID,
        maker_amt=2_000_000, # 2.0 YES shares
        taker_amt=1_400_000, # $1.40 USDC
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "SELL"
    assert row["outcome"] == "Yes"
    assert abs(row["price"] - 0.7) < 1e-9
    assert abs(row["size"] - 2.0) < 1e-9


# ---------------------------------------------------------------------------
# _normalize_order_fill — taker perspective (wallet == taker)
# ---------------------------------------------------------------------------

def _fill_taker(maker_asset, taker_asset, maker_amt, taker_amt, tx="0xdef"):
    return {
        "id": f"{tx}_0x00",
        "timestamp": "1700000001",
        "maker": "0xother",
        "taker": WALLET,
        "makerAssetId": maker_asset,
        "takerAssetId": taker_asset,
        "makerAmountFilled": str(maker_amt),
        "takerAmountFilled": str(taker_amt),
        "fee": "0",
        "transactionHash": tx,
    }


def test_normalize_taker_buys_yes():
    """Taker gives USDC, receives YES tokens → BUY YES."""
    # Taker pays 500000 USDC ($0.50) for 1000000 YES tokens
    fill = _fill_taker(
        maker_asset="tok_yes",    # maker gives YES
        taker_asset=_CLOB_USDC_TOKEN_ID,  # taker gives USDC
        maker_amt=1_000_000,
        taker_amt=500_000,
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "BUY"
    assert row["outcome"] == "Yes"
    assert abs(row["price"] - 0.5) < 1e-9
    assert abs(row["size"] - 1.0) < 1e-9


def test_normalize_taker_sells_yes():
    """Taker gives YES tokens, receives USDC → SELL YES."""
    fill = _fill_taker(
        maker_asset=_CLOB_USDC_TOKEN_ID,
        taker_asset="tok_yes",
        maker_amt=800_000,  # USDC to taker
        taker_amt=1_000_000,  # YES from taker
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is not None
    assert row["side"] == "SELL"
    assert abs(row["price"] - 0.8) < 1e-9


def test_normalize_unknown_token_returns_none():
    """Fills with token IDs not in lookup are unmapped → return None."""
    fill = _fill(
        maker_asset=_CLOB_USDC_TOKEN_ID,
        taker_asset="unknown_tok",  # not in lookup
        maker_amt=100_000,
        taker_amt=200_000,
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is None


def test_normalize_both_usdc_returns_none():
    """Edge case: both assets are USDC token → invalid fill, return None."""
    fill = _fill(
        maker_asset=_CLOB_USDC_TOKEN_ID,
        taker_asset=_CLOB_USDC_TOKEN_ID,
        maker_amt=100_000,
        taker_amt=100_000,
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is None


def test_normalize_zero_outcome_amount_returns_none():
    """Division by zero guard: outcome token amount = 0 → return None."""
    fill = _fill(
        maker_asset=_CLOB_USDC_TOKEN_ID,
        taker_asset="tok_yes",
        maker_amt=100_000,
        taker_amt=0,  # zero shares — can't compute price
    )
    row = _normalize_order_fill(fill, WALLET, TOKEN_LOOKUP)
    assert row is None


# ---------------------------------------------------------------------------
# Subgraph fallback integration in fetch_wallet_data
# ---------------------------------------------------------------------------

def test_fetch_uses_subgraph_when_cap_hit():
    """When activity_cap_hit, fetch_wallet_data replaces rows with subgraph data."""
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data, _ACTIVITY_CAP

    # 3000 REST rows → cap hit
    rest_rows = [
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes",
         "side": "BUY", "price": "0.6", "size": "1.0",
         "timestamp": str(1_700_000_000 + i), "transactionHash": f"0x{i:04x}"}
        for i in range(_ACTIVITY_CAP)
    ]
    # One subgraph fill for tok_yes (mappable) + one for unknown token
    subgraph_fills = [
        {
            "id": "0xfill1_0x00",
            "timestamp": "1699000000",
            "maker": "0xwallet",
            "taker": "0xother",
            "makerAssetId": "0",  # USDC
            "takerAssetId": "tok_yes",
            "makerAmountFilled": "600000",
            "takerAmountFilled": "1000000",
            "fee": "0",
            "transactionHash": "0xfill1",
        },
        {
            "id": "0xfill2_0x00",
            "timestamp": "1699000001",
            "maker": "0xwallet",
            "taker": "0xother",
            "makerAssetId": "0",
            "takerAssetId": "unknown_tok",  # not in lookup
            "makerAmountFilled": "200000",
            "takerAmountFilled": "500000",
            "fee": "0",
            "transactionHash": "0xfill2",
        },
    ]

    with patch("polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
             return_value=subgraph_fills,
         ):
        data = fetch_wallet_data("0xwallet", requests.Session())

    # activity_cap_hit cleared after fallback
    assert data.activity_cap_hit is False
    # Only the mappable fill is included
    assert any(r.get("conditionId") == "cid_a" for r in data.activity_rows)
    # Unmapped token is flagged
    assert any("ACTIVITY_SUBGRAPH_UNMAPPED" in f for f in data.fetch_quality_flags)
    # Subgraph path is flagged
    assert any("ACTIVITY_SOURCE" in f for f in data.fetch_quality_flags)


def test_fetch_no_subgraph_when_cap_not_hit():
    """When cap not hit, subgraph is NOT called."""
    import requests
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data, _ACTIVITY_CAP

    rest_rows = [
        {"asset": "tok_yes", "conditionId": "cid_a", "outcome": "Yes",
         "side": "BUY", "price": "0.6", "size": "1.0",
         "timestamp": "1700000000", "transactionHash": "0x0000"}
    ]  # only 1 row, well under cap

    with patch("polymarket_api.fetch_closed_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_positions", return_value=[]), \
         patch("polymarket_api.fetch_user_activity", return_value=rest_rows), \
         patch("polymarket_api.fetch_user_value", return_value=0.0), \
         patch(
             "sniper.analysis.wallet_protocol.fetch.fetch_order_filled_events",
         ) as mock_sg:
        data = fetch_wallet_data("0xwallet", requests.Session())

    mock_sg.assert_not_called()
    assert not any("ACTIVITY_SOURCE" in f for f in data.fetch_quality_flags)
```

- [ ] Run to confirm they fail:
  ```bash
  python3 -m pytest tests/test_wallet_protocol_fetch_subgraph_fallback.py -q 2>&1 | head -20
  ```
  Expected: ImportError — `_build_token_lookup`, `_normalize_order_fill`, `_CLOB_USDC_TOKEN_ID` not yet exported.

### Step 2.2: Implement `_build_token_lookup`, `_normalize_order_fill`, and the fallback in `fetch.py`

- [ ] Edit `sniper/analysis/wallet_protocol/fetch.py`:

Add import at top:
```python
from sniper.analysis.wallet_protocol.subgraph_reader import fetch_order_filled_events
```

Add module-level constant:
```python
# USDC collateral in the Polymarket CLOB orderbook is represented as token ID "0".
# Assumption: verified by test fixtures (makerAssetId="0" in synthetic fills) and
# by Polymarket CLOB contract behavior where the collateral asset is assigned ID 0.
# If Polymarket ever changes this, update here and re-run live verification.
_CLOB_USDC_TOKEN_ID = "0"

# Polymarket CLOB token amounts are 6-decimal (same as USDC).
_CLOB_TOKEN_SCALE = 1_000_000
```

Add these two pure functions before `fetch_wallet_data`:
```python
def _build_token_lookup(
    activity_rows: list[dict],
) -> dict[str, tuple[str, str]]:
    """Build {asset_id: (conditionId, outcome)} from REST /activity rows.

    REST rows carry both the CLOB token ID (`asset`) and the protocol-level
    `conditionId` + `outcome`. The lookup lets us map subgraph OrderFilledEvent
    token IDs (makerAssetId / takerAssetId) back to the protocol's coordinate
    system. USDC token ID is excluded — it's never an outcome.
    """
    lookup: dict[str, tuple[str, str]] = {}
    for row in activity_rows:
        asset = str(row.get("asset") or "").strip()
        cid = str(row.get("conditionId") or "").strip()
        outcome = str(row.get("outcome") or "").strip()
        if not asset or not cid or not outcome:
            continue
        if asset == _CLOB_USDC_TOKEN_ID:
            continue
        lookup[asset] = (cid, outcome)
    return lookup


def _normalize_order_fill(
    event: dict,
    wallet: str,
    token_lookup: dict[str, tuple[str, str]],
) -> dict | None:
    """Normalize one OrderFilledEvent to REST /activity row shape.

    Returns None for:
    - Fills where the outcome token isn't in `token_lookup`
    - Fills where the outcome token amount is zero (price undefined)
    - Fills where both assets are USDC (degenerate/error case)

    USDC detection: token ID == _CLOB_USDC_TOKEN_ID ("0").

    BUY/SELL logic (from wallet's perspective):
        wallet == maker:
            makerAssetId == USDC → wallet paid USDC to receive outcome tokens → BUY
            makerAssetId != USDC → wallet gave outcome tokens for USDC → SELL
        wallet == taker:
            takerAssetId == USDC → wallet paid USDC to receive outcome tokens → BUY
            takerAssetId != USDC → wallet gave outcome tokens for USDC → SELL

    Price/size (both assets are 6-decimal):
        BUY:  price = usdc_amount / outcome_amount; size = outcome_amount / 1e6
        SELL: price = usdc_amount / outcome_amount; size = outcome_amount / 1e6
    """
    wallet_lc = wallet.lower()
    is_maker = (event.get("maker") or "").lower() == wallet_lc
    is_taker = (event.get("taker") or "").lower() == wallet_lc

    if not is_maker and not is_taker:
        return None  # fill doesn't involve this wallet

    maker_asset = str(event.get("makerAssetId") or "")
    taker_asset = str(event.get("takerAssetId") or "")

    # Identify USDC vs outcome token
    maker_is_usdc = maker_asset == _CLOB_USDC_TOKEN_ID
    taker_is_usdc = taker_asset == _CLOB_USDC_TOKEN_ID

    if maker_is_usdc and taker_is_usdc:
        return None  # degenerate fill

    try:
        maker_amt = int(event.get("makerAmountFilled") or 0)
        taker_amt = int(event.get("takerAmountFilled") or 0)
    except (TypeError, ValueError):
        return None

    if is_maker:
        if maker_is_usdc:
            # BUY: wallet paid USDC (maker), received outcome (taker)
            side = "BUY"
            outcome_token = taker_asset
            usdc_amt = maker_amt
            outcome_amt = taker_amt
        else:
            # SELL: wallet gave outcome (maker), received USDC (taker)
            side = "SELL"
            outcome_token = maker_asset
            usdc_amt = taker_amt
            outcome_amt = maker_amt
    else:  # is_taker
        if taker_is_usdc:
            # BUY: wallet paid USDC (taker), received outcome (maker)
            side = "BUY"
            outcome_token = maker_asset
            usdc_amt = taker_amt
            outcome_amt = maker_amt
        else:
            # SELL: wallet gave outcome (taker), received USDC (maker)
            side = "SELL"
            outcome_token = taker_asset
            usdc_amt = maker_amt
            outcome_amt = taker_amt

    if outcome_amt == 0:
        return None

    mapping = token_lookup.get(outcome_token)
    if mapping is None:
        return None  # caller counts these as unmapped

    condition_id, outcome = mapping
    price = usdc_amt / outcome_amt   # both 6-decimal → dimensionless ratio in [0,1]
    size = outcome_amt / _CLOB_TOKEN_SCALE

    try:
        ts = int(event.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None

    return {
        "conditionId": condition_id,
        "outcome": outcome,
        "side": side,
        "asset": outcome_token,
        "price": price,
        "size": size,
        "timestamp": ts,
        "transactionHash": str(event.get("transactionHash") or ""),
        "type": "TRADE",
        "_source": "subgraph",  # internal tag — lets downstream skip if needed
    }
```

In `fetch_wallet_data`, replace the existing activity fetch + cap_hit block with:
```python
    # 3. /activity?type=TRADE — capped at _ACTIVITY_CAP unless walk_full_history=True
    try:
        activity = polymarket_api.fetch_user_activity(
            session, wallet, activity_type="TRADE",
            walk_full_history=walk_full_history,
        )
    except Exception:
        logger.exception(
            "wallet_protocol activity fetch failed wallet=%s — using empty list",
            wallet[:14],
        )
        activity = []

    activity_cap_hit = (not walk_full_history) and len(activity) >= _ACTIVITY_CAP
    fetch_quality_flags: list[str] = []

    if activity_cap_hit:
        # Build token lookup from the REST rows we already have (free, no extra API).
        token_lookup = _build_token_lookup(activity)
        logger.info(
            "wallet_protocol activity_cap_hit wallet=%s rest_rows=%d token_lookup=%d — trying subgraph",
            wallet[:14], len(activity), len(token_lookup),
        )
        try:
            fills = fetch_order_filled_events(wallet, session=session)
            normalized: list[dict] = []
            n_unmapped = 0
            for fill in fills:
                row = _normalize_order_fill(fill, wallet, token_lookup)
                if row is not None:
                    normalized.append(row)
                else:
                    # Check if it was actually unmapped (vs zero-amount or other skip)
                    maker_a = str(fill.get("makerAssetId") or "")
                    taker_a = str(fill.get("takerAssetId") or "")
                    outcome_tok = taker_a if maker_a == _CLOB_USDC_TOKEN_ID else maker_a
                    if outcome_tok and outcome_tok not in token_lookup and outcome_tok != _CLOB_USDC_TOKEN_ID:
                        n_unmapped += 1
            activity = normalized
            activity_cap_hit = False  # cap cleared — subgraph has no row ceiling
            fetch_quality_flags.append(
                f"ACTIVITY_SOURCE:subgraph n_fills={len(fills)} n_mapped={len(normalized)} n_unmapped={n_unmapped}"
            )
            if n_unmapped > 0:
                fetch_quality_flags.append(
                    f"ACTIVITY_SUBGRAPH_UNMAPPED: n={n_unmapped} (token IDs not in recent REST window; "
                    f"flip-recon rows for these markets dropped)"
                )
            logger.info(
                "wallet_protocol subgraph fallback wallet=%s fills=%d mapped=%d unmapped=%d",
                wallet[:14], len(fills), len(normalized), n_unmapped,
            )
        except Exception:
            logger.exception(
                "wallet_protocol subgraph fallback failed wallet=%s — keeping REST rows",
                wallet[:14],
            )
            fetch_quality_flags.append("ACTIVITY_SOURCE:rest_cap_hit_subgraph_failed")
    else:
        logger.info(
            "wallet_protocol activity wallet=%s n=%d cap_hit=%s",
            wallet[:14], len(activity), activity_cap_hit,
        )
```

Update `return WalletData(...)` to include `fetch_quality_flags`:
```python
    return WalletData(
        ...
        fetch_quality_flags=fetch_quality_flags,
    )
```

### Step 2.3: Run tests and verify

- [ ] Run the subgraph fallback tests:
  ```bash
  python3 -m pytest tests/test_wallet_protocol_fetch_subgraph_fallback.py -q
  ```
  Expected: all tests PASS.

- [ ] Run full wallet protocol regression:
  ```bash
  python3 -m pytest tests/test_wallet_protocol*.py -q
  ```
  Expected: no regressions.

- [ ] **Live verification on SwissMiss** (33K fills, well above the 3K cap):
  ```bash
  python3 scripts/run_wallet_protocol.py \
      0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
      --no-exec --no-history --no-tags --no-peers --no-archetype \
      --n-iter 500 2>&1 | grep -E "ACTIVITY|subgraph|fills"
  ```
  Expected:
  - `ACTIVITY_SOURCE:subgraph n_fills=33000+` in output
  - `activity_cap_hit=False` in the report header
  - No `(CAP HIT)` marker next to `activity_rows_fetched`

### Step 2.4: Commit

- [ ] Commit:
  ```bash
  git add \
    sniper/analysis/wallet_protocol/fetch.py \
    sniper/analysis/wallet_protocol/types.py \
    tests/test_wallet_protocol_fetch_subgraph_fallback.py
  git commit -m "feat(wallet-protocol): subgraph fallback in fetch.py when REST activity cap hit (C2 integration)"
  ```

- [ ] Write Supabase brain entry documenting closure.

---

## Task 3: ±2% Accounting Tie (Follow-up 4)

**Prerequisite:** Task 1 (capital ledger on ProtocolReport) must be verified first.

**Files:**
- Modify: `sniper/analysis/wallet_protocol/analyze_wallet.py` — add `_compute_accounting_tie`
- Modify: `sniper/analysis/wallet_protocol/report.py` — render accounting tie section
- Test: `tests/test_wallet_protocol_accounting_tie.py`

**Formula:**

```
unrealized_pnl = sum(
    (float(p.get("curPrice") or 0) - float(p.get("avgPrice") or 0)) * float(p.get("size") or 0)
    for p in open_positions
)
estimated_equity = net_capital_funded_usdc + realized_pnl + unrealized_pnl
actual_equity    = portfolio_value_usdc (from fetch_user_value())
delta            = estimated_equity - actual_equity
pct_error        = abs(delta) / max(abs(actual_equity), 1.0)
```

**Caveats (document in code):**
- `net_capital_funded` = USDC traded through CTF (SPLIT−MERGE), NOT the wallet's starting USDC balance. The tie can fail for wallets that have USDC sitting idle outside CTF positions.
- `realized_pnl` = sum of `realizedPnl` on `/closed-positions`. This should include settlement values (redemptions). If Polymarket's API books some redemptions separately, the tie will drift.
- `curPrice` may be stale (Polymarket caches prices). Small price movements cause noise around the 2% boundary.
- If the tie fails, emit the full discrepancy. "Fail" = informative signal, not a bug.

### Step 3.1: Write the failing tests

- [ ] Create `tests/test_wallet_protocol_accounting_tie.py`:

```python
"""Tests: ±2% accounting tie in analyze_wallet.

Hermetic — no live HTTP. CapitalLedger mocked.
"""
from __future__ import annotations
from dataclasses import replace

import pytest

from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
from sniper.analysis.wallet_protocol.analyze_wallet import _compute_accounting_tie
from sniper.analysis.wallet_protocol.types import ProtocolReport


def _ledger(net=10_000.0) -> CapitalLedger:
    return CapitalLedger(
        wallet="0xtest",
        net_capital_funded_usdc=net,
        total_deposits_usdc=net + 2_000.0,
        total_withdrawals_usdc=2_000.0,
        total_redemption_payout_usdc=0.0,
        n_splits=5, n_merges=2, n_redemptions=0,
    )


def _open_pos(cur_price, avg_price, size):
    return {"curPrice": str(cur_price), "avgPrice": str(avg_price), "size": str(size)}


# ---------------------------------------------------------------------------
# _compute_accounting_tie
# ---------------------------------------------------------------------------

def test_tie_passes_when_within_two_pct():
    """estimated ≈ actual → no ACCOUNTING_TIE_FAILED flag."""
    ledger = _ledger(net=10_000.0)
    realized_pnl = 1_000.0
    open_positions = [_open_pos(0.7, 0.5, 100.0)]  # unrealized = (0.7-0.5)*100 = 20
    # estimated = 10000 + 1000 + 20 = 11020
    actual = 11_000.0  # delta = 20, pct = 20/11000 = 0.18% < 2%

    flags = _compute_accounting_tie(ledger, realized_pnl, open_positions, actual)
    assert not any("ACCOUNTING_TIE_FAILED" in f for f in flags)
    assert any("ACCOUNTING_TIE_OK" in f for f in flags)


def test_tie_fails_when_outside_two_pct():
    """estimated >> actual → ACCOUNTING_TIE_FAILED flag."""
    ledger = _ledger(net=10_000.0)
    realized_pnl = 5_000.0
    open_positions = []
    # estimated = 10000 + 5000 = 15000
    actual = 12_000.0  # delta = 3000, pct = 3000/12000 = 25% >> 2%

    flags = _compute_accounting_tie(ledger, realized_pnl, open_positions, actual)
    assert any("ACCOUNTING_TIE_FAILED" in f for f in flags)
    fail_flag = next(f for f in flags if "ACCOUNTING_TIE_FAILED" in f)
    assert "estimated" in fail_flag
    assert "actual" in fail_flag
    assert "delta_pct" in fail_flag


def test_tie_skipped_when_no_ledger():
    """No capital ledger → no tie flag at all."""
    flags = _compute_accounting_tie(None, 1000.0, [], 11_000.0)
    assert flags == []


def test_tie_skipped_when_no_portfolio_value():
    """No portfolio_value_usdc → no tie flag."""
    ledger = _ledger(net=10_000.0)
    flags = _compute_accounting_tie(ledger, 1000.0, [], None)
    assert flags == []


def test_tie_shows_discrepancy_details():
    """Fail flag contains estimated, actual, delta, pct for human review."""
    ledger = _ledger(net=5_000.0)
    flags = _compute_accounting_tie(ledger, 0.0, [], 1_000.0)
    # estimated = 5000, actual = 1000 → 400% off
    fail_flag = next((f for f in flags if "ACCOUNTING_TIE_FAILED" in f), None)
    assert fail_flag is not None
    assert "5000" in fail_flag or "5,000" in fail_flag
    assert "1000" in fail_flag or "1,000" in fail_flag


# ---------------------------------------------------------------------------
# Integration: flags appear on ProtocolReport
# ---------------------------------------------------------------------------

def test_accounting_tie_flag_on_report():
    """ACCOUNTING_TIE_OK/FAILED must appear in report.data_quality_flags."""
    from unittest.mock import patch
    from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
    from sniper.analysis.wallet_protocol.types import WalletData

    closed = [
        {"conditionId": f"cid_{i}", "outcome": "Yes", "realizedPnl": "10.0",
         "avgPrice": "0.5", "size": "20", "totalBought": "100",
         "endDate": "2025-01-01T00:00:00Z", "curPrice": "1.0"}
        for i in range(30)
    ]
    data = WalletData(
        wallet="0xtest",
        closed_positions=closed,
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_000_000,
        capital_ledger=_ledger(net=10_000.0),
        portfolio_value_usdc=10_300.0,   # ~3% off from realized
        fetch_quality_flags=[],
    )

    report = analyze_wallet("0xtest", pre_fetched_data=data)
    tie_flags = [f for f in report.data_quality_flags if "ACCOUNTING_TIE" in f]
    assert len(tie_flags) == 1
```

- [ ] Run to confirm failure:
  ```bash
  python3 -m pytest tests/test_wallet_protocol_accounting_tie.py -q 2>&1 | head -20
  ```
  Expected: ImportError — `_compute_accounting_tie` not yet exported.

### Step 3.2: Implement `_compute_accounting_tie` in `analyze_wallet.py`

- [ ] Edit `sniper/analysis/wallet_protocol/analyze_wallet.py`:

Add import at top:
```python
from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger
```

Add this pure function before `analyze_wallet`:
```python
_ACCOUNTING_TIE_THRESHOLD = 0.02  # 2%


def _compute_accounting_tie(
    capital_ledger: "CapitalLedger | None",
    realized_pnl: float,
    open_positions: list[dict],
    portfolio_value_usdc: float | None,
) -> list[str]:
    """Check ±2% accounting tie: net_capital_funded + realized_pnl + unrealized ≈ portfolio_value.

    Returns a list of zero or one flag strings:
        ACCOUNTING_TIE_OK: estimated=X actual=Y delta_pct=Z%
        ACCOUNTING_TIE_FAILED: estimated=X actual=Y delta=D delta_pct=Z%

    Returns [] when capital_ledger is None or portfolio_value_usdc is None
    (tie cannot be evaluated without both inputs).

    Caveats:
    - net_capital_funded covers only USDC traded through CTF (SPLIT−MERGE).
      USDC sitting idle in the wallet is excluded. The tie will be negative
      for wallets that have significant idle USDC (estimated < actual).
    - realized_pnl = sum(realizedPnl on closed-positions). This includes
      settlement (redemption) values — cross-reference with capital_ledger's
      total_redemption_payout_usdc if the tie fails by roughly that amount.
    - open position prices (curPrice) may be stale. Small deviations are
      expected; 2% threshold is generous.
    """
    if capital_ledger is None or portfolio_value_usdc is None:
        return []

    unrealized_pnl = sum(
        (float(p.get("curPrice") or 0.0) - float(p.get("avgPrice") or 0.0))
        * float(p.get("size") or 0.0)
        for p in open_positions
    )
    estimated = capital_ledger.net_capital_funded_usdc + realized_pnl + unrealized_pnl
    actual = portfolio_value_usdc
    delta = estimated - actual
    pct = abs(delta) / max(abs(actual), 1.0)

    if pct <= _ACCOUNTING_TIE_THRESHOLD:
        return [
            f"ACCOUNTING_TIE_OK: estimated=${estimated:,.2f} actual=${actual:,.2f} "
            f"delta_pct={pct*100:.2f}%"
        ]
    return [
        f"ACCOUNTING_TIE_FAILED: estimated=${estimated:,.2f} actual=${actual:,.2f} "
        f"delta=${delta:,.2f} delta_pct={pct*100:.2f}% "
        f"(unrealized=${unrealized_pnl:,.2f} net_funded=${capital_ledger.net_capital_funded_usdc:,.2f})"
    ]
```

In `analyze_wallet`, after the capital ledger passthrough block, add:
```python
    # Follow-up 4: ±2% accounting tie.
    tie_flags = _compute_accounting_tie(
        data.capital_ledger,
        report.net_pnl,
        data.open_positions,
        data.portfolio_value_usdc,
    )
    report.data_quality_flags.extend(tie_flags)
```

### Step 3.3: Render accounting tie in `report.py`

- [ ] Edit `sniper/analysis/wallet_protocol/report.py`:

The `data_quality_flags` are already rendered in the Stage 1 section. The `ACCOUNTING_TIE_OK/FAILED` flag will appear there automatically. No extra rendering needed — the flag is human-readable.

However, to make it prominent, add it to the Capital ledger section when visible. In the Capital ledger section of `render_report`, add after the `portfolio_roi` line:
```python
        tie_flag = next(
            (f for f in report.data_quality_flags if f.startswith("ACCOUNTING_TIE")),
            None,
        )
        if tie_flag is not None:
            lines.append(f"- {tie_flag}")
```

### Step 3.4: Run tests and verify

- [ ] Run accounting tie tests:
  ```bash
  python3 -m pytest tests/test_wallet_protocol_accounting_tie.py -q
  ```
  Expected: all tests PASS.

- [ ] Run full regression:
  ```bash
  python3 -m pytest tests/test_wallet_protocol*.py -q
  ```
  Expected: no regressions.

- [ ] **Live verification on SwissMiss** with `--capital-ledger`:
  ```bash
  python3 scripts/run_wallet_protocol.py \
      0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
      --capital-ledger \
      --no-exec --no-history --no-tags --no-peers --no-archetype \
      --n-iter 500 2>&1 | grep -E "ACCOUNTING_TIE|portfolio_roi|net_capital"
  ```
  Expected:
  - Either `ACCOUNTING_TIE_OK` or `ACCOUNTING_TIE_FAILED` present (either is valid)
  - If FAILED, discrepancy surfaced explicitly — that's the correct behavior
  - `portfolio_roi` line shows SwissMiss's realized P&L / net_funded ratio

### Step 3.5: Commit

- [ ] Commit:
  ```bash
  git add \
    sniper/analysis/wallet_protocol/analyze_wallet.py \
    sniper/analysis/wallet_protocol/report.py \
    tests/test_wallet_protocol_accounting_tie.py
  git commit -m "feat(wallet-protocol): ±2% accounting tie check (Follow-up 4)"
  ```

- [ ] Write Supabase brain entry.

---

## Post-completion checklist

- [ ] Full test suite green:
  ```bash
  python3 -m pytest tests/ -q 2>&1 | tail -5
  ```
- [ ] All three commits on branch, PR or merge to main.
- [ ] Update `docs/superpowers/specs/wallet-protocol-bulletproofing.md`: mark C1 wiring and C2 wiring as CLOSED with commit SHAs.

---

## Scope notes and known limits

**Follow-up 2 token lookup coverage:**
The fallback builds a token-ID lookup from the most-recent REST rows (up to 3000). For wallets with very long histories, fills on very old markets won't be in the REST window. Those fills are dropped and flagged as `ACTIVITY_SUBGRAPH_UNMAPPED: n=K`. The count is emitted to the human so the gap is visible.

A future improvement: batch-query the Gamma API (`GET /markets?clob_token_ids=<ids>`) for the unknown tokens. Not in scope for this plan — it adds a dependency and latency that should be evaluated separately.

**Follow-up 4 tie expectations:**
The tie formula `net_capital_funded + realized_pnl + unrealized_pnl ≈ fetch_user_value()` excludes idle USDC not deployed to CTF. For wallets with large idle USDC balances, the estimated value will consistently understate actual value. If the tie fails, the discrepancy direction and magnitude tell the story:
- `estimated < actual` → wallet has idle USDC or income not captured by CTF events
- `estimated > actual` → data quality issue or P&L double-counting

**Follow-up 3 (FH6 live verify on flipper wallet) remains deferred** — no curated flipper-mode wallet available in the current set.
