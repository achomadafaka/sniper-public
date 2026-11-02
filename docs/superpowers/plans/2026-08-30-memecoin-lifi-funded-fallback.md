# LI.FI Funded Fallback Implementation Plan

> **For agentic workers:** Use `superpowers:test-driven-development` for each task. Use `superpowers:verification-before-completion` before each commit.

**Goal:** Add a default-off LI.FI fallback for safe chain-4663 entries and exits after an exact direct-route miss.

**Architecture:** Keep the direct EVM path unchanged. Add one pure route module and one lifecycle executor. Reuse the current EVM signer, sender, receipt reads, risk gates, heartbeat, and SQLite ledger.

**Tech stack:** Python, SQLite, `eth_abi`, Flask, JavaScript, pytest, JSON-RPC, and the current content-addressed memecoin runtime.

## Authority and scope

- The approved safety and quota specifications control this implementation.
- The implementation must stay default off.
- This plan does not approve a deploy, restart, canary, or funded activation.
- Public mode permits read-only probes and one separately approved manual canary.
- Authenticated mode requires current provider quota headers.
- Direct entries and exits must remain available if LI.FI has a fault.
- Do not add an SDK or a dependency.
- Do not change a funded gate, cap, clip, rail, or cadence.
- Do not run the full test suite.

The first release will permit `nordstern` only.

`fly` remains research-only. Its `swapWithBackendSignature(bytes)` payload has no complete local schema in the inspected LI.FI contract source.

The audit retains the `fly` buy and sell proof. That proof does not grant execution authority.

The sell proof used block `50375987` with hash `0xb8900ffa971df2dc9782a1cb7ed84043170cab2b531b5cb97c753530152545c9`.

Both proof RPCs agreed on the Diamond, facet, fee target, and `nordstern` code hashes at that block.

---

### Task 1: Add the release pin-set and fail-closed settings

**Files:**

- Create: `sniper/execution/lifi_4663_manifest.json`
- Modify: `sniper/settings/memecoin.py`
- Modify: `scripts/memecoin_runtime_verify.py`
- Modify: `scripts/deploy_memecoin_runtime.sh`
- Create: `tests/test_memecoin_lifi_manifest.py`
- Modify: `tests/test_memecoin_runtime_verify.py`

**Interfaces:**

- `load_lifi_manifest(path) -> (manifest, canonical_hash)`
- `cfg.memecoin_lifi_mode() -> disabled | public_probe | authenticated_funded`
- `cfg.validate_lifi_config() -> None`, or it raises `RuntimeError`

- [ ] **Step 1: Write the RED manifest tests**

Add tests for these rules:

1. The manifest chain must equal `4663`.
2. Canonical JSON must use sorted keys and compact separators.
3. The canonical SHA-256 value must be stable.
4. `allowExchanges` must equal `['nordstern']`.
5. The top-level type must equal `lifi`.
6. The top-level tool must equal `nordstern`.
7. The first step must equal `feeCollection/protocol`.
8. The second step must equal `nordstern/swap`.
9. The service fee numerator must equal `25`.
10. The service fee denominator must equal `10000`.
11. The integrator fee must equal zero.
12. Each target, spender, selector, ABI, facet, and code hash must exist.
13. Permit2, Permit2Proxy, and each permit selector must exist in the reject set.

Use the audited authority values:

```text
Diamond:       0xB477751B76CF82d00a686A1232f5fCD772414Af3
approval spender: 0xB477751B76CF82d00a686A1232f5fCD772414Af3
Diamond hash:  0xca0fd158089c97d9e77828a4254bc7037be8ee6f784e45d80204df0e56013101
outer selector: 0x5fd9ae2e
facet:         0xb129ce9c3fcd55726ff314a2764d3937fa496071
facet hash:    0xdb3f706ca7f78237a197ccab9300628db168f0bc6c509835771200f9c163daa5
fee target:    0xf4bffe4dfc693f37715a47c15bda8af9ed8f7cf1
fee hash:      0x7ee455a6853068874bfd201f93d6383ed6d88934a922316db5575b057e2ebe74
fee selector:  0x332d746b
nordstern:     0x603206d6105217dd972e4ab30676a220ca393346
nordstern hash: 0x5c9dd55aa2b736702f8cc44aef400fca06ea1b708f35f32978bc4ba37ec4d30f
nordstern selector: 0x3f0bde25
```

Pin these ABI signatures:

```text
swapTokensMultipleV3ERC20ToERC20(bytes32,string,string,address,uint256,(address,address,address,address,uint256,bytes,bool)[])
forwardERC20Fees(address,(address,uint256)[])
IceCreamSwap()
facetAddress(bytes4)
allowance(address,address)
approve(address,uint256)
```

- [ ] **Step 2: Verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_lifi_manifest.py tests/test_memecoin_runtime_verify.py -q
```

Expected: FAIL because the manifest and settings do not exist.

- [ ] **Step 3: Add the minimum settings**

Add these environment reads:

```text
MEMECOIN_LIFI_MODE=disabled
MEMECOIN_LIFI_API_URL=https://li.quest/v1/quote
MEMECOIN_LIFI_API_KEY=
MEMECOIN_LIFI_PROOF_RPC_URL_4663=
MEMECOIN_LIFI_ACTIVE_RELEASE_SHA=
MEMECOIN_LIFI_ACTIVE_MANIFEST_HASH=
```

Use the current quote age and gate values. Do not add duplicate knobs.

`authenticated_funded` must require a key, a proof RPC, a terminal canary lifecycle, and an activation record.

The canary lifecycle and activation record must match the current release SHA and manifest hash.

`public_probe` must reject each automated entry and exit call.

- [ ] **Step 4: Bind the manifest to the runtime release**

Add the manifest hash to the deploy candidate manifest.

Make `memecoin_runtime_verify.py` recalculate the hash from the release file.

Reject an absent file, a dirty file, a hash mismatch, or a chain mismatch.

Do not read authority from mutable state.

- [ ] **Step 5: Verify GREEN**

Run:

```bash
python3 -m pytest tests/test_memecoin_lifi_manifest.py tests/test_memecoin_runtime_verify.py tests/test_memecoin_runtime_deploy.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 6: Commit Task 1**

```bash
git add sniper/execution/lifi_4663_manifest.json sniper/settings/memecoin.py \
  scripts/memecoin_runtime_verify.py scripts/deploy_memecoin_runtime.sh \
  tests/test_memecoin_lifi_manifest.py tests/test_memecoin_runtime_verify.py
git commit -m "feat(memecoin): pin LI.FI chain authority"
```

---

### Task 2: Add the pure decoder, fee arithmetic, and quota guard

**Files:**

- Create: `sniper/execution/lifi_route.py`
- Create: `tests/test_memecoin_lifi_route.py`
- Create: `tests/test_memecoin_lifi_quota.py`

**Interfaces:**

- `decode_quote(response, request, manifest) -> DecodedQuote`
- `fee_atomic(input_atomic) -> int`
- `economic_roundtrip(input_atomic, buy, sell, cap_bps) -> GateResult`
- `QuotaGuard.admit_entry(slots_used, in_flight) -> GateResult`
- `QuotaGuard.reserve_request(role) -> context manager`

- [ ] **Step 1: Write the RED fee and sequence tests**

Generate synthetic calldata with `eth_abi.encode` and the release ABI.

Test at least these exact inputs:

```text
1 -> fee 0
399 -> fee 0
400 -> fee 1
1000000000000000 -> fee 2500000000000
6528997907204926094418 -> fee 16322494768012315236
525403522853228280622757 -> fee 1313508807133070701556
```

Require this formula for every input:

```python
fee = input_atomic * 25 // 10_000
```

Reject each wrong step count, step order, step type, tool, fee, token, amount, and atomic link.

Require one fee distribution. Require its amount to equal the calculated fee.

Require the fee input token to equal the request input token.

Require the fee output to equal `input_atomic - fee_atomic`.

Require the swap input to equal the fee output.

Require the fee output and swap input to use the same token.

Require exact calldata consumption. Reject malformed offsets, unused bytes, and trailing bytes.

Require `IceCreamSwap()` calldata to contain exactly its four-byte selector.

- [ ] **Step 2: Write the RED semantic tests**

Reject each of these forms:

- a chain other than `4663`;
- a bridge option;
- an empty exchange list;
- `all` or `default` exchange authority;
- a receiver other than custody;
- a wrong input or output token;
- a wrong exact input;
- a wrong minimum output;
- a non-zero transaction value;
- a wrong `callTo` or `approveTo`;
- a wrong selector or token path;
- a third step;
- a Composer call;
- Permit2, Permit2Proxy, EIP-2612, or typed data;
- a non-zero integrator fee;
- a service fee other than `0.0025`;
- a response without `includedSteps`.

Hash the request, response, calldata, and decoded semantics.

Do not return raw calldata in a trace object.

- [ ] **Step 3: Write the RED economic tests**

Use integer atomic values only.

Set the final sell input to the refreshed buy `toAmountMin`.

Set guaranteed proceeds to the final sell `toAmountMin`.

Prove that both service fees affect the current round-trip cap.

Use audited buy-side and sell-side `nordstern` fixtures.

Verify the tool, selector, target, facet, code hash, and atomic continuity for both sides.

Test the exact pass and refuse boundary for the current cap.

- [ ] **Step 4: Write the RED quota tests**

Test these boundaries with zero and non-zero in-flight requests:

```text
canary: 6 refuses, 7 permits
one position: 7209 refuses, 7210 permits
two positions: 14415 refuses, 14416 permits
```

Test these reserve values:

```text
one-position exit reserve: 7206
two-position exit reserve: 14412
entry request cost: 4
canary request cost: 7
```

Prove that an exit request wins the lock before an entry request.

Prove that an entry cannot consume exit reserve.

- [ ] **Step 5: Verify RED**

Run:

```bash
python3 -m pytest tests/test_memecoin_lifi_route.py tests/test_memecoin_lifi_quota.py -q
```

- [ ] **Step 6: Implement the pure module**

Use `json.dumps(..., sort_keys=True, separators=(',', ':'))` for canonical JSON.

Use `hashlib.sha256` for stored content hashes.

Use `eth_abi.decode` only with ABI types from the manifest.

After decode, re-encode each value and require exact byte equality.

This equality check rejects unused and trailing bytes.

Use one `threading.Condition` for request priority and the in-flight count.

Do not add a cache, background thread, SDK, or retry adapter.

- [ ] **Step 7: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_route.py tests/test_memecoin_lifi_quota.py -q
git diff --check
git add sniper/execution/lifi_route.py tests/test_memecoin_lifi_route.py \
  tests/test_memecoin_lifi_quota.py
git commit -m "feat(memecoin): validate LI.FI quote authority"
```

---

### Task 3: Add append-only lifecycle storage

**Files:**

- Modify: `sniper/persistence/db_memecoin.py`
- Create: `tests/test_memecoin_lifi_lifecycle.py`

**Interfaces:**

- `start_lifi_lifecycle(conn, pending_id, ...) -> lifecycle_id`
- `append_lifi_event(conn, lifecycle_id, expected_state, new_state, ...) -> bool`
- `latest_lifi_lifecycle(conn, lifecycle_id) -> dict | None`
- `unresolved_lifi_lifecycles(conn, chain_id, custody) -> list[dict]`
- `lifi_slots_used(conn, chain_id, custody) -> int`
- `finalize_lifi_entry(conn, ...) -> position_id`
- `finalize_lifi_close(conn, ...) -> bool`

- [ ] **Step 1: Write the RED schema and CAS tests**

Create `memecoin_lifi_authority_events` in `ensure_memecoin_tables()`.

Use this minimum schema:

```sql
CREATE TABLE memecoin_lifi_authority_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lifecycle_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    pending_id INTEGER NOT NULL,
    position_id INTEGER,
    expected_prior_state TEXT,
    state TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stage TEXT,
    chain_id TEXT NOT NULL,
    custody_wallet TEXT NOT NULL,
    token TEXT,
    spender TEXT,
    amount_atomic TEXT,
    input_atomic TEXT,
    output_min_atomic TEXT,
    fee_atomic TEXT,
    allowance_atomic TEXT,
    settlement_in_atomic TEXT,
    settlement_out_atomic TEXT,
    tx_hash TEXT,
    tx_identity TEXT,
    nonce TEXT,
    signed_payload_sha256 TEXT,
    block_number TEXT,
    block_hash TEXT,
    proof_sha256 TEXT,
    request_sha256 TEXT,
    response_sha256 TEXT,
    calldata_sha256 TEXT,
    decoded_semantics_sha256 TEXT,
    release_sha TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    tool TEXT,
    provider_failure_type TEXT,
    quota_limit TEXT,
    quota_remaining TEXT,
    quota_reset TEXT,
    detail_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(lifecycle_id, sequence)
)
```

Add `lifi_dependent INTEGER NOT NULL DEFAULT 0` to `memecoin_positions`.

Add a partial unique index on `pending_id` for sequence zero.

Add a partial unique index on non-null `position_id`.

Add a trigger that rejects each delete from the lifecycle table.

Add a trigger that rejects each change to `memecoin_positions.lifi_dependent`.

Test that `open_position()` sets this value only in the insert operation.

Do not add a later update function for this field.

Test these invariants:

1. Sequence zero owns one reservation.
2. A stale expected state cannot append an event.
3. A failed CAS causes a fresh row read.
4. Each atomic amount uses canonical decimal text.
5. Each revoke attempt receives a new sequence.
6. One lifecycle links one position once.
7. A linked open position and its lifecycle count once.
8. An unresolved pre-position lifecycle counts once.
9. An invalid lifecycle stays unresolved and counts once.
10. No delete function exists.
11. A direct SQL delete fails.
12. A direct SQL update of `lifi_dependent` fails.
13. Each stored atomic amount uses canonical decimal text.
14. A canary terminal event binds release, manifest, custody, chain, token, and each allowance proof.

- [ ] **Step 2: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_lifecycle.py -q
```

- [ ] **Step 3: Implement transaction helpers**

Use `BEGIN IMMEDIATE` for each CAS append and terminal transaction.

Add `commit=True` to `book_full_exit_and_finalize()`.

Add `commit=True` to `confirm_pending()`.

Keep its current default behavior.

`finalize_lifi_entry()` must complete these actions in one transaction:

1. Insert the real position with `lifi_dependent=1`.
2. Insert the entry fill.
3. Confirm the pending reservation.
4. Append the lifecycle position link.
5. Commit once.

`finalize_lifi_close()` must complete these actions in one transaction:

1. Book the full exit and close the position.
2. Append the terminal lifecycle event.
3. Commit once.

- [ ] **Step 4: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_lifecycle.py \
  tests/test_crash_windows.py tests/test_memecoin_wo6_exit_intent.py -q
python3 scripts/validate_schema_refs.py
git diff --check
git add sniper/persistence/db_memecoin.py tests/test_memecoin_lifi_lifecycle.py
git commit -m "feat(memecoin): store LI.FI authority lifecycle"
```

---

### Task 4: Add one-request HTTP and on-chain authority proof

**Files:**

- Create: `sniper/execution/lifi_executor.py`
- Create: `tests/test_memecoin_lifi_authority.py`
- Create: `tests/test_memecoin_lifi_client.py`

**Interfaces:**

- `LifiClient.quote(request, role, stage) -> QuoteResult`
- `prove_quote_authority(decoded, request_hash, response_hash, rpc, proof_rpc) -> Proof`
- `prove_execution_block(unsigned_tx, decoded, ...) -> Proof`
- `audit_receipt_block(receipt, decoded, ...) -> ProofResult`

- [ ] **Step 1: Write the RED client tests**

Use an injected HTTP function.

Require one network call per request. Set all transport retry counts to zero.

Require these query controls:

```text
fromChain=4663
toChain=4663
allowBridges=<explicit empty>
allowExchanges=nordstern
integrator fee=0
simulation enabled
current maxPriceImpact
```

Bind the response to the complete request, corrected receive time, and quota headers.

Map timeout, HTTP 429, provider error, malformed response, and `no_route` separately.

Do not store the API key in any result or exception text.

- [ ] **Step 2: Write the RED proof tests**

Use injected JSON-RPC responses.

For each quote response, require one distinct canonical block proof.

Require custody RPC and proof RPC agreement for the block hash.

Check every target with `eth_getCode` at that block.

Check the Diamond selector mapping with `facetAddress(bytes4)` at that block.

Check each spender and each executable target.

Reject each address, selector, facet, bytecode, ABI, and block mismatch.

Reject proof reuse between initial buy, initial sell, refreshed buy, and final sell.

- [ ] **Step 3: Write the RED execution-block tests**

Require this exact order:

1. Estimate gas.
2. Construct the complete unsigned transaction.
3. Select one canonical block.
4. Recheck all code authority at that block.
5. Check all pinned allowances at that block.
6. Simulate the exact final transaction at that block.
7. Sign without another provider call.
8. Persist the proof before send.

Reject a semantic mismatch even when simulation succeeds.

Require the selected spender allowance to equal the exact input.

Require every other pinned LI.FI spender allowance to equal zero.

Require Permit2 and Permit2Proxy allowances to equal zero.

- [ ] **Step 4: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_client.py \
  tests/test_memecoin_lifi_authority.py -q
```

- [ ] **Step 5: Implement the network boundary**

Reuse `evm_wallet._rpc`, `eth_call_raw`, `eth_nonce`, and the current gas helper.

Reuse the injected EVM HTTP seam. Do not add another pool.

Use the custody RPC and one configured proof RPC.

Return sanitized hashes and decoded values only.

- [ ] **Step 6: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_client.py \
  tests/test_memecoin_lifi_authority.py tests/test_memecoin_evm_leg.py -q
git diff --check
git add sniper/execution/lifi_executor.py tests/test_memecoin_lifi_client.py \
  tests/test_memecoin_lifi_authority.py
git commit -m "feat(memecoin): prove LI.FI quote code"
```

---

### Task 5: Add the dedicated approval and settlement lifecycle

**Files:**

- Modify: `sniper/execution/lifi_executor.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Create: `tests/test_memecoin_lifi_settlement.py`
- Create: `tests/test_memecoin_lifi_approval.py`

**Interfaces:**

- `approve_exact_and_wait(lifecycle, token, spender, amount) -> ApprovalResult`
- `revoke_and_wait(lifecycle, token, spender) -> RevokeResult`
- `settle_entry(receipt, balances, decoded) -> SettlementResult`
- `settle_exit(receipt, balances, decoded) -> SettlementResult`

- [ ] **Step 1: Write the RED approval tests**

Require classic `approve(spender, exact_amount)` only.

Persist the signed hash, nonce, payload hash, token, spender, wallet, and amount before send.

Do not call the current generic or pipelined approval function.

Wait for the exact approval receipt before the refreshed buy quote.

Keep an ambiguous approval unresolved. Do not revoke it.

After a failed pre-sign check or confirmed swap revert, require a durable zero-approval action.

Release a failed-entry reservation only after a confirmed zero allowance.

Block new LI.FI entries while one LI.FI allowance stays unresolved.

Keep direct entries available.

- [ ] **Step 2: Write the RED settlement tests**

For entry, require these receipt facts:

1. Exact WETH input equals refreshed `fromAmount`.
2. Each WETH recipient matches decoded semantics.
3. No other WETH leaves custody.
4. Exact-mint custody input is at least `toAmountMin`.

For exit, require these receipt facts:

1. Exact-mint output equals the executed `fromAmount`.
2. Each mint recipient matches decoded semantics.
3. WETH custody proceeds are at least `toAmountMin`.
4. No unexpected custody token movement exists.

Use a current balance delta when it agrees with the receipt.

Use validated receipt transfers when an immediate balance read lags.

Keep settlement unresolved when a current balance conflicts with the receipt.

After a confirmed fill, read the exact allowance.

Open the position before any required cleanup.

A cleanup fault must not hide the position.

Audit the Diamond, facet, selector, and executable code at the canonical receipt block.

A receipt-block mismatch must not erase a confirmed fill.

It must disable each LI.FI action and keep the lifecycle unresolved.

- [ ] **Step 3: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_approval.py \
  tests/test_memecoin_lifi_settlement.py -q
```

- [ ] **Step 4: Implement with current EVM authority**

Reuse the current signer and same-bytes EVM sender.

Add a LI.FI-specific approval function beside `_approve()`.

Do not change `_approve()` or direct execution behavior.

Decode ERC-20 `Transfer` logs locally.

Accept only the exact receipt hash and exact custody address.

- [ ] **Step 5: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_approval.py \
  tests/test_memecoin_lifi_settlement.py tests/test_memecoin_evm_leg.py \
  tests/test_memecoin_fast_submit_crash_safety.py -q
git diff --check
git add sniper/execution/lifi_executor.py sniper/execution/evm_real_seams.py \
  tests/test_memecoin_lifi_approval.py tests/test_memecoin_lifi_settlement.py
git commit -m "feat(memecoin): settle LI.FI swaps safely"
```

---

### Task 6: Add the direct-first four-request entry path

**Files:**

- Modify: `sniper/execution/evm_swap_executor.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Create: `tests/test_memecoin_lifi_entry.py`

**Interfaces:**

- Add optional `lifi_entry_fn` to `execute_entry_evm()`.
- Call it only after `classify_quote_miss()` returns exact `no_route`.

- [ ] **Step 1: Write the RED direct-precedence tests**

Prove that a valid direct quote causes zero LI.FI calls.

Prove that `provider_*`, timeout, HTTP 429, and unattributed direct results cause zero LI.FI calls.

Prove that a later safety, chase, approval, or execution refusal cannot select LI.FI.

Prove that only the first direct quote miss can select LI.FI.

Prove that the attempt trace stores the direct miss and LI.FI result as separate fields.

- [ ] **Step 2: Write the RED four-request tests**

Require this exact entry sequence:

1. Initial buy quote and proof.
2. Initial sell quote and proof.
3. Exact approval and confirmed receipt.
4. Refreshed buy quote and proof.
5. Final sell quote and proof.

Reserve four requests before request one.

An early failure can use fewer requests.

Never request a fifth entry quote.

Require the initial sell input to equal the initial buy `toAmountMin`.

Require the final sell input to equal the refreshed buy `toAmountMin`.

Require the refreshed spender and exact input to match the approved values.

- [ ] **Step 3: Write the RED gate-repeat tests**

After the refreshed buy, rerun these current local gates:

- impact;
- chase;
- execution premium;
- post-peak;
- gas sufficiency;
- contract-specific round-trip cap.

Include the buy fee and sell fee in each economic value.

A local refusal must start allowance cleanup.

- [ ] **Step 4: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_entry.py -q
```

- [ ] **Step 5: Implement the smallest shared hook**

Keep the current direct quote loop unchanged.

At the current first-miss return, pass the typed miss to `lifi_entry_fn`.

Return its typed result through the current entry result and trace surfaces.

Write one trace substage for each LI.FI stage, rung, fee, gas value, quota header, and failure type.

Do not change token safety, reservation, risk, or alert order.

- [ ] **Step 6: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_entry.py \
  tests/test_memecoin_evm_quote_miss_attribution.py tests/test_memecoin_evm_leg.py \
  tests/test_memecoin_signal_speed_fixes.py -q
git diff --check
git add sniper/execution/evm_swap_executor.py sniper/execution/evm_real_seams.py \
  sniper/services/memecoin_evm_leg.py tests/test_memecoin_lifi_entry.py
git commit -m "feat(memecoin): add direct-first LI.FI entry"
```

---

### Task 7: Add direct-first LI.FI previews and exits

**Files:**

- Modify: `sniper/execution/evm_exit_executor.py`
- Modify: `sniper/execution/evm_real_seams.py`
- Create: `tests/test_memecoin_lifi_exit.py`

**Interfaces:**

- Add optional `lifi_quote_fn` to `execute_exit_evm()` and `execute_partial_exit_evm()`.
- Add a LI.FI fallback to the chain-4663 `preview_exit_fn`.

- [ ] **Step 1: Write the RED preview tests**

Each preview must try the direct ladder first.

A direct success must cause zero LI.FI calls.

Only exact direct `no_route` can select LI.FI.

Map each LI.FI provider fault to an infrastructure failure.

Do not advance the token unsellable counter for a LI.FI provider fault.

- [ ] **Step 2: Write the RED exit ladder tests**

Advance a LI.FI rung only after a typed slippage or minimum-output failure.

Do not advance after `no_route`, timeout, HTTP 429, or a provider fault.

Keep the signed claim and LI.FI reserve after an unknown send.

Keep the LI.FI slot after a partial exit.

Keep the slot when a later direct route closes only part of the position.

Release the slot only with the atomic terminal close.

- [ ] **Step 3: Write the RED freshness and authority tests**

Each exit quote must use the current release manifest.

Decode and prove each quote before approval or signature.

Check quote age immediately before signature.

A stale quote must start allowance cleanup without another quote request.

- [ ] **Step 4: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_exit.py -q
```

- [ ] **Step 5: Implement the optional fallback**

Keep direct close authority and direct exit behavior unchanged.

Use the same lifecycle executor for full and partial LI.FI exits.

Keep direct exits active when LI.FI is disabled.

- [ ] **Step 6: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_exit.py \
  tests/test_memecoin_wo6_exit_intent.py tests/test_memecoin_sweep_liveness.py \
  tests/test_memecoin_wo4_quote_failures.py -q
git diff --check
git add sniper/execution/evm_exit_executor.py sniper/execution/evm_real_seams.py \
  tests/test_memecoin_lifi_exit.py
git commit -m "feat(memecoin): add direct-first LI.FI exits"
```

---

### Task 8: Add recovery and finalized replacement proof

**Files:**

- Modify: `sniper/execution/lifi_executor.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Create: `tests/test_memecoin_lifi_recovery.py`

**Interfaces:**

- `recover_lifi_lifecycles(conn, context, now_iso) -> RecoverySnapshot`
- `prove_finalized_replacement(lifecycle, custody_rpc, proof_rpc) -> DropProof | None`

- [ ] **Step 1: Write the RED crash matrix**

Inject one crash before and after each durable write and network call.

Cover quote, proof, approval sign, approval send, approval receipt, swap sign, swap send, and swap receipt.

Cover settlement, allowance read, revoke sign, revoke send, revoke receipt, and receipt-block audit.

After each crash, rerun recovery twice.

Prove that recovery never duplicates a sign, send, fill, position, revoke identity, or terminal close.

- [ ] **Step 2: Write the RED replacement-proof tests**

Require all these facts at `finalized`:

1. Custody RPC and proof RPC agree on block number and hash.
2. The exact signed hash has no canonical receipt.
3. The custody nonce is above the signed nonce.
4. A same-nonce canonical transaction has a different hash.
5. The replacement has a canonical receipt.
6. No exact-mint custody transfer or swap effect exists.
7. Balance and allowance reconciliation is complete.

If either RPC lacks `finalized`, keep `submitted_unknown`.

Treat elapsed time, mempool absence, null lookup, timeout, and pending nonce as no proof.

After a proven drop, confirm zero allowance or complete a revoke before reservation release.

Find the same-nonce transaction with full block transactions from the persisted submission block.

Scan a fixed block page per recovery pass. Persist the next scan block as a lifecycle event.

Do not claim a drop until the complete range reaches the agreed finalized block.

- [ ] **Step 3: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_lifi_recovery.py -q
```

- [ ] **Step 4: Implement recovery without delay**

Start direct route contexts before LI.FI recovery.

Start one daemon recovery worker after the first heartbeat write.

Process one bounded lifecycle page in each worker pass.

Do not delay direct entry, direct exit, or runner heartbeat.

An invalid lifecycle must remain unresolved and consume one slot.

Do not admit a LI.FI entry before recovery completes.

- [ ] **Step 5: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_lifi_recovery.py \
  tests/test_memecoin_reservation_recovery.py tests/test_memecoin_runner_loop_cadence.py \
  tests/test_memecoin_sweep_liveness.py -q
git diff --check
git add sniper/execution/lifi_executor.py sniper/services/memecoin_real_runner.py \
  scripts/memecoin_runner_loop.py tests/test_memecoin_lifi_recovery.py
git commit -m "feat(memecoin): recover LI.FI authority"
```

---

### Task 9: Publish runner-owned state and bounded alerts

**Files:**

- Modify: `sniper/services/memecoin_runner.py`
- Modify: `sniper/services/memecoin_alerts.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Modify: `tests/test_memecoin_sweep_liveness.py`
- Modify: `tests/test_memecoin_real_alerts.py`
- Modify: `tests/test_memecoin_real_status.py`
- Create: `tests/test_memecoin_lifi_ui.py`

**Interfaces:**

- Add `lifi_status` to `write_heartbeat()`.
- Add `lifi` to `/api/memecoin/real/status` from the runner heartbeat only.

- [ ] **Step 1: Write the RED state tests**

The state must contain these fields:

```text
corrected_at
runner_heartbeat_at
release_sha
manifest_hash
chain_id
custody_wallet
mode
authenticated
effective_limit
remaining
reset_s
in_flight
exit_reserve
slots_used
slot_limit
entry_admitted
disable_reasons[]
unresolved_approvals
unresolved_swaps
unresolved_revokes
unresolved_finality
```

Use the heartbeat JSON as the one atomic state snapshot.

Pass one sanitized LI.FI object into the current atomic heartbeat writer.

The web process must not infer these fields from its environment.

Mark state stale when its corrected timestamp exceeds the approved bound.

Show every active disable reason.

- [ ] **Step 2: Write the RED alert tests**

Emit one alert for each new disable reason.

Do not repeat an active reason on each tick.

Emit one recovery alert when the reason clears.

Compare against the prior heartbeat reason set after a restart.

Do not expose the API key in alerts, logs, status, or trace values.

- [ ] **Step 3: Write the RED UI tests**

Add a compact LI.FI row to the Robinhood venue card.

Show mode, quota, reserve, slots, manifest prefix, and all disable reasons.

Show `STALE` when the runner snapshot is stale.

Do not show a green LI.FI state from web configuration.

Direct Robinhood readiness must stay independent.

- [ ] **Step 4: Verify RED**

```bash
python3 -m pytest tests/test_memecoin_sweep_liveness.py \
  tests/test_memecoin_real_alerts.py tests/test_memecoin_real_status.py \
  tests/test_memecoin_lifi_ui.py -q
```

- [ ] **Step 5: Implement the status path**

Reuse the current heartbeat file. Do not add another UI poll.

Add one whitelist field to `write_heartbeat()`.

Read the field in `/api/memecoin/real/status`.

Render it in `_venueCard()` for chain `4663`.

- [ ] **Step 6: Verify GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_sweep_liveness.py \
  tests/test_memecoin_real_alerts.py tests/test_memecoin_real_status.py \
  tests/test_memecoin_lifi_ui.py tests/test_memecoin_ui_render_runtime.py -q
git diff --check
git add sniper/services/memecoin_runner.py sniper/services/memecoin_alerts.py \
  scripts/memecoin_runner_loop.py sniper/routes/api_memecoin.py static/app.js \
  tests/test_memecoin_sweep_liveness.py tests/test_memecoin_real_alerts.py \
  tests/test_memecoin_real_status.py tests/test_memecoin_lifi_ui.py
git commit -m "feat(memecoin): show LI.FI funded state"
```

---

### Task 10: Prove the complete default-off system

**Files:**

- Modify only if a focused test finds a defect.

- [ ] **Step 1: Run the LI.FI contract tests**

```bash
python3 -m pytest \
  tests/test_memecoin_lifi_manifest.py \
  tests/test_memecoin_lifi_route.py \
  tests/test_memecoin_lifi_quota.py \
  tests/test_memecoin_lifi_lifecycle.py \
  tests/test_memecoin_lifi_client.py \
  tests/test_memecoin_lifi_authority.py \
  tests/test_memecoin_lifi_approval.py \
  tests/test_memecoin_lifi_settlement.py \
  tests/test_memecoin_lifi_entry.py \
  tests/test_memecoin_lifi_exit.py \
  tests/test_memecoin_lifi_recovery.py \
  tests/test_memecoin_lifi_ui.py -q
```

- [ ] **Step 2: Run the funded path regression set**

```bash
python3 -m pytest \
  tests/test_memecoin_evm_leg.py \
  tests/test_memecoin_evm_quote_miss_attribution.py \
  tests/test_crash_windows.py \
  tests/test_memecoin_fast_submit_crash_safety.py \
  tests/test_memecoin_reservation_recovery.py \
  tests/test_memecoin_wo6_exit_intent.py \
  tests/test_memecoin_sweep_liveness.py \
  tests/test_memecoin_runner_loop_cadence.py \
  tests/test_memecoin_real_alerts.py \
  tests/test_memecoin_real_status.py \
  tests/test_memecoin_ui_render_runtime.py \
  tests/test_memecoin_runtime_verify.py \
  tests/test_memecoin_runtime_deploy.py -q
```

- [ ] **Step 3: Run static validation**

```bash
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts/memecoin_runner_loop.py scripts/memecoin_runtime_verify.py
git diff --check
git status --short
```

- [ ] **Step 4: Verify default-off behavior**

Prove these facts with injected clients:

1. Disabled mode makes zero LI.FI HTTP calls.
2. Public mode makes zero automated funded calls.
3. A direct success makes zero LI.FI calls.
4. A LI.FI fault does not delay direct startup or heartbeat.
5. The API key appears in no captured output.
6. `fly` cannot receive funded authority.

- [ ] **Step 5: Commit any test-only correction**

Use one scoped commit only if Step 1 through Step 4 require a correction.

Do not deploy this commit.

---

## Post-implementation approval gates

Stop after Task 10.

The next operator review must inspect the exact release SHA, manifest hash, focused test output, and diff.

A separate approval must authorize the pinned-runtime deployment.

A later approval must authorize the manual `$5` canary.

The canary must bind to the exact release SHA, manifest hash, custody wallet, chain, token, and spender set.

Canary allowance proof must cover each pinned token and spender pair.

Canary success must not enable automation.

A final separate approval must activate `authenticated_funded` for the exact release and manifest.

## Knowledge sync

Run the Knowledge Sync Gate after implementation review. The likely durable facts are the lifecycle state contract and the quota boundary contract.
