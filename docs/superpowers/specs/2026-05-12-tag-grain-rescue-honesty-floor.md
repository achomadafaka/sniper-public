# Tag-Grain Rescue Honesty Floor (Caveat-Only Slice)

**Date:** 2026-05-12
**Status:** spec draft for /ship pipeline
**Severity:** doctrine — strategy-intelligence honesty bar
**Related:** Issue #24 / PR #31 (negative-PnL honesty floor)
**Triggered by:** operator-curated 5-wallet pass (2026-05-12), SwissMiss
case — branch promoted on a single `politics/nav` tag-grain support while
every primary hypothesis's time-split holdout failed.

---

## Problem

In `_branch_verdict` (sniper/analysis/wallet_protocol/analyze_wallet.py:507),
the held / flipper branch is promoted to `supported` when **any** of the
following is true:

1. A primary hypothesis (`HELD_PRIMARY_TESTS` / `FLIPPER_PRIMARY_TESTS`) is
   `supported` AND survives its time-split (or has no time-split).
2. A primary hypothesis is `supported` but its time-split fails — **rescued**
   when another primary is supported **or** at least one tag grain is
   supported (analyze_wallet.py:548–569).
3. No primary is supported, but at least one tag grain is — branch promotes
   purely on tag-grain evidence.

Cases (2) and (3) above let a single supported tag grain rescue a branch
whose wallet-level OOS evidence is entirely absent. This is doctrinally
weak for strategy-intelligence output: the report renders `edge_detected`
when no wallet-level signal survived out-of-sample testing.

Live evidence from the 2026-05-12 calibration pass (`/tmp/calibration-pass-2026-05-12/04-swissmiss.json`):

- Wallet `0xdbade4c8...a9c95` (SwissMiss)
- `held_analyst_branch.hypotheses`: H1=supported, H2=supported, H2_sub080=inconclusive, H3=inconclusive, H4/H6=underpowered
- `held_analyst_branch.time_split`: H2.holdout_status=`failed`, H2_sub080.holdout_status=`failed`, H3.holdout_status=`failed`
- `held_analyst_branch.tag_grains`: 1 entry with `grain=nav`, `tag=politics`, `verdict=supported`
- Result: `overall_verdict="edge_detected"`, `primary_edge_type="entry_timing"`, no caveat surfaced

The verdict is not wrong by current code, but the report does not warn the
operator that the edge claim is **tag-local, not wallet-level OOS-validated**.

---

## Fix (caveat-first; no verdict mutation in this slice)

Add a **branch-level honesty caveat** that runs after `_apply_negative_pnl_honesty_floor`
(analyze_wallet.py:1007) and emits a structured `data_quality_flags` entry
plus an `applicable_limitations` line when a branch was promoted by tag
grains alone (cases 2 or 3 above). **No verdict downgrade in this PR.**

### Predicate

A branch is "promoted on tag grain only" iff all three hold:

```text
branch_promoted_on_tag_grain_only(branch) :=
    branch.is_active == True
    AND _branch_verdict(branch) == "supported"
    AND at least one g in branch.tag_grains has g.verdict == "supported"
    AND no primary hypothesis is BOTH "supported" AND "did not fail OOS":
        for each h in branch.hypotheses where h.name in primary_set(branch):
            "did not fail OOS" iff h.verdict == "supported" AND (
                branch.time_split.get(h.name) is None
                OR branch.time_split[h.name].holdout_status != "failed"
            )
        no h satisfies "did not fail OOS"
```

`holdout_status` is `Literal["survived", "failed", "underpowered", "not_attempted"]`
(types.py:164). The predicate matches the existing veto convention
(analyze_wallet.py:560) — only `"failed"` is treated as OOS failure; all
other values (including `"underpowered"` and `"not_attempted"`) mean the
hypothesis did not fail OOS and therefore disqualifies the report from
this caveat.

`primary_set(branch)` is `HELD_PRIMARY_TESTS` for the held branch and
`FLIPPER_PRIMARY_TESTS` for the flipper branch. `H2_sub080` is included in
`primary_set` only when it would be promoted into primary consultation by
the ADR-002 path (analyze_wallet.py:526–534) — i.e., H2 refuted AND H2_sub080
present AND H2_sub080.verdict not in {underpowered, n/a}.

### Emission

When the predicate fires for a branch:

```text
flag_string := f"BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY: {branch.branch} "
               f"(grain={top_grain.grain}/{top_grain.tag}, "
               f"primaries_failed_oos={sorted(failed_primary_names)})"

# `top_grain` = first supported tag_grain in deterministic order
# (sorted by (grain, tag) tuples for stable output)
# `failed_primary_names` = names of primaries that had h.verdict=="supported"
# AND time_split[name].holdout_status=="failed"; empty list when case (3)

report.data_quality_flags.append(flag_string)
report.applicable_limitations.append(
    f"Branch '{branch.branch}' promoted to supported on tag-local "
    f"evidence ({top_grain.grain}/{top_grain.tag}) only; no primary "
    f"hypothesis survived out-of-sample holdout. Edge claim is "
    f"category-local, not validated by wallet-level OOS testing."
)
```

### When NOT to fire

- Branch inactive (`branch.is_active == False`) → never fires for that branch.
- Branch verdict is not `supported` → never fires (per current `_branch_verdict`,
  rescue logic only matters when the branch promotes).
- At least one primary hypothesis is `supported` AND its time-split is not
  `failed` (i.e., survived OOS) → wallet-level evidence exists; tag grain is
  bonus, not the sole pillar. No flag.
- Branch has no `tag_grains` with `verdict=="supported"` → there's nothing
  to credit the promotion to. The branch must be promoting on some primary
  (or this would be a `_branch_verdict` bug, not a tag-grain rescue).

### Stacking with the negative-PnL floor

The two floors are orthogonal observations:

- `NEGATIVE_PNL_EDGE_UNEXPLAINED` (Issue #24): report-level — net_pnl≤0 +
  no explanatory-surface coverage. Mutates `overall_verdict`.
- `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` (this spec): branch-level — branch
  promoted but no primary survived OOS. Does **not** mutate `overall_verdict`.

When both apply to the same wallet, both flags MUST be emitted. The
negative-PnL floor runs first (analyze_wallet.py:1007) and may set
`overall_verdict` to `underpowered`. The tag-grain caveat runs after and
emits independently of the current `overall_verdict` — it reflects what
the branch resolved, not what the report-level verdict ended up at.

### Branch coverage

Both branches are eligible. The predicate evaluates each active branch
independently; the flag is branch-tagged so the operator can tell which
branch was rescued. Mixed-mode wallets can therefore emit two flags
(one per branch) when both rescues fire.

---

## Hard scope guards

The implementation must NOT:

- Change `_branch_verdict` logic. The tag-grain rescue in
  analyze_wallet.py:540–541, 548–551, 567 stays exactly as-is.
- Mutate `overall_verdict`, `primary_edge_type`, `copyability_note`, or
  `gate_result`. This slice is caveat-only.
- Mutate any hypothesis verdict, time-split outcome, tag-grain verdict, or
  branch_verdict computation.
- Add new surface compute calls, price-history lookups, or live API calls.
- Touch the negative-PnL floor implementation (`_apply_negative_pnl_honesty_floor`,
  the three coverage predicates, `NEGATIVE_PNL_EDGE_UNEXPLAINED`).
- Emit copy / trigger / simulator / forward-prediction language anywhere
  in the flag string or the limitation line.
- Add new fields to `ProtocolReport` or `BranchReport`. Only existing
  `data_quality_flags: list[str]` and `applicable_limitations: list[str]`
  are appended to.

---

## Acceptance criteria

1. `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` constant lives at module-level in
   `analyze_wallet.py` next to `NEGATIVE_PNL_EDGE_UNEXPLAINED` (line 60).
2. A new helper `_branch_promoted_on_tag_grain_only(branch)` returns the
   predicate boolean. A new `_apply_tag_grain_rescue_caveat(report)`
   helper iterates the two branches and emits the flag + limitation when
   the predicate fires.
3. `_apply_tag_grain_rescue_caveat` is called from the verdict-assembly
   path immediately after `_apply_negative_pnl_honesty_floor`
   (analyze_wallet.py:1007).
4. Strategy Pattern Audit (`sniper/analysis/strategy_pattern_audit/core.py`,
   `_evaluate_rule`) treats `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` as a
   `surface_quality` caveat: a report carrying this flag CANNOT have a
   rule transition to `MATCHES`. The rule evaluation returns `INCONCLUSIVE`
   with `reason="tag_grain_only_evidence"` instead. The flag is checked
   alongside the existing `_surface_quality_caveats` path.

---

## Test matrix (write all of these as failing tests in Phase 1)

### Predicate-direct tests (held branch)

| # | Case | H verdicts (held) | time_split holdout_status | tag_grain supported? | Predicate result |
|---|------|-------------------|---------------------------|----------------------|------------------|
| 1 | SwissMiss-shape | H2=sup, H3=inc | H2=failed | yes (politics/nav) | **TRUE** |
| 2 | 6a72-shape | H3=sup | H3=failed | no | FALSE (branch did not promote — see test #21) |
| 3 | Pure-grain promotion | H2/H3=inc | n/a | yes (entertainment/leaf) | **TRUE** (case 3) |
| 4 | Healthy edge | H2=sup | H2=`survived` | yes | FALSE (primary survived OOS) |
| 5 | Healthy edge no grain | H2=sup | H2=`survived` | no | FALSE |
| 6 | Two primaries, one survives | H2=sup, H3=sup | H2=failed, H3=survived | yes | FALSE (H3 survived) |
| 7 | Two primaries, neither survives, grain supported | H2=sup, H3=sup | H2=failed, H3=failed | yes | **TRUE** |
| 8 | Time-split absent | H2=sup | (no entry for H2) | yes | FALSE (no failure to rescue from) |
| 9 | ADR-002 H2_sub080 path | H2=ref, H2_sub080=sup | H2_sub080=failed | yes | **TRUE** (sub080 in primary set per ADR-002) |
| 10 | Branch inactive | branch.is_active=False | n/a | n/a | FALSE |
| 11 | Branch not promoted | H2=sup, time-split=failed, no other supports | H2=failed | no | FALSE (`_branch_verdict` returns `inconclusive`, predicate guard) |
| 12 | Only tag_grain inconclusive | H2/H3=inc | n/a | grain=inconclusive | FALSE |
| 12a | Two primaries mutually rescue, no grain | H2=sup, H3=sup | H2=failed, H3=failed | no | FALSE (no supported grain — out of scope; see Out of scope §) |
| 12b | Holdout_status="underpowered" not "failed" | H2=sup | H2=`underpowered` | yes | FALSE (only `"failed"` disqualifies; `"underpowered"` means did-not-fail) |

### Predicate-direct tests (flipper branch)

13. Mirror test #1 with `flipper` branch and `FH1`/`FH2`/`FH4`: predicate TRUE.
14. Mirror test #4 with flipper: predicate FALSE.

### Emission shape tests

15. When predicate TRUE, `data_quality_flags` gains exactly one entry
    starting with `"BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY: held_analyst "` (or
    `"flipper "`). Existing flags preserved.
16. `applicable_limitations` gains exactly one entry, mentioning the
    branch name AND the supporting grain identifier. Existing limitations
    preserved.
17. Flag string contains `grain=<grain>/<tag>` and `primaries_failed_oos=[...]`
    in deterministic order (sorted tag-grain tuple, sorted primary names).
18. With two supported grains, only one is named in the flag string (the
    first in sort order). All other grains preserved verbatim in the branch.

### Doctrine + scope guards

19. `report.overall_verdict` is NEVER mutated by this floor — equal before
    and after `_apply_tag_grain_rescue_caveat`.
20. `report.primary_edge_type` is NEVER mutated.
21. `report.copyability_note` is NEVER mutated by this floor (the negative-PnL
    floor's copyability_note write is independent).
22. `report.gate_result` and `report.gate_reason` are NEVER mutated.
23. Per-hypothesis verdicts, time-split outcomes, tag-grain verdicts, and
    independence/stationarity blocks are NEVER mutated.
24. No new field is added to `ProtocolReport` or `BranchReport` (only
    appends to existing list fields).
25. Forbidden output keys / phrasing: the flag string and the limitation
    line emit NO copy / trigger / simulator / forward-prediction strings.

### Stacking with negative-PnL floor

26. Pestle-shape report (overall_verdict=`edge_detected` pre-floor,
    net_pnl<0, zero explanatory coverage) → negative-PnL floor downgrades
    to `underpowered` and appends `NEGATIVE_PNL_EDGE_UNEXPLAINED`. If
    the same report also has a tag-grain-only branch promotion, the new
    flag is appended ALSO. Both flags appear in `data_quality_flags`.
27. Pestle without a tag-grain-only branch (the live case): only
    `NEGATIVE_PNL_EDGE_UNEXPLAINED` appears. `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY`
    must NOT fire.
28. SwissMiss-shape report with positive net_pnl + tag-grain-only
    promotion: negative-PnL floor does NOT fire; tag-grain caveat DOES fire.
    `overall_verdict` stays `edge_detected`.

### SPA caveat integration

29. SPA: a `ThesisRule` whose `pattern_predicate` would otherwise return
    `MATCHES` on a report DOES NOT match when that report's
    `data_quality_flags` contains `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY:` (any
    branch). The evaluation state becomes `INCONCLUSIVE`,
    `reason="tag_grain_only_evidence"`, with the flag string surfaced in
    `coverage_caveats`.
30. SPA: a report with `NEGATIVE_PNL_EDGE_UNEXPLAINED` (no tag-grain
    flag) follows pre-existing behavior (the negative-PnL flag is not
    given new semantics in this PR).
31. SPA: a report carrying both flags is `INCONCLUSIVE` with both flags
    surfaced; the rule is not promoted to `MATCHES` and not silently
    `REJECTED` (the `tag_grain_only_evidence` reason is preferred over
    `no_match`).

### Live-cohort fixture replay

32. Loading `/tmp/calibration-pass-2026-05-12/04-swissmiss.json` (live
    SwissMiss capture, 2026-05-12) through the floor → flag fires for
    `held_analyst` branch with `grain=nav/politics`.
33. Loading the live 6a72, YatSen, and Pestle captures → flag does NOT
    fire on any of them (no supported tag grain).

---

## Files in scope

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | Add `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` constant near line 60; add `_branch_promoted_on_tag_grain_only` and `_apply_tag_grain_rescue_caveat` helpers; call from verdict-assembly path after `_apply_negative_pnl_honesty_floor` at line 1007. |
| `sniper/analysis/strategy_pattern_audit/core.py` | Extend `_evaluate_rule` (or `_surface_quality_caveats`) so that a report carrying `BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY` (any branch) cannot transition to `MATCHES`; instead returns `INCONCLUSIVE` with `reason="tag_grain_only_evidence"` and the flag in `coverage_caveats`. |
| `tests/test_wallet_protocol_tag_grain_rescue_floor.py` (new) | Predicate, emission shape, scope guards, and stacking tests #1–28. |
| `tests/test_strategy_pattern_audit_tag_grain_caveat.py` (new) | SPA integration tests #29–31. |
| `tests/test_wallet_protocol_calibration_live_2026_05_12.py` (new, optional) | Tests #32–33 fixture replay if the JSON captures are checked in or copied into a `tests/fixtures/` location; otherwise this becomes a manual-replay step in §Verification. |

No other file should be touched in this PR. Specifically: no changes to
`types.py`, `report.py`, `serializers.py`, `web_settings.py`, or any
other test file outside the new test files above.

---

## Verification (after Phase 2 lands)

```bash
python3 -m pytest tests/test_wallet_protocol_honesty_floor.py \
                  tests/test_wallet_protocol_tag_grain_rescue_floor.py \
                  tests/test_strategy_pattern_audit_tag_grain_caveat.py -q
python3 -m pytest tests/test_strategy_pattern_audit_core.py \
                  tests/test_strategy_pattern_audit_seed_rules.py -q
python3 -m pytest tests/test_wallet_protocol_edge_surface.py \
                  tests/test_wallet_protocol_exit_attribution.py \
                  tests/test_wallet_protocol_completeness_surface.py \
                  tests/test_wallet_protocol_timing_surface.py \
                  tests/test_wallet_protocol_position_building.py \
                  tests/calibration/ -q
python3 scripts/validate_schema_refs.py
```

Expected: all green; calibration fixtures don't regress; existing
`edge_detected` verdicts on wallets with surviving primary OOS continue
to emit without the new flag; SwissMiss-live-shape fires the new flag;
Pestle still emits only `NEGATIVE_PNL_EDGE_UNEXPLAINED`.

Live verification (post-merge, manual):

```bash
python3 scripts/run_wallet_protocol.py 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \
  --json --no-persist --n-iter 500 --no-history --no-exec | \
  python3 -c "import json,sys; r=json.load(sys.stdin); print(r['overall_verdict'], r['data_quality_flags'])"
```

Expected: `edge_detected ['DW_EW_DIVERGENCE: ...', 'BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY: held_analyst (grain=nav/politics, primaries_failed_oos=[H2])']`.

---

## Out of scope (track as future work, NOT in this PR)

- **Downgrade decision.** Whether tag-grain-only promotion should
  eventually downgrade `overall_verdict` (analogous to the negative-PnL
  floor) is deferred. The downgrade question depends on cohort base rates
  for this flag — too rare to be load-bearing vs too common to be
  informative. Re-evaluate after 30+ wallets have been processed and the
  flag's fire-rate is known.
- **Tag-grain peer-baseline strength tier.** The H5 / FH4 tag-grain test
  has a `confidence_flag` field (`ok` / `low_population`) that could
  partition the caveat by peer-baseline robustness. Not in this slice;
  the current flag treats all supported grains as equally "tag-local."
- **Multi-grain weighting.** When more than one grain supports, the flag
  names only the first in deterministic order. A weighted summary (or
  listing all supporting grains) is a later refinement.
- **Flipper-side fixture.** The live cohort had zero flipper-active
  wallets (all routed `held_analyst_only` on low flip_rate). Synthetic
  flipper fixture tests are included (test #13/14), but a live-flipper
  validation pass is deferred.
- **6a72-style "supported primary, time-split failed, no grain" cohort
  analysis.** That's a different question — what happens when wallets
  have wallet-level signal in-sample but it fails OOS — and is the
  natural downgrade-policy follow-up for this floor. Out of scope here.
- **Two-primaries-mutual-rescue (no grain).** Under current `_branch_verdict`
  logic (analyze_wallet.py:555–569), if two primaries are both `supported`
  in-sample and both fail OOS holdout, the branch still promotes —
  each primary acts as the "another supported primary" that disables
  the other's time-split veto, with no tag grain involved. This spec
  does NOT flag that case. Whether in-sample-only mutual rescue is
  doctrinally acceptable is a separate honesty-bar question; the
  predicate here requires `at least one grain.verdict == "supported"`
  precisely to keep this PR scoped to the tag-grain question. See
  test #12a for the explicit guard.
