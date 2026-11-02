# Wallet Universe Strategy Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Compare wallet/coin-setup policy evidence on matched forward origins and expose actual blockers without auto-changing funded policy.

**Architecture:** Offline evidence summaries reuse frozen paper policies, immutable entry snapshots/contracts, strict origin matching and existing refusal capture. Existing funded selector remains unchanged. The Map displays diagnostic comparisons and a frozen forward-validation manifest alongside current effective rails.

**Tech Stack:** Python/SQLite read-only analysis, existing JSON producer and Map JavaScript, pytest/browser fixtures.

**Spec:** `docs/superpowers/specs/2026-10-02-wallet-universe-design.md`.

## Global Constraints

- No extra synchronous call/lock/retry is allowed on the entry path.
- No DexScreener prices may enter fill evidence.
- No automatic arming or filter exemptions. Unknown stays unknown.
- Policy search is training; subsequent unchanged paper is validation.
- Preserve current strict-selector outputs separately from the survivor “best arm.” Never borrow evidence from another chain.
- Existing funded contracts and current per-wallet chain overrides remain authoritative; no policy apply endpoint is added.

## Review Focus

1. Missing/open/unpriced/duplicate arm rows cannot disappear from the origin denominator — Task 1.
2. Policy/cohort changes or late setup enrichment cannot be treated as entry-time knowledge — Task 1.
3. Shared coins and one dominating coin cannot masquerade as independent wallet wins — Task 1.
4. Missing gate inputs or bar-modeled refusal outcomes cannot be called executable evidence — Task 2.
5. Selecting a paper adjustment cannot mutate funded authority/contracts — Task 2.

---

## File responsibilities

- Create `sniper/services/wallet_setup_evidence.py`: pure strict evidence summaries and provenance joins.
- Modify `scripts/research/wallet_follow_funnel.py`: offline read-only joins into Task 2 Map projection `setups` field.
- Modify `static/wallet_map.js`: setup/policy comparison and blockers; reuse existing UI components.
- Create `tests/test_wallet_setup_evidence.py`; extend existing UI runtime fixtures.
- Reuse `memecoin_paper_report` strict grouping, `memecoin_policy`, refusal-capture classifications and immutable snapshots. No funded selector change.

### Task 1: Strict whole-origin setup comparisons

**Interfaces:** `summarize_setup(rows: list[dict], *, chain: str, uid: str, setup: str, arms: list[str], cutoff: str) -> dict` returns `origins`, `complete_origins`, `open_origins`, `censored_origins`, `missing_arm_origins`, `duplicate_origins`, `distinct_coins`, and `policies: dict`. Input rows contain `origin`, `chain`, `uid`, `mint`, `setup`, `cohort_id`, `policy_id`, `arm`, `known_at`, `opened_at`, `closed`, `censored`, `notional`, `net_pnl`. Each policy result contains `notional`, `net_pnl`, `return`, `top_coin_share`, `leave_best_coin_out_return`; empty priced population returns null metrics. Net costs unavailable means net result unavailable; never silently substitute gross. Output also reports full-book priced/open/censored totals separately from complete-origin comparisons.

- [x] **Step 1: Write red paired-origin tests.**

```python
def test_missing_arm_is_retained_as_an_evidence_gap():
    from sniper.services.wallet_setup_evidence import summarize_setup
    row = dict(origin='o1', chain='solana', uid='A', mint='M', setup='pump-native',
               cohort_id='c1', policy_id='p1', arm='fixed',
               known_at='2026-10-01T00:00:00+00:00', opened_at='2026-10-02T00:00:00+00:00',
               closed=True, censored=False, notional=25, net_pnl=5)
    out = summarize_setup([row], chain='solana', uid='A', setup='pump-native',
                          arms=['fixed', 'trail'], cutoff='2026-10-01T12:00:00+00:00')
    assert out['origins'] == 1 and out['complete_origins'] == 0
    assert out['missing_arm_origins'] == 1
    assert out['policies']['fixed']['return'] is None
```

Test unequal notionals (sum PnL/sum notional, not mean percentage), multiple buys in one mint, overlapping wallet coins, two chains with same uid, duplicate arm row, one arm open/censored, changed cohort/policy id, unknown fees, selection cutoff and setup known after entry. Leave-best-coin-out removes all origins for that coin; if none remain, return null. Show each selected policy's cohort separately; never pool policy versions to fill sample size.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_setup_evidence.py -q`.
- [x] **Step 3: Reuse the existing strict group matcher in a read-only adapter.** `memecoin_paper_report._project_rows(conn, chain)` and `_exact_groups(rows, arms, forward=True)` already enforce shared origin/cohort membership; inspect their current schema before adapting. Preserve existing funded-selector behavior with its unchanged tests. Apply closed/priced eligibility across the whole origin, retain all rejected origins with typed reason, and join setup only via exact immutable entry identity. Use `memecoin_entry_snapshots`, frozen funded contract and exact source episode; missing joins remain unknown. Core weighted metric:

```python
notional = sum(r['notional'] for r in complete_rows)
net_pnl = sum(r['net_pnl'] for r in complete_rows)
result = None if notional <= 0 else net_pnl / notional
```

Group concentration by chain/mint (or proven shared launch episode), not wallet-row count. Produce contemporaneous current-roster comparisons only for matching setups/windows; uncertainty uses independent coin clusters and is unknown if independent support is inadequate. Do not introduce a new weighted score or funding threshold. Freeze shortlist selection/arms/setup predicate and cutoff into an explicit validation manifest; only later observations form validation evidence. Existing paper versus Observation's quote-grid model remain separate cohorts.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_setup_evidence.py tests/test_memecoin_paper_report.py tests/test_memecoin_copy_arm.py tests/test_wallet_follow_gate_matched.py -q`. Compare existing funded selector output before/after on the same fixture; it must match exactly.
- [x] **Step 5: Commit.** `git add sniper/services/wallet_setup_evidence.py scripts/research/wallet_follow_funnel.py tests/test_wallet_setup_evidence.py && git commit -m 'feat(wallet-map): compare strict setup-matched paper evidence'`.

### Task 2: Blockers and reviewable paper adjustments

**Interfaces:** `blocker_evidence(attempts: list[dict], captures: list[dict], *, origin: str) -> dict` in `wallet_setup_evidence.py` returns `{origin, actual_gate, inputs_state, capture_state, outcome_kind, affected}`. Outcome kind `exact_quote|bar_model|unknown`; inputs state `complete|partial|unknown`. `freeze_adjustment(*, chain: str, uid: str, cutoff: str, setup: str, arms: list[str], change: dict) -> dict` produces a descriptive validation manifest only, containing one selection-gate change, no safety exemptions. Browser consumes Task 1 `setups` and these fields; current strict selected policy/current exit override/band fallback/frozen funded contract are labeled separately.

- [x] **Step 1: Add red blocker and immutability tests.**

```python
def test_bar_refusal_model_is_not_executable():
    from sniper.services.wallet_setup_evidence import blocker_evidence
    out = blocker_evidence([{'origin': 'o1', 'gate': 'mcap', 'inputs_state': 'partial'}],
                           [{'origin': 'o1', 'kind': 'bar_model', 'state': 'ok'}], origin='o1')
    assert out['outcome_kind'] == 'bar_model'
    assert out['inputs_state'] == 'partial'
```

Add exact-origin collision/retry dedup, missing attempt, infrastructure failure versus token refusal, missing capture, one-change manifest and non-exemptible safety/identity/route/reserve/signing/close-claim controls. UI runtime fixture selects and exports a proposal; spy asserts zero funded mutation POSTs and byte-identical source contracts. Historical rows lacking original inputs display unsupported comparison, not a made-up passing gate.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_setup_evidence.py::test_bar_refusal_model_is_not_executable -q`.
- [x] **Step 3: Join existing immutable attempt/refusal facts offline and render.** Reuse `memecoin_refusal_capture` classification and its exact quote provenance. `memecoin_refusal_shadow` stays a labeled bar model. Refusal rows never become filled origins. Render priced/unpriced/open/rejected, distinct coins, notional/net costs, concentration, leave-best-out, clock trust/lag and exact route coverage; every number opens its supporting origins. Relabel old `gated_paper` as partial feasibility with omitted volume/turnover/route gates named. Show coin type/venue/quote asset/setup unknowns and current wallet-chain rails beside paper comparisons. Proposal core:

```python
if change['category'] != 'selection' or len(change['fields']) != 1:
    raise ValueError('one selection change required')
return dict(version=1, chain=chain, uid=uid, cutoff=cutoff,
            setup=setup, arms=list(arms), change=change,
            state='unvalidated', authority='none')
```

Validate `change.fields` against the existing documented selection-gate names; never accept arbitrary control names. Export a manifest for a frozen future paper comparison through a browser download; no funded apply control or new production writer. A positive retrospective result remains training; candidate freeze and later contemporaneous matched controls are required before any forward-fit label.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_setup_evidence.py tests/test_memecoin_ui_render_runtime.py tests/test_memecoin_refusal_shadow.py tests/test_refusal_shadow_report.py -q`; `node --check static/wallet_map.js`. Assert no production writes/network and existing selector unchanged.
- [x] **Step 5: Commit.** `git add sniper/services/wallet_setup_evidence.py scripts/research/wallet_follow_funnel.py static/wallet_map.js tests/test_wallet_setup_evidence.py tests/test_memecoin_ui_render_runtime.py && git commit -m 'feat(wallet-map): expose blockers and paper adjustment evidence'`.

## Rollback and limits

Revert the evidence/UI projection; existing paper/funded contracts remain unchanged. No expected-profit claim, causal relationship, automatic funding, retrospectively perfect gate result or active policy modification. Positive fixtures prove arithmetic and denominator integrity, not live strategy fit.

## Completion rulings, 2026-10-02

The eight code tasks are delivered with review corrections. Named example tests were adapted to actual repository interfaces; the final focused behavioral union passed 389 tests. Source clocks and paced completion clocks are now explicit. Frozen policies are scoped to the selected setup/window and ambiguous identities disable hypothesis export. Current proof helpers include verified watch-only Pump profiles. Seven existing chain ledgers materialized to scratch without production writes.

Provider isolation is an activation prerequisite, not a completed live test: configured host separation and explicit capacity declaration are checked, but different URLs alone cannot prove independent provider/key/IP capacity. No live provider smoke, service activation, continuous-rail policy verdict, new cash/native route or funded latency improvement is claimed. Actual scratch snapshot size required a shared 16 MiB producer/reader bound.
