# Chain 4663 V2 Exit Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing real VIM position executable through its verified `VIM -> VIRTUAL -> WETH` Uniswap V2 route without enabling unproven V2 entries.

**Architecture:** Add a chain-configured, exit-only Uniswap V2 quote/transaction builder that scans direct and one-connector paths and emits `swapExactTokensForTokensSupportingFeeOnTransferTokens`. The EVM executor keeps V3 as the only entry/safety venue; only exit preview and exit execution fall back to V2 when the configured V3 quote is absent.

**Tech Stack:** Python, JSON-RPC `eth_call`, canonical Uniswap V2 Router02 ABI, pytest, SQLite read-only runtime evidence.

## Global Constraints

- Robinhood is network `4663`; coin identity remains `(network_id, mint)`.
- Do not submit a funded sale during implementation or verification.
- Do not change entry routing, token-safety admission, sizing, stops, reservations, accounting, kill-switch behavior, or duplicate-submit protection.
- V2 routes are discovered from immutable chain connectors, never hardcoded per coin.
- Exact token balance is approved and sold; approval remains exact-amount.
- Quote failure, RPC failure, missing router, missing path, zero output, or invalid slippage returns `None`.
- Fee-on-transfer protection uses the existing `MEMECOIN_EVM_MAX_TAX_PCT` as an input haircut and the existing exit slippage ladder for `amountOutMin`.

---

### Task 1: Exit-only Uniswap V2 quote builder

**Files:**
- Create: `sniper/execution/evm_v2_exit_quote.py`
- Modify: `sniper/settings/memecoin.py`
- Test: `tests/test_memecoin_evm_v2_exit_quote.py`

**Interfaces:**
- Consumes: `chain`, `sell_token`, `buy_token`, exact atomic balance, taker, slippage bps, verified router, connector list.
- Produces: `dex_v2_exit_quote(...) -> dict | None`, shaped like the existing 0x/V3 quote dictionaries.

- [ ] **Step 1: Write failing unit tests**

Cover path selection, tax haircut, nonzero `amountOutMin`, fee-on-transfer selector `0x5c11d795`, exact full `sellAmount`, exact router spender, and fail-closed RPC/no-route cases.

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest -q tests/test_memecoin_evm_v2_exit_quote.py`

Expected: collection/import failure because `sniper.execution.evm_v2_exit_quote` does not exist.

- [ ] **Step 3: Implement the minimal builder**

Add verified chain-4663 settings:

```python
MEMECOIN_EVM_V2_ROUTER = {
    "4663": os.getenv("MEMECOIN_EVM_V2_ROUTER_4663", "0x89e5DB8B5aA49aA85AC63f691524311AEB649eba"),
}
MEMECOIN_EVM_V2_CONNECTORS = {
    "4663": ("0xc6911796042b15d7fa4f6cde69e245ddcd3d9c31",  # VIRTUAL
             "0x5fc5360d0400a0fd4f2af552add042d716f1d168"), # USDG
}
```

Implement:

```python
def dex_v2_exit_quote(chain, sell_token, buy_token, sell_amount_atomic, taker, *,
                      http=None, slippage_bps=None, cfg_router=None,
                      connector_tokens=None, tax_buffer_bps=None,
                      call_fn=None, now_fn=None):
    """Return a conservative executable V2 fee-on-transfer exit quote or None."""
```

Scan `[sell, buy]` plus `[sell, connector, buy]`. Call Router02 `getAmountsOut` using the amount after the existing maximum-tax haircut, choose the largest output, then build `swapExactTokensForTokensSupportingFeeOnTransferTokens` using the full exact balance and the existing slippage setting.

- [ ] **Step 4: Verify GREEN**

Run: `python3 -m pytest -q tests/test_memecoin_evm_v2_exit_quote.py`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/evm_v2_exit_quote.py sniper/settings/memecoin.py tests/test_memecoin_evm_v2_exit_quote.py
git commit -m "feat(memecoin): add chain-4663 V2 exit quotes"
```

### Task 2: Bind V2 only as an exit fallback

**Files:**
- Modify: `sniper/execution/evm_real_seams.py`
- Test: `tests/test_memecoin_evm_seams.py`

**Interfaces:**
- Consumes: the existing exact raw-balance exit path and `dex_v2_exit_quote`.
- Produces: `_exit_venue_quote(...)` used by both `preview_exit_fn` and `_exit_quote`; `_venue_quote` remains unchanged for entry and token safety.

- [ ] **Step 1: Write failing integration tests**

Add one test proving a missing V3 exit quote falls back to V2, one proving a working V3 quote never calls V2, and one proving `venue_quote_fn` used by entry safety remains V3-only.

- [ ] **Step 2: Verify RED**

Run: `python3 -m pytest -q tests/test_memecoin_evm_seams.py -k 'v2_exit or direct_venue'`

Expected: fallback test fails because preview returns `None` and the V2 builder is never called.

- [ ] **Step 3: Implement the minimal binding**

Create one helper inside `build_real_evm_executor`:

```python
def _exit_venue_quote(mint, atomic, *, slippage_bps):
    quote = _venue_quote(mint, quote_token, atomic, slippage_bps=slippage_bps)
    if quote is not None or chain != "4663" or cfg.memecoin_evm_exec_venue(chain) != "direct":
        return quote
    return dex_v2_exit_quote(chain, mint, quote_token, atomic, address,
                             http=http, slippage_bps=slippage_bps)
```

Use it only from `_exit_quote` and `preview_exit_fn`. Do not expose it as `venue_quote_fn`.

- [ ] **Step 4: Verify GREEN and focused regression**

Run:

```bash
python3 -m pytest -q tests/test_memecoin_evm_v2_exit_quote.py tests/test_memecoin_evm_dex_quote.py tests/test_memecoin_evm_seams.py tests/test_memecoin_evm_leg.py tests/test_memecoin_evm_token_safety.py
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/execution/evm_real_seams.py tests/test_memecoin_evm_seams.py
git commit -m "fix(memecoin): recover V2-only 4663 exits"
```

### Task 3: Evidence, full verification, merge and deploy

**Files:**
- Modify: `docs/research/2026-07-16-robinhood-4663-dex-venue.md`

**Interfaces:**
- Consumes: verified on-chain V2 factory/router/pairs and read-only full-balance simulation.
- Produces: durable route evidence and a deployed pinned runtime whose preview returns a positive VIM exit quote.

- [ ] **Step 1: Record evidence**

Document router `0x89e5...`, factory `0x8bce...`, VIM/VIRTUAL pair `0x3b2c...`, VIRTUAL/WETH pair `0xd95e...`, the original 0x entry route, and the successful full-balance `eth_call` with nonzero minimum.

- [ ] **Step 2: Run verification**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest -q tests/test_memecoin*.py
python3 -m compileall -q sniper scripts
git diff --check
python3 scripts/validate_knowledge_sync.py --mode local
```

- [ ] **Step 3: Commit evidence**

```bash
git add docs/research/2026-07-16-robinhood-4663-dex-venue.md docs/superpowers/plans/2026-07-16-memecoin-4663-v2-exit-recovery.md
git commit -m "docs(memecoin): record VIM V2 exit route"
```

- [ ] **Step 4: Review, push, merge, deploy**

Adversarially review the complete diff, complete the 11-question Knowledge Sync block, push a feature PR, wait for required checks, merge, and deploy the merged full SHA with `scripts/deploy_memecoin_runtime.sh <sha> --restart`.

- [ ] **Step 5: Prove the deployed exit without selling**

From the pinned runtime, call `preview_exit_fn` for real position `3427`. Acceptance: positive exact-balance quote, route target is the verified V2 router, runtime health is `ok`, zero duplicate runners, and no transaction was submitted.
