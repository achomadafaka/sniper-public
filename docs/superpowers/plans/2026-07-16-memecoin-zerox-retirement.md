# Memecoin 0x Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove every live 0x dependency and false 0x readiness claim while preserving direct Robinhood execution and historical 0x provenance.

**Architecture:** EVM execution becomes direct-only and context construction fails closed unless a chain explicitly selects `direct` and has verified router/quoter configuration. GeckoTerminal becomes the sole blind-chain price/capture provider. Base, BSC, and Ethereum remain observable but do not build funded contexts until separate direct-routing work lands.

**Tech Stack:** Python 3.13, Flask, SQLite, pytest, EVM JSON-RPC, GeckoTerminal.

## Global Constraints

- Robinhood is network `4663`, never generic EVM.
- Coin identity remains `(network_id, mint)`.
- Preserve token safety, atomic reservation, duplicate-submit protection, kill switch, and accounting semantics.
- Make no new network request and retain no provider fallback.
- Historical `source='zerox'` and `dex_id='zerox'` rows remain readable and unchanged.
- Missing or invalid execution venue fails closed before reservation.
- Do not mutate paper strategies, exit policy, sizing, or adaptive policy.
- Use failing-first tests for every production behavior change.

---

### Task 1: Direct-or-disabled venue contract

**Files:**
- Modify: `sniper/settings/memecoin.py:497-565`
- Modify: `sniper/services/memecoin_evm_leg.py:27-78`
- Modify: `tests/test_memecoin_evm_leg.py`
- Create: `tests/test_memecoin_zerox_retirement.py`

**Interfaces:**
- Produces: `memecoin_evm_exec_venue(chain_id: str) -> Literal['direct', 'disabled']`.
- Produces: `build_real_evm_context(chain_id, *, http=None) -> dict | None`, with `None` returned before wallet/RPC construction for unsupported venues.
- Preserves temporarily: the provider setting and executor arguments until Task 2 removes the provider atomically with its call sites.

- [ ] **Step 1: Write failing venue and context tests**

```python
def test_evm_venue_defaults_and_invalid_values_fail_closed(monkeypatch):
    for chain in ("1", "56", "8453", "4663"):
        monkeypatch.delenv(f"MEMECOIN_EVM_EXEC_VENUE_{chain}", raising=False)
        assert cfg.memecoin_evm_exec_venue(chain) == "disabled"
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "zerox")
    assert cfg.memecoin_evm_exec_venue("8453") == "disabled"
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_4663", "direct")
    assert cfg.memecoin_evm_exec_venue("4663") == "direct"


def test_build_evm_context_refuses_disabled_chain_before_wallet_load(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setenv("MEMECOIN_EVM_EXEC_VENUE_8453", "disabled")
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KEYPAIR", "/does/not/exist")
    assert leg.build_real_evm_context("8453") is None
```

- [ ] **Step 2: Run the tests and verify the expected failures**

Run:

```bash
python3 -m pytest tests/test_memecoin_zerox_retirement.py tests/test_memecoin_evm_leg.py -q
```

Expected: the default/invalid venue assertions fail because the current default is `zerox`; the old Base context test also conflicts with the new direct-only contract.

- [ ] **Step 3: Implement direct-or-disabled configuration**

Replace the venue function with:

```python
def memecoin_evm_exec_venue(chain_id):
    value = os.getenv(f"MEMECOIN_EVM_EXEC_VENUE_{chain_id}", "disabled").strip().lower()
    return value if value == "direct" else "disabled"
```

At the top of `build_real_evm_context`, after the real-enabled gate, add:

```python
chain = str(chain_id)
if cfg.memecoin_evm_exec_venue(chain) != "direct":
    return None
if cfg.memecoin_evm_v3_addresses(chain) is None:
    return None
```

Leave the provider arguments unchanged in this task so the direct 4663 context remains independently testable. Task 2 removes the setting, arguments, and provider implementation in one green change.

- [ ] **Step 4: Update the old context test to require direct 4663 configuration**

```python
def test_build_evm_context_builds_direct_4663_when_armed_with_creds(monkeypatch, tmp_path):
    # Generate the keypair, set the 4663 RPC and venue=direct, then assert the context seams.
```

Delete the assertion that Base builds with credentials alone. Add an assertion that Base returns `None` when its venue is disabled.

- [ ] **Step 5: Run focused tests and commit**

```bash
python3 -m pytest tests/test_memecoin_zerox_retirement.py tests/test_memecoin_evm_leg.py -q
git add sniper/settings/memecoin.py sniper/services/memecoin_evm_leg.py \
  tests/test_memecoin_evm_leg.py tests/test_memecoin_zerox_retirement.py
git commit -m "fix(memecoin): fail closed without direct EVM venue"
```

Expected: focused tests pass.

---

### Task 2: Remove 0x execution and token-safety bindings

**Files:**
- Modify: `sniper/execution/evm_real_seams.py`
- Modify: `sniper/execution/evm_token_safety.py`
- Modify: `sniper/services/memecoin_evm_leg.py`
- Modify: `sniper/settings/memecoin.py`
- Modify: `tests/test_memecoin_evm_seams.py`
- Modify: `tests/test_memecoin_evm_token_safety.py`
- Modify: `tests/test_memecoin_evm_v2_entry.py`
- Modify: `tests/test_memecoin_evm_quote_token.py`

**Interfaces:**
- Changes: `build_real_evm_executor(..., venue_quote_fn=None)` replaces the `api_key` parameter.
- Changes: `build_evm_token_safety(*, chain_id, rpc_url, taker, quote_fn, ...)` requires an executable quote seam.
- Removes: `ZEROX_BASE`, `zerox_quote`, and `MEMECOIN_ZEROX_API_KEY`.

- [ ] **Step 1: Write failing direct-only tests**

```python
def test_executor_uses_injected_direct_quote_without_provider_lookup(monkeypatch):
    seen = []
    def quote(sell, buy, amount, *, slippage_bps=None):
        seen.append((sell, buy, amount))
        return {"buyAmount": "100", "transaction": {"to": "0x" + "44" * 20}}
    ex = S.build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc",
        venue_quote_fn=quote, usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0,
    )
    assert ex["venue_quote_fn"]("0xa", "0xb", 1)["buyAmount"] == "100"
    assert seen == [("0xa", "0xb", 1)]


def test_token_safety_requires_explicit_quote_fn():
    with pytest.raises(TypeError):
        build_evm_token_safety(chain_id="4663", rpc_url="https://rpc", taker=TAKER)
```

- [ ] **Step 2: Run focused tests and verify failures**

```bash
python3 -m pytest tests/test_memecoin_evm_seams.py tests/test_memecoin_evm_token_safety.py \
  tests/test_memecoin_evm_v2_entry.py tests/test_memecoin_evm_quote_token.py -q
```

Expected: the new `venue_quote_fn` signature and required token-safety quote do not exist yet.

- [ ] **Step 3: Delete the provider client and bind direct quotes**

In `evm_real_seams.py`:

- delete `ZEROX_BASE` and `zerox_quote`;
- remove `api_key` from `build_real_evm_executor`;
- add `venue_quote_fn=None`;
- bind `venue_quote_fn` to `dex_v3_quote` when not injected;
- keep `venue_quote_with_v2_fallback`, but remove its `venue` parameter and `zerox` branch;
- preserve exact approve, sign, send, confirmation, balance-delta, exit-ladder, and quote-attribution behavior.

The direct binding is:

```python
if venue_quote_fn is None:
    from sniper.execution.evm_dex_quote import dex_v3_quote
    def venue_quote_fn(sell_token, buy_token, amount_atomic, *, slippage_bps=None):
        return dex_v3_quote(
            chain, sell_token, buy_token, amount_atomic, address,
            http=http, slippage_bps=slippage_bps,
        )
```

In `evm_token_safety.py`, make `quote_fn` required and remove the API-key/provider fallback. Default the route label to `direct_configured`.

In `memecoin_evm_leg.py`, remove both API-key arguments and continue injecting `ex['venue_quote_fn']` into token safety. In `settings/memecoin.py`, delete `MEMECOIN_ZEROX_API_KEY` and provider-specific comments.

- [ ] **Step 4: Rewrite provider-coupled tests around the injected direct seam**

Replace GET/0x mocks with `venue_quote_fn` stubs. Keep all assertions covering:

- exact approvals;
- nonce write-ahead;
- sign/send/confirm;
- V3 then V2 behavior on 4663;
- sell-route rejection;
- funded sell simulation and tax checks;
- quote/fill attribution.

Delete tests whose only contract is constructing or parsing a 0x HTTP request.

- [ ] **Step 5: Run focused tests and commit**

```bash
python3 -m pytest tests/test_memecoin_evm_seams.py tests/test_memecoin_evm_token_safety.py \
  tests/test_memecoin_evm_v2_entry.py tests/test_memecoin_evm_quote_token.py \
  tests/test_memecoin_evm_leg.py -q
git add sniper/execution/evm_real_seams.py sniper/execution/evm_token_safety.py \
  sniper/services/memecoin_evm_leg.py sniper/settings/memecoin.py tests/test_memecoin_evm_seams.py \
  tests/test_memecoin_evm_token_safety.py tests/test_memecoin_evm_v2_entry.py \
  tests/test_memecoin_evm_quote_token.py
git commit -m "refactor(memecoin): retire 0x execution seams"
```

Expected: all focused execution and safety tests pass.

---

### Task 3: Make blind-chain pricing GeckoTerminal-only

**Files:**
- Modify: `sniper/services/memecoin_evm_price.py`
- Modify: `sniper/services/memecoin_capture_v2.py`
- Modify: `tests/test_memecoin_evm_price.py`
- Modify: `tests/test_memecoin_capture_blind.py`
- Modify: `tests/test_memecoin_geckoterminal_quote.py`

**Interfaces:**
- Changes: `build_evm_price_for(*, blind_chains=None, _gt_fn=None, ...)` has no `_quote_fn` fallback.
- Changes: `_default_blind_price_batch_fn(blind, *, gt_batch_fn=None)` has no second provider seam.
- Preserves: injected price-map source strings remain accepted as historical/generic provenance.

- [ ] **Step 1: Write failing no-fallback tests**

```python
def test_build_price_for_returns_none_after_gt_miss_without_http_fallback(monkeypatch):
    calls = []
    price_for = build_evm_price_for(
        blind_chains={"4663"}, _gt_fn=lambda *_a: None,
        http=lambda *a, **k: calls.append(a) or (500, {}, None),
    )
    assert price_for("4663", "0xtok") is None
    assert calls == []


def test_default_blind_batch_does_not_call_second_provider_after_gt_miss():
    fn = capture._default_blind_price_batch_fn(
        {"4663"}, gt_batch_fn=lambda keys: {
            ("4663", "0xa"): {"price": 1.0, "source": "geckoterminal"}
        },
    )
    assert fn([("4663", "0xa"), ("4663", "0xb")]) == {
        ("4663", "0xa"): {"price": 1.0, "source": "geckoterminal"}
    }
```

- [ ] **Step 2: Run focused tests and verify failures**

```bash
python3 -m pytest tests/test_memecoin_evm_price.py tests/test_memecoin_capture_blind.py \
  tests/test_memecoin_geckoterminal_quote.py -q
```

Expected: current code performs or exposes the 0x fallback.

- [ ] **Step 3: Remove the fallback implementations**

In `memecoin_evm_price.py`:

- remove the provider quote import, key, RPC token-decimal probe, and quote-token conversion from `build_evm_price_for`;
- return a GeckoTerminal `(price, 'geckoterminal')` tuple or `None`;
- change the bare-float compatibility source in `evm_price_batch` from `zerox` to `probe`.

In `memecoin_capture_v2.py`:

- remove `zerox_batch_fn` and its lazy binding;
- return only the GeckoTerminal batch result;
- keep exceptions fail-closed and continue writing `probe_unpriced` gaps.

- [ ] **Step 4: Update active tests and retain explicit historical fixtures**

Rename current fallback tests to assert GeckoTerminal-only behavior. Change active injected sources to `geckoterminal` or `probe`. Keep database reader tests using literal `zerox` only when the test explicitly states it verifies historical provenance.

- [ ] **Step 5: Run focused tests and commit**

```bash
python3 -m pytest tests/test_memecoin_evm_price.py tests/test_memecoin_capture_blind.py \
  tests/test_memecoin_geckoterminal_quote.py tests/test_memecoin_network_id_keying.py -q
git add sniper/services/memecoin_evm_price.py sniper/services/memecoin_capture_v2.py \
  tests/test_memecoin_evm_price.py tests/test_memecoin_capture_blind.py \
  tests/test_memecoin_geckoterminal_quote.py tests/test_memecoin_network_id_keying.py
git commit -m "fix(memecoin): make blind pricing GeckoTerminal only"
```

Expected: focused pricing/capture tests pass without provider fallback calls.

---

### Task 4: Remove false readiness and operator dependencies

**Files:**
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `scripts/memecoin_evm_smoke_test.py`
- Modify: `tests/test_memecoin_real_api.py` or the existing real-status route test
- Modify: `tests/test_memecoin_marks_endpoint.py`
- Modify: `tests/test_memecoin_zerox_retirement.py`

**Interfaces:**
- Changes real-status venue payload to include `execution_venue` and direct `configured` truth; removes `zerox_key_set`.
- Changes the marks endpoint `source` from a fabricated constant to the newest persisted row source or `None`.
- Changes the smoke test to direct 4663 only.

- [ ] **Step 1: Write failing readiness and structural tests**

```python
ACTIVE = (
    "sniper/execution/evm_real_seams.py",
    "sniper/execution/evm_token_safety.py",
    "sniper/services/memecoin_evm_leg.py",
    "sniper/services/memecoin_evm_price.py",
    "sniper/services/memecoin_capture_v2.py",
    "sniper/settings/memecoin.py",
    "sniper/routes/api_memecoin.py",
    "scripts/memecoin_evm_smoke_test.py",
)


def test_active_runtime_has_no_zerox_dependency():
    forbidden = ("api.0x.org", "MEMECOIN_ZEROX_API_KEY", "zerox_quote",
                 "zerox_allowance_holder", "zerox_key_set")
    for name in ACTIVE:
        text = Path(name).read_text()
        assert not any(token in text for token in forbidden), name
```

Add route assertions:

```python
assert venue["execution_venue"] in {"direct", "disabled"}
assert "zerox_key_set" not in venue
assert venue["configured"] is (venue["execution_venue"] == "direct" and direct_config_present)
```

- [ ] **Step 2: Run route/structural tests and verify failures**

```bash
python3 -m pytest tests/test_memecoin_zerox_retirement.py tests/test_memecoin_marks_endpoint.py \
  -q
```

Expected: active modules still contain provider symbols and the status payload still exposes the key.

- [ ] **Step 3: Implement honest readiness and smoke behavior**

For each EVM venue in `real_status`:

```python
execution_venue = cfg.memecoin_evm_exec_venue(chain)
direct_config = cfg.memecoin_evm_v3_addresses(chain)
configured = bool(rpc) and e_present and execution_venue == "direct" and bool(direct_config)
```

Return `execution_venue` and `direct_configured`; remove `zerox_key_set`. Update the impact-cap comment to provider-neutral language.

For marks, select `source` with each row and return the newest row's source. Preserve old rows exactly.

For the smoke script, remove key imports and direct provider calls. Default to `4663`, require `execution_venue == 'direct'`, build the production executor, and obtain the dry-run quote from `ex['venue_quote_fn']`.

- [ ] **Step 4: Run focused tests and commit**

```bash
python3 -m pytest tests/test_memecoin_zerox_retirement.py tests/test_memecoin_marks_endpoint.py \
  tests/test_memecoin_runtime_deploy.py tests/test_memecoin_runtime_verify.py -q
python3 -m compileall -q sniper scripts
git add sniper/routes/api_memecoin.py scripts/memecoin_evm_smoke_test.py \
  tests/test_memecoin_zerox_retirement.py tests/test_memecoin_marks_endpoint.py
git commit -m "fix(memecoin): report direct EVM readiness honestly"
```

Expected: route, structural, runtime, and compile tests pass.

---

### Task 5: Full validation, Knowledge Sync, merge, and deployment

**Files:**
- Modify if required by review: files from Tasks 1-4 only.
- Runtime state after merge: `/home/bigpapi/Desktop/Sniper/.fomo.env` (remove key without printing it).

**Interfaces:**
- Produces a merged `main` release with no active 0x dependency.
- Preserves a uniquely pinned funded runtime and historical database provenance.

- [ ] **Step 1: Run adversarial static review**

```bash
rg -n 'api\.0x\.org|MEMECOIN_ZEROX_API_KEY|zerox_quote|zerox_allowance_holder|zerox_key_set' \
  sniper scripts
rg -n 'zerox' sniper scripts
git diff --check origin/main...HEAD
```

Expected: the first command returns no active runtime matches. Remaining `zerox` matches, if any, are explicitly historical provenance readers/comments and are reviewed one by one.

- [ ] **Step 2: Run full validation**

```bash
python3 scripts/validate_schema_refs.py
python3 -m pytest tests/test_memecoin*.py -q
python3 -m pytest tests/test_memecoin_chain_identity_concurrency.py \
  tests/test_memecoin_reservation_recovery.py tests/test_memecoin_runtime_deploy.py \
  tests/test_memecoin_runtime_verify.py -q
python3 -m compileall -q sniper scripts
node --check static/app.js
git diff --check
```

Expected: every command passes.

- [ ] **Step 3: Run the Knowledge Sync Gate and commit any required documentation delta**

Record that the project no longer has an active 0x subsystem; historical provenance remains valid; Base/BSC/Ethereum funded execution is disabled pending direct-route specs.

- [ ] **Step 4: Push, open the PR, pass checks, and merge**

Use a PR body ending with the 11-question Knowledge Delta Report. Do not push directly to `main`.

- [ ] **Step 5: Remove the obsolete runtime secret and deploy exact merged main**

Delete only the `MEMECOIN_ZEROX_API_KEY=` line from `.fomo.env` without printing its value. Then run:

```bash
git fetch origin --prune
scripts/deploy_memecoin_runtime.sh <full-merged-main-sha> --restart
```

The deployment's mandatory backup, reservation, reconciliation, full-suite, compile, manifest, PID/path, and heartbeat gates remain authoritative.

- [ ] **Step 6: Verify live behavior and clean up**

Verify:

- runtime symlink and manifest point at the merged SHA;
- exactly one healthy pinned runner exists;
- runner log arms `solana` and direct `4663`, but not disabled Base/BSC/Ethereum contexts;
- 4663 GeckoTerminal capture continues;
- no deployed file references `api.0x.org`;
- main and origin/main match, the feature worktree is removed, and feature branches are deleted.

Commit messages remain one logical change per task; no squash or force push.
