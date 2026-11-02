# Tape-priced momentum_a on 4663 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `momentum_a` a tape-derived USD price on chain 4663 and un-block the arm there, so it can run on a chain where it has only ever failed for want of a quote ($0.00 over 138 closes, 100% `a_timecap_noquote`).

**Architecture:** Token decimals are cached into `pool_meta` through the existing bounded `resolve_pool_meta` pass. A single pure function converts the tape's atomic quote/base ratio into USD. The momentum_a exit sweep prefers that price on 4663 and falls back to DexScreener. Everything stays inert until a pre-registered cross-check proves the conversion reconciles with DexScreener — only then is the arm un-blocked.

**Tech Stack:** Python 3, SQLite (the separate ticks DB `data/memecoin_ticks.db`), `sniper.analysis.tick_features`, `sniper.analysis.exit_features`, `sniper.execution.evm_wallet`, pytest.

## Global Constraints

- **PAPER only. Zero funded-behaviour change.** No funded rail, env, production-DB, or runner behaviour ships from this plan.
- **No clip-size or exposure increase** while live expectancy is negative (brief §6).
- **`momentum_decision` logic is NOT changed.** Only the `price` fed into it differs.
- **Fail-closed:** any missing input (decimals, quote USD, empty block path) ⇒ no tape price, never a guessed number.
- **New thresholds go in `sniper/settings/memecoin.py`**, never in service files.
- **4663 only.** The tape covers 4663; 8453 is watched-pool-filtered and `evm-unknown`/56/1 have no tape. They stay blocked.
- **No gate read below n≥30.**
- Spec: `docs/superpowers/specs/2026-07-24-memecoin-tape-priced-momentum-design.md`.

## File Structure

| File | Responsibility |
|---|---|
| `sniper/analysis/tick_features.py` (modify) | `pool_meta` gains `token0_decimals`/`token1_decimals`; `resolve_pool_meta` fills them |
| `sniper/analysis/tape_price.py` (create) | ONE pure function: atomic block VWAP → USD. No DB, no network |
| `sniper/settings/memecoin.py` (modify) | tape freshness bound + cross-check tolerance/min-n |
| `sniper/services/memecoin_exits.py` (modify) | momentum_a 4663 price precedence + attribution |
| `sniper/services/memecoin_copy_trader.py` (modify) | chain-aware un-block (LAST, gated on Task 4) |
| `scripts/memecoin_tape_price_crosscheck.py` (create) | the §5 validation harness |
| `tests/test_tape_price.py` (create) | conversion + fail-closed |
| `tests/test_pool_meta_decimals.py` (create) | decimals resolution, bounded + cached |
| `tests/test_momentum_tape_pricing.py` (create) | price precedence, attribution, chain gating |

**Execution order is deliberate:** Tasks 1–3 are inert (the arm is still blocked on 4663, so nothing changes at runtime). Task 4 measures. Task 5 un-blocks **only if Task 4 passes**.

---

### Task 1: Token decimals cached in `pool_meta`

**Files:**
- Modify: `sniper/analysis/tick_features.py` (DDL at `:33-41`, `resolve_pool_meta` at `:63`)
- Test: `tests/test_pool_meta_decimals.py`

**Interfaces:**
- Consumes: the existing injected `eth_call(address, selector) -> hex str | None` seam.
- Produces: `pool_meta.token0_decimals` / `token1_decimals` (INTEGER, NULL = unresolved). `resolve_pool_meta` signature is unchanged.

**Note on the selector:** `tick_features` selectors are `0x`-prefixed (`SEL_TOKEN0 = "0x0dfe1681"`), while `evm_wallet.SEL_DECIMALS` is **not** (`"313ce567"`). Define a local `0x`-prefixed constant. A mismatched selector fails silently — this is the `assets_ids` failure mode.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pool_meta_decimals.py
"""Task 1 — token decimals cached in pool_meta, bounded and fail-closed."""
import sqlite3

from sniper.analysis import tick_features as tf


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    tf.ensure_feature_tables(c)
    return c


def _eth_call(responses):
    """Injected seam: {(address, selector): hex}. Missing -> None (a failed call)."""
    def call(addr, selector):
        return responses.get((addr, selector))
    return call


def test_decimals_are_resolved_and_cached():
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): hex(18),
        (base, tf.SEL_DECIMALS): hex(9),
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["quote_side"] == 0
    assert r["token0_decimals"] == 18 and r["token1_decimals"] == 9


def test_failed_decimals_call_leaves_null_not_a_guess():
    tc = _tc()
    quote, base = "0x" + "1" * 40, "0x" + "2" * 40
    calls = {
        ("0xp", tf.SEL_TOKEN0): "0x" + "0" * 24 + quote[2:],
        ("0xp", tf.SEL_TOKEN1): "0x" + "0" * 24 + base[2:],
        (quote, tf.SEL_DECIMALS): hex(18),
        # base decimals call fails -> absent
    }
    tf.resolve_pool_meta(tc, "4663", ["0xp"], _eth_call(calls), {quote}, "t")
    r = tc.execute("SELECT token0_decimals, token1_decimals FROM pool_meta").fetchone()
    assert r["token0_decimals"] == 18
    assert r["token1_decimals"] is None      # never 18, never 0, never guessed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_pool_meta_decimals.py -v`
Expected: FAIL — `AttributeError: module 'sniper.analysis.tick_features' has no attribute 'SEL_DECIMALS'`

- [ ] **Step 3: Add the selector and the DDL columns**

In `sniper/analysis/tick_features.py`, beside `SEL_TOKEN0`/`SEL_TOKEN1`:

```python
SEL_DECIMALS = "0x313ce567"   # decimals(). 0x-prefixed to match SEL_TOKEN0/1 in this module —
                              # evm_wallet.SEL_DECIMALS is bare and would fail silently here.
```

In `ensure_feature_tables`, after the `CREATE TABLE ... pool_meta` statement, add idempotent column adds (the table already exists in the live ticks DB, so `CREATE TABLE IF NOT EXISTS` alone will not add them):

```python
    for col in ("token0_decimals", "token1_decimals"):
        try:
            tc.execute(f"ALTER TABLE pool_meta ADD COLUMN {col} INTEGER")
        except sqlite3.OperationalError:
            pass          # already present
```

Add `import sqlite3` to the module imports if absent.

- [ ] **Step 4: Resolve decimals inside `resolve_pool_meta`**

Replace the body of the `for pool in todo:` loop's tail (the `tc.execute("INSERT OR REPLACE INTO pool_meta ...")` call) with:

```python
        def _dec(token):
            raw = eth_call(token, SEL_DECIMALS)
            if not raw:
                return None                     # failed call -> unresolved, never guessed
            try:
                v = int(raw, 16)
            except (TypeError, ValueError):
                return None
            # ERC-20 decimals is a uint8. A value outside that range means the call did NOT return
            # a decimals word (contract lacks decimals(), or the node echoed something else) — a
            # garbage scale is worse than none, so fail closed. Also keeps SQLite from overflowing
            # on an address-shaped response.
            return v if 0 <= v <= 255 else None

        tc.execute(
            "INSERT OR REPLACE INTO pool_meta (chain_id, pool, token0, token1, quote_side,"
            " resolved_at, token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)",
            (str(chain_id), pool, token0, token1, quote_side, now_iso, _dec(token0), _dec(token1)))
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_pool_meta_decimals.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Verify no regression in the existing tick-feature surface**

Run: `python3 -m pytest tests/ -q -k "tick"`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add sniper/analysis/tick_features.py tests/test_pool_meta_decimals.py
git commit -m "feat(memecoin): cache token decimals in pool_meta (fail-closed)"
```

---

### Task 2: `tape_price_usd()` — the one conversion

**Files:**
- Create: `sniper/analysis/tape_price.py`
- Test: `tests/test_tape_price.py`

**Interfaces:**
- Consumes: `exec_price_path_per_block(swaps, *, quote_side) -> [(block_ts, price, n_prints)]` from `sniper.analysis.exit_features`.
- Produces: `tape_price_usd(swaps, *, quote_side, base_decimals, quote_decimals, quote_usd) -> float | None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tape_price.py
"""Task 2 — atomic block VWAP -> USD. The one place units convert; fail-closed everywhere."""
from sniper.analysis.tape_price import tape_price_usd


def _swap(block_ts, quote_atomic, base_atomic):
    """quote_side=0, so amount0 is quote and amount1 is base."""
    return {"kind": "v2_swap", "block_ts": block_ts,
            "amount0": str(quote_atomic), "amount1": str(-base_atomic)}


def test_converts_atomic_ratio_to_usd():
    # 1 WETH (18dp) buys 1000 tokens (9dp); WETH = $3000 -> $3.00 per token
    swaps = [_swap(10, 10**18, 1000 * 10**9)]
    px = tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0)
    assert abs(px - 3.0) < 1e-9


def test_uses_the_latest_block_not_the_first():
    swaps = [_swap(10, 10**18, 1000 * 10**9),      # $3.00
             _swap(11, 10**18, 500 * 10**9)]       # $6.00 — newer block wins
    px = tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0)
    assert abs(px - 6.0) < 1e-9


def test_fail_closed_on_every_missing_input():
    swaps = [_swap(10, 10**18, 1000 * 10**9)]
    assert tape_price_usd(swaps, quote_side=0, base_decimals=None, quote_decimals=18, quote_usd=3000.0) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=None, quote_usd=3000.0) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=None) is None
    assert tape_price_usd(swaps, quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=0) is None
    assert tape_price_usd([], quote_side=0, base_decimals=9, quote_decimals=18, quote_usd=3000.0) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_tape_price.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sniper.analysis.tape_price'`

- [ ] **Step 3: Write the implementation**

```python
# sniper/analysis/tape_price.py
"""Atomic tape ratio -> USD. The ONE place these units convert.

`exec_price_path_per_block` yields a size-weighted per-block price as an ATOMIC quote/base ratio —
not a human price, and emphatically not a USD price. Everything downstream that wants dollars comes
through here so the conversion exists exactly once.

Fail-closed: any missing input returns None. A missing decimal or quote-USD must never become a
silently-wrong number — mixing atomic and human units already produced one P0 defect in this project
(2026-07-23, drawdown-from-executable-peak).
"""
from __future__ import annotations

import math

from sniper.analysis.exit_features import exec_price_path_per_block


def tape_price_usd(swaps, *, quote_side, base_decimals, quote_decimals, quote_usd):
    """USD per base token from the most recent block in `swaps`, or None.

    human = (|quote|/10**quote_decimals) / (|base|/10**base_decimals)   # quote-token per base-token
    usd   = human * quote_usd                                           # USD per base-token
    """
    if base_decimals is None or quote_decimals is None:
        return None
    if not quote_usd or quote_usd <= 0:
        return None
    path = exec_price_path_per_block(swaps, quote_side=quote_side)
    if not path:
        return None
    atomic = path[-1][1]                      # newest block's size-weighted ratio
    human = atomic * (10 ** (int(base_decimals) - int(quote_decimals)))
    usd = human * float(quote_usd)
    return usd if math.isfinite(usd) and usd > 0 else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_tape_price.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/tape_price.py tests/test_tape_price.py
git commit -m "feat(memecoin): tape_price_usd — the single atomic->USD conversion"
```

---

### Task 3: momentum_a price precedence on 4663 (inert until Task 5)

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_exits.py` (momentum_a loop; `_mid_liq` is at `:304`)
- Test: `tests/test_momentum_tape_pricing.py`

**Interfaces:**
- Consumes: `tape_price_usd(...)` (Task 2); `pool_meta.token0_decimals`/`token1_decimals` (Task 1).
- Produces: `tape_mid_for_position(tc, pos, *, quote_usd, now_ts) -> (price, source) | (None, None)` in `sniper/services/memecoin_exits.py`.

**Why a new resolver rather than editing `_mid_liq`:** `_mid_liq` is shared by the scalp, momentum_a and alpha_b loops. Changing it would alter pricing for arms this plan does not touch.

**CORRECTED 2026-07-24 (spec §4.3b):** the position→pool lookup must NOT use `quote_route_target` —
that is the router/settler, not the pool, and has zero ticks by construction. Resolve the pool from
the position's `mint`, matching the **base** side only, on pools with `quote_side` and both decimals
resolved; **fail closed when more than one pool matches** (97% of resolvable mints match exactly one).

- [ ] **Step 1: Add the settings knob**

In `sniper/settings/memecoin.py`, beside the other memecoin knobs:

```python
# --- tape-priced momentum_a on 4663 (2026-07-24) ---
# How stale the newest tape block may be and still price a position. 90s ~ 3 momentum sweeps;
# beyond that the tape is not "faster than DexScreener" in any useful sense.
MEMECOIN_TAPE_PRICE_MAX_AGE_S = float(os.getenv("MEMECOIN_TAPE_PRICE_MAX_AGE_S", "90"))
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_momentum_tape_pricing.py
"""Task 3 — momentum_a prefers the tape price on 4663, falls back, and records attribution."""
import sqlite3

from sniper.analysis import tick_features as tf
from sniper.persistence import db_ticks as t
from sniper.services.memecoin_exits import tape_mid_for_position


def _tc(block_ts_iso, quote_dec=18, base_dec=9):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); tf.ensure_feature_tables(c)
    t.insert_ticks(c, [{"chain_id": "4663", "pool": "0xp", "kind": "v2_swap", "block_no": 5,
                        "tx_sig": "0xa", "log_index": 0, "maker": "0xr",
                        "amount0": str(10**18), "amount1": str(-1000 * 10**9),
                        "observed_at": block_ts_iso}])
    t.backfill_block_times(c, "4663", [5], lambda b: 1_700_000_000, block_ts_iso)
    c.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at,"
              " token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)",
              ("4663", "0xp", "0xq", "0xb", 0, block_ts_iso, quote_dec, base_dec))
    c.commit()
    return c


def test_tape_prices_a_4663_position():
    tc = _tc("2026-07-24T00:00:00+00:00")
    pos = {"id": 1, "network_id": "4663", "quote_route_target": "0xP"}   # mixed case on purpose
    price, source = tape_mid_for_position(tc, pos, quote_usd=3000.0, now_ts=1_700_000_010)
    assert abs(price - 3.0) < 1e-9
    assert source == "tape"


def test_stale_tape_does_not_price():
    tc = _tc("2026-07-24T00:00:00+00:00")
    pos = {"id": 1, "network_id": "4663", "quote_route_target": "0xp"}
    price, source = tape_mid_for_position(tc, pos, quote_usd=3000.0, now_ts=1_700_009_999)
    assert price is None and source is None      # older than MEMECOIN_TAPE_PRICE_MAX_AGE_S


def test_unresolved_decimals_do_not_price():
    tc = _tc("2026-07-24T00:00:00+00:00")
    tc.execute("UPDATE pool_meta SET token1_decimals = NULL")
    tc.commit()
    pos = {"id": 1, "network_id": "4663", "quote_route_target": "0xp"}
    assert tape_mid_for_position(tc, pos, quote_usd=3000.0, now_ts=1_700_000_010) == (None, None)


def test_non_4663_chain_is_never_tape_priced():
    tc = _tc("2026-07-24T00:00:00+00:00")
    for net in ("solana", "8453", "56", None):
        pos = {"id": 1, "network_id": net, "quote_route_target": "0xp"}
        assert tape_mid_for_position(tc, pos, quote_usd=3000.0, now_ts=1_700_000_010) == (None, None)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_momentum_tape_pricing.py -v`
Expected: FAIL — `ImportError: cannot import name 'tape_mid_for_position'`

- [ ] **Step 4: Implement the resolver**

Add to `sniper/services/memecoin_exits.py` at module level (not inside the sweep closure):

```python
def tape_mid_for_position(tc, pos, *, quote_usd, now_ts):
    """USD mid for a 4663 position from the recorded tick tape, or (None, None).

    4663 ONLY: the tape covers that chain unfiltered. Fail-closed on a missing pool, missing
    pool_meta, unresolved decimals, an empty block path, or a tape older than
    MEMECOIN_TAPE_PRICE_MAX_AGE_S. Returns (price, "tape") so the caller can attribute the close.
    """
    from sniper.analysis.tape_price import tape_price_usd
    from sniper.persistence import db_ticks as _t
    from sniper.settings import memecoin as _cfg

    if str(pos.get("network_id") or "") != "4663":
        return (None, None)
    pool = pos.get("quote_route_target")
    if not pool:
        return (None, None)
    pool = str(pool).lower()
    try:
        meta = tc.execute(
            "SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id='4663' AND pool=?", (pool,)).fetchone()
        if not meta or meta["quote_side"] is None:
            return (None, None)
        qs = int(meta["quote_side"])
        quote_dec = meta["token0_decimals"] if qs == 0 else meta["token1_decimals"]
        base_dec = meta["token1_decimals"] if qs == 0 else meta["token0_decimals"]
        rows = tc.execute(
            "SELECT kind, block_no, amount0, amount1 FROM pool_ticks "
            "WHERE chain_id='4663' AND pool=? AND kind IN ('v2_swap','v3_swap') "
            "AND block_no IS NOT NULL ORDER BY block_no DESC LIMIT 200", (pool,)).fetchall()
        swaps = [dict(r) for r in rows]
        if not swaps:
            return (None, None)
        bt = _t.block_ts_map(tc, "4663", {s["block_no"] for s in swaps})
        timed = []
        for s in swaps:
            iso = bt.get(s["block_no"])
            if iso is None:
                continue
            ep = _epoch_s(iso)
            if ep is None:
                continue
            timed.append({**s, "block_ts": ep})
        if not timed:
            return (None, None)
        newest = max(s["block_ts"] for s in timed)
        max_age = float(getattr(_cfg, "MEMECOIN_TAPE_PRICE_MAX_AGE_S", 90.0))
        age = float(now_ts) - float(newest)
        if age < 0 or age > max_age:
            return (None, None)          # stale, or event time ahead of the clock -> fail closed
        px = tape_price_usd(timed, quote_side=qs, base_decimals=base_dec,
                            quote_decimals=quote_dec, quote_usd=quote_usd)
        return (px, "tape") if px else (None, None)
    except Exception:
        logger.exception("tape mid failed pos=%s", pos.get("id"))
        return (None, None)


def _epoch_s(iso):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None
```

If `memecoin_exits.py` has no module-level `logger`, add `import logging` and `logger = logging.getLogger("memecoin")` near the top.

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_momentum_tape_pricing.py -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Wire the precedence into the momentum_a loop**

In `sniper/services/memecoin_exits.py`, inside `for pos in m.open_positions_by_strategy(conn, "momentum_a"):`, replace the opening `mid, liq = _mid_liq(pos)` with:

```python
        mid, liq = _mid_liq(pos)
        price_source = "dexscreener" if mid else None
        if tc is not None:                      # ticks DB available -> 4663 may be tape-priced
            t_mid, t_src = tape_mid_for_position(
                tc, pos, quote_usd=tape_quote_usd, now_ts=_now_ts)
            if t_mid:
                mid, price_source = t_mid, t_src
```

`tc`, `tape_quote_usd` and `_now_ts` are supplied by the sweep's caller; when `tc` is None the behaviour is byte-identical to today. Pass `source=price_source` into the momentum_a `_close(...)` call so `price_attribution` records which source priced the exit.

- [ ] **Step 7: Verify the full memecoin surface is unchanged**

Run: `python3 -m pytest tests/ -q -k "memecoin or exit_ or tick"`
Expected: all pass. The arm is still blocked on 4663, so this is inert at runtime.

- [ ] **Step 8: Commit**

```bash
git add sniper/settings/memecoin.py sniper/services/memecoin_exits.py tests/test_momentum_tape_pricing.py
git commit -m "feat(memecoin): tape price precedence for momentum_a on 4663 (inert until un-blocked)"
```

---

### Task 4: Cross-check harness — prove the conversion before it drives anything

**Files:**
- Create: `scripts/memecoin_tape_price_crosscheck.py`
- Modify: `docs/superpowers/specs/2026-07-24-memecoin-tape-priced-momentum-design.md` (record the pre-registration)

**Interfaces:**
- Consumes: `tape_price_usd(...)`, `pool_meta`, `pool_ticks`, `block_times`, and `memecoin_position_marks`
  — real columns are `(chain_id, mint, sweep_ts, mid, executable_exit_value, liquidity, mcap,
  route_quote_id, source, quality)`. There is **no** `position_id`/`price`/`observed_at`; join on
  `(chain_id, mint)`. 548,714 live 4663 marks across 269 mints exist as of 2026-07-24.
- Produces: a printed report — n compared, median and p90 relative error, pass/fail against the pre-registered tolerance.

- [ ] **Step 1: PRE-REGISTER the thresholds — before computing anything**

Append to §5 of the design spec and commit **before** running the harness:

```markdown
### 5.1 Pre-registration (written 2026-07-24, before any comparison was computed)
- **Alignment window:** a tape block and a DexScreener quote are comparable when their timestamps
  are within **120 s**.
- **Tolerance:** PASS requires **median relative error ≤ 5%** AND **p90 ≤ 15%**.
- **Minimum n:** **30** aligned pairs. Below that the harness reports progress only and no verdict.
- **Branch on PASS:** the tape price may drive momentum_a on 4663 (Task 5).
- **Branch on FAIL:** the conversion is wrong. Task 5 does NOT ship. Investigate decimals
  resolution and quote_side orientation first — those are the two ways this goes wrong.
```

```bash
git add docs/superpowers/specs/2026-07-24-memecoin-tape-priced-momentum-design.md
git commit -m "docs(memecoin): pre-register tape-price cross-check thresholds (before any read)"
```

- [ ] **Step 2: Write the harness**

```python
#!/usr/bin/env python3
"""Cross-check tape-derived USD against DexScreener USD on 4663 (design §5).

Read-only. Prints n, median and p90 relative error, and the pre-registered verdict. The tape price
may not drive any exit until this passes.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.analysis.tape_price import tape_price_usd          # noqa: E402
from sniper.persistence import db_ticks as t                    # noqa: E402

ALIGN_S = 120.0
TOL_MEDIAN = 0.05
TOL_P90 = 0.15
MIN_N = 30


def main():
    tc = t.connect_ticks()
    conn = __import__("sqlite3").connect("file:sniper_tracker.db?mode=ro", uri=True)
    conn.row_factory = __import__("sqlite3").Row
    errs = []
    # Each mark is a DexScreener USD price for a 4663 coin at a known time; find the tape block
    # nearest that time for the coin's pool and compare.
    # memecoin_position_marks is keyed (chain_id, mint, sweep_ts) — there is NO position_id column.
    # Join on (chain_id, mint). quality='live' excludes quarantined spike marks. NEVER read
    # executable_exit_value here: it is an ALIAS for mid (binding retraction, exit-brain spec §1),
    # not an executable price.
    marks = conn.execute(
        "SELECT DISTINCT p.quote_route_target AS pool, mk.mid AS dex_usd, mk.sweep_ts, mk.source "
        "FROM memecoin_position_marks mk "
        "JOIN memecoin_positions p ON p.mint = mk.mint AND p.network_id = mk.chain_id "
        "WHERE mk.chain_id='4663' AND mk.quality='live' AND mk.mid > 0 "
        "AND p.quote_route_target IS NOT NULL").fetchall()
    for mk in marks:
        pool = str(mk["pool"]).lower()
        meta = tc.execute(
            "SELECT quote_side, token0_decimals, token1_decimals FROM pool_meta "
            "WHERE chain_id='4663' AND pool=?", (pool,)).fetchone()
        if not meta or meta["quote_side"] is None:
            continue
        qs = int(meta["quote_side"])
        qd = meta["token0_decimals"] if qs == 0 else meta["token1_decimals"]
        bd = meta["token1_decimals"] if qs == 0 else meta["token0_decimals"]
        if qd is None or bd is None:
            continue
        rows = tc.execute(
            "SELECT kind, block_no, amount0, amount1 FROM pool_ticks WHERE chain_id='4663' "
            "AND pool=? AND kind IN ('v2_swap','v3_swap') AND block_no IS NOT NULL "
            "ORDER BY block_no DESC LIMIT 400", (pool,)).fetchall()
        if not rows:
            continue
        swaps = [dict(r) for r in rows]
        bt = t.block_ts_map(tc, "4663", {s["block_no"] for s in swaps})
        from datetime import datetime
        def _ep(x):
            try:
                return datetime.fromisoformat(str(x).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return None
        target = _ep(mk["sweep_ts"])
        timed = [{**s, "block_ts": _ep(bt.get(s["block_no"]))} for s in swaps]
        timed = [s for s in timed if s["block_ts"] is not None]
        if target is None or not timed:
            continue
        aligned = [s for s in timed if abs(s["block_ts"] - target) <= ALIGN_S]
        if not aligned:
            continue
        px = tape_price_usd(aligned, quote_side=qs, base_decimals=bd,
                            quote_decimals=qd, quote_usd=_quote_usd())
        if not px:
            continue
        errs.append(abs(px - float(mk["dex_usd"])) / float(mk["dex_usd"]))

    n = len(errs)
    if n < MIN_N:
        print(f"tape cross-check: {n}/{MIN_N} aligned pairs — no verdict (pre-registered).")
        return
    med = statistics.median(errs)
    p90 = sorted(errs)[int(0.9 * (n - 1))]
    ok = med <= TOL_MEDIAN and p90 <= TOL_P90
    print(f"tape cross-check: n={n} median_rel_err={med:.4f} p90={p90:.4f}")
    print(f"VERDICT: {'PASS — tape may price momentum_a on 4663' if ok else 'FAIL — do NOT ship Task 5'}")


def _quote_usd():
    from sniper.execution.evm_wallet import quote_usd_price
    return quote_usd_price("4663")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run the harness**

Run: `python3 scripts/memecoin_tape_price_crosscheck.py`
Expected: either a progress line (`n/30`) or a verdict line. **Record the exact output.**

- [ ] **Step 4: Follow the pre-registered branch**

- PASS → proceed to Task 5.
- FAIL → **stop.** Do not implement Task 5. Investigate decimals resolution and `quote_side` orientation, report to the operator.
- Below n=30 → the tape needs to accumulate more overlap with 4663 marks. Report and stop; do not lower the gate.

- [ ] **Step 5: Commit the harness**

```bash
git add scripts/memecoin_tape_price_crosscheck.py
git commit -m "feat(memecoin): tape-vs-DexScreener price cross-check harness (design §5)"
```

---

### Task 5: Un-block momentum_a on 4663 — ONLY if Task 4 passed

**Files:**
- Modify: `sniper/services/memecoin_copy_trader.py:370`
- Test: `tests/test_momentum_tape_pricing.py` (append)

**Interfaces:**
- Consumes: the Task 4 verdict. **Do not start this task if the harness did not print PASS.**

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_momentum_tape_pricing.py
def test_momentum_a_chain_gate_admits_4663_and_solana_only():
    from sniper.services.memecoin_copy_trader import momentum_a_chain_allowed
    assert momentum_a_chain_allowed("solana") is True
    assert momentum_a_chain_allowed("") is True          # unknown-net legacy path, unchanged
    assert momentum_a_chain_allowed("4663") is True      # NEW: tape-priced
    for blocked in ("8453", "56", "1", "evm-unknown"):
        assert momentum_a_chain_allowed(blocked) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_momentum_tape_pricing.py::test_momentum_a_chain_gate_admits_4663_and_solana_only -v`
Expected: FAIL — `ImportError: cannot import name 'momentum_a_chain_allowed'`

- [ ] **Step 3: Implement the gate**

In `sniper/services/memecoin_copy_trader.py`, at module level:

```python
def momentum_a_chain_allowed(network_id):
    """Chains momentum_a may open on. Solana has always worked; 4663 is admitted from 2026-07-24
    because the tick tape prices it faster than DexScreener (the 15-min hold used to expire before
    the first quote landed -> $0.00 over 138 closes). Every other EVM chain has NO tape, so its
    quotes are still too slow for a 15-min arm and it stays refused."""
    return str(network_id or "").strip().lower() in ("solana", "", "4663")
```

Replace the guard at `:370`:

```python
        def _open(a):
            if a == "momentum_a" and not momentum_a_chain_allowed(net):
                return "skipped_slow_chain"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_momentum_tape_pricing.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Full regression**

Run: `python3 -m pytest tests/ -q -k "memecoin or exit_ or tick"` then `python3 scripts/validate_schema_refs.py` and `git diff --check`
Expected: all pass, no schema violations, clean diff

- [ ] **Step 6: Commit**

```bash
git add sniper/services/memecoin_copy_trader.py tests/test_momentum_tape_pricing.py
git commit -m "feat(memecoin): un-block momentum_a on 4663 (tape-priced, cross-check passed)"
```

---

## Verification & handoff

- After each task: the task's own pytest command, plus `python3 -m pytest tests/ -q -k "memecoin or exit_ or tick"`.
- Before "complete": `python3 -m pytest tests/ -q` (report exact counts; `test_ui_algo_tabs` and `tests/calibration/test_calibration_set.py::test_calibration_label[0xb3b50ef6]` are KNOWN pre-existing failures — verified pre-existing at `f2c3cf83`), `python3 scripts/validate_schema_refs.py`, `git diff --check`.
- **Reads that inform funding are armed-only**, split by `network_id` AND `price_attribution` (spec §6). The Solana +$3,221 control never merges with 4663 numbers.
- Then `superpowers:requesting-code-review` and the Knowledge Sync Gate before any PR.

## Self-review against the spec

- §4.1 decimals → Task 1. §4.2 conversion → Task 2. §4.3 un-block → Task 5. §4.4 price selection → Task 3. §4.5 thresholds → Task 3 Step 1 + Task 4 Step 1. §5 validation gate → Task 4. §6 evidence hygiene → Verification section. §7 Solana → out of scope by design. §8 testing → each task's tests.
- Deviation from spec §4.1, deliberate: decimals are read through `resolve_pool_meta`'s **injected `eth_call` seam** rather than `erc20_decimals(token, rpc_url)`. The seam keeps the tests RPC-free and matches the function's existing contract; `erc20_decimals` would require threading an `rpc_url` through a function that does not take one. Same fail-closed semantics.
- No placeholders. Every code step carries complete code. `tape_price_usd` and `tape_mid_for_position` signatures are identical everywhere they appear.
