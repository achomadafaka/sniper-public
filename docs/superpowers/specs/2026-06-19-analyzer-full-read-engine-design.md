# Analyzer Full-Read Engine — design

**Status:** designed 2026-06-19, approved for implementation (operator + Codex adversarial design review incorporated). Scope = the **analyzer sub-project only**. Downstream Signals board, favorite/delete CRUD, strategy authoring, and the Analyzer-tab UI are **separate sub-projects** that consume this engine's output; they are explicitly out of scope here.

> **One-line goal:** every wallet that enters the system gets ONE coherent "full read" that (a) distinguishes a *fresh, information-advantage-shaped* account from a *historically-established* edge wallet, (b) names *what the edge is and where it lives*, and (c) never overclaims — fresh flags are watch-only, statistically honest, and never use the word "insider".

---

## 1. Problem / goal

The current analyzer is good at history-rich wallets — it proves edge with a shuffle/permutation test (`ThesisProfile.shuffle_baseline`) that structurally needs sample size. It is blind to **fresh accounts** that show an information-advantage shape before they have enough resolved bets to clear that test. A `classify_fresh_specialist` classifier exists but is (a) **unwired** (only tests call it) and (b) statistically naive — its core is `qualifies = large_win_same_area >= 1` plus `excess_pp = (win_rate − mean_entry) × 100`, which flags ordinary variance as suspicious (3 wins out of 12 bets at \$0.20 each has P(≥3) ≈ **0.44** under the binomial null — not an anomaly).

We need:
1. **Two engines, one verdict.** A history-rich **thesis engine** (exists) and a rebuilt **fresh-anomaly engine**, fused by a deterministic **synthesis** that produces one coherent primary verdict + an operational route + a preserved evidence profile.
2. **Statistical correctness.** Calibrated-probability surprise (not cheap-win counts), independent-event counting (not bet counts), anytime-valid inference (we re-analyze continuously), online false-discovery control (we scan many wallets), and empirical-Bayes shrinkage.
3. **A universal funnel.** Whatever enters the system — discovery, followed, watchlist, manual, favorite, (later) stream sightings — lands in one analyzer-owned queue and gets a full read; nothing is silently skipped.

## 2. Decisions locked (design review)

- **Becker DB is OFF the critical path.** It is a stale (Feb-2026 cutoff) one-time dump; our own 2026-04-01 calibration study found the market is ≈ calibrated at market level for held-to-resolution bets. The fresh engine needs only per-bet data we already fetch live (`entry_vwap`, `won`, `buy_cost`, `condition_id`, `category`, `event_slug`). v1 baseline probability = **market price at entry**. (If a residual false-positive rate later demands calibration curves, the fresh source is pmxt hourly dumps or a self-rolling table — never the stale Becker dump.)
- **All five fresh-evidence channels ship live in v1** (operator chose the full build): outcome-surprise, timing/CLV, concentration, materiality, coordination/linkage. The coordination channel (on-chain funding graph) and historical-CLV are real data pipelines built in this sub-project.
- **Full statistical rigor in v1:** Poisson-binomial surprise, anytime-valid e-process, online-FDR over the wallet stream, empirical-Bayes shrinkage, event-family clustering, alternative-explanation gate.
- **Verdict model = `primary_verdict` + `operational_route` + `evidence_profile`** (one coherent read, no flattening), not a single enum.
- **Analyzer-owned, immutable persistence.** New tables `wallet_analysis_queue` + `wallet_analysis_reads` (append-only + a latest-read view). **No legacy reuse** (`user_analysis`, `insider_score`, low-buy, `analysis_runs`) and **no downstream routing eligibility fields** in the analyzer — routing is a downstream concern. `wallet_signal_profiles` is NOT written by this sub-project; an adapter/view is added when the Signals sub-project is built.

## 3. Architecture (units, each independently testable)

```
intake (discovery / followed / watchlist / manual / favorite / later: stream)
        │  enqueue (idempotent)
        ▼
[U1] wallet_analysis_queue ──lease──> [U2] worker (rate-limited, fail-loud)
                                          │
            ┌─────────────────────────────┼───────────────────────────────┐
            ▼                              ▼                                ▼
   [U3] thesis engine (exists)   [U4] fresh-anomaly engine v2        [U5] archetype (exists)
   ThesisProfile + shuffle       ex-ante + ex-post, 5 channels,      Layer-1/Layer-2 + confidence
                                 Poisson-binomial e-process,
                                 online-FDR, EB-shrinkage,
                                 event clustering, alt-explanation
            │                              │                                │
            └──────────────┬───────────────┴────────────────────────────────┘
                           ▼
                 [U6] synthesize_read(thesis, fresh, archetype, quality)  ── PURE
                 primary_verdict + operational_route + evidence_profile + headline
                           │
                           ▼
                 [U7] wallet_analysis_reads (immutable) + latest view
```

Supporting data feeds:
- **[F-B] CLV feed** — `sniper/analysis/winning_wallet_thesis/clv.py`, wraps the existing `wallet_protocol/price_history_adapter.build_price_history_fn` (CLOB `/prices-history`, keyed by **CLOB asset id**, mapped from raw activity).
- **[F-E] Funding graph** — `sniper/analysis/winning_wallet_thesis/funding_graph.py`, reuses `conversions.py` Polygon RPC plumbing (`_polygon_rpc_url`, JSON-RPC `eth_getLogs`) over USDC (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`) Transfer logs.

---

## 4. The fresh-anomaly engine v2 (`sniper/analysis/winning_wallet_thesis/fresh_anomaly.py`)

Replaces the cheap-win core of `fresh_specialist.py`. **Pure** (classifies a `PositionBook` + injected feed closures + cohort priors + an online-FDR allocator state; never fetches inside the pure core — feeds are injected). Keeps the existing **hard excludes** (structural paired-outcome, churn/flip) which are correct.

### 4.1 Ex-ante vs ex-post (Codex point 2)

A fresh wallet can look unusual **before** any market resolves (ex-ante, knowable at trade time) or **only after** it wins (ex-post). These are stored separately and never conflated. A no-resolved-history wallet can be routed to **monitoring** from ex-ante evidence alone, but is **never** described as having demonstrated edge.

- `ex_ante_anomaly_level ∈ {none, weak, moderate, strong}`
- `ex_post_surprise_level ∈ {none, weak, moderate, strong}` (requires resolved bets)
- `coordination_level ∈ {none, weak, moderate, strong}`

### 4.2 The five evidence channels

| Channel | Ex-ante/post | Statistic | Data source (v1) |
|---|---|---|---|
| **A — outcome surprise** | ex-post | Poisson-binomial e-process over independent event families (§4.3) | ledger (`entry_vwap` as null p, `won` as outcome) |
| **B — timing / CLV** | ex-ante | closing-line value: did the held side's price move favorably from entry → close; fraction of bets that beat the line, exposure-weighted | [F-B] price-history adapter |
| **C — concentration** | ex-ante | top-area share + HHI (existing `_area` logic) | ledger |
| **D — economic materiality** | ex-ante | absolute USDC size + size relative to the wallet's own book; *size-vs-market-depth* is a flagged `data_gap` in v1 (depth feed not built) | ledger (+ depth gap) |
| **E — coordination / linkage** | ex-ante | shared-funder graph + synchronized entries (same markets within Δt) across linked wallets | [F-E] funding graph |

**Strong fresh flag requires ALL of:** strong evidence in **≥ 2 independent channels**, **no** surviving alternative-structural/latency explanation (§4.6), sufficient data quality, an event-cluster-aware sample, and an **FDR-adjusted** threshold (§4.5). When channels are dark (no feed), the flag degrades conservatively — fewer "strong", more "monitor"/"insufficient". A dark channel is a `data_gap`, never a silent pass.

### 4.3 Channel A — calibrated surprise over independent events (Codex points 3, 4)

1. **Cluster into independent event families.** Group directional episodes by `event_slug` (collapses multiple outcomes of one election / multiple markets of one match / DCA into one opinion). Cross-event correlation (e.g. correlated-but-different-slug geopolitics) is **coarser than ideal in v1** and is emitted as a `data_gap` ("event-level clustering only").
2. **Collapse each family `f` to one Bernoulli trial:** null probability `p_f` = exposure-weighted mean `entry_vwap`; outcome `y_f` = exposure-weighted-majority `won` (ties → fractional, see test matrix). Exposure = `buy_cost`.
3. **Surprise = Poisson-binomial** over the independent `{p_f}`: `expected = Σ p_f`, `observed = Σ y_f`. The 12-bet/3-win example collapses to ≈ 2.4 expected vs 3 observed → not surprising. Three independent \$0.01 wins (Σp = 0.03, observed 3) → extreme.
4. **Effective sample:** `n_independent_events` = number of families; report a Kish-style `effective_sample_size` from the exposure weights.

### 4.4 Anytime-valid inference (Codex point 5a)

The worker re-reads each wallet as new families resolve. A fixed Poisson-binomial p-value is **invalid under optional stopping**. Use a **test supermartingale / e-process** instead:

- Per independent trial: `m_f = 1 + λ·(y_f − p_f)` is a non-negative martingale increment under H₀ (`E[y_f] = p_f` ⇒ `E[m_f] = 1`); keep `λ` in a safe range so `m_f ≥ 0`.
- **E-value** `E = Π_f m_f`. To avoid tuning `λ`, average e-values over a fixed `λ`-grid (method-of-mixtures) — pin the grid in the plan.
- By Ville's inequality, `P(sup_t E ≥ 1/α) ≤ α` — valid under continuous monitoring and optional stopping.
- Stored: `e_value`, `anytime_p = min(1, 1/E)`, plus the (display-only) fixed Poisson-binomial `obs_over_expected` and `p_value` for human intuition (labelled "fixed-sample, not the decision statistic").

### 4.5 Online false-discovery control (Codex point 5b)

We scan thousands of wallets, so an uncorrected threshold will mint "anomalies" by chance. v1 uses a **conservative e-value bound** over the *stream* of wallet e-values: flag-eligible iff `E_t ≥ 1/(α·γ_t)`, `γ_t = 6/(π²t²)` (Σγ ≤ 1), `α` default 0.10. By Markov + `E[E_t|H0] ≤ 1` this bounds the **expected NUMBER of false discoveries ≤ α** — a **PFER** (per-family error rate) bound, which is *stronger* than FDR in the sparse-discovery regime we expect. (True FDR via adaptive e-LOND / Wang–Ramdas 2022 is deferred; the static schedule here intentionally trades recall for precision — the threshold grows fast, so only strong e-values clear it late in a long stream.) Target `α` configurable:
- The allocator persists its sequential state (test index, allocated wealth) in the queue/reads layer so it survives restarts.
- A wallet is `fresh_high_surprise`-eligible on Channel A **only if** its e-value clears the online-FDR threshold at its test index.
- Test matrix includes "thousands of null wallets ⇒ realized FDR ≤ target" and "repeated re-analysis under the null ⇒ false-alert rate controlled".

### 4.6 Alternative-explanation gate (Codex)

Before raising any fresh flag, subtract structural/benign explanations; if one survives as the dominant explanation, the flag is suppressed (and the reason recorded):
- **structural / arb / hedging / market-making** → from archetype (hard excludes already cover paired-outcome + churn).
- **single correlated event** → event-family clustering (a "specialist" that only ever bet one event family has `n_independent_events = 1` → not assessable for surprise).
- **public-truth-stale / official-result latency** → entry timestamp *after* the market's effective resolution/closed time (`resolution_ts` / Gamma closed time) ⇒ latency arbitrage, not foresight. Flagged `alt_explanation = public_truth_stale`.
- **extreme luck** → handled intrinsically by the e-process + online-FDR + EB-shrinkage.

### 4.7 Empirical-Bayes shrinkage (Codex point 5c)

The *displayed* anomaly magnitude (not the flag decision, which the e-process governs) is shrunk toward the population/cohort base rate via a Beta-Binomial empirical-Bayes posterior. Priors come from our existing survivorship cohorts (SwissMiss net-loser / scraper low-var-winner / SmallFish high-var-winner archetype base rates) — most thin extreme results are pulled hard toward the null until evidence accumulates.

### 4.8 Direction ground-truth (Codex microstructure note)

Archetype/direction decisions must use fill/on-chain ground truth, not order-book-inferred maker/taker. The thesis ledger is built from `/activity` fills (on-chain), and optional Goldsky fills (`with_fills`) — both are ground truth. The spec records this as a **verification item** (confirm no load-bearing decision uses order-book-inferred direction).

---

## 5. The synthesis (`synthesize_read`, PURE — `sniper/analysis/winning_wallet_thesis/synthesis.py`)

`synthesize_read(thesis, fresh, archetype, quality) -> WalletRead`. The core "no nonsense" guarantee: two engines in, **one coherent read** out, every precedence collision a golden test.

### 5.1 Output shape

```
WalletRead:
  primary_verdict: str          # one of the 7 buckets below
  operational_route: str        # expedited_review | monitor | strategy_candidate | ignore
  headline: str                 # one plain-English sentence combining verdict + route + key evidence
  reasons: list[str]
  warnings: list[str]           # incl. fresh sample_warning; never the word "insider"
  edge_location: {domains, loss_centers, entry_shape}   # reported for EVERY verdict
  evidence_profile:
    history_depth: thin | moderate | deep
    n_independent_events: int
    effective_sample_size: float
    edge_evidence: {e_value, anytime_p, fdr_pass, obs_over_expected, fixed_p_value}
    fresh: {ex_ante_anomaly_level, ex_post_surprise_level, coordination_level,
            channels: {A,B,C,D,E -> level + reason}, alt_explanation}
    recent_anomaly_level: none | weak | moderate | strong
    regime_shift_candidate: bool
    structural_share: float
    archetype: str
    intent: str
  provenance: {analyzer_version, rule_version, data_as_of, source_snapshot_hash}
```

### 5.2 The 7 buckets (Codex's taxonomy)

1. `insufficient_evidence` — neither engine assessable.
2. `structural_non_directional` — structural archetype explains most PnL/activity **and** no independently-supported directional residual.
3. `fresh_high_surprise` — thin history + strong multi-channel fresh evidence + alt-explanations cleared + FDR-pass.
4. `established_directional_edge` — enough **independent** history + significant (shuffle and/or e-process) + positive realized PnL.
5. `emerging_directional_evidence` — positive directional signs, not yet significant.
6. `negative_edge_or_antipattern` — negative expectancy or harmful tail (e.g. high win-rate, catastrophic tail losses).
7. `no_supported_edge` — flat / noise.

### 5.3 Precedence (first match — collisions resolved here)

```
if archetype.structural_share_high:                        -> structural_non_directional
elif fresh.history_thin and fresh.high_surprise
     and fresh.channels_strong >= 2
     and not fresh.alt_explained and fresh.fdr_pass:        -> fresh_high_surprise
elif thesis.established_edge_supported:                     -> established_directional_edge
elif thesis.emerging_positive:                             -> emerging_directional_evidence
elif thesis.negative_evidence:                             -> negative_edge_or_antipattern
elif not thesis.assessable and not fresh.assessable:       -> insufficient_evidence
else:                                                       -> no_supported_edge
```

`structural_share_high` IS the "structural explains most of the activity" condition, so it is the
primary mechanism unconditionally — but a directional residual is **retained** in `edge_location`
+ `evidence_profile` and flips the route to `monitor` (not erased). `insufficient_evidence` is
checked LAST among the non-defaults: a fresh-assessable wallet (even an unconfirmed one) is
`no_supported_edge` with a possible `monitor` route, never suppressed as "insufficient".

Codex-driven corrections baked in:
- **`insufficient_evidence` is NOT first.** It fires only when *both* engines lack evidence — a thin wallet that is fresh-assessable is not suppressed.
- **`structural_non_directional` is conditional**, not an unconditional override: an arb-heavy wallet with an independently-supported directional subset keeps `primary = structural_non_directional` **but** retains the directional finding in `evidence_profile` (secondary directional evidence is not erased).
- **The fresh/established collision cannot occur:** `established_directional_edge` *requires* enough independent history, so a thin wallet can never satisfy it. `proven_edge` → renamed `established_directional_edge` (statistics support evidence, not a permanent edge). `unproven` → `emerging_directional_evidence`. `no_edge` → split into `no_supported_edge` vs `negative_edge_or_antipattern`.

### 5.4 Deep wallet with a suspicious recent burst (checkpoint Q2)

Primary stays `established_directional_edge` (do not let a short burst erase a long record). The burst sets `evidence.recent_anomaly_level = strong`, `regime_shift_candidate = true` when recent behavior diverges sharply from history, and **upgrades the route** to `expedited_review`. The burst changes routing, never the lifetime verdict. Headline: *"Established directional edge with an unusual recent cluster of low-priced, high-impact wins; routed for expedited review."*

### 5.5 `operational_route`

Derived (pure) from `(primary_verdict, recent_anomaly_level, data_freshness)`:
- `expedited_review` — `fresh_high_surprise`, or `established_directional_edge` with strong recent anomaly.
- `strategy_candidate` — `established_directional_edge` / `emerging_directional_evidence` (clean) — downstream strategy-authoring surfaces may use this as a review hint; the analyzer only labels, never creates paper positions.
- `monitor` — ex-ante-only fresh signals, thin emerging, stale data needing refresh.
- `ignore` — `no_supported_edge`, `negative_edge_or_antipattern`, `structural_non_directional` (unless a directional residual flips it to monitor).

---

## 6. The funnel — universal queue + worker

### 6.1 `wallet_analysis_queue` (new, analyzer-owned)

<!-- schema:external=wallet_analysis_queue -->
```sql
CREATE TABLE IF NOT EXISTS wallet_analysis_queue (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet            TEXT    NOT NULL,
    source            TEXT    NOT NULL,            -- discovery|followed|watchlist|manual|favorite|stream
    source_ref        TEXT    NOT NULL DEFAULT '', -- e.g. discovery run id, operator id
    reason            TEXT    NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 100,-- lower = sooner
    status            TEXT    NOT NULL DEFAULT 'pending', -- pending|leased|done|failed
    lease_owner       TEXT    NOT NULL DEFAULT '',
    lease_expires_ts  INTEGER NOT NULL DEFAULT 0,
    heartbeat_ts      INTEGER NOT NULL DEFAULT 0,
    attempt_count     INTEGER NOT NULL DEFAULT 0,
    next_retry_ts     INTEGER NOT NULL DEFAULT 0,
    analyzer_version  INTEGER NOT NULL DEFAULT 0,
    data_as_of        INTEGER NOT NULL DEFAULT 0,
    last_error        TEXT    NOT NULL DEFAULT '',
    enqueued_ts       INTEGER NOT NULL,
    updated_ts        INTEGER NOT NULL
);
-- Idempotency: at most one live (pending|leased) row per wallet.
CREATE UNIQUE INDEX IF NOT EXISTS idx_waq_wallet_live
    ON wallet_analysis_queue(wallet) WHERE status IN ('pending','leased');
CREATE INDEX IF NOT EXISTS idx_waq_status_priority
    ON wallet_analysis_queue(status, priority, next_retry_ts);
```

### 6.2 Worker

- **Lease** atomically: `UPDATE ... SET status='leased', lease_owner, lease_expires_ts, heartbeat_ts WHERE id = (SELECT id FROM wallet_analysis_queue WHERE status='pending' AND next_retry_ts<=now ORDER BY priority, enqueued_ts LIMIT 1)` (mirrors the `analysis_runs` lease pattern, hardened with heartbeat + expiry so a dead worker's lease is reclaimable).
- **Run:** `run_extractor` (thesis) → fresh engine v2 (with injected CLV + funding-graph feeds) → archetype → `synthesize_read` → append to `wallet_analysis_reads` → `status='done'`.
- **Dedup / cadence:** skip if a fresh read exists (`data_as_of` within N hours; favorites get a shorter N); else (re)queue. Re-analysis bumps the online-FDR test index (handled by the e-process — anytime-valid).
- **Rate limit:** a shared ≤ 8 RPS limiter across all outbound Polymarket/CLOB/RPC calls; bounded worker concurrency.
- **Fail loud:** geo-block / auth failures raise a distinct, surfaced error class (the wallet stays `failed` with `last_error`, retried with backoff) — **never** a silent skip or a false "insufficient_evidence". A read row is only `done` when a canonical `WalletRead` exists (even if that read is `insufficient_evidence`).

### 6.3 Geo / host reality (gap surfaced)

Always-on auto-analysis hammers Polymarket; the Amsterdam VPS is geo-restricted. The worker is **region-agnostic and fails loud**; "always-on" liveness depends on running from a permitted-region host (local or the planned migration). This is an operational dependency, documented, not silently assumed.

## 7. Persistence — immutable reads

### 7.1 `wallet_analysis_reads` (new, append-only)

<!-- schema:external=wallet_analysis_reads -->
```sql
CREATE TABLE IF NOT EXISTS wallet_analysis_reads (
    read_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet              TEXT    NOT NULL,
    analyzer_version    INTEGER NOT NULL,
    rule_version        INTEGER NOT NULL,
    data_as_of          INTEGER NOT NULL,
    primary_verdict     TEXT    NOT NULL,
    operational_route   TEXT    NOT NULL,
    history_depth       TEXT    NOT NULL,
    ex_ante_level       TEXT    NOT NULL DEFAULT 'none',
    ex_post_level       TEXT    NOT NULL DEFAULT 'none',
    coordination_level  TEXT    NOT NULL DEFAULT 'none',
    recent_anomaly_level TEXT   NOT NULL DEFAULT 'none',
    archetype           TEXT    NOT NULL DEFAULT 'unknown',
    intent              TEXT    NOT NULL DEFAULT '',
    headline            TEXT    NOT NULL DEFAULT '',
    evidence_json       TEXT    NOT NULL DEFAULT '{}',
    edge_location_json  TEXT    NOT NULL DEFAULT '{}',
    warnings_json       TEXT    NOT NULL DEFAULT '[]',
    source_snapshot_hash TEXT   NOT NULL DEFAULT '',
    supersedes_read_id  INTEGER,
    created_ts          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_war_wallet_created
    ON wallet_analysis_reads(wallet, created_ts DESC);
```
- **Immutable:** never UPDATE a read; a re-analysis appends a new row with `supersedes_read_id` pointing at the prior latest.
- **Latest pointer:** a view `wallet_analysis_latest` = the max-`created_ts` read per wallet (or a thin pointer table if the view is hot). Downstream consumers read the view.
- **Idempotency:** `(wallet, analyzer_version, data_as_of)` — the worker does not append a duplicate read for the same data snapshot.
- **No lane or downstream eligibility fields.** Routing is downstream; the analyzer emits `operational_route` only. `wallet_signal_profiles` is untouched by this sub-project.

---

## 8. Data feeds built in this sub-project

### 8.1 [F-B] CLV feed (`clv.py`, Channel B)
- Map `condition_id`+`outcome_index` → CLOB **asset id** from `raw_activity` (reuse `goldsky_fills.asset_to_condition_from_activity` inverse).
- For each resolved/closing directional bet: `entry_price` (have it) vs `close_price = price_history_fn(asset_id, resolution_ts − ε)`; CLV = signed move in the held side's favor.
- Wallet-level Channel B level from the exposure-weighted fraction of bets that beat the closing line; missing prices → `data_gap`, not zero.
- Injected as a closure (pure core stays pure); rate-limited + memoized via the adapter's cache.

### 8.2 [F-E] Funding graph (`funding_graph.py`, Channel E)
- `eth_getLogs` for USDC (`0x2791…84174`) `Transfer(address,address,uint256)` with `topic2 = pad(wallet)` (inbound) over the wallet's active block range; earliest inbound non-exchange transfer → funder address(es) + `funded_ts` (→ wallet/funding age, an ex-ante signal).
- **Linkage:** wallets sharing a funder form a cluster; **synchronized entries** = linked wallets entering the same `condition_id` within Δt. Strong coordination requires both shared funding **and** synchronized entries.
- Reuses `conversions.py` RPC plumbing (`_polygon_rpc_url`, JSON-RPC POST, retry/backoff). **Graceful + loud degrade:** no `POLYGON_RPC_URL` ⇒ Channel E dark ⇒ `data_gap = "funding graph unavailable (no RPC)"`, never a silent pass and never a coordination claim.
- Linked-wallet de-duplication feeds back into Channel A (synchronized duplicates are not independent events).

---

## 9. Honesty & framing (non-negotiable)

- **Never the word "insider."** Public/stored framing: "fresh account with unusually concentrated, high-surprise results and entry timing that warrants forward monitoring." Bucket name is `fresh_high_surprise`.
- Every fresh flag carries the base-rate `sample_warning` (high false-positive shape; survivorship; watch-only; forward test is the only verdict).
- `edge_location` and `data_gaps` are always populated; dark channels and missing feeds are explicit. No expected PnL, no sizing, no copy advice anywhere in the read.

## 10. Testing — acceptance matrix

Synthesis and the fresh engine are pure ⇒ exhaustive golden tests. Codex's 12 cases are adopted verbatim as the acceptance suite, plus statistical-validity tests:

1. 12 independent \$0.20 bets, 3 wins → **not** `fresh_high_surprise` (Poisson-binomial null).
2. 3 independent \$0.01 wins, material size → strong `ex_post_surprise_level`.
3. 3 cheap wins from one event family → effective sample collapses to ~1 → not assessable for surprise.
4. Fresh wallet, no resolved bets, extreme CLV/timing → `monitor` route via ex-ante, **not** established edge.
5. Arb wallet, huge PnL → `structural_non_directional`.
6. Arb-heavy wallet with an independently-supported directional subset → `structural_non_directional` primary, directional evidence **retained**.
7. Established edge + suspicious recent burst → `established_directional_edge` + `expedited_review` route + `regime_shift_candidate`.
8. High win-rate, negative PnL from tail losses → `negative_edge_or_antipattern`.
9. Public-truth-stale trade (entry after effective resolution) → `alt_explanation = public_truth_stale`, no fresh flag.
10. Linked wallets, synchronized bets → cluster-deduplicated evidence; coordination level set; independence reduced.
11. Repeated re-analysis under the null (peeking) → false-alert rate controlled (e-process / anytime-valid).
12. Thousands of null wallets → realized FDR ≤ target (online e-FDR).

Plus: queue lease/heartbeat/retry/idempotency tests (atomic lease, reclaim on expiry, no duplicate live row, no duplicate read per snapshot); feed deg- radation tests (no RPC → Channel E dark + data_gap; price 404 → CLV gap); worker fail-loud test (geo error → `failed` + surfaced, not `insufficient_evidence`). Schema validator + brain-index freshness green.

## 11. Phasing within this sub-project (each shippable + green)

- **P1 — synthesis + verdict model (pure).** `WalletRead`, `synthesize_read`, the 7 buckets + precedence, route derivation. Golden tests for cases 5–8 (no new data needed). Fresh/thesis inputs stubbed via dataclasses.
- **P2 — fresh engine v2 statistics (pure).** Event clustering, Poisson-binomial, e-process, EB-shrinkage, alt-explanation gate; Channel A/C/D (ledger-only). Cases 1–3, 9.
- **P3 — online-FDR allocator.** Stream state + e-LOND threshold; cases 11–12.
- **P4 — feeds.** [F-B] CLV (Channel B) + [F-E] funding graph (Channel E); cases 4, 10; degradation tests.
- **P5 — persistence.** `wallet_analysis_reads` + latest view; immutability + idempotency tests.
- **P6 — queue + worker + intake.** `wallet_analysis_queue`, lease/heartbeat/retry, rate limiter, fail-loud, intake adapters; lease/idempotency tests; wire `run_extractor` + fresh + archetype + synthesis end-to-end on one real wallet.

## 12. Risks

- **Over-flagging** → mitigated by Poisson-binomial + event independence + ≥2-channel rule + alt-explanation gate + anytime-valid e-process + online-FDR + EB-shrinkage. The whole point of the rebuild.
- **Geo/host** → worker fails loud; liveness is a host dependency, documented.
- **Funding-graph cost/latency** → bounded block range, memoized, rate-limited; dark-degrades to a data_gap.
- **Statistical-formula correctness** → exact `λ`-grid, e-LOND constants, and EB priors pinned in the implementation plan with the §10 tests as the lock; research-methodologist adversarial review before merge.
- **Scope creep into downstream** → routing fields and `wallet_signal_profiles` deliberately excluded; Signals/UI/strategy authoring are separate specs.

## 13. Out of scope (downstream sub-projects, named so they aren't lost)

Signals board v2 (auto lanes + clickable wallet links), favorite/delete wallet CRUD, the Analyzer-tab UI redesign, strategy authoring, and any promotion workflow. They consume `operational_route` + `evidence_profile` from this engine; they do not belong in this spec.
