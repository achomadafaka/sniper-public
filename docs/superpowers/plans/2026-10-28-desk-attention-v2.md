# Desk D attention and provider claims implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Epsilon implements inline; Delta reviews the stable SHA. Beta owns sheet wiring and Codex owns policy/CID.

**Goal:** Supply causal heat v2, provider list claims and cached public-page Dex facts for cohort D.

**Architecture:** Three local readers export frozen definitions and closed shapes. The X producer preserves snapshot provenance and publishes coverage evidence. A separate bounded anonymous Dex worker captures only cheap-gate-pass candidates; JUDGE never fetches.

**Tech Stack:** Python standard library, SQLite, existing Playwright browser support.

**Spec:** `docs/audits/2026-10-28-desk-cohort-d-all-sheet-gaps-preregistration.md`, Gamma freeze `8a21625527`, Amendments 2/5/6/7/8/9/10 (`dc5fcf0dd`, `11fa336fc`, `bbc25ce6e`, `87c0f666f`, `29e493498`, `255a813a4`, `e9e979d0b`). These are available from Git while this branch starts at operator-selected `2c40d0a2b`.

## Global constraints

- Paper facts only; no funded entry, exit, sizing, signing, credentials, `.env`, restart or activation changes.
- External facts use `sheet_as_of`; captured source clocks retain host receipt and sampled offset. Missing offset is unknown.
- Heat: first matching-version receipt, mint OR word-bounded ticker, one post ID once, initial snapshots excluded, deletions do not erase prior observations.
- Heat windows 15m/1h/6h need >=90% proven coverage and heartbeat age <=60s; gaps carry covered_s/gap_s and lower_bound. Before the new producer epoch is gap.
- Promoters: freeze the existing empty v0 list; empty means null promoter count, never zero. Approved common-word list plus length <=3/digits/past-only 24h collisions flags ambiguity.
- List claims use the exact intake row that surfaced the ticket, freshness <=300s, provider label, per-field parse errors.
- Dex: anonymous public page only, own worker/local store, cheap-gate-pass population, one attempt, <=1 request/mint/300s, <=1 request/s globally, frozen daily cap. Refusal/challenge halts.
- No-gate v28 bytes and all Beta-owned context/contract/facts files remain untouched.
- Tests use `-n 2`; commits use `scripts/crew_commit.sh` and explicit owned paths.

## Review focus

- A fresh heartbeat alone cannot establish a covered X window; process death/restart, missing offsets and old snapshot provenance must retain gaps/unknown.
- Updated/deleted/backfilled post IDs cannot acquire new window membership or future author metadata.
- Bare short/common tickers and future same-symbol coins cannot manufacture unique attribution.
- Human list suffixes, signs, separators, non-finite values and absent Pump.fun fields must preserve units and unknown values.
- A future/wrong-pair/stale Dex capture and a rate-limited/challenged page must never become a known empty result.

### Task 1: frozen interfaces and list claims

**Files:** three `sniper/services/memecoin_desk_{heat_v2,list_claims,dex_token}.py` modules; five JSON files in `sniper/services/desk_context_definitions/`; `tests/test_desk_list_claims.py`.

**Interfaces:** Each module exports `definition() -> {version, definition_sha256}`, `KNOWN_KEYS`, `UNKNOWN_KEYS`, `REASONS`, `FIELD_REASONS`. Readers are `heat_for/list_claims_for/dex_token_for(candidate, *, desk_dir, as_of)` and never raise source failures. Heat also exports closed window key sets.

- [x] Write the definition-binding test and prove import failure before implementation (3 RED; 3 GREEN after accessors).
- [ ] Commit exact JSON bytes, list snapshots and definition accessors; send SHA/digests to Beta/Codex/Gamma.
- [x] Add listing fixtures with causal source-row identity, missing/stale offsets, Fomo suffix/sign/locale cells and Pump.fun USD-only fields; prove RED.
- [x] Reuse the bounded existing intake source reader, parse numeric claims, prove focused GREEN, commit.

### Task 2: X provenance, coverage and heat

**Files:** `scripts/memecoin_desk_x_producer.py`, `tests/test_memecoin_desk_x_producer.py`, heat reader, `tests/test_desk_heat_v2.py`.

- [ ] Write failing tests for distinct initialTweets, producer epochs and uninterrupted coverage; preserve socket/credential tests.
- [ ] Add only the Alpha-approved tagging/index/coverage and stored-clock seam; no historical reconstruction.
- [ ] Test mint/ticker union, post dedup, 15m/1h/6h boundaries, missing authors/followers, empty promoters, cross-midnight coverage, stale/missing tape, future ambiguity and bounded work; prove RED then GREEN.
- [ ] Measure whole-reader fixture p50/p90 and 6h tape cost, run relevant focused controls, commit.

### Task 3: public-page Dex capture and cache reader

**Files:** Dex reader, `scripts/memecoin_desk_dex_token.py`, `tests/test_desk_dex_token.py`.

- [ ] Use the inspected anonymous PMUP 24H panel as a provider-format fixture; keep exact mint/pair identity.
- [ ] Test missing/future/stale/malformed captures, no stats, refusal, deadline and per-mint/global/daily request limits before implementing.
- [ ] Reuse the existing cheap-gate-pass population; capture off JUDGE with a separate singleton worker and local store.
- [ ] Prove focused GREEN and absence of inline fetches, run schema/diff checks, commit and hand stable SHA to Delta/Gamma/Codex/Beta.

No historical sheet rewrite, full-X coverage claim, promotion/payment proof or executable-price claim is in scope. Alpha owns D activation and the new X epoch.
