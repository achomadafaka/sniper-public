# Fomo Account Analyzer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compute a wide per-trader raw-statistics table from each fomo account's own swap ledger, so the operator can rank/screen curated and candidate traders independent of our own copies.

**Architecture:** New pure package `sniper/analysis/fomo_account/` (classify → reconstruct → metrics) fed by a thin network adapter `sniper/feeds/fomo_account.py`; results upserted to a new `memecoin_trader_analytics` table; surfaced via CLI, one read API endpoint, and a sortable UI table. The reconstruction + metrics core is network-free and fully unit-tested with synthetic swaps.

**Tech Stack:** Python 3.13, stdlib only (`statistics`, `json`, `datetime`, `collections`, `urllib` via existing `fomo_get`), SQLite (WAL), Flask (existing blueprint), vanilla JS (existing `memecoin` controller).

## Global Constraints

- **No score, no verdict, no copy advice.** Output is raw descriptive statistics only. (spec §2)
- **Reconstructed PnL is realized-only and windowed.** Every profitability number persists alongside its coverage fields (`closed_coverage`, `hit_full_history`, `pct_off_platform`, `n_open_bags`). (spec §11)
- **Core is network-free.** All `urllib`/`fomo_get` calls live in `sniper/feeds/fomo_account.py`; `classify`/`reconstruct`/`metrics` take plain dicts. (spec §4)
- **Cash-leg set** (chain-agnostic, verified live): `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC), `So11111111111111111111111111111111111111112` (WSOL), `Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB` (USDT). USD always from `humanUsdAmount*`. (spec §3)
- **Rate limit:** adapter paging is caller-throttled ≥1.0s/call (fomo WAF). Never run inside the copy runner. (spec §12)
- **DB access:** all reads/writes for the new table go through `db_memecoin.py` (no raw SQL in routes/CLI/UI). (CLAUDE.md ADR / rule 5)
- After any schema change: `python3 scripts/brain_index.py generate` + commit; `python3 scripts/validate_schema_refs.py` green. (CLAUDE.md)
- Do not use `datetime.utcnow()` — use `datetime.now(timezone.utc)`. Guard all divisions. (CLAUDE.md)

---

## Review Deltas (2026-07-06) — APPLY THESE; they override the task snippets below

Two adversarial reviews + deeper live probing revised the design. Authoritative detail is in the
spec's **§13**. Concrete overrides to the tasks below:

- **Task 2 (reconstruct):** fix the partial-sell leftover bug. After the FIFO match loop, count the
  leftover: `if remaining > 1e-9: unmatched += 1` **even if some quantity matched** (currently the
  `matched` flag suppresses it). Add a test: buy 500 then sell 1000 same mint → 1 round-trip (500) +
  `unmatched_sells == 1`. Also expose `open_bag_cost = Σ(qty*price)` of leftover lots and
  `total_buy_notional = Σ buy usd` in the return dict (metrics needs them).
- **Task 4 (adapter): NO PAGINATION.** fomo returns only the newest ≤100 swaps; every page/cursor
  param is a no-op (spec §13a). Replace `iter_swaps(...)` with
  `fetch_recent_swaps(auth, uid, fetch=None) -> list[dict]`: a single
  `GET /v2/users/{uid}/swaps?limit=100`, **dedup by `id`**, return the list. Drop `max_pages`.
  Settings: drop `MEMECOIN_ANALYZER_MAX_PAGES`; keep `MEMECOIN_ANALYZER_SLEEP_S`; add
  `MEMECOIN_ANALYZER_SWAP_LIMIT = 100`.
- **Task 5 (orchestrator):** `analyze_account(auth, uid, *, now_iso, large_buy_usd=None, fetch=None)`
  — no `max_pages`. Fetch profile + `fetch_recent_swaps`; `swap_count = profile.get("swapCount")`;
  pass `swap_count` to `compute_metrics`; `hit_full_history = swaps_analyzed >= (swap_count or 0)`.
- **Task 3 (metrics):** signature becomes
  `compute_metrics(profile, swaps, recon, *, now_iso, large_buy_usd, swap_count)`. Add
  `coverage_ratio`, `total_buy_notional`, `open_bag_cost_usd`, `dollar_weighted_return_pct`,
  `unmatched_sell_ratio`, `undefeated`, `top3_win_share`, `top3_loss_share`. Rename
  `max_drawdown_usd`→`realized_max_drawdown_usd`, `sharpe_like`→`pnl_pct_dispersion_ratio` (null when
  `n_closed < 5`). Remove `expectancy_pct`, `top3_pnl_share`, `pages_fetched`, `hit_full_history` arg
  (now derived from `swap_count`). Update tests accordingly (power-law test asserts `top3_win_share`
  high, not `top3_pnl_share`).
- **Task 6 (persistence):** update `TRADER_ANALYTICS_COLUMNS` + `CREATE TABLE` to match the revised
  metric set above (add the 8 new columns, rename 2, drop 3).
- **Task 7 (CLI):** drop `--max-pages`. Add `db_memecoin.get_wallet_by_handle(conn, handle)` and use
  it in `resolve_targets` (no raw SQL). Printed table leads with coverage columns
  (`coverage_ratio`, `unmatched_sell_ratio`, `n_closed`, `hit_full_history`) next to
  `realized_pnl_usd`.
- **Task 8 (route): the module is a blueprint FACTORY.** Add the route **inside**
  `create_api_memecoin_blueprint(ctx)` (before `return bp`), use the closed-over `conn = ctx["conn"]`,
  `@bp.get("/api/memecoin/traders/analytics")`, return `jsonify({"rows": m.list_trader_analytics(conn)})`.
  **Do not open/close a connection.** Test: `bp = api.create_api_memecoin_blueprint({"conn": c})`
  with an in-memory `c` (row_factory=Row), then register and GET.
- **Task 9 (UI):** use `this.fetchJson(url, {suppressErrorToast:true})`; hook `this._loadAnalytics()`
  into the memecoin branch of the controller's `load()` dispatch (app.js ~2262), not the 4s poll.

---

### Task 1: Swap classifier

**Files:**
- Create: `sniper/analysis/fomo_account/__init__.py` (empty)
- Create: `sniper/analysis/fomo_account/classify.py`
- Test: `tests/test_fomo_account_classify.py`

**Interfaces:**
- Produces: `CASH_TOKENS: set[str]`; `classify_swap(swap: dict) -> tuple[str, str|None, float, float]` returning `(kind, mint, usd, token_qty)`, `kind ∈ {"buy","sell","cash_route","token_route"}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_classify.py
from sniper.analysis.fomo_account.classify import classify_swap, CASH_TOKENS

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
MEME = "4U4U8oXwDyVXGeTffMXds4NAgBgLFwq3wNvTCRTSpump"
MEME2 = "GaZb3DE2U3Jcjx7ddAVwobsBKnCaDoJWLbzTvJYhpump"


def _swap(inm, outm, usd_in, usd_out, in_amt, out_amt):
    return {"inTokenAddress": inm, "outTokenAddress": outm,
            "humanUsdAmountIn": usd_in, "humanUsdAmountOut": usd_out,
            "inHumanAmount": in_amt, "outHumanAmount": out_amt}


def test_buy_is_cash_in_meme_out():
    k, mint, usd, qty = classify_swap(_swap(USDC, MEME, "500", "500", "500", "29663"))
    assert k == "buy" and mint == MEME and usd == 500.0 and qty == 29663.0


def test_sell_is_meme_in_cash_out():
    k, mint, usd, qty = classify_swap(_swap(MEME, USDC, "600", "600", "29663", "600"))
    assert k == "sell" and mint == MEME and usd == 600.0 and qty == 29663.0


def test_cash_route_both_legs_cash():
    k, mint, usd, qty = classify_swap(_swap(USDC, WSOL, "100", "100", "100", "0.5"))
    assert k == "cash_route" and mint is None


def test_token_route_neither_leg_cash():
    k, mint, usd, qty = classify_swap(_swap(MEME, MEME2, "100", "98", "1000", "2000"))
    assert k == "token_route" and mint is None


def test_malformed_numbers_default_zero():
    k, mint, usd, qty = classify_swap(_swap(USDC, MEME, None, None, None, None))
    assert k == "buy" and usd == 0.0 and qty == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_classify.py -q`
Expected: FAIL — `ModuleNotFoundError: sniper.analysis.fomo_account.classify`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/fomo_account/classify.py
"""Classify a fomo swap into buy / sell / route using the canonical cash-leg set.

fomo canonicalizes the USD leg to the Solana USDC mint on every chain (verified
2026-07-06 across Solana/Base/BSC/Ethereum), so direction detection is
chain-agnostic. USD amounts always come from humanUsdAmount* fields.
"""
from __future__ import annotations

CASH_TOKENS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC — canonical cash leg (all chains)
    "So11111111111111111111111111111111111111112",   # WSOL
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def classify_swap(swap):
    """Return (kind, mint, usd, token_qty).

    buy:  cash in, memecoin out  -> mint=outToken, usd=humanUsdAmountIn, qty=outHumanAmount
    sell: memecoin in, cash out  -> mint=inToken,  usd=humanUsdAmountOut, qty=inHumanAmount
    cash_route / token_route:    -> mint=None, usd=max(in,out), qty=0.0
    """
    inm = swap.get("inTokenAddress")
    outm = swap.get("outTokenAddress")
    in_cash = inm in CASH_TOKENS
    out_cash = outm in CASH_TOKENS
    usd_in = _f(swap.get("humanUsdAmountIn"))
    usd_out = _f(swap.get("humanUsdAmountOut"))
    if in_cash and out_cash:
        return ("cash_route", None, max(usd_in, usd_out), 0.0)
    if in_cash and not out_cash:
        return ("buy", outm, usd_in, _f(swap.get("outHumanAmount")))
    if out_cash and not in_cash:
        return ("sell", inm, usd_out, _f(swap.get("inHumanAmount")))
    return ("token_route", None, max(usd_in, usd_out), 0.0)
```

Also create empty `sniper/analysis/fomo_account/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_classify.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/fomo_account/__init__.py sniper/analysis/fomo_account/classify.py tests/test_fomo_account_classify.py
git commit -m "feat(fomo-analyzer): swap buy/sell/route classifier (cash-leg canonical)"
```

---

### Task 2: Round-trip reconstruction

**Files:**
- Create: `sniper/analysis/fomo_account/reconstruct.py`
- Test: `tests/test_fomo_account_reconstruct.py`

**Interfaces:**
- Consumes: `classify_swap` from Task 1.
- Produces: `reconstruct(swaps: list[dict]) -> dict` with keys `roundtrips, open_bags, unmatched_sells, routes, token_routes, off_platform, buys, sells, total, window`. Each roundtrip: `{mint, qty, entry_price, exit_price, entry_ts, exit_ts, realized_usd, pnl_pct, hold_s}`. Each buy in `buys`: `{mint, usd, ts, price}`. `window`: `{oldest, newest, days}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_reconstruct.py
from sniper.analysis.fomo_account.reconstruct import reconstruct

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME = "4U4U8oXwDyVXGeTffMXds4NAgBgLFwq3wNvTCRTSpump"


def _buy(mint, usd, tok, ts, off="False"):
    return {"inTokenAddress": USDC, "outTokenAddress": mint,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(usd), "outHumanAmount": str(tok),
            "createdAt": ts, "isOffPlatform": off, "networkId": 1399811149, "provider": "DFLOW"}


def _sell(mint, usd, tok, ts, off="False"):
    return {"inTokenAddress": mint, "outTokenAddress": USDC,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(tok), "outHumanAmount": str(usd),
            "createdAt": ts, "isOffPlatform": off, "networkId": 1399811149, "provider": "DFLOW"}


def test_clean_roundtrip():
    # buy 1000 tok for $100 (price .1), sell 1000 tok for $150 (price .15) -> +$50, +50%
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(MEME, 150, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    rt = r["roundtrips"][0]
    assert abs(rt["realized_usd"] - 50.0) < 1e-6
    assert abs(rt["pnl_pct"] - 50.0) < 1e-6
    assert abs(rt["hold_s"] - 3600.0) < 1e-6
    assert r["open_bags"] == []


def test_partial_sell_leaves_open_bag():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _sell(MEME, 75, 500, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    assert len(r["open_bags"]) == 1
    assert abs(r["open_bags"][0]["qty"] - 500.0) < 1e-6


def test_average_down_fifo():
    # buy 1000@.10, buy 1000@.05, sell 1000@.20 -> matches first lot (.10) -> +$100
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z"),
             _buy(MEME, 50, 1000, "2026-07-01T10:30:00Z"),
             _sell(MEME, 200, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert len(r["roundtrips"]) == 1
    assert abs(r["roundtrips"][0]["realized_usd"] - 100.0) < 1e-6
    assert len(r["open_bags"]) == 1  # the .05 lot still open


def test_no_sell_trader_all_open_bags():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T10:00:00Z")]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert len(r["open_bags"]) == 1
    assert r["sells"] == 0


def test_unmatched_sell_counted_not_crashed():
    swaps = [_sell(MEME, 150, 1000, "2026-07-01T11:00:00Z")]
    r = reconstruct(swaps)
    assert r["roundtrips"] == []
    assert r["unmatched_sells"] == 1


def test_routes_and_offplatform_counted():
    swaps = [
        {"inTokenAddress": USDC, "outTokenAddress": "So11111111111111111111111111111111111111112",
         "humanUsdAmountIn": "10", "humanUsdAmountOut": "10", "inHumanAmount": "10",
         "outHumanAmount": "0.05", "createdAt": "2026-07-01T09:00:00Z", "isOffPlatform": "False"},
        _buy(MEME, 100, 1000, "2026-07-01T10:00:00Z", off="True"),
    ]
    r = reconstruct(swaps)
    assert r["routes"] == 1
    assert r["off_platform"] == 1
    assert r["total"] == 2


def test_window_days():
    swaps = [_buy(MEME, 100, 1000, "2026-07-01T00:00:00Z"),
             _sell(MEME, 150, 1000, "2026-07-03T00:00:00Z")]
    r = reconstruct(swaps)
    assert abs(r["window"]["days"] - 2.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_reconstruct.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/fomo_account/reconstruct.py
"""Reconstruct per-mint round-trips (avg-cost FIFO) from a fomo swap list.

Pure / network-free. Realized-only: leftover buy lots become open_bags
(censored). Sells with no open lot are unmatched (no cost basis).
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime

from .classify import classify_swap


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def reconstruct(swaps):
    ordered = sorted(swaps, key=lambda s: s.get("createdAt") or "")
    lots = defaultdict(deque)          # mint -> deque of [qty, price, ts]
    roundtrips = []
    buys = []
    unmatched = 0
    routes = 0
    token_routes = 0
    off_platform = 0
    sells = 0

    for s in ordered:
        if str(s.get("isOffPlatform")) == "True":
            off_platform += 1
        kind, mint, usd, qty = classify_swap(s)
        ts = s.get("createdAt")
        if kind == "buy":
            price = (usd / qty) if qty else 0.0
            if qty > 0 and price > 0:
                lots[mint].append([qty, price, ts])
                buys.append({"mint": mint, "usd": usd, "ts": ts, "price": price})
        elif kind == "sell":
            sells += 1
            price = (usd / qty) if qty else 0.0
            remaining = qty
            matched = False
            while remaining > 1e-9 and lots[mint]:
                lot = lots[mint][0]
                take = min(remaining, lot[0])
                entry_price = lot[1]
                et = _parse_ts(lot[2])
                xt = _parse_ts(ts)
                roundtrips.append({
                    "mint": mint, "qty": take, "entry_price": entry_price,
                    "exit_price": price, "entry_ts": lot[2], "exit_ts": ts,
                    "realized_usd": (price - entry_price) * take,
                    "pnl_pct": ((price / entry_price - 1.0) * 100.0) if entry_price else 0.0,
                    "hold_s": (xt - et).total_seconds() if (et and xt) else None,
                })
                lot[0] -= take
                remaining -= take
                matched = True
                if lot[0] <= 1e-9:
                    lots[mint].popleft()
            if not matched and remaining > 1e-9:
                unmatched += 1
        elif kind == "cash_route":
            routes += 1
        else:  # token_route
            routes += 1
            token_routes += 1

    open_bags = []
    for mint, dq in lots.items():
        for qty, price, ts in dq:
            if qty > 1e-9:
                open_bags.append({"mint": mint, "qty": qty, "entry_price": price,
                                  "entry_ts": ts, "cost_usd": qty * price})

    parsed = [t for t in (_parse_ts(s.get("createdAt")) for s in ordered) if t]
    window = {
        "oldest": ordered[0].get("createdAt") if ordered else None,
        "newest": ordered[-1].get("createdAt") if ordered else None,
        "days": ((parsed[-1] - parsed[0]).total_seconds() / 86400.0) if len(parsed) >= 2 else 0.0,
    }
    return {"roundtrips": roundtrips, "open_bags": open_bags,
            "unmatched_sells": unmatched, "routes": routes, "token_routes": token_routes,
            "off_platform": off_platform, "buys": buys, "sells": sells,
            "total": len(ordered), "window": window}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_reconstruct.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/fomo_account/reconstruct.py tests/test_fomo_account_reconstruct.py
git commit -m "feat(fomo-analyzer): FIFO round-trip reconstruction from swap ledger"
```

---

### Task 3: Metrics computation

**Files:**
- Create: `sniper/analysis/fomo_account/metrics.py`
- Test: `tests/test_fomo_account_metrics.py`

**Interfaces:**
- Consumes: `reconstruct` output (Task 2).
- Produces: `compute_metrics(profile: dict, swaps: list[dict], recon: dict, *, now_iso: str, large_buy_usd: float, hit_full_history: bool, pages_fetched: int) -> dict` — flat dict of the spec §6 scalars plus `chain_mix`, `provider_mix`, `metrics_json` (all JSON strings). Also stat helpers `_pct, _mean, _median, _std, _safe_div`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_metrics.py
from sniper.analysis.fomo_account.reconstruct import reconstruct
from sniper.analysis.fomo_account.metrics import compute_metrics

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _buy(mint, usd, tok, ts):
    return {"inTokenAddress": USDC, "outTokenAddress": mint,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(usd), "outHumanAmount": str(tok), "createdAt": ts,
            "isOffPlatform": "False", "networkId": 1399811149, "provider": "DFLOW",
            "platformFeeHumanAmount": "0.1", "referralFeeHumanAmount": "0"}


def _sell(mint, usd, tok, ts):
    return {"inTokenAddress": mint, "outTokenAddress": USDC,
            "humanUsdAmountIn": str(usd), "humanUsdAmountOut": str(usd),
            "inHumanAmount": str(tok), "outHumanAmount": str(usd), "createdAt": ts,
            "isOffPlatform": "False", "networkId": 1399811149, "provider": "DFLOW",
            "platformFeeHumanAmount": "0.1", "referralFeeHumanAmount": "0"}


PROFILE = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
           "followers": 100, "following": 50, "numTrades": 3, "totalVolume": 1000.0,
           "swapCount": 6, "twitter": "alice", "isRestricted": False, "private": False}


def _metrics(swaps):
    recon = reconstruct(swaps)
    return compute_metrics(PROFILE, swaps, recon, now_iso="2026-07-06T00:00:00Z",
                           large_buy_usd=100.0, hit_full_history=True, pages_fetched=1)


def test_power_law_winrate_low_pnl_high():
    # 3 small losers (-50% each) + 1 huge winner (+10000%)
    M = "MINT111"
    swaps = []
    t = 1
    for _ in range(3):
        swaps += [_buy(M, 100, 1000, f"2026-07-0{t}T10:00:00Z"),
                  _sell(M, 50, 1000, f"2026-07-0{t}T11:00:00Z")]
        t += 1
    swaps += [_buy(M, 100, 1000, "2026-07-04T10:00:00Z"),
              _sell(M, 10100, 1000, "2026-07-04T11:00:00Z")]  # +10000%
    m = _metrics(swaps)
    assert m["n_closed"] == 4
    assert m["win_rate"] == 0.25            # 1 of 4
    assert m["realized_pnl_usd"] > 9000     # dominated by the winner
    assert m["top3_pnl_share"] is not None and m["top3_pnl_share"] > 0.9  # concentration


def test_no_sell_trader_coverage_zero_no_crash():
    m = _metrics([_buy("MINT222", 100, 1000, "2026-07-05T10:00:00Z")])
    assert m["n_closed"] == 0
    assert m["n_open_bags"] == 1
    assert m["closed_coverage"] == 0.0
    assert m["win_rate"] is None
    assert m["realized_pnl_usd"] == 0.0


def test_recency_counts():
    M = "MINT333"
    swaps = [_buy(M, 200, 1000, "2026-07-05T23:00:00Z"),   # within 24h of now
             _sell(M, 250, 1000, "2026-07-05T23:30:00Z")]
    m = _metrics(swaps)
    assert m["swaps_24h"] == 2
    assert m["swaps_7d"] == 2
    assert m["large_buys_7d"] == 1          # the $200 buy >= 100 threshold
    assert m["last_swap_age_h"] is not None


def test_coverage_and_profile_fields():
    M = "MINT444"
    m = _metrics([_buy(M, 100, 1000, "2026-07-01T10:00:00Z"),
                  _sell(M, 150, 1000, "2026-07-01T11:00:00Z")])
    assert m["hit_full_history"] == 1
    assert m["pct_off_platform"] == 0.0
    assert m["follower_ratio"] == 2.0
    assert m["has_twitter"] == 1
    assert m["profile_swap_count"] == 6
    assert "chain_mix" in m and "1399811149" in m["chain_mix"]
    assert "metrics_json" in m
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_metrics.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/fomo_account/metrics.py
"""Compute the per-trader raw statistics envelope from reconstruct() output.

Pure / network-free. No score, no verdict — descriptive statistics only.
Every profitability figure is emitted next to its coverage fields.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _median(vals):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def _std(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return (sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)) ** 0.5


def _pct(vals, q):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _safe_div(a, b):
    return (a / b) if b else None


def _parse(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def compute_metrics(profile, swaps, recon, *, now_iso, large_buy_usd,
                    hit_full_history, pages_fetched):
    rts = recon["roundtrips"]
    bags = recon["open_bags"]
    buys = recon["buys"]
    total = recon["total"]

    pnls = [r["realized_usd"] for r in rts]
    pcts = [r["pnl_pct"] for r in rts]
    holds = [r["hold_s"] for r in rts if r["hold_s"] is not None]
    n_closed = len(rts)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_pcts = [r["pnl_pct"] for r in rts if r["realized_usd"] > 0]
    loss_pcts = [r["pnl_pct"] for r in rts if r["realized_usd"] < 0]

    # max drawdown on cumulative realized (chronological by exit)
    cum = peak = mdd = 0.0
    for r in sorted(rts, key=lambda x: x["exit_ts"] or ""):
        cum += r["realized_usd"]
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)

    abs_sorted = sorted((abs(p) for p in pnls), reverse=True)
    top3_share = _safe_div(sum(abs_sorted[:3]), sum(abs_sorted)) if abs_sorted else None

    sizes = [b["usd"] for b in buys]

    now = _parse(now_iso) or datetime.now(timezone.utc)

    def _age_h(ts):
        t = _parse(ts)
        return ((now - t).total_seconds() / 3600.0) if t else None

    swap_ts = [t for t in (_parse(s.get("createdAt")) for s in swaps) if t]
    swaps_24h = sum(1 for t in swap_ts if (now - t).total_seconds() <= 86400)
    swaps_7d = sum(1 for t in swap_ts if (now - t).total_seconds() <= 7 * 86400)
    vol_7d = sum(_f(s.get("humanUsdAmountIn")) for s in swaps
                 if _parse(s.get("createdAt"))
                 and (now - _parse(s.get("createdAt"))).total_seconds() <= 7 * 86400)
    large_7d = sum(1 for b in buys if b["usd"] >= large_buy_usd and _parse(b["ts"])
                   and (now - _parse(b["ts"])).total_seconds() <= 7 * 86400)
    active_days = len({t.date() for t in swap_ts})
    ss = sorted(swap_ts)
    gaps = [(b - a).total_seconds() / 3600.0 for a, b in zip(ss, ss[1:])]
    longest_gap_h = max(gaps) if gaps else None

    # avg-down rate per mint (chronological buys)
    by_mint = defaultdict(list)
    for b in sorted(buys, key=lambda x: x["ts"] or ""):
        by_mint[b["mint"]].append(b["price"])
    downs = nb = 0
    for prices in by_mint.values():
        for i in range(1, len(prices)):
            nb += 1
            if prices[i] < prices[i - 1]:
                downs += 1
    distinct_mints = len({b["mint"] for b in buys})

    fee_sum = sum(_f(s.get("platformFeeHumanAmount")) + _f(s.get("referralFeeHumanAmount"))
                  for s in swaps)
    vol_sum = sum(_f(s.get("humanUsdAmountIn")) for s in swaps)

    chain_mix, prov_mix = {}, {}
    for s in swaps:
        c = str(s.get("networkId"))
        p = str(s.get("provider"))
        chain_mix[c] = chain_mix.get(c, 0) + 1
        prov_mix[p] = prov_mix.get(p, 0) + 1

    created = _parse(profile.get("createdAt"))
    acct_age = ((now - created).total_seconds() / 86400.0) if created else None
    followers = _int(profile.get("followers"))
    following = _int(profile.get("following"))

    m = {
        "source_user_id": profile.get("id"),
        "handle": profile.get("userHandle") or profile.get("displayName"),
        "computed_at": now_iso,
        "data_as_of": recon["window"]["newest"],
        # coverage
        "swaps_analyzed": total,
        "pages_fetched": pages_fetched,
        "hit_full_history": 1 if hit_full_history else 0,
        "window_days": round(recon["window"]["days"], 3),
        "oldest_swap": recon["window"]["oldest"],
        "newest_swap": recon["window"]["newest"],
        "pct_off_platform": _safe_div(recon["off_platform"], total),
        "pct_route_approx": _safe_div(recon["routes"], total),
        "n_open_bags": len(bags),
        "n_unmatched_sells": recon["unmatched_sells"],
        "closed_coverage": (_safe_div(n_closed, n_closed + len(bags))
                            if (n_closed + len(bags)) else None),
        # profitability
        "n_closed": n_closed,
        "realized_pnl_usd": round(sum(pnls), 2),
        "mean_pnl_pct": _mean(pcts),
        "median_pnl_pct": _median(pcts),
        "win_rate": _safe_div(len(wins), n_closed),
        "profit_factor": (_safe_div(sum(wins), abs(sum(losses))) if losses else None),
        "expectancy_pct": _mean(pcts),
        "pnl_pct_p10": _pct(pcts, 0.10), "pnl_pct_p25": _pct(pcts, 0.25),
        "pnl_pct_p50": _pct(pcts, 0.50), "pnl_pct_p75": _pct(pcts, 0.75),
        "pnl_pct_p90": _pct(pcts, 0.90),
        "best_pct": max(pcts) if pcts else None,
        "worst_pct": min(pcts) if pcts else None,
        "return_std": _std(pcts),
        "sharpe_like": (_safe_div(_mean(pcts), _std(pcts)) if _std(pcts) else None),
        "max_drawdown_usd": round(mdd, 2),
        "avg_win_pct": _mean(win_pcts),
        "avg_loss_pct": _mean(loss_pcts),
        "top3_pnl_share": top3_share,
        # behavior
        "median_hold_s": _median(holds),
        "mean_hold_s": _mean(holds),
        "hold_p10": _pct(holds, 0.10), "hold_p90": _pct(holds, 0.90),
        "pct_held_under_60s": (_safe_div(sum(1 for h in holds if h < 60), len(holds))
                               if holds else None),
        "pct_held_over_1d": (_safe_div(sum(1 for h in holds if h > 86400), len(holds))
                             if holds else None),
        "avg_buy_usd": _mean(sizes),
        "buy_usd_p10": _pct(sizes, 0.10), "buy_usd_p50": _pct(sizes, 0.50),
        "buy_usd_p90": _pct(sizes, 0.90),
        "max_buy_usd": max(sizes) if sizes else None,
        "size_cv": (_safe_div(_std(sizes), _mean(sizes)) if (sizes and _mean(sizes)) else None),
        "re_entry_rate": (_safe_div(total, distinct_mints) if distinct_mints else None),
        "avg_buys_per_token": (_safe_div(len(buys), distinct_mints) if distinct_mints else None),
        "avg_down_rate": _safe_div(downs, nb),
        "fee_drag_pct": _safe_div(fee_sum, vol_sum),
        # recency
        "last_swap_age_h": _age_h(recon["window"]["newest"]),
        "swaps_24h": swaps_24h, "swaps_7d": swaps_7d,
        "volume_7d": round(vol_7d, 2), "large_buys_7d": large_7d,
        "active_days": active_days, "longest_gap_h": longest_gap_h,
        # profile
        "num_trades": _int(profile.get("numTrades")),
        "total_volume": _f(profile.get("totalVolume")),
        "account_age_days": round(acct_age, 2) if acct_age is not None else None,
        "followers": followers, "following": following,
        "follower_ratio": _safe_div(followers, following),
        "has_twitter": 1 if profile.get("twitter") else 0,
        "is_restricted": 1 if profile.get("isRestricted") else 0,
        "is_private": 1 if profile.get("private") else 0,
        "profile_swap_count": _int(profile.get("swapCount")),
        # mixes + blob
        "chain_mix": json.dumps(chain_mix),
        "provider_mix": json.dumps(prov_mix),
    }
    m["metrics_json"] = json.dumps({"pnl_pct_all": pcts, "hold_s_all": holds,
                                    "buy_usd_all": sizes})
    return m
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_metrics.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/fomo_account/metrics.py tests/test_fomo_account_metrics.py
git commit -m "feat(fomo-analyzer): per-trader metrics envelope (profitability/behavior/recency/coverage)"
```

---

### Task 4: Network adapter + settings

**Files:**
- Create: `sniper/feeds/fomo_account.py`
- Modify: `sniper/settings/memecoin.py` (append three constants)
- Test: `tests/test_fomo_account_adapter.py`

**Interfaces:**
- Consumes: `fomo_get` from `sniper.feeds.fomo_feed`.
- Produces: `get_profile(auth, uid, fetch=None) -> dict`; `iter_swaps(auth, uid, max_pages, fetch=None) -> tuple[list[dict], bool]` returning `(swaps, hit_full_history)`. `fetch` is an injectable `(auth, path) -> (status, json, date)` defaulting to `fomo_get` (network isolation for tests).
- Settings: `MEMECOIN_ANALYZER_MAX_PAGES=20`, `MEMECOIN_ANALYZER_SLEEP_S=1.0`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_adapter.py
from sniper.feeds.fomo_account import get_profile, iter_swaps


def _ro(obj):
    return {"responseObject": obj}


def test_get_profile_unwraps_response_object():
    def fetch(auth, path):
        assert path == "/v2/users/u1"
        return 200, _ro({"id": "u1", "userHandle": "alice"}), "date"
    p = get_profile(None, "u1", fetch=fetch)
    assert p["userHandle"] == "alice"


def test_iter_swaps_paginates_until_no_next():
    pages = {
        1: _ro({"swaps": [{"id": "a"}, {"id": "b"}], "hasNextPage": True}),
        2: _ro({"swaps": [{"id": "c"}], "hasNextPage": False}),
    }
    calls = []

    def fetch(auth, path):
        page = int(path.split("page=")[1].split("&")[0])
        calls.append(page)
        return 200, pages[page], "date"

    swaps, full = iter_swaps(None, "u1", max_pages=10, fetch=fetch)
    assert [s["id"] for s in swaps] == ["a", "b", "c"]
    assert full is True                 # reached hasNextPage=False
    assert calls == [1, 2]


def test_iter_swaps_respects_max_pages_and_flags_partial():
    def fetch(auth, path):
        return 200, _ro({"swaps": [{"id": "x"}], "hasNextPage": True}), "date"
    swaps, full = iter_swaps(None, "u1", max_pages=2, fetch=fetch)
    assert len(swaps) == 2              # one per page, capped at 2
    assert full is False                # cap hit, more remained
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_adapter.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/feeds/fomo_account.py
"""Network adapter for per-account fomo data (profile + paginated swap ledger).

The ONLY network surface for the analyzer. Paging is caller-throttled; this
module does not sleep (keeps it testable) — the CLI inserts the WAF delay.
"""
from __future__ import annotations

from sniper.feeds.fomo_feed import fomo_get


def _ro(j):
    return j.get("responseObject", j) if isinstance(j, dict) else j


def get_profile(auth, uid, fetch=None):
    fetch = fetch or fomo_get
    _s, j, _d = fetch(auth, f"/v2/users/{uid}")
    return _ro(j) or {}


def iter_swaps(auth, uid, max_pages, fetch=None):
    """Return (swaps, hit_full_history). hit_full_history is True iff a page
    reported hasNextPage=False before the cap (i.e. we saw the whole ledger)."""
    fetch = fetch or fomo_get
    out = []
    hit_full = False
    for page in range(1, max_pages + 1):
        _s, j, _d = fetch(auth, f"/v2/users/{uid}/swaps?page={page}&limit=100")
        o = _ro(j)
        swaps = (o.get("swaps") if isinstance(o, dict) else None) or []
        out.extend(swaps)
        if not (isinstance(o, dict) and o.get("hasNextPage")) or not swaps:
            hit_full = True
            break
    return out, hit_full
```

Append to `sniper/settings/memecoin.py` (follow the existing `_get_*` env pattern in that file):

```python
# --- fomo account analyzer ---
MEMECOIN_ANALYZER_MAX_PAGES = int(os.getenv("MEMECOIN_ANALYZER_MAX_PAGES", "20"))
MEMECOIN_ANALYZER_SLEEP_S = float(os.getenv("MEMECOIN_ANALYZER_SLEEP_S", "1.0"))
```

(If `sniper/settings/memecoin.py` does not already `import os`, add it at the top.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_adapter.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/fomo_account.py sniper/settings/memecoin.py tests/test_fomo_account_adapter.py
git commit -m "feat(fomo-analyzer): profile + paginated swap adapter (+ analyzer settings)"
```

---

### Task 5: Orchestrator

**Files:**
- Create: `sniper/analysis/fomo_account/analyzer.py`
- Test: `tests/test_fomo_account_analyzer.py`

**Interfaces:**
- Consumes: `get_profile`, `iter_swaps` (Task 4); `reconstruct` (Task 2); `compute_metrics` (Task 3); settings (Task 4).
- Produces: `analyze_account(auth, uid, *, now_iso, max_pages=None, large_buy_usd=None, fetch=None) -> dict` — the full metrics row for one trader.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_analyzer.py
from sniper.analysis.fomo_account.analyzer import analyze_account

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
M = "MINT999"


def test_analyze_account_end_to_end_with_stubbed_fetch():
    profile = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
               "followers": 10, "following": 5, "swapCount": 2}
    swaps_page = {"responseObject": {"swaps": [
        {"inTokenAddress": USDC, "outTokenAddress": M, "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
        {"inTokenAddress": M, "outTokenAddress": USDC, "humanUsdAmountIn": "150",
         "humanUsdAmountOut": "150", "inHumanAmount": "1000", "outHumanAmount": "150",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
    ], "hasNextPage": False}}

    def fetch(auth, path):
        if path == "/v2/users/u1":
            return 200, {"responseObject": profile}, "date"
        return 200, swaps_page, "date"

    row = analyze_account(None, "u1", now_iso="2026-07-06T00:00:00Z",
                          max_pages=5, large_buy_usd=100.0, fetch=fetch)
    assert row["source_user_id"] == "u1"
    assert row["n_closed"] == 1
    assert abs(row["realized_pnl_usd"] - 50.0) < 1e-6
    assert row["hit_full_history"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_analyzer.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/analysis/fomo_account/analyzer.py
"""Orchestrate one trader's analysis: fetch profile + swaps -> reconstruct -> metrics."""
from __future__ import annotations

from sniper.feeds.fomo_account import get_profile, iter_swaps
from sniper.settings import memecoin as _cfg
from .reconstruct import reconstruct
from .metrics import compute_metrics


def analyze_account(auth, uid, *, now_iso, max_pages=None, large_buy_usd=None, fetch=None):
    max_pages = _cfg.MEMECOIN_ANALYZER_MAX_PAGES if max_pages is None else max_pages
    large_buy_usd = _cfg.MEMECOIN_COPY_SIZE_USD if large_buy_usd is None else large_buy_usd
    profile = get_profile(auth, uid, fetch=fetch)
    profile.setdefault("id", uid)
    swaps, hit_full = iter_swaps(auth, uid, max_pages, fetch=fetch)
    recon = reconstruct(swaps)
    return compute_metrics(profile, swaps, recon, now_iso=now_iso,
                           large_buy_usd=large_buy_usd, hit_full_history=hit_full,
                           pages_fetched=min(max_pages, (len(swaps) // 100) + 1))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_analyzer.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/fomo_account/analyzer.py tests/test_fomo_account_analyzer.py
git commit -m "feat(fomo-analyzer): orchestrator wiring fetch->reconstruct->metrics"
```

---

### Task 6: Persistence

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (extend `ensure_memecoin_tables`; add three functions)
- Test: `tests/test_fomo_account_persistence.py`

**Interfaces:**
- Consumes: metrics row (Task 3/5).
- Produces: table `memecoin_trader_analytics`; `upsert_trader_analytics(conn, row: dict) -> None`; `list_trader_analytics(conn) -> list[dict]`; `get_trader_analytics(conn, uid) -> dict|None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_persistence.py
import sqlite3
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _row(uid="u1", pnl=50.0):
    return {"source_user_id": uid, "handle": "alice", "computed_at": "2026-07-06T00:00:00Z",
            "n_closed": 1, "realized_pnl_usd": pnl, "win_rate": 1.0,
            "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}


def test_upsert_then_list():
    c = _conn()
    m.upsert_trader_analytics(c, _row())
    rows = m.list_trader_analytics(c)
    assert len(rows) == 1
    assert rows[0]["source_user_id"] == "u1"
    assert rows[0]["realized_pnl_usd"] == 50.0


def test_upsert_is_idempotent_latest_wins():
    c = _conn()
    m.upsert_trader_analytics(c, _row(pnl=50.0))
    m.upsert_trader_analytics(c, _row(pnl=999.0))   # same uid -> update
    rows = m.list_trader_analytics(c)
    assert len(rows) == 1
    assert rows[0]["realized_pnl_usd"] == 999.0


def test_get_single():
    c = _conn()
    m.upsert_trader_analytics(c, _row())
    assert m.get_trader_analytics(c, "u1")["handle"] == "alice"
    assert m.get_trader_analytics(c, "nope") is None


def test_partial_row_uses_column_defaults():
    c = _conn()
    m.upsert_trader_analytics(c, {"source_user_id": "u2"})   # only the key
    assert m.get_trader_analytics(c, "u2")["source_user_id"] == "u2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_persistence.py -q`
Expected: FAIL — `AttributeError: module has no attribute 'upsert_trader_analytics'`.

- [ ] **Step 3: Write minimal implementation**

In `sniper/persistence/db_memecoin.py`, inside `ensure_memecoin_tables` (before the final `conn.commit()`), add the table. Use a column list constant at module level so the upsert stays DRY:

```python
# module level, near top of db_memecoin.py
TRADER_ANALYTICS_COLUMNS = [
    "source_user_id", "handle", "computed_at", "data_as_of",
    "swaps_analyzed", "pages_fetched", "hit_full_history", "window_days",
    "oldest_swap", "newest_swap", "pct_off_platform", "pct_route_approx",
    "n_open_bags", "n_unmatched_sells", "closed_coverage",
    "n_closed", "realized_pnl_usd", "mean_pnl_pct", "median_pnl_pct", "win_rate",
    "profit_factor", "expectancy_pct", "pnl_pct_p10", "pnl_pct_p25", "pnl_pct_p50",
    "pnl_pct_p75", "pnl_pct_p90", "best_pct", "worst_pct", "return_std", "sharpe_like",
    "max_drawdown_usd", "avg_win_pct", "avg_loss_pct", "top3_pnl_share",
    "median_hold_s", "mean_hold_s", "hold_p10", "hold_p90", "pct_held_under_60s",
    "pct_held_over_1d", "avg_buy_usd", "buy_usd_p10", "buy_usd_p50", "buy_usd_p90",
    "max_buy_usd", "size_cv", "re_entry_rate", "avg_buys_per_token", "avg_down_rate",
    "fee_drag_pct", "last_swap_age_h", "swaps_24h", "swaps_7d", "volume_7d",
    "large_buys_7d", "active_days", "longest_gap_h", "num_trades", "total_volume",
    "account_age_days", "followers", "following", "follower_ratio", "has_twitter",
    "is_restricted", "is_private", "profile_swap_count", "chain_mix", "provider_mix",
    "metrics_json",
]
```

```python
# inside ensure_memecoin_tables, before conn.commit()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_trader_analytics (
            source_user_id TEXT PRIMARY KEY,
            handle TEXT, computed_at TEXT, data_as_of TEXT,
            swaps_analyzed INTEGER, pages_fetched INTEGER, hit_full_history INTEGER,
            window_days REAL, oldest_swap TEXT, newest_swap TEXT,
            pct_off_platform REAL, pct_route_approx REAL,
            n_open_bags INTEGER, n_unmatched_sells INTEGER, closed_coverage REAL,
            n_closed INTEGER, realized_pnl_usd REAL, mean_pnl_pct REAL, median_pnl_pct REAL,
            win_rate REAL, profit_factor REAL, expectancy_pct REAL,
            pnl_pct_p10 REAL, pnl_pct_p25 REAL, pnl_pct_p50 REAL, pnl_pct_p75 REAL, pnl_pct_p90 REAL,
            best_pct REAL, worst_pct REAL, return_std REAL, sharpe_like REAL,
            max_drawdown_usd REAL, avg_win_pct REAL, avg_loss_pct REAL, top3_pnl_share REAL,
            median_hold_s REAL, mean_hold_s REAL, hold_p10 REAL, hold_p90 REAL,
            pct_held_under_60s REAL, pct_held_over_1d REAL,
            avg_buy_usd REAL, buy_usd_p10 REAL, buy_usd_p50 REAL, buy_usd_p90 REAL,
            max_buy_usd REAL, size_cv REAL, re_entry_rate REAL, avg_buys_per_token REAL,
            avg_down_rate REAL, fee_drag_pct REAL,
            last_swap_age_h REAL, swaps_24h INTEGER, swaps_7d INTEGER, volume_7d REAL,
            large_buys_7d INTEGER, active_days INTEGER, longest_gap_h REAL,
            num_trades INTEGER, total_volume REAL, account_age_days REAL,
            followers INTEGER, following INTEGER, follower_ratio REAL,
            has_twitter INTEGER, is_restricted INTEGER, is_private INTEGER,
            profile_swap_count INTEGER,
            chain_mix TEXT, provider_mix TEXT, metrics_json TEXT
        )""")
```

Add the three access functions (anywhere after `ensure_memecoin_tables`):

```python
def upsert_trader_analytics(conn, row):
    """Insert-or-replace one trader's analytics row. Only known columns are
    written; missing keys fall to NULL. source_user_id is required."""
    cols = [c for c in TRADER_ANALYTICS_COLUMNS if c in row]
    placeholders = ",".join("?" for _ in cols)
    updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "source_user_id")
    sql = (f"INSERT INTO memecoin_trader_analytics ({','.join(cols)}) "
           f"VALUES ({placeholders}) "
           f"ON CONFLICT(source_user_id) DO UPDATE SET {updates}")
    conn.execute(sql, [row[c] for c in cols])
    conn.commit()


def list_trader_analytics(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_trader_analytics ORDER BY realized_pnl_usd DESC")]


def get_trader_analytics(conn, uid):
    r = conn.execute("SELECT * FROM memecoin_trader_analytics WHERE source_user_id=?",
                     (uid,)).fetchone()
    return dict(r) if r else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_persistence.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Regenerate brain index + commit**

```bash
python3 scripts/brain_index.py generate
git add sniper/persistence/db_memecoin.py tests/test_fomo_account_persistence.py index/schema_registry.json index/dependency_index.json
git commit -m "feat(fomo-analyzer): memecoin_trader_analytics table + upsert/list/get"
```

---

### Task 7: CLI runner

**Files:**
- Create: `scripts/analyze_fomo_accounts.py`
- Test: `tests/test_fomo_account_cli.py`

**Interfaces:**
- Consumes: `analyze_account` (Task 5); `db_memecoin` upsert/list + `list_wallets` + `curated_user_ids`; `get_connection`.
- Produces: `resolve_targets(conn, args) -> list[tuple[uid, handle]]`; `run(conn, auth, targets, *, now_iso, max_pages, sleep_s, fetch=None, analyze=None) -> list[dict]`; `main()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fomo_account_cli.py
import sqlite3
from sniper.persistence import db_memecoin as m
import scripts.analyze_fomo_accounts as cli


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u1", "alice", source="follow")
    m.add_wallet(c, "u2", "bob", source="manual")
    return c


def test_resolve_targets_all_returns_curated():
    c = _conn()
    targets = cli.resolve_targets(c, uids=None, handles=None, all_curated=True)
    assert {t[0] for t in targets} == {"u1", "u2"}


def test_run_analyzes_and_persists():
    c = _conn()

    def fake_analyze(auth, uid, **kw):
        return {"source_user_id": uid, "handle": "x", "realized_pnl_usd": 10.0,
                "chain_mix": "{}", "provider_mix": "{}", "metrics_json": "{}"}

    rows = cli.run(c, None, [("u1", "alice")], now_iso="2026-07-06T00:00:00Z",
                   max_pages=1, sleep_s=0.0, analyze=fake_analyze)
    assert len(rows) == 1
    assert m.get_trader_analytics(c, "u1")["realized_pnl_usd"] == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_cli.py -q`
Expected: FAIL — module not found.

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""analyze_fomo_accounts.py — compute per-trader raw analytics from fomo swap ledgers.

Usage:
  python3 scripts/analyze_fomo_accounts.py --all
  python3 scripts/analyze_fomo_accounts.py --uid <id>
  python3 scripts/analyze_fomo_accounts.py --handle connerb
  python3 scripts/analyze_fomo_accounts.py --ids u1,u2,u3 --sort win_rate
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_ENV = os.path.join(_ROOT, ".fomo.env")
if os.path.exists(_ENV):
    for line in open(_ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

from sniper.persistence.db_core import get_connection
from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_feed import FomoAuth, fomo_get
from sniper.analysis.fomo_account.analyzer import analyze_account
from sniper.settings import memecoin as cfg


def resolve_targets(conn, uids=None, handles=None, all_curated=False):
    if all_curated:
        return [(w["source_user_id"], w.get("handle")) for w in m.list_wallets(conn)]
    out = []
    for uid in (uids or []):
        out.append((uid, None))
    for h in (handles or []):
        row = conn.execute("SELECT source_user_id, handle FROM memecoin_wallets WHERE handle=?",
                           (h,)).fetchone()
        if row:
            out.append((row["source_user_id"], row["handle"]))
        else:
            out.append((h, h))   # allow raw handle passthrough; analyzer will 400 if invalid
    return out


def run(conn, auth, targets, *, now_iso, max_pages, sleep_s, fetch=None, analyze=None):
    analyze = analyze or analyze_account
    rows = []
    for i, (uid, _handle) in enumerate(targets):
        try:
            row = analyze(auth, uid, now_iso=now_iso, max_pages=max_pages, fetch=fetch)
            m.upsert_trader_analytics(conn, row)
            rows.append(row)
        except Exception as e:                       # noqa: BLE001 — one bad trader must not kill the batch
            print(f"  ! {uid}: {type(e).__name__}: {e}", file=sys.stderr)
        if sleep_s and i < len(targets) - 1:
            time.sleep(sleep_s)
    return rows


def _print_table(rows, sort_key):
    rows = sorted(rows, key=lambda r: (r.get(sort_key) is None, -(r.get(sort_key) or 0)))
    cols = ["handle", "n_closed", "realized_pnl_usd", "win_rate", "profit_factor",
            "top3_pnl_share", "median_hold_s", "large_buys_7d", "closed_coverage",
            "hit_full_history"]
    print("  ".join(c[:14].ljust(14) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c))[:14].ljust(14) for c in cols))


def _dump_csv(rows, path):
    if not rows:
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--uid")
    ap.add_argument("--handle")
    ap.add_argument("--ids")
    ap.add_argument("--sort", default="realized_pnl_usd")
    ap.add_argument("--max-pages", type=int, default=cfg.MEMECOIN_ANALYZER_MAX_PAGES)
    ap.add_argument("--sleep", type=float, default=cfg.MEMECOIN_ANALYZER_SLEEP_S)
    args = ap.parse_args()

    conn = get_connection()
    m.ensure_memecoin_tables(conn)
    uids = [args.uid] if args.uid else (args.ids.split(",") if args.ids else [])
    handles = [args.handle] if args.handle else []
    targets = resolve_targets(conn, uids=uids, handles=handles, all_curated=args.all)
    if not targets:
        print("no targets — pass --all / --uid / --handle / --ids", file=sys.stderr)
        return 2

    auth = FomoAuth()
    try:
        auth.refresh_token()
    except Exception as e:                            # noqa: BLE001
        print(f"auth refresh warning: {e}", file=sys.stderr)

    now_iso = datetime.now(timezone.utc).isoformat()
    print(f"analyzing {len(targets)} trader(s), max_pages={args.max_pages} ...")
    rows = run(conn, auth, targets, now_iso=now_iso, max_pages=args.max_pages,
               sleep_s=args.sleep, fetch=fomo_get)
    _print_table(rows, args.sort)
    os.makedirs(os.path.join(_ROOT, "reports"), exist_ok=True)
    out = os.path.join(_ROOT, "reports",
                       f"fomo_trader_analytics_{now_iso[:10]}.csv")
    _dump_csv(rows, out)
    print(f"\nwrote {len(rows)} rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_cli.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add scripts/analyze_fomo_accounts.py tests/test_fomo_account_cli.py
git commit -m "feat(fomo-analyzer): CLI runner (--all/--uid/--handle/--ids), CSV + table output"
```

---

### Task 8: API endpoint

**Files:**
- Modify: `sniper/routes/api_memecoin.py` (add one route)
- Test: `tests/test_fomo_account_route.py`

**Interfaces:**
- Consumes: `db_memecoin.list_trader_analytics`; the blueprint's existing connection helper.
- Produces: `GET /api/memecoin/traders/analytics` → `{"rows": [...]}`.

- [ ] **Step 1: Read the existing route module to match its connection + blueprint pattern**

Run: `grep -n "def \|blueprint\|get_connection\|@.*route\|def _conn\|ctx" sniper/routes/api_memecoin.py | head -40`
Use the SAME connection helper and blueprint object the other memecoin routes use (do not introduce a new pattern).

- [ ] **Step 2: Write the failing test**

```python
# tests/test_fomo_account_route.py
import json
import sqlite3
from sniper.persistence import db_memecoin as m


def test_traders_analytics_route(monkeypatch, tmp_path):
    # Build an isolated DB and point the route's connection at it.
    dbp = tmp_path / "t.db"
    c = sqlite3.connect(dbp)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.upsert_trader_analytics(c, {"source_user_id": "u1", "handle": "alice",
                                  "realized_pnl_usd": 50.0, "chain_mix": "{}",
                                  "provider_mix": "{}", "metrics_json": "{}"})
    c.close()

    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api, "get_connection", lambda *a, **k: sqlite3.connect(dbp))

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(api.bp)
    client = app.test_client()
    resp = client.get("/api/memecoin/traders/analytics")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["rows"][0]["source_user_id"] == "u1"
```

(Adjust `api.get_connection` / `api.bp` names in Step 1 to whatever the module actually exports. If the module uses a shared `_conn()` helper, patch that instead. The row_factory must be `sqlite3.Row` for `dict(r)` in `list_trader_analytics`.)

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fomo_account_route.py -q`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 4: Write minimal implementation**

Add to `sniper/routes/api_memecoin.py` (using the module's existing blueprint name and connection helper — shown here as `bp` / `get_connection`; substitute the real names found in Step 1):

```python
@bp.route("/api/memecoin/traders/analytics", methods=["GET"])
def memecoin_traders_analytics():
    conn = get_connection()
    try:
        rows = db_memecoin.list_trader_analytics(conn)
    finally:
        conn.close()
    return jsonify({"rows": rows})
```

Ensure `db_memecoin` and `jsonify` are imported in the module (they almost certainly are — verify in Step 1).

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fomo_account_route.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add sniper/routes/api_memecoin.py tests/test_fomo_account_route.py
git commit -m "feat(fomo-analyzer): GET /api/memecoin/traders/analytics"
```

---

### Task 9: UI table

**Files:**
- Modify: `static/app.js` (extend the `memecoin` controller with a `_loadAnalytics` method + render target)
- Modify: `templates/index.html` (add an "Analytics" sub-section container inside `#tab-memecoin`)

**Interfaces:**
- Consumes: `GET /api/memecoin/traders/analytics`.
- Produces: a sortable HTML table rendered into `#memecoin-analytics`.

- [ ] **Step 1: Add the container to the memecoin tab**

In `templates/index.html`, inside the `#tab-memecoin` panel (near the traders panel), add:

```html
<div class="memecoin-analytics-wrap">
  <h3>Trader Analytics <small id="memecoin-analytics-meta"></small></h3>
  <div id="memecoin-analytics" class="scroll-x"></div>
</div>
```

- [ ] **Step 2: Add the loader/renderer to the `memecoin` controller in `static/app.js`**

Locate the `memecoin` controller object (search `memecoin` near line ~1984) and add a method, following the existing `_load*` fetch/render style already used there:

```javascript
_loadAnalytics() {
  fetch('/api/memecoin/traders/analytics')
    .then(r => r.json())
    .then(d => {
      const rows = (d && d.rows) || [];
      const cols = ['handle','n_closed','realized_pnl_usd','win_rate','profit_factor',
                    'top3_pnl_share','median_pnl_pct','median_hold_s','avg_buy_usd',
                    'large_buys_7d','swaps_7d','last_swap_age_h','closed_coverage',
                    'pct_off_platform','hit_full_history','window_days'];
      const fmt = (k, v) => {
        if (v === null || v === undefined) return '';
        if (['win_rate','closed_coverage','pct_off_platform','top3_pnl_share'].includes(k))
          return (v*100).toFixed(0)+'%';
        if (typeof v === 'number') return (Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));
        return v;
      };
      let sortKey = this._anSort || 'realized_pnl_usd';
      rows.sort((a,b) => (b[sortKey]||-1e18) - (a[sortKey]||-1e18));
      let html = '<table class="mc-analytics"><thead><tr>' +
        cols.map(c => `<th data-k="${c}">${c}</th>`).join('') + '</tr></thead><tbody>';
      for (const r of rows) {
        html += '<tr>' + cols.map(c => `<td>${fmt(c, r[c])}</td>`).join('') + '</tr>';
      }
      html += '</tbody></table>';
      const el = document.getElementById('memecoin-analytics');
      el.innerHTML = html;
      el.querySelectorAll('th').forEach(th => th.onclick = () => {
        this._anSort = th.getAttribute('data-k'); this._loadAnalytics();
      });
      const meta = document.getElementById('memecoin-analytics-meta');
      if (meta) meta.textContent = rows.length ? `${rows.length} traders · computed ${rows[0].computed_at || ''}` : 'no data yet — run scripts/analyze_fomo_accounts.py --all';
    })
    .catch(() => {});
},
```

- [ ] **Step 3: Call `_loadAnalytics` when the memecoin tab activates**

Find where the memecoin controller's other `_load*` methods are invoked on tab show/poll, and add `this._loadAnalytics();` alongside them (analytics is on-demand data, so it does NOT need to be in the fast 4s poll — calling it on tab activation is enough).

- [ ] **Step 4: Verify (manual, no unit test for DOM)**

Run: `node --check static/app.js`
Expected: no syntax error. (If `node` is unavailable, run `python3 -c "import esprima"` is not available; instead visually confirm the braces/commas match the surrounding methods.)

Then, with the web server running and at least one analytics row present, load `/?tab=memecoin` and confirm the "Trader Analytics" table renders and column-click re-sorts.

- [ ] **Step 5: Commit**

```bash
git add static/app.js templates/index.html
git commit -m "feat(fomo-analyzer): Trader Analytics table in memecoin tab (sortable)"
```

---

### Task 10: Integration smoke + finalize

**Files:**
- Test: `tests/test_fomo_account_integration.py`

**Interfaces:**
- Consumes: all prior tasks.

- [ ] **Step 1: Write an end-to-end test through analyzer → persistence → list (stubbed network)**

```python
# tests/test_fomo_account_integration.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.analysis.fomo_account.analyzer import analyze_account

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
MEME = "MINTINT"


def test_full_pipeline_persists_a_rankable_row():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)

    profile = {"id": "u1", "userHandle": "alice", "createdAt": "2026-01-01T00:00:00Z",
               "followers": 10, "following": 5, "swapCount": 2}
    page = {"responseObject": {"swaps": [
        {"inTokenAddress": USDC, "outTokenAddress": MEME, "humanUsdAmountIn": "100",
         "humanUsdAmountOut": "100", "inHumanAmount": "100", "outHumanAmount": "1000",
         "createdAt": "2026-07-01T10:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
        {"inTokenAddress": MEME, "outTokenAddress": USDC, "humanUsdAmountIn": "150",
         "humanUsdAmountOut": "150", "inHumanAmount": "1000", "outHumanAmount": "150",
         "createdAt": "2026-07-01T11:00:00Z", "isOffPlatform": "False",
         "networkId": 1399811149, "provider": "DFLOW"},
    ], "hasNextPage": False}}

    def fetch(auth, path):
        return (200, {"responseObject": profile}, "d") if path == "/v2/users/u1" \
            else (200, page, "d")

    row = analyze_account(None, "u1", now_iso="2026-07-06T00:00:00Z", max_pages=3, fetch=fetch)
    m.upsert_trader_analytics(c, row)
    listed = m.list_trader_analytics(c)
    assert len(listed) == 1
    assert listed[0]["realized_pnl_usd"] == 50.0
    assert listed[0]["win_rate"] == 1.0
```

- [ ] **Step 2: Run the full new-package suite**

Run:
```bash
python3 -m pytest tests/test_fomo_account_classify.py tests/test_fomo_account_reconstruct.py \
  tests/test_fomo_account_metrics.py tests/test_fomo_account_adapter.py \
  tests/test_fomo_account_analyzer.py tests/test_fomo_account_persistence.py \
  tests/test_fomo_account_cli.py tests/test_fomo_account_route.py \
  tests/test_fomo_account_integration.py -q
```
Expected: all PASS.

- [ ] **Step 3: Schema validator + brain index freshness**

Run:
```bash
python3 scripts/brain_index.py generate
python3 scripts/validate_schema_refs.py
git diff --quiet index/schema_registry.json index/dependency_index.json && echo "index fresh" || echo "index changed — will commit"
```
Expected: validator "No schema violations found"; index fresh (already regenerated in Task 6) or staged now.

- [ ] **Step 4: One live smoke against fomo (manual, optional but recommended)**

Run: `python3 scripts/analyze_fomo_accounts.py --handle connerb --max-pages 3`
Expected: prints a one-row table and writes a CSV. Confirms the live adapter + auth path work end-to-end. (connerb is a known all-buys trader → `n_closed=0`, `n_open_bags>0` — a good censoring smoke.)

- [ ] **Step 5: Commit**

```bash
git add tests/test_fomo_account_integration.py index/schema_registry.json index/dependency_index.json
git commit -m "test(fomo-analyzer): end-to-end pipeline smoke + finalize"
```

---

## Self-Review

**Spec coverage:**
- §3 data model → Tasks 1 (cash set), 4 (endpoints). ✓
- §4 architecture units → Tasks 1–9 map 1:1 to the table. ✓
- §5 reconstruction → Task 2. ✓
- §6 metrics catalog (all 6 groups) → Task 3 (implemented) + Task 6 (columns). ✓
- §7 persistence → Task 6. ✓
- §8 surfaces (CLI/API/UI) → Tasks 7/8/9. ✓
- §9 settings → Task 4. ✓
- §10 testing matrix → Tasks 1–3 tests cover every listed case (clean, partial, avg-down, power-law, no-sell, unmatched, routes, off-platform, multichain, page-cap). ✓
- §11 honesty rails → coverage fields emitted in Task 3, surfaced in Task 9. ✓
- §12 risks → page-cap flag (Task 4/5), rate limit (Task 7 sleep), cash drift (route_approx metric). ✓

**Placeholder scan:** No TBD/TODO. The only "substitute the real name" notes are in Task 8/9 where the exact blueprint/connection symbol must be read from the existing file first (Step 1 of Task 8 does this) — the code is otherwise complete.

**Type consistency:** `analyze_account(auth, uid, *, now_iso, max_pages, large_buy_usd, fetch)` used identically in Tasks 5, 7, 10. `compute_metrics(profile, swaps, recon, *, now_iso, large_buy_usd, hit_full_history, pages_fetched)` consistent Tasks 3/5. `reconstruct` return keys consistent Tasks 2/3. `upsert_trader_analytics`/`list_trader_analytics`/`get_trader_analytics` consistent Tasks 6/7/8/10. Column list single-sourced via `TRADER_ANALYTICS_COLUMNS` (Task 6).
