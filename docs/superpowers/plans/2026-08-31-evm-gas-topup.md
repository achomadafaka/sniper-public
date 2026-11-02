# EVM Gas Top-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep chain 4663 native gas between the approved low-water and target values.

**Architecture:** Reuse WETH `withdraw(uint256)`. The runner runs a non-blocking pass every 300 seconds. A SQLite row records the signed hash before broadcast. An unresolved row blocks another unwrap.

**Tech Stack:** Python, SQLite, EVM JSON-RPC, `eth_account`.

**Spec:** Operator approval on 2026-08-31.

## Global Constraints

- Scope the feature to chain `4663`.
- Set low-water to `0.001 ETH`.
- Set target to `0.002 ETH`.
- Set the WETH reserve to `0.010 ETH`.
- Set the daily cap to `0.004 ETH`.
- Store atomic amounts as decimal `TEXT`.
- Use `wallet_exec_lock` without wait.
- Do not alter entry, exit, route, or position logic.
- Do not run a full test suite.

---

### Task 1: Add the durable top-up record

**Files:**

- Modify: `sniper/persistence/db_memecoin.py`
- Test: `tests/test_memecoin_evm_gas_topup.py`

**Interfaces:**

- Produces: `record_evm_gas_topup_submission`, `finish_evm_gas_topup`, `evm_gas_topup_spent_today`, and `has_unresolved_evm_gas_topup`.

- [ ] **Step 1: Write the failing test**

```python
row_id = m.record_evm_gas_topup_submission(
    conn, now_iso=NOW, day="2026-08-31", chain_id="4663",
    native_amount_atomic="1500000000000000", native_before_atomic="500000000000000",
    tx_hash="0xabc", nonce=12,
)
assert row_id
assert m.evm_gas_topup_spent_today(conn, "4663", "2026-08-31") == 1500000000000000
assert m.has_unresolved_evm_gas_topup(conn, "4663") is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: FAIL because the durable functions do not exist.

- [ ] **Step 3: Add the schema and functions**

```python
conn.execute(
    "INSERT INTO memecoin_evm_gas_topups "
    "(created_at, day, chain_id, native_amount_atomic, native_before_atomic, tx_hash, nonce, status) "
    "VALUES (?,?,?,?,?,?,?, 'submitted')",
    ("2026-08-31T12:00:00+00:00", "2026-08-31", "4663",
     "1500000000000000", "500000000000000", "0xabc", 12),
)
```

Keep the atomic amount when the status changes. The cap must count submitted and unknown rows.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: PASS.

### Task 2: Journal the unwrap before send

**Files:**

- Modify: `sniper/services/memecoin_evm_gas_topup.py`
- Test: `tests/test_memecoin_evm_gas_topup.py`

**Interfaces:**

- Consumes: a signed `(raw, hash)` pair, `record_submission_fn`, and `finish_fn`.
- Produces: one terminal state or `submitted_unknown`.

- [ ] **Step 1: Write the failing test**

```python
order = []
out = G.evm_gas_topup_once(
    **seams(record_submission_fn=lambda p: order.append(("record", p["tx_hash"])),
            send_fn=lambda raw: order.append(("send", raw)) or "0xhash")
)
assert out["status"] == "confirmed"
assert order[0] == ("record", "0xhash")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: FAIL because the helper records after send.

- [ ] **Step 3: Add the minimal helper change**

```python
raw, tx_hash = sign_fn(tx)
record_submission_fn({**plan, "tx_hash": tx_hash, "nonce": nonce})
accepted = send_fn(raw)
```

The caller owns `wallet_exec_lock`. A missing or mismatched send hash remains `submitted_unknown`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: PASS.

### Task 3: Call the helper from the runner

**Files:**

- Modify: `sniper/settings/memecoin.py`
- Modify: `scripts/memecoin_runner_loop.py`
- Test: `tests/test_memecoin_evm_gas_topup.py`

**Interfaces:**

- Produces: one chain-4663 top-up pass every 300 seconds.

- [ ] **Step 1: Write the failing test**

```python
result = run_evm_gas_topup_pass(
    context=low_gas_4663_context(), now_iso=NOW, topup_fn=recording_topup,
)
assert result["state"] == "confirmed"
assert recording_topup.native_amount_atomic == "1500000000000000"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: FAIL because the runner has no EVM top-up pass.

- [ ] **Step 3: Add the runner pass and settings**

```python
if EVM_GAS_TOPUP_ENABLED and cycle - last_evm_gas_topup >= EVM_GAS_TOPUP_PERIOD_S:
    last_evm_gas_topup = cycle
    run_evm_gas_topup_pass(conn, contexts=contexts, now_iso=real_utc_now_iso())
```

Read WETH and native balances under the non-blocking shared lock. Skip an unresolved top-up. Write before `eth_sendRawTransaction`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py`

Expected: PASS.

### Task 4: Verify and activate

**Files:**

- Modify: `.env` only through the funded deployment command.

- [ ] **Step 1: Run focused verification**

Run: `python3 -m pytest -q tests/test_memecoin_evm_gas_topup.py tests/test_memecoin_evm_leg.py tests/test_memecoin_evm_revert_retry.py`

Expected: PASS.

- [ ] **Step 2: Run schema and diff checks**

Run: `python3 scripts/validate_schema_refs.py && git diff --check main...HEAD`

Expected: PASS.

- [ ] **Step 3: Deploy the merged SHA**

Run: `scripts/deploy_memecoin_runtime.sh "$(git rev-parse HEAD)"`.

Set `MEMECOIN_EVM_GAS_TOPUP_ENABLED=1`. Keep low-water `0.001`, target `0.002`, reserve `0.010`, daily cap `0.004`, and period `300`.

- [ ] **Step 4: Verify the runtime state**

Check the active release, runner heartbeat, effective settings, and native balance. A balance at or above low-water must not unwrap.
