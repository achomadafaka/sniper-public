# Followup R3 — Split Stage 1 F1 metric: `dead-against` vs `unredeemed-winners`

**Status:** closed (no fix needed; resolved by R2/R9.1)
**Severity:** high (depends on R2 resolution) — moot under R2 Outcome 2
**Source:** wallet-protocol semantic-correctness audit (2026-05-06), §R3
**Owner:** unassigned
**Depends on:** R2 (`closed_positions[].curPrice` empirical resolution)

---

## 1. The bug

`sniper/analysis/wallet_protocol/stage1_gates.py:65-74`:

```python
def _count_open_dead(data: WalletData, threshold: float = 0.01) -> int:
    """Open positions with curPrice <= threshold (effectively-resolved-against)."""
    n = 0
    for p in data.open_positions:
        cp = p.get("curPrice")
        if cp is None:
            continue
        if float(cp) <= threshold:
            n += 1
    return n
```

The function is **label-agnostic**: it counts any open position with
`curPrice ≤ 0.01` as "effectively-resolved-against" without inspecting
the outcome.

## 2. Why it's potentially wrong

Under audit R2 hypothesis A (`curPrice` = YES probability for binary
outcomes), this function would count both:

- OPEN YES positions where YES probability collapsed → losers ✓
- OPEN NO positions where YES probability collapsed → **winners** (NO
  won, $1/share unredeemed) ✗

Conflating unredeemed winners with worthless losers gives a misleading
F1 "sloppy_operator" caution flag. The wallet may have material
unredeemed winnings sitting in open positions, which is a different
operational meaning from a wallet that left worthless losers
unredeemed.

Under R2 hypothesis B (curPrice = held-token price uniformly), the
function is correct as written — `curPrice ≤ 0.01` catches losers across
all outcome types.

**This ticket cannot be resolved until R2 is resolved empirically.**

## 3. Required fix (Outcome 1: R2 → curPrice = YES probability for binary)

Split the metric into two label-aware functions:

```python
def _count_open_dead_against(data: WalletData, eps: float = 0.01) -> int:
    """Open positions effectively resolved against the wallet (worthless)."""
    n = 0
    for p in data.open_positions:
        cp = p.get("curPrice")
        if cp is None:
            continue
        outcome = (p.get("outcome") or "").lower()
        cp_f = float(cp)
        if outcome in ("yes", "long"):
            if cp_f <= eps:                        # YES collapsed → YES-holder loser
                n += 1
        elif outcome in ("no", "short"):
            if cp_f >= 1.0 - eps:                  # YES probability → 1 → NO-holder loser
                n += 1
        else:
            # Categorical / OTHER: treat held-outcome's price ≤ eps as resolved-against.
            if cp_f <= eps:
                n += 1
    return n


def _count_open_unredeemed_winners(data: WalletData, eps: float = 0.01) -> int:
    """Open positions effectively resolved IN FAVOR but unredeemed."""
    n = 0
    for p in data.open_positions:
        cp = p.get("curPrice")
        if cp is None:
            continue
        outcome = (p.get("outcome") or "").lower()
        cp_f = float(cp)
        if outcome in ("yes", "long"):
            if cp_f >= 1.0 - eps:                  # YES → 1 → YES-holder winner
                n += 1
        elif outcome in ("no", "short"):
            if cp_f <= eps:                        # YES → 0 → NO-holder winner
                n += 1
        else:
            if cp_f >= 1.0 - eps:                  # categorical winning outcome
                n += 1
    return n
```

Update `evaluate_stage1_gates()` to surface BOTH counts and split F1:

```python
F1_DEAD_AGAINST_RATIO_THRESHOLD = 0.50
F1_UNREDEEMED_WINNER_THRESHOLD = 10            # different operational meaning

n_open_dead_against = _count_open_dead_against(data)
n_open_unredeemed   = _count_open_unredeemed_winners(data)
n_open = len(data.open_positions)

if n_open_dead_against > N_OPEN_DEAD_F1_MIN and n_open > 0:
    ratio = n_open_dead_against / n_open
    if ratio > F1_DEAD_AGAINST_RATIO_THRESHOLD:
        caution.append(
            f"F1_dead_against_unredeemed (n={n_open_dead_against}, ratio={ratio:.2f})"
        )

if n_open_unredeemed >= F1_UNREDEEMED_WINNER_THRESHOLD:
    caution.append(
        f"F1_unredeemed_winners (n={n_open_unredeemed})"
    )
```

The two metrics have different operational meanings:
- `F1_dead_against_unredeemed`: housekeeping issue (wallet holds many
  worthless losing positions; clutter, not money).
- `F1_unredeemed_winners`: **money issue** (wallet has unredeemed $1/share
  winning positions sitting in open state — actual unrealized payouts
  the operator can claim).

`GateEvaluation.n_open_dead` should also split into
`n_open_dead_against` + `n_open_unredeemed_winners`. The existing
`n_open_dead` field name is back-compat-ambiguous; either rename
both new fields or keep `n_open_dead = n_open_dead_against` to preserve
the existing semantic and add `n_open_unredeemed_winners` as a new
field.

## 4. Required fix (Outcome 2: R2 → curPrice = held-token price uniformly)

The existing function is correct. Close this ticket as "no fix needed"
with a docstring update on `_count_open_dead` clarifying that under the
held-token-price convention, `curPrice ≤ 0.01` catches losers
universally regardless of outcome label.

## 5. Required regression tests

```python
def test_count_open_dead_against_counts_yes_losers():
    """OPEN YES position with curPrice → 0 → dead-against (loser)."""
    data = _wallet_data(open_positions=[
        {"outcome": "Yes", "curPrice": 0.005}
    ])
    assert _count_open_dead_against(data) == 1
    assert _count_open_unredeemed_winners(data) == 0


def test_count_open_dead_against_counts_no_losers():
    """OPEN NO position with curPrice → 1 (YES probability) → dead-against (loser)."""
    data = _wallet_data(open_positions=[
        {"outcome": "No", "curPrice": 0.995}
    ])
    assert _count_open_dead_against(data) == 1
    assert _count_open_unredeemed_winners(data) == 0


def test_count_open_unredeemed_winners_counts_no_winners():
    """OPEN NO position with curPrice → 0 (YES probability) → unredeemed winner."""
    data = _wallet_data(open_positions=[
        {"outcome": "No", "curPrice": 0.005, "size": 100}
    ])
    assert _count_open_dead_against(data) == 0
    assert _count_open_unredeemed_winners(data) == 1


def test_count_open_unredeemed_winners_counts_yes_winners():
    """OPEN YES position with curPrice → 1 → unredeemed winner."""
    data = _wallet_data(open_positions=[
        {"outcome": "Yes", "curPrice": 0.995, "size": 100}
    ])
    assert _count_open_dead_against(data) == 0
    assert _count_open_unredeemed_winners(data) == 1
```

These tests assume R2 Outcome 1 (curPrice = YES probability for binary).
Under R2 Outcome 2, the test fixtures (and `_is_win`) are inverted; the
right tests would change accordingly.

## 6. Acceptance criteria

- R2 is resolved first (this ticket explicitly waits).
- Under R2 Outcome 1: split functions land, F1 caution string splits,
  regression tests above pass, full suite green.
- Under R2 Outcome 2: ticket closes as "no fix needed" with a
  documentation update to `_count_open_dead` docstring.
- Audit cross-reference: this ticket is closed in the
  `2026-05-06-wallet-protocol-semantic-audit.md` followup tracker.

## 7. Out of scope

- Touching F2/F3/F4 caution flags. This ticket is strictly the F1
  open-dead split.
- Changing the F1 threshold values (`N_OPEN_DEAD_F1_MIN`,
  `F1_OPEN_DEAD_RATIO_THRESHOLD`). Keep existing semantics for
  `dead_against`; the unredeemed-winners metric gets its own threshold.
- Refactoring the rest of `evaluate_stage1_gates()`.

## 8. Resolution (2026-05-06)

R2 resolved empirically as **Outcome 2** (Hypothesis B): `curPrice` is
the held-token price uniformly across all outcome types
(`docs/superpowers/specs/2026-05-06-curprice-coordinate-empirical-resolution.md`,
commit `cba241e`). Under this convention, `_count_open_dead`'s
`cp ≤ 0.01` filter correctly catches losers universally — held tokens
that resolved to ~0 are worthless regardless of which side (YES/NO/
categorical) the wallet held.

**Decision: close as "no fix needed" per §4.**

Per R9.4: docstring on `_count_open_dead`
(`sniper/analysis/wallet_protocol/stage1_gates.py:65`) updated to
explain the held-token-price convention and cite the R2 / R9 commits.
The F1 caution flag remains useful as-is (it catches dead-against held
tokens correctly under held-token semantics).

The split into `dead_against` vs `unredeemed_winners` proposed in §3 is
unnecessary — it was conditional on R2 Outcome 1 (Hypothesis A), which
the empirical data refuted (488 B-supporting vs 60 A-supporting on
NO-side discriminating rows; A-tagged rows are pre-resolution-sell
pnl-anomaly artifacts).

R9.1 commit (the related code fix that makes this closure stable):
inverts `_is_win`'s NO branch in `held_analyst.py`. See R9 commit SHA
in the R2 doc / repo history.

Audit cross-reference: `docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md`
§R3 — mark this row "closed (no fix needed); resolved by R2 + R9".
