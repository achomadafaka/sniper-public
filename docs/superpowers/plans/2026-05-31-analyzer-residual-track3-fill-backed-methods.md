# Analyzer Residual — Track 3: Fill-Backed Methods Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Feed the Track-2 historical fills to the fill-backed detectors — first-attention (already implemented; just needs condition-tagged fills), market-making (maker fraction), and adverse-selection (post-fill drift proxy) — while keeping MTM drawdown deferred.

**Architecture:** Fills are tagged with `condition_id` (Task 1) so `OnchainFillIdentityProvider.fills_for_market` can group them. `first_attention_discovery` is **already implemented and already wired** in the runner — Task 2 only verifies it flips to `supported_proxy` once fills carry `condition_id` (no signature change). The two microstructure detectors are real stubs; Task 3 implements them with an injectable provider, deriving the wallet from `ctx.book.wallet` (no new `wallet=` param). No order book — adverse-selection drift is reconstructed from subsequent fills, an explicit proxy.

**Tech Stack:** Python 3, pytest. Consumes Track 2's `build_onchain_fill_provider`.

**Spec:** `docs/superpowers/specs/2026-05-31-analyzer-residual-work-design.md` §5.

**Branch:** `feat/analyzer-track3-methods` before Task 1.

**Depends on:** Track 2 (fills). If Track 2 is blocked-pending-access, this track is too — the detectors return `blocked_missing_feed` with no provider, so the analyzer stays honest until fills exist.

---

## File Structure

| File | Responsibility |
|---|---|
| `sniper/feeds/goldsky_fills.py` | Modify: `attach_condition_ids(fills, asset_to_condition)` + build the map for the wallet's (resolved-inclusive) conditions; call it in `build_onchain_fill_provider`. |
| `sniper/analysis/winning_wallet_thesis/methods/microstructure.py` | Implement maker-fraction + post-fill-drift proxy (were blocked stubs). |
| `sniper/analysis/winning_wallet_thesis/methods/runner.py` | Pass `fill_identity` to the two microstructure detectors (first-attention is already wired). |
| `sniper/analysis/winning_wallet_thesis/cli.py` | Optional `--with-fills` building the provider via Track 2. |
| `tests/winning_wallet_thesis/test_method_attention.py`, `test_method_microstructure.py`, `test_method_runner.py`, `tests/feeds/test_goldsky_fills.py` | Fixture-fill tests. |

---

## Task 1: Tag fills with `condition_id` (asset→condition map, resolved-inclusive)

**Files:**
- Modify: `sniper/feeds/goldsky_fills.py`
- Test: `tests/feeds/test_goldsky_fills.py`

- [ ] **Step 1: Write the failing test**

```python
def test_fills_get_condition_id_from_asset_map():
    from sniper.feeds.goldsky_fills import attach_condition_ids
    from sniper.feeds.thesis_feed_types import FillEvent
    fills = [FillEvent("tx", "l", 1, "0xm", "0xt", "A", "0", 1, 1)]   # outcome token "A", USDC "0"
    out = attach_condition_ids(fills, asset_to_condition={"A": "0xcond"})
    assert out[0].condition_id == "0xcond"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/feeds/test_goldsky_fills.py::test_fills_get_condition_id_from_asset_map -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

```python
# add to sniper/feeds/goldsky_fills.py
from dataclasses import replace

def attach_condition_ids(fills, asset_to_condition: dict[str, str]):
    """The non-USDC side of a fill is the outcome token; map it to its condition_id.
    (USDC asset id is '0'.) FillEvent is frozen -> use dataclasses.replace."""
    out = []
    for f in fills:
        token = f.maker_asset_id if f.maker_asset_id != "0" else f.taker_asset_id
        out.append(replace(f, condition_id=asset_to_condition.get(token)))
    return out
```

Build `asset_to_condition` for the **wallet's own conditions, including resolved markets**.
Do NOT use `polymarket_api.fetch_market_index` — it queries `active=true, closed=false`, so
historical fills on resolved markets would be unmapped. Instead map each condition the wallet
touched via `polymarket_api.fetch_market_by_condition(cond)` and parse its `clobTokenIds` with
`polymarket_api._parse_token_ids` (token_id → condition_id). The wallet's condition set is the
one the extractor already enriches (`EnrichedLedger`/resolutions), so reuse that universe rather
than refetching. `build_onchain_fill_provider(wallet)` calls `attach_condition_ids` before wrapping.

> Implementer note: a fill whose token is not in the map keeps `condition_id=None` (it simply
> won't be grouped by `fills_for_market`) — acceptable, not an error.

- [ ] **Step 4: Verify + commit**

Run: `python3 -m pytest tests/feeds/test_goldsky_fills.py -q` → PASS
```bash
git add sniper/feeds/goldsky_fills.py tests/feeds/test_goldsky_fills.py
git commit -m "feat(feeds): tag fills with condition_id (resolved-inclusive map)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: Verify first-attention flips with condition-tagged fills (no code change)

`first_attention_discovery` is already implemented and already runs in the runner with the
provider; it returns `blocked_missing_feed` only when `not fill_identity.available` OR no
condition-tagged fills land in its window. This task proves Task 1 is the real unblocker — no
signature change, no `wallet=` param (it derives the wallet from `ep.wallet`).

**Files:**
- Test: `tests/winning_wallet_thesis/test_method_attention.py`

- [ ] **Step 1: Write the tests**

```python
def test_first_attention_flips_to_supported_with_condition_tagged_fills():
    from sniper.analysis.winning_wallet_thesis.methods.attention import first_attention_discovery
    from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
    ep = PositionEpisode("0xw", "cond1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    ctx = ClusterContext(EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1, [("cond1", 0, 0)]),
                         PositionBook("0xw", [ep]))
    fills = [
        FillEvent("t0", "l0", 100, "0xw", "0xother", "A", "0", 1, 1, condition_id="cond1"),  # her own, first
        FillEvent("t1", "l1", 200, "0xf1", "0xx", "A", "0", 1, 1, condition_id="cond1"),      # follower 1
        FillEvent("t2", "l2", 300, "0xf2", "0xy", "A", "0", 1, 1, condition_id="cond1"),      # follower 2
    ]
    f = first_attention_discovery(ctx, fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "supported_proxy"
    assert f.evidence["first_attention_events"] == 1


def test_first_attention_blocks_when_fills_lack_condition_id():
    # fills present but untagged -> fills_for_market finds nothing -> evaluable 0 -> blocked
    from sniper.analysis.winning_wallet_thesis.methods.attention import first_attention_discovery
    from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
    ep = PositionEpisode("0xw", "cond1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=100, resolved=True, won=1)
    ctx = ClusterContext(EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1, [("cond1", 0, 0)]),
                         PositionBook("0xw", [ep]))
    fills = [FillEvent("t0", "l0", 100, "0xw", "0xo", "A", "0", 1, 1)]  # condition_id=None
    f = first_attention_discovery(ctx, fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state == "blocked_missing_feed"
```

- [ ] **Step 2: Run**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_attention.py -q`
Expected: PASS (no production change needed; if a fixture reveals a real gap, fix it minimally and note it).

- [ ] **Step 3: Commit**

```bash
git add tests/winning_wallet_thesis/test_method_attention.py
git commit -m "test(thesis): first-attention flips to supported with condition-tagged fills

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: market-making + adverse-selection on fills

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/methods/microstructure.py`
- Test: `tests/winning_wallet_thesis/test_method_microstructure.py`

- [ ] **Step 1: Write failing tests**

```python
def _ctx(cond="c1"):
    from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
    ep = PositionEpisode("0xw", cond, "e1", 0, 0, buy_shares=1, buy_cost=1,
                         first_buy_ts=1, resolved=True, won=1)
    return ClusterContext(EdgeCluster("c", "x", {}, "tag", 1, 1, 1, 1, [(cond, 0, 0)]),
                          PositionBook("0xw", [ep]))


def test_market_making_uses_maker_fraction_when_fills_present():
    from sniper.analysis.winning_wallet_thesis.methods.microstructure import market_making_spread_capture
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    fills = [FillEvent("t", f"l{i}", i + 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1")
             for i in range(8)]                                       # all maker == wallet
    f = market_making_spread_capture(_ctx(), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state != "blocked_missing_feed"
    assert f.evidence["maker_fraction"] == 1.0


def test_market_making_blocks_without_provider():
    from sniper.analysis.winning_wallet_thesis.methods.microstructure import market_making_spread_capture
    f = market_making_spread_capture(_ctx(), fill_identity=None)
    assert f.state == "blocked_missing_feed"


def test_adverse_selection_is_labelled_proxy_with_fills():
    from sniper.analysis.winning_wallet_thesis.methods.microstructure import adverse_selection_aware_making
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    # wallet makes at price 0.50; subsequent fills on the same asset drift UP (favorable to a maker buyer)
    fills = [FillEvent("t", "l0", 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1", price=0.50),
             FillEvent("t", "l1", 5, "0xa", "0xb", "A", "0", 1, 1, condition_id="c1", price=0.55),
             FillEvent("t", "l2", 9, "0xc", "0xd", "A", "0", 1, 1, condition_id="c1", price=0.58)]
    f = adverse_selection_aware_making(_ctx(), fill_identity=OnchainFillIdentityProvider(fills))
    assert f.state != "blocked_missing_feed"
    assert f.controls.get("proxy")                                    # explicitly labelled a proxy


def test_adverse_selection_blocks_without_provider():
    from sniper.analysis.winning_wallet_thesis.methods.microstructure import adverse_selection_aware_making
    f = adverse_selection_aware_making(_ctx(), fill_identity=None)
    assert f.state == "blocked_missing_feed"
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_microstructure.py -q`
Expected: FAIL (detectors take only `ctx`; no `fill_identity`).

- [ ] **Step 3: Implement**

```python
# sniper/analysis/winning_wallet_thesis/methods/microstructure.py
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis.data.fill_identity import FillIdentityProvider
from sniper.analysis.winning_wallet_thesis.methods.context import ClusterContext
from sniper.analysis.winning_wallet_thesis.methods.types import MethodFinding

_BIG_TS = 1 << 62


def _wallet(ctx: ClusterContext) -> str:
    return (ctx.book.wallet or "").lower()


def _blocked(method: str, blocker: str) -> MethodFinding:
    return MethodFinding(method, "blocked_missing_feed", 0, None, 0,
                         evidence={}, controls={}, blockers=[blocker], runnability="data_blocked")


def market_making_spread_capture(ctx: ClusterContext, *,
                                 fill_identity: FillIdentityProvider | None = None) -> MethodFinding:
    if fill_identity is None or not fill_identity.available:
        return _blocked("market_making_spread_capture", "missing_maker_taker_or_order_detail")
    w = _wallet(ctx)
    wf = fill_identity.fills_for_wallet(w, start_ts=0, end_ts=_BIG_TS)
    if not wf:
        return _blocked("market_making_spread_capture", "missing_wallet_identified_fills")
    maker = sum(1 for f in wf if (f.maker or "").lower() == w)
    frac = maker / len(wf)
    return MethodFinding("market_making_spread_capture", "descriptive", frac * 100, frac, len(wf),
                         evidence={"maker_fraction": round(frac, 4), "n_fills": len(wf)},
                         controls={"note": "maker_fraction_is_attribution_not_a_promotable_edge"},
                         blockers=[], runnability="descriptive_only")


def adverse_selection_aware_making(ctx: ClusterContext, *,
                                   fill_identity: FillIdentityProvider | None = None) -> MethodFinding:
    if fill_identity is None or not fill_identity.available:
        return _blocked("adverse_selection_aware_making", "missing_passive_fill_baseline")
    w = _wallet(ctx)
    maker_fills = [f for f in fill_identity.fills_for_wallet(w, start_ts=0, end_ts=_BIG_TS)
                   if (f.maker or "").lower() == w and f.price is not None]
    if not maker_fills:
        return _blocked("adverse_selection_aware_making", "missing_wallet_identified_fills")
    drifts = []
    for mf in maker_fills:
        later = sorted((x for x in fill_identity.fills_for_market(mf.condition_id, start_ts=mf.ts + 1, end_ts=_BIG_TS)
                        if x.price is not None), key=lambda x: x.ts)
        if later:
            drifts.append(later[-1].price - mf.price)            # sparse path: last subsequent fill
    if not drifts:
        return _blocked("adverse_selection_aware_making", "no_subsequent_fills_for_drift")
    avg_drift = sum(drifts) / len(drifts)
    state = "supported_proxy" if avg_drift >= 0 else "contradicted"   # non-adverse vs adverse
    return MethodFinding("adverse_selection_aware_making", state, avg_drift * 100, avg_drift, len(drifts),
                         evidence={"avg_post_fill_drift": round(avg_drift, 4), "n_maker_fills": len(drifts)},
                         controls={"proxy": "drift_reconstructed_from_subsequent_fills_not_orderbook"},
                         blockers=[], runnability="replication_required" if state == "supported_proxy" else "descriptive_only")
```

- [ ] **Step 4: Verify + commit**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_microstructure.py -q` → PASS
```bash
git add sniper/analysis/winning_wallet_thesis/methods/microstructure.py tests/winning_wallet_thesis/test_method_microstructure.py
git commit -m "feat(thesis): maker-fraction + adverse-selection proxy from fills

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: Wire the microstructure detectors to the provider + CLI flag

`run_detectors` already accepts `fill_identity` and already passes it to `first_attention_discovery`
via the explicit `findings.extend([...])` block. Only the two microstructure calls need the provider.
Do NOT add the methods to the `DETECTORS` tuple — they are in the extend block, so adding them would
double-run. No `wallet=` param (the detectors derive it from `ctx.book.wallet`).

**Files:**
- Modify: `sniper/analysis/winning_wallet_thesis/methods/runner.py`
- Modify: `sniper/analysis/winning_wallet_thesis/cli.py`
- Test: `tests/winning_wallet_thesis/test_method_runner.py`

- [ ] **Step 1: Write the failing test**

```python
def test_runner_feeds_provider_to_microstructure_detectors():
    from sniper.analysis.winning_wallet_thesis.methods.runner import run_detectors
    from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
    from sniper.feeds.thesis_feed_types import FillEvent
    from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
    ep = PositionEpisode("0xw", "c1", "e1", 0, 0, buy_shares=10, buy_cost=4,
                         first_buy_ts=1, resolved=True, won=1)
    cluster = EdgeCluster("c", "UFC", {"tag": "UFC"}, "tag", 1, 1, 1, 1, [("c1", 0, 0)])
    fills = [FillEvent("t", f"l{i}", i + 1, "0xw", "0xo", "A", "0", 1, 1, condition_id="c1")
             for i in range(5)]
    out = run_detectors(PositionBook("0xw", [ep]), [cluster],
                        fill_identity=OnchainFillIdentityProvider(fills))
    methods = [f.method for f in out["c"]]
    assert methods.count("market_making_spread_capture") == 1        # NOT double-run
    mm = next(f for f in out["c"] if f.method == "market_making_spread_capture")
    assert mm.state != "blocked_missing_feed"                        # fed the provider
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_method_runner.py::test_runner_feeds_provider_to_microstructure_detectors -q`
Expected: FAIL (microstructure detectors called without the provider → still blocked).

- [ ] **Step 3: Implement**

In `runner.run_detectors`, change the extend block's two microstructure calls to pass the provider
(leave first-attention as-is, leave `DETECTORS` unchanged):
```python
        findings.extend([
            stale_liquidity(ctx, price_path=price_path),
            dead_hour_timing(ctx),
            first_attention_discovery(ctx, fill_identity=identity),
            market_making_spread_capture(ctx, fill_identity=identity),
            adverse_selection_aware_making(ctx, fill_identity=identity),
        ])
```
(`identity = fill_identity or UnavailableFillIdentityProvider()` already exists; `Unavailable*` has
`available = False`, so with no provider both microstructure detectors return `blocked_missing_feed`.)

In `cli.run_extractor` (or the equivalent entry that builds the run), add `with_fills: bool = False`;
when set, `fill_identity = build_onchain_fill_provider(wallet)` (Track 2) and pass it into
`run_detectors`. Default off — no behavior change without the flag.

- [ ] **Step 4: Full regression + schema**

Run:
```bash
python3 -m pytest tests/winning_wallet_thesis tests/feeds tests/thesis_cards -q
python3 scripts/validate_schema_refs.py
```
Expected: all pass. Without `--with-fills` the three detectors stay `blocked_missing_feed` (honest
default); with fills + a reachable subgraph they produce real states.

- [ ] **Step 5: Commit + merge**

```bash
git add sniper/analysis/winning_wallet_thesis/methods/runner.py sniper/analysis/winning_wallet_thesis/cli.py tests/winning_wallet_thesis/test_method_runner.py
git commit -m "feat(thesis): feed fill provider to microstructure detectors + --with-fills CLI

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git checkout main && git merge --no-ff feat/analyzer-track3-methods -m "merge: analyzer Track 3 — fill-backed methods"
git push origin main && git branch -d feat/analyzer-track3-methods
```

## Self-Review
- **Spec §5 (first-attention, market-making, adverse-selection proxy; MTM deferred):** Task 2 (first-attention already implemented), Task 3 (microstructure), Task 4 (wiring). MTM untouched. ✓
- **Codex fixes applied:** asset→condition uses `fetch_market_by_condition` (resolved-inclusive), NOT the active-only `fetch_market_index` (Task 1); `first_attention_discovery` keeps deriving the wallet from episodes — no `wallet=` param, evidence key is `first_attention_events` (Task 2); runner feeds only the two microstructure detectors, no `DETECTORS` change / no double-run (Task 4). ✓
- **Honest defaults:** all three stay `blocked_missing_feed` without a provider; `--with-fills` opt-in; adverse-selection labelled proxy in `controls`. ✓
- **Types:** `attach_condition_ids` (Task 1) used by `build_onchain_fill_provider`; microstructure detectors gain `(*, fill_identity=None)` and derive the wallet from `ctx.book.wallet`; `run_detectors` already has `fill_identity` and is consumed by `cli`.
- **Depends-on honesty:** if Track 2's gate is BLOCKED, no provider is built → detectors stay blocked → analyzer remains truthful.
