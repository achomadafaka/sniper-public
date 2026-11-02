# Spec — Public-Truth Stale, source-first redesign

**Date:** 2026-06-16
**Status:** design, rev 1 (supersedes the event-first framing of the v0 manual scorer)
**Context:** the v0 scorer (`sniper/analysis/public_truth_stale/`) is event-first — *you* observe an event, *then* hunt for its market and capture the book. Two problems forced this rethink: (1) the "buy almost-resolved favorite" price-scrape proved **−EV at scale** (24h, n=261 markets, z=−2.8 to −10 — see research doc 2026-06-15 update); (2) event-first is reactive, manual, and the "find the market" step is lossy. The real edge is the **lag between an authoritative source publishing and the Polymarket market repricing**. This spec inverts the flow to **source-first**.

## 1. Core inversion

```
v0 (event-first):   observe event -> hunt for its market -> capture book -> score
v1 (source-first):  curate sources -> monitor them -> on fire, LINK to pre-mapped market(s)
                     -> check staleness -> paper-enter the now-known winner below fair
```

The edge is the **publish→reprice lag**. We win by being mapped and watching the source, so the moment authoritative info drops we already know which market + outcome it resolves and whether the book is stale.

## 2. The hard problem: source→market linking

Matching an arbitrary news item to the exact Polymarket condition + winning outcome is the crux. Two regimes, hardest deferred:

- **(A) Pre-linked / scheduled (build first).** For events on a *calendar* — sports finals, macro releases (BLS/Fed), scheduled token listings — both the market AND its authoritative source are known *in advance*. We pre-register `(condition_id, outcome_map, source_url, rule_text_hash, scheduled_resolve_ts)` BEFORE the event. Monitoring is then just "watch this source at this time." No fuzzy matching. This is the tractable, high-confidence core. Polymarket markets expose `resolutionSource` + `description` + `endDate`, so pre-linking is largely automatable from market metadata.
- **(B) Reactive / breaking (later).** Unscheduled news. Requires entity/keyword matching of a feed item to candidate markets (via `resolutionSource` domain, title entities, tags). Lower confidence, higher false-match risk — explicitly Phase 2+, gated behind (A) working.

## 3. Pipeline (v1, pre-linked regime)

1. **Source registry** — extend `source_accounts_seed.json`: each entry = {family, authority_tier, fetch_method (RSS/API/official-social/scheduled-page), rate_limit}. Tiers ranked per the research note (sports official finals > exchange listings > macro/gov > weather > trusted accounts).
2. **Market pre-linker** — for upcoming Polymarket markets with a hard `resolutionSource`, build `(condition_id → source_url, rule_text_hash, outcome_map, scheduled_resolve_ts)`. Auditable: store the rule text + its sha256.
3. **Source monitor** — at/after `scheduled_resolve_ts`, poll the pre-linked source for the authoritative result. Record `(source_value, source_timestamp, source_url)`. Respect rate limits; transient failures stay "unknown", never cached as a value.
4. **Outcome resolver** — map `source_value` → market outcome via the pre-registered `outcome_map` + `rule_text` (exact-match gate). Sets `finality_status = official_final` only when the source is authoritative + final.
5. **Staleness check** — fetch the determined-winner token's live book; depth-weighted entry for the fixed $20 stake; `net_edge = 1.0 − entry − cost`. Fire (paper) iff edge ≥ gate AND book depth sufficient.
6. **Confirmation tracker** — after market resolves, record `confirmed_by_resolution` vs `contradicted` → forward CLV / hit-rate per source family.

## 4. Reuse from v0 (do not rebuild)

- `pricing.py` depth-weighted buy + `InsufficientDepthError` — unchanged.
- `report.py` lane segmentation + `PaperEntry` — unchanged; lanes A/B/C are the source families.
- `manual_log.py` observation schema — extend with `scheduled_resolve_ts` + `linked_at` (pre-link time) for lag measurement.
- The scorer gates (`exact_match`, fixed $20, Lane-A `official_final`) — KEEP. They are the auditability backbone Codex hardened.

## 5. What this fixes vs v0

- **Proactive, not reactive** — monitor many sources continuously vs hand-watch one event.
- **No fuzzy market hunt** — pre-linking does the matching once, ahead of time, auditable.
- **Targets the real edge** — the publish→reprice lag, measured by `source_timestamp` vs entry time and forward CLV.
- **Honest** — no price-band scraping (proven −EV); the only claimed edge is a *documented* source lag on an *officially-determined* outcome.

## 6. Open decisions (need operator input)

1. **First source family to wire**: sports official finals (most frequent, instant authoritative score) vs macro/gov (scheduled, fewer, cleaner rule text). Research note ranked sports first.
2. **Lag measurement**: do we paper-enter at first authoritative confirmation, or wait N seconds to confirm the source isn't revised? (Revision risk vs lag capture.)
3. **Monitoring cadence + infra**: manual operator polling (v0 discipline) vs a scheduled poller (the design warned against a *market* scanner, but a *source* poller on a pre-linked watchlist is different — it's bounded and auditable).
4. **The +7h env clock skew** breaks any scheduled poller built on the env clock — must align to real (source/exchange) time.

## 7. Phase plan (sketch)

- **Phase 1**: source registry + market pre-linker + manual source-check + staleness scorer, on ONE scheduled family (sports finals). Offline/paper, auditable. Reuses v0 pricing/report.
- **Phase 2**: scheduled source poller (real-clock-aligned) for the pre-linked watchlist.
- **Phase 3**: reactive/breaking matching (entity/keyword) — only after Phase 1 shows a real, repeatable lag edge.

## Sources
- Polymarket resolution/orderbook/market-data docs (resolutionSource, endDate fields).
- Research note: `docs/research/2026-06-15-public-truth-stale-source-research.md` (source ranking + the −EV favorite-scrape finding).
