# Followup R9.5 — Re-analyze wallets affected by inverted `_is_win` NO branch

**Status:** complete (executed 2026-05-10 in `r9-5-execution`)
**Severity:** high (material consequence — historical wallet reports may
have inverted H2 win/loss on NO subsets)
**Source:** R2 empirical resolution + R9.1 fix
**Depends on:** R9.1 (held_analyst._is_win fix; commit landed alongside R9.2/R9.3/R9.4) — do not re-analyze before R9.1 lands.

**Execution closeout:** R9.5 regenerated the SwissMiss informational baseline
from the captured 2026-04-30 fixture, updated the SwissMiss knowledge dossier,
stale-bannered the five mark-stale artifacts, manually inspected the five
`unknown_needs_inspection` dossiers, and skipped Phase D because the inventory
found no persisted analyzer rows in DB or brain memory.

---

## 1. The material consequence

Before R9.1, `_is_win`'s NO branch tested `cp ≤ ε`. Under the
empirically-confirmed held-token-price semantics (R2,
`docs/superpowers/specs/2026-05-06-curprice-coordinate-empirical-resolution.md`),
this caused the following inversion on the NO subset of held-to-resolution
positions:

- NO held-to-resolution **wins** (NO redeems at $1 → cp ≈ 1.0 → fails old
  `cp ≤ ε` test) were classified as **losses**.
- NO held-to-resolution **losses** (NO redeems at $0 → cp ≈ 0.0 → passes
  old `cp ≤ ε` test) were classified as **wins**.

YES held-to-resolution positions were unaffected (the YES branch was
always correct, both pre- and post-R9.1 use `cp ≥ 1−ε`).

### Surfaces affected by the inversion

`_is_win` is called only inside `sniper/analysis/wallet_protocol/held_analyst.py`
(verified by repo grep, 2026-05-06). Affected hypothesis tests / surfaces:

| Surface | Affected? | Notes |
|---|---|---|
| H1 (net P&L) | No | Uses `realizedPnl` directly. |
| H2 (excess WR) | **Yes** | NO-subset wins/losses inverted. |
| H3 (size conviction) | **Yes** | Permutation test on `_is_win` outputs. |
| H4 (entry timing) | No (gated by execution_profile) | Did not consume `_is_win`. |
| H5 tag grains | **Yes** | Grain win-rate / excess-WR / avg-win-loss fields consume `_is_win`. |
| H6 (exit quality) | No | Compares SELL price to future held-token price; does not consume `_is_win`. |
| H7 (stationarity) | No | Runs on chronological `realizedPnl` series. |
| Time-split / k-fold | Partial | Only H2 / H2_sub080 / H3 fold results depend on `_is_win`. |
| Edge surface | No | Uses `realizedPnl > 0`, not `_is_win` (see `analyze_wallet.py:296`). |
| Exit attribution | No | Uses `_at_resolution` + PnL sign (classifier in `exit_attribution.py:139-144`). |
| Timing surface v1 | No (by design) | Does not consume `curPrice` magnitude. |
| Stage 1 gates `_count_open_dead` | No | Already correct under held-token semantics (R3 closure). |

PnL aggregates (gross, net, by-bucket) are unaffected — they trace
`realizedPnl`, not `_is_win`.

### Calibration baseline impact (observed 2026-05-06 run)

After R9.1, two of the 10 calibration wallets flipped `overall_verdict`
from `underpowered` → `edge_detected`:

- `0x1eb06435d217aafbc511e5e0225b46c6e2dbd25b` (operator_prior band:
  `[edge_detected, no_edge, operator_review]`)
- `0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4` (operator_prior band:
  `[edge_detected, operator_review]`)

Both new verdicts fall **within** their operator_prior accepted bands.
The legacy `expected: underpowered` baselines are stale (recorded under
buggy `_is_win`). Re-anchoring the calibration v2 baseline is in scope
for R9.5.

The other 8 calibration wallets (including swissmiss `0xdbade4c8…`
and yatsen `0x5bffcf56…`) remain at `underpowered` — their NO-subset
content does not move affected H2/H3/H5 or H2/H3 time-split outputs
across verdict thresholds.

## 2. What needs re-analysis

### 2.1 Production / deployed reports

- All wallets in `knowledge/wallets/*.md` (curated set: followed ∪
  watchlist) whose dossier cites H2 win-rate or per-bucket NO win-rate
  derived from `_is_win`. Audit the `Held analyst` section of each page.
- `tests/calibration/baselines/swissmiss_phase0_informational.json` —
  stored, but not asserted against. Regenerate for archival accuracy.

### 2.2 Calibration baselines (`tests/calibration/labels.yaml`)

- The two flipped wallets above need their `expected` field re-anchored
  with a fresh evidence row pointing to the post-R9.1 calibration report.
- The other 8 do not require updates but should be confirmed by re-run.

### 2.3 Audit memos

- `docs/audits/2026-04-30-calibration-report.md` and any later report
  that cites `actual=` values for affected wallets. Those reports are
  point-in-time and remain historically accurate; new reports should
  cite R9.1 as the cause of subsequent verdict changes.

## 3. Re-analysis approach (proposed)

To be filled in after R9.1 lands. Likely shape:

1. Re-run `scripts/run_wallet_protocol.py` against each affected wallet's
   captured fixture (`tests/calibration/fixtures/<wallet>_2026-04-30.json`)
   with the post-R9.1 code.
2. Diff `overall_verdict`, H2/H3/H5 verdicts, H2/H3 time-split results,
   and `held_analyst_branch.*`
   numerics against pre-R9.1 baselines.
3. Re-anchor `tests/calibration/labels.yaml` `expected` field for any
   wallet whose verdict changed; cite R9.1 commit as the trigger.
4. Regenerate `tests/calibration/baselines/swissmiss_phase0_informational.json`
   from a fresh run.
5. For each `knowledge/wallets/*.md` page, decide:
   - If the dossier cites `held_analyst` numerics on a wallet with
     material NO-subset content → regenerate the page.
   - If not → no action.

## 4. Out of scope

- Real-money decisions based on historical reports (no re-analysis is
  required for paper-trade-only contexts).
- v1.5 strict-repricing work (separate design).
- Any new analyzer features.
- Touching Timing Surface v1 — independent of `_is_win`.

## 5. Acceptance criteria (when R9.5 executes)

- All affected wallets re-run; diffs documented.
- Calibration v2 baseline re-anchored for the two flipped wallets, with
  evidence rows citing R9.1.
- Knowledge layer pages with affected H2/H3/H5 or H2/H3 time-split
  numerics regenerated.
- `swissmiss_phase0_informational.json` baseline regenerated.
- Audit cross-reference:
  `docs/superpowers/specs/2026-05-06-wallet-protocol-semantic-audit.md` updated to
  reflect R9.5 closure.

## 6. Impact inventory (2026-05-06)

Read-only inventory of every persisted analyzer-output artifact in the repo
that may carry pre-R9.1 inverted NO-held WR numbers. R9.1 landed at
`b78a644` on 2026-05-06; any artifact generated on or before that commit
that depends on `_is_win`'s NO branch is potentially stale.

**Definitions used below:**
- `_is_win`-tainted surfaces: H2, H3, H5 (per-grain `metric_value=WR_held`),
  H2/H3 time-split, H2/H3 k-fold, `primary_edge_type`, `overall_verdict`
  (when derived from those).
- Surfaces NOT tainted: H1, H4, H6, H7, Edge Surface bucket WR, Exit
  Attribution, Timing Surface v1, capital ledger, all PnL aggregates
  (per audit §3, §4, §R2 Outcome 2; cross-checked against the swissmiss
  baseline JSON).
- Verdict-flip risk only materializes when (a) the wallet has material
  NO-held content **and** (b) the flipped per-position win labels move
  H2/H3/H5 across `supported`/`refuted`/`inconclusive` thresholds. The
  R9.1 calibration re-run showed that 2 of 10 calibration wallets flipped;
  the other 8 did not (their NO-subset content was immaterial). The
  same property applies in production: most NO-light wallets won't move.

### 6.1 Inventory table

Grouped by NO-held risk (high → medium → low → none). Within a risk band,
ordered by path.

| Artifact path | Type | Wallet(s) | Pre-R9.1? | NO-held risk | Uses H2/H3/H6? | Action | Notes |
|---|---|---|---|---|---|---|---|
| `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md` | dossier | swissmiss | yes (`0b1ced0`, 2026-04-26) | **high** | yes (H2 refuted at -14pp, H2_sub080, H2/H3 time-split — explicit) | `rerun_required` | Heaviest NO content of any production dossier (geopolitics-NO regime, 12+ open Iran/Ukraine NO positions). Already shows `underpowered` in calibration; verdict won't flip but the cited "H2 refuted at -14pp" and "H2_sub080 inconclusive (n=646)" magnitudes derive from inverted NO labels. Numbers must be regenerated. |
| `tests/calibration/baselines/swissmiss_phase0_informational.json` | calibration baseline | swissmiss | yes (`5874876` per file's `analyzed_at`=1777592509 → 2026-04-29) | **high** | yes (full `held_analyst_branch.hypotheses[]` with H1–H6 verdicts, n, CIs, p-values) | `rerun_required` | Stored, not asserted against (informational only). Contains `H2 refuted, ci=[-0.168, -0.099]` etc. Regenerate from a post-R9.1 run on the captured fixture. R9.5 §2.1 already names this. |
| `knowledge/wallets/0x1cc16713196d456f86fa9c7387dd326a7f73b8df.md` | dossier | eatpraylove (Wickier) | yes (`da7e5e8`, 2026-04-22) | **medium** | unknown — needs read | `unknown_needs_inspection` | Calibration audit (`2026-05-01-calibration-result-mismatch.md:56`) shows H2=refuted with CERTAINTY_BUYER_DETECTED → wallet has held-to-resolution NO content. Dossier doesn't include hypothesis prose at the grep level; manual read needed to confirm whether it cites WR_held or per-grain numbers. |
| `knowledge/wallets/0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee.md` | dossier | exec-bot | yes (`a26c247`, 2026-04-22) | **medium** | unknown — needs read | `unknown_needs_inspection` | Calibration audit shows H2_sub080=refuted; permutation-fix audit (`2026-05-05`) cites it specifically as the wallet that "flipped from underpowered → edge_detected" under the H3 fix — so its H3 verdict is sensitive. Manual read needed. |
| `knowledge/wallets/0x22e4248bdb066f65c9f11cd66cdd3719a28eef1c.md` | dossier | HWR (high_winrate_review) | yes (`da7e5e8`, 2026-04-22) | **medium** | unknown — needs read | `unknown_needs_inspection` | Calibration audit shows H2=refuted with CERTAINTY_BUYER_DETECTED. Dossier has hypothesis prose unverified at grep level. |
| `knowledge/wallets/0x1eb06435d217aafbc511e5e0225b46c6e2dbd25b.md` | dossier | speculator_bot | n/a — no dossier file present in repo | n/a | n/a | `no_action` | One of the two calibration wallets that flipped under R9.1. No knowledge-layer dossier exists for it — calibration baseline is the only persisted artifact (covered separately). |
| `knowledge/wallets/0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4.md` | dossier | high_winrate_review | yes (`0f25c71`, 2026-04-18) | **medium** | likely no — auto-generated `Added reason` only | `mark_stale_only` | Second of the two calibration wallets that flipped. Auto-detected dossier with shallow analyzer fields (cites `insider_score` legacy, not wallet-protocol H2). Not affected by R9.1 itself, but flag the page for transparency since calibration baseline references it. |
| `knowledge/wallets/0x5bffcf561bcae83af680ad600cb99f1184d6ffbe.md` | dossier | YatSen | yes (`da7e5e8`, 2026-04-22) | **low** | unknown — likely no | `unknown_needs_inspection` | Calibration shows underpowered both before and after R9.1 per R9.5 §1. Manual read to confirm no protocol numerics cited. |
| `knowledge/wallets/0x8493374739b45eb032382857feeb7ed025215366.md` | dossier | (unnamed; calibration set) | yes (`0f25c71`, 2026-04-18) | **low** | likely no — auto-generated reason only | `unknown_needs_inspection` | Auto-detected addition stub. Manual confirm. |
| `knowledge/wallets/0x84dbb7103982e3617704a2ed7d5b39691952aeeb.md` and 33 other auto-detected stubs (legacy `insider_score` adds: `0x104171…`, `0x20ad75e1…`, `0x2803931e…`, `0x2a2c53bd…`, `0x380002b0…`, `0x38c2734d…`, `0x3bcf0c1c…`, `0x5cd5c8d7…`, `0x6a7e51dd…`, `0x6a88c5ec…`, `0x6ade597c…`, `0x72983c97…`, `0x751a2b86…`, `0x7b02b2ba…`, `0x92b83435…`, `0x95074f70…`, `0x9c16127e…`, `0x9cb990f1…`, `0xa16a1302…`, `0xa49becb6…`, `0xa71093ca…`, `0xb10047d6…`, `0xbad457dc…`, `0xbe89905c…`, `0xbf93328f…`, `0xc10972c0…`, `0xc25427ea…`, `0xc30c0116…`, `0xc8ab97a9…`, `0xd25b8718…`, `0xde9f7f4e…`, `0xe20a1538…`, `0xed107a85…`, `0xf1528f12…`, `0xf67f72df…`, `0xf94fe179…`, `0xtest123`, `0x000000…dead`, `0x02981ea7…`, `0x9c16127e…`) | dossier stubs | various | yes (mostly `0f25c71`/`d21ece0`) | **none** | no — only legacy `insider_score`, `win_rate=N/N` lines from auto-detection | `no_action` | Auto-generated knowledge stubs that pre-date the wallet-protocol entirely. They cite legacy scoring fields (`insider_score`, raw N/N WR) which derive from `paper_trades` / `user_analysis`, not `_is_win`. Confirmed via grep. |
| `knowledge/strategies/active/021-political_no_value_v1.md` | strategy page | (none) | yes (`0f25c71`, 2026-04-18) | **none** | no | `no_action` | grep confirms no H2/H3/H6/`held_analyst`/`primary_edge_type` mentions. Pure strategy-PnL aggregate page. |
| `knowledge/strategies/active/037-insider_copy_v1.md` | strategy page | (none) | yes (`0f25c71`) | **none** | no | `no_action` | Same as above. |
| `knowledge/strategies/active/040-insider_copy_v1.md` | strategy page | (none) | yes (`0f25c71`) | **none** | no | `no_action` | Same. |
| `knowledge/strategies/active/041-stink_bid_v1.md` | strategy page | (none) | yes (`0f25c71`) | **none** | no | `no_action` | Same. |
| `knowledge/strategies/retired/*.md` (35 files) | strategy pages | (none) | yes (`0f25c71`/`a7481a1`/`d21ece0`/`da7e5e8`) | **none** | no | `no_action` | Bulk-confirmed via grep. None cite hypothesis machinery. |
| `tests/calibration/fixtures/<10 wallets>_2026-04-30.json` (10 files) | API capture (input) | calibration set | yes (2026-04-30 capture date in name) | **none** | no — these are raw `/closed-positions` + `/activity` payloads, not analyzer output | `no_action` | These are inputs to the protocol, not output. Re-analysis consumes them. **Do not modify or regenerate.** |
| `tests/calibration/labels.yaml` | calibration label set | calibration set | yes (re-anchored at `1223de1`, 2026-05-06) | n/a | n/a | `no_action` | Already re-anchored under R9 closeout. Out of scope per R9.5 hard rule #2. |
| `tests/replay/fixtures/swissmiss_fetch_snapshot.json` | replay fixture | synthetic-swissmiss | yes (synthetic seed) | **none** | no | `no_action` | Synthetic fixture using `currentPrice` (camelCase) which is silently ignored by `_at_resolution`/`_is_win` (they read `curPrice`). The filter rejects all rows, so `_is_win` is never reached on this fixture. Verified by reading `held_analyst.py:129` and the fixture's field name. |
| `docs/audits/2026-04-30-calibration-report.md` | audit memo | full calibration set | yes (`2026-04-30`) | **medium** (reports verdicts on the 10 calibration wallets, including the two that flipped) | yes (table of `actual=` per wallet) | `mark_stale_only` | Per R9.5 §2.3: "Those reports are point-in-time and remain historically accurate." Add a banner pointing to the post-R9.1 re-run. The two flipped wallets (#7, #10) should be flagged inline. |
| `docs/audits/2026-05-01-calibration-result-mismatch.md` | audit memo | full calibration set | yes (`2026-05-01`) | **medium** (cites H2 verdicts inline for SwissMiss / eatpraylove / exec-bot / HWR) | yes | `mark_stale_only` | Same point-in-time rationale. The H2/H2_sub080 verdicts cited in §"Sampled hypothesis verdicts" derive from inverted NO labels. Add a banner; do not regenerate (the memo's purpose is to explain the v1→v2 reframe, which stands regardless). |
| `docs/audits/2026-05-05-permutation-verdict-fix.md` | audit memo | exec-bot (`0x6a72f61…`) | yes (`2026-05-05`) | **low** (cites a single `underpowered → edge_detected` flip due to H3 perm-test fix; the H3 verdict change is from the perm fix, not R9.1, but the underlying win labels are R9.1-tainted) | yes | `mark_stale_only` | Add a banner noting that the cited H3 magnitudes assume pre-R9.1 win labels for NO-held positions. |
| `docs/audits/2026-04-30-calibration-label-selection.md` | audit memo | full calibration set | yes (`2026-04-30`) | **low** (cites operator priors and `expected` values, not protocol output) | partial | `mark_stale_only` | Cites `edge_detected`/`underpowered` as operator priors and label bands. Two flipped wallets' bands now exact-match the protocol's R9.1 output, but the doc's analysis is structurally fine. Banner only. |
| `docs/audits/2026-05-01-wallet-data-completeness.md` | audit memo | calibration set | yes (`2026-05-01`) | **none** | the H3/H4/H6 mentions are about fixture data shape, not about specific wallet verdicts | `no_action` | Verified via line-level grep — no specific-wallet verdict claims. |
| `docs/audits/2026-04-30-h2-source-trace-verification.md` | audit memo | various | yes (`2026-04-30`) | **none** | `H2` refers to a Phase H2 milestone, not hypothesis H2 | `no_action` | False positive on hypothesis-name grep. |
| `docs/research/wallet_dossiers/2026-04-22-swissmiss.md` | manual research dossier | swissmiss | yes (`2026-04-22`) | **none** | no — uses archetype extractor verdicts, not wallet-protocol hypotheses | `no_action` | "Manual verdict" / "System verdict" lines refer to the archetype extractor (`multi_outcome_basket_builder`, `directional_unknown`), not `held_analyst`. Cross-confirmed via line-level grep. |
| `docs/research/wallet_dossiers/2026-04-22-kch123.md` | manual research dossier | kch123 | yes (`2026-04-22`) | **none** | no | `no_action` | Same — archetype-extractor only. |
| `docs/research/scopes/2026-04-23-swissmiss-edge-interpretation.md` | scope doc | swissmiss | yes (`2026-04-23`) | **none** | no — single "hypothesis" hit is narrative prose, not a verdict citation | `no_action` | Verified line-level. |
| `docs/superpowers/specs/*.md` and `docs/superpowers/plans/*.md` (90+ files) | spec/plan docs | various | mix; mostly post-R9.1-aware after `b78a644` | **none** for verdict citations | hypothesis names appear in design docs but not as wallet-specific verdict claims | `no_action` | Spec-grep confirmed: docs reference `H2`/`H3`/`H6` as design abstractions. R9-related docs (`2026-05-06-r9-5-...`, `2026-05-06-curprice-...`, `2026-05-06-wallet-protocol-semantic-audit.md`, `2026-05-06-wallet-protocol-timing-surface-v1-design.md`) explicitly capture the post-R9 state. No mark-stale needed. |
| `logs/*.log` (16 files) | runtime logs | mixed | yes (mixed dates) | **none** | grep returned 0 verdict citations | `no_action` | Logs do not persist verdict outputs. |
| Local SQLite — `wallet_layer5_decision`, `wallet_analysis_batch_item`, `analyzer_runs` | DB tables (writable) | n/a | n/a | n/a | n/a | `no_action` | All three tables exist locally with **0 rows** in `sniper_tracker.db`; not present in `sniper.db`. Confirmed by direct count via read-only sqlite connection. No persisted analyzer runs in the local DB. |
| Supabase `memories` — `name LIKE 'wallet_protocol_run:%'` | brain memory | n/a | n/a | n/a | n/a | `no_action` | Read-only query against Supabase `memories` returned **0 rows**. The `memo_emitter.py` codepath that would write these is gated by `persist_memo=True` + Supabase env, and the code is also `pytest`-skipped. No persisted analyzer memos exist. |

### 6.2 High-risk artifacts (re-run required)

Two artifacts. Both are SwissMiss-specific because she is the wallet with
the heaviest NO-held content in the curated set (per §1's calibration
re-run analysis and the dossier's own description of the active
"geopolitics-NO regime").

- `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md` — pre-R9.1
  swissmiss dossier explicitly cites `H2 refuted at -14pp`,
  `H2_sub080 inconclusive (n=646)`, and `H2 / H2_sub080 time-split FAILED
  out-of-sample`. All three magnitudes derive from `_is_win` with NO labels
  inverted. Verdict (`underpowered + CERTAINTY_BUYER_DETECTED`) does not
  change post-R9.1 (per §1) but the cited numbers do.
- `tests/calibration/baselines/swissmiss_phase0_informational.json` —
  full archived `held_analyst_branch.hypotheses[]` with H1–H6 verdicts,
  n's, CIs, and p-values. Pre-R9.1; R9.5 §2.1 already named this for
  regeneration.

### 6.3 Medium-risk artifacts (mark-stale or manual-review)

- `knowledge/wallets/0x1cc16713…b8df.md` (eatpraylove) — `unknown_needs_inspection`
- `knowledge/wallets/0x6a72f618…33ee.md` (exec-bot) — `unknown_needs_inspection`
- `knowledge/wallets/0x22e4248b…ef1c.md` (HWR) — `unknown_needs_inspection`
- `knowledge/wallets/0xb3b50ef6…bef4.md` (high_winrate_review) — `mark_stale_only`
- `docs/audits/2026-04-30-calibration-report.md` — `mark_stale_only`
- `docs/audits/2026-05-01-calibration-result-mismatch.md` — `mark_stale_only`
- `docs/audits/2026-05-05-permutation-verdict-fix.md` — `mark_stale_only`
- `docs/audits/2026-04-30-calibration-label-selection.md` — `mark_stale_only`

### 6.4 Re-analysis execution plan (proposed)

Phases for the **execution dispatch** (separate from this inventory).

#### Phase A — `rerun_required` artifacts (high-risk, 2 items)

A.1. `tests/calibration/baselines/swissmiss_phase0_informational.json` —
regenerate from a post-R9.1 run on
`tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-04-30.json`.
**No live API calls needed** — the captured fixture is the exact replay
input the v2 calibration baseline doc names. Can run in a worktree:
```
python3 scripts/run_wallet_protocol.py 0xdbade4c8…9c95 \
    --from-fixture tests/calibration/fixtures/0xdbade4c8…9c95_2026-04-30.json \
    --json --no-persist > tests/calibration/baselines/swissmiss_phase0_informational.json
```
Verification: diff the new file against the old one, confirm only H2/H3/H5
related fields and downstream verdict-derivatives changed (and that all
H1/H4/H6/H7 numerics are unchanged). Effort: ~1 hour.

A.2. `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md` —
re-run analyzer, then update the cited H2/H2_sub080/H2-time-split
magnitudes in the prose. Live API calls are NOT required if we accept
running against the 2026-04-30 captured fixture (the dossier was originally
generated against the live API on 2026-04-25, so a fixture replay is a
small drift but acceptable for the inverted-NO correction). For full
fidelity: re-fetch live and regenerate via the wallet-dossier-writer
agent. **Live API call required for full fidelity; fixture-replay is the
fast path.** Effort: ~2-4 hours including human review of the regenerated
prose. Can run in a worktree.

#### Phase B — `mark_stale_only` artifacts (4 items)

Bulk operation: prepend a banner to each file. Banner template:

```
> **Pre-R9.1 stale-numbers banner.** This document was generated before
> the held_analyst `_is_win` NO-branch fix (commit `b78a644`, 2026-05-06).
> Numbers derived from `_is_win` on NO-held-to-resolution positions
> (H2, H3, H5, H2/H3 time-split, H2/H3 k-fold, primary_edge_type) may be
> inverted on the NO subset. PnL aggregates, H1, H4, H6, H7, Edge Surface,
> Exit Attribution, and Timing Surface results are unaffected. See
> `docs/superpowers/specs/2026-05-06-r9-5-historical-reanalysis-plan.md`.
```

Targets:
- `docs/audits/2026-04-30-calibration-report.md`
- `docs/audits/2026-05-01-calibration-result-mismatch.md`
- `docs/audits/2026-05-05-permutation-verdict-fix.md`
- `docs/audits/2026-04-30-calibration-label-selection.md`
- `knowledge/wallets/0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4.md`

Live API: not required. Worktree: not required (5-line additions per file).
Effort: ~30 minutes.

#### Phase C — `unknown_needs_inspection` (4 items, `manual_review`)

For each of the 4 dossiers:
- Open the file. Search for any cited magnitude derivable from `_is_win`
  (per-grain WR_held, H2 magnitude, H3 magnitude, H5 grain numbers).
- If none cited → demote to `mark_stale_only` (banner only).
- If cited → promote to `rerun_required`.

Targets:
- `knowledge/wallets/0x1cc16713196d456f86fa9c7387dd326a7f73b8df.md`
- `knowledge/wallets/0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee.md`
- `knowledge/wallets/0x22e4248bdb066f65c9f11cd66cdd3719a28eef1c.md`
- `knowledge/wallets/0x5bffcf561bcae83af680ad600cb99f1184d6ffbe.md`
- `knowledge/wallets/0x8493374739b45eb032382857feeb7ed025215366.md`

Live API: only if any get promoted to `rerun_required` and the original
dossier was authored against live data. Effort: ~30 minutes inspection
+ whatever rerun count is triggered.

#### Phase D — Brain memory / DB rows

**Skip this phase.** Verified read-only:
- Local `sniper_tracker.db`: 0 rows in all three analyzer-output tables.
- `sniper.db`: tables don't exist.
- Supabase `memories WHERE name LIKE 'wallet_protocol_run:%'`: 0 rows.

No persistent storage of `_is_win`-derived numbers exists outside the
file artifacts already inventoried above.

### 6.5 Files inspected

**Knowledge layer** (47 wallet dossiers + 39 strategy pages):
- `knowledge/wallets/*.md` — 47 files (45 production + `0xtest123` + `0x000…dead`)
- `knowledge/strategies/active/*.md` — 4 files
- `knowledge/strategies/retired/*.md` — 35 files
- `knowledge/README.md`

**Calibration**:
- `tests/calibration/labels.yaml`
- `tests/calibration/fixtures/*.json` — 10 files (read-only field check)
- `tests/calibration/baselines/swissmiss_phase0_informational.json`
- `tests/calibration/fixtures_ledger/README.md`
- `tests/calibration/test_calibration_set.py`

**Replay fixtures**:
- `tests/replay/fixtures/swissmiss_fetch_snapshot.json` (field-name check vs `held_analyst.py:129`)

**Audit / spec / plan docs** (greppped for hypothesis-name + verdict-token + wallet-address co-occurrence):
- `docs/audits/*.md` — 13 files
- `docs/superpowers/specs/*.md` — 50+ files
- `docs/superpowers/plans/*.md` — 50+ files
- `docs/superpowers/research/*.md` — 3 files
- `docs/research/**/*.md` — 13 files including `wallet_dossiers/`, `scopes/`, `prior_art/`

**Code paths** (to confirm DB persistence surface):
- `sniper/analysis/wallet_protocol/persistence.py`
- `sniper/analysis/wallet_protocol/memo_emitter.py`
- `sniper/persistence/db_layer5.py`
- `db.py`, `db_*.py`, `bot/supabase_helper.py`

**DB tables introspected** (read-only counts only):
- `analyzer_runs`, `wallet_analysis_batch`, `wallet_analysis_batch_item`,
  `wallet_layer5_decision`, `wallet_analyzer_intake_queue` (local SQLite)
- Supabase `memories` (filtered by `name LIKE 'wallet_protocol_run:%'`)

**Logs**:
- `logs/*.log` — 16 files (grep for verdict tokens; 0 hits)

**Schema lookup**:
- `python3 scripts/brain_index.py schema --list` (74 tables)

## 7. Execution closeout (2026-05-10)

### Phase A — complete

- Regenerated
  `tests/calibration/baselines/swissmiss_phase0_informational.json` from
  `tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-04-30.json`
  with:

  ```bash
  python scripts/run_wallet_protocol.py \
      0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
      --from-fixture tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-04-30.json \
      --seed 42 --n-iter 500 --json --no-persist
  ```

- Updated `knowledge/wallets/0xdbade4c82fb72780a0db9a38f821d8671aba9c95.md`.
  The stale "H2 refuted at -14pp" sentence is superseded by fixture replay:
  H2 inconclusive at +2.2pp (n=950, CI -0.4pp to +4.6pp), H2_sub080
  inconclusive at +1.9pp (n=709), H2/H2_sub080/H3 time-split failed,
  overall verdict still `underpowered`.
- Diff caveat: the regenerated JSON differs beyond H2/H3/H5 because the
  old artifact was an older live/API-shaped output, while the R9.5 replay uses
  the captured fixture and current report schema. That is accepted for archival
  replacement; no live API was used.

### Phase B — complete

Added stale-number banners to:

- `docs/audits/2026-04-30-calibration-report.md`
- `docs/audits/2026-05-01-calibration-result-mismatch.md`
- `docs/audits/2026-05-05-permutation-verdict-fix.md`
- `docs/audits/2026-04-30-calibration-label-selection.md`
- `knowledge/wallets/0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4.md`

### Phase C — complete

Manual read found no wallet-protocol H2/H3/H5 or time-split numeric citations
in these dossiers; no rerun required:

- `knowledge/wallets/0x1cc16713196d456f86fa9c7387dd326a7f73b8df.md`
- `knowledge/wallets/0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee.md`
- `knowledge/wallets/0x22e4248bdb066f65c9f11cd66cdd3719a28eef1c.md`
- `knowledge/wallets/0x5bffcf561bcae83af680ad600cb99f1184d6ffbe.md`
- `knowledge/wallets/0x8493374739b45eb032382857feeb7ed025215366.md`

### Phase D — skipped

No DB or brain-memory analyzer-output rows existed in inventory, so no storage
rewrite was needed.

### 7.1 Residual caveats

1. The five `unknown_needs_inspection` dossiers were opened in Phase C.
   They cite only legacy `user_analysis`/profile fields or stub metadata,
   not wallet-protocol H2/H3/H5/time-split numerics. No rerun was promoted.

2. The exact extent of NO-held content in each medium-risk wallet was not
   measured. That is acceptable because the close-read question was whether
   persisted dossier prose cited `_is_win`-derived protocol numbers; it did not.

3. The fixture `tests/replay/fixtures/swissmiss_fetch_snapshot.json` uses
   `currentPrice` (camelCase) but `held_analyst.py:129` reads `curPrice`.
   Field-name mismatch means `_is_win` never fires on those rows under
   the `_at_resolution` filter. Marked `none` / `no_action`. Assumption:
   no other code path silently aliases these field names. Verified by
   inspecting `held_analyst.filter_held_to_resolution` and `_is_win`;
   not exhaustively checked across the entire wallet-protocol module.
