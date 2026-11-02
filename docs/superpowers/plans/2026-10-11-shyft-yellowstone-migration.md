# Complete Shyft / Yellowstone Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reach measured successful same-slot fills on currently authorized native Solana paths by combining Shyft/Yellowstone infrastructure with preparation before entry, while preserving proof, price and funded execution rules. Grade routed-confirmed and graduation constraints separately.

**Architecture:** Reuse the existing processed/confirmed Shyft adapters, receipt worker, decoders, price materializer, RPC pool and direct TPU transport. Add an explicit Shyft-only profile and narrowly adapt account/observer consumers to Yellowstone; preserve legacy topology solely for staged comparison and rollback. HTTP stays responsible for reads and RPC sends; gRPC replaces streaming. Reuse warm native builders and local solders signing; entitled RabbitStream starts unsigned preparation while Yellowstone supplies authoritative state/proof.

**Tech Stack:** Python 3, existing grpcio/protobuf/base58, vendored Yellowstone messages, existing Rust/Agave QUIC TPU sidecar, SQLite, immutable pinned releases.

**Spec:** `docs/superpowers/specs/2026-10-11-shyft-yellowstone-migration-design.md`. Read the spec and current AGENTS.md before implementation. This plan is a proposed migration, not proof Shyft is faster.

## Global Constraints

- No new synchronous provider request, lock, retry, database write or protobuf compilation on funded entry.
- Preserve 500 ms original receipt/resume window, TPU 1000 ms frozen-clock and two-slot guard.
- No cap, sizing, roster, initiation-proof, exit or durable-claim relaxation; no synthetic funded trades.
- Processed balance deltas alone never become confirmed signer/payment proof. Missing metadata is unknown.
- READ rotation may preserve existing allowlist; WRITE HTTP/transport ambiguity never rotates or releases claims.
- Capacity amendment (2026-10-12): nine steady funded gRPC connections with RabbitStream and Relay deposit proof enabled; the ten-connection account permits one handover connection. Launch the replacement confirmed primary, verify its complete roster ACK and fresh head, retire the old watcher, then open the replacement secondary. Actual peak must stay at or below ten; disabled research opens zero connections.
- No Helius/public or Sender/Nozomi connection in final Shyft profile. Direct TPU and Jupiter remain.
- Tests explicitly use `-n 2`; one broad suite under `/home/bigpapi/.cache/codex-pytest-full.lock`.
- Normal pinned deployment with `--restart --defer-backup`; no backup, gate bypass or mutable-checkout runner.

## Review Focus

1. Mixed processed/confirmed streams or forged provenance must not grant receipt proof (Task 3).
2. Partial roster ack, empty roster, replay, failed transaction and disconnect must not create a false complete/healthy feed (Tasks 2/5).
3. Shared account quota behind different keys must not count as isolated capacity (Tasks 1/10).
4. Existing executable exits, account deletion/recreation, same-slot write versions and native WSOL accounting must survive streaming conversion (Tasks 4/5).
5. Half-applied deployment with unresolved signed claims must restore topology without duplicate broadcast or releasing ownership (Tasks 6/10).

## File and ownership map

| Task | Production responsibility | Tests/evidence |
|---|---|---|
| 0 | Existing trace/fill timing, slot lane, native signer and identity binding | Same-slot grader and exact per-stage evidence |
| 1 | `sniper/services/memecoin_research_rpc.py`, new `sniper/services/memecoin_solana_provider.py`, `sniper/settings/memecoin.py`, `.env.example` | new `tests/test_memecoin_shyft_provider_profile.py`; research/RPC pool tests |
| 2 | `sniper/services/memecoin_slot_signal.py`, `memecoin_shyft_signal.py`, `memecoin_slot_lane.py` | Shyft signal, handoff, slot signal tests |
| 3 | `sniper/services/memecoin_shyft_confirmed.py`, `scripts/research/onchain_wallet_watch.py` | confirmed adapter, source-initiation, route/cashback fixtures |
| 4 | new `sniper/services/memecoin_shyft_accounts.py`, `memecoin_solana_swap_marks.py`, `memecoin_slot_graduation.py`, `scripts/memecoin_runner_loop.py` | new `tests/test_memecoin_shyft_accounts.py`; graduation/price stream tests |
| 5 | `sniper/services/memecoin_execution_observer.py`, `memecoin_shyft_confirmed.py`, runner wiring | execution observer tests and new Yellowstone cases |
| 6 | `sniper/execution/submission_backends.py`, `sniper/settings/memecoin_submission.py`, `sniper/services/memecoin_real_runner.py`, TPU provider-control verifier | transport/profile tests; existing signed identity/claim tests |
| 7 | active background worker/read callers and watchdogs listed below; `memecoin_sol_cohort.py` | existing worker tests plus new `tests/test_memecoin_shyft_runtime_inventory.py` |
| 8 | Existing LanePrefetcher, builder caches, WarmExoticLegs and metadata preparation | Ready/no-network/cold/stale/identity tests |
| 9 | New bounded RabbitStream unsigned preparation adapter | No-meta/no-authority/expired/exact join tests |
| 10 | deploy/runtime verify scripts, spec amendments, audit | capability/canary report, live topology/rollback proof |

Tasks 2–6 depend on Task 1 and share runner/config integration: implement sequentially with fresh review; do not let parallel workers edit the runner or settings concurrently. Task 7 closes the full inventory after earlier interfaces stabilize.

## Task 0: Separate slot timing, proof waiting, local signing and durable writes

**Files:** `sniper/services/memecoin_slot_lane.py`, `sniper/execution/solana_real_seams.py`, `sniper/execution/solana_slot_executor.py`, `sniper/execution/submission_wiring.py`, `sniper/services/memecoin_fill_timing.py`, `tests/test_memecoin_fill_timing.py`, new `tests/test_solana_same_slot_measurement.py`.

**Interfaces:** use existing trace `mark_substage`, original monotonic receipt and source signature. Add pure `same_slot_outcome(*, source_slot, landing_slot, confirmed, chain_error, acquired_atomic) -> str` to fill timing: returns `same_slot`, `later_slot`, `failed`, or `unknown`; it never changes admission. Add pure `same_slot_receipt_evidence(source, attempt, receipt, fill) -> dict` for the exact confirmed source/our-signature/network/owner/mint/payment/acquisition and actual receipt-slot join. This join is independent of the processed observer and boot/clock witnesses. Keep existing `execution_bound(...)` solely for <=200 ms interval evidence: it excludes failed attempts and cannot serve as the same-slot grader. Treat `chain_error is not None` as failure only when receipt confirms that error; absent receipt stays unknown.

- [ ] Write failure witnesses for S-1 head skew, confirmed failed receipt in S, wrong signature, absent acquired quantity and unresolved receipt. Add confirmed S+0 acquisition with no processed observation: it grades same_slot by the exact receipt, while the millisecond execution bound remains unknown. A confirmed failed receipt in S grades failed even when `execution_bound` excludes it. Example:

```python
from sniper.services.memecoin_fill_timing import same_slot_outcome

def test_same_slot_error_is_not_a_fill():
    assert same_slot_outcome(source_slot=100, landing_slot=100, confirmed=True,
        chain_error='ExceededSlippage', acquired_atomic='0') == 'failed'

def test_real_acquisition_in_source_slot_is_same_slot():
    assert same_slot_outcome(source_slot=100, landing_slot=100, confirmed=True,
        chain_error=None, acquired_atomic='123') == 'same_slot'
```

- [ ] Run `python3 -m pytest -n 2 tests/test_solana_same_slot_measurement.py -q`; verify red.
- [ ] Add the pure confirmed-receipt join/grade and existing-trace spans for decode/queue/proof-ready, nonblocking wallet ownership, attempt-shell INSERT, classification reads, reservation transaction, unsigned build, local solders signature, signed identity commit, first transport dispatch/write and ACK. Preserve existing required writes; publish new measurements through the current bounded async journal, never add a synchronous telemetry writer.
- [ ] Keep `head_at_recv - source_slot` as feed-local skew with head age/generation. Attach actual received slot lifecycle status when provided by Yellowstone; bank-close/phase estimates remain labelled observations, not guaranteed remaining time. No blockTime or fixed 400 ms slot subtraction. Add a delayed head notification test that produces -1 while the transaction already belongs to S.
- [ ] Add exact-cohort summary for S+0/S+1/S+2+, failed/refused/no-submission/unresolved/missing; group native local-proof, routed confirmed-proof, graduation and ordinary fallback independently of warmth or our success. Denominator includes cold/missed preparation; warm-hit/submitted-only rates are secondary.
- [ ] Green measurement/existing timing tests; benchmark pure local signing and instrumentation with bounded replay, then report organic per-stage p50/p90 with n. Commit `feat: measure same-slot Solana execution without head-clock shortcuts`.

## Task 1: Provider profile, neutral selectors and capacity preflight

**Files:** map row 1; `tests/test_memecoin_research_rpc.py`, `tests/test_memecoin_solana_rpc_pool.py`.

**Interfaces:** new `provider_urls(role: str, env=None) -> tuple[str, ...]` in `memecoin_solana_provider.py`; roles `funded`, `signal`, `research`, `read`, `tpu`, `cohort`. New `infra_provider(env=None) -> str` returns `legacy` or `shyft`, unknown value raises ValueError. Existing `research_urls(env=None) -> list[str]` delegates research selection. New `validate_shyft_capacity(record: dict) -> tuple[str, ...]` returns refusal reasons, never secrets; used by offline preflight, not per-order code.

- [ ] Write tests for missing required URL, public URL, URL credentials leaking, legacy alias ignored in Shyft profile, funded/research overlap and account-level quota sharing. Candidate test body:

```python
import pytest
from sniper.services.memecoin_solana_provider import provider_urls

def test_shyft_profile_never_uses_legacy_research_alias():
    env = {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
           'MEMECOIN_RESEARCH_HELIUS_RPC_URL': 'https://legacy.invalid/?api-key=secret'}
    with pytest.raises(ValueError, match='research_missing'):
        provider_urls('research', env)

def test_shyft_research_selector_is_explicit():
    env = {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
           'MEMECOIN_RESEARCH_SOLANA_RPC_URLS': 'https://rpc.shyft.to/?api_key=research-test'}
    assert provider_urls('research', env) == (env['MEMECOIN_RESEARCH_SOLANA_RPC_URLS'],)
```

- [ ] Run `python3 -m pytest -n 2 tests/test_memecoin_shyft_provider_profile.py -q`; confirm red before implementation.
- [ ] Implement role mapping from spec: neutral aliases in Shyft mode, current lookup behavior in legacy. Validate HTTPS, exact supported provider hostname, nonempty authentication, duplicates, unknown role/profile and missing enabled-role setting. Endpoint hostname allowlist comes from authenticated configured Shyft products; do not accept arbitrary `evilshyft.to` suffix. Reuse existing key fingerprint/overlap helpers, not URL logging. Add profile-aware funded pool shape: Shyft >=1 distinct URL; legacy retains >=3 when pool configured. Read config once, pass it; no mutation of shared env/module globals.
- [ ] Define offline allocation record fields `role`, `account_label`, `quota_scope_label`, `rpc_rps`, `send_rps`, `grpc_connections`, `grpc_filters`, `region`, `verified_at`, `allocation_evidence`. Two key labels with same quota scope refuse isolated roles unless provider-enforced reserved capacity is documented. Add fake record tests. Do not call billing APIs from entry.
- [ ] Run the new suite plus research RPC and funded pool tests; preserve errors as unavailable/unknown. Commit `feat: add explicit Shyft Solana provider profile`.

## Task 2: Yellowstone-only processed signal coordinator

**Files:** map row 2 and `tests/test_memecoin_shyft_signal.py`, `tests/test_slot_lane_shyft_handoff.py`, `tests/test_memecoin_slot_signal.py`.

**Interfaces:** extend existing `start_signal_thread(..., shyft_config=None, measurement=None)` and `_run_signals(...)` with keyword `infra_provider='legacy'`; legacy callers stay compatible. Existing `run_feed(url, token, owners_fn, on_buy, status, stop, *, on_metadata=None, measurement=None)` remains reusable. No second decoder.

- [ ] Add a fake-network coordinator test where `ws_url=None`, Shyft is configured, and any legacy `_run` invocation raises AssertionError. Extend current async fixture rather than opening sockets. Two region updates for one `(signature, owner)` dispatch once, retain both arrival receipts, and keep first monotonic timestamp.
- [ ] Add proto subscription regression:

```python
from sniper.services.memecoin_shyft_signal import subscription
from sniper.feeds.yellowstone import geyser_pb2 as pb

def test_empty_roster_is_not_all_transactions():
    request = subscription({})
    assert not request.transactions
    assert request.commitment == pb.PROCESSED
```

- [ ] Confirm the Shyft-only fake-network test fails under current unconditional Helius startup. Run `python3 -m pytest -n 2 tests/test_memecoin_shyft_signal.py tests/test_slot_lane_shyft_handoff.py -q`.
- [ ] Branch coordinator topology once at startup: legacy keeps today's dual-feed option; Shyft starts only configured Yellowstone tasks. Build FeedArrivals from actual feeds, aggregate readiness without the existing `mirror_helius` dependency, retain bounded dedup/refusal/native ownership and metadata publication order. Select simulation/baseline/read endpoints using Task 1 signal selector instead of direct Helius variables in `start_from_context` and mint learner setup.
- [ ] Add wrong/old pong, one-region failure, all-region failure, stale head, backwards/future slot, oversized frame, queue overflow, roster change and reconnect cases. A pong attests only the matched requested roster; no all-wallet filter on empty roster. Replayed frames retain original clocks and cannot dispatch stale trades.
- [ ] Green all three suites; commit `feat: run Solana signals solely on Shyft Yellowstone`.

## Task 3: Confirmed receipt watcher with Shyft HTTP recovery

**Files:** map row 3; `tests/test_memecoin_shyft_confirmed.py`, `tests/test_memecoin_solana_source_initiation.py`, `tests/test_route_payment_closed_wsol.py`, `tests/test_pumpswap_buy.py`.

**Interfaces:** retain `confirmed_transaction(update, owners, *, head_slot, recv_monotonic, head_monotonic)` and `transaction_metadata(...)`; retain `ReceiptWorker` and watcher `process_transaction(...)`/`start_confirmed_feed(owners_fn, writer, cache)`. Add profile selection in watcher `run()`/`_run()`; `_env_helius_url` survives legacy only. HTTP recovery uses Task 1 signal URL, outside entry.

- [ ] Extend current `wire_receipt` fixture to encode the captured KRAW receipt and Santaro BuyEvent through protobuf. Assert routed payment 499050000, external fee 950000 excluded, existing initiation proof retained, and cashback-inclusive gross 6602442885. Preserve compiled instruction order, loaded addresses and exact balances; do not substitute simulated prices.
- [ ] Add profile startup test: all Helius variables absent, fake confirmed streams acknowledge the current roster, HTTP stub records Shyft-only fallback for incomplete metadata, confirmed writer dispatches once. Current `_select_key()` must fail this test before the change.
- [ ] Pin confirmed stream authority with tests that a processed update may prepare a candidate but cannot call confirmed proof publication; error/meta absent, signature mismatch, nonsigner, unsupported token opcode, missing stack depth/duplicate groups, versioned lookup mismatch and failed receipt remain unknown.
- [ ] Minimal watcher restructure: select neutral signal HTTP before startup, start confirmed Yellowstone without legacy logsSubscribe when Shyft-only, preserve durable observation writer/read-only observers and source-sell notifications. Add bounded Shyft HTTP retrieval for unsupported wire parsing; keep original receipt time and 500 ms window. Stream-replay coverage >150 slots stays gap/censored; queued recovery cannot trigger expired automatic entries.
- [ ] Run confirmed/source-initiation/route/cashback tests with `-n 2`; commit `feat: migrate confirmed Solana proof and sells to Shyft`.

## Task 4: Yellowstone accounts for marks, native pool state and graduation

**Files:** map row 4; locate current stream tests with `rg --files tests | rg 'swap_marks|graduation|price_stream|cohort'` and extend their fixtures.

**Interfaces:** new `account_update(update) -> dict | None` in `memecoin_shyft_accounts.py` normalizes `{pubkey, owner, lamports, data, slot, write_version, is_startup, deleted}`; new `run_accounts(url, token, watch_fn, on_account, status, stop)` is async. `watch_fn() -> tuple[str, ...]` supplies the bounded existing dynamic watch set; `on_account(row)` feeds current materializers. One shared account stream per runner, no per-order subscriptions.

- [ ] Write proto normalization regression:

```python
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services.memecoin_shyft_accounts import account_update

def test_zero_lamports_is_account_deletion_not_missing_update():
    update = pb.SubscribeUpdate()
    update.account.slot = 123
    account = update.account.account
    account.pubkey = bytes([1]) * 32
    account.owner = bytes([2]) * 32
    account.lamports = 0
    account.write_version = 7
    row = account_update(update)
    assert row['lamports'] == 0
    assert row['deleted'] is True
    assert row['slot'] == 123
```

- [ ] Confirm red; then normalize actual protobuf fields and reject wrong key lengths, absent account envelope or malformed payload. Do not read balances/price into floats when downstream expects atomic bytes.
- [ ] Feed account bytes into existing `run_price_stream_epoch` materialization seam; preserve pool/curve identity and existing freshness thresholds. Initial seeds stay bounded Shyft HTTP off entry. Dynamic pool/vault additions update one subscription with generation-bound acknowledgement. Same-slot updates order by write_version; delayed previous-generation/startup/replayed updates cannot overwrite newer state or create latency evidence.
- [ ] Replace `start_migration_thread` / `start_migration_listeners` WS paths with an explicit program-transaction filter on the existing processed Yellowstone connection. The filter uses current validated Pump/PumpSwap program constants, remains separate from the owner filter, and forwards parsed migration logs to the existing `on_pool` callback; no all-transaction catch-all and no new connection. Test an unknown new pool becoming watched, duplicate migration frames and non-Pump unrelated logs.
- [ ] Use accepted pool/migration/account notifications to wake current graduation worker. Preserve its lost-wake correction, 400 ms baseline poll and original deadlines. Account stream is notification, not fabricated pool proof; actual owner/address/layout still validate through existing pool state logic.
- [ ] Test account deletion/recreation, startup snapshots, reversed writes, partial filter rejection, one subscription for many vaults, queue bounds, stale prior stream updates, reconnect and LP/token identity. Verify funded exact-exit quotes remain authoritative; nominal marks cannot ratchet executable peaks.
- [ ] Run account/price/graduation suites; commit `feat: stream Solana account state from Shyft Yellowstone`.

## Task 5: Yellowstone execution observer without trade authority

**Files:** map row 5; `tests/test_memecoin_execution_observer.py`, new Yellowstone cases in that file.

**Interfaces:** add `start_yellowstone_execution_observer(*, custody_pubkey, url, token, journal, status, stop)` to existing observer module. Keep WS starter for legacy rollback. Reuse Task 3 metadata parser and existing `receipt_execution_evidence`; observer has public key only, no signer, no wallet-roster insertion.

- [ ] Add tests that observer subscription includes only custody public key and `vote=False`, but does NOT filter `failed=False`: Chada/QLI failures must be observed. Failed transaction metadata is retained as typed chain error, not rejected by a successful-source-only converter. Account, slot, signature, exact transfer/fee validation remain unchanged.
- [ ] Add missing/failed metadata, idle timeout, replay, changed generation, unrelated signer, observation before attempt persistence and reconnect cases. Pending join is bounded and cannot block submit. Expected: zero executor calls and durable unknown coverage on observation gaps.
- [ ] Red first, then factor only the necessary common wire validation in `memecoin_shyft_confirmed.py`: successful-source gate stays in confirmed wrapper; observer path may accept a structurally valid failed transaction. Preserve pure existing observer calculation and exact signed identity joins; no new generic trading event framework.
- [ ] Wire runner profile selection once. Validate observer callback before deploy; make no per-order stream. Run observer plus confirmed proof tests; commit `feat: observe Solana execution through Shyft Yellowstone`.

## Task 6: Shyft custody submission and TPU metadata, no third-party fallback

**Files:** map row 6; `tests/test_memecoin_solana_rpc_pool.py`, existing submission backend tests located by `rg --files tests | rg 'submission|tpu|sender|nozomi|claim'`.

**Interfaces:** `build_solana_backend(..., env=None)` already accepts env: read Task 1 profile there. Keep current `auto_free`/`pooled_rpc`/`rpc` modes and same-bytes transport; profile only selects allowed Solana providers. `resolve_tpu_sidecar(rpc_url=...)` and Rust protocol stay unchanged unless verified Shyft SDK compatibility needs a narrow fix.

- [ ] Fake-client test in Shyft profile asserts provider set contains Shyft custody RPC plus existing local TPU and no public endpoint, Sender or Nozomi. Reject conflicting A/B flags at startup; EVM backend test proves unchanged behavior. Fail red before modifying factory.
- [ ] Test sendTransaction HTTP timeout/429/malformed response is ambiguous and does not rotate WRITE endpoint; only existing documented explicit pre-admission rejection rotates. Shyft error bodies must not be treated as Helius credit exhaustion. Preserve signature and bytes on every attempt/resend.
- [ ] Suppress construction/warm/probe of official-public providers in Shyft profile; retain TPU launch and Shyft custody provider. Keep `pooled_rpc` Shyft-only rollback available inside final profile. Do not use `pooled_rpc` to accidentally switch TPU off for normal final mode.
- [ ] Run bounded existing `memecoin_tpu_read_provider_control.py` with separate scratch socket/child and Shyft TPU endpoint: chain identity, slot/head schedule, getClusterNodes, SDK WS behavior, two warmed leaders, 1000 ms/two-slot guards. Health frames only; no signed bytes. Clean up only that spawned child; never remove live socket. Record p50/p90 metadata refresh and stale-head ages, not fill latency.
- [ ] Run transport/pool/TPU/claim suites and protocol checks; commit `feat: restrict Solana transport to Shyft RPC and local TPU`.

## Task 7: Migrate every active worker and remove fallback leaks

**Files:** `sniper/services/memecoin_research_rpc.py`, `memecoin_solana_swap_marks.py`, `memecoin_refusal_capture.py`, `memecoin_rh_deposit_proof.py`, `memecoin_paper_sol_ticks.py`, `memecoin_coin_age.py`, `memecoin_helius_evidence.py`, `memecoin_sol_cohort.py`, `memecoin_exit_followup.py`; `scripts/memecoin_pool_prewarm.py`, `memecoin_sol_cohort_stream.py`, `memecoin_sol_cohort_watchdog.sh`, `memecoin_sol_tape_worker.py`, `memecoin_cluster_gate_worker.py`, `memecoin_exit_tape_follow.py`, `memecoin_runner_loop.py`; live maintenance callers `memecoin_reclaim_ata_rent.py`, `memecoin_swap_sol_usdc.py`, `memecoin_backfill_derisk.py`, `repair_unbooked_derisk.py`; current active callers discovered by inventory. New `tests/test_memecoin_shyft_runtime_inventory.py`.

**Interfaces:** all active read callers use Task 1 selectors or existing `build_research_call`; cohort uses Task 4 account stream and Shyft HTTP seeds. Keep historical script names/DB evidence formats when renaming gives no behavior benefit.

- [ ] Inventory actual runner/web/watchdog/cron/systemd descendants and enabled flags using read-only process inspection; redact endpoints. Classify every Solana network role, including custody reconciliation, rent/bridge utilities, backfill, pool lookup, tape and cohort. Record file/function/role/profile selector; no known active caller may remain unmapped. Search all repo callers, not only the initial list:

```bash
rg -n 'RESEARCH_HELIUS|ONCHAIN_WATCH_HELIUS|SOL_COHORT_HELIUS|helius-rpc\.com|api\.mainnet-beta\.solana\.com|publicnode' sniper scripts bot --glob '*.py' --glob '*.sh'
```

- [ ] Write fake-network startup/config tests for each enabled worker with legacy keys absent and Shyft neutral settings supplied. A test HTTP/WS connector records hostnames and rejects any non-Shyft Solana endpoint. Tests must execute startup and at least one request/subscription path; a string scan alone is insufficient. Disabled workers stay disabled and open zero streams.
- [ ] Replace direct aliases with selectors; remove Helius hostname/key-format assumptions in active `memecoin_helius_evidence` and research helpers without weakening credential/role isolation. Keep old names compatible in legacy mode only. Update cohort watchdog enable predicate to neutral variable in Shyft profile. Move cohort/account stream to Yellowstone; tape transaction retrieval stays paced Shyft HTTP, no inline entry decoding.
- [ ] Generalize provider exhaustion diagnostics only where verified Shyft error semantics require it; preserve rate-limit backoff and return unknown instead of zero/empty success. No research fallback onto funded credentials.
- [ ] Make Yellowstone the sole source of automatic Solana wallet buy/sell triggers; external J7/Fomo hints may remain display/route-hint data. Preserve the existing verified backup route, ordinary fallback policy and its gates: successful confirmed Yellowstone proof can initiate the same backup path when native ownership permits it, without requiring an external buy trigger. Do not remove Jupiter fallback coverage. Test native owner refusal, native confirmed failure, verified backup proof, external hint alone (no admission), and existing positions receiving source-sell exits. Manual entries keep ordinary gates and Shyft execution.
- [ ] Green affected worker/exit tests and inventory smoke; commit `feat: complete Shyft routing for active Solana workers`.

## Task 8: Warm requirements and unsigned construction before authoritative arrival

**Files:** `sniper/services/memecoin_slot_lane.py::LanePrefetcher`, `::RoutedHandoff`, existing native builders `sniper/execution/pump_curve_buy.py`, `pumpswap_buy.py`, `raydium_launchlab_buy.py::WarmExoticLegs`, `sniper/services/memecoin_shyft_signal.py`, `sniper/services/memecoin_shyft_confirmed.py`, `sniper/execution/solana_real_seams.py`; affected builder/warm/preparation tests plus new `tests/test_solana_same_slot_ready_path.py`.

**Interfaces:** reuse `LanePrefetcher.snapshot() -> PrefetchSnapshot`, `WarmExoticLegs.seed(venue, quote_mint, usdc_atomic, *, builder, facts)`, `get(venue, quote_mint, usdc_atomic, *, builder=None, facts=None, quote_params=None)`, and existing builder `build(buy, clip_usd, snapshot, *, funding_lamports=None, ...)`. Feed accepted structured Yellowstone metadata to current pool caches and on-demand unsigned worker. Do not add a generic cache framework or replace signer.

- [ ] Build a ready native fake fixture using current builder tests. Stub every provider HTTP/RPC function to raise `AssertionError('ready path made network request')`; admit/build/sign/bind identity/first dispatch must still succeed. Simultaneously assert reservation and signed-payload commits occur before send. Add cold fixture that stays on the existing typed cold/preparation path, without granting an admission override.
- [ ] Red first: select only currently demonstrated blocking prerequisites from Task 0. Do not invent a speed bug when the ready lane already makes zero provider calls; prove cold-field redundancy or fallback waiting with an exact fixture before changing that field's producer.
- [ ] Feed validated account deltas and full metadata into existing state producers: token program/decimals, pool address/layout, fee recipients/global state, custody ATA existence, funding balance and blockhash/expiry. Bound generation/slot/write_version and existing freshness rules. Source account balances do not satisfy custody prebalance. Stream deletion/ATA close invalidates readiness; unknown stays unknown. Initial seeds remain off-path Shyft reads.
- [ ] Produce the preparation dependency matrix in the audit: for each entry-required field, current producer, identity binding, freshness/expiry, stream/API replacement, cold behavior and measured blocking time. Fields include ordinary backup market cap, mint metadata, token prebalance, fee/safety facts, venue/pool state, ATA setup, lookup tables, funding balances, blockhash, approved clip and price floor. `reserve_entry`, current book/exposure/dedup/loss and identity CAS are marked authoritative, not cacheable permission.
- [ ] Reuse compiled unsigned instruction skeletons/validated ALT accounts already stored by builders. At authoritative arrival, bind current approved clip, current source floor/expected output, exact account/pool/fee configuration and valid blockhash; then perform local solders signature. Never pre-sign, share a signed payload among candidates, reuse stale min-output or omit idempotent account setup to gain time.
- [ ] Route processed preparation and subsequent confirmed proof through the existing watcher-to-runner wake and RoutedHandoff. Preparation completion cannot become proof; matching late proof resumes only inside the original 500 ms window. Confirmed-only routes with proof after source-slot closure remain a measured constraint.
- [ ] Keep current WarmExoticLegs bounds (two hot keys, four real HTTP starts per 1.5 s, existing backoff) initially. Use the verified Jupiter Developer tier/general 10 req/s budget and measured total workload; separate credentials do not increase org quota. Run a pure fixture control of current Jupiter request chain versus subscription-supported Swap V2 `/build` off-path; add a live read-only control only within the actual available request budget. A single-request builder may replace a redundant preparation chain only after exact quote/floor/fees/ALT/blockhash/packet/token-safety parity tests pass. `mode=fast` remains a separately measured quote-quality choice, never enabled by assumption. Managed Jupiter execute is outside this migration.
- [ ] Test clip mismatch, config generation change, stale/expired blockhash, changed pool/fees, missing/closed ATA, Token-2022 hook requirements, wrong ALT, unseen mint and changing state between prep and reserve. Concurrent entries must still reserve once and current global exposure remain authoritative. Failed prepared state cannot hide itself as zero balance or confirmed no-route.
- [ ] Run affected suites `-n 2`; record ready path authority-to-dispatch p50 <=10 / p90 <=25 ms and local signing p90 <=1 ms as engineering targets. Failed targets are findings; do not add timeout-based order refusal or bypass checks. Commit `perf: prepare Solana entry requirements before source authority` only for witnessed changes.

## Task 9: Use entitled RabbitStream for unsigned early preparation

**Files:** new `sniper/services/memecoin_shyft_prepare.py`, `sniper/services/memecoin_shyft_signal.py`, `memecoin_slot_signal.py`, existing slot-lane on-demand worker/warm cache; new `tests/test_memecoin_shyft_prepare.py`; provider profile/capacity tests.

**Interfaces:** new pure `preparation_candidate(update, owners, *, recv_monotonic, generation) -> dict | None` returns exact structural identity `{signature, owner, slot, recv_monotonic, generation, account_keys, instructions}` only. New `run_prepare_feed(url, token, owners_fn, on_prepare, status, stop)` is async; `on_prepare(candidate)` enqueues to existing bounded preparation worker and cannot execute a trade. New optional setting `MEMECOIN_SHYFT_PREPARE_GRPC_URL`, required only when `MEMECOIN_SHYFT_PREPARE_ENABLED=1`; actual production RabbitStream endpoint must be account-entitled and validated.

- [ ] Write no-meta proto fixture and assert structural candidate decode succeeds without `meta`; the existing native authority decoder still rejects it. A spy reserve/sign/send callback must remain uncalled. Fixture includes signature/owner mismatch, nonsigner, invalid keys/ALT identities, unsupported instruction format and ambiguous route cases; those produce preparation misses, not a fallback grant.
- [ ] Write subscription test proving no slots/accounts/blocks filters and no success/error assertion. Yellowstone subscription owns head/account/proof, RabbitStream only transaction intent. Do not use `failed=False` as proof of success; failed/abandoned source transactions can occur later.
- [ ] Run `python3 -m pytest -n 2 tests/test_memecoin_shyft_prepare.py -q`; confirm red. Implement one transaction-filtered authenticated feed using existing grpcio/protobuf, not raw UDP or a new Rust decoder. Limit pending intent to 128 exact candidates and two seconds of prep retention; overflows expire with typed coverage/miss. These bounds do not reset or extend the independent authoritative 500 ms execution window. Reuse Task 8 fetch budget, do not add another HTTP budget.
- [ ] Start harmless candidate-specific mint/pool/ATA/ALT/build preparation only when identities are actually decoded. No exact source fill or source minimum can be derived from requested instruction amounts alone. Match authoritative Yellowstone signature, signer owner, source slot and venue/mint/pool/account keys before using prep. Rebind final approved clip, latest floor/config/blockhash locally. Missing resolved lookup addresses are a miss; provider ALT filtering coverage does not prove all addresses needed by our own builder.
- [ ] Test processed-first, Rabbit-first, duplicate region/reconnect/replay, authoritative failed receipt, no follow-up receipt, venue mismatch, expired prep, wrong owner/slot, and changed clip. All must retain one authoritative candidate and no extra funded authority. Original signal receipts and prep receipt timestamps remain separate telemetry surfaces.
- [ ] Extend capacity verifier to eight steady connections including Rabbit plus at least one reconnect slot. Serialize reconnect overlap as necessary to stay within actual entitlement; disable/skip this producer if not purchased, explicitly recording the speed-tool gap. Green prep, signal/dedup, warm-bound and proof suites; commit `perf: prepare Solana candidates from Shyft RabbitStream`.

## Task 10: Shadow comparison, pinned cutover and rollback proof

**Files:** new `scripts/research/shyft_migration_preflight.py`, new `tests/test_shyft_migration_preflight.py`, `scripts/memecoin_runtime_verify.py`, `scripts/deploy_memecoin_runtime.sh`, `AGENTS.md`, current free-direct/credit-rotation specs, new `docs/audits/2026-10-11-shyft-yellowstone-migration/` evidence folder. Amend exact obsolete Helius/public policy paragraphs, not historical incident records.

**Interfaces:** preflight CLI `--state-root PATH --allocation-record PATH --output PATH` is read-only, no signer loading or funded DB writes. Report includes per-role redacted provider fingerprints, region RTT/slot freshness, method capability, connection budget, current producer topology and refusal reasons. Runtime verify adds profile/topology checks to existing manifest/config checks.

- [ ] Red fake preflight cases: wrong genesis, unverified/shared quota, too few purchased connections, stale head, unsupported getSlotLeaders/transaction version, gRPC auth rejected, incomplete subscription ack, hidden public fallback and secret-bearing output. Assert zero sendTransaction calls. Prove its checker fails against a known wrong provider graph.
- [ ] Inspect authenticated Shyft account without changing subscription or buying capacity. Record explicit production endpoints, funded/research/TPU allocation, actual method/send/filter/connection limits, RabbitStream access and ALT behavior, accelerated discovery entitlement, and Shyft submission/stake/routing product. Record the tradeoff from removing current Sender/Nozomi delivery paths; a metadata endpoint is not a replacement delivery entitlement. Verify region reachability from the trading host, not from a remote benchmark. If capacity is insufficient, name required entitlement; do not invent endpoint/accounts or repeatedly request keys in chat.
- [ ] Select nearest measured primary and independent Shyft region secondary. Run bounded dual-feed observer under legacy profile, preserving original funded feed ownership. Use existing feed-arrival/receipt measurement, 30 minutes or 200 eligible signatures maximum. Report paired arrival and usable-proof p50/p90, missing-only signatures and union denominator, plus actual successful S+0/S+1/S+2+ acquisitions and failures/refusals/no-submission/unresolved outcomes from the frozen stratum cohorts; ignore replay in speed samples. Baseline/candidate load must fit documented quotas. No duplicate executor invocation.
- [ ] Shadow topology/correctness must pass before activation. Review actual source/confirmed sell coverage and all provider method tests. Prepare `.env` as a private staged file, complete neutral mappings, production gRPC URLs, profile `shyft`, A/B flags off and no active public fallback. Compare exact numeric policy/roster/signing keys before/after; changes refuse. Missing speed samples remain unproved, not an invented benefit.
- [ ] Run the required scoped/core deployment suites once under shared host lock, two workers. Run import/protobuf compatibility and affected Rust checks. Do not install grpcio-tools into the funded runtime; use already-pinned runtime grpcio/protobuf and vendored generated classes. Obtain final merged SHA after peer changes, build manifest-bound sidecar artifact for that SHA.
- [ ] Extend normal deployment's existing atomic env/release rollback to apply the staged provider profile. Fail before stopping if preflight/allocation/profile validation fails. Reconcile signed/reserved state, drain normal entry gate, recheck after stop, switch runner/web and affected worker roots, preserve DB/state locations. Execute normal deployment with `--restart --defer-backup`; no independent ad-hoc funded restart or bypass.
- [ ] Fake deployment interruption tests at env-write, old-stop, manifest-switch and new-start verify exact rollback and no lost claims/duplicate sends. Startup must not leave watcher on old Helius while runner is Shyft; all affected process generations are accounted for.
- [ ] Fresh proof: exact SHA, sole runner and watcher PIDs, actual gRPC regions and HTTP/TPU endpoint fingerprints, no Helius/public/Nozomi/Sender sockets or request attempts, current roster ack, quote/exit readiness, observer coverage status, funded reservation reconciliation, authenticated HTTP health. A static env file alone cannot pass. Keep credentials private and record only redacted allocation/topology.
- [ ] Watch 15 minutes of ordinary operation with bounded read-only samples. Correctness failures trigger normal rollback; no organic buys means speed unproved. Append organic measurements as available without keeping the implementation turn open for an arbitrary trade sample. Topology/correctness passing closes the migration milestone only. Same-slot execution remains open/unproved until the forward outcome report supports it; no arbitrary extra funded trades or perpetual implementation turn to create a sample. Record measured local targets and the exact remaining blocker if ordinary trades still land S+2.
- [ ] Commit red/green tests, review decisions, baseline/after reports, provider capacity facts and deployed SHA. Review the spec and plan before starting implementation; select subagent-driven or native execution. Final statement must distinguish complete migration from measured improvement and <=200 ms execution proof.

## Execution order and done criteria

Tasks 0 → 1 → 2 → 3 → 4 → 8 → 9 → 5 → 6 → 7 → 10. Each task: red witness, focused green tests, one logical commit, independent review. Do not begin final activation until the complete provider graph, quota allocation and rollback test pass. No production change occurs as part of writing this plan.

Migration done: every enabled Solana role is Shyft-only in actual processes; Yellowstone owns authoritative streams; signed transaction/exit/proof invariants pass; rollback is executable and required tests pass. Execution goal is separate: report confirmed successful acquisition in source slot with exact cohort counts; ACK, head -1, faster detection or topology alone cannot close it. A 30-event minimum per claimed stratum is an evidence floor, not a guarantee or funding promotion rule.

## Self-review

Spec coverage: provider/roles/capacity (1), processed signals/receipt clocks (2), confirmed proof/sells/recovery (3), account marks/graduation (4), execution observer/errors (5), custody/TPU/WRITE ambiguity (6), all workers/external automatic fallback (7), slot/stage measurement (0), warm ready path (8), entitled RabbitStream preparation (9), deployment/rollback/current-doc amendments (10).

Review Focus maps to explicit negative tests above. No new quote service, trading strategy, custom raw-shred engine, signer rewrite, protobuf regeneration or Rust rewrite is required. Existing unknown proof and supported venue limitations remain.

Review revision 2026-10-11: migration-only completion was insufficient for the same-slot objective. Task 0 removes clock ambiguity; Tasks 8/9 prepare entry requirements; Task 10 grades actual execution and preserves distinct proof/venue strata. Existing signing/wake/caches are reused. See `docs/audits/2026-10-11-shyft-same-slot-plan-review.md`.
