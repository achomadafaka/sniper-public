# Polymarket crypto5m — Phase 1B: Feature Completion, Fee Truth, and Training-Dataset Readiness

**Status:** spec (commit #1 of the Phase 1B sequence)
**Branch:** `feat/polymarket-crypto5m-phase1b-feature-completion`
**Supersedes nothing.** Extends the Phase 0 / 0.5 capture spine and the Phase 1A shadow simulator
(`docs/superpowers/specs/2026-06-20-polymarket-crypto5m-phase1a-shadow-simulator-design.md`,
`docs/superpowers/specs/2026-06-20-crypto5m-phase05-calibration-integrity-design.md`).

## Goal

Complete the known-at-decision feature matrix, resolve the per-market fee source-of-truth, harden real
`market_resolved` settlement parsing, and produce a **deterministic, leakage-audited training dataset**
suitable for later walk-forward probability modeling — without training a model, activating paper
entries, or loosening any fail-closed gate.

## Hard scope guards (binding — copied from the Phase 1B directive)

- **Do NOT** begin calibrated-model training. Dataset creation, coverage reports, diagnostic baseline
  scoring, leakage tests, fee reconciliation, and learning-curve infrastructure only.
- **Do NOT** activate paper entries. `price_to_beat_resolver.LOCKED_BOUNDARY_RULE` stays `None`; every
  decision remains `blocked_decision` / `shadow_prediction` / `paper_no_trade`.
- **Do NOT** loosen any fail-closed gate (the 10 gates enumerated in the Phase 1A/0.5 specs and
  `decision.py`). New code adds gates; it never removes one.
- **Binance is a predictor feature only** — never settlement truth, never Price-to-Beat, never the label.
  The label is the final Polymarket winning token from `crypto5m_settlements` (`up_won` 1/0).
- No live orders, no private keys, no authenticated/user WS channels, no production DB writes (local
  SQLite only), no service install/restart. A **bounded read-only** live smoke (fee payloads, Binance
  observations, basis, a real `market_resolved` event if one arrives) is in scope and explicitly
  authorized by the directive.
- The 200-window dataset is enough to calibrate/hold-out the **Chainlink boundary-selection rule**. It is
  **not** asserted to be enough to train a multi-feature probability model. Keep collecting past 200; use
  learning curves later to judge adequacy. No edge claim from the boundary-calibration sample.

## Current-state grounding (verified against source 2026-06-20)

What already exists (so Phase 1B is additive, not greenfield):

| Concern | Module | Status |
|---|---|---|
| Fee resolver (CLOB + observed, fail-closed) | `sniper/algos/polymarket_crypto5m/fee_model.py` | exists; `decode_fee`/`parse_clob_market_fee`/`fetch_market_fee`/`is_known`/`cost_per_share` |
| Settlement parse + reconcile | `sniper/algos/polymarket_crypto5m/settlement.py` | exists; `from_gamma`/`from_ws_resolved`/`reconcile` |
| Decision-time feature engine | `sniper/algos/polymarket_crypto5m/features.py` | exists; `build_feature_snapshot`, `FEATURE_VERSION="fv0"`, families A/B/E/F |
| Chainlink RTDS ingest | `sniper/algos/polymarket_crypto5m/chainlink_rtds.py` | exists; `ChainlinkTick` (price-only) |
| Basis (research audit) | `sniper/algos/polymarket_crypto5m/basis_audit.py` | exists; pre-computed start/end, no source-age/skew, not a feature |
| Executable price / taker fill | `sniper/algos/polymarket_crypto5m/taker_sim.py` | exists; `estimate_taker_buy` already emits fee-inclusive cost / slippage / levels — called once at 10 USDC, not persisted as features |
| Book state | `sniper/algos/polymarket_crypto5m/book_state.py` | exists; `taker_buy_fill(usdc=...)` |
| Capture/sim persistence | `persistence.py` (capture) + `sim_persistence.py` (Phase 1A) | exists; full `crypto5m_*` schema |
| Continuous collector | `scripts/run_polymarket_crypto5m_capture.py --continuous --max-windows N` | exists |

What is **missing** and is Phase 1B's work:

- No `fee_source` precedence layer; no WS `new_market.fee_schedule` parse; no `fee_exponent` / `taker_only`
  / `rounding_precision` / structured `raw_source_payloads`.
- No live Binance ingestion at all (only an hourly-kline backfill into a *separate* `price_history` table,
  and the research-only `basis_audit`). RTDS Chainlink is the sole live feed feeding features.
- No Binance-reframed features (returns/vol/volume/VWAP/EMA/trend), no basis-as-feature with
  source-age/skew/quality, no executable-price features persisted into the snapshot.
- No feature registry (only a global `FEATURE_VERSION` string).
- No dataset exporter / Parquet writer / manifest / hash; no leakage+readiness report.
- `from_ws_resolved` does not validate `condition_id`; no real `market_resolved` fixture.

## Load-bearing design decisions

### D-1. Fee vocabulary is preserved, not rewritten (non-breaking)

`decode_fee()` returns `fee_status ∈ {known, fee_unknown, fee_conflict, fee_stale}`, consumed by
`fee_model.is_known()` and `decision.py` Gate 4. **These stay exactly as-is** (11 fee tests depend on
them). Phase 1B adds a *new* reconciliation function returning the directive's richer shape; it reuses
`decode_fee` for the core agree/conflict logic and maps to the directive's `fee_status` vocab:

| `decode_fee` status | Phase 1B `fee_status` | Meaning |
|---|---|---|
| `known` | `known` | usable; `is_known()` still True |
| `fee_conflict` | `conflict` | sources disagree (the 1000-vs-0 finding); blocked |
| `fee_unknown` | `missing` | canonical source present but undecodable / no validating observation; blocked |
| `fee_stale` | `missing` (caveat `stale`) | params too old; blocked |
| (market type unsupported, e.g. negRisk fee descriptor we don't model) | `unsupported` | blocked |

`known` is the only value that admits a (future) paper entry. The new layer never makes a blocked market
pass; it only enriches lineage. `decision.py` continues to gate on `is_known()` → unchanged behavior.

### D-2. `getClobMarketInfo(conditionID).fd` vs current flat fields

The directive's canonical source is `getClobMarketInfo(conditionID).fd` (a fee descriptor with rate /
exponent / taker-only). The current REST `/markets/<condition_id>` payload is parsed for a flat
`taker_base_fee`. The actual shape is an **empirical question** to be answered by the bounded read-only
probe (≥20 live BTC 5m markets + several resolved). The fee-reconciliation parser therefore accepts BOTH
shapes — a nested `fd`/`fee_schedule` descriptor when present, falling back to the flat
`taker_base_fee` — and records which it found in `raw_source_payloads`. No raw integer (`1000`) or
`fee_rate_bps` is ever interpreted as a rate without a documented descriptor or an agreeing observation.

### D-3. Settlement WS parsing stays "provisional" until a real fixture lands

`from_ws_resolved` gains `condition_id` validation (reject wrong-market messages) and forward-field
preservation; tests cover field-ordering, missing-winner, condition/token mismatch, and conflict
quarantine using a recorded fixture. Until a **real** captured BTC 5m `market_resolved` event exists,
the settlement WS lane is reported as `provisional` (a persisted flag, not just a comment). The capture
path records any real event verbatim to `tests/fixtures/crypto5m/market_resolved_real.json` when one
arrives during the read-only smoke; the parser is marked `verified` only after a real fixture passes.

### D-4. Binance is a SEPARATE direct market-data adapter

RTDS supplies Binance **price only** (symbol/timestamp/value) — no volume, no VWAP inputs. Volume/VWAP
features require a separate direct Binance adapter (trade/aggTrade or 1-second kline). Volume/VWAP/flow
features are emitted **only** when a real volume-bearing observation exists; they are never fabricated
from price updates. Binance observations persist to a new `crypto5m_binance_observations` table and are
consumed strictly as predictors with a hard `source_ts <= feature_cutoff_ts` / `recv_ts <= decision_ts`
cutoff. Legacy categorical TREND/REVERSION/LONG/SHORT may remain only as a single diagnostic field
`legacy_spot_signal_diagnostic`; continuous numeric fields are the load-bearing inputs.

### D-5. Parquet via duckdb (no new dependency)

`duckdb>=1.0` is already a dependency and writes Parquet via `COPY (SELECT …) TO '…' (FORMAT PARQUET)`
and reads via `read_parquet(...)`. The dataset exporter is built on duckdb over the capture SQLite DB —
no pandas/pyarrow added. Determinism: fixed column order + `ORDER BY condition_id, checkpoint`, a manifest
JSON (row count, eligibility breakdown, feature versions, source lineage, build config), and a content
hash over the canonical-ordered rows. No imputation; null reasons persisted.

### D-6. Schema additions (additive `ensure_*`, never destructive)

New/extended SQLite tables (created/altered via additive `ensure_*` in `sim_persistence.py`; existing
columns untouched). Column names below are the proposed contract.

<!-- schema:external=crypto5m_fee_configs -->
```sql
-- EXTEND crypto5m_fee_configs (existing) with reconciliation lineage:
ALTER TABLE crypto5m_fee_configs ADD COLUMN fee_source TEXT;            -- clob_market_info|websocket_fee_schedule|reconciled|unavailable
ALTER TABLE crypto5m_fee_configs ADD COLUMN fee_exponent REAL;
ALTER TABLE crypto5m_fee_configs ADD COLUMN taker_only INTEGER;        -- 0/1/NULL
ALTER TABLE crypto5m_fee_configs ADD COLUMN rounding_precision INTEGER;-- 5
ALTER TABLE crypto5m_fee_configs ADD COLUMN raw_source_payloads TEXT;  -- JSON {clob,ws,gamma,last_trade}
```

<!-- schema:external=crypto5m_binance_observations -->
```sql
CREATE TABLE IF NOT EXISTS crypto5m_binance_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT UNIQUE,            -- symbol|source_event_ts|delivery_source
    symbol TEXT,
    source_event_ts INTEGER,         -- Binance event time (ms)
    receive_ts INTEGER,              -- our recv (s)
    price REAL,
    quantity REAL,                   -- NULL unless a volume-bearing source
    quote_quantity REAL,             -- NULL unless available
    trade_count INTEGER,             -- NULL unless available
    delivery_source TEXT,            -- rtds_price|binance_aggtrade|binance_kline_1s
    raw_payload TEXT,
    persist_ts INTEGER
);
```

<!-- schema:external=crypto5m_feature_registry -->
```sql
CREATE TABLE IF NOT EXISTS crypto5m_feature_registry (
    feature_name TEXT,
    feature_family TEXT,             -- A|B|C|D|E|F|executable
    feature_version TEXT,
    required_sources TEXT,           -- JSON list
    minimum_history TEXT,
    time_scope TEXT,                 -- known_at_decision|post_resolution_label|diagnostic_only
    null_reason_vocab TEXT,          -- JSON list
    decision_safe INTEGER,           -- 0/1
    PRIMARY KEY (feature_name, feature_version)
);
```

The training dataset is **exported** (Parquet + manifest), not stored as a new SQLite table; it reads the
existing `crypto5m_feature_snapshots`, `crypto5m_settlements`, `crypto5m_book_reconciliation`,
`crypto5m_price_to_beat`, and the extended `crypto5m_fee_configs`.

## Deliverables → commit sequence (checklist)

- [x] **#1 — this spec** (`8dd5f9b`).
- [x] **#2 — Fee-source reconciliation** (`c6393b8`). CLOB→WS→Gamma precedence; raw 1000 never originates a rate; NaN/exponent≠1 fail closed (review fix `410e785`).
- [x] **#3 — `market_resolved` parser hardening + provisional flag** (`3db37e6`; single-market identity fix `410e785`). Real fixture still pending a live capture (parser reports `provisional`).
- [x] **#4 — Binance raw-observation adapter + persistence** (`f4c0334`; dedup-collision/partial-kline fixes `410e785`). RTDS price-only, no volume fabrication.
- [x] **#5 — Feature family C** (`d432e22`).
- [x] **#6 — Feature family D** (`059e824`; point/series consistency fix `ff14ef0`).
- [x] **#7 — Executable-price features** (`6f235ab`).
- [x] **#8 — Feature registry + availability accounting** (`e89f2ca`).
- [x] **#9 — Deterministic dataset builder** (`70b74f2`; hash-matches-parquet + version-grain fixes `ff14ef0`).
- [x] **#10 — Data-readiness + leakage audit** (`4c6ccc8`; independent eligible-row invariant `ff14ef0`).
- [x] **#11 — Review fixes** (3 adversarial reviewers → `410e785`, `ff14ef0`, `b831daa`; replay wiring `6589d1c`).

Runtime (parallel, not a code commit): **Deliverable 10 collection campaign** — run the continuous
collector to ≥200 usable resolved windows and beyond (learning-curve checkpoints 200/500/1000/2000/4000),
read-only, no orders.

## Known-at-time / leakage rules (must hold in every feature and in the exporter)

- No feature source timestamp after `feature_cutoff_ts`; no `receive_ts` after `decision_ts`.
- Gamma post-resolution `priceToBeat` never seeds a decision-time feature (decision-time Price-to-Beat
  comes from the RTDS reconstruction lane with `price_to_beat_known_ts <= decision_ts`).
- No settlement/winner field in predictors; label sourced only from the Polymarket winner.
- No final-5-minute Binance close used before close; no future book state; no future volume baseline
  (volume z-scores use only history strictly before the decision).
- Any leakage violation **fails the dataset build** (does not warn-and-continue).

## Test plan (TDD — red first, per the repo's TDD discipline)

Fee authority · CLOB-info parse · WS fee-schedule parse · source agreement/conflict · missing canonical ·
exact 5-dp rounding · no direct `1000` interpretation · fee-disabled.
Binance features · strict cutoff · multi-horizon returns · vol units · insufficient obs · volume-z uses
prior history only · VWAP = Σ(price·qty)/Σqty · continuous trend/reversion · no final-candle leakage.
Basis · aligned ts · excessive skew → `stale_or_misaligned` · stale Chainlink/Binance · change horizons ·
null/caveat.
Executable · Up/Down independent · stake-specific fillability · multi-level ask walk · fee-inclusive vs
exclusive · unknown fee → null · ask-ordering irrelevant.
Dataset builder · one row per market/checkpoint · deterministic ordering + hash · correct label · conflict
exclusion · null reasons preserved · no post-decision source ts · no post-resolution leakage · replay
reproduces identical dataset.
Real fixture · captured `market_resolved` parses · winner→token · unknown fields preserved · Gamma
agreement · conflict quarantine · condition_id mismatch rejected.

## Verification

Focused: `python3 -m pytest tests/test_crypto5m_*.py -q`.
Then: `python3 scripts/validate_schema_refs.py` · `python3 scripts/brain_index.py generate` ·
`python3 -m pytest -q`.
Bounded read-only/local-DB live smoke for current fee payloads, Binance observations, Chainlink/Binance
basis, and a real `market_resolved` event if one arrives. No orders, keys, production DB, or services.

## Final report fields (per the directive)

branch/worktree · commits + files · test results · fee source-of-truth finding · whether 1000-vs-0 is
resolved · real `market_resolved` fixture status · feature-family coverage · one example snapshot with
known-at-time lineage · one deterministic dataset row · leakage-audit result · model-eligible window
count · 200-window collector progress · blockers before model calibration · confirmation that paper
entries remain impossible.
