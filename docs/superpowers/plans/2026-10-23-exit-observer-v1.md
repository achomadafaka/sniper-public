# exit-observer-v1 implementation

Forward-only measurement of the frozen death-candidate definitions. No actions, rail changes, model gating, providers, future outcomes or trading-ledger/env writes. No runtime activation in this branch. Fixed funded limits remain 5 open / $25 clips / $200 daily halt / -$200 cumulative kill.

Source contracts: parent death prereg c74aeaa7b section 3, executable-V repair f732935a4, forward prereg 08aed890e and amendments 5ff5170ba/e97ca0a64/38af498e3/53fe28e82/39280a39d; Beta owns the cohort validator/provisioner. The frozen observer key is {version:exit-observer-v1, grid:[2,3,4,6], baseline_drop_pct:10.0}; absent disables it. Population is labelled per record. Combinations are computed offline, never in the observer.

Edge map before edits:
- Every read stays in the existing shadow process with read-only ledger/source handles. No entry, signing or funded sweep call/lock/retry. Reuse the existing RISK shadow CLI and position loader; an explicit observer mode bypasses the SDK.
- Missing quote atomic quantity, stale/misbound quote, unknown partial quantity, missing fee component, missing own signature, wrong venue/order or capture discontinuity remains unknown. Zero is distinct from absent. A fee-only value drop must remain visible even if spot price is flat.
- Actual reserves already include our buy: quote current residual quantity directly; never add hypothetical qn again. Apply one residual quantity consistently to historical states. Raw native SOL value and exact USDC unit quotes/persisted peaks remain separate.
- Exclude own signatures from flow/buyer/seller/activity and noise inputs. ONE_SWAP_DROP skips our destination event but keeps factual predecessor state. Complete launch capture is required for holder/first-swapper proxies; bounded retained history never invents it.
- Graduation/PS, malformed/partial data, late observations, startup and restart are explicit censor reasons. A peak not proven from the fill is left-censored. The worker logs evaluations only; offline grading identifies first-drop/new-peak events. Repeated evaluation clocks cannot duplicate a row.
- Each eligible open position gets an audit row including unknowns. Record the frozen version/config hash, cohort, clocks/cut, atomic quantity as decimal TEXT, population, all k flags/reasons, noise definition, value and peak/censoring. Observer data has no engine action/expiry columns or consumer.
- Writes are only to the observer DB, transactions atomic. Rollback disables new observation; existing logs and funded rails stay intact. No automatic source index build or migration on retained live data.

Does not handle: PumpSwap executable candidates, missing historical cashback fees, unavailable complete launch history, on-chain holder identity, model/default-exit policy, live sellability or profitability validation. Those remain censored, never false.

Implementation:
1. Pure core module and RED/GREEN tests for each indicator/grid, exact native sell math/fee rounding, no synthetic buy twice, missing data, own fills, closed-window/noise semantics and partial quantity consistency.
2. Bounded read-only curve adapter reusing capture proof and source helpers; preserve all fee/order/quantity fields and honest launch coverage. Bind exact atomic input from the latest retained exit-decision evidence to current identity/residual quantity and both quote clocks.
3. Existing shadow CLI observer mode, frozen cohort lookup on its existing read-only connection, separate observer persistence with a durable enrollment boundary and evaluation identity. No SDK call in observer mode; legacy shadow behavior unchanged.
4. Focused tests -n 2 with the shared host lock, synthetic offline timing, independent eCat/Gamma review on a fixed local SHA. No full suite, outcome reads, push or deployment.

Implementation boundaries:
- `scripts/memecoin_desk_risk_shadow.py --exit-observer --once|--loop` reuses the RO loader. Position, quote, completed-partial clocks, own signatures and frozen cohort are read in one short SQLite snapshot. No model adapter import. Separate lock/DB; kill knob `MEMECOIN_DESK_EXIT_OBSERVER_ENABLED=0`.
- Missing signatures on any real fill for the mint make exclusion unproven. Both quote clocks must follow the latest completed partial; a matching rounded float is insufficient proof alone. Exact atoms remain quote-time balance evidence, not an independent current balance read.
- `exit_observer_meta` freezes first start; `exit_observer_population` counts seen enabled/disabled open/closed positions without reading outcomes. Pre-start positions, identity changes and uncovered fill peaks cannot claim an uncensored baseline. Current residual quantity is applied consistently to every historical state; partials remain eligible, with quantity changes recorded separately. A 500-row metadata-only primary-key cursor catches positions closed between polls/outages as enrolled unknowns, without reading PnL or inventing historical observations. Census backlog is explicit; old pre-start closed positions are outside the forward population.
- `exit_observations` stores per-evaluation flags, unknown reasons, native SOL model values, fees, clocks, quantity and config hash. Retained USDC quote and persisted USDC unit peak are separate. Nonfinite raw quote/fee values are NULL with invalid-field names, preserving the denominator. There are no action/rails/expiry columns or engine consumer.
- ONE_SWAP_DROP is sell-only per amendment 3. A fee jump on a buy may trigger STRUCT_BREAK but never becomes a ONE_SWAP_DROP candidate.
- Reads are capped at one hour/20,000 rows and connection-local SQLite work. Fill coverage older than this window remains censored. Sampling gaps are visible through evaluated_at/cut/previous_cut; the worker does not claim retrospective first-event timing or produce future labels.

The included confirmatory preregistration is copied byte-for-byte from Gamma 39280a39d (amendment 4). The enrollment boundary is the first durable observer-start metadata row, before the first eligible position; the observer writes no future labels. Gamma reviewed the core and worker at 378bfee12 with no measurement blocker; no activation is part of this work.

Rollback: stop only the observer process or set its kill knob; keep its immutable audit data. No funded state, provider use, rails or reservations change.

Verification evidence (offline fixtures):
- Core: 49 tests; reader: 75 tests. Worker regressions cover frozen enrollment/restart, missing closed positions, bounded census backlog, same-snapshot concurrent quote publication, missing fill signatures, latest aware partial time, malformed quote/fee audit values, explicit CLI and kill-before-I/O.
- Core timing: 120 rows p50 0.916 ms / p90 0.996 ms; 20,000 rows p50 136.295 ms / p90 143.790 ms. Ten five-position worker passes (120 rows each), core + audit persistence with injected source/ledger reads: p50 7.061 ms / p90 7.386 ms. These do not measure funded-entry latency or live source load.
- Separate deployment-gate repair ce1af029b (local equivalent a2d4c897f) was independently reviewed by eCat: 111 focused tests. No production watermark/as-of behavior changed; a stale synthetic fixture now advances coverage only on real block frames.

Final local review at implementation commit 120b54805:
- 379 focused tests passed in 13.62 s with `-n 2` and the shared focused lock; schema reference and funded read-only connection hooks passed. Generated dependency-index change records the new cohort read only.
- Independent core/reader cross-review findings have regression witnesses and repairs. Fresh research-methodologist review: implementation PASS, no material code findings; this is not Gamma approval.
- Before any outcome grading, Gamma must reconcile coin-versus-position clustering in prereg sections 1/4 and the multiple-testing treatment across six indicators plus two combinations. These affect inference, not observation; the frozen prereg has not been changed here.
- Gamma completed the final core/worker source review at 378bfee12 with no measurement blocker; he did not review the adapter/tests or rerun tests. eCat independently passed the final source tree; adapter/core cross-review and focused tests cover those boundaries. No runtime activation, funding claim or trading-policy change is authorized by a review report.

Final census-label repair: disabled cohorts remain counted with `enabled=0,enrolled=0`; both open and missed-close paths require frozen enablement plus time eligibility. One regression was RED before the three-line fix; all 26 worker tests pass afterward. Fresh methodology review of this diff passed; indicator/reader code is unchanged from 120b54805.

Gamma amendment 4 (39280a39d) resolves the grading questions before any observation: events use logged cuts, exclude gaps over 10 s, cluster by coin, and use a family-wise one-sided level 0.05/8 across six indicators plus two combinations. The frozen document is copied unchanged; no grading or outcome reads were performed. The observer code remains exactly 378bfee12.
