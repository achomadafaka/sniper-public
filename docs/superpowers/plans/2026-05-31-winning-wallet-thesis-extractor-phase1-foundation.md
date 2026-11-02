# Winning-Wallet Thesis Extractor — Phase 1 (Foundation + Cluster Engine) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the proven `scripts/wallet_anatomy.py` prototype into tested modules under `sniper/analysis/winning_wallet_thesis/`, separated per spec rev 2 (ledger → enrich → position-book → slicer + shuffle-baseline → cluster-builder → profile assembler + CLI), producing an **unvalidated** multi-cluster thesis profile for one operator-curated winning wallet.

**Architecture:** Clean module boundaries — ingestion/normalization (`ledger`) never decides edge; enrichment is platform-tag/series-driven with no hardcoded entities; the position-builder reconstructs episodes; the slicer computes `(r − p)`/PnL surfaces plus a within-wallet outcome-shuffle false-discovery baseline; the new `edge_cluster_builder` forms guarded candidate clusters (`n_events ≥ 5`, `n_positions ≥ 15`, conjunction depth ≤ 2, overlap reported); the assembler ranks them into a profile. Detector pack and validation spine are Phases 2–3 (separate plans). Phase 1 is **not a validated thesis extractor** until Phase 3 wires the validation spine.

**Tech Stack:** Python 3, dataclasses, `pytest`, the existing `sniper.markets.polymarket_api` Data-/Gamma-API client, JSON disk caches under `runs/wallet_anatomy/` (gitignored).

**Spec:** `docs/superpowers/specs/2026-05-31-winning-wallet-thesis-extractor-design.md` (rev 2, commit `2c5771b`).

**Source of proven logic to extract:** `scripts/wallet_anatomy.py`, `tests/test_wallet_anatomy.py` (16 focused anatomy tests; 48 tests when combined with tag-backfill tests).

---

## Pre-flight (operator/first task)

Spec rev 2 §9 requires implementation start on a feature branch, not dirty `main`. The current tree has the uncommitted foundation work (`scripts/wallet_anatomy.py`, `tests/test_wallet_anatomy.py`, `sniper/markets/polymarket_api.py`, `.gitignore`). Task 0 lands those, then all Phase-1 modules build on the branch.

## File Structure

New package `sniper/analysis/winning_wallet_thesis/`:

| File | Responsibility |
|---|---|
| `__init__.py` | Package marker; re-export the public `run_extractor` entry. |
| `types.py` | Frozen-ish dataclasses from spec §8: `LedgerRow` (including original feed `seq` so same-timestamp rows stay ordered), `WalletEventLedger`, `EnrichedLedger`, `PositionEpisode`, `EventBasket`, `PositionBook`, `EdgeSlice`, `ShuffleBaseline`, `EdgeCluster`, `ThesisProfile`. No logic. |
| `ledger.py` | `build_ledger(wallet, *, refresh) -> WalletEventLedger`. Fetch full activity (extract `load_activity`) and normalize each row into a typed `LedgerRow`. Must not decide edge. |
| `enrichment.py` | `enrich_ledger(ledger, *, refresh) -> EnrichedLedger`. Attach resolution (`r`), platform tags, parent category, specific tag, and series/slug-derived template. Extract `resolve_conditions`, `load_event_tags`, `parent_category`, `specific_tag`; add `event_template`. Transient failures stay unknown. |
| `position_builder.py` | `build_position_book(enriched) -> PositionBook`. Fold ledger rows into `PositionEpisode`s (episode-split on flat-then-rebuy) + a minimal `EventBasket` per event_slug + a reward/inventory book. Extract `reconstruct_positions` + `enrich` field-mapping. |
| `slicer.py` | `slice_edges(book) -> dict[str, dict[str, EdgeSlice]]` and `shuffle_baseline(book, *, n_shuffles, seed) -> ShuffleBaseline`. Extract `Slice`, `slice_by`, `slice_by_tags`, `_price_regime`, `_wilson`, `_entry_hour`, `reward_attribution`, `reward_share_pct`; add the shuffle baseline. |
| `clusters.py` | `build_clusters(slice_tables, *, min_events, min_positions, max_depth) -> list[EdgeCluster]`. NEW. Candidate clusters from green/loss slices + depth-≤2 conjunctions; apply guards; report overlap. |
| `assembler.py` | `assemble_profile(wallet, book, slice_tables, baseline, clusters) -> ThesisProfile`. NEW. Rank clusters, attach loss centers, reward attribution, runnability hint, deterministic `run_id`. |
| `cli.py` | `main()` argparse entry for `scripts/run_winning_wallet_thesis.py`. |

New thin script: `scripts/run_winning_wallet_thesis.py` → calls `winning_wallet_thesis.cli.main()`.

New tests under `tests/winning_wallet_thesis/`: one file per module.

`scripts/wallet_anatomy.py` stays as the prototype/throwaway runner during Phase 1 (delete in Phase 3 once parity is proven); do not edit it in this plan except where Task 0 lands it.

---

## Task 0: Land the foundation on a feature branch

**Files:**
- Modify (git only): `scripts/wallet_anatomy.py`, `tests/test_wallet_anatomy.py`, `sniper/markets/polymarket_api.py`, `.gitignore`

- [ ] **Step 1: Create the feature branch carrying current work**

Run:
```bash
git rev-parse --abbrev-ref HEAD   # confirm starting point
git checkout -b feat/winning-wallet-thesis
```
Expected: switched to a new branch with the dirty foundation files intact.

- [ ] **Step 2: Verify the foundation is green before committing**

Run: `python3 -m pytest tests/test_wallet_anatomy.py -q`
Expected: PASS (16 tests as of `2c5771b`). Optional broader check: `python3 -m pytest tests/test_wallet_anatomy.py tests/test_backfill_market_tags.py -q` → 48 tests.

- [ ] **Step 3: Commit the foundation**

```bash
git add scripts/wallet_anatomy.py tests/test_wallet_anatomy.py sniper/markets/polymarket_api.py .gitignore
git commit -m "feat(wallet-anatomy): prototype foundation + logger bugfix + run-cache gitignore

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```
Expected: clean commit; `git status` shows only untracked `runs/` (ignored).

---

## Task 1: Package skeleton + `types.py`

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/__init__.py`
- Create: `sniper/analysis/winning_wallet_thesis/types.py`
- Test: `tests/winning_wallet_thesis/test_types.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_types.py
from sniper.analysis.winning_wallet_thesis import types as T


def test_position_episode_realized_pnl_held_winner():
    ep = T.PositionEpisode(
        wallet="0xw", condition_id="0xc", event_slug="ufc-x", outcome_index=0,
        episode_index=0, buy_shares=100.0, buy_cost=40.0, sell_shares=0.0,
        sell_proceeds=0.0, resolved=True, won=1,
    )
    # held 100 winning shares -> $100 payout, cost $40 -> +$60
    assert round(ep.realized_pnl, 2) == 60.0
    assert round(ep.entry_vwap, 4) == 0.40


def test_position_episode_unresolved_pnl_is_none():
    ep = T.PositionEpisode(wallet="0xw", condition_id="0xc", event_slug="e",
                           outcome_index=0, episode_index=0, buy_shares=10.0,
                           buy_cost=5.0, resolved=False)
    assert ep.realized_pnl is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: sniper.analysis.winning_wallet_thesis`.

- [ ] **Step 3: Write `types.py`**

```python
# sniper/analysis/winning_wallet_thesis/types.py
"""Data objects for the winning-wallet thesis extractor (spec rev 2 §8). No logic
beyond pure derived fields; ingestion/edge decisions live in the other modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

RESOLUTION_WIN = 0.97
RESOLUTION_LOSS = 0.03


@dataclass
class LedgerRow:
    kind: str                       # buy|sell|redeem|split|merge|reward
    wallet: str
    condition_id: str               # can be empty for wallet-level reward/yield rows
    ts: int
    seq: int = 0                    # original feed order; tie-breaker for same-timestamp rows
    outcome_index: int | None = None
    outcome: str = ""
    price: float = 0.0
    size: float = 0.0
    usdc: float = 0.0
    tx: str = ""
    event_slug: str = ""
    title: str = ""


@dataclass
class WalletEventLedger:
    wallet: str
    rows: list[LedgerRow] = field(default_factory=list)
    raw_activity: list[dict] = field(default_factory=list)   # kept for reward attribution


@dataclass
class EnrichedLedger:
    ledger: WalletEventLedger
    resolutions: dict[str, dict] = field(default_factory=dict)   # condition_id -> {resolved,winner,push}
    tags: dict[str, list[str]] = field(default_factory=dict)     # event_slug -> [tag]
    templates: dict[str, str] = field(default_factory=dict)      # event_slug -> template


@dataclass
class PositionEpisode:
    wallet: str
    condition_id: str
    event_slug: str
    outcome_index: int
    episode_index: int
    outcome: str = ""
    title: str = ""
    buy_shares: float = 0.0
    buy_cost: float = 0.0
    sell_shares: float = 0.0
    sell_proceeds: float = 0.0
    split_shares: float = 0.0
    merge_shares: float = 0.0
    redeemed: bool = False
    first_buy_ts: int | None = None
    last_ts: int = 0
    resolved: bool = False
    won: int | None = None
    is_push: bool = False
    # semantic (filled by enrichment mapping)
    category: str = ""
    subcat: str = ""
    tags: list[str] = field(default_factory=list)
    template: str = ""

    @property
    def entry_vwap(self) -> float:
        return self.buy_cost / self.buy_shares if self.buy_shares > 0 else 0.0

    @property
    def is_directional(self) -> bool:
        return self.buy_shares > 0

    @property
    def exit_mechanism(self) -> str:
        if not self.resolved and not self.redeemed:
            return "open"
        if self.sell_shares > 0 and self.sell_shares >= 0.5 * max(self.buy_shares, 1e-9):
            return "sold"
        if self.redeemed or self.resolved:
            return "held"
        return "sold" if self.sell_shares > 0 else "open"

    @property
    def realized_pnl(self) -> float | None:
        if not self.resolved or self.won is None:
            return None
        held_shares = max(self.buy_shares - self.sell_shares, 0.0)
        payout = held_shares * (0.5 if self.is_push else (1.0 if self.won else 0.0))
        return self.sell_proceeds + payout - self.buy_cost


@dataclass
class EventBasket:
    event_slug: str
    condition_ids: set[str] = field(default_factory=set)
    net_cost: float = 0.0
    net_proceeds: float = 0.0
    redeem_payout: float = 0.0
    split_shares: float = 0.0
    merge_shares: float = 0.0
    rewards: float = 0.0
    final_pnl: float | None = None


@dataclass
class PositionBook:
    wallet: str
    episodes: list[PositionEpisode] = field(default_factory=list)
    baskets: dict[str, EventBasket] = field(default_factory=dict)
    reward_income: dict[str, float] = field(default_factory=dict)

    def resolved_directional(self) -> list[PositionEpisode]:
        return [e for e in self.episodes
                if e.is_directional and e.resolved and e.won is not None and not e.is_push]


@dataclass
class EdgeSlice:
    slice_key: str
    axis: str
    n_positions: int = 0
    n_events: int = 0
    wins: int = 0
    entry_sum: float = 0.0
    pnl: float = 0.0
    cost: float = 0.0
    members: list[PositionEpisode] = field(default_factory=list)

    @property
    def win_rate(self) -> float: return self.wins / self.n_positions if self.n_positions else 0.0
    @property
    def mean_entry(self) -> float: return self.entry_sum / self.n_positions if self.n_positions else 0.0
    @property
    def edge_pp(self) -> float: return (self.win_rate - self.mean_entry) * 100.0
    @property
    def roi_pct(self) -> float: return self.pnl / self.cost * 100.0 if self.cost else 0.0


@dataclass
class ShuffleBaseline:
    n_shuffles: int
    expected_green: float
    observed_green: int
    obs_over_expected: float
    top_slice_rank_under_null: float


@dataclass
class EdgeCluster:
    cluster_id: str
    label: str
    scope: dict[str, Any]
    axis: str
    n_positions: int
    n_events: int
    pnl: float
    edge_pp: float
    member_position_keys: list[tuple] = field(default_factory=list)
    is_loss_center: bool = False
    overlap: list[dict] = field(default_factory=list)


@dataclass
class ThesisProfile:
    wallet: str
    run_id: str
    headline: str
    edge_clusters: list[EdgeCluster] = field(default_factory=list)
    loss_centers: list[EdgeCluster] = field(default_factory=list)
    reward_share_pct: float = 0.0
    shuffle_baseline: ShuffleBaseline | None = None
    data_gaps: list[str] = field(default_factory=list)
```

Also create the package marker:
```python
# sniper/analysis/winning_wallet_thesis/__init__.py
"""Winning-wallet thesis extractor (spec 2026-05-31 rev 2)."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_types.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/__init__.py sniper/analysis/winning_wallet_thesis/types.py tests/winning_wallet_thesis/test_types.py
git commit -m "feat(thesis): data objects (spec rev2 §8)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: `ledger.py` — normalize activity into typed rows

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/ledger.py`
- Test: `tests/winning_wallet_thesis/test_ledger.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_ledger.py
from sniper.analysis.winning_wallet_thesis import ledger as L


def _act(typ, **kw):
    base = {"type": typ, "conditionId": "0xc", "timestamp": 1, "eventSlug": "e",
            "title": "t", "size": 0, "usdcSize": 0}
    base.update(kw)
    return base


def test_normalize_maps_trade_redeem_split():
    acts = [
        _act("TRADE", side="BUY", outcomeIndex=0, price=0.4, size=10, usdcSize=4),
        _act("TRADE", side="SELL", outcomeIndex=0, price=0.6, size=10, usdcSize=6),
        _act("REDEEM"),
        _act("SPLIT", size=100, usdcSize=100),
        _act("REWARD", usdcSize=3),
    ]
    rows = L.normalize(acts)
    kinds = [r.kind for r in rows]
    assert kinds == ["buy", "sell", "redeem", "split", "reward"]
    assert rows[0].price == 0.4 and rows[0].usdc == 4 and rows[0].outcome_index == 0


def test_normalize_keeps_conditionless_reward_rows():
    rows = L.normalize([{"type": "REWARD", "timestamp": 9, "usdcSize": 3.5}])
    assert len(rows) == 1
    assert rows[0].kind == "reward"
    assert rows[0].condition_id == ""
    assert rows[0].usdc == 3.5


def test_normalize_skips_trade_rows_without_condition():
    assert L.normalize([{"type": "TRADE", "side": "BUY"}]) == []


def test_normalize_preserves_feed_sequence_for_tie_breaking():
    rows = L.normalize([
        _act("TRADE", side="BUY", outcomeIndex=0, price=0.4, size=10, usdcSize=4, timestamp=1),
        _act("TRADE", side="SELL", outcomeIndex=0, price=0.6, size=10, usdcSize=6, timestamp=1),
    ])
    assert [r.seq for r in rows] == [0, 1]
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_ledger.py -v`
Expected: FAIL (`ModuleNotFoundError` / `normalize` undefined).

- [ ] **Step 3: Implement `ledger.py`**

```python
# sniper/analysis/winning_wallet_thesis/ledger.py
"""wallet_event_ledger — ingestion + normalization only. Never decides edge."""
from __future__ import annotations

import json
import os
import sys
from typing import Iterable, Mapping

from sniper.markets import polymarket_api as P
from sniper.analysis.winning_wallet_thesis.types import LedgerRow, WalletEventLedger

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "runs", "wallet_anatomy")

_SIDE_KIND = {"BUY": "buy", "SELL": "sell"}
_TYPE_KIND = {"REDEEM": "redeem", "SPLIT": "split", "MERGE": "merge",
              "REWARD": "reward", "YIELD": "reward", "MAKER_REBATE": "reward"}


def normalize(activity: Iterable[Mapping]) -> list[LedgerRow]:
    rows: list[LedgerRow] = []
    for seq, a in enumerate(activity):
        cond = a.get("conditionId") or a.get("condition_id")
        typ = a.get("type")
        if typ == "TRADE":
            if not cond:
                continue
            kind = _SIDE_KIND.get((a.get("side") or "").upper())
            if kind is None:
                continue
        else:
            kind = _TYPE_KIND.get(typ)
            if kind is None:
                continue
            if kind != "reward" and not cond:
                continue
        oidx = a.get("outcomeIndex")
        try:
            oidx = int(oidx) if oidx is not None else None
        except (TypeError, ValueError):
            oidx = None
        rows.append(LedgerRow(
            kind=kind, wallet=str(a.get("proxyWallet") or ""), condition_id=str(cond or ""),
            ts=int(a.get("timestamp") or 0), seq=seq, outcome_index=oidx,
            outcome=a.get("outcome") or "", price=float(a.get("price") or 0.0),
            size=float(a.get("size") or 0.0), usdc=float(a.get("usdcSize") or 0.0),
            tx=a.get("transactionHash") or "", event_slug=a.get("eventSlug") or "",
            title=a.get("title") or ""))
    return rows


def load_activity(wallet: str, *, refresh: bool = False, max_pages: int = 400) -> list[dict]:
    """Full backward-walked activity history, cached to disk (reused from prototype)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f"{wallet.lower()}_activity.json")
    if os.path.exists(path) and not refresh:
        with open(path) as f:
            return json.load(f)
    s = P.build_session()

    def emit(event_name, payload=None):
        print(f"  ...{event_name}", file=sys.stderr)

    acts = P.fetch_user_activity(s, wallet, activity_type=None, page_limit=500,
                                 max_pages=max_pages, walk_full_history=True,
                                 progress_emitter=emit)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(acts, f)
    os.replace(tmp, path)
    return acts


def build_ledger(wallet: str, *, refresh: bool = False) -> WalletEventLedger:
    acts = load_activity(wallet, refresh=refresh)
    return WalletEventLedger(wallet=wallet, rows=normalize(acts), raw_activity=acts)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_ledger.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/ledger.py tests/winning_wallet_thesis/test_ledger.py
git commit -m "feat(thesis): wallet_event_ledger normalize + fetch

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: `enrichment.py` — resolution + tags + series-derived template

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/enrichment.py`
- Test: `tests/winning_wallet_thesis/test_enrichment.py`

Port `parent_category`, `specific_tag`, `_PARENT_RULES`, `_GENERIC_TAGS`, `resolve_conditions`, `load_event_tags` verbatim from `scripts/wallet_anatomy.py` (they carry the transient-failure-not-cached fixes and 48 passing tests). Add **series/slug-derived** `event_template` (spec §4.4 — NOT title regex).

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_enrichment.py
from sniper.analysis.winning_wallet_thesis import enrichment as E


def test_parent_category_is_tag_driven():
    assert E.parent_category(["Sports", "UFC", "Games"]) == "sports"
    assert E.parent_category(["Geopolitics", "Iran", "Politics"]) == "geopolitics"
    assert E.parent_category([]) == "untagged"


def test_event_template_from_slug_not_title():
    # template = slug with trailing matchup/date tokens stripped -> recurring family
    assert E.event_template("ufc-fight-night-oliveira-vs-gamrot") == "ufc-fight-night"
    assert E.event_template("cfb-duke-uconn-2025-11-08") == "cfb"
    assert E.event_template("") == ""
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_enrichment.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `enrichment.py`**

Copy `parent_category`, `specific_tag`, `_PARENT_RULES`, `_GENERIC_TAGS`, `resolve_conditions`, `_parse_resolution`, `_json_list`, `load_event_tags` from `scripts/wallet_anatomy.py` unchanged (update `CACHE_DIR` import to reuse `ledger.CACHE_DIR`). Then add:

```python
import re

# slug template = leading non-matchup tokens. Polymarket slugs are
# "<series>-<matchup>-<date>"; strip trailing date and the vs-matchup so recurring
# market families collapse (series/slug-derived, never title regex).
_DATE_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")
_TRAIL_RE = re.compile(r"-\d{4}$")


def event_template(event_slug: str) -> str:
    s = (event_slug or "").strip().lower()
    if not s:
        return ""
    s = _DATE_RE.sub("", s)
    s = _TRAIL_RE.sub("", s)
    parts = s.split("-")
    # keep the recognizable series prefix: tokens up to the first 'vs' or the
    # first matchup token (heuristic: stop at the 2nd content token for known
    # multi-word series like 'ufc-fight-night').
    if "vs" in parts:
        parts = parts[:parts.index("vs")]
    KNOWN_MULTI = {"ufc-fight-night", "noche-ufc", "blast-open"}
    for k in KNOWN_MULTI:
        if s.startswith(k):
            return k
    return parts[0] if parts else ""


def enrich_ledger(ledger, *, refresh: bool = False):
    from sniper.analysis.winning_wallet_thesis.types import EnrichedLedger
    conds = {r.condition_id for r in ledger.rows}
    slugs = {r.event_slug for r in ledger.rows if r.event_slug}
    resolutions = resolve_conditions(conds, wallet=ledger.wallet, refresh=refresh)
    tags = load_event_tags(slugs, wallet=ledger.wallet, refresh=refresh)
    templates = {s: event_template(s) for s in slugs}
    return EnrichedLedger(ledger=ledger, resolutions=resolutions, tags=tags, templates=templates)
```

> NOTE for implementer: `KNOWN_MULTI` is a *structural* slug-prefix set (series families that contain hyphens), NOT entity names — acceptable under spec §7 because it keys on Polymarket slug structure, not market subject. Keep it tiny; prefer extending from Gamma `series` metadata in Phase 2.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_enrichment.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/enrichment.py tests/winning_wallet_thesis/test_enrichment.py
git commit -m "feat(thesis): market_semantics_enricher (tags + series-derived templates)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: `position_builder.py` — episodes + event baskets

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/position_builder.py`
- Test: `tests/winning_wallet_thesis/test_position_builder.py`

Reuse the proven episode logic from `scripts/wallet_anatomy.py:reconstruct_positions` (chronological sort, flat-then-rebuy episode increment, REDEEM marks all episodes of a condition, SPLIT/MERGE → sentinel `outcome_index = -1` leg) but emit `PositionEpisode` objects and a per-`event_slug` `EventBasket`, and apply enrichment (resolution + tags + category + template) inline.

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_position_builder.py
from sniper.analysis.winning_wallet_thesis import ledger as L, enrichment as E
from sniper.analysis.winning_wallet_thesis import position_builder as PB
from sniper.analysis.winning_wallet_thesis.types import WalletEventLedger, EnrichedLedger


def _trade(ts, side, size, price, cond="0xc", slug="ufc-x"):
    return {"type": "TRADE", "conditionId": cond, "outcomeIndex": 0, "outcome": "Y",
            "title": "t", "eventSlug": slug, "timestamp": ts, "side": side,
            "size": size, "usdcSize": size * price}


def _enriched(acts):
    led = WalletEventLedger(wallet="0xw", rows=L.normalize(acts), raw_activity=acts)
    return EnrichedLedger(ledger=led,
                          resolutions={"0xc": {"resolved": True, "winner": 0, "push": False}},
                          tags={"ufc-x": ["Sports", "UFC"]},
                          templates={"ufc-x": "ufc"})


def test_flat_then_rebuy_splits_into_two_episodes():
    book = PB.build_position_book(_enriched([
        _trade(1, "BUY", 10, 0.2), _trade(2, "SELL", 10, 0.5), _trade(3, "BUY", 5, 0.7),
    ]))
    dir_eps = [e for e in book.episodes if e.is_directional]
    assert len(dir_eps) == 2
    assert sorted(e.buy_shares for e in dir_eps) == [5.0, 10.0]
    assert all(e.category == "sports" and e.template == "ufc" for e in dir_eps)


def test_redeem_marks_all_episodes_and_resolution_applies():
    book = PB.build_position_book(_enriched([
        _trade(1, "BUY", 10, 0.2), {"type": "REDEEM", "conditionId": "0xc", "timestamp": 2},
    ]))
    e = [e for e in book.episodes if e.is_directional][0]
    assert e.redeemed and e.resolved and e.won == 1


def test_split_appears_in_event_basket_not_directional():
    book = PB.build_position_book(_enriched([
        {"type": "SPLIT", "conditionId": "0xc", "eventSlug": "ufc-x", "timestamp": 1,
         "size": 100, "usdcSize": 100},
    ]))
    assert all(not e.is_directional for e in book.episodes) or not book.episodes
    assert book.baskets["ufc-x"].split_shares == 100


def test_conditionless_rewards_are_kept_in_reward_income():
    acts = [{"type": "REWARD", "timestamp": 1, "usdcSize": 7.5}]
    led = WalletEventLedger(wallet="0xw", rows=L.normalize(acts), raw_activity=acts)
    enriched = EnrichedLedger(ledger=led)
    book = PB.build_position_book(enriched)
    assert book.reward_income["wallet"] == 7.5
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_builder.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `position_builder.py`**

```python
# sniper/analysis/winning_wallet_thesis/position_builder.py
"""event_level_position_builder — folds the enriched ledger into episodes + baskets."""
from __future__ import annotations

from collections import defaultdict

from sniper.analysis.winning_wallet_thesis.enrichment import parent_category, specific_tag
from sniper.analysis.winning_wallet_thesis.types import (
    EnrichedLedger, EventBasket, PositionBook, PositionEpisode,
)


def build_position_book(enriched: EnrichedLedger) -> PositionBook:
    led = enriched.ledger
    episodes: dict[tuple, PositionEpisode] = {}
    baskets: dict[str, EventBasket] = {}
    rewards: dict[str, float] = defaultdict(float)
    current_episode: dict[tuple[str, int], int] = defaultdict(int)

    def basket(slug, cond) -> EventBasket:
        b = baskets.get(slug)
        if b is None:
            b = EventBasket(event_slug=slug)
            baskets[slug] = b
        b.condition_ids.add(cond)
        return b

    def get(cond, oidx, outcome, title, slug, episode) -> PositionEpisode:
        key = (cond, oidx, episode)
        e = episodes.get(key)
        if e is None:
            e = PositionEpisode(wallet=led.wallet, condition_id=cond, event_slug=slug,
                                outcome_index=oidx, episode_index=episode,
                                outcome=outcome or "", title=title or "")
            episodes[key] = e
        return e

    for r in sorted(led.rows, key=lambda r: (r.ts, r.seq)):
        slug, cond = r.event_slug, r.condition_id
        if r.kind in ("buy", "sell"):
            oidx = r.outcome_index if r.outcome_index is not None else 0
            base = (cond, oidx)
            ep = current_episode[base]
            if r.kind == "buy":
                prior = episodes.get((cond, oidx, ep))
                if prior is not None and prior.buy_shares > 0 and prior.sell_shares > 0:
                    net = prior.buy_shares + prior.split_shares - prior.sell_shares - prior.merge_shares
                    if net <= 1e-9:
                        ep += 1
                        current_episode[base] = ep
            e = get(cond, oidx, r.outcome, r.title, slug, ep)
            e.last_ts = max(e.last_ts, r.ts)
            b = basket(slug, cond)
            if r.kind == "buy":
                e.buy_shares += r.size; e.buy_cost += r.usdc
                e.first_buy_ts = r.ts if e.first_buy_ts is None else min(e.first_buy_ts, r.ts)
                b.net_cost += r.usdc
            else:
                e.sell_shares += r.size; e.sell_proceeds += r.usdc
                b.net_proceeds += r.usdc
        elif r.kind == "redeem":
            for (c, oi, ep), e in episodes.items():
                if c == cond:
                    e.redeemed = True; e.last_ts = max(e.last_ts, r.ts)
        elif r.kind in ("split", "merge"):
            b = basket(slug, cond)
            if r.kind == "split":
                b.split_shares += r.size
            else:
                b.merge_shares += r.size
        elif r.kind == "reward":
            rewards[slug or cond or "wallet"] += r.usdc

    # apply resolution + semantics
    for e in episodes.values():
        e.tags = list(enriched.tags.get(e.event_slug, []))
        e.category = parent_category(e.tags)
        e.subcat = specific_tag(e.tags)
        e.template = enriched.templates.get(e.event_slug, "")
        res = enriched.resolutions.get(e.condition_id) or {}
        if res.get("resolved") and e.outcome_index >= 0:
            e.resolved = True
            e.is_push = bool(res.get("push"))
            winner = res.get("winner")
            if res.get("push"):
                e.won = None
            elif winner is not None:
                e.won = 1 if winner == e.outcome_index else 0

    for b in baskets.values():
        b.final_pnl = b.net_proceeds - b.net_cost + b.redeem_payout

    return PositionBook(wallet=led.wallet, episodes=list(episodes.values()),
                        baskets=baskets, reward_income=dict(rewards))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_position_builder.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/position_builder.py tests/winning_wallet_thesis/test_position_builder.py
git commit -m "feat(thesis): event_level_position_builder (episodes + baskets)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 5: `slicer.py` — edge surfaces + shuffle baseline

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/slicer.py`
- Test: `tests/winning_wallet_thesis/test_slicer.py`

Port `_wilson`, `_price_regime`, `_entry_hour`, `reward_attribution`, `reward_share_pct` from the prototype. Build `slice_edges` returning `{axis: {slice_key: EdgeSlice}}` over `book.resolved_directional()`, with `n_events` counted as distinct `event_slug`s per slice. Add the NEW `shuffle_baseline` (spec §4.4).

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_slicer.py
import random

from sniper.analysis.winning_wallet_thesis import slicer as S
from sniper.analysis.winning_wallet_thesis.types import PositionBook, PositionEpisode


def _ep(cat, won, entry, slug, pnl_cost=100.0):
    e = PositionEpisode(wallet="0xw", condition_id=slug, event_slug=slug, outcome_index=0,
                        episode_index=0, buy_shares=pnl_cost / entry, buy_cost=pnl_cost,
                        resolved=True, won=won)
    e.category = cat; e.subcat = cat.upper(); e.tags = [cat.upper()]
    return e


def _book(eps):
    return PositionBook(wallet="0xw", episodes=eps)


def test_slice_edges_computes_edge_and_n_events():
    eps = [_ep("sports", 1, 0.4, f"ufc-{i}") for i in range(10)]
    tables = S.slice_edges(_book(eps))
    s = tables["category"]["sports"]
    assert s.n_positions == 10 and s.n_events == 10
    assert s.win_rate == 1.0 and round(s.mean_entry, 2) == 0.40


def test_shuffle_baseline_reports_expected_vs_observed():
    # mostly-random book -> observed green slices should be near expected under null
    rng = random.Random(0)
    eps = [_ep("sports", rng.randint(0, 1), 0.5, f"e{i}") for i in range(60)]
    base = S.shuffle_baseline(_book(eps), n_shuffles=20, seed=1)
    assert base.n_shuffles == 20
    assert base.expected_green >= 0
    assert base.obs_over_expected >= 0
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_slicer.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `slicer.py`**

```python
# sniper/analysis/winning_wallet_thesis/slicer.py
"""edge_position_slicer — (r-p)/PnL surfaces + within-wallet shuffle baseline."""
from __future__ import annotations

import math
import random
from collections import defaultdict
from datetime import datetime, timezone

from sniper.analysis.winning_wallet_thesis.types import EdgeSlice, PositionBook, ShuffleBaseline

NOISE_TYPES = {"REWARD", "YIELD", "MAKER_REBATE"}


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _price_regime(entry: float) -> str:
    if entry < 0.10: return "deep-longshot"
    if entry < 0.30: return "longshot"
    if entry < 0.50: return "underdog"
    if entry < 0.70: return "lean-fav"
    if entry < 0.90: return "favorite"
    return "near-certain"


def _entry_hour(ep) -> str:
    if not ep.first_buy_ts:
        return "??"
    return f"{datetime.fromtimestamp(ep.first_buy_ts, tz=timezone.utc).hour:02d}h"


def reward_attribution(activity) -> dict:
    income, counts = defaultdict(float), defaultdict(int)
    for a in activity:
        t = a.get("type")
        if t in NOISE_TYPES:
            income[t] += float(a.get("usdcSize") or 0.0)
            counts[t] += 1
    return {"income": dict(income), "counts": dict(counts), "total": sum(income.values())}


def reward_share_pct(*, trading_pnl: float, reward_total: float) -> float:
    denom = abs(trading_pnl) + reward_total
    return reward_total / denom * 100.0 if denom else 0.0


def _add(table: dict, axis: str, key: str, ep) -> None:
    s = table.setdefault(key, EdgeSlice(slice_key=key, axis=axis))
    s.n_positions += 1
    s.wins += int(ep.won == 1)
    s.entry_sum += ep.entry_vwap
    s.pnl += ep.realized_pnl or 0.0
    s.cost += ep.buy_cost
    s.members.append(ep)


def _finalize_events(table: dict) -> None:
    for s in table.values():
        s.n_events = len({e.event_slug for e in s.members})


def slice_edges(book: PositionBook) -> dict[str, dict[str, EdgeSlice]]:
    rd = book.resolved_directional()
    tables: dict[str, dict[str, EdgeSlice]] = {a: {} for a in
        ("category", "tag", "subcat", "template", "price", "mechanism", "hour", "tag_price")}
    for ep in rd:
        _add(tables["category"], "category", ep.category, ep)
        _add(tables["subcat"], "subcat", ep.subcat or "untagged", ep)
        _add(tables["template"], "template", ep.template or "untagged", ep)
        _add(tables["price"], "price", _price_regime(ep.entry_vwap), ep)
        _add(tables["mechanism"], "mechanism", ep.exit_mechanism, ep)
        _add(tables["hour"], "hour", _entry_hour(ep), ep)
        _add(tables["tag_price"], "tag_price",
             f"{ep.subcat} | {_price_regime(ep.entry_vwap)}", ep)
        for t in (ep.tags or ["untagged"]):
            _add(tables["tag"], "tag", t, ep)
    for tbl in tables.values():
        _finalize_events(tbl)
    return tables


def _count_green(tables: dict, *, min_pos: int, min_edge_pp: float) -> int:
    return sum(1 for tbl in tables.values() for s in tbl.values()
               if s.n_positions >= min_pos and s.edge_pp > min_edge_pp and s.pnl > 0)


def shuffle_baseline(book: PositionBook, *, n_shuffles: int = 50, seed: int = 0,
                     min_pos: int = 15, min_edge_pp: float = 2.0) -> ShuffleBaseline:
    """Shuffle win/loss within coarse price bands, rerun the slice family, and compare
    observed green-slice count vs the null distribution (spec §4.4 — descriptive, not a gate)."""
    rd = book.resolved_directional()
    observed = _count_green(slice_edges(book), min_pos=min_pos, min_edge_pp=min_edge_pp)
    bands: dict[str, list] = defaultdict(list)
    for ep in rd:
        bands[_price_regime(ep.entry_vwap)].append(ep)
    rng = random.Random(seed)
    null_counts: list[int] = []
    orig = {id(ep): ep.won for ep in rd}
    for _ in range(n_shuffles):
        for band_eps in bands.values():
            outcomes = [orig[id(ep)] for ep in band_eps]
            rng.shuffle(outcomes)
            for ep, w in zip(band_eps, outcomes):
                ep.won = w
        null_counts.append(_count_green(slice_edges(book), min_pos=min_pos, min_edge_pp=min_edge_pp))
    for ep in rd:                       # restore truth
        ep.won = orig[id(ep)]
    expected = sum(null_counts) / len(null_counts) if null_counts else 0.0
    over = observed / expected if expected else float(observed)
    rank = sum(1 for c in null_counts if c >= observed) / len(null_counts) if null_counts else 1.0
    return ShuffleBaseline(n_shuffles=n_shuffles, expected_green=round(expected, 2),
                           observed_green=observed, obs_over_expected=round(over, 2),
                           top_slice_rank_under_null=round(rank, 3))
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_slicer.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/slicer.py tests/winning_wallet_thesis/test_slicer.py
git commit -m "feat(thesis): edge_position_slicer + within-wallet shuffle baseline (spec §4.4)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 6: `clusters.py` — guarded edge-cluster builder (the missing center)

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/clusters.py`
- Test: `tests/winning_wallet_thesis/test_clusters.py`

NEW per spec §4.5: candidate clusters from green/loss slices + depth-≤2 conjunctions, with guards `n_events ≥ 5`, `n_positions ≥ 15`, conjunction depth ≤ 2, and overlap reporting. Deterministic `cluster_id`.

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_clusters.py
from sniper.analysis.winning_wallet_thesis import clusters as C
from sniper.analysis.winning_wallet_thesis.types import EdgeSlice


def _slice(axis, key, n, n_events, pnl, wins, entry):
    s = EdgeSlice(slice_key=key, axis=axis, n_positions=n, n_events=n_events,
                  wins=wins, entry_sum=entry * n, pnl=pnl, cost=abs(pnl) + 100)
    return s


def test_green_slice_above_guards_becomes_cluster():
    tables = {"tag": {"UFC": _slice("tag", "UFC", 30, 20, 50000, 20, 0.5)}}
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert len(clusters) == 1
    assert clusters[0].label == "UFC" and clusters[0].pnl == 50000
    assert not clusters[0].is_loss_center


def test_slice_below_guards_is_dropped():
    tables = {"tag": {"Tiny": _slice("tag", "Tiny", 8, 3, 9000, 6, 0.5)}}  # n<15, events<5
    assert C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2) == []


def test_loss_center_is_emitted():
    tables = {"tag": {"Iran": _slice("tag", "Iran", 40, 20, -300000, 20, 0.7)}}
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    assert clusters and clusters[0].is_loss_center


def test_cluster_id_is_deterministic():
    tables = {"tag": {"UFC": _slice("tag", "UFC", 30, 20, 50000, 20, 0.5)}}
    a = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)[0]
    b = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)[0]
    assert a.cluster_id == b.cluster_id


def test_deeper_than_max_depth_axis_is_dropped():
    tables = {"tag_price_hour": {"UFC | favorite | 03h": _slice("tag_price_hour", "UFC | favorite | 03h", 30, 20, 50000, 20, 0.5)}}
    assert C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_clusters.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `clusters.py`**

```python
# sniper/analysis/winning_wallet_thesis/clusters.py
"""edge_cluster_builder — turns slices into guarded candidate edge groups (spec §4.5)."""
from __future__ import annotations

import hashlib

from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, EdgeSlice


def _cid(axis: str, key: str) -> str:
    return hashlib.sha256(f"{axis}|{key}".encode()).hexdigest()[:16]


def _cluster_from_slice(s: EdgeSlice) -> EdgeCluster:
    return EdgeCluster(
        cluster_id=_cid(s.axis, s.slice_key), label=s.slice_key,
        scope={"axis": s.axis, "key": s.slice_key}, axis=s.axis,
        n_positions=s.n_positions, n_events=s.n_events, pnl=round(s.pnl, 2),
        edge_pp=round(s.edge_pp, 2),
        member_position_keys=[(e.condition_id, e.outcome_index, e.episode_index)
                              for e in s.members],
        is_loss_center=s.pnl < 0)


def _axis_depth(axis: str) -> int:
    return axis.count("_") + 1


def build_clusters(slice_tables: dict[str, dict[str, EdgeSlice]], *,
                   min_events: int = 5, min_positions: int = 15,
                   max_depth: int = 2) -> list[EdgeCluster]:
    """V1: single-axis clusters that clear the guards, ranked by |pnl|. Conjunction
    depth is capped at `max_depth` (the slicer already emits one depth-2 axis,
    `tag_price`); deeper conjunctions are out of scope for V1."""
    candidates: list[EdgeCluster] = []
    for axis, tbl in slice_tables.items():
        if _axis_depth(axis) > max_depth:
            continue
        for s in tbl.values():
            if s.slice_key in ("untagged", "??"):
                continue
            if s.n_positions < min_positions or s.n_events < min_events:
                continue
            if abs(s.pnl) < 1.0:
                continue
            candidates.append(_cluster_from_slice(s))
    candidates.sort(key=lambda c: abs(c.pnl), reverse=True)
    # overlap: share of member positions in common with each earlier cluster
    for i, c in enumerate(candidates):
        ci = set(c.member_position_keys)
        for prev in candidates[:i]:
            inter = ci & set(prev.member_position_keys)
            if inter:
                c.overlap.append({"cluster_id": prev.cluster_id,
                                  "share": round(len(inter) / max(len(ci), 1), 3)})
    return candidates
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_clusters.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/clusters.py tests/winning_wallet_thesis/test_clusters.py
git commit -m "feat(thesis): edge_cluster_builder with guards + overlap (spec §4.5)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 7: `assembler.py` — rank clusters into a thesis profile

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/assembler.py`
- Test: `tests/winning_wallet_thesis/test_assembler.py`

NEW. Split clusters into edge clusters vs loss centers, attach reward share + shuffle baseline, deterministic `run_id`, headline. (Per-cluster method detectors + runnability are Phase 2 — leave a documented seam.)

- [ ] **Step 1: Write the failing test**

```python
# tests/winning_wallet_thesis/test_assembler.py
from sniper.analysis.winning_wallet_thesis import assembler as A
from sniper.analysis.winning_wallet_thesis.types import (
    EdgeCluster, PositionBook, ShuffleBaseline,
)


def _cluster(label, pnl, loss=False):
    return EdgeCluster(cluster_id=label, label=label, scope={}, axis="tag",
                       n_positions=30, n_events=20, pnl=pnl, edge_pp=5.0,
                       is_loss_center=loss)


def test_profile_splits_edge_and_loss_and_is_deterministic():
    clusters = [_cluster("UFC", 50000), _cluster("Iran", -300000, loss=True)]
    book = PositionBook(wallet="0xw")
    base = ShuffleBaseline(20, 1.0, 1, 1.0, 0.5)
    p1 = A.assemble_profile("0xw", book, {}, base, clusters, reward_share=7.3)
    p2 = A.assemble_profile("0xw", book, {}, base, clusters, reward_share=7.3)
    assert [c.label for c in p1.edge_clusters] == ["UFC"]
    assert [c.label for c in p1.loss_centers] == ["Iran"]
    assert p1.reward_share_pct == 7.3
    assert p1.run_id == p2.run_id          # deterministic
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_assembler.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement `assembler.py`**

```python
# sniper/analysis/winning_wallet_thesis/assembler.py
"""thesis_profile_assembler — ranks clusters into a wallet thesis profile.

Phase 1 stops at clusters. Per-cluster `method_detector_runner` findings and the
`thesis_validation_spine` are Phase 2/3 — `theses` is intentionally left for them."""
from __future__ import annotations

import hashlib

from sniper.analysis.winning_wallet_thesis.types import (
    EdgeCluster, PositionBook, ShuffleBaseline, ThesisProfile,
)


def _run_id(wallet: str, clusters: list[EdgeCluster]) -> str:
    fp = wallet + "|" + ";".join(sorted(f"{c.cluster_id}:{c.pnl}" for c in clusters))
    return hashlib.sha256(fp.encode()).hexdigest()[:16]


def assemble_profile(wallet: str, book: PositionBook,
                     slice_tables: dict, baseline: ShuffleBaseline | None,
                     clusters: list[EdgeCluster], *, reward_share: float = 0.0) -> ThesisProfile:
    edge = sorted([c for c in clusters if not c.is_loss_center],
                  key=lambda c: c.pnl, reverse=True)
    loss = sorted([c for c in clusters if c.is_loss_center], key=lambda c: c.pnl)
    top = edge[0].label if edge else "none"
    worst = loss[0].label if loss else "none"
    headline = f"{len(edge)} edge cluster(s); top={top}; loss-center={worst}"
    gaps = []
    if reward_share >= 25:
        gaps.append("reward_share high — verify prediction vs liquidity-farming")
    return ThesisProfile(
        wallet=wallet, run_id=_run_id(wallet, clusters), headline=headline,
        edge_clusters=edge, loss_centers=loss, reward_share_pct=round(reward_share, 1),
        shuffle_baseline=baseline, data_gaps=gaps)
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_assembler.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/assembler.py tests/winning_wallet_thesis/test_assembler.py
git commit -m "feat(thesis): thesis_profile_assembler (Phase 1 cluster ranking)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 8: `cli.py` + runner + end-to-end wiring

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/cli.py`
- Create: `scripts/run_winning_wallet_thesis.py`
- Modify: `sniper/analysis/winning_wallet_thesis/__init__.py` (export `run_extractor`)
- Test: `tests/winning_wallet_thesis/test_pipeline_e2e.py`

- [ ] **Step 1: Write the failing test (offline e2e with injected enriched ledger)**

```python
# tests/winning_wallet_thesis/test_pipeline_e2e.py
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A
from sniper.analysis.winning_wallet_thesis import ledger as L
from sniper.analysis.winning_wallet_thesis.types import WalletEventLedger, EnrichedLedger


def _trade(ts, won_price, slug, i):
    # winners priced 0.4 -> +edge; one event per condition
    return {"type": "TRADE", "conditionId": f"{slug}-{i}", "outcomeIndex": 0,
            "outcome": "Y", "title": "t", "eventSlug": f"{slug}-{i}", "timestamp": ts,
            "side": "BUY", "size": 100, "usdcSize": 40}


def test_full_pipeline_produces_edge_cluster():
    acts = [_trade(i, 0.4, "ufc", i) for i in range(20)]
    led = WalletEventLedger(wallet="0xw", rows=L.normalize(acts), raw_activity=acts)
    res = {f"ufc-{i}": {"resolved": True, "winner": 0, "push": False} for i in range(20)}
    tags = {f"ufc-{i}": ["Sports", "UFC"] for i in range(20)}
    tmpl = {f"ufc-{i}": "ufc" for i in range(20)}
    enriched = EnrichedLedger(ledger=led, resolutions=res, tags=tags, templates=tmpl)

    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    profile = A.assemble_profile("0xw", book, tables, None, clusters)

    assert any(c.label == "UFC" for c in profile.edge_clusters)
    assert profile.run_id
```

- [ ] **Step 2: Run to verify fail**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_pipeline_e2e.py -v`
Expected: FAIL (until `cli`/wiring exist — though this test only needs the modules; it should pass once they're imported correctly). If it already passes after Task 7, proceed to wire the CLI in Step 3 and add the CLI smoke as Step 3a.

- [ ] **Step 3: Implement `cli.py` + runner**

```python
# sniper/analysis/winning_wallet_thesis/cli.py
"""CLI for the winning-wallet thesis extractor (operator-triggered, cached)."""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from sniper.analysis.winning_wallet_thesis import ledger as L, enrichment as E
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A


def run_extractor(wallet: str, *, refresh: bool = False) -> "A.ThesisProfile":
    enriched = E.enrich_ledger(L.build_ledger(wallet, refresh=refresh), refresh=refresh)
    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    baseline = S.shuffle_baseline(book, n_shuffles=50, seed=1)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    rd = book.resolved_directional()
    trading_pnl = sum(e.realized_pnl or 0.0 for e in rd)
    reward = S.reward_attribution(enriched.ledger.raw_activity)["total"]
    share = S.reward_share_pct(trading_pnl=trading_pnl, reward_total=reward)
    return A.assemble_profile(wallet, book, tables, baseline, clusters, reward_share=share)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wallet")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    profile = run_extractor(args.wallet, refresh=args.refresh)
    if args.json:
        print(json.dumps(asdict(profile), default=str, indent=2))
    else:
        print(f"WALLET {profile.wallet}  run_id={profile.run_id}")
        print(profile.headline)
        print(f"reward_share={profile.reward_share_pct}%  "
              f"shuffle obs/exp={profile.shuffle_baseline.obs_over_expected if profile.shuffle_baseline else 'n/a'}")
        print("EDGE CLUSTERS:")
        for c in profile.edge_clusters[:10]:
            print(f"  [{c.axis}] {c.label:<24} n={c.n_positions} ev={c.n_events} "
                  f"edge={c.edge_pp:+.1f}pp pnl=${c.pnl:,.0f}")
        print("LOSS CENTERS:")
        for c in profile.loss_centers[:5]:
            print(f"  [{c.axis}] {c.label:<24} pnl=${c.pnl:,.0f}")
```

```python
# scripts/run_winning_wallet_thesis.py
#!/usr/bin/env python3
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sniper.analysis.winning_wallet_thesis.cli import main

if __name__ == "__main__":
    main()
```

Update `__init__.py`:
```python
# sniper/analysis/winning_wallet_thesis/__init__.py
"""Winning-wallet thesis extractor (spec 2026-05-31 rev 2)."""
from sniper.analysis.winning_wallet_thesis.cli import run_extractor  # noqa: F401
```

- [ ] **Step 4: Run the module suite to verify pass**

Run: `python3 -m pytest tests/winning_wallet_thesis/ -q`
Expected: PASS (all module tests).

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/cli.py sniper/analysis/winning_wallet_thesis/__init__.py scripts/run_winning_wallet_thesis.py tests/winning_wallet_thesis/test_pipeline_e2e.py
git commit -m "feat(thesis): CLI + run_extractor end-to-end wiring (Phase 1)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 9: SwissMiss parity fixture (acceptance)

**Files:**
- Test: `tests/winning_wallet_thesis/test_swissmiss_parity.py`

Validates the modular pipeline reproduces the prototype's headline facts on the cached SwissMiss activity, and that the spec §10 fixture assertions hold (multiple clusters, sports positive, Iran loss center). Skips cleanly if the cache isn't present (CI has no live API).

- [ ] **Step 1: Write the test**

```python
# tests/winning_wallet_thesis/test_swissmiss_parity.py
import os

import pytest

from sniper.analysis.winning_wallet_thesis import ledger as L, enrichment as E
from sniper.analysis.winning_wallet_thesis import position_builder as PB, slicer as S
from sniper.analysis.winning_wallet_thesis import clusters as C, assembler as A

SWISS = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"


def _cache_present() -> bool:
    base = os.path.join(L.CACHE_DIR, SWISS.lower())
    return all(os.path.exists(f"{base}_{k}.json") for k in ("activity", "resolutions", "tags"))


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache not present (no live API in CI)")
def test_swissmiss_multi_cluster_and_loss_center():
    enriched = E.enrich_ledger(L.build_ledger(SWISS), refresh=False)
    book = PB.build_position_book(enriched)
    tables = S.slice_edges(book)
    clusters = C.build_clusters(tables, min_events=5, min_positions=15, max_depth=2)
    profile = A.assemble_profile(SWISS, book, tables, None, clusters)

    labels = {c.label for c in profile.edge_clusters}
    # sports edge clusters present (durable platform tags)
    assert {"UFC", "MLB", "CFB"} & labels
    # Iran / Middle East loss center present
    loss = {c.label for c in profile.loss_centers}
    assert {"Iran", "Middle East", "Geopolitics"} & loss
    # full-book realized PnL in the historically-verified band (+$300k..+$420k)
    rd = book.resolved_directional()
    pnl = sum(e.realized_pnl or 0.0 for e in rd)
    assert 300_000 < pnl < 420_000
```

- [ ] **Step 2: Run it**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_swissmiss_parity.py -v`
Expected: PASS if the SwissMiss cache exists locally; SKIP in CI.

- [ ] **Step 3: Run the full module suite + the prototype suite (no regressions)**

Run: `python3 -m pytest tests/winning_wallet_thesis/ tests/test_wallet_anatomy.py -q`
Expected: PASS.

- [ ] **Step 4: Validate schema refs (CI parity)**

Run: `python3 scripts/validate_schema_refs.py`
Expected: PASS (no DB columns are named in this phase, but run it per project policy).

- [ ] **Step 5: Commit**

```bash
git add tests/winning_wallet_thesis/test_swissmiss_parity.py
git commit -m "test(thesis): SwissMiss parity fixture (multi-cluster + loss center, spec §10)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Follow-on (separate plans — required before validated claims)

Phase 1 ends with a ranked, unvalidated thesis profile. It is useful for inspection and for selecting validation work, but it must not be presented as a proven edge. Any "validated", "promoted", or board-level claim requires Phase 3.

- **Phase 2 — detector pack + runnability:** `method_detector_runner` + the V1 detectors (specialist, recurring-template, market-weighted favorite/longshot, sizing[rare], exit[descriptive], capital-velocity, reward, stale-liquidity-proxy *iff* CLOB price-history pipeline exists), plus the `runnability` classifier as `MethodFinding` annotations. Per spec §5/§9.
- **Phase 3 — validation spine:** wire ranked theses into `thesis_validation_spine` (forward-power gate → single-wallet paper via existing enrollment → cross-wallet replication fallback → Null Calibration Gate), reusing `sniper.analysis.thesis_cards.*`. Then delete `scripts/wallet_anatomy.py` once parity holds. Per spec §4.8.
- **Push 2 (deferred by spec):** full event-basket/negRisk accounting + true order-book stale-liquidity/CLV. Per spec §9 "Leave as explicit gap or proxy".

## Self-Review

- **Spec coverage:** §3 ledger/enrich/event-grouping → Tasks 2–4. §4.4 slicer + shuffle baseline → Task 5. §4.5 cluster guards (n_events≥5, n_positions≥15, depth≤2, overlap) → Task 6. §4.7 assembler → Task 7. §8 data objects → Task 1. §9 V1 foundation + intake (operator-curated wallet via CLI arg) + branch hygiene → Tasks 0, 8. §10 fixture tests (failed-fetch-unknown carried by reused enrichment; episode/redeem/split tests; tag-driven categories; SwissMiss multi-cluster) → Tasks 3–6, 9. **Deferred by design (flagged):** §4.6 detector runner, §4.8 validation spine, §5 catalog, full event-basket → Phases 2/3. The plan now explicitly says Phase 1 is unvalidated until Phase 3.
- **Placeholder scan:** none — every step has real code/commands. The one heuristic (`KNOWN_MULTI` in Task 3) is explicitly scoped as structural slug-prefix, not entity taxonomy, with a Phase-2 upgrade path.
- **Type consistency:** `PositionEpisode`/`EdgeSlice`/`EdgeCluster`/`ThesisProfile` fields are defined in Task 1 and used unchanged in Tasks 4–9; `build_clusters(...)` signature and `EdgeSlice.edge_pp`/`.n_events` are consistent across slicer/clusters/tests.
