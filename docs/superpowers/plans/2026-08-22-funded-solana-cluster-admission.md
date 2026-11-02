# Funded Solana Cluster Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Block every funded Solana entry unless a fresh supply-weighted funder-cluster decision allows it.

**Architecture:** A pinned local worker owns the research RPC, the cluster database, and the Unix socket. The common funded Solana entry seam calls the worker after cheap checks and before any reservation or quote. Scanner paper paths do not call this service.

**Tech Stack:** Python 3.13, SQLite WAL, Unix sockets, Solana JSON-RPC, pytest, Bash pinned-runtime scripts.

**Spec:** `docs/superpowers/specs/2026-08-22-memecoin-shared-cluster-admission-design.md`

## Global Constraints

- The policy ID is `solana-funded-cluster-admission-v3`.
- The clustered attempted-supply limit is 25% inclusive.
- The largest-funder attempted-supply limit is 50% inclusive.
- An allow requires all eligible owners to resolve.
- The trace deadline is 12 seconds.
- The client deadline is 13 seconds.
- The trace uses eight workers.
- An allowed decision expires five seconds after completion.
- The service uses only `MEMECOIN_RESEARCH_HELIUS_RPC_URL`.
- A missing service, timeout, 429, trace error, or unresolved owner blocks the funded entry.
- The gate applies only to funded Solana entries.
- No wallet, manual-entry, retry, or source-path bypass exists.
- Scanner paper paths keep their current behavior.
- The gate runs after cheap funded checks and before any reservation or entry quote.
- The implementation does not change sizing, exit policy, roster policy, or EVM entry behavior.
- The implementation stays default off until a separate funded activation.
- The implementation uses an isolated worktree.
- The implementation uses TDD and focused tests.
- Each blocked decision opens V2 marks at 15, 30, 60, and 300 seconds when a fresh reference price exists.
- A missing blocked reference price stays `coverage_missing_ref_price`. It never becomes a zero return.

---

## File Map

- `sniper/services/memecoin_cluster_admission.py`: Pure policy, types, metrics, and asymmetric decision logic.
- `sniper/services/memecoin_cluster_rpc.py`: Strict research-RPC holder, owner, and first-funder reads.
- `sniper/services/memecoin_cluster_store.py`: Dedicated schema and immutable decision persistence.
- `sniper/services/memecoin_cluster_service.py`: Unix-socket protocol, service controller, and fail-closed client.
- `scripts/memecoin_cluster_gate_worker.py`: Pinned worker entry point and heartbeat owner.
- `scripts/memecoin_cluster_gate_watchdog.sh`: Isolated worker supervision.
- `sniper/services/memecoin_risk.py`: Read-only funded risk preflight before the cluster call.
- `sniper/services/memecoin_real_leg.py`: Common Solana cluster seam before reservation.
- `sniper/services/memecoin_real_runner.py`: Client injection for feed, follow, on-chain, retry, and manual paths.
- `sniper/services/memecoin_entry_trace.py`: Cluster stage timing.
- `sniper/services/memecoin_capture_v2.py`: V2 marks for blocked opportunity paths.
- `sniper/persistence/db_memecoin.py`: Decision identity on attempts, reservations, and funded positions.
- `sniper/settings/memecoin.py`: Default-off policy settings.
- `scripts/memecoin_watchdog.sh`: Start the cluster worker without coupling it to exit health.
- `scripts/deploy_memecoin_runtime.sh`: Validate the worker and support explicit activation.

---

### Task 1: Create the pure cluster policy

**Files:**
- Create: `sniper/services/memecoin_cluster_admission.py`
- Create: `tests/test_memecoin_cluster_admission.py`
- Modify: `scripts/research/runner_scanner.py`
- Test: `tests/test_runner_scanner_gate.py`

**Interfaces:**
- Consumes: `list[tuple[str, int]]` owner balances and `dict[str, str]` cached funders.
- Produces: `ClusterDecision`, `decide_cluster()`, and stable reason codes.

- [ ] **Step 1: Write exact-boundary and asymmetric-decision tests**

```python
from sniper.services.memecoin_cluster_admission import decide_cluster


def test_exact_25_percent_cluster_blocks():
    owners = [("A", 15), ("B", 10), ("C", 25), ("D", 25), ("E", 25)]
    funders = {"A": "WEB", "B": "WEB", "C": "C", "D": "D", "E": "E"}
    out = decide_cluster(owners, funders, trace_fn=lambda _owner: None)
    assert out.allowed is False
    assert out.reason == "cluster_share_high"


def test_allow_requires_every_owner():
    owners = [("A", 40), ("B", 30), ("C", 20), ("D", 10)]
    out = decide_cluster(
        owners,
        {"A": "FA", "B": "FB"},
        trace_fn=lambda _owner: (None, "unresolved"),
    )
    assert out.allowed is False
    assert out.reason == "cluster_unknown_unresolved"


def test_fewer_than_four_owners_blocks():
    owners = [("A", 40), ("B", 30), ("C", 30)]
    out = decide_cluster(
        owners,
        {"A": "A", "B": "B", "C": "C"},
        trace_fn=lambda _owner: (_ for _ in ()).throw(AssertionError("must not trace")),
    )
    assert out.allowed is False
    assert out.reason == "cluster_too_few_owners"


def test_clean_book_allows_only_after_full_resolution():
    owners = [("A", 40), ("B", 30), ("C", 20), ("D", 10)]
    out = decide_cluster(owners, {}, trace_fn=lambda owner: ("F" + owner, "resolved"))
    assert out.allowed is True
    assert out.metrics["coverage"] == 1.0
```

Add named tests for these required cases:

- `test_exact_50_percent_top_funder_blocks`
- `test_exact_50_percent_coverage_cannot_allow`
- `test_aas_supply_fixture_blocks`
- `test_eigenslur_lower_bound_blocks`
- `test_unknown_rank_one_stays_eligible`
- `test_buyer_count_cannot_satisfy_supply_policy`
- `test_deadline_returns_unknown_block`
- `test_trace_error_returns_unknown_block`

- [ ] **Step 2: Run the new tests and confirm the import failure**

Run: `python3 -m pytest tests/test_memecoin_cluster_admission.py -q`

Expected: FAIL because `sniper.services.memecoin_cluster_admission` does not exist.

- [ ] **Step 3: Implement immutable types and the pure decision**

```python
POLICY_ID = "solana-funded-cluster-admission-v3"
CLUSTERED_LIMIT = 0.25
TOP_FUNDER_LIMIT = 0.50
MIN_ELIGIBLE_OWNERS = 4


class ClusterQuotaError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClusterDecision:
    decision_id: str
    allowed: bool
    reason: str
    policy_id: str
    snapshot_at: str
    completed_at: str
    metrics: dict[str, int | float | None]


def decide_cluster(
    owners_by_supply: list[tuple[str, int]],
    cached: dict[str, str],
    *,
    trace_fn: Callable[[str], tuple[str | None, str]],
    persist_fn: Callable[[str, str | None, str], None] | None = None,
    workers: int = 8,
    deadline_s: float = 12.0,
    clock: Callable[[], float] = time.monotonic,
    now_fn: Callable[[], str] = real_utc_now_iso,
    snapshot_at: str | None = None,
) -> ClusterDecision:
    snapshot_at = snapshot_at or now_fn()
    balances: dict[str, int] = {}
    for owner, balance in owners_by_supply:
        if int(balance) > 0:
            balances[owner] = balances.get(owner, 0) + int(balance)
    attempted = sum(balances.values())
    if len(balances) < MIN_ELIGIBLE_OWNERS or attempted <= 0:
        reason = "cluster_too_few_owners" if len(balances) < MIN_ELIGIBLE_OWNERS else "cluster_no_holders"
        return ClusterDecision(
            decision_id=uuid.uuid4().hex,
            allowed=False,
            reason=reason,
            policy_id=POLICY_ID,
            snapshot_at=snapshot_at,
            completed_at=now_fn(),
            metrics={"attempted_supply": attempted, "eligible": len(balances)},
        )
    funders = {owner: cached[owner] for owner in balances if cached.get(owner)}
    started = clock()

    def metrics() -> dict[str, int | float | None]:
        groups: dict[str, list[str]] = {}
        for owner, funder in funders.items():
            groups.setdefault(funder, []).append(owner)
        clustered = sum(
            balances[owner]
            for owners in groups.values() if len(owners) >= 2
            for owner in owners
        )
        top = max(
            (sum(balances[owner] for owner in owners) for owners in groups.values()),
            default=0,
        )
        resolved = sum(balances[owner] for owner in funders)
        return {
            "attempted_supply": attempted,
            "resolved_supply": resolved,
            "clustered_supply": clustered,
            "largest_funder_supply": top,
            "coverage": resolved / attempted,
            "clustered_of_attempted": clustered / attempted,
            "top_funder_of_attempted": top / attempted,
            "eligible": len(balances),
            "resolved": len(funders),
        }

    def final(reason: str, allowed: bool = False) -> ClusterDecision:
        return ClusterDecision(
            decision_id=uuid.uuid4().hex,
            allowed=allowed,
            reason=reason,
            policy_id=POLICY_ID,
            snapshot_at=snapshot_at,
            completed_at=now_fn(),
            metrics=metrics(),
        )

    def block_if_final() -> ClusterDecision | None:
        current = metrics()
        if current["clustered_of_attempted"] >= CLUSTERED_LIMIT:
            return final("cluster_share_high")
        if current["top_funder_of_attempted"] >= TOP_FUNDER_LIMIT:
            return final("cluster_top_funder_high")
        return None

    early = block_if_final()
    if early is not None:
        return early
    todo = sorted(
        (owner for owner in balances if owner not in funders),
        key=balances.get,
        reverse=True,
    )
    while todo:
        remaining = deadline_s - (clock() - started)
        if remaining <= 0:
            return final("cluster_unknown_deadline")
        batch, todo = todo[:workers], todo[workers:]
        pool = ThreadPoolExecutor(max_workers=len(batch))
        try:
            futures = {pool.submit(trace_fn, owner): owner for owner in batch}
            done, pending = wait(futures, timeout=remaining)
            if pending:
                for future in pending:
                    future.cancel()
                return final("cluster_unknown_deadline")
            for future in done:
                owner = futures[future]
                try:
                    funder, status = future.result()
                except ClusterQuotaError:
                    return final("cluster_unknown_quota")
                except Exception:
                    return final("cluster_unknown_trace_error")
                if persist_fn is not None:
                    persist_fn(owner, funder, status)
                if status == "resolved" and funder:
                    funders[owner] = funder
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        early = block_if_final()
        if early is not None:
            return early
    if len(funders) != len(balances):
        return final("cluster_unknown_unresolved")
    return final("allowed", allowed=True)
```

Use atomic integer balances. Keep the stable reason separate from all display percentages.

- [ ] **Step 4: Preserve the research API without duplicate policy code**

Replace the research implementation with imports that keep current call sites valid:

```python
from sniper.services.memecoin_cluster_admission import (
    CLUSTERED_LIMIT as CLUSTER_BLOCK_CLUSTERED,
    TOP_FUNDER_LIMIT as CLUSTER_BLOCK_TOP,
    decide_cluster as _shared_cluster_decide,
)


def cluster_decide(*args, **kwargs):
    out = _shared_cluster_decide(*args, **kwargs)
    return out.allowed, (None if out.allowed else out.reason), out.metrics
```

Do not connect this import to any scanner arm.

- [ ] **Step 5: Run focused policy tests**

Run: `python3 -m pytest tests/test_memecoin_cluster_admission.py tests/test_runner_scanner_gate.py -q`

Expected: PASS.

- [ ] **Step 6: Commit the pure policy**

```bash
git add sniper/services/memecoin_cluster_admission.py scripts/research/runner_scanner.py \
  tests/test_memecoin_cluster_admission.py tests/test_runner_scanner_gate.py
git commit -m "feat(memecoin): add funded cluster policy"
```

---

### Task 2: Add strict research-RPC reads and the durable store

**Files:**
- Create: `sniper/services/memecoin_cluster_rpc.py`
- Create: `sniper/services/memecoin_cluster_store.py`
- Create: `tests/test_memecoin_cluster_rpc.py`
- Create: `tests/test_memecoin_cluster_store.py`

**Interfaces:**
- Consumes: one explicit research RPC URL and one mint.
- Produces: `ResearchClusterRpc`, `ClusterStore`, holder-owner rows, and cached funders.

- [ ] **Step 1: Write RPC boundary tests**

```python
def test_429_has_a_typed_quota_result(fake_http):
    fake_http.add_status(429)
    rpc = ResearchClusterRpc("https://research.invalid", http=fake_http)
    with pytest.raises(ResearchRpcQuotaError):
        rpc.call("getSignaturesForAddress", ["OWNER", {"limit": 1000}])


def test_first_funder_reads_inner_transfer(fake_rpc):
    fake_rpc.signatures("OWNER", ["SIG"])
    fake_rpc.transaction("SIG", inner_transfer("FUNDER", "OWNER"))
    assert fake_rpc.client.trace_first_funder("OWNER") == ("FUNDER", "resolved")
```

Add tests that keep the creator and rank-one owner in the eligible book. Exclude only a verified curve, pool vault, or program account.

- [ ] **Step 2: Write store migration and immutability tests**

```python
def test_store_keeps_atomic_balances_as_text(tmp_path):
    store = ClusterStore(tmp_path / "cluster.db")
    snapshot_id = store.create_snapshot("MINT", "2026-08-22T00:00:00Z")
    store.add_holder(snapshot_id, "ATA", "OWNER", "18446744073709551615")
    assert store.holders(snapshot_id)[0]["balance_atomic"] == "18446744073709551615"


def test_decision_rows_are_immutable(tmp_path):
    store = ClusterStore(tmp_path / "cluster.db")
    store.insert_decision(_decision("decision-1"))
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_decision(_decision("decision-1", reason="changed"))


def test_unresolved_owner_enters_newest_first_queue(tmp_path):
    store = ClusterStore(tmp_path / "cluster.db")
    store.enqueue_unresolved("OLD", observed_at="2026-08-22T00:00:00Z")
    store.enqueue_unresolved("NEW", observed_at="2026-08-22T00:00:01Z")
    assert store.claim_cache_work(limit=2) == ["NEW", "OLD"]
```

- [ ] **Step 3: Run the tests and confirm both imports fail**

Run: `python3 -m pytest tests/test_memecoin_cluster_rpc.py tests/test_memecoin_cluster_store.py -q`

Expected: FAIL because both modules do not exist.

- [ ] **Step 4: Implement the strict RPC adapter**

```python
@dataclass(frozen=True)
class EligibleHolder:
    ata: str
    owner: str | None
    balance_atomic: int
    exclusion: str | None


class ResearchClusterRpc:
    def __init__(self, url: str, *, http=urlopen, timeout_s: float = 8.0):
        if not url or "api-key" not in url:
            raise ValueError("a dedicated research Helius URL is required")
        self.url = url
        self.http = http
        self.timeout_s = timeout_s

    def call(self, method: str, params: list) -> object:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": method, "params": params,
        }).encode()
        request = Request(self.url, data=body, headers={"content-type": "application/json"})
        try:
            return json.load(self.http(request, timeout=self.timeout_s))["result"]
        except HTTPError as exc:
            if exc.code == 429:
                raise ClusterQuotaError() from exc
            raise
```

Add these exact methods:

```text
holder_accounts(mint: str) -> list[tuple[ata: str, balance_atomic: str]]
token_account_owners(accounts: list[str]) -> dict[ata: str, owner: str]
eligible_holders(mint: str) -> list[EligibleHolder]
trace_first_funder(owner: str, max_pages: int = 20) -> tuple[funder: str | None, status: str]
```

Do not read any fallback URL. Parse top-level and inner `transfer` and `createAccount` instructions.

- [ ] **Step 5: Implement the dedicated WAL store**

Implement these exact methods:

```text
create_snapshot(mint: str, snapshot_at: str) -> str
snapshot(snapshot_id: str) -> dict
add_holder(snapshot_id: str, ata: str, owner: str | None, balance_atomic: str, exclusion: str | None) -> None
cached_funders(owners: list[str], parser_version: int) -> dict[str, str]
put_funder(owner: str, funder: str | None, status: str, source_signature: str | None, parser_version: int) -> None
insert_decision(decision: ClusterDecision, snapshot_id: str | None, source_path: str) -> None
enqueue_unresolved(owner: str, observed_at: str, status: str) -> None
claim_cache_work(limit: int, now_iso: str) -> list[str]
finish_cache_work(owner: str, status: str, next_attempt_at: str | None) -> None
```

Use `SNIPER_STATE_ROOT/data/memecoin_cluster_gate.db`. Use PASSIVE WAL behavior only.

The queue orders work by newest observation first. A quota result stops the batch and applies bounded backoff.

- [ ] **Step 6: Run focused RPC and store tests**

Run: `python3 -m pytest tests/test_memecoin_cluster_rpc.py tests/test_memecoin_cluster_store.py -q`

Expected: PASS.

- [ ] **Step 7: Commit RPC and storage**

```bash
git add sniper/services/memecoin_cluster_rpc.py sniper/services/memecoin_cluster_store.py \
  tests/test_memecoin_cluster_rpc.py tests/test_memecoin_cluster_store.py
git commit -m "feat(memecoin): add cluster RPC store"
```

---

### Task 3: Add the local cluster service and fail-closed client

**Files:**
- Create: `sniper/services/memecoin_cluster_service.py`
- Create: `scripts/memecoin_cluster_gate_worker.py`
- Create: `scripts/memecoin_cluster_gate_watchdog.sh`
- Create: `tests/test_memecoin_cluster_service.py`
- Create: `tests/test_memecoin_cluster_gate_worker.py`

**Interfaces:**
- Consumes: `{"mint", "source_path", "requested_at"}` over a Unix socket.
- Produces: one persisted `ClusterDecision` response and a one-second heartbeat.

- [ ] **Step 1: Write protocol and failure tests**

```python
def test_client_timeout_blocks():
    client = ClusterGateClient("/missing/socket", timeout_s=0.01)
    out = client.decide("MINT", source_path="global_feed")
    assert out.allowed is False
    assert out.reason == "cluster_store_unavailable"


def test_service_maps_429_to_quota_block(service):
    service.rpc.raise_quota = True
    out = service.decide("MINT", source_path="onchain")
    assert out.allowed is False
    assert out.reason == "cluster_unknown_quota"
    assert service.store.decision(out.decision_id)["reason"] == out.reason


def test_service_without_research_rpc_blocks(service):
    service.rpc = None
    out = service.decide("MINT", source_path="manual")
    assert out.allowed is False
    assert out.reason == "cluster_no_rpc"


def test_client_rejects_a_stale_allow(fake_socket):
    fake_socket.reply(_allowed_decision(completed_at="2026-08-22T00:00:00Z"))
    client = ClusterGateClient(fake_socket.path, now_fn=lambda: "2026-08-22T00:00:06Z")
    out = client.decide("MINT", source_path="retry")
    assert out.allowed is False
    assert out.reason == "cluster_decision_stale"


def test_cache_pass_stops_after_first_429(worker):
    worker.store.queue("A", "B", "C")
    worker.rpc.quota_on("B")
    assert worker.cache_pass() == {"resolved": 1, "quota": 1}
    assert worker.rpc.trace_calls == ["A", "B"]
```

- [ ] **Step 2: Run the tests and confirm the import failure**

Run: `python3 -m pytest tests/test_memecoin_cluster_service.py tests/test_memecoin_cluster_gate_worker.py -q`

Expected: FAIL because the service does not exist.

- [ ] **Step 3: Implement the service controller**

```python
class ClusterAdmissionService:
    def decide(self, mint: str, *, source_path: str, requested_at: str) -> ClusterDecision:
        if self.rpc is None:
            decision = ClusterDecision(
                decision_id=uuid.uuid4().hex,
                allowed=False,
                reason="cluster_no_rpc",
                policy_id=POLICY_ID,
                snapshot_at=self.now_fn(),
                completed_at=self.now_fn(),
                metrics={},
            )
            self.store.insert_decision(decision, None, source_path)
            return decision
        holders = self.rpc.eligible_holders(mint)
        snapshot_id = self.store.create_snapshot(mint, self.now_fn())
        owners_by_supply: dict[str, int] = {}
        for holder in holders:
            self.store.add_holder(
                snapshot_id, holder.ata, holder.owner,
                str(holder.balance_atomic), holder.exclusion,
            )
            if holder.owner is not None and holder.exclusion is None:
                owners_by_supply[holder.owner] = (
                    owners_by_supply.get(holder.owner, 0) + holder.balance_atomic
                )
        cached = self.store.cached_funders(list(owners_by_supply), PARSER_VERSION)
        for owner in owners_by_supply:
            if owner not in cached:
                self.store.enqueue_unresolved(owner, self.now_fn(), "pending")
        decision = decide_cluster(
            list(owners_by_supply.items()),
            cached,
            trace_fn=self.rpc.trace_first_funder,
            persist_fn=self.store.put_funder_result,
            workers=8,
            deadline_s=12.0,
            snapshot_at=self.store.snapshot(snapshot_id)["snapshot_at"],
        )
        self.store.insert_decision(decision, snapshot_id, source_path)
        return decision
```

Serialize requests in one process. Keep the eight-way parallelism inside one decision.

- [ ] **Step 4: Implement the line-delimited JSON socket protocol**

Use one request and one response per connection. Limit each line to 16 KiB.

```json
{"mint":"MINT","requested_at":"2026-08-22T00:00:00Z","source_path":"global_feed"}
```

```json
{"allowed":false,"completed_at":"2026-08-22T00:00:01Z","decision_id":"decision-1","metrics":{},"policy_id":"solana-funded-cluster-admission-v3","reason":"cluster_share_high","snapshot_at":"2026-08-22T00:00:00Z"}
```

- [ ] **Step 5: Implement the cache pass**

When no socket request waits, process a bounded queue batch. Stop on the first 429 response.

Write unresolved results with bounded backoff. Never delay a funded socket request for cache work.

- [ ] **Step 6: Implement the worker, heartbeat, and status**

The worker must refuse startup without `MEMECOIN_RESEARCH_HELIUS_RPC_URL`. Write `data/memecoin_cluster_gate.heartbeat` once per second.

Write `data/memecoin_cluster_gate.status.json` atomically. Include queue depth, cache counts, last decision age, and block counts by reason.

- [ ] **Step 7: Implement isolated supervision**

The watchdog must use `/proc` process checks. It must not use `pgrep -f` or `pkill -f`.

- [ ] **Step 8: Run focused service tests and shell validation**

Run: `python3 -m pytest tests/test_memecoin_cluster_service.py tests/test_memecoin_cluster_gate_worker.py -q`

Run: `bash -n scripts/memecoin_cluster_gate_watchdog.sh`

Expected: PASS.

- [ ] **Step 9: Commit the service**

```bash
git add sniper/services/memecoin_cluster_service.py scripts/memecoin_cluster_gate_worker.py \
  scripts/memecoin_cluster_gate_watchdog.sh tests/test_memecoin_cluster_service.py \
  tests/test_memecoin_cluster_gate_worker.py
git commit -m "feat(memecoin): add cluster gate service"
```

---

### Task 4: Persist the decision through the funded ledger

**Files:**
- Modify: `sniper/persistence/db_memecoin.py`
- Modify: `sniper/services/memecoin_entry_trace.py`
- Modify: `sniper/services/memecoin_capture_v2.py`
- Modify: `sniper/execution/solana_swap_executor.py`
- Modify: `tests/test_memecoin_entry_trace.py`
- Modify: `tests/test_memecoin_capture_v2.py`
- Create: `tests/test_memecoin_cluster_ledger.py`

**Interfaces:**
- Consumes: an allowed or blocked `ClusterDecision`.
- Produces: immutable cluster fields on the attempt, reservation, and funded position.

- [ ] **Step 1: Write schema and propagation tests**

<!-- schema-validator: ignore-next-block -->
```python
def test_cluster_fields_exist_on_all_funded_entry_rows(conn):
    ensure_memecoin_tables(conn)
    assert {"cluster_decision_id", "cluster_completed_at", "cluster_ms"} <= columns(
        conn, "memecoin_entry_attempts")
    assert {"cluster_decision_id"} <= columns(conn, "memecoin_pending_entries")
    assert {"cluster_decision_id"} <= columns(conn, "memecoin_positions")


def test_cluster_decision_reaches_filled_position(conn):
    pending_id = reserve_fixture(conn, cluster_decision_id="decision-1")
    position_id = confirm_fixture(conn, pending_id)
    row = conn.execute(
        "SELECT cluster_decision_id FROM memecoin_positions WHERE id=?", (position_id,)
    ).fetchone()
    assert row[0] == "decision-1"


def test_blocked_decision_opens_fixed_v2_outcome(conn):
    attempt_id = "attempt-1"
    outcome_id = create_cluster_block_outcome(
        conn,
        attempt_id=attempt_id,
        mint="MINT",
        observed_at="2026-08-22T00:00:00Z",
        ref_price=0.001,
    )
    outcome = conn.execute(
        "SELECT upstream_event_id,target_horizons_json FROM memecoin_signal_outcomes_v2 "
        "WHERE outcome_id=?", (outcome_id,),
    ).fetchone()
    assert tuple(outcome) == ("cluster-block:attempt-1", "[15,30,60,300]")


def test_missing_block_reference_stays_typed(conn):
    status = record_cluster_block_outcome(
        conn, attempt_id="attempt-1", mint="MINT", observed_at="2026-08-22T00:00:00Z",
        ref_price=None,
    )
    assert status == "coverage_missing_ref_price"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_signal_outcomes_v2"
    ).fetchone()[0] == 0
```

- [ ] **Step 2: Run the tests and confirm the missing-column failure**

Run: `python3 -m pytest tests/test_memecoin_cluster_ledger.py tests/test_memecoin_entry_trace.py -q`

Expected: FAIL because cluster columns do not exist.

- [ ] **Step 3: Add additive schema migrations and immutable setters**

Add these attempt columns:

```text
cluster_decision_id TEXT
cluster_reason TEXT
cluster_metrics_json TEXT
cluster_completed_at TEXT
cluster_ms REAL
cluster_outcome_id TEXT
cluster_outcome_status TEXT
```

Add `cluster_decision_id TEXT` to `memecoin_pending_entries` and `memecoin_positions`.

Implement:

<!-- schema-validator: ignore-next-block -->
```python
def set_entry_attempt_cluster_decision(
    conn, attempt_id, *, decision_id, reason, metrics_json, completed_at, duration_ms
) -> bool:
    row = conn.execute(
        "SELECT cluster_decision_id FROM memecoin_entry_attempts WHERE attempt_id=?",
        (attempt_id,),
    ).fetchone()
    if row is None:
        raise ValueError("unknown entry attempt")
    if row[0] not in (None, decision_id):
        raise ValueError("cluster decision is immutable")
    changed = conn.execute(
        "UPDATE memecoin_entry_attempts SET cluster_decision_id=?, cluster_reason=?, "
        "cluster_metrics_json=?, cluster_completed_at=?, cluster_ms=? "
        "WHERE attempt_id=? AND terminal_status IS NULL",
        (decision_id, reason, metrics_json, completed_at, duration_ms, attempt_id),
    ).rowcount
    conn.commit()
    return changed == 1
```

Reject a second different decision ID for one attempt.

- [ ] **Step 4: Add the blocked opportunity outcome**

Implement this helper in `memecoin_capture_v2.py`:

```python
CLUSTER_BLOCK_HORIZONS_S = (15, 30, 60, 300)


def create_cluster_block_outcome(
    conn, *, attempt_id: str, mint: str, observed_at: str, ref_price: float,
    source_user_id: str | None = None, handle: str | None = None,
) -> str:
    return create_outcome_v2(
        conn,
        upstream_event_id=f"cluster-block:{attempt_id}",
        source_user_id=source_user_id,
        handle=handle,
        network_id="solana",
        mint=mint,
        ticker=None,
        signal_observed_at=observed_at,
        ref_price=ref_price,
        horizons=CLUSTER_BLOCK_HORIZONS_S,
        created_at=observed_at,
    )


def record_cluster_block_outcome(conn, **kwargs) -> str:
    attempt_id = kwargs["attempt_id"]
    ref_price = kwargs.pop("ref_price", None)
    if not isinstance(ref_price, (int, float)) or ref_price <= 0:
        db_memecoin.set_entry_attempt_cluster_outcome(
            conn, attempt_id, outcome_id=None, status="coverage_missing_ref_price",
        )
        return "coverage_missing_ref_price"
    outcome_id = create_cluster_block_outcome(conn, ref_price=ref_price, **kwargs)
    db_memecoin.set_entry_attempt_cluster_outcome(
        conn, attempt_id, outcome_id=outcome_id, status="open",
    )
    return "open"
```

After a cluster block, request one fresh market price. Open the outcome from the block completion time.

Store `coverage_missing_ref_price` when the fresh read has no positive price. Do not store a zero return.

Add an immutable `set_entry_attempt_cluster_outcome()` setter for the outcome ID and status.

The existing V2 capture pass will register the open outcome as a priority-100 signal cohort.

- [ ] **Step 5: Add the `cluster` trace stage**

Add `cluster` to `_STAGES` and map it to `cluster_completed_at` and `cluster_ms`.

- [ ] **Step 6: Propagate the decision ID**

Add `cluster_decision_id` to `reserve_entry()`, `reserve_pending()`, `open_position()`, and the Solana fill call.

- [ ] **Step 7: Run focused persistence tests**

Run: `python3 -m pytest tests/test_memecoin_cluster_ledger.py tests/test_memecoin_entry_trace.py tests/test_memecoin_capture_v2.py tests/test_memecoin_funded_contract_storage.py -q`

Expected: PASS.

- [ ] **Step 8: Commit ledger persistence**

```bash
git add sniper/persistence/db_memecoin.py sniper/services/memecoin_entry_trace.py \
  sniper/services/memecoin_capture_v2.py sniper/execution/solana_swap_executor.py \
  tests/test_memecoin_entry_trace.py tests/test_memecoin_capture_v2.py \
  tests/test_memecoin_cluster_ledger.py
git commit -m "feat(memecoin): persist cluster decisions"
```

---

### Task 5: Put the gate in the common funded Solana path

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_risk.py`
- Modify: `sniper/services/memecoin_real_leg.py`
- Modify: `sniper/services/memecoin_real_runner.py`
- Create: `tests/test_memecoin_funded_cluster_gate.py`
- Modify: `tests/test_memecoin_entry_retry_lane.py`
- Modify: `tests/test_memecoin_manual_actions.py`

**Interfaces:**
- Consumes: `ClusterGateClient.decide(mint, source_path) -> ClusterDecision`.
- Produces: one common funded Solana gate before `reserve_entry()`.

- [ ] **Step 1: Write ordering and no-bypass tests**

<!-- schema-validator: ignore-next-block -->
```python
def test_cheap_rejection_does_not_call_cluster(conn):
    calls = []
    out = funded_hook(conn, stale_event(), cluster_fn=lambda *_a, **_k: calls.append(1))
    assert out["status"] == "stale"
    assert calls == []


def test_cluster_block_has_no_funded_side_effect(conn):
    out = funded_hook(conn, valid_solana_event(), cluster_fn=blocked_cluster)
    assert out["status"] == "rejected"
    assert out["reason"] == "cluster_share_high"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_positions").fetchone()[0] == 0


def test_cluster_block_records_fresh_outcome_without_a_reservation(conn):
    prices = iter((0.001, 0.0012))
    out = funded_hook(
        conn,
        valid_solana_event(),
        stats_fn=lambda *_a, **_k: {"price": next(prices)},
        cluster_fn=blocked_cluster,
    )
    attempt = conn.execute(
        "SELECT cluster_outcome_id,cluster_outcome_status FROM memecoin_entry_attempts"
    ).fetchone()
    assert attempt[0] is not None
    assert attempt[1] == "open"
    assert conn.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 0


def test_evm_entry_does_not_call_solana_cluster(conn):
    calls = []
    funded_hook(conn, valid_evm_event(), cluster_fn=lambda *_a, **_k: calls.append(1))
    assert calls == []
```

Add `test_cluster_outage_blocks_buy_but_exit_sweep_runs`.

Add `test_cluster_429_blocks_without_reservation`.

- [ ] **Step 2: Run the tests and confirm the missing injection failure**

Run: `python3 -m pytest tests/test_memecoin_funded_cluster_gate.py -q`

Expected: FAIL because the common path has no cluster seam.

- [ ] **Step 3: Add default-off settings**

```python
MEMECOIN_REAL_CLUSTER_GATE_ENABLED = os.getenv(
    "MEMECOIN_REAL_CLUSTER_GATE_ENABLED", "0"
).strip().lower() in {"1", "true", "yes"}
MEMECOIN_CLUSTER_GATE_SOCKET = os.getenv(
    "MEMECOIN_CLUSTER_GATE_SOCKET",
    str(state_root() / "data" / "memecoin_cluster_gate.sock"),
)
```

- [ ] **Step 4: Add a read-only risk preflight**

```python
def preflight_entry(
    conn, *, mint, day, usdc_balance, sol_balance,
    kill_tripped=False, classification=None,
):
    risk_scale = classification.risk_scale if classification is not None else 1.0
    ctx = build_memecoin_risk_context(
        conn,
        usdc_balance=usdc_balance,
        sol_balance=sol_balance,
        day=day,
        kill_tripped=kill_tripped,
        risk_scale=risk_scale,
    )
    return memecoin_entry_allowed(ctx, mint=mint, trade_size=ctx.trade_size_usd)
```

The final `reserve_entry()` must repeat the risk check inside `BEGIN IMMEDIATE`. The preflight never grants a reservation.

- [ ] **Step 5: Call the cluster service in `real_entry()`**

Place the call after route, base-asset, and preflight checks. Place it before `reserve_entry()`.

```python
if route == "solana" and cfg.MEMECOIN_REAL_CLUSTER_GATE_ENABLED:
    cluster = cluster_decide_fn(ev.mint, source_path=getattr(ev, "signal_source", "global_feed"))
    persist_cluster_attempt(trace, cluster)
    _trace_mark(trace, "cluster")
    if not cluster.allowed:
        fresh = read_market_stats(ev.mint, identity.network_id)
        record_cluster_block_outcome(
            conn,
            attempt_id=trace.attempt_id,
            mint=ev.mint,
            observed_at=cluster.completed_at,
            ref_price=(fresh or {}).get("price"),
            source_user_id=getattr(ev, "source_user_id", None),
            handle=getattr(ev, "source_handle", None),
        )
        return {"status": "rejected", "reason": cluster.reason,
                "cluster_decision_id": cluster.decision_id}
```

The fresh read occurs only after a block. It cannot delay an allowed entry.

- [ ] **Step 6: Inject one client into all funded constructors**

Build one client in `make_real_hook()` and `make_real_buy_fn()`. The on-chain and retry lanes already use the returned common hook.

- [ ] **Step 7: Prove retry freshness and manual coverage**

The retry test must assert two different decision IDs for attempts more than five seconds apart. The manual test must assert a cluster block before reservation.

- [ ] **Step 8: Prove scanner paper isolation**

Run a source test that rejects any `ClusterGateClient` import under the scanner paper-arm constructors.

- [ ] **Step 9: Run focused funded-path tests**

Run: `python3 -m pytest tests/test_memecoin_funded_cluster_gate.py tests/test_memecoin_entry_retry_lane.py tests/test_memecoin_manual_actions.py tests/test_memecoin_real_runner.py tests/test_memecoin_real_leg.py -q`

Expected: PASS.

- [ ] **Step 10: Commit funded wiring**

```bash
git add sniper/settings/memecoin.py sniper/services/memecoin_risk.py \
  sniper/services/memecoin_real_leg.py sniper/services/memecoin_real_runner.py \
  tests/test_memecoin_funded_cluster_gate.py tests/test_memecoin_entry_retry_lane.py \
  tests/test_memecoin_manual_actions.py
git commit -m "feat(memecoin): gate funded Solana clusters"
```

---

### Task 6: Add pinned runtime support without activation

**Files:**
- Modify: `scripts/memecoin_watchdog.sh`
- Modify: `scripts/deploy_memecoin_runtime.sh`
- Modify: `scripts/memecoin_runtime_verify.py`
- Create: `tests/test_memecoin_cluster_runtime.py`
- Modify: `tests/test_memecoin_real_deploy.py`

**Interfaces:**
- Consumes: the merged full SHA and explicit `--activate-cluster-gate`.
- Produces: a pinned worker, verified heartbeat, and atomic default-off activation support.

- [ ] **Step 1: Write default-off and rollback tests**

```python
def test_deploy_does_not_start_cluster_worker_by_default(fake_deploy):
    result = fake_deploy.run("SHA")
    assert result.cluster_worker_started is False


def test_activation_refuses_without_healthy_cluster_worker(fake_deploy):
    result = fake_deploy.run("SHA", "--activate-cluster-gate", worker_health="stale")
    assert result.exit_code != 0
    assert result.runner_restarted is False
```

- [ ] **Step 2: Run runtime tests and confirm failure**

Run: `python3 -m pytest tests/test_memecoin_cluster_runtime.py tests/test_memecoin_real_deploy.py -q`

Expected: FAIL because activation support does not exist.

- [ ] **Step 3: Add isolated watchdog support**

When the gate is enabled, `memecoin_watchdog.sh` must start the cluster worker first. A worker failure must not stop the funded exit runner.

- [ ] **Step 4: Add explicit deploy activation**

Add `--activate-cluster-gate` to `deploy_memecoin_runtime.sh`.

The activation must perform this order:

1. Validate the exact release.
2. Verify zero pending reservations.
3. Start the pinned cluster worker.
4. Wait for a fresh worker heartbeat and socket health.
5. Write `MEMECOIN_REAL_CLUSTER_GATE_ENABLED=1` atomically.
6. Restart the funded runner.
7. Verify one runner, one worker, fresh heartbeats, and `auth_ok=true`.
8. Restore the prior environment and runtime on any failure.

- [ ] **Step 5: Extend runtime verification**

Verify the worker command points at `.runtime/memecoin/current`. Verify the socket and database live under `SNIPER_STATE_ROOT`.

- [ ] **Step 6: Run focused deployment checks**

Run: `python3 -m pytest tests/test_memecoin_cluster_runtime.py tests/test_memecoin_real_deploy.py -q`

Run: `bash -n scripts/memecoin_watchdog.sh scripts/deploy_memecoin_runtime.sh scripts/memecoin_cluster_gate_watchdog.sh`

Run: `python3 -m compileall -q sniper scripts/memecoin_cluster_gate_worker.py`

Expected: PASS.

- [ ] **Step 7: Run the final focused regression set**

Run:

```bash
python3 -m pytest \
  tests/test_memecoin_cluster_admission.py \
  tests/test_memecoin_cluster_rpc.py \
  tests/test_memecoin_cluster_store.py \
  tests/test_memecoin_cluster_service.py \
  tests/test_memecoin_cluster_gate_worker.py \
  tests/test_memecoin_cluster_ledger.py \
  tests/test_memecoin_capture_v2.py \
  tests/test_memecoin_funded_cluster_gate.py \
  tests/test_memecoin_entry_retry_lane.py \
  tests/test_memecoin_manual_actions.py \
  tests/test_memecoin_real_runner.py \
  tests/test_memecoin_real_leg.py \
  tests/test_memecoin_cluster_runtime.py \
  tests/test_memecoin_real_deploy.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit runtime support**

```bash
git add scripts/memecoin_watchdog.sh scripts/deploy_memecoin_runtime.sh \
  scripts/memecoin_runtime_verify.py tests/test_memecoin_cluster_runtime.py \
  tests/test_memecoin_real_deploy.py
git commit -m "feat(memecoin): add cluster gate activation"
```

---

## Deferred Live Activation

This plan does not activate the funded gate.

After merge, the operator must separately approve `--activate-cluster-gate`.

The live proof must show these facts:

1. The worker uses only the research RPC.
2. The worker and runner use the same pinned release.
3. The scanner process and paper counts stay unchanged.
4. A synthetic blocked request creates no reservation.
5. A clean fixture reaches the existing funded risk seam.
6. The runner exit heartbeat stays fresh during a worker outage.
7. The first organic funded attempt stores `cluster_decision_id` and `cluster_ms`.
8. The operator report separates gate latency from source, quote, and submission latency.
9. A blocked organic attempt has V2 marks, or a typed capture status explains the missing path.
