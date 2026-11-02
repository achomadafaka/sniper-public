# Desk v28 partial observed flow

Gamma freeze6a97dfe59, read amendmentsbf297fdb1/61fdbd3d8 and execution amendment45895eb56; Alpha authorized.
Delta owns ps_facts.py, ps_losses.py receipt diagnostics, structure.py vocabulary, focused tests/this plan.
Codex owns contract/playbook/cohort/service/release. Zeta reviews.

Acceptance: pumpswap-facts-v2 carries causal seen ordinary flow with coverage, partial flag, distinct disjoint unknown/global/own-pool receipt counts and loss_classes_exclusive:true. Totals are lower_bound:true across all causal seen swaps in60s; covered_s is the continuous proven-complete suffix. No seen swaps or unreadable tape => null; zero coverage with seen totals stays visible (Amendment3). Partial chart retains observations with aligned per-bar coverage; uncovered empty demand never zero-filled. Safety, complete price-bound checks, ours/agent/price-only exclusion and original losses unchanged.

Edge cases: zero/unknown coverage; sparse/no swaps; multiple receipts at one time; duplicate attested batches/members; legacy gaps; malformed/future scope; indexed cap failures; seed cut; per-bar holes; same-row/clock/proof reuse without cross-sheet cache. No RPC/model/production write/funded path. No completeness or executable-edge claim. No activation before freeze and release review.

TDD: focused partial-flow/count/chart cases must fail before source changes. Existing semantics assertions change only where v28 explicitly amends them; v1 historical pinned release remains its own stratum.

Implementation: v2 flow extends existing buy_n/sell_n/usd/makers/largest_buy with buyers/sellers/n_swaps, lower_bound:true, covered_s, partial and the three disjoint receipt counts plus loss_classes_exclusive:true. The facts version identifies these semantics. Source/legacy gaps use gap rowid; decoder receipts use(signature,cause); attested wrappers excluded before counting. Classified receipt details reuse the same bounded SQL read and per-sheet memo; no new DB query per receipt or cross-sheet cache.

Chart remains24x8 scalar bars; aligned chart_coverage gives covered_s/partial per bar. Observed demand survives a partial bucket; an empty uncovered bucket stays null. Descriptive windows blinded by the old three-loss threshold recover seen totals, with path-dependent price metrics left unknown. Other structure estimators/rails remain conservative. Complete price-bound fields still require complete capture. Inferred reserve continuity breaks shorten coverage but are not fabricated into retained receipt counters.

RED evidence: initial3cases failed (missingv2/receipt details/partialflow); Amendment3 then3zero-coverage/no-observation cases failed; inferred continuity and disjointcapture-suffix checks each failed before repair. Final focused affected set189passed7.16s; contract/input/structure compatibility45passed2.72s. No full suite. Updated v1-era assertions only for the explicitly changed null/partial semantics.

Timing:12interleaved calls/version on six-row temporarySQLite fixture with3sourcegaps; old p50/p90=1.668/1.875ms, new=1.966/2.148ms. Fixture only; no production latency/speedup claim. No provider/model/runtime/production-state changes. Raw research report remains untracked.

Limits: receipt counters describe retained receipts, not an estimate of the number of missed swaps. covered_s is a conservative continuous proof suffix, not the duration of all observations or a sum across capture holes. A seen agent-only window can have zero ordinary flow after the unchanged agent exclusion. Historical v1 results retain their original version/stratum.

Compatibility seam approved by Alpha/Codex: facts_for(..., version='pumpswap-facts-v1') remains default. V28 must explicitly pass version='pumpswap-facts-v2' from the frozen policy. Both versions validate under their own closed flow shapes. V1 keeps prior coverage sums/null behavior/window/chart shape. Five byte-equivalence cases against7a6eea169 source passed:complete,partial,unselected,quiet,loss. Codex owns version bindings in contract/provisioner/record_decision/JUDGE/paper/risk; Delta does not edit those.

Final boundary REDs: covered_s=False accepted as0 before numeric check; empty fully-covered chart bar showed synthetic zeros before no-seen masking; default/explicitv1/version rejection cases failed before compatibility branching. Final focused suite239passed8.03s with-n2, diff check clean. Final fixture benchmark12interleaved calls:old p50/p90=1.762/1.808ms,newv2=2.046/2.090ms. No production speed claim. No usable seen swap gives null bar demand even if coverage is complete; an agent-only seen bar retains the unchanged ordinary-flow exclusion.
