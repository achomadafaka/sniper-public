# Analyzer Residual — Track 2: Goldsky Historical Fill Fetch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch a wallet's historical on-chain fills (maker/taker identity) from the Polymarket order-book subgraph and expose them through the existing `OnchainFillIdentityProvider`, unblocking Track 3 on historical data with no live loop.

**Architecture:** A single `goldsky_fills.py` fetch client queries the subgraph for `OrderFilled` events involving the wallet, paginates, caches per-wallet, and normalizes via the already-built `fill_event_from_subgraph_row`. The real subgraph schema is verified FIRST (Task 1) — if no endpoint is reachable, the track is blocked-pending-access and says so rather than shipping a fake.

**Tech Stack:** Python 3, GraphQL over `sniper.markets.polymarket_api` session helpers, JSON cache, pytest.

**Spec:** `docs/superpowers/specs/2026-05-31-analyzer-residual-work-design.md` §4.

**Branch:** `feat/analyzer-track2-goldsky` before Task 1.

**Depends on:** nothing in Track 1 (independent). Track 3 depends on this.

---

## File Structure

| File | Responsibility |
|---|---|
| `sniper/feeds/goldsky_fills.py` | NEW. `fetch_wallet_fills(wallet, *, refresh)` (subgraph GraphQL + paginate + cache) and `build_onchain_fill_provider(wallet)`. |
| `tests/feeds/test_goldsky_fills.py` | Offline tests (fixture GraphQL responses → FillEvents, cache dedup, transient-failure discipline). |

Reuses unchanged: `sniper/feeds/thesis_feed_types.py` (`FillEvent`), `sniper/feeds/fill_normalizer.py` (`fill_event_from_subgraph_row`), `sniper/analysis/winning_wallet_thesis/data/onchain_fill_identity.py` (`OnchainFillIdentityProvider`).

---

## Task 1: Verify the subgraph endpoint + schema (DEPENDENCY GATE)

**Files:**
- Create: `docs/audits/2026-05-31-goldsky-fill-endpoint-gate.md` (durable, auditable decision record)

- [ ] **Step 1: Locate any existing subgraph access in the repo**

Run:
```bash
grep -rniE "goldsky|subgraph|thegraph|orderfilled|orderFilled|gateway.thegraph" --include=*.py --include=*.env* --include=*.md . | grep -v node_modules | head -30
grep -riE "SUBGRAPH|GOLDSKY|GRAPH_API" .env .env.example 2>/dev/null
```
Expected: either a usable endpoint/key surfaces, or nothing.

- [ ] **Step 2: Confirm the OrderFilled schema (if an endpoint exists)**

If an endpoint is found, issue one read-only introspection/sample query for the `orderFilledEvents` (or equivalent) entity and record the real field names (maker, taker, makerAssetId, takerAssetId, makerAmountFilled, takerAmountFilled, timestamp, transactionHash, id). Compare to the fields `fill_event_from_subgraph_row` already reads.

- [ ] **Step 3: Write the decision to a durable audit doc**

Create `docs/audits/2026-05-31-goldsky-fill-endpoint-gate.md` with: the endpoint URL (or "none found"), a captured schema sample (entity + field names) or "n/a", the field reconciliation vs `fill_event_from_subgraph_row`, and an explicit **PROCEED** or **BLOCKED-PENDING-ACCESS** verdict with date and what access is needed. This is the auditable artifact — not just an in-branch note.

```markdown
# Audit — Goldsky / order-book subgraph fill endpoint gate (2026-05-31)

**Verdict:** PROCEED | BLOCKED-PENDING-ACCESS
**Endpoint:** <url or "none found in repo / env">
**Entity:** orderFilledEvents (or actual) — schema sample:
    <paste the queried fields, or "n/a — no endpoint">
**Field reconciliation vs fill_event_from_subgraph_row:** <match | differences listed>
**If BLOCKED:** access needed = subgraph URL + key (env `POLYMARKET_SUBGRAPH_URL`). Track 2/3
methods (first-attention, market-making, adverse-selection) stay `blocked_missing_feed`.
```

- [ ] **Step 4: Decision branch**

- If a working endpoint + schema exist → **PROCEED** to Task 2, reconciling `fill_event_from_subgraph_row` field names in the audit doc if they differ.
- If NOT → **STOP. Track 2/3 are blocked-pending-access.** Commit the audit doc with the BLOCKED verdict; do not ship a stub that pretends to fetch. Surface to the operator what access is needed.

```bash
git add docs/audits/2026-05-31-goldsky-fill-endpoint-gate.md
git commit -m "audit(feeds): Goldsky fill endpoint dependency gate verdict

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> This gate exists because the spec (§4) flags subgraph access as an unverified dependency. Per the project's verify-before-claiming rule, do not assume it.

---

## Task 2: `goldsky_fills.fetch_wallet_fills` (offline-testable)

**Files:**
- Create: `sniper/feeds/goldsky_fills.py`
- Test: `tests/feeds/test_goldsky_fills.py`

- [ ] **Step 1: Write the failing tests (injectable query fn — no network in CI)**

```python
# tests/feeds/test_goldsky_fills.py
from sniper.feeds.goldsky_fills import fetch_wallet_fills


def _fake_query(pages):
    calls = {"i": 0}
    def q(_endpoint, _gql, variables):
        i = calls["i"]; calls["i"] += 1
        return pages[i] if i < len(pages) else []
    return q


def test_fetch_paginates_and_normalizes(tmp_path):
    page1 = [{"id": "l1", "timestamp": "100", "maker": "0xM", "taker": "0xT",
              "makerAssetId": "5", "takerAssetId": "0", "makerAmountFilled": "1000000",
              "takerAmountFilled": "500000", "transactionHash": "0xtx1"}]
    fills = fetch_wallet_fills("0xW", endpoint="http://x", query_fn=_fake_query([page1, []]),
                               cache_dir=str(tmp_path))
    assert len(fills) == 1 and fills[0].maker == "0xm" and fills[0].tx_hash == "0xtx1"


def test_fetch_uses_cache_on_second_call(tmp_path):
    page = [{"id": "l1", "timestamp": "1", "maker": "0xa", "taker": "0xb",
             "makerAssetId": "1", "takerAssetId": "0", "makerAmountFilled": "1",
             "takerAmountFilled": "1", "transactionHash": "0xt"}]
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not refetch"))
    fetch_wallet_fills("0xW", endpoint="http://x", query_fn=_fake_query([page, []]), cache_dir=str(tmp_path))
    fills = fetch_wallet_fills("0xW", endpoint="http://x", query_fn=boom, cache_dir=str(tmp_path))
    assert len(fills) == 1                       # served from cache, query_fn not called
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/feeds/test_goldsky_fills.py -q`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `goldsky_fills.py`**

```python
# sniper/feeds/goldsky_fills.py
"""Fetch a wallet's historical fills from the Polymarket order-book subgraph (Goldsky).
Operator-triggered, cached, no live loop. The GraphQL query fn is injectable for offline
tests; the default posts to the endpoint via the project session."""
from __future__ import annotations

import json
import os
from typing import Callable

from sniper.feeds.fill_normalizer import fill_event_from_subgraph_row
from sniper.feeds.thesis_feed_types import FillEvent
from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "runs", "wallet_anatomy")

# entity/field names confirmed in Track 2 Task 1; adjust if the real schema differs.
_QUERY = """
query Fills($wallet: String!, $skip: Int!) {
  orderFilledEvents(first: 1000, skip: $skip, orderBy: timestamp,
                    where: {or: [{maker: $wallet}, {taker: $wallet}]}) {
    id timestamp maker taker makerAssetId takerAssetId
    makerAmountFilled takerAmountFilled transactionHash
  }
}"""


def _post_graphql(endpoint: str, gql: str, variables: dict) -> dict:
    """Minimal GraphQL POST. polymarket_api._request_json is GET-only, so post directly with
    httpx (bounded timeout). Kept tiny and dependency-light; not a general client."""
    import httpx
    resp = httpx.post(endpoint, json={"query": gql, "variables": variables},
                      headers={"Content-Type": "application/json",
                               "User-Agent": "polymarket-insider-tracker/1.0"},
                      timeout=30.0)
    resp.raise_for_status()
    return resp.json() or {}


def _default_query(endpoint: str, gql: str, variables: dict) -> list[dict]:
    data = _post_graphql(endpoint, gql, variables)
    return ((data or {}).get("data") or {}).get("orderFilledEvents") or []


def fetch_wallet_fills(wallet: str, *, endpoint: str | None = None,
                       query_fn: Callable[[str, str, dict], list[dict]] | None = None,
                       refresh: bool = False, cache_dir: str | None = None) -> list[FillEvent]:
    root = cache_dir or CACHE_DIR
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, f"{wallet.lower()}_fills.json")
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return [FillEvent(**r) for r in json.load(f)]
    q = query_fn or _default_query
    ep = endpoint or os.environ.get("POLYMARKET_SUBGRAPH_URL", "")
    rows: list[dict] = []
    skip = 0
    while True:
        page = q(ep, _QUERY, {"wallet": wallet.lower(), "skip": skip})
        if not page:
            break
        rows.extend(page)
        skip += len(page)
        if len(page) < 1000:
            break
    fills = [fill_event_from_subgraph_row(r) for r in rows]
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump([f_.__dict__ for f_ in fills], f)
    os.replace(tmp, path)
    return fills


def build_onchain_fill_provider(wallet: str, *, refresh: bool = False) -> OnchainFillIdentityProvider:
    return OnchainFillIdentityProvider(fills=fetch_wallet_fills(wallet, refresh=refresh))
```

> Implementer note (Task 1 output): `_post_graphql` already uses `httpx.post` directly (the repo's `_request_json` is GET-only — do not pass it a `method=`/`json_body=`). Reconcile `_QUERY` entity/field names with the schema confirmed in the Task 1 audit doc before the live smoke.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/feeds/test_goldsky_fills.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/goldsky_fills.py tests/feeds/test_goldsky_fills.py
git commit -m "feat(feeds): Goldsky historical fill fetch (cached, offline-testable)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: Live read-only smoke (gated) + Track 2 verification

**Files:**
- Test: `tests/feeds/test_goldsky_fills_live.py`

- [ ] **Step 1: Write a skip-gated live smoke**

```python
import os
import pytest
from sniper.feeds.goldsky_fills import fetch_wallet_fills

ENDPOINT = os.environ.get("POLYMARKET_SUBGRAPH_URL")


@pytest.mark.skipif(not ENDPOINT, reason="POLYMARKET_SUBGRAPH_URL not set (no live subgraph in CI)")
def test_live_fetch_returns_fills_for_known_wallet(tmp_path):
    fills = fetch_wallet_fills("0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
                               cache_dir=str(tmp_path))
    assert isinstance(fills, list)               # real call; may be empty if subgraph lacks history
```

- [ ] **Step 2: Run the suite + schema**

Run:
```bash
python3 -m pytest tests/feeds -q
python3 scripts/validate_schema_refs.py
```
Expected: offline tests pass; live smoke SKIPS without `POLYMARKET_SUBGRAPH_URL`.

- [ ] **Step 3: Commit + merge**

```bash
git add tests/feeds/test_goldsky_fills_live.py
git commit -m "test(feeds): gated live Goldsky smoke

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git checkout main && git merge --no-ff feat/analyzer-track2-goldsky -m "merge: analyzer Track 2 — Goldsky historical fill fetch"
git push origin main && git branch -d feat/analyzer-track2-goldsky
```

## Self-Review
- **Spec §4 (fetch client, cache, normalizer reuse, dependency gate):** Tasks 1–3. ✓
- **Dependency-verified-not-assumed:** Task 1 is an explicit STOP gate that writes a durable audit doc (`docs/audits/2026-05-31-goldsky-fill-endpoint-gate.md`) with a PROCEED/BLOCKED verdict. ✓
- **POST mechanism explicit:** `_post_graphql` uses `httpx.post` directly — the repo's `_request_json` is GET-only and is NOT reused for the query. ✓
- **No live API in CI:** offline tests inject `query_fn`; live smoke skips without env var. ✓
- **Types:** `fetch_wallet_fills`/`build_onchain_fill_provider` (Task 2) reuse `FillEvent` + `fill_event_from_subgraph_row` + `OnchainFillIdentityProvider` (all pre-existing); Track 3 consumes `build_onchain_fill_provider`.
- **Remaining schema-reconciliation:** `_QUERY` entity/field names are confirmed against the live schema in the Task 1 audit doc before the smoke — a verify step, not skipped logic; the offline path and interface are fully specified.
