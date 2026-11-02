# Polymarket Crypto-5m Paper Simulator — Design (Phase 0: Capture Spine)

**Date:** 2026-06-20
**Status:** building (autonomous, operator-directed via GPT Pro / Codex / Claude tandem)
**Branch:** `feat/polymarket-crypto5m-capture-v0` (isolated worktree from `origin/main` @ `d0c45f1`)
**Scope of THIS phase:** capture-only infrastructure. No live trader, no paper PnL, no strategy claim.

---

## Thesis correction (locked — read before touching any module)

The 2026-06-18 BTC5m experiment ([[project-btc5m-algo]], spec `2026-06-18-btc5m-algo-design.md`)
traded **spot/perpetual direction** and paid **exchange trading costs**. It reached an honest null
(5m crypto spot mean-reversion is efficient under realistic execution). **That null does not transfer
here, because the instrument is different.**

This system studies a **Polymarket binary**:

- Polymarket Up/Down **outcome tokens** priced in `[0, 1]`,
- **Chainlink-defined** start/end settlement (BTC/USD data stream; Up wins when end ≥ start),
- payout of **$1 or $0** per share,
- **Polymarket orderbook depth** and **Polymarket taker fees**.

Binance is a **feature and timing proxy only**. Binance is **NOT**:
- the settlement source,
- the Price to Beat,
- the training label,
- the economic PnL truth.

The model will eventually estimate **`P(Polymarket Up wins under the Chainlink market rule | data available at decision time)`** and compare it to the Polymarket-implied price. Phase 0 builds none of that — it builds the data spine and proves the data is trustworthy.

---

## Empirical findings (validated 2026-06-20, n=50 markets; fixtures in `tests/fixtures/crypto5m/`)

These drive the data model and the fail-closed gates. Sources: gamma `series_id=10684`, CLOB `/book`, Polymarket WS docs, two independent settlement-rule confirmations.

1. **`eventMetadata.priceToBeat` is populated ONLY at/after resolution — Gamma is the post-hoc *validation oracle*, not the live source.** Present + float in 40/40 resolved markets; `None` in every active window (4 consecutive, all `acceptingOrders=true`). **The live Price-to-Beat is reconstructed from the Chainlink RTDS stream** (see next section) and validated against Gamma's later `priceToBeat`. Never derive the threshold from Binance. (The earlier "priceToBeat unknowable live" framing was too strong — corrected per GPT Pro.)
2. **Window timestamps deterministic:** `window_start_ts = int(slug.rsplit("-",1)[1])`, `window_end_ts = market.endDate`; `end - start == 300s` in 50/50. The per-market `startDate` is **listing** time, not window start — do not use it for the window.
3. **Identity 100%:** `conditionId` present; exactly 2 `clobTokenIds` ordered `[Up, Down]` matching `outcomes == ["Up","Down"]`; `resolutionSource` is the Chainlink BTC/USD stream. Winner from `outcomePrices`: `["1","0"]`=Up, `["0","1"]`=Down.
4. **Fees:** `feesEnabled=true`, `feeType=crypto_fees_v2`, `takerBaseFee=1000` (raw bps-like integer — **capture raw, interpret in the taker-sim phase, do not hardcode**). WS `last_trade_price` carries realized `fee_rate_bps`.
5. **Tick size varies per market** (`0.01` vs `0.001`); `orderMinSize=5`. Capture per market; never assume.
6. **Book-ordering trap:** REST `/book` returns bids **ascending** / asks **descending** (best = last element); the WS `book` doc states bids highest-first / asks lowest-first. **They disagree.** Never trust array position — build a price→size map and compute `best_bid = max(prices)`, `best_ask = min(prices)`.
7. **Chainlink RTDS is live + no-auth (verified 2026-06-20).** `wss://ws-live-data.polymarket.com`, subscribe `topic=crypto_prices_chainlink`, `filters={"symbol":"btc/usd"}`, PING every 5s. Updates carry `full_accuracy_value` (price × 10¹⁸ integer string → exact `Decimal`, e.g. `"63598411047494102500000"` → `63598.4110474941025`), `value` (float convenience), `payload.timestamp` (Chainlink source ms, 1s-aligned), and an outer server `timestamp` (sub-second). ~1s cadence. This is the live Price-to-Beat source.

---

## Price-to-Beat acquisition (the authoritative-reference lane)

The Chainlink start reference is the threshold the whole instrument turns on. We do **not** guess it and do **not** take it from Binance. Four quality levels, fail-closed:

| Quality | Source | Use |
|---|---|---|
| `exact_chainlink_report` | authenticated Chainlink Data Streams signed report for the start ts (deferred — design the resolver to accept it later) | best; out of v0 core |
| `validated_rtds_reconstruction` | RTDS tick selected by the **empirically-locked boundary rule** | **the practical v0 source** |
| `proxy_only` | Binance (or other exchange) px at the boundary | research / basis only — **never** a paper decision |
| `unavailable` | no trustworthy boundary observation | skip decision; `reason=missing_authoritative_price_to_beat` |

**The boundary rule is unknown and must be calibrated, not guessed.** We know `window_start_ts`; we do not yet know which Chainlink observation Polymarket uses as the open. Candidate rules: `last tick with source_ts ≤ start`, `first tick with source_ts ≥ start`, `nearest tick`. Calibration experiment (≥100 resolved windows): capture RTDS ticks `[start−10s, start+10s]` (raw JSON + `Decimal(full_accuracy_value)` + source/server/recv ts), compute each candidate, then compare against Gamma's populated `priceToBeat`; measure exact-match rate, abs/max error, source delay, missing-window rate, rule stability. **Lock a rule only if one matches consistently** (gate: ≥99% match at a predeclared precision on usable windows); quarantine mismatches.

Because RTDS source data can arrive a few seconds late, the authoritative open may only be *known* shortly after the window begins. Record `price_to_beat_source_ts`, `price_to_beat_receive_ts`, `price_to_beat_known_latency_ms`; a paper decision may not run before the reference is known. Use `Decimal` end-to-end and preserve raw payloads (float display loses precision).

**Two evaluation lanes, never mixed:** Lane A = validated Chainlink reference (decisions); Lane B = Binance proxy (research-only basis/disagreement).

## Three independent gates (do not conflate — Codex review)

1. **`rule_supported`** (`rule_contract`): the market matches the contract we understand — `outcomes==("Up","Down")`, **exact** Chainlink-source allowlist (not `startswith`; `btc-usd-v2` must fail), `>=`-semantics rule text, 5-min window, **exactly two distinct** token ids, `conditionId` present, **series identity** (`series.id==10684` / accepted `seriesSlug`/tag — a direct slug fetch must not accept a look-alike). priceToBeat is **not** a rule property.
2. **`capture_ready`** (`market_resolver`, market-state): the live book is capturable **now** — `active && !closed && acceptingOrders && enableOrderBook && window_end_ts > now`. (The `event_live.json` fixture is `enableOrderBook=false`/old — `rule_supported` but **not** `capture_ready`.)
3. **price-to-beat quality** (`price_to_beat_resolver`): the four levels above.

---

## Architecture — `sniper/algos/polymarket_crypto5m/` (isolated from the spot backtest)

Small, single-responsibility, mostly-pure units. Network/WS I/O is thin and injected so logic is unit-testable from recorded fixtures (no test touches the live API).

| Unit | File | Responsibility |
|---|---|---|
| Models | `models.py` | Frozen dataclasses: `MarketSpec`, `RuleVerdict`, `Settlement`, `ReferenceObservation`, `CaptureGap`, `BookLevel`. Enums: `RuleReason`, `QualityFlag`, `PriceToBeatQuality`. Pure. (The mutable book class with update logic lives in `book_state.py`.) |
| Rule contract | `rule_contract.py` | Deterministic `rule_supported` validator → `RuleVerdict`. Versioned. **Exact** Chainlink-source allowlist, exactly-2-distinct-tokens, conditionId required, series-identity. `outcome_for(start,end)`: `end>=start ⇒ Up`. Reason taxonomy below. |
| Market resolver | `market_resolver.py` | Gamma pagination (`series_id=10684`); pure `parse_event(raw)→(MarketSpec, RuleVerdict)`; **series-identity gate**; `capture_ready(raw,now)` market-state gate (active/!closed/acceptingOrders/enableOrderBook/window-not-ended). Thin fetch injected. |
| Chainlink RTDS | `chainlink_rtds.py` | RTDS collector for `crypto_prices_chainlink` btc/usd. Pure `parse_frame(raw)→[ChainlinkTick]` (snapshot `data[]` + `update`), `Decimal(full_accuracy_value)/10**18`, source/server/recv ts. Thin WS loop injected (PING/5s). |
| Price-to-Beat | `price_to_beat_resolver.py` | Candidate boundary rules (`last≤start`/`first≥start`/`nearest`) over captured ticks; `PriceToBeatQuality` classification; calibration vs Gamma oracle (match rate / abs+max err / latency / missing). Fail-closed. |
| Book state | `book_state.py` | Deterministic in-memory book **per token**. Applies `book`/`price_change`/`best_bid_ask`/`last_trade_price`/`tick_size_change`. Best from price→size map (`max`/`min`), never array position. Depth retained for a $10/$20 FOK taker-fill calc. No cross-token complement arithmetic. |
| Persistence | `persistence.py` | Dedicated SQLite namespace (configurable path). 9 tables below. Idempotent upserts / append-only event keys. Caller-supplied server-time ints (never local +7h clock). Raw payloads + version lineage. Schema initializer. |
| Capture | `capture.py` | Orchestrates REST-snapshot-init → WS-subscribe(Up,Down) → persist. Gap detection + REST reconcile + reconnect generation. Emits quality flags. |
| Settlement | `settlement.py` | Winner from `market_resolved` WS event, reconciled against gamma/CLOB resolved fields. Stores winning token/outcome/ts/`outcomePrices`/`umaResolutionStatus`/source/reconciliation. Does **not** require exact Chainlink end value. |
| Basis audit | `basis_audit.py` | `binance_chainlink_settlement_proxy_audit`: pairs `priceToBeat` + Binance start/checkpoints/end + Polymarket winner. Computes Binance-vs-winner disagreement by threshold-distance and seconds-to-close. Explicitly **not** Chainlink reconstruction. |
| Entry point | `scripts/run_polymarket_crypto5m_capture.py` | Capture CLI. Read-only public endpoints; local SQLite; no order/key flags. |

### Rule-contract reason taxonomy
`supported`, `unsupported_rule`, `unsupported_source`, `malformed_window`, `missing_token_mapping`, `unexpected_outcome_shape`, `missing_condition_id`, `wrong_series`. Persist `RULE_CONTRACT_VERSION`; a rule/source change creates a new version or quarantines the market. (priceToBeat is **not** a rule reason — it lives in the price-to-beat quality lane.)

### Capture quality flags
`book_stale`, `websocket_gap`, `rest_recovery`, `timestamp_regression`, `malformed_update`, `token_unmapped`, `rule_changed`, `market_closed`, `capture_incomplete`. An uncertain book is never treated as executable truth.

## Persistence schema (`crypto5m_*`, dedicated DB; **never** `paper_trades`)

| Table | Grain | Notes |
|---|---|---|
| `crypto5m_markets` | one row per market (condition_id) | identity, window ts, token ids, fee/tick/minsize, priceToBeat (nullable until resolution), raw gamma payload, resolver/version lineage |
| `crypto5m_market_rules` | one row per (condition_id, rule_text_hash) | raw rule text, hash, resolution_source, verdict, rule_contract_version |
| `crypto5m_book_events` | append-only per WS/REST event | token_id, condition_id, event_type, raw payload, exchange/recv/persist ts, reconnect_generation, quality flags |
| `crypto5m_book_snapshots` | periodic/checkpoint book state per token | bids/asks depth json, best_bid/ask/spread, hash, tick, min_size, source_event, quality flags |
| `crypto5m_chainlink_ticks` | append-only per RTDS tick | symbol, source_ts (ms), server_ts, recv_ts, value (float), full_accuracy_value (text, exact), raw payload, window_start_ts (nullable bucket) |
| `crypto5m_price_to_beat` | one row per (condition_id, candidate_rule) | reconstructed Decimal value, source_ts, recv_ts, known_latency_ms, quality level, gamma_oracle (nullable until resolution), abs_error_vs_oracle (nullable), match flag |
| `crypto5m_reference_observations` | per (condition_id, source, checkpoint) | Binance proxy px at start/mid/end (Lane B research only) |
| `crypto5m_settlements` | one row per resolved condition_id | winning token/outcome, resolution_ts, umaResolutionStatus, outcomePrices, source_used, reconciliation_status, gamma priceToBeat oracle |
| `crypto5m_capture_gaps` | append-only | condition_id, window, gap_kind, detected_ts, recovery action, reconnect_generation |

Requirements: idempotent upserts or append-only keys; UTC; raw source payload where useful; analyzer/code version + source lineage on every row; configurable local DB path; schema initializer covered by tests.

## Phase boundary

- **Phase 0 (this build):** resolver + rule contract + persistence + book state + WS/REST capture + gap recovery + settlement + basis-audit **inputs** + CLI + read-only live smoke + report. **No paper fill, no PnL, no edge claim.**
- **Phase 1 (next, separate):** **taker** paper simulator (depth-aware FOK fills on captured books, real fee config, decision-time information set). Gated behind the Phase-0 data-quality gates.
- **Deferred:** maker simulation (separate, harder — fill-probability model).

### Phase-0 gates before any paper simulator
≥100 resolved BTC 5m windows compared; ≥95% valid token mapping; ≥95% usable dual-book coverage at planned checkpoints; no unexplained capture gaps; deterministic settlement for resolved windows; **the selected RTDS boundary rule matches Gamma `priceToBeat` at a predeclared precision for ≥99% of usable windows**, with every mismatch explained/quarantined; missing-boundary rate measured; Price-to-Beat arrival latency measured; Binance-vs-Chainlink disagreement measured; **paper decisions cannot run without a validated Chainlink reference**; **no live-order capability exists anywhere in the package.**

## First operational research deliverable

> How often would a **Binance-based** Up/Down direction label disagree with the **actual Chainlink-settled** Polymarket winner — overall, and especially near the threshold / in the last seconds? That disagreement rate is the hard floor on any Binance-driven edge.

## Safety / scope guards
No live orders. No private keys. No authenticated/user WS channels or order endpoints. No production DB writes (local SQLite only, even in smoke). No service install/restart. No expected-PnL or sizing in any user-facing output. Binance never used as settlement truth.

## Out of scope (Phase 0)
Taker/maker paper fills, PnL, strategy signals, multi-asset (BTC only; ETH/SOL architecturally allowed later via the `asset` field), the app UI tab, real-time priceToBeat acquisition (a Phase-1 blocker, documented).
