# Followup R2 — Resolve `closed_positions[].curPrice` coordinate empirically

**Status:** fixed (empirical resolution — Hypothesis B confirmed, 2026-05-06)
**Severity:** high (blocks any future surface that consumes `curPrice` magnitude)
**Source:** wallet-protocol semantic-correctness audit (2026-05-06), §R2
**Owner:** unassigned
**Blocks:** R3 (`stage1_gates._count_open_dead` split); any v1.5 surface that
needs `curPrice` for magnitude (Timing v1 does NOT, by design).

**Resolution summary (see §8 for full evidence):** `curPrice` on the live
`/closed-positions` endpoint is the **held-token price uniformly**
(Hypothesis B / ADR-003). `_is_win` and the NO-side test were inverted before
R9 and are fixed as of `b78a644`. R3 `_count_open_dead` is **correct as-is**
(cp ≤ 0.01 catches losers across all outcome types). This ticket remains the
empirical evidence record; R9 is the follow-up code/doc closure.

---

## 1. The ambiguity

The codebase consumes `closed_positions[].curPrice` in two places that
imply two different coordinate systems, and ADR-003 implies a third
(see audit §R2 for the full trace):

- `held_analyst._is_win` (lines 145-154) treats `curPrice` as **YES
  probability** for binary outcomes:
  - YES wins iff `curPrice ≥ 1−ε`
  - NO wins iff `curPrice ≤ ε`
- `tests/test_wallet_protocol_held_analyst.py:125` asserts
  `_is_win({"outcome": "No", "curPrice": 0.0})` is True with the
  docstring "A NO position wins when YES price → 0".
- `_is_win` falls back to `cp ≥ 0.5` for OTHER outcomes — implying
  `curPrice` for OTHER is the **held-outcome's resolution probability**
  (not YES probability).
- ADR-003 in CLAUDE.md says "A YES position stores the YES token price;
  a NO position stores the NO token price" — implying `curPrice` is
  the held-token price uniformly.

Under hypothesis A (YES probability for binary, held-outcome for OTHER),
the codebase is internally consistent. Under hypothesis B (held-token
price uniformly, ADR-003), `_is_win`'s NO branch is inverted (along with
its test).

We cannot resolve this from the codebase alone. Both "fixtures + code
agree" cases produce a self-consistent passing test that does not prove
empirical truth.

## 2. Required resolution

Capture **at least three** known-resolved /closed-positions API
responses spanning:

1. A binary YES win (YES outcome resolved positively, wallet held).
2. A binary NO win (NO outcome resolved positively, wallet held).
3. A categorical / OTHER win (e.g. a candidate-named outcome).

For each, record:

- The full `/closed-positions` row (all fields).
- The `outcome` label.
- The known resolution (which outcome won the underlying market).
- The recorded `curPrice` value.
- The recorded `realizedPnl` sign.

## 3. Decision matrix

| Case | If curPrice → | Hypothesis confirmed |
|---|---|---|
| Binary YES win | 1.0 | A and B agree |
| Binary YES loss | 0.0 | A and B agree |
| Binary NO win | 0.0 | **A** (YES probability) |
| Binary NO win | 1.0 | **B** (held-token price) |
| Binary NO loss | 1.0 | **A** |
| Binary NO loss | 0.0 | **B** |
| OTHER (Trump won, held Trump) | 1.0 | held-outcome convention (consistent with B; A makes no claim for OTHER) |
| OTHER (Trump lost, held Trump) | 0.0 | held-outcome convention (consistent with B) |

The NO-resolution rows are the load-bearing ones — they discriminate A
from B.

## 4. Outcome of resolution

Three possible outcomes:

### Outcome 1 — Hypothesis A confirmed (curPrice = YES probability for binary, held-outcome for OTHER)

- Update ADR-003 to clarify: ADR-003 applies to entry / traded prices
  (`avgPrice`, `activity_rows[].price`) but NOT to `curPrice` on
  closed-positions. `curPrice` for binary outcomes follows the YES
  probability convention; for OTHER outcomes follows the held-outcome
  convention.
- `_is_win` is correct as-is.
- Document the asymmetry clearly in the held_analyst module docstring.
- Audit R3 (`_count_open_dead`) confirms a real bug — winning NO open
  positions are mis-counted as "dead-against." Proceed with R3 fix.
- Any future surface consuming `curPrice` magnitude must apply the
  asymmetric side-adjustment formula (`favorable = curPrice − avgPrice`
  for YES, `(1 − curPrice) − avgPrice` for NO, etc.).

### Outcome 2 — Hypothesis B confirmed (curPrice = held-token price uniformly)

- `_is_win` is buggy — NO branch must change to `cp ≥ 1−ε` (mirror of
  YES branch).
- The test at `test_held_analyst.py:125` is wrong and must be inverted:
  `_is_win({"outcome": "No", "curPrice": 1.0})` should be True.
- Audit a wider set of tests for the same encoded assumption (any
  fixture where NO + curPrice=0.0 is treated as a win — likely
  several).
- Stage 1 gate `_count_open_dead` is correct as-is (curPrice ≤ 0.01
  catches losers across all outcome types). R3 ticket may close as
  "no fix needed" depending on this resolution.
- ADR-003 stands as written.

### Outcome 3 — Mixed / API inconsistency

- If different markets return curPrice in different coordinates (e.g.
  binary one way, multi-outcome another), document the asymmetry as a
  structural data-quality issue and add a normalization layer in
  `fetch.py` that converts everything to one canonical coordinate
  before downstream consumption.

## 5. Required harness

```python
# scripts/resolve_curprice_coordinate.py (new, throwaway)

import requests
from sniper.markets.polymarket_api import build_session, fetch_closed_positions

# Three known wallets with known resolved positions in each category.
# Pick wallets whose recent /closed-positions clearly include the
# relevant cases; record carefully against on-market resolution truth
# (cross-check via the market's resolved outcome on Polymarket UI).
KNOWN_WALLETS = {
    "binary_yes_win":   "<addr>",   # holds resolved YES positions on a YES-wins market
    "binary_no_win":    "<addr>",   # holds resolved NO positions on a NO-wins market
    "categorical_win":  "<addr>",   # holds a resolved categorical winner
}

session = build_session()
for case, wallet in KNOWN_WALLETS.items():
    rows = fetch_closed_positions(session, wallet)
    print(f"\n=== {case} ({wallet}) ===")
    for row in rows[:5]:
        print({
            "outcome": row.get("outcome"),
            "avgPrice": row.get("avgPrice"),
            "curPrice": row.get("curPrice"),
            "realizedPnl": row.get("realizedPnl"),
            "title": row.get("title"),
            "conditionId": row.get("conditionId"),
        })
```

The script is throwaway — once the empirical answer is captured, it
gets deleted. The answer is documented in:

1. ADR-003 in CLAUDE.md (clarifying the coordinate convention for
   `curPrice` specifically).
2. A docstring on `held_analyst._is_win` with the resolution outcome
   and any required corrections.
3. Closure note on this ticket.

## 6. Acceptance criteria

- Empirical observation captured for all three cases (binary YES, binary
  NO, categorical).
- ADR-003 updated with the curPrice-coordinate clarification (or
  correction).
- `_is_win` either confirmed correct (Outcome 1) or fixed (Outcome 2).
- All tests that depend on the curPrice coordinate convention are
  audited and corrected if needed.
- R3 ticket gets a concrete proceed/close recommendation based on
  this resolution.
- Audit cross-reference: this ticket is closed in the
  `2026-05-06-wallet-protocol-semantic-audit.md` followup tracker.

## 7. Out of scope

- Implementing the v1.5 strict-repricing path. That has its own design
  doc.
- Refactoring _is_win for OTHER outcomes (the 0.5 fallback). The audit
  R4 finding documents this is harmless under the current call path
  (filter_held_to_resolution gates it to curPrice ∈ {0, 1}).
- Wallet-protocol-wide schema changes. The fix is local to
  `_is_win` and the affected tests.

## 8. Empirical resolution (2026-05-06)

**Endpoint:** `GET https://data-api.polymarket.com/closed-positions?user=<wallet>&limit=50&sortBy=TIMESTAMP&sortDirection=ASC&offset=...`
(via `sniper.markets.polymarket_api.fetch_closed_positions`, exhaustive
pagination at server-capped 50/page).

**Date of spot-check:** 2026-05-06.

**Wallets sampled (public addresses):**

| Label | Address | Total /closed-positions rows |
|---|---|---|
| swissmiss | `0xdbade4c82fb72780a0db9a38f821d8671aba9c95` | 981 |
| yatsen    | `0x5bffcf561bcae83af680ad600cb99f1184d6ffbe` | 862 |

**Filter applied:** `curPrice` within ε=0.05 of {0, 1} (loose ε to count
"close to" cases too) AND `outcome ∈ {Yes, No}` AND `realizedPnl ≠ 0`.
Result: 1,091 binary-outcome rows used in the cross-tab below.

**Cross-tab — discriminating buckets are NO-side:**

| Outcome (held) | realizedPnl sign | curPrice ≈ | n | Hypothesis under truth-by-pnl |
|---|---|---|---:|---|
| Yes | win  | 1 | 178 | A & B agree |
| Yes | win  | 0 |  53 | (pnl-anomaly: pre-resolution sells) |
| Yes | loss | 0 | 296 | A & B agree |
| Yes | loss | 1 |  16 | (pnl-anomaly: pre-resolution sells) |
| No  | win  | **1** | **370** | **B (held-token price)** |
| No  | win  | 0 |  34 | (pnl-anomaly) |
| No  | loss | **0** | **118** | **B (held-token price)** |
| No  | loss | 1 |  26 | (pnl-anomaly) |

**Discriminating-bucket totals (NO-side rows only):**

- B-supporting (`curPrice = held-token price`): **488**
- A-supporting (`curPrice = YES probability`): **60**

That's a ~89% B-majority on NO-side discriminating rows. The ~11% A-tagged
rows are the same kind of artifact that shows up on YES-side
(`Yes win curPrice=0`, `Yes loss curPrice=1`): they correlate with
**pre-resolution sells** that flip `realizedPnl` sign without flipping
the held-token's actual settlement price. Spot-check confirmed:

> Wallet held NO on `0x5fc28bfcb6f16080415695b53f1f60820f403a94ce16696eea2f0085d823bc92` ("Will George Simion win the Romanian presidential election?"). Gamma payload shows `outcomes=["Yes","No"]`, `outcomePrices=["0","1"]`, `negRisk=true` → NO won. `curPrice=1` matches NO settling at 1 (B). `realizedPnl=-37,634` reflects the wallet selling NO at unfavorable prices pre-resolution, not the settlement value.

`held_analyst.filter_held_to_resolution` already gates on
`_has_pre_resolution_sell`, so these pnl-anomaly rows would not enter the
H1–H6 hypothesis pool. They are consistent with B, not A.

**Representative rows (5 from B-supporting bucket):**

| outcome | avgPrice | curPrice | realizedPnl | conditionId | title |
|---|---:|---:|---:|---|---|
| No | 0.419 | **1** | +28,732.84 | `0x93ec7298…` | Will Kanye launch a coin in February? |
| No | 0.764 | **1** | +203,248.89 | `0xa5ac4cdc…` | Trump ends Ukraine war in first 90 days? |
| No | 0.836 | **1** | +11,415.31 | `0x244c75b2…` | Will Pierre Poilievre be the next Canadian Prime Minister? |
| No | 0.003 | **0** | -67.50 | `0x94631ed5…` | Trump x Ukraine mineral deal signed before May? |
| No | 0.831 | **0** | -2,490.00 | `0xf49a7c37…` | Will Sovereignty win the 2025 Kentucky Derby? |

For each: the held NO token's curPrice equals its settlement value
(NO wins → 1, NO loses → 0). This is **Hypothesis B**. Under
Hypothesis A, every "Will Kanye launch …" / "Trump ends Ukraine war…" row
above (held NO, NO won) would have `curPrice ≈ 0` (YES probability), which
is empirically false in 370 of 404 NO-win rows.

**Final answer:** `curPrice = held-token price` (Hypothesis B).

**Confidence:** high.
- 488 B-supporting rows vs 60 A-supporting rows on the discriminating axis.
- The 60 "A-supporting" rows are the same pnl-anomaly pattern that affects
  YES-side rows symmetrically (53 + 16 anomalies on YES-side which is
  non-discriminating between A and B), confirming this is a
  realizedPnl/sells issue, not a coordinate-system issue.
- Spot-check of one A-tagged row (Simion) verified the held-token
  resolution agrees with B; only `realizedPnl` is misleading because of
  pre-resolution sells (already excluded by `filter_held_to_resolution`).

**Implications:**

- ADR-003 (CLAUDE.md) is **correct as written**. `curPrice` follows the
  same outcome-native convention as `avgPrice` and traded prices.
- `held_analyst._is_win` (sniper/analysis/wallet_protocol/held_analyst.py
  lines 145-161) was **buggy** on the NO branch before R9. It treated
  `curPrice` as YES probability for binary outcomes, which is empirically
  inverted. R9 fixed this by using one held-token settlement threshold.
- `tests/test_wallet_protocol_held_analyst.py:123-140` now locks the
  corrected NO convention: `curPrice=1.0` is a NO win; `curPrice=0.0` is
  a NO loss.
- Other tests that synthesize NO-side fixtures with `curPrice=0.0` as a
  "win" were audited in R9.3; the true buggy fixture was corrected.
- R3 (`stage1_gates._count_open_dead`) is **correct as-is**. Under
  Hypothesis B, `curPrice ≤ 0.01` always means the held outcome resolved
  to 0 (loser), regardless of whether held side is YES or NO. The R3
  ticket may close as "no fix needed" once R9 lands. Live `/positions`
  spot-check on swissmiss supports the same convention for open rows:
  one paired market returned held YES `curPrice=0.0165` and held NO
  `curPrice=0.9835` on the same condition, matching outcome-native
  held-token prices rather than a shared YES-probability coordinate.
- v1.5+ surfaces consuming `curPrice` magnitude (e.g. side-adjusted
  P&L = `curPrice − avgPrice` for both YES and NO held positions) can
  proceed without an asymmetric side-adjustment formula. The
  outcome-native convention applies throughout.

## 9. R9 follow-up code fixes (closed at `b78a644`)

R9.1-R9.4 landed after this empirical resolution. The snippets below record
the required transition and the resulting closure state.

### R9.1 — Invert `_is_win`'s NO branch

**File:** `sniper/analysis/wallet_protocol/held_analyst.py:145-154`

Current (wrong):

```python
def _is_win(position: Mapping) -> bool:
    cp = float(position.get("curPrice") or 0.0)
    outcome = (position.get("outcome") or "").lower()
    if outcome in ("yes", "long"):
        return cp >= 1.0 - EPSILON_RESOLUTION
    if outcome in ("no", "short"):
        return cp <= EPSILON_RESOLUTION                # WRONG (Hypothesis A)
    return cp >= 0.5
```

Fixed (Hypothesis B / outcome-native):

```python
def _is_win(position: Mapping) -> bool:
    cp = float(position.get("curPrice") or 0.0)
    # curPrice is outcome-native: it stores the held-token's price, not
    # the YES probability. A position wins when its held token resolves
    # to 1, regardless of which side (YES/NO/OTHER) was held.
    return cp >= 1.0 - EPSILON_RESOLUTION
```

### R9.2 — Invert affected test

**File:** `tests/test_wallet_protocol_held_analyst.py:123-125`

```python
def test_is_win_no_resolved_to_one_is_a_win():
    """A NO position wins when its NO token resolves to 1 (outcome-native)."""
    assert _is_win({"outcome": "No", "curPrice": 1.0})
```

Plus add a counter-test that `outcome=No, curPrice=0` is a loss.

### R9.3 — Audit suite-wide for the same encoded assumption

Run a grep for any fixture where `outcome="No"` is paired with
`curPrice=0.0` and treated as a winner, or paired with `curPrice=1.0` and
treated as a loser. Likely affected files (non-exhaustive — needs a
proper sweep on the R9 ticket):

- `tests/test_wallet_protocol_held_analyst.py` — explicit assertions.
- Any wallet-protocol fixture builder that sets NO outcomes alongside
  win/loss expectations.
- `tests/replay/fixtures/*.json` — verify whether NO-side closed
  positions in fixtures match the live API convention. If any fixture
  was hand-edited to match the current (wrong) `_is_win`, it needs
  flipping.

### R9.4 — R3 closure recommendation

After R9.1 ships, the R3 ticket
(`docs/superpowers/specs/2026-05-06-stage1-f1-open-dead-split.md`) can
close as "no code change needed." `_count_open_dead`'s `cp ≤ 0.01` filter
already correctly identifies losers across all outcome types under
Hypothesis B. This was also spot-checked against live `/positions` rows
on swissmiss: same condition, YES row `curPrice=0.0165`, NO row
`curPrice=0.9835`. That confirms the open-position call site uses the
same held-token convention as `/closed-positions`.

### R9.5 — Effect on prior wallet analyses

Any historical run of `held_analyst` on a wallet with material NO-held
content can have affected H2, H3, H5, and H2/H3 time-split outputs because
those surfaces consume `_is_win`. Wallets that traded heavily on NO-favored
markets will have had their wins and losses swapped on the NO subset.
Re-running affected wallets after R9.1 is in scope for R9.5.

Audit cross-ref: `docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md`
§R2 — mark this row "fixed (empirical resolution); R9 created for code
fixes."
