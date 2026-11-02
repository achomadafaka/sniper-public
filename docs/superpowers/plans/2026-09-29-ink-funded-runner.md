# Ink Funded Runner Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this handoff task by task; use subagents only when authorized. Steps use checkboxes for tracking. This document authorizes no deployment or funded action.

**Goal:** Deliver an explicitly enabled Ink chain context with fast on-chain observation, complete applicable filter parity, safe funded execution, exact exits and restart recovery.

**Architecture:** Add an Ink Subblocks source adapter to the existing shared funded EVM engine. Reuse its custody, global risk book, reservations, policy contracts, route executors, submission identity and exit claims. Keep high-frequency stream persistence outside the main ledger and reconcile early observations against canonical receipts.

**Tech Stack:** Existing Python, Ethereum transaction libraries, WebSocket/HTTP clients, SQLite persistence, pytest and pinned runtime/watchdog scripts. No new broker, trading framework, language rewrite, paid provider or LLM in the order path.

**Spec:** [Design and release contract](../specs/2026-09-29-ink-funded-runner-design.md). Also read [verified research and evidence](../research/2026-09-29-ink-live-runner-research.md) and the [normative filter parity matrix](../research/2026-09-29-ink-filter-parity.md).

Baseline: `6f882ffe695394231e32411221ca16c050843921`, researched 2026-09-29. Rebase the handoff against current code before implementation; do not infer deployed behavior from this checkout.

## Global constraints

- Chain ID `57073`; gas ETH; WETH has 18 decimals. Native Circle USDC and USDC.e are different six-decimal contracts. Pin exact addresses from the research manifest.
- Ink is default OFF and outside the funded allowlist. Preconfirmed admission has a separate default-OFF flag. Native ETH float and approval pipelining remain OFF initially.
- No new blocking lookup/retry/lock justified without measured p50/p90. Targets below are not measurements.
- Every current applicable blocker must have real Ink evidence and positive/refusal/unknown tests. Preserve documented transient-unknown behavior; an entirely unsupported required capability blocks Ink activation.
- No symbol substitution, cross-chain address guessing, or provisional hash/logIndex fabricated as canonical.
- Raw amounts use integer arithmetic and decimal TEXT persistence. Never store atomic token quantities as SQLite INTEGER or binary floating-point.
- Same economic source creates at most one reservation. One shared runner owns the global book and one nonce owner owns each chain/custody pair.
- A provider failure is not zero balance, a healthy quiet market, or an unsellable token. Unknown submission retains durable identity and reserved exposure.
- Required confirmation uses canonical receipts. Preconfirmation is an observation state, not a finalized fill.
- Entry-off leaves protective/manual exits and reconciliation working. Rollback must retain code able to manage all Ink inventory and unresolved transactions.
- Do not arm wallets, purchase providers, transfer funds, deploy, restart or change production state during implementation validation. Those are separate operator actions.
- Pinned deployment uses `--defer-backup`; no backup unless explicitly requested. Do not activate rollover/band policy incidentally.

## Review focus

1. Replacement payload after our order was sent: preserve signed identity and inventory; invalidate source evidence without deleting exposure. Tests: Tasks 3 and 8.
2. Same transaction sells through two pools: aggregate economic sale before dedup; delayed reads must not inflate the sold fraction. Tests: Tasks 4 and 9.
3. RPC reports pending state but cannot simulate the observed build: canonical fallback or typed refusal, never a quote/safety snapshot assembled from incompatible states. Tests: Tasks 5 and 6.
4. Shared custody executes two transactions in one block: account for each separately, including active OP fees; never assign the block balance delta to both. Tests: Task 8.
5. Half-configured deployment builds Solana successfully but omits Ink: activation fails with a chain-specific reason; existing Ink exits remain recoverable during rollback. Tests: Tasks 2 and 11.

## Required inputs and procurement decisions

| Need | Starting choice | Evidence required before relying on it |
|---|---|---|
| Early observations | Official raw Subblocks WebSocket | Sequence/reconnect behavior, processing lag, canonical match rate, burst capture |
| State and submission | Official Gelato and QuickNode HTTP/WSS endpoints | Chain identity, independent failure domains, connection reuse, state lag, quotas, receipt and send semantics |
| Simulation | Existing EVM state-override safety on an Ink-capable endpoint | Real buy/approve/sell execution under the selected route and compatible state; test override support explicitly |
| Recovery | Canonical headers, receipts and bounded logs; traces where accounting requires them | Log range limits, archive availability, transaction-local proceeds and restart reconstruction |
| Market facts | Canonical pool swaps/liquidity plus verified supply and quote/USD conversion | Freshness, units, source-excluded causal features, provider-independent outage witness |
| Safety facts | Existing simulation/proxy inspection plus verified producers for enabled filters | Exact coverage matrix; GoPlus currently cannot be assumed to support Ink |
| Custody and capital | Existing custody implementation, separately allocated Ink bankroll and ETH gas | Operator-selected limits, protected gas reserve, manual recovery; no hot-path bridge |
| Venues | Verified Uniswap V3, then certified V4; others driven by census | Contracts/proxies/ABIs, executable depth, complete entry/exit/safety support |
| Hosting | Existing host first | p50/p90/p99 processing and provider results under concurrent chains; dedicated node only if evidence requires it |

Public endpoints have no assumed production SLA. Ask prospective providers for pending-state and override support, rate/burst limits, geographic placement, historical retention and price. Benchmark the actual capability before buying capacity. Do not buy a “fast RPC” based solely on marketing latency.

## File ownership and interfaces

Three workstreams can proceed after Task 1 freezes contracts:

- **A — Detection/state:** Tasks 3–5, stream decoder, watcher, stream persistence and canonical tape integration.
- **B — Execution/policy:** Tasks 2, 6–9, chain/settings registries, existing EVM executor/safety/risk/exit seams.
- **C — Integration/operations:** Tasks 10–12, UI/API, watchdog, runtime validation and evidence acceptance. Task 1 census can run alongside A/B after the initial manifest is pinned.

One owner per shared file. B owns `memecoin_real_runner.py` and `settings/memecoin.py`; A supplies reviewed integration patches instead of concurrently editing them. C owns UI/API/watchdogs. Changes to `db_memecoin.py` get one designated persistence owner. Every workstream keeps other engineers' changes.

Minimal proposed production files:

| File | Responsibility |
|---|---|
| `sniper/services/memecoin_ink_subblocks.py` | Validate raw frame sequencing, typed transactions and provisional receipts; no network or trading calls |
| `sniper/persistence/db_evm_stream.py` | Separate stream journal and restart cursor; SQL confined here; existing canonical tick frontier remains in `db_ticks.py` |
| `scripts/research/onchain_ink_watch.py` | Reuse existing watcher/wake integration with raw Subblocks ingestion and canonical reconciliation |

Prefer extending an existing equivalent owner if it already covers a responsibility when work begins. Do not create an interface/factory framework for this one adapter. Venue-specific new modules are permitted only when existing math/ABI cannot represent a certified venue.

Proposed decoder contract, to implement in Task 3:

```python
class InkSubblockState:
    def accept(self, frame: dict, *, received_mono_ns: int,
               received_utc: str) -> dict:
        # Result keys: status, observations, invalidated_payload_ids.
        # status: waiting_base | accepted | sequence_gap |
        #         payload_replaced | malformed
        # observations: normalized records matching design section 3A.
        ...
```

The ellipsis above documents an interface, not implementation supplied by this plan. `payload_replaced` may emit observations only after a valid new index-zero base; it also names invalidated old payloads. Return no candidate on malformed/incomplete input. Journal inserts return whether an observation was newly committed, and projection uses the design's origin key for idempotency. Existing funded event and executor interfaces remain authoritative; do not add another trading API.

## Test workflow used by every task

All new tests below live under proposed `tests/ink/`; extend existing shared suites when the assertion belongs there. Use temporary DBs, injected provider responses and recorded public frames. No production DB, environment secrets, live signer or network in pytest. The initial recorded sample is public transaction data, not a signer fixture.

Each task follows: add behavioral failures, run red, implement the smallest change, run green plus named adjacent suites, review, commit only that task. A red test must fail on the claimed behavior, not only on a missing import. For newly introduced APIs, import failure is scaffolding evidence only; replace a working invariant temporarily and prove the behavioral test fails before marking the task complete.

Tests with the `ink_flow` fixture below require Task 2 to implement a **test-only** fixture in `tests/ink/conftest.py`. Its contract is `ink_flow(case: str) -> dict`: construct temporary ledgers and invoke the real shared seams for the named scenario, supplying fake RPC/quote/clock/signer/transport dependencies. Return observed `approve_calls`, `sign_calls`, `send_calls`, durable row counts and stage results. Never implement it as a dictionary of expected answers or mock the predicate being tested. Each task adds its scenario to that fixture alongside the real fixture inputs. Keep subblock pure-function tests separate.

### Task 1 — Freeze venue coverage and provider capabilities

**Files:** Create `scripts/research/ink_capability_probe.py`, `tests/ink/test_capabilities.py`, and `docs/superpowers/research/2026-09-29-ink-coverage-manifest.json`. Use existing research evidence in this handoff as the initial reference, not a production configuration.

**Interface:** Read-only CLI `python3 scripts/research/ink_capability_probe.py --rpc-url <public-url> --output <file>`; no send/sign/approval methods. In that script, pure `validate_chain_reply(reply: dict, expected: int = 57073) -> dict` returns `capability` and `usable`, distinguishing malformed reply, RPC error and wrong chain. Produce versioned JSON with chain identity, observation time, method capability/typed failure, addresses, code/proxy provenance and measured RTT samples. Census mode must be a separate explicit bounded block range; it cannot launch an unbounded historical scan.

- [ ] Define one manifest row per venue/version/route class: exact contracts, proxy implementation where relevant, supported token semantics, quote assets, entry/exit/simulation proof, covered block interval and unsupported reason.
- [ ] Add a failing fixture case for a chain-8453 endpoint supplied as Ink and one for HTTP 200 with a JSON-RPC error. Assert `supported=false` with distinct reasons, not empty success.

```python
def test_wrong_chain_is_not_a_usable_provider():
    from scripts.research.ink_capability_probe import validate_chain_reply
    r = validate_chain_reply({"jsonrpc": "2.0", "id": 1, "result": hex(8453)})
    assert r["capability"] == "wrong_chain"
    assert r["usable"] is False
```

- [ ] Run `python3 -m pytest tests/ink/test_capabilities.py -q`; prove red, then implement response/identity checks and rerun green. The probe's injected HTTP test must assert that its complete method list contains reads only; unexpected RPC methods fail the fixture.
- [ ] With explicit read-only research scope, census a declared recent complete block interval. Count verified pool creations, swaps, distinct tokens/owners, credible quote-side depth, migrations, unsupported protocols and gaps. State denominator and coverage; do not rank venues from an ecosystem listing.
- [ ] Probe pending calls, overrides, canonical receipt fields, log limits and trace availability with known public transaction fixtures. Do not use funded wallet balances or submit test transactions. Store absent capability explicitly.
- [ ] Choose initial route classes from measured coverage, not convenience. V3-only funding is valid only as a visibly limited scope; support for V4/Velodrome/launchpads cannot be implied.
- [ ] Commit: `docs(ink): pin provider and venue capability evidence`. Gate G0 remains incomplete until bytecode/proxy provenance and required facts are verified.

The repository ignores JSON evidence by default. Include the six reviewed public evidence files explicitly when committing the handoff, so another checkout receives the fixtures:

```bash
git add -f docs/superpowers/research/2026-09-29-ink-evidence/*.json
```

### Task 2 — Chain identity, authority and reusable test harness

**Modify:** `sniper/services/memecoin_chains.py`, `sniper/settings/memecoin.py`, `sniper/execution/evm_wallet.py`, `sniper/execution/free_endpoint_registry.py`, `sniper/services/axiom_address_verify.py`, `sniper/persistence/db_memecoin.py`. Provider maps in `sniper/execution/solana_quote.py` and the existing Gecko client only if actual support is verified.

**Tests:** `tests/ink/conftest.py`, `tests/ink/test_identity.py`; extend `tests/test_memecoin_chains.py` and existing RPC/submission config tests.

**Interface:** Existing `resolve_chain`, `canonical_chain_id`, `funded_chain_keys`, quote-asset mappings and `build_real_evm_context` accept Ink. Add Ink to `_evm_rpc_pool` dispatch and per-chain settings, without adding it to the default allowlist. Declare proposed `MEMECOIN_INK_PRECONFIRMED_ADMISSION_ENABLED=false`; keep raw stream URL separate from ordinary JSON-RPC WSS.

- [ ] Add the direct failing identity assertion:

```python
def test_ink_canonical_identity():
    from sniper.services.memecoin_chains import canonical_chain_id
    assert canonical_chain_id("ink") == "57073"
    assert canonical_chain_id("57073") == "57073"
```

- [ ] Run `python3 -m pytest tests/ink/test_identity.py -q`; expect current Ink resolution to fail. Implement exact aliases and assets; rerun green.
- [ ] Build `ink_flow` from current shared test fixtures, real entry/reservation functions and fake RPC/signer counters. Verify a fake refusal stops before every money seam and a successful fake flow actually reaches the transport counter.
- [ ] Add cases: default disabled, enabled but missing context, wrong RPC chain, missing quote conversion, ETH gas shortage, USDC.e passed as USDC, 6/18-decimal amounts, Ink address with no chain proof, pause clears authority, other-chain arm does not authorize Ink.
- [ ] Keep Ink out of `NATIVE_IS_QUOTE_CHAINS` and cross-chain token-resolution guessing. Do not fabricate a market-data mapping to get past `real_runner` readiness; Task 5 supplies validated on-chain facts if providers are absent.
- [ ] Run `python3 -m pytest tests/ink/test_identity.py tests/test_memecoin_chains.py -q`. Commit: `feat(ink): add disabled chain identity and configuration`.

### Task 3 — Raw Subblocks decoder, journal and canonical lifecycle

**Create:** the three minimal files identified above, `tests/ink/test_subblocks.py`, `tests/ink/test_stream_recovery.py`. **Modify:** `sniper/persistence/db_ticks.py` only for necessary canonical integration; preserve its frontier ownership.

**Interface:** `InkSubblockState.accept` as defined above. Journal stores proposed observation records, invalidations and durable projection cursor in a separate DB. Canonical reconciliation maps receipt-local ordinals to real receipt logs and updates lifecycle without replacing order identities.

- [ ] Copy a small recorded payload from the evidence bundle into a test fixture; preserve raw signed transaction bytes and receipt keys. Add a helper that selects an observed index-zero base and an observed continuation, rather than inventing provider fields.

```python
def test_mid_build_frame_cannot_emit_candidate(recorded_nonzero_frame):
    from sniper.services.memecoin_ink_subblocks import InkSubblockState
    r = InkSubblockState().accept(
        recorded_nonzero_frame, received_mono_ns=1,
        received_utc="2026-09-29T08:37:00Z")
    assert r["status"] == "waiting_base"
    assert r["observations"] == []
```

- [ ] Run `python3 -m pytest tests/ink/test_subblocks.py -q`; implement only the parser/sequencer first, then prove it fails if the index-zero rule is removed.
- [ ] Cover contiguous sequence, skipped/duplicate/out-of-order index, redundant identical transport frames, changed payload at same height, changed head, empty build, malformed JSON, oversized frames, metadata receipt missing/raw hash mismatch, system/deposit transactions and failed status. Validate typed transaction hash and signer with installed Ethereum libraries.
- [ ] Add journal/restart tests: crash after journal commit before wake, after wake before cursor advance, gap across restart, canonical reorg and source retraction after a signed order. Projection can repeat; reservation cannot duplicate.

```python
def test_retracted_source_keeps_submitted_order(ink_flow):
    r = ink_flow("source_replaced_after_send_then_restart")
    assert r["economic_orders"] == 1
    assert r["reserved_exposure_preserved"] is True
    assert r["source_state"] == "retracted"
    assert r["send_calls"] == 1
```

- [ ] Keep receive callback bounded; enqueue parsing/work without RPC, synchronous backfill or main-ledger locks. On overflow persist a gap marker and invalidate incomplete coverage; never silently drop and declare healthy.
- [ ] Run `python3 -m pytest tests/ink/test_subblocks.py tests/ink/test_stream_recovery.py -q`. Commit: `feat(ink): journal and reconcile subblock observations`.

### Task 4 — Source attribution, canonical backfill and causal features

**Modify:** `sniper/services/memecoin_evm_onchain_watch.py`, `sniper/services/memecoin_evm_post_trade.py`, `sniper/services/memecoin_onchain_push.py`, `sniper/services/memecoin_onchain_lane.py`, `sniper/services/memecoin_onchain_volume.py`, `sniper/services/memecoin_volume_writer.py`, `scripts/research/onchain_evm_wallet_watch.py`; wire the Ink watcher into existing commit-then-wake behavior.

**Tests:** `tests/ink/test_source_attribution.py`, `tests/ink/test_coverage.py`. **Interface:** normalized events retain original source clock, chain, owner, token, side and exact aggregated atomic amounts; existing `EntryAttemptTrace` records receive/projection timestamps separately.

- [ ] Build multi-leg fixtures from public canonical receipt shape. Test wrapped/native quote movements, refunds, router intermediaries, failed transactions, relayed owner attribution and token transfer taxes. A transfer alone is not proof of a buy.

```python
def test_two_pool_sale_is_one_complete_economic_event(ink_flow):
    r = ink_flow("owner_sells_12_and_8_same_token_same_tx")
    assert r["source_sold_atomic"] == "20"
    assert r["projected_origins"] == 1
    assert r["coverage_complete"] is True
```

- [ ] Run `python3 -m pytest tests/ink/test_source_attribution.py tests/ink/test_coverage.py -q`; prove red before fixing/adding shared source aggregation.
- [ ] Recover token flow before origin dedup. Verify source pre/post balance refers to the source transaction/state, not a later reread containing another trade. Exclude source transaction from prior-buyer/volume features whose contract is prior-only.
- [ ] Preserve real timestamps on backfilled/dark-polled events. Test a 45-second-old event fetched now is still 45 seconds old; recovery events never become live latency samples. Clear gap state only after durable canonical coverage reaches its declared boundary.
- [ ] Launch/pool creation and migrations feed coverage/setup research; they cannot fabricate armed-wallet events. One token may have several pools; bind price/safety/LP evidence to actual selected routes.
- [ ] Fix applicable shared defects from the September 29 review before Ink reuses them: dark-poll clock refresh, early backfill gap clearing, split-pool sale dedup and delayed source-balance rereads. Pin each with its own behavioral test; unrelated brain-service findings are outside this work.
- [ ] Run adjacent `tests/test_memecoin_onchain_volume_integrity.py` plus Ink source/coverage tests. Commit: `fix(ink): preserve source identity and causal coverage`.

### Task 5 — Coherent pending state and complete market/safety evidence

**Modify:** `sniper/execution/evm_token_safety.py`, `sniper/execution/evm_wallet.py`, `sniper/services/memecoin_evm_leg.py`, existing chain data adapters and Ink watcher. No new global cache manager.

**Tests:** `tests/ink/test_state_evidence.py`. **Interface:** existing quote/safety results carry verified chain, route/token identity, state block/build where available, read interval, freshness and typed availability. Cache entries are immutable; invalidate on code/proxy/route change or coverage loss.

- [ ] Test a fresh pending quote paired with older incompatible safety state:

```python
def test_incoherent_state_cannot_reach_signer(ink_flow):
    r = ink_flow("pending_quote_a_safety_from_replaced_build_b")
    assert r["reason"] == "state_not_ready"
    assert r["sign_calls"] == r["send_calls"] == 0
```

- [ ] Run `python3 -m pytest tests/ink/test_state_evidence.py -q`; implement watermark/read-window validation and explicit canonical fallback, then green.
- [ ] Verify state-override support using actual certified router/token fixture calls. Unsupported override, proxy implementation change, unknown holder/cluster producer, malformed supply, supply-zero and supply-unavailable must be distinguishable. Full buy→approve→sell simulation must use actual route/clip and expected token ownership.
- [ ] Use existing asynchronous warming for factory/pool/token/supply/code/price facts. Cold tokens retain required checks and a separate latency cohort. Proxy/code-sensitive safety evidence cannot live indefinitely because the token address stayed constant.
- [ ] Provide on-chain market-data fields consumed by applicable entry and exit gates: supply basis, quote/USD, market cap, coin age provenance, liquidity, turnover, sell share, source-excluded volume, LP changes and chain witness. Quantities and fee-on-transfer effects retain explicit units. V3 active liquidity is not V2 reserves.
- [ ] Classify complete producer absence as an activation capability failure; preserve existing transient-unknown handling per parity row. Do not turn missing DexScreener/GoPlus support into a green safety result.
- [ ] Run Ink state tests and `tests/test_memecoin_evm_token_safety.py`. Commit: `feat(ink): bind admission evidence to coherent chain state`.

### Task 6 — Certified entry and whole-balance exit routes

**Modify:** `sniper/settings/memecoin.py`, `sniper/execution/evm_v4_quote.py`, existing V3/V2 quote modules, `sniper/services/memecoin_evm_leg.py`, `sniper/execution/evm_real_seams.py`. Reuse existing rung selection and ABI encoders. Separate Velodrome ABI/math only if selected by Task 1.

**Tests:** `tests/ink/test_routes.py`; extend existing V3/V4 unit and executor tests. **Interface:** current quote result retains expected output separately from minOut and binds exact chain/token/raw input/route/fee/hook/spender/deadline.

- [ ] Parameterize certified V3/V4 route fixtures and explicit unsupported classes:

```python
def test_route_without_full_clip_exit_proof_cannot_enter(ink_flow):
    r = ink_flow("v3_buy_works_dust_sell_works_full_clip_sell_fails")
    assert r["admitted"] is False
    assert r["approve_calls"] == r["sign_calls"] == 0
```

- [ ] Run `python3 -m pytest tests/ink/test_routes.py -q`; add verified manifest addresses and exact reverse-clip behavior, then green. Configure Ink explicitly in `MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS` for the intended stronger contract.
- [ ] Test tick crossing, low liquidity, decimal extremes, price impact, stale pool state, wrong factory, wrong spender, unknown hooks, dynamic fees, native wrapping and identical token symbols. V4 PoolKey must hash to the selected pool; router version must support the emitted commands.
- [ ] Prove buy→actual owned balance→full sell, and partial exit→remaining-balance sell, per certified class on a local mainnet fork. Testnet proves mechanics only. Treat unsupported token/venue classes as visible coverage exclusions.
- [ ] Keep 0x optional and explicitly configured; if enabled, test quote binding, approved spender, expired quote, provider outage and exact exit availability. Do not make all direct entries depend on it.
- [ ] Remove the V3-config precondition only if a V4-only certified context genuinely supports every required operation; otherwise retain an explicit missing-capability refusal.
- [ ] Commit: `feat(ink): certify direct routes and exact reverse safety`.

### Task 7 — Full admission parity and frozen adaptive policy

**Modify:** `sniper/services/memecoin_real_leg.py`, `sniper/services/memecoin_evm_leg.py`, `sniper/services/memecoin_real_runner.py`, existing entry classification/risk/contract owners. Do not introduce a second policy engine.

**Tests:** `tests/ink/test_admission_parity.py`; parameterize existing reentry, age, risk, execution-premium and frozen-contract tests. **Interface:** same funded identity, `EntryClassification`, reservation and frozen policy contract used by other chains.

- [ ] Turn every applicable row in the companion matrix into a fixture entry with three outcomes: pass, refusal and producer failure/unknown. Maintain a coverage table mapping each row to exact pytest node IDs; a missing row fails the parity completeness check.

```python
def test_ink_losing_reentry_guard_runs_before_money_seams(ink_flow):
    r = ink_flow("ink_reentry_after_loss_inside_blocked_window")
    assert r["admitted"] is False
    assert r["gate"] == "losing_reentry"
    assert (r["approve_calls"], r["sign_calls"], r["send_calls"]) == (0, 0, 0)
```

- [ ] Run `python3 -m pytest tests/ink/test_admission_parity.py -q`. Move truly shared checks before chain dispatch or supply explicit Ink equivalents; do not casually change existing chain policy.
- [ ] Close Solana-only gaps: losing/winner reentry semantics, presign unknown-age recheck, enabled cluster checks, source-sale support and independent market-data readiness. Any mechanism N/A must name the equivalent invariant and its evidence.
- [ ] Exercise all independent gates: authority/pause/kill, duplicates/concurrency, max book/exposure including reservations across chains, chain-local bankroll/gas, age/chase, cap band/supply, liquidity/tax/simulation, volume/turnover/sell share, configured buyer/cluster checks, exact execution premium and exact post-peak confirmation before approval/signing.
- [ ] Test `hot_narrow`, `thin`, `unknown` shrink but never independently veto/increase risk; frozen contract matches reservation/pending/fill byte-for-byte. Test concurrent Ink+Solana reservations against the shared cap.
- [ ] Keep launch/established/revival setups as existing classification fields. Chain activity may warm capacity and inform research; it cannot arm wallets, increase limits or nominate independent funded launches through forged copy events. Autonomous launch funding requires a separately approved strategy contract.
- [ ] Run Ink parity tests plus shared `tests/test_memecoin_evm_risk.py`, `tests/test_memecoin_funded_contract_storage.py`, `tests/test_memecoin_losing_reentry.py`, `tests/test_memecoin_coin_age_presign.py`. Commit: `feat(ink): enforce complete funded admission parity`.

### Task 8 — Signing, submission, canonical accounting and restart

**Modify:** `sniper/execution/evm_real_seams.py`, `sniper/execution/evm_swap_executor.py`, `sniper/execution/evm_exit_executor.py`, existing submission wiring/transport and reconcile owners.

**Tests:** `tests/ink/test_execution_recovery.py`, `tests/ink/test_accounting.py`. **Interface:** existing durable order/close identities and canonical fill insertion; no alternate Ink order ledger.

- [ ] Add crash points before signing, after signed identity persistence, after first send, after receipt read and before/after atomic fill commit. Test accepted/ambiguous/rejected/not-sent responses across endpoints and restarts.

```python
def test_ambiguous_send_restart_does_not_sign_another_order(ink_flow):
    r = ink_flow("send_timeout_restart_canonical_receipt_later")
    assert r["sign_calls"] == 1
    assert r["distinct_signed_hashes"] == 1
    assert r["fills"] == 1
    assert r["unresolved_reservations"] == 0
```

- [ ] Run `python3 -m pytest tests/ink/test_execution_recovery.py tests/ink/test_accounting.py -q`; extend the current seams, preserving bytes and nonce ownership. Unknown send must not manufacture failure or release exposure.
- [ ] Ordinary `eth_getTransactionReceipt` may be preconfirmed at a capable endpoint. Require canonical block/hash linkage and required receipt fields before durable fill finalization; speed up the existing bounded confirmation window/notification without confusing observed inclusion with canonical settlement.
- [ ] Keep receipt-first approvals initially. Test insufficient allowance, revert, pending approval across restart, wrong chain/router and racing entry/exit nonce usage. Validate existing legacy transaction fee mode; add EIP-1559 only when measured/required by certified submission behavior.
- [ ] Account for active L2 execution, L1 data and operator fees from authoritative chain evidence. Test WETH transfers, unwraps, taxes, external deposits and two custody transactions in the same block. Missing proceeds remains unresolved; never fill from the quote or a gas-only whole-block native delta.
- [ ] Native ETH float remains disabled until transaction-local accounting, gas reserve and recovery tests pass. The default WETH path still needs truthful fee reporting and bankroll reconciliation.
- [ ] Canonically included own trade with retracted source retains its inventory and original approved exit policy. A changed source does not grant automatic unwind authority.
- [ ] Run shared `tests/test_memecoin_evm_entry_executor.py`, `tests/test_memecoin_evm_exit_executor.py`, `tests/test_memecoin_funded_reconcile.py` plus Ink tests. Commit: `feat(ink): reconcile funded execution and OP fee accounting`.

### Task 9 — Exact exits, source selling and position adaptation

**Modify:** `sniper/services/memecoin_real_runner.py`, `sniper/services/memecoin_volume_death.py`, `sniper/services/memecoin_evm_tape_marks.py`, source mirror integration and canonical volume owners. Use the current exact-quote demand/budget and executable-trigger audit.

**Tests:** `tests/ink/test_exit_parity.py`. **Interface:** existing `effective_trail`, `executable_exit_allowed`, durable close claim, partial-fill and frozen exit-policy semantics; Ink supplies equivalent route/tape/witness inputs.

- [ ] Add exact-balance and provider-failure fixtures:

```python
def test_quote_outage_cannot_ratchet_peak_or_count_unsellable(ink_flow):
    r = ink_flow("armed_trail_ink_provider_timeout")
    assert r["executable_peak_after"] == r["executable_peak_before"]
    assert r["unsellable_count_after"] == r["unsellable_count_before"]
    assert r["send_calls"] == 0
```

- [ ] Run `python3 -m pytest tests/ink/test_exit_parity.py -q`; supply Ink exact whole-balance quotes, independent health witness and source sales through the existing owners.
- [ ] Test trail arm/give-back without derisk, derisk then changed quantity, nominal peak divergence, stop/catastrophe distinct-observation debounce, exact confirmation on every venue, trigger audit on allowed/refused exits and two concurrent close requests.
- [ ] Source mirror tests: 12+8 sale legs become 20; prebalance bound to source state; sale before our fill; intervening buy; partial cumulative trims; canonical duplicate; disabled mirror at scheduled retry. Recheck current operator override immediately before action.
- [ ] Prove LP/volume-death inputs distinguish healthy zero activity from a dead feed, incomplete backfill, wrong pool or stale witness. Never copy 4663-only observer assumptions into Ink. Exit-only recovery remains available when new admissions are stopped.
- [ ] Keep provider budgets chain/group scoped; Ink quote starvation or 429 must not consume Solana/Base capacity. Bound quotes/backfills and protect exit demand first.
- [ ] Run Ink exit tests plus `tests/test_memecoin_volume_death.py`, `tests/test_volume_death_coverage_guards.py`, `tests/test_memecoin_quote_demand.py`. Commit: `feat(ink): wire exact exits and causal source-sale coverage`.

### Task 10 — Operator controls and truthful setup visibility

**Modify:** `sniper/routes/api_memecoin.py`, `sniper/services/memecoin_links.py`, existing frontend chain lists/templates, manual entry/exit intent routing and leftover-token recovery dispatch.

**Tests:** `tests/ink/test_operator_flow.py`; extend current authenticated API/manual-control tests. **Interface:** current API intent contract with canonical Ink identity; no parallel UI-only executor.

- [ ] Test authority and close routing through the API and runner:

```python
def test_paused_ink_position_can_be_manually_closed(ink_flow):
    r = ink_flow("authenticated_manual_close_paused_ink_position")
    assert r["entry_allowed"] is False
    assert r["exit_chain_id"] == "57073"
    assert r["close_claims"] == r["fills"] == 1
```

- [ ] Run `python3 -m pytest tests/ink/test_operator_flow.py -q`; update every hardcoded funded-chain list, explorer/provider URL and balance/holdings dispatch. The current leftover-token recovery special cases require a real Ink executor path.
- [ ] Display chain-specific arm/watch/pause, balances and quote asset, source age, gaps, provisional/canonical status, quote age, queued/unresolved actions, unsupported venue/filter reasons and enabled policy. Missing marks display unavailable, not zero PnL.
- [ ] Expose setup coverage using existing classification views: launches, established trends and revivals with evidence/freshness. Keep unsupported launchpad observations visible without making them funded candidates.
- [ ] Test expired manual intent, restart, missing context, stale balance, duplicate close, authorization failure and manual buy's documented gate exceptions. A rendered button is not acceptance; verify intent→runner→claim→canonical fill.
- [ ] Commit: `feat(ink): expose authority recovery and setup coverage`.

### Task 11 — Pinned startup, chain isolation and safe rollback

**Modify:** `scripts/memecoin_watchdog.sh`, `scripts/memecoin_runtime_verify.py`, `scripts/deploy_memecoin_runtime.sh`, `scripts/memecoin_runner_loop.py`, shared startup context validation and the Ink watcher entrypoint.

**Tests:** `tests/ink/test_runtime.py`; extend `tests/test_memecoin_runtime_verify.py` and deploy-route tests. **Interface:** existing pinned release manifest plus one Ink source worker; code root and state root remain distinct.

- [ ] Add a mixed-chain startup test:

```python
def test_one_working_chain_cannot_hide_missing_ink_context(ink_flow):
    r = ink_flow("enabled_solana_and_ink_only_solana_context_built")
    assert r["startup_valid"] is False
    assert "57073" in r["missing_context_chains"]
    assert r["send_calls"] == 0
```

- [ ] Run `python3 -m pytest tests/ink/test_runtime.py -q`; make startup/deploy validate every enabled chain, each required capability and each chain with open inventory/unresolved orders. Do not merely check whether any context exists.
- [ ] Test single nonce/runner ownership, worker restart, old release with new additive journal schema, absent stream/state paths, and independent budgets. No observer may hold a writable main-ledger connection indefinitely.
- [ ] Rehearse rollback: stop Ink admissions; preserve claims/reservations; switch to a release certified for Ink recovery; restart and reconcile; retain exits until inventory and pending orders are zero. Entry allowlist removal must not erase the contexts needed for exits/recovery.
- [ ] Validate pinned code path/process count, manifest, effective config, authenticated API, durable frontiers and ledger truth independently. A running heartbeat is insufficient.
- [ ] Deployment command, only after separate operator authorization:

```bash
scripts/deploy_memecoin_runtime.sh <reviewed-merged-sha> --defer-backup
```

- [ ] Commit: `feat(ink): validate pinned runtime and retain rollback recovery`. No live deployment in this task.

### Task 12 — End-to-end reliability, speed and promotion evidence

**Create:** `tests/ink/test_end_to_end.py`, `scripts/research/ink_shadow_report.py`; write results under an operator-chosen evidence directory. **Modify:** existing entry trace/metrics only for missing timestamp boundaries. No new telemetry call on the order path.

**Interface:** report reads recorded observations, canonical coverage and existing trace/lifecycle records. It outputs cohort counts, exclusions, stage distributions, route/filter coverage, reconciliation defects and G0–G5 readiness; it cannot arm or deploy.

- [ ] Add a complete fake event→filter→reservation→buy→canonical fill→derisk/close→restart trace and its refusal counterpart:

```python
def test_end_to_end_has_one_inventory_lifecycle(ink_flow):
    r = ink_flow("certified_v3_buy_partial_exit_full_close_restart")
    assert r["economic_entries"] == 1
    assert r["open_qty_atomic"] == "0"
    assert r["unresolved_orders"] == 0
    assert r["cash_and_fee_reconciliation_error_atomic"] == "0"
    assert r["missing_parity_rows"] == []
```

- [ ] Run `python3 -m pytest tests/ink -q`, followed by the focused shared suites named by prior tasks. Run `python3 scripts/validate_schema_refs.py --warn-unknown-tables`; inspect warnings against actual schemas, including separate tape/journal DBs. No stress suite or broad backtest by default.
- [ ] Replay captured bursts with queue contention, provider delays and concurrent other-chain work. Report warm/cold, approval-needed, canonical fallback and outage separately. Include refusals and dropped/censored observations in denominator accounting.
- [ ] Run at least 24 hours of read-only shadow after authorized startup of the observation worker, covering reconnect/restart and a burst. Compare every eligible source in a frozen interval to canonical truth. Require no unreported gaps or duplicate economic-origin projection. Unknown coverage is censored, not called zero misses.
- [ ] Proposed healthy warm targets: receive→enqueue p90≤10 ms; wake→local gates/reservation p90≤50 ms; complete source→submission acknowledgment p50≤150 ms/p90≤350 ms; accepted→canonical receipt p90≤2 s. Measure inclusion distance, aiming at the next 1–2 eligible subblocks. These are engineering targets, not provider guarantees or current measurements.
- [ ] Compare other chains under the same workload/provider budgets; report p50/p90/p99 and sample counts. If state/safety/approval dominates, optimize the measured boundary without weakening checks. Canonical-only operation may be correct while missing the early-inclusion target; report both verdicts.
- [ ] Compile G0 dependency, G1 offline, G2 shadow and G3 fork-rehearsal evidence. An operator then chooses canary bankroll/clip/exposure, wallets, venues and whether to allow preconfirmed admission. Do not infer this approval from a green report.
- [ ] Canary evidence must include canonical buy and sell, partial exit if enabled, open-position restart/recovery, usable manual close, exact exit coverage and organic latency. Do destructive fault injection on forks/shadow. Stop new Ink entries on duplicate orders, wrong amounts/identity, broken required safety/exit capability or unresolved accounting inconsistency; retain compatible exits and recovery.
- [ ] Expand at G5 only after actual canary evidence. Setup profitability remains a separate prospective evaluation with common origins, costs, adverse selection, missed fills and distinct-coin robustness; a fast reliable runner alone does not establish edge.
- [ ] Commit: `test(ink): prove end-to-end parity recovery and latency accounting`.

## Completion and engineer handoff checklist

- [ ] Every parity matrix row maps to implementation, evidence producer, exact tests and capability state; no silent waiver.
- [ ] Contract manifest and live capability report are dated and pinned; no assumed GoPlus/market-data support.
- [ ] End-to-end lifecycle works for every advertised route class, including manual recovery and restarts.
- [ ] Queue/coverage/receipt semantics remain honest through sequence gaps and reorgs.
- [ ] Source-to-send and fill latency measured separately; public 200 ms frames are not a fill benchmark.
- [ ] Existing chains retain policies and meet paired performance checks.
- [ ] G0–G3 evidence is reviewable before any funded approval; G4/G5 remain operator decisions.
- [ ] Rollback has compatible Ink exit/recovery code and does not require a database backup.

Planning estimate: roughly **3–5 engineering weeks** for two engineers with SRE/QA support for certified initial venues, assuming usable pending-state/simulation providers and no custody redesign. This is an estimate, not a deadline. Proprietary launchpad support, missing security data, provider limits or new venue math can dominate the schedule. Discovery/copy policy changes are separate from chain transport readiness.

## Self-review coverage

Design sections 1–2 map to global constraints and Tasks 1–2; 3A to Tasks 3–4; 3B to Tasks 5–6; 3C to Task 7 and the parity matrix; 3D to Tasks 8–9; 3E to Tasks 10–11; performance and release gates to Task 12. All five review-focus failure classes have explicit owning tests. Proposed files/interfaces are labeled; no production code or funded configuration was changed by this handoff.
