# Analyzer Consumer Layer — design (Signals + wallet CRUD + UI)

**Status:** updated 2026-06-20 after removing the cohort-following paper surface. This spec consumes the analyzer full-read engine (`wallet_analysis_reads` / `operational_route` / `evidence_profile`) and exposes read-only wallet intelligence.

> **Goal:** show analyzer-backed open wallet positions with explainable context, let the operator pin or remove wallets, and provide a path to author a named Strategy. The consumer layer does not create paper positions by favoriting a wallet.

## Locked Decisions

1. **Signals = read-only hypotheses.** Analyzer-detected wallet positions appear by lane with the read's reason and caveats.
2. **Favorite = pin + re-analysis priority only.** It does not create strategy state, paper positions, or any hidden validation book.
3. **Fresh accounts stay warned.** Thin-history wallets may surface in a fresh watch lane, but they carry explicit survivorship/out-of-sample-validation caveats and no proven-edge claim.
4. **Strategy validation is explicit.** To test a thesis, author a named Strategy in the Strategies tab and let that strategy's paper/live lifecycle own the results.

## Units

### C1 — Wallet Flags + CRUD

`sniper/persistence/db_wallet_flags.py`, `sniper/routes/api_wallet_flags.py`

`wallet_flags`: `wallet` PK, `favorite_ts`, `deleted_ts`, `updated_ts`.

- `POST /api/wallet-analyzer/favorite` sets `favorite_ts`, restores soft-deleted wallets, and enqueues a boosted re-analysis.
- `DELETE /api/wallet-analyzer/favorite/<wallet>` clears favorite.
- `DELETE /api/wallet-analyzer/wallet/<wallet>` soft-deletes and hides the wallet.
- `POST /api/wallet-analyzer/restore/<wallet>` restores a soft-deleted wallet.

### C2 — Signals Board

`sniper/routes/api_live_signals.py`

Join `wallet_analysis_latest` with `live_positions`. Each row includes wallet, market, outcome, lane, primary verdict, operational route, headline, edge-domain match, warnings, and favorite state.

Lanes:

- `directional_edge`: established/emerging directional reads.
- `fresh_watch`: thin-history reads with explicit warning.
- `algo`: structural/non-directional reads.
- `other`: favorited unsupported verdicts.

Deleted wallets are excluded. Favorites pin to the top.

### C3 — UI

`static/app.js`, `templates/index.html`

- Signals renders lane sections and wallet links that open Analyzer detail.
- Analyzer renders pinned wallet cards, live/closed position tabs, and plain-English caveats.
- Favorite/delete actions stop propagation inside clickable cards.
- No old score, hidden validation book, copied-bet badge, or copied-bet route appears in the UI.

## Data Flow

`analyzer worker -> wallet_analysis_reads(latest) -> live_positions join -> Signals / Analyzer`.

Operator favorite only pins and re-queues analysis. Derived strategies are created explicitly in the Strategies tab.

## Honesty / Doctrine

No copy advice, expected PnL, sizing, or automatic paper entry from wallet favoriting. Consumers do not mutate analyzer verdicts.

## Testing

- C1: flags CRUD, idempotent favorite, soft-delete/restore, boosted queue.
- C2: lane assignment, deleted exclusion, favorites pinned, edge-domain match, no score fields.
- C3: `node --check`, no inline handlers, no removed surface strings, endpoint-shape tests.
