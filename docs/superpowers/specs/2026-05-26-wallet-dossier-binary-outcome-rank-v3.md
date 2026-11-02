# Wallet Dossier — Binary Outcome Rank v3

**Date:** 2026-05-26
**Status:** implementation slice
**Supersedes:** §7.3 favorite/longshot deferral for binary conditions in `2026-05-25-wallet-dossier-semantics-v0.md`
**Schema version:** bumps `DOSSIER_SCHEMA_VERSION` from `"v2_market_structure"` to `"v3_binary_outcome_rank"`

## 0. Why This Exists

After `v2_market_structure`, many named sports/team rows correctly become `market_type="binary"`. They still fall into `outcome_role="unknown"` because v0 only recognized binary labels `"yes"` and `"no"`.

That is false uncertainty. A binary condition with outcome label `"Canucks"` is a selected named binary outcome, not an unknown role. For binary CTF conditions, entry price also gives a defensible favorite/underdog proxy because the sibling token price is `1 - entry_price`.

This slice resolves those binary-only semantics. Multi-outcome rank still requires sibling token prices and remains out of scope.

## 1. Scope

### In Scope

- Add outcome role `binary_named_outcome`.
- For `market_type="binary"`:
  - labels in `{yes, true}` -> `binary_yes`
  - labels in `{no, false}` -> `binary_no`
  - any other non-empty label -> `binary_named_outcome`
  - empty label -> `unknown`
- Add binary favorite/rank derivation from `entry_price`:
  - `entry_price >= 0.50` -> `favorite_status="favorite"`, `outcome_role_rank=1`
  - `0.25 <= entry_price < 0.50` -> `favorite_status="underdog"`, `outcome_role_rank=2`
  - `entry_price < 0.25` -> `favorite_status="longshot"`, `outcome_role_rank=2`
- Use `favorite_status_source = outcome_role_rank_source = "binary_entry_price_complement"` when derived.
- Make `sibling_token_prices_at_entry_not_fetched` count-based and emit it only for rows where rank/favorite remain unavailable.

### Out of Scope

- Sibling prices for 3+ outcome conditions.
- Full favorite rank among event-level sibling markets.
- Event family/subtype.
- Market-selection baselines.
- Strategy Pattern Audit ingestion.
- Simulation or paper validation.

## 2. Derivation Rules

### 2.1 Outcome Role

```text
if market_type == "binary" and lower(outcome_label) in {"yes", "true"}:
  outcome_role = "binary_yes"
elif market_type == "binary" and lower(outcome_label) in {"no", "false"}:
  outcome_role = "binary_no"
elif market_type == "binary" and outcome_label is non-empty:
  outcome_role = "binary_named_outcome"
elif market_type == "multi_outcome":
  outcome_role = "multi_outcome_selected"
else:
  outcome_role = "unknown"
```

`binary_named_outcome` is not an accounting bucket and must not be collapsed into `OTHER`.

### 2.2 Favorite Status And Rank

Only binary conditions are eligible.

```text
if entry_price is missing:
  favorite_status = "unknown"
  favorite_status_source = "entry_price_missing"
  outcome_role_rank = NULL
  outcome_role_rank_source = "entry_price_missing"

elif market_type != "binary":
  favorite_status = "unknown"
  favorite_status_source = "sibling_token_prices_at_entry_not_fetched"
  outcome_role_rank = NULL
  outcome_role_rank_source = "sibling_token_prices_at_entry_not_fetched"

elif entry_price >= 0.50:
  favorite_status = "favorite"
  outcome_role_rank = 1
  source = "binary_entry_price_complement"

elif entry_price < 0.25:
  favorite_status = "longshot"
  outcome_role_rank = 2
  source = "binary_entry_price_complement"

else:
  favorite_status = "underdog"
  outcome_role_rank = 2
  source = "binary_entry_price_complement"
```

The `0.50` tie follows the existing null-baseline surface convention: classify as favorite for determinism. At exactly 0.50, the favorite-vs-underdog comparison is symmetric.

## 3. Data Gap Rule

`sibling_token_prices_at_entry_not_fetched` is no longer blanket-emitted for every position.

Emit only when at least one `positions.outcome_role_rank` is NULL. `affected_grain_count` is the NULL-rank count.

## 4. Acceptance

1. Unit tests cover `binary_named_outcome`, favorite, underdog, longshot, and unavailable cases.
2. Canonical real-shape fixture populates `favorite_status` for all rows and `outcome_role_rank ∈ {1,2}` for all rows.
3. Canonical real-shape fixture no longer emits `sibling_token_prices_at_entry_not_fetched`.
4. Lineage exists for non-null `positions.outcome_role_rank`.
5. Focused dossier suite passes.
6. Schema reference validator passes.
7. No live API calls, DB writes, service restarts, verdict changes, or copy/trade language.

## 5. Carry Forward

This slice only handles binary CTF conditions. The remaining rank work is:

- sibling token prices for multi-outcome or event-level sibling markets
- favorite rank among all event outcomes
- event family/subtype
- market-selection baselines
- cross-wallet Strategy Pattern Audit
- thesis lifecycle and simulation validation
