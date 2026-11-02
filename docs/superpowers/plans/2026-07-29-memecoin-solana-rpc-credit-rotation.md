# Memecoin Solana RPC Credit Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rotate three funded Solana Helius URLs only on explicit credit exhaustion and retain the current public RPC as final fallback.

**Architecture:** Parse one ordered environment pool, wrap the existing HTTP seam with a thread-safe selector, and pass that same callable through the existing executor/submission construction. The wrapper retries identical requests only on JSON-RPC `-32429`; all ambiguous failures retain existing fail-closed behavior.

**Tech Stack:** Python 3.13, stdlib threading/json/urllib parsing, pytest, existing pooled HTTP and pinned-runtime deploy scripts.

## Global Constraints

- Exactly three pool URLs plus the existing `MEMECOIN_SOLANA_RPC_URL` fallback when rotation is configured.
- Never log or persist URLs, API keys, response bodies, or credentials.
- Never rotate on HTTP 429/503, timeout, malformed response, or a JSON-RPC code other than `-32429`.
- Never alter signed bytes, transaction identity, admission, sizing, exit policy, confirmation, or accounting.
- Observation-only read/tape traffic stays outside the funded pool.
- Use TDD; no live RPC calls in tests.

---

### Task 1: Ordered pool parser and selector

**Files:**
- Create: `sniper/execution/solana_rpc_pool.py`
- Modify: `sniper/settings/memecoin.py`
- Create: `tests/test_memecoin_solana_rpc_pool.py`

**Interfaces:**
- Produces: `solana_funded_rpc_urls(env=None) -> tuple[str, ...]`
- Produces: `RotatingSolanaRpcHttp(http, urls, retry_s=3600, clock=None, log=None)` callable
- Produces: `RotatingSolanaRpcHttp.snapshot() -> dict`

- [ ] **Step 1: Write parser and rotation tests**

```python
def test_pool_requires_three_https_urls_and_appends_fallback():
    env = {
        "MEMECOIN_SOLANA_RPC_POOL_URLS": "https://h1,https://h2,https://h3",
        "MEMECOIN_SOLANA_RPC_URL": "https://public",
    }
    assert solana_funded_rpc_urls(env) == (
        "https://h1", "https://h2", "https://h3", "https://public")

def test_credit_exhaustion_retries_identical_body_on_next_url():
    calls = []
    def http(method, url, **kwargs):
        calls.append((url, kwargs["body"]))
        if url == "https://h1":
            return 200, {"error": {"code": -32429, "message": "max usage reached"}}, None
        return 200, {"result": 7}, None
    rpc = RotatingSolanaRpcHttp(http, ("https://h1", "https://h2"))
    body = '{"method":"getBalance"}'
    assert rpc("POST", "https://h1", body=body)[1] == {"result": 7}
    assert calls == [("https://h1", body), ("https://h2", body)]

@pytest.mark.parametrize("response", [
    (429, {"error": "rate"}, None),
    (503, {"error": "down"}, None),
    (200, {"error": {"code": -32000}}, None),
    (200, {"garbage": True}, None),
])
def test_transient_and_ambiguous_failures_do_not_rotate(response):
    calls = []
    rpc = RotatingSolanaRpcHttp(
        lambda _m, url, **_k: calls.append(url) or response,
        ("https://h1", "https://h2"))
    assert rpc("POST", "https://h1", body="{}") == response
    assert calls == ["https://h1"]

def test_concurrent_exhaustion_advances_once():
    barrier = threading.Barrier(2)
    calls = []
    def http(_method, url, **_kwargs):
        calls.append(url)
        if url == "https://h1":
            barrier.wait()
            return 200, {"error": {"code": -32429}}, None
        return 200, {"result": "ok"}, None
    rpc = RotatingSolanaRpcHttp(http, ("https://h1", "https://h2", "https://h3"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: rpc("POST", "https://h1", body="{}"), range(2)))
    assert all(result[1] == {"result": "ok"} for result in results)
    assert rpc.snapshot()["active_index"] == 1

def test_cooldown_probe_restores_refilled_key():
    now = [0.0]
    refilled = [False]
    def http(_method, url, **kwargs):
        method = json.loads(kwargs["body"])["method"]
        if url == "https://h1" and not refilled[0]:
            return 200, {"error": {"code": -32429}}, None
        if method == "getGenesisHash":
            return 200, {"result": SOLANA_MAINNET_GENESIS}, None
        return 200, {"result": "ok"}, None
    rpc = RotatingSolanaRpcHttp(
        http, ("https://h1", "https://h2"), retry_s=3600, clock=lambda: now[0])
    rpc("POST", "https://h1", body='{"method":"getBalance"}')
    refilled[0] = True
    now[0] = 3601
    rpc("POST", "https://h1", body='{"method":"getBalance"}')
    assert rpc.snapshot()["active_index"] == 0

def test_snapshot_never_contains_urls_or_keys():
    rpc = RotatingSolanaRpcHttp(lambda *_a, **_k: (200, {}, None),
                                ("https://h1?api-key=secret", "https://public"))
    assert "https" not in repr(rpc.snapshot())
    assert "secret" not in repr(rpc.snapshot())
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_memecoin_solana_rpc_pool.py -q`
Expected: collection failure because `sniper.execution.solana_rpc_pool` does not exist.

- [ ] **Step 3: Implement the minimal parser and callable**

Use one lock for index/exhaustion state and one non-blocking probe lock. Detect only
`payload.get("error", {}).get("code") == -32429`. A cooldown probe sends only `getGenesisHash` and
accepts only the pinned Solana mainnet genesis.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_solana_rpc_pool.py -q`
Expected: all tests pass.

### Task 2: Funded-context wiring

**Files:**
- Modify: `sniper/services/memecoin_real_runner.py`
- Modify: `sniper/execution/submission_backends.py`
- Modify: `tests/test_memecoin_real_runner.py`
- Modify: `tests/test_memecoin_submission_backends.py`

**Interfaces:**
- Consumes: `solana_funded_rpc_urls()` and `RotatingSolanaRpcHttp`
- Produces: one shared callable used by executor, balances, submission, confirmation, validation, and warming

- [ ] **Step 1: Write failing real-context wiring tests**

```python
def test_real_context_shares_rotating_http_and_uses_fallback_for_sidecar(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_POOL_URLS",
                        ("https://h1", "https://h2", "https://h3"))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "https://public")
    seen = {}
    monkeypatch.setattr(solana_tpu_sidecar, "resolve_tpu_sidecar",
                        lambda **kwargs: seen.setdefault("sidecar_rpc", kwargs["rpc_url"]) or None)
    context = R.build_real_context(http=identity_ok_http)
    assert context["rpc_pool_status_fn"]()["endpoint_count"] == 4
    assert seen["sidecar_rpc"] == "https://public"

def test_external_owned_http_can_keep_submission_warming(monkeypatch):
    started = []
    monkeypatch.setattr(B, "_start_warming", lambda specs, interval: started.append(specs))
    B.build_solana_backend(
        keypair=Keypair(), custody_rpc_url="https://h1", http=identity_ok_http,
        warm_external_http=True)
    assert started and started[0][0][0] == "solana:custody_rpc"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_submission_backends.py -q`
Expected: failures showing no pool wiring and no managed external warmer.

- [ ] **Step 3: Wire the selector through existing seams**

When more than one funded URL exists, create one pooled client, register it with the existing backend
lifecycle, wrap it once, and pass the wrapper everywhere. Resolve the TPU sidecar with the last URL.
Keep the legacy single-URL branch byte-for-byte equivalent.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_memecoin_real_runner.py tests/test_memecoin_submission_backends.py -q`
Expected: all tests pass.

### Task 3: Regression, knowledge sync, and deployment gate

**Files:**
- Modify: `AGENTS.md`
- Verify: `docs/superpowers/specs/2026-07-29-memecoin-solana-rpc-credit-rotation-design.md`

**Interfaces:**
- Consumes: completed implementation and approved funded-path design
- Produces: merged SHA eligible for pinned deployment

- [ ] **Step 1: Run focused and adjacent suites**

Run: `python3 -m pytest tests/test_memecoin_solana_rpc_pool.py tests/test_memecoin_real_seams.py tests/test_memecoin_solana_wallet.py tests/test_memecoin_submission_backends.py tests/test_memecoin_submission_transport.py tests/test_memecoin_real_runner.py tests/test_memecoin_runtime_verify.py tests/test_memecoin_runtime_deploy.py -q`

- [ ] **Step 2: Run static repository checks**

Run: `python3 scripts/validate_schema_refs.py && python3 -m compileall -q sniper scripts && git diff --check`

- [ ] **Step 3: Complete the Knowledge Sync Gate**

Record the new funded availability contract in `AGENTS.md`; the PR body must contain the 11-question
Knowledge Delta Report and identify the new design/plan. Do not write Supabase automatically.

- [ ] **Step 4: Commit and publish the feature branch**

Run: `git add <scoped-files> && git commit -m "fix(memecoin): rotate exhausted Solana RPC credits"`
Then push the feature branch and open a PR; never push directly to `origin/main`.

- [ ] **Step 5: Deploy only a merged SHA with all runtime gates**

Run: `scripts/deploy_memecoin_runtime.sh <merged-full-sha> --restart`
Then run the pinned verifier and smoke checks against `.runtime/memecoin/current`; verify release SHA,
one process, heartbeat, authenticated health, zero unresolved reservations, clear kill switch, sidecar,
rotation status, logs, and backup. Do not activate `MEMECOIN_SOLANA_RPC_POOL_URLS` until three real URLs
are present.
