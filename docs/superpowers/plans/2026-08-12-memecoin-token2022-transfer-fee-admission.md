# Token-2022 Transfer-Fee Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit immutable Token-2022 fees through `300` bps and verify the planned sell quantity before a funded signature.

**Architecture:** The mint parser checks both fee schedules and all applicable authorities. The entry executor uses the normal entry quote floor for one ExactIn reverse quote.

**Tech Stack:** Python 3, SQLite, pytest, Solana JSON-RPC, and Jupiter Swap API.

## Global Constraints

- Use `MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS=300` as the default.
- Permit a positive fee only when the fee-change authority is null.
- Reject a live permanent delegate.
- Use the entry quote `otherAmountThreshold` for the reverse quote.
- Complete the reverse quote before balance reads and the signature step.
- Keep all other funded controls unchanged.
- Make no live API call during tests.
- Make no schema or dependency change.
- Keep all text in ASD-STE100 Simplified Technical English.

---

### Task 1: Add the immutable fee policy

**Files:**

- Modify: `sniper/settings/memecoin.py:620-680`
- Modify: `sniper/settings/memecoin.py:1258-1320`
- Modify: `sniper/execution/solana_token_safety.py:27-97`
- Modify: `sniper/services/memecoin_real_leg.py:150-180`
- Modify: `sniper/services/memecoin_real_runner.py:725-740`
- Test: `tests/test_memecoin_token_safety.py`
- Test: `tests/test_memecoin_real_policy_validation.py`
- Test: `tests/test_memecoin_entry_identity.py`

**Interfaces:**

- Consumes: A JSON-parsed Token-2022 mint account.
- Produces: `token_safety_ok(mint, rpc_url, *, max_transfer_fee_bps, http=None) -> tuple[bool, str]`.
- Produces: `MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS: int`.
- Produces: A `token_safety` entry-trace substage with the gate result and reason.
- Preserves: The old fixed route probe until Task 2 replaces it.

- [ ] **Step 1: Write the fee-policy tests**

Replace the fee fixture with this helper in `tests/test_memecoin_token_safety.py`:

```python
import pytest


def _token_2022(*, older=300, newer=300, authority=None, delegate=None):
    extensions = [{
        "extension": "transferFeeConfig",
        "state": {
            "olderTransferFee": {"transferFeeBasisPoints": older},
            "newerTransferFee": {"transferFeeBasisPoints": newer},
            "transferFeeConfigAuthority": authority,
        },
    }]
    if delegate is not None:
        extensions.append({
            "extension": "permanentDelegate",
            "state": {"delegate": delegate},
        })
    return {"result": {"value": {
        "owner": ts.TOKEN_2022_PROGRAM,
        "data": {"parsed": {"type": "mint", "info": {
            "freezeAuthority": None,
            "mintAuthority": None,
            "decimals": 9,
            "extensions": extensions,
        }}},
    }}}
```

Add these tests:

```python
def test_accepts_immutable_transfer_fee_at_limit():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022()),
    )
    assert ok is True
    assert "transfer_fee_bps=300" in reason
    assert "fee_limit_bps=300" in reason
    assert "fee_authority_immutable=true" in reason


def test_rejects_transfer_fee_over_limit():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(newer=301)),
    )
    assert ok is False
    assert "301 bps over 300 bps limit" in reason


def test_rejects_live_fee_change_authority():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(authority="FeeAuth")),
    )
    assert ok is False
    assert reason == "live token-2022 transfer-fee authority"


def test_rejects_permanent_delegate():
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(delegate="Delegate")),
    )
    assert ok is False
    assert reason == "live token-2022 permanent delegate"


@pytest.mark.parametrize(("older", "newer"), [(None, 300), (300, None), ("bad", 300)])
def test_rejects_malformed_transfer_fee_schedule(older, newer):
    ok, reason = ts.token_safety_ok(
        "M", "rpc", max_transfer_fee_bps=300,
        http=_http_returning(_token_2022(older=older, newer=newer)),
    )
    assert ok is False
    assert reason == "token-2022 transfer-fee schedule unreadable (fail-closed)"
```

Remove the old sell-route tests from this file. Task 2 moves that check to the executor.
Pass `max_transfer_fee_bps=300` to every remaining `token_safety_ok()` call.

- [ ] **Step 2: Write the setting and trace tests**

Add this parameter to `test_funded_classifier_settings_fail_closed`:

```python
("MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS", 10001),
```

Add this assertion to the deployed-default test:

```python
assert cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS == 300
```

Set `_solana_context()` to return this gate result:

```python
"token_safety_fn": lambda mint, **kw: (
    True,
    "ok transfer_fee_bps=300 fee_limit_bps=300 fee_authority_immutable=true",
),
```

Import `json`. Add this check to `test_manual_funded_buy_creates_exactly_one_trace()`:

```python
trace = json.loads(rows[0]["trace_json"])
substage = next(
    item for item in trace["substage_events"]
    if item["name"] == "token_safety"
)
assert substage["allowed"] is True
assert "fee_limit_bps=300" in substage["reason"]
```

- [ ] **Step 3: Run the tests and verify the RED state**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_token_safety.py \
  tests/test_memecoin_real_policy_validation.py \
  tests/test_memecoin_entry_identity.py -q
```

Expected: The new fee, signature, and trace contracts fail.

- [ ] **Step 4: Add the setting and parser**

Add this setting near the Solana entry transaction settings:

```python
MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS = int(os.getenv(
    "MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS", "300"))
```

Add this armed startup check to `validate_real_exit_policy()`:

```python
if (isinstance(MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS, bool)
        or not isinstance(MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS, int)
        or not 0 <= MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS <= 10_000):
    errors.append(
        "MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS must be an integer from 0 through 10000")
```

Replace the one-schedule parser with this private result shape:

```python
def _transfer_fee_config(info, owner):
    if owner != TOKEN_2022_PROGRAM:
        return None
    for ext in info.get("extensions") or []:
        if ext.get("extension") != "transferFeeConfig":
            continue
        state = ext.get("state") or {}
        try:
            older = int(state["olderTransferFee"]["transferFeeBasisPoints"])
            newer = int(state["newerTransferFee"]["transferFeeBasisPoints"])
        except (KeyError, TypeError, ValueError):
            return {"valid": False, "bps": None, "authority": None}
        valid = (
            0 <= older <= 10_000
            and 0 <= newer <= 10_000
            and "transferFeeConfigAuthority" in state
        )
        return {
            "valid": valid,
            "bps": max(older, newer) if valid else None,
            "authority": state.get("transferFeeConfigAuthority"),
        }
    return None
```

Keep `_transfer_fee_bps()` as the shared observation adapter. Return `10_001` for an invalid schedule.

- [ ] **Step 5: Add the fee decision and trace evidence**

Use this decision order in `token_safety_ok()`:

```python
fee = _transfer_fee_config(info, owner)
if _permanent_delegate(info, owner) is not None:
    return False, "live token-2022 permanent delegate"
if fee is not None and not fee["valid"]:
    return False, "token-2022 transfer-fee schedule unreadable (fail-closed)"
if fee is not None and fee["authority"] is not None:
    return False, "live token-2022 transfer-fee authority"
if fee is not None and fee["bps"] > max_transfer_fee_bps:
    return False, f"token-2022 transfer fee {fee['bps']} bps over {max_transfer_fee_bps} bps limit"
if fee is not None and fee["bps"] > 0:
    return True, (
        f"ok transfer_fee_bps={fee['bps']} fee_limit_bps={max_transfer_fee_bps} "
        "fee_authority_immutable=true"
    )
return True, "ok"
```

Record the result in `real_entry()` before the top-level safety stage mark:

```python
marker = getattr(trace, "mark_substage", None) if trace is not None else None
if marker is not None:
    marker("token_safety", allowed=bool(safe), reason=str(reason))
```

- [ ] **Step 6: Run the tests and verify the GREEN state**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_token_safety.py \
  tests/test_memecoin_wo14_token_authority.py \
  tests/test_memecoin_real_policy_validation.py \
  tests/test_memecoin_entry_identity.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the fee policy**

```bash
git add \
  sniper/settings/memecoin.py \
  sniper/execution/solana_token_safety.py \
  sniper/services/memecoin_real_leg.py \
  tests/test_memecoin_token_safety.py \
  tests/test_memecoin_real_policy_validation.py \
  tests/test_memecoin_entry_identity.py
git commit -m "fix(memecoin): admit immutable Token-2022 fees"
```

---

### Task 2: Verify the planned sell quantity

**Files:**

- Modify: `sniper/execution/solana_swap_executor.py:70-170`
- Modify: `sniper/execution/solana_real_seams.py:350-470`
- Modify: `sniper/services/memecoin_real_runner.py:140-145`
- Modify: `sniper/services/memecoin_real_runner.py:670-740`
- Test: `tests/test_memecoin_entry_executor.py`
- Test: `tests/test_memecoin_real_seams.py`
- Test: `tests/test_memecoin_entry_impact_cap.py`
- Test: `tests/test_memecoin_fast_submit_crash_safety.py`
- Test: `tests/test_memecoin_real_deploy.py`
- Test: `tests/test_memecoin_entry_drift.py`

**Interfaces:**

- Consumes: The approved clip and its normal ExactIn entry quote.
- Produces: `reverse_quote_fn(mint: str, amount_atomic: int) -> dict | None`.
- Produces: A `planned_exit_route` trace substage.
- Produces: A `route_failed` result before any balance read or signature.

- [ ] **Step 1: Write the executor regression tests**

Update `_Deps.quote_fn()` in `tests/test_memecoin_entry_executor.py`:

```python
def quote_fn(self, mint, usd):
    self.calls.append("quote")
    return {
        "inAmount": "10000000",
        "outAmount": "500",
        "otherAmountThreshold": "470",
        "priceImpactPct": self._impact,
    }

def reverse_quote_fn(self, mint, amount_atomic):
    self.calls.append(("reverse_quote", mint, amount_atomic))
    return {
        "inAmount": str(amount_atomic),
        "outAmount": "9000000",
        "otherAmountThreshold": "8100000",
    }
```

Pass `reverse_quote_fn=d.reverse_quote_fn` from `_run()`.

Add these tests:

```python
def test_reverse_quote_uses_entry_floor_before_balance_or_signature():
    c, pid = _c()
    d = _Deps()
    result = _run(c, pid, d)
    assert result["status"] == "filled"
    assert ("reverse_quote", "MINT", 470) in d.calls
    assert d.calls.index(("reverse_quote", "MINT", 470)) < d.calls.index("sign")


def test_absent_reverse_route_releases_before_signature():
    c, pid = _c()
    d = _Deps()
    d.reverse_quote_fn = lambda mint, amount: None
    result = _run(c, pid, d)
    assert result == {
        "status": "route_failed",
        "reason": "no planned-quantity sell route",
        "attempt_id": None,
        "origin_event_id": None,
        "network_id": "solana",
        "signal_source": None,
    }
    assert "sign" not in d.calls
    assert m.pending_totals(c) == (0, 0.0)
```

Add parameter cases for absent, zero, non-integer, and mismatched reverse amounts.

- [ ] **Step 2: Run the executor tests and verify the RED state**

Run:

```bash
python3 -m pytest tests/test_memecoin_entry_executor.py -q
```

Expected: `execute_entry()` rejects the new `reverse_quote_fn` argument.

- [ ] **Step 3: Add the planned-quantity route check**

Add the required `reverse_quote_fn` argument to `execute_entry()`.

Place this code after the impact check and before all balance reads:

```python
try:
    planned_atomic = int(quote["otherAmountThreshold"])
except (KeyError, TypeError, ValueError):
    planned_atomic = 0
reverse_quote = reverse_quote_fn(mint, planned_atomic) if planned_atomic > 0 else None
try:
    reverse_valid = (
        isinstance(reverse_quote, dict)
        and int(reverse_quote["inAmount"]) == planned_atomic
        and int(reverse_quote["outAmount"]) > 0
        and int(reverse_quote["otherAmountThreshold"]) > 0
    )
except (KeyError, TypeError, ValueError):
    reverse_valid = False
marker = getattr(trace, "mark_substage", None) if trace is not None else None
if marker is not None:
    marker(
        "planned_exit_route",
        amount_atomic=planned_atomic,
        available=reverse_valid,
    )
if not reverse_valid:
    m.release_pending(conn, pending_id, "sell_route_unavailable")
    return {
        "status": "route_failed",
        "reason": "no planned-quantity sell route",
        **provenance,
    }
```

Add `route_failed` to the executor result contract in its module and function docstrings.

- [ ] **Step 4: Bind the runtime reverse quote**

Add this seam inside `build_real_solana_executor()`:

```python
def _entry_reverse_quote(mint, amount_atomic):
    from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
    return jupiter_exit_quote(
        mint,
        amount_atomic,
        MEMECOIN_EXIT_SLIPPAGE_LADDER[-1],
        http=http,
    )
```

Pass it to `execute_entry()`:

```python
quote_fn=_entry_quote,
reverse_quote_fn=_entry_reverse_quote,
sign_fn=_sign,
```

Remove `_SELL_PROBE_ATOMIC` and `_sell_route()` from `memecoin_real_runner.py`.

Bind the authority gate with the approved limit:

```python
def token_safety_fn(mint):
    return token_safety_ok(
        mint,
        rpc_url,
        max_transfer_fee_bps=cfg.MEMECOIN_SOLANA_MAX_IMMUTABLE_TRANSFER_FEE_BPS,
        http=_rpc_http,
    )
```

- [ ] **Step 5: Update every injected executor call**

Add a valid `otherAmountThreshold` to each entry quote fixture.

Pass this seam to each direct `execute_entry()` call:

```python
reverse_quote_fn=lambda mint, amount: {
    "inAmount": str(amount),
    "outAmount": "1",
    "otherAmountThreshold": "1",
},
```

Update the factory HTTP script in `tests/test_memecoin_real_seams.py`. The sequence must be entry quote, reverse quote, swap, send, and confirmation.

- [ ] **Step 6: Run the focused tests and verify the GREEN state**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_entry_executor.py \
  tests/test_memecoin_real_seams.py \
  tests/test_memecoin_entry_impact_cap.py \
  tests/test_memecoin_fast_submit_crash_safety.py \
  tests/test_memecoin_real_deploy.py \
  tests/test_memecoin_entry_drift.py \
  tests/test_memecoin_real_runner.py -q
```

Expected: PASS.

- [ ] **Step 7: Run the full scoped verification**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_token_safety.py \
  tests/test_memecoin_wo14_token_authority.py \
  tests/test_memecoin_real_policy_validation.py \
  tests/test_memecoin_entry_identity.py \
  tests/test_memecoin_real_leg.py \
  tests/test_memecoin_entry_executor.py \
  tests/test_memecoin_real_seams.py \
  tests/test_memecoin_entry_impact_cap.py \
  tests/test_memecoin_fast_submit_crash_safety.py \
  tests/test_memecoin_real_deploy.py \
  tests/test_memecoin_entry_drift.py \
  tests/test_memecoin_real_runner.py -q
python3 scripts/validate_schema_refs.py
```

Expected: All tests pass. The schema check reports no violations.

- [ ] **Step 8: Commit the planned-quantity route check**

```bash
git add \
  sniper/execution/solana_swap_executor.py \
  sniper/execution/solana_real_seams.py \
  sniper/services/memecoin_real_runner.py \
  tests/test_memecoin_entry_executor.py \
  tests/test_memecoin_real_seams.py \
  tests/test_memecoin_entry_impact_cap.py \
  tests/test_memecoin_fast_submit_crash_safety.py \
  tests/test_memecoin_real_deploy.py \
  tests/test_memecoin_entry_drift.py
git commit -m "fix(memecoin): verify the planned Solana exit quantity"
```

## Completion Check

- The branch contains no change outside the listed files.
- The original worktree stays unchanged.
- The runtime stays on its current release.
- The next valid funded signal performs the two-way quote check.
- No historical signal causes a quote, signature, submit action, or DB change.
