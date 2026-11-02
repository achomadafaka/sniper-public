# Phase B — Fixture Re-Capture + Card-First Replay

**Date:** 2026-05-17
**Status:** spec → operator-approved capture → fixture-only replay → cards → diff → operator review
**Origin:** post-Phase A closeout (G2/G3/G4/G5/G6 shipped). User direction (2026-05-17): re-record 10 calibration fixtures before any WIW/Strategy Pattern Audit work grows on top of the new surfaces, so we can tell expected richer-data lift from regression.
**Scope:** Operator-gated live capture for 10 wallets; fixture-only replay; per-wallet report cards; verdict/surface shift classification; closeout summary. **No analyzer persistence, no batch production writes, no service restart.**

## 0. Doctrine

The wallet analyzer is strategy-intelligence, not a copy bot. Phase B is a truth-validation pass — we are checking whether the Phase A surface additions changed actual wallet reads or only improved visibility. WIW Phase 1 thesis cards built on v3 fixtures (pre-Phase A) would inherit stale coverage gaps; we close that gap before consuming the analyzer in any downstream layer.

## 1. Wallets in scope

Exactly the 10 wallets in `tests/calibration/labels.yaml` (calibration_version=3). Each one will get a fresh capture, a fresh card, and an operator-classifiable shift entry.

```text
0x1cc16713196d456f86fa9c7387dd326a7f73b8df
0x1eb06435d217aafbc511e5e0225b46c6e2dbd25b
0x22e4248bdb066f65c9f11cd66cdd3719a28eef1c
0x2b46bb71681777202368d095196f266944a1684d
0x58f8f1138be2192696378629fc9aa23c7910dc70
0x5bffcf561bcae83af680ad600cb99f1184d6ffbe
0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee
0x8493374739b45eb032382857feeb7ed025215366
0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4
0xdbade4c82fb72780a0db9a38f821d8671aba9c95
```

v3 fixtures stay at `tests/calibration/fixtures/<wallet>_2026-05-13.json`. New captures land at `tests/calibration/fixtures/<wallet>_2026-05-17.json`. Both sets coexist (operator preference) for the duration of Phase B and any subsequent rollback.

## 2. B1 — re-capture (LIVE Polymarket Data API)

### 2.1 Exact command per wallet

For each wallet address `<W>`:

```bash
python3 scripts/record_fetch_snapshot.py <W> \
    --out tests/calibration/fixtures/<W>_2026-05-17.json \
    --capital-ledger
```

`--capital-ledger` matches the v3 capture default. `walk_full_history=True` is the default in `record_fetch_snapshot.py` (no flag needed). No analyzer is invoked here — the script only writes a `WalletData` JSON.

### 2.2 Execution discipline

- **Sequential**, not parallel. Polymarket's `/closed-positions` / `/activity` / `/positions` endpoints can rate-limit; sequential keeps us under their per-IP threshold.
- **No retries past the script's built-in resilience.** If a capture fails for one wallet, we record the failure and continue with the remaining wallets. The closeout summary names every failed capture.
- **No DB writes.** `record_fetch_snapshot.py` writes one JSON per wallet to disk. No SQLite, no Supabase, no analyzer_runs row.
- **No service restart.** None of the legacy services need to be touched.

### 2.3 Approval gate

The operator has held B1 once for "need more spec first." After reading this doc, the operator re-grants approval. Until then, B1 does not run. Subsequent slices (B2–B6) require B1 outputs and are auto-gated.

### 2.4 Expected wall time

10 wallets × {30s small, ~2–3 min for high-volume (rank1, swisstony, pestle)}. Total expected window: 10–25 minutes. The largest wallets (those that exhibited cap_hit in v3) drive the upper bound.

### 2.5 What the new fixtures will carry that v3 did not

These are Phase A additions that show up in `WalletData` and downstream `ProtocolReport`:

- `WalletData.fetch_termination["closed_positions"]` — already in v3 (Phase A split #3 landed pre-v3); the new `data_completeness.closed_positions_capture` block surfaces it as named fields.
- No new `WalletData` fields. The fixture schema is unchanged at the data layer.

What changes is the **analyzer output** when replaying the fixture under Phase A code:

| Surface | Pre-A behavior | Post-A behavior |
|---|---|---|
| `data_completeness` keys | `{activity_capture, attribution_coverage, caveats}` | `{activity_capture, attribution_coverage, closed_positions_capture, tag_lookup_state, caveats}` |
| `data_quality_flags` | no `TAG_LOOKUP_UNAVAILABLE` | `TAG_LOOKUP_UNAVAILABLE` when applicable |
| `outcome_side_breakdown` | no `category_clusters` key on any wallet | `category_clusters` populated when leafs+nav available |
| Strategy Pattern Audit on the population | `category_specialist_v1` always INCONCLUSIVE | matches real wallets that concentrate |
| Caveats | no `closed_positions_truncated`, no rendered closed-position cap line | both present |

## 3. B2 — fixture-only replay

For each freshly-captured fixture:

```bash
python3 scripts/run_wallet_protocol.py <W> \
    --from-fixture tests/calibration/fixtures/<W>_2026-05-17.json \
    --json --no-persist \
    --seed 1 --n-iter 2000 \
    --no-tags --no-peers --no-exec --no-history --no-archetype \
    > /tmp/phase-b/<W>.json
```

These flags match `calibration_report_card.py`'s `HARNESS_ARGS` exactly — the card is bounded by design so its prose doesn't couple to expensive surfaces or local DB state. The Phase A surfaces (`closed_positions_capture`, `tag_lookup_state`, `category_clusters`, `triage_context`) are all wired into the bounded path; we will see them on every card.

Acceptance: `rc == 0` for every wallet. JSON parses. `protocol_version == "v2"`.

## 4. B3 — report cards

**Amended 2026-05-17:** `scripts/calibration_report_card.py` already accepts an explicit `--fixture <path>` flag (it overrides the `<wallet>_<date>.json` date convention). No script patch needed — B3 calls `calibration_report_card.py --fixture tests/calibration/fixtures/<W>_2026-05-17.json` directly. The earlier plan to add a `--fixture-date` flag is dropped.

Output convention: cards land at `docs/audits/phase-b-cards/<W>_2026-05-17.md`. The card script renders the JSON it just produced into the standard prose template.

## 5. B4 — verdict / surface shift diff

For each wallet, compute a diff between the v3 expectation and the new card. Fields compared:

```text
overall_verdict
gate_result
primary_edge_type
held_analyst_branch.verdict     (when present)
flipper_branch.verdict          (when present)
data_completeness.tag_lookup_state            (new in Phase A)
data_completeness.closed_positions_capture    (new in Phase A)
outcome_side_breakdown.category_clusters      (new in Phase A)
report.data_quality_flags                     (any additions/removals)
```

For each wallet, emit a row of the shift table:

```text
| wallet | v3 verdict | v4 verdict | gate | new surfaces | classification |
```

The `classification` cell is filled by the operator using the rubric in §6.

**Built 2026-05-17:** `scripts/phase_b_shift_diff.py` implements B4 — it replays each wallet's v3 AND v4 fixture under current analyzer code, extracts the structured fields above, diffs them, and emits the markdown shift table (classification column `PENDING` for operator B5). Pure functions are unit-tested (`tests/test_phase_b_shift_diff.py`, 16 tests). Run: `python3 scripts/phase_b_shift_diff.py --json-out /tmp/phase-b/`. Wallets missing a v3 or v4 fixture are listed under a "Skipped" section instead of crashing — so the harness is runnable before B1 completes (it just skips everything until v4 fixtures land).

## 6. B5 — shift classification rubric

Each row gets ONE label from the fixed set:

| Label | Meaning |
|---|---|
| `expected_richer_data_lift` | The Phase A surface now visible adds information the v3 card couldn't have shown. No verdict / gate flip. Examples: `tag_lookup_state == "unavailable"` on a wallet whose v3 card silently ignored tags. |
| `expected_verdict_lift` | The Phase A surface caused a verdict / gate flip in a defensible direction. Example: `category_specialist_v1` now matches a wallet that genuinely concentrates; v3 said INCONCLUSIVE. |
| `bug_regression` | The Phase A surface caused a verdict / gate / branch flip that the operator judges wrong. Example: a wallet that was previously `edge_detected` is now `underpowered` due to a stricter caveat trigger, AND the operator believes the stricter trigger is mis-firing. |
| `operator_label_decision` | The shift is real and defensible, BUT it changes what labels.yaml should record. labels.yaml needs an explicit re-baseline (calibration_version becomes 4). |
| `still_insufficient_data` | The new surfaces don't change the picture; the wallet's coverage was already poor and is still poor. No labels.yaml change needed. |

`bug_regression` is the only label that BLOCKS Phase C. Any of the others is acceptable.

## 7. B6 — closeout

Write `docs/audits/2026-05-17-phase-b-fixture-recapture.md` containing:

1. Per-wallet shift table (10 rows).
2. Summary counts per classification.
3. Aggregate "honesty grade" delta — does the audit's "Analyzer grade" table need updating?
4. Open follow-ups (bug_regression rows → spec + fix; operator_label_decision rows → labels.yaml v4 entries).

If any `bug_regression` rows exist, Phase C remains gated. Else, Phase C is cleared to start.

## 8. Risks

1. **Live API failure mid-run.** Mitigated by sequential capture + continue-on-failure + named failures in closeout.
2. **Rate limiting.** Mitigated by sequential captures + the per-script throttling already in `polymarket_api.py:_throttle_requests`.
3. **Fixture schema drift.** WalletData v2 schema is locked; new captures will land at the same schema with Phase A's `fetch_termination["closed_positions"]` populated. Replay will read them via `load_fetch_snapshot` exactly as v3.
4. **CORRECTION (2026-05-21) — earlier draft of this risk was wrong.** The earlier text claimed `category_clusters` is always empty because HARNESS_ARGS passes `--no-tags`, forcing `leafs_by_cid=None`. That is false: under `--from-fixture`, `--no-tags` is *advisory* — fixture surface availability governs. The capture step (`record_fetch_snapshot.py`) reads `market_tags` from the local `sniper_tracker.db` and bakes `leafs_by_cid`/`nav_lookup` INTO the fixture. The main-worktree `sniper_tracker.db` has `market_tags` fully populated (~1.6M rows) + `tag_nav_category` seeded — so fixtures captured there carry full tag data, and the bounded replay uses it. `category_clusters` DOES populate. The original error came from observing a fresh *git worktree* (no `sniper_tracker.db` — the file is gitignored), where tag lookups fail. Consequence for Phase B: 9/10 v4 fixtures (all sweep + validation captures, run in the main worktree) carry tags; only `0x8493` (the smoke, captured in a DB-less codex worktree) lacks them and must be re-captured from the main worktree so the v4 set is uniform. v3 fixtures already carry tags.
5. **The card script's hardcoded fixture date.** Resolved — `calibration_report_card.py` already accepts `--fixture <path>`; no patch needed (see §4 amendment).

## 9. Out of scope

- `market_tags` provisioning — NOTE (2026-05-21): the local `sniper_tracker.db`
  already has `market_tags` populated (~1.6M rows); the "G4 collection gap"
  was a DB-less-worktree artifact, not a real gap. A refresh/provisioning
  tool exists on branch `codex/market-tags-provisioning`. Phase B itself
  needs no provisioning work.
- Any WIW Phase 1 work.
- Any Strategy Pattern Audit cross-wallet population analysis.
- Any analyzer batch run.
- Any persistence row writes.

## 10. Done definition

- 10 fresh fixtures landed at `tests/calibration/fixtures/<W>_2026-05-17.json` (or a documented subset if any wallet fetch failed).
- 10 fresh cards at `docs/audits/phase-b-cards/<W>_2026-05-17.md`.
- 10-row shift table in `docs/audits/2026-05-17-phase-b-fixture-recapture.md`.
- Every row classified per §6.
- Zero `bug_regression` labels → Phase C cleared. Else, the regressions are documented and Phase C stays gated.

## 11. Operator approval gates

| Gate | Action | Approval required |
|---|---|---|
| B1 — live capture | Run `record_fetch_snapshot.py` for 10 wallets, sequential | **YES** — live API |
| B2 — fixture replay | Run replay against each new fixture | No (no live, no writes) |
| B3 — render cards | Patch + run `calibration_report_card.py --fixture-date 2026-05-17` | No |
| B4 — diff | Compute shift table | No |
| B5 — classify | Operator labels each shift | **YES** — operator judgment |
| B6 — closeout | Write closeout doc | No |
| labels.yaml v4 | Re-baseline if any `operator_label_decision` rows | **YES** — operator approval |
