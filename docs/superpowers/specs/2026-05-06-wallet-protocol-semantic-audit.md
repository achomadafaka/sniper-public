# Wallet Protocol — Semantic-Correctness Audit (read-only)

**Date:** 2026-05-06
**Trigger:** NO-token side-adjustment defect surfaced during Timing Surface v1 spec review.
**Scope:** Wallet-protocol modules under `sniper/analysis/wallet_protocol/` plus
the upstream price/PnL plumbing in `sniper/markets/polymarket_api.py`. Read-only —
no code changes, no DB mutation, no service restart, no batch runs.
**Status:** complete; post-audit followups R1/R2/R3/R8/R9.1-R9.5 closed.

---

## 1. Executive summary

| | |
|---|---|
| **Critical findings** | 1 (FH6 side-adjustment matches the same defect pattern that would have shipped in Timing Surface v1) |
| **High findings** | 2 (curPrice coordinate mismatch within `closed_positions`; F1 sloppy-operator gate may misclassify winning NO positions) |
| **Medium findings** | 2 (label-based polarity in `_is_win` fallback; `last SELL` collapse risks dust-close distortion) |
| **Low findings** | 2 (open-mark accounting tie may inherit YES-probability/held-token mismatch; OTHER bucket polarity unverified) |
| **Confirmed safe** | 6 (BUY/SELL classification; activity-row price coordinate; FIFO matching; flip_roi computation; capital-ledger sign convention; `realizedPnl` passthrough) |

**Recommendation:** **Proceed with Timing Surface v1 r1 as-designed**, plus a small required precondition: add a NO-token oracle test (described in §6) that locks the held-token coordinate for `avgPrice` and `activity_rows[].price`. The Timing Surface r1 spec already avoids the highest-risk surfaces (it never reads `curPrice` for captured-travel magnitude). The two HIGH findings (FH6, F1) are pre-existing and orthogonal to Timing Surface v1; they should be tracked as separate fix tickets rather than blocking the timing surface.

---

## 2. Methodology

Read-only inspection of:
- `sniper/analysis/wallet_protocol/{fetch,subgraph_reader,flip_reconstruction,exit_attribution,held_analyst,flipper,stage1_gates,capital_ledger,price_history_adapter,analyze_wallet,report,types}.py`
- `sniper/markets/polymarket_api.py` (closed-positions / activity / value endpoints)
- Existing tests in `tests/test_wallet_protocol_*.py` (treated as evidence of intended behavior, **not** as proof of correctness)
- CLAUDE.md ADRs §ADR-003, ADR-004, ADR-005

For each finding, a toy example was traced through the code path mentally to verify the sign / coordinate of the result.

**Hard rule applied throughout:** existing tests are treated as **codified assumptions**, not as oracles. Where a test passes by virtue of fixture and code sharing the same assumption, that proves only internal consistency, not external correctness.

---

## 3. Confirmed-safe assumptions

These were checked and found semantically correct:

### S1 — BUY/SELL classification in subgraph fallback
**File:** `fetch.py:_normalize_order_fill` (lines 58–139).
**Behavior:** When `wallet == maker, makerAssetId == USDC`, the row is BUY (wallet paid USDC for outcome token). Symmetric for taker. `outcome_token` is the non-USDC side.
**Why safe:** USDC ↔ outcome-token swap is unambiguous. `_CLOB_USDC_TOKEN_ID = "0"` is verified by ADR comment and used consistently. Tested by `test_wallet_protocol_fetch_subgraph_fallback.py`.

### S2 — Activity-row `price` coordinate
**File:** `fetch.py:120` — `price = usdc_amt / outcome_amt`.
**Behavior:** For a BUY of NO token at $0.40 each, `price = 0.40` (USDC per outcome share). This is the **held-token traded price**, not YES probability.
**Why safe:** Math is direct: USDC paid divided by outcome shares received. Identical for YES, NO, OTHER, categorical labels — the formula is label-agnostic. The same `price` field appears on REST `/activity` rows and on subgraph-normalized rows, both held-token-priced.
**Implication for Timing Surface:** Pre-resolution captured move = `last_sell_price − avgPrice` is in held-token coordinate uniformly. **Long-token rule applies; no side adjustment.** The r1 spec is correct on this point.

### S3 — FIFO flip matching
**File:** `flip_reconstruction.py:_fifo_match_one_position`.
**Behavior:** Groups by `(conditionId, outcome)` (token-pair stability), sorts by timestamp ascending, consumes BUY shares FIFO. `flip_roi = (sell_price − buy_price) / buy_price`.
**Why safe:** Both `buy_price` and `sell_price` come from activity-row `price` (held-token coordinate per S2). Same-coordinate subtraction works for any outcome. Confirmed: a NO flip from 0.40 → 0.60 yields `flip_roi = +0.50`, which is the correct realized return.

### S4 — `realizedPnl` passthrough
**File:** `held_analyst._realized_pnl` (line 161) and all consumers.
**Behavior:** `_realized_pnl(p) = float(p.get("realizedPnl") or 0.0)`. Pure passthrough of Polymarket's API field.
**Why safe:** Polymarket computes realizedPnl in dollars (signed). All downstream PnL aggregations (H1 winsorized bootstrap, exit-attribution PnL, edge-surface bucket PnL, DW/EW ROI) use this field as the authority. No re-derivation is attempted from prices, so coordinate-system bugs cannot leak into PnL. **PnL signs are trusted from upstream.**

### S5 — Capital-ledger sign convention
**File:** `capital_ledger.py`.
**Behavior:** SPLIT = deposit (USDC → outcome tokens), MERGE = withdrawal (outcome tokens → USDC), REDEMPTION = settlement payout (separate from withdrawals). `net_capital_funded = deposits − withdrawals`.
**Why safe:** Onchain semantics are unambiguous: `Split` and `Merge` are distinct subgraph entities with distinct stakeholder field names; `Redemption` is a third entity with `redeemer` + `payout` fields. The aggregator does not mix them.

### S6 — Activity-row attribution by token identity, not label
**Files:** `flip_reconstruction._group_by_position` (line 65), `held_analyst._has_pre_resolution_sell` (line 94), `exit_attribution.classify_position` (line 95).
**Behavior:** All three group/match by `(conditionId, outcome)` strings. The `outcome` string is itself bound to a stable token id at the API level (Polymarket maps outcome strings to `asset` token ids per market).
**Why safe:** Although the matching uses the label string, the label is per-market unique and stable (Polymarket does not rename outcomes mid-flight). The `asset` token id is the deeper identity but the (conditionId, outcome) pair is sufficient for matching within a single fetch. Risk of drift only if Polymarket starts renaming outcomes — currently low.

---

## 4. Risk register

### R1 — `flipper.py:run_fh6_market_direction` side-adjustment (CRITICAL)

```
Issue:                    FH6 applies an asymmetric side-adjustment that
                          appears inconsistent with the rest of the codebase's
                          coordinate handling.
File / function:          sniper/analysis/wallet_protocol/flipper.py:417-419
                          (`market_dir = -market_dir` when outcome ∈ {no, short})
Current behavior:         For a NO flip, market_dir = price_history_fn(asset_id, sell_ts)
                          − price_history_fn(asset_id, buy_ts) is then negated.
Why it may be wrong:      `f.asset_id` is the held token's id (a NO token id for
                          NO flips, per flip_reconstruction.py:104). `price_history_fn`
                          (price_history_adapter.py) returns prices for that
                          specific asset id — i.e., NO-token prices in held-token
                          coordinate. `flip_roi = (sell_price − buy_price) / buy_price`
                          on the same prices works correctly without any side
                          adjustment (S3). FH6 then applies a -1 flip on NO,
                          which over-corrects: it flips a coordinate that is
                          already aligned. Net effect: a winning NO flip whose
                          market also moved in the wallet's favor (NO appreciated)
                          would be classified `against_market` instead of
                          `with_market`. Inverts the with/against split for NO.
Example:                  Wallet bought NO at 0.40, sold at 0.60. flip_won=1.0.
                          price_history_fn(no_token_id, buy_ts) ≈ 0.40,
                          price_history_fn(no_token_id, sell_ts) ≈ 0.60.
                          market_dir = +0.20. Side-adjusted to −0.20. Code routes
                          to against_market: append 1.0. The ground truth is that
                          the market moved IN the wallet's favor (NO appreciated),
                          so the flip should be `with_market`.
Severity:                 critical
Confidence:               high (matches the same pattern that triggered the
                          Timing Surface r0 → r1 fix; tracing both is identical)
Caveat:                   not currently exercised in production — FH6 is
                          underpowered without an injected `price_history_fn`,
                          and the protocol does not inject one in the default
                          run path. The defect would activate the moment v1.5
                          wires the strict price-history adapter.
Recommended action:       FIX before v1.5 ships and before any caller wires a
                          live `price_history_fn` into the flipper branch.
                          Likely fix: remove the `market_dir = -market_dir`
                          line. Verify with a NO-flip oracle test analogous
                          to the §6 NO-token oracle proposed for the held
                          analyst.
Block Timing Surface?     no — Timing Surface v1 does not call price_history_fn
                          and does not depend on FH6. FH6 fix can land
                          independently.
```

### R2 — `closed_positions[].curPrice` coordinate-system mismatch (HIGH)

```
Issue:                    `curPrice` on /closed-positions rows is consumed in
                          two places that imply two different coordinates;
                          ADR-003 implies a third.
File / function:          held_analyst._is_win (lines 145-154); stage1_gates._count_open_dead;
                          analyze_wallet._compute_accounting_tie (lines 76-85);
                          ADR-003 in CLAUDE.md.
Current behavior:         _is_win treats curPrice as YES probability for binary
                          outcomes:
                              YES wins iff curPrice ≥ 1−ε
                              NO  wins iff curPrice ≤ ε
                          test_wallet_protocol_held_analyst.py:125 explicitly
                          asserts `_is_win({"outcome": "No", "curPrice": 0.0})` is
                          True with the docstring "A NO position wins when YES
                          price → 0".
                          For OTHER outcomes, _is_win falls back to `cp ≥ 0.5`
                          which would be consistent with curPrice = held-outcome's
                          price (NOT YES probability).
                          ADR-003 says "A YES position stores the YES token price;
                          a NO position stores the NO token price" — implying
                          curPrice should be held-token price for both binary sides.
Why it may be wrong:      Either (a) Polymarket /closed-positions returns curPrice
                          as YES probability for binary outcomes and as held-token
                          price for OTHER outcomes (asymmetric API — possible but
                          unverified), or (b) curPrice is held-token price uniformly
                          and `_is_win`'s NO branch is inverted (the test would also
                          be wrong, encoding the same assumption).
                          We cannot resolve which is correct from the codebase
                          alone; both "fixtures + code agree" cases produce a
                          self-consistent passing test that does not prove
                          empirical truth.
Example A (if curPrice = YES probability uniformly):
                          NO position resolves: NO token redeems at $1, but
                          curPrice = 0.0 (YES probability collapsed). _is_win NO
                          branch fires. realizedPnl > 0. Coherent.
Example B (if curPrice = held-token price uniformly):
                          NO position resolves: NO token redeems at $1, curPrice
                          = 1.0. _is_win NO branch checks cp ≤ ε → False.
                          _is_win returns False (claims loss). realizedPnl > 0.
                          Contradiction inside the same row. _is_win is buggy.
Severity:                 high
Confidence:               medium (cannot fully verify without a live API call;
                          the codebase is internally consistent under hypothesis A)
Recommended action:       Resolve empirically: capture a known-NO-resolved
                          /closed-positions response from a live wallet and
                          inspect curPrice. Document the answer in ADR-003 as
                          a clarification (Polymarket API row vs ADR-003's
                          intended convention).
                          Until resolved, **do not let Timing Surface v1 consume
                          curPrice for captured-travel magnitude.** The r1 spec
                          already complies — it uses the universal payout
                          convention (1.0 for win, 0.0 for loss) instead of
                          curPrice.
Block Timing Surface?     no — r1 spec already routes around this surface.
                          But the empirical resolution should be added to the
                          implementation plan as a precondition test (§6).
```

### R3 — `stage1_gates._count_open_dead` may misclassify winning NO positions (HIGH)

```
Issue:                    F1 sloppy-operator caution flag uses a label-agnostic
                          curPrice ≤ 0.01 test on /positions (open) rows.
File / function:          sniper/analysis/wallet_protocol/stage1_gates.py:65-74
Current behavior:         An open position is counted as "effectively-resolved-
                          against" when curPrice ≤ 0.01. The function does NOT
                          inspect the outcome label.
Why it may be wrong:      Under R2 hypothesis A (curPrice = YES probability),
                          this would count:
                            - OPEN YES positions where YES collapsed → losers ✓
                            - OPEN NO positions where YES collapsed → WINNERS ✗
                              (NO won; the wallet has $1/share unredeemed)
                          Under R2 hypothesis B (curPrice = held-token price),
                          curPrice ≤ 0.01 catches losers correctly for YES, NO,
                          and OTHER alike.
Example:                  Wallet has 60 OPEN NO positions, all at curPrice=0.005
                          and avgPrice=0.40. F1 fires "sloppy_operator". If
                          curPrice = YES probability, those positions are
                          unredeemed winners worth $1 each — the gate label
                          "sloppy" might still apply (failed to redeem) but
                          conflating winners with worthless losers misleads
                          the operator.
Severity:                 high
Confidence:               medium (depends on R2 resolution)
Recommended action:       Fix once R2 is resolved. Likely outcome: split the
                          metric into "open_dead_against" (YES collapsed for
                          a YES holder OR NO collapsed for a NO holder) and
                          "open_unredeemed_winners" (the converse). Each has a
                          different operational meaning.
Block Timing Surface?     no — Stage 1 gates are independent of Timing Surface.
```

### R4 — `_is_win` fallback for OTHER outcomes uses 0.5 threshold (MEDIUM)

```
Issue:                    For OTHER (non-yes/no) outcomes, _is_win returns
                          cp ≥ 0.5 — a heuristic that may misclassify multi-
                          outcome categorical markets.
File / function:          held_analyst._is_win:153-154
Current behavior:         "Unknown outcome label: treat curPrice ≥ 0.5 as win."
Why it may be wrong:      For a 5-outcome categorical market (e.g. election with
                          Biden / Harris / Trump / DeSantis / Other), each
                          outcome's curPrice could plausibly resolve to anything
                          in [0, 1]. The "win" condition is curPrice → 1
                          (held outcome resolves), regardless of where the 0.5
                          midpoint sits. A categorical position resolved to a
                          non-winning outcome would have curPrice → 0; resolved
                          to the winning outcome would have curPrice → 1. The
                          0.5 threshold is correct for those endpoints, but
                          mid-market unresolved positions (curPrice in [0.4, 0.6])
                          would be mis-attributed under this fallback.
                          However, _is_win is only consulted on held-to-resolution
                          positions where curPrice is at resolution (0 or 1) per
                          held_analyst.filter_held_to_resolution (lines 115-142),
                          which uses EPSILON_RESOLUTION = 0.01. So the 0.5
                          fallback is never exercised on mid-market values in
                          the documented call path.
Severity:                 medium
Confidence:               high
Recommended action:       No fix needed at v1 scope. Document in held_analyst
                          that _is_win is only valid when curPrice is at
                          resolution. Add a test that asserts the OTHER
                          fallback is never reached except at curPrice ∈ {0, 1}.
Block Timing Surface?     no.
```

### R5 — `last SELL` approximation can hide dust closes (MEDIUM)

```
Issue:                    Timing Surface r1 collapses partial-fill / multi-SELL
                          ladders to the latest SELL by timestamp. If the latest
                          SELL is a small dust close at a non-representative
                          price, the captured-move number is misleading.
File / function:          Timing Surface r1 spec §6.1 (proposed); same partial-
                          fill limitation already in Completeness Surface v1.
Current behavior:         "last SELL" = max-by-timestamp matching (cid, outcome).
                          A 1-share dust close at 0.05 after a 99-share main exit
                          at 0.65 would surface −$0.35/share as the "captured
                          move" instead of +$0.25/share.
Why it may be wrong:      For wallets that close in tranches and leave a small
                          dust position, the latest-by-timestamp rule mis-
                          represents the meaningful exit price.
Example:                  Wallet bought 100 NO shares at 0.40. Closed 99 shares
                          at 0.65, then 1 dust share at 0.05 (perhaps a forced
                          maker exit). Last SELL = 0.05. Reported captured-move
                          = 0.05 − 0.40 = −0.35, rendered as a "loss cut"
                          even though realizedPnl is +$24.95 (clear win).
                          The realizedPnl-based attribution category would
                          correctly say `pre_resolution_profitable_exit`, but
                          the captured-move number shown alongside contradicts
                          the sign.
Severity:                 medium
Confidence:               high (algorithm is exactly as specified; question
                          is materiality)
Recommended action:       Two options for Timing Surface v1:
                            (a) Replace latest-SELL with size-weighted average
                                SELL: sum(price_i × size_i) / sum(size_i) over
                                all SELLs for the (cid, outcome). Single test
                                fixture lock.
                            (b) Keep latest-SELL but render the captured-move
                                ONLY when the latest SELL covers ≥ 50% of the
                                position size; otherwise render "—" with a
                                "size-weighted exit not yet computed" note.
                          (a) is cheap and avoids the dust-close pitfall.
                          (b) is safer-by-default but more complex to render.
Recommendation:           **Switch to size-weighted average SELL (option a).**
                          Costs ~5 lines and is provably more representative
                          than max-by-timestamp.
Block Timing Surface?     no, but apply (a) before implementation.
```

### R6 — Open-mark accounting tie inherits curPrice ambiguity (LOW)

```
Issue:                    analyze_wallet._compute_accounting_tie reads curPrice
                          on /positions rows for "open mark" parity. If R2 is
                          unresolved, this number's coordinate is also ambiguous.
File / function:          analyze_wallet.py:76-85
Current behavior:         open_value = sum(curPrice * size for p in open_positions)
                          unrealized_pnl = sum((curPrice - avgPrice) * size for p ...)
Why it may be wrong:      Same as R2. If curPrice on OPEN positions is YES
                          probability and avgPrice is held-token price, then
                          unrealized_pnl is computed from mismatched coordinates
                          for NO positions. open_value would also misrepresent
                          NO holdings.
Severity:                 low (display-only diagnostic; not a verdict input)
Confidence:               medium (depends on R2 resolution)
Recommended action:       Resolve with R2; fix in same patch.
Block Timing Surface?     no.
```

### R7 — OTHER bucket polarity (LOW)

```
Issue:                    The Edge Surface OTHER bucket aggregates closed
                          positions by their non-yes/no outcome label, but
                          does not currently attempt any polarity inference
                          (which is correct). However, downstream Timing Surface
                          interpretation might over-extend this.
File / function:          analyze_wallet._other_bucket_detail; report.py
                          OTHER bucket render.
Current behavior:         No polarity inference — pure aggregation.
Why it may be wrong:      Not wrong today. Risk: a future contributor adds
                          captured-travel display for OTHER without verifying
                          curPrice's coordinate for categorical outcomes.
                          Polymarket's behavior for multi-outcome curPrice is
                          unverified in this codebase.
Severity:                 low (not wrong today; risk is forward-looking)
Confidence:               high
Recommended action:       Document in Timing Surface implementation that the
                          held-bucket captured-travel formula uses the universal
                          payout convention (1.0 / 0.0) which is correct for any
                          outcome (binary or categorical) — keeping us safe
                          regardless of curPrice's actual coordinate.
Block Timing Surface?     no.
```

---

## 5. Unsafe / ambiguous assumptions (summary)

These do not block Timing Surface v1 but should be documented:

1. **`closed_positions[].curPrice` coordinate** — **RESOLVED 2026-05-06: Hypothesis B (held-token price uniformly) confirmed empirically.** ADR-003 is correct as written. `_is_win`'s NO branch and affected tests were fixed in R9 (`b78a644`). (Original entry: empirically unverified for binary YES/NO outcomes; codebase internally consistent under hypothesis A but ADR-003 implies B.)
2. **FH6 side-adjustment** — **RESOLVED in R1** (`20a1888`). The NO-side flip was removed before any v1.5 strict repricing work.
3. **F1 sloppy-operator metric** — **RESOLVED in R3/R9.4 as no-fix-needed.** Under held-token semantics, `curPrice <= 0.01` identifies dead-against open positions across YES/NO/OTHER.
4. **Latest-SELL approximation** in any future captured-move display can be distorted by dust closes (R5). Switch to size-weighted average SELL.

---

## 6. Recommended pre-implementation tests

These are minimum oracle locks that should land **as part of** Timing Surface v1's test suite (or as a separate harness if it lands first). They cost very little and prevent the same NO-coordinate defect class from re-emerging.

### T1 — NO-token held-to-resolution captured-travel sign
Lock that the universal-payout formula gives the correct sign on NO held wins / losses.

```python
def test_no_held_win_captured_travel_is_positive():
    # NO bought at 0.40, NO won → universal payout: 1.0 - 0.40 = +0.60
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 0.0,
           "realizedPnl": 60.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.60)

def test_no_held_loss_captured_travel_is_negative():
    pos = {"outcome": "No", "avgPrice": 0.40, "curPrice": 1.0,
           "realizedPnl": -40.0, "size": 100, "conditionId": "cid_a"}
    travel = held_resolution_travel(pos, "held_to_resolution_loss")
    assert travel == pytest.approx(-0.40)
```

### T2 — NO-token pre-resolution captured-move sign
Lock that long-token rule (no side adjustment) is correct for NO sells.

```python
def test_no_pre_res_profit_captured_move_is_positive():
    # NO bought at 0.40, sold at 0.60 → +0.20 favorable
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.60, "timestamp": 2000, "size": 100, "asset": "tok_no"},
    ]
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 20.0}
    move = last_sell_move(pos, activity)
    assert move == pytest.approx(0.20)
```

### T3 — Size-weighted SELL average (if R5 (a) is adopted)
Lock that a dust SELL after a main exit doesn't dominate the captured-move.

```python
def test_dust_sell_does_not_dominate_size_weighted_average():
    activity = [
        {"side": "BUY",  "conditionId": "cid_a", "outcome": "No",
         "price": 0.40, "timestamp": 1000, "size": 100, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.65, "timestamp": 2000, "size": 99, "asset": "tok_no"},
        {"side": "SELL", "conditionId": "cid_a", "outcome": "No",
         "price": 0.05, "timestamp": 3000, "size": 1,  "asset": "tok_no"},
    ]
    pos = {"conditionId": "cid_a", "outcome": "No",
           "avgPrice": 0.40, "realizedPnl": 24.95}
    # Size-weighted average = (0.65*99 + 0.05*1) / 100 = 0.644
    move = last_sell_move(pos, activity)  # using size-weighted variant
    assert move == pytest.approx(0.244, abs=1e-3)
```

### T4 — OTHER (categorical) captured-travel sign
Lock universal-payout formula on an OTHER outcome label.

```python
def test_other_outcome_held_win_captured_travel_is_positive():
    # Trump bought at 0.30, Trump wins → 1.0 - 0.30 = +0.70
    pos = {"outcome": "Trump", "avgPrice": 0.30, "curPrice": 1.0,
           "realizedPnl": 70.0, "size": 100, "conditionId": "cid_x"}
    travel = held_resolution_travel(pos, "held_to_resolution_win")
    assert travel == pytest.approx(0.70)
```

### T5 — Verdict invariance across coordinate hypotheses (paranoia)
Same wallet data with curPrice flipped (1.0 ↔ 0.0 for held positions), realizedPnl preserved → Timing Surface classification, n_attributed, and captured-travel signs unchanged. This proves the surface is independent of curPrice's coordinate (it should be — r1 only uses curPrice indirectly via exit_attribution for category determination, and exit_attribution's category determination uses realizedPnl sign, not curPrice magnitude).

```python
def test_timing_surface_invariant_under_curprice_coordinate_flip():
    # Build a held-to-resolution position set, then build a "flipped" twin
    # where curPrice=1.0 ↔ curPrice=0.0 on every position. realizedPnl is
    # preserved (it's the source of truth). n_unmapped_fills, activity,
    # everything else is identical.
    # Expectation: Timing Surface dict is identical between runs.
```

---

## 7. Decision and proceed/wait recommendation

**Proceed with Timing Surface v1 r1 implementation, with one required change and tests T1–T5 added as preconditions.**

**Required change to r1 spec:**
- Apply R5 fix (a): change "last SELL" to **size-weighted average SELL** across all SELLs for (cid, outcome) within the position's lifetime. Update §6.1 and §9 accordingly. Cost: ~5 lines + 1 test.

**Reasons proceed is safe:**
- R1 (FH6) is orthogonal: Timing Surface v1 does not call `price_history_fn`.
- R2 (curPrice) is routed around: r1 uses universal payout (1.0/0.0) for held-bucket captured-travel, never reads curPrice for magnitude.
- R3 (F1 gate) is orthogonal: Stage 1 gates run before Timing Surface and don't share code.
- R4 (OTHER fallback) is filtered out by held_analyst.filter_held_to_resolution; Timing Surface inherits the same filter via exit_attribution.
- R6, R7 are LOW and orthogonal.
- R5 has a cheap fix (size-weighted SELL) that should land with v1.

**Post-audit followup status (2026-05-06):**
- R1 fixed FH6 NO-side side adjustment before v1.5 enables `price_history_fn` injection.
- R2 empirically resolved `curPrice` as held-token price uniformly.
- R3 closed as no-fix-needed under R2 held-token semantics.
- R8 fixed `walk_full_history` propagation in exit attribution.
- R9.1-R9.4 fixed `_is_win`, inverted affected tests, audited fixtures, and closed R3.
- R9.5 closed historical wallet-report re-analysis: regenerated the SwissMiss
  informational baseline from fixture, updated the SwissMiss dossier,
  stale-bannered the affected historical audit/dossier docs, inspected the
  medium-risk unknown dossiers, and skipped DB/brain storage work because no
  persisted analyzer rows existed.

**Updates to Timing Surface v1 r1 spec required:**
1. §6.1: replace "latest SELL" with "size-weighted average SELL".
2. §6: rename `avg_last_sell_move_*` → `avg_sw_sell_move_*` (sw = size-weighted) to match.
3. §9: add T1, T2, T3 (size-weighted variant), T4, T5 to the test matrix.
4. §11 cross-reference this audit document.

If reviewer accepts these updates, the implementation can resume after a second commit applying them.

---

## 8. Files reviewed

- `sniper/analysis/wallet_protocol/__init__.py`
- `sniper/analysis/wallet_protocol/analyze_wallet.py`
- `sniper/analysis/wallet_protocol/capital_ledger.py`
- `sniper/analysis/wallet_protocol/exit_attribution.py`
- `sniper/analysis/wallet_protocol/fetch.py`
- `sniper/analysis/wallet_protocol/flip_reconstruction.py`
- `sniper/analysis/wallet_protocol/flipper.py` (FH6 only — full scope deferred)
- `sniper/analysis/wallet_protocol/held_analyst.py` (top + H6 region)
- `sniper/analysis/wallet_protocol/price_history_adapter.py`
- `sniper/analysis/wallet_protocol/report.py`
- `sniper/analysis/wallet_protocol/stage1_gates.py`
- `sniper/analysis/wallet_protocol/subgraph_reader.py`
- `sniper/analysis/wallet_protocol/types.py`
- `sniper/markets/polymarket_api.py` (closed-positions + activity endpoints)
- `tests/test_wallet_protocol_completeness_surface.py`
- `tests/test_wallet_protocol_held_analyst.py` (selected tests)
- `tests/test_wallet_protocol_exit_attribution.py` (selected tests)
- `tests/test_wallet_protocol_edge_surface.py` (selected tests)
- `CLAUDE.md` (ADR-003, ADR-004, ADR-005)

## 9. Files NOT reviewed in this pass (deferred)

These were out of scope for the immediate Timing Surface v1 decision but are recommended for a follow-up audit:

- `flipper.py` outside FH6 (FH1–FH5, stationarity, time-split)
- `held_analyst.py` lines 200–1500 (H1–H5, H7, BH pool assembly)
- `independence.py` (cluster reductions)
- `tag_inference.py` (tag-grain enumeration)
- `statistics.py` (bootstrap / permutation / CUSUM)
- All ledger/* materializer code
- All paper-trading and copy infrastructure outside `analysis/wallet_protocol/`
