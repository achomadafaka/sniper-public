# Memecoin Free-Direct Submission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking. Do not dispatch overlapping agents: this plan crosses shared funded-path files.

**Goal:** Make one zero-configuration auto_free backend the default submission path for every funded memecoin entry and exit, using custody RPC, official public endpoints, and a keyless local Solana TPU sidecar while preserving exact signed-byte identity and crash safety.

**Architecture:** Python retains construction, signing, durable ownership, bounded fanout, audit, and recovery. Official EVM endpoints reuse the current raw-transaction provider. A manifest-bound Rust process accepts already-signed Solana wire bytes over a private Unix socket, validates their hash and first signature, and sends those exact bytes through Agave's QUIC TPU client. Optional paths degrade to custody without changing execution policy.

**Tech Stack:** Python 3.13, pytest, SQLite, pooled JSON-RPC, Rust stable, Tokio Unix sockets, Agave/Solana 4.1.2, Docker, Bash pinned-runtime deployment.

## Global Constraints

- Read AGENTS.md and docs/superpowers/specs/2026-07-25-memecoin-free-direct-submission-design.md completely before editing.
- Work in an isolated worktree based on this plan commit. Never edit the user's dirty main checkout.
- Use RED-first TDD for every behavior change. Record each failing command and reason.
- One logical commit per task. Stage explicit paths only. Never use git add -A.
- Never sign twice, mutate signed bytes, refresh a blockhash/nonce, or create a replacement transaction in submission code.
- Persist signed identity and payload hash before any network write. Every possibly-sent result remains submitted_unknown.
- auto_free is the default. pooled_rpc and rpc are rollback modes.
- Remove paid/tipped/configured fanout modes only after proving they have no remaining production caller.
- No provider account, API key, subscription, extra tip, operator endpoint, or full node.
- No mainnet test transaction. Read-only mainnet health calls and one ephemeral devnet self-transfer are authorized.
- Keep the funded kill switch present. Do not enable V5 authority, deploy, restart services, mutate production DBs, or remove the kill switch.
- Do not run the full repository suite. Run the focused gate in Task 10.
- Do not claim a speed multiplier. Report observed latency only.
- Persist and log provider labels and payload hashes only; never URLs, payloads, keys, or raw secret-bearing errors.
- Pin stable Agave v4.1.2 at commit 182084b82aae88b1b0731540f63bf7416f27773a and commit Cargo.lock. Do not use prereleases.
- Knowledge Sync is required before merge. Do not write Supabase without explicit per-invocation authority.

---

## File Map

Create:

- sniper/execution/free_endpoint_registry.py — immutable official endpoints and URL-free labels.
- sniper/execution/submission_health.py — health snapshots, quarantine, counters, sanitized status.
- sniper/execution/solana_tpu_sidecar.py — Unix-socket provider and owned sidecar process.
- sniper/services/memecoin_submission_status.py — read-only status and audit projection.
- scripts/memecoin_submission_smoke.py — read-only mainnet health and explicit devnet smoke.
- rust/solana-tpu-sidecar/Cargo.toml, Cargo.lock, Dockerfile.
- rust/solana-tpu-sidecar/src/protocol.rs, delivery.rs, main.rs.
- scripts/build_memecoin_tpu_sidecar.sh.
- focused tests named below.

Modify:

- sniper/execution/submission_transport.py
- sniper/execution/submission_backends.py
- sniper/execution/solana_fast_submit.py
- sniper/execution/evm_fast_submit.py
- sniper/settings/memecoin_submission.py
- sniper/persistence/db_memecoin.py
- sniper/services/memecoin_real_runner.py
- sniper/services/memecoin_evm_leg.py
- scripts/memecoin_runner_loop.py
- sniper/routes/api_memecoin.py
- scripts/memecoin_runtime_verify.py
- scripts/deploy_memecoin_runtime.sh
- docs/superpowers/specs/2026-07-25-memecoin-shared-fast-submission-design.md

Delete only after caller proof:

- sniper/execution/solana_fast_transaction.py
- tests/test_memecoin_solana_fast_transaction.py

Tests that only used the deleted module as a fixture factory must construct a minimal signed transaction locally. Do not retain dead production code for tests.

---

### Task 1: Final config surface and dead paid-mode removal

**Files:**
- Modify: sniper/settings/memecoin_submission.py
- Modify: sniper/execution/solana_fast_submit.py
- Modify: sniper/execution/submission_backends.py
- Delete after audit: sniper/execution/solana_fast_transaction.py
- Modify: tests/test_memecoin_submission_config.py
- Modify: tests/test_memecoin_submission_backends.py
- Modify: affected fast-submit tests

**Produces:**
- SolanaSubmitConfig(mode, deadline_ms, timeout_ms, warm_interval_s)
- EvmSubmitConfig(mode, deadline_ms, timeout_ms)
- Accepted modes: auto_free, pooled_rpc, rpc
- Missing mode: auto_free

- [ ] Prove old surfaces have no outside production caller:

~~~bash
rg -n "HeliusSenderProvider|JitoProvider|verify_tip_and_fee|build_solana_fast_tx_builder|sender_url|jito_url|fast_fanout|MEMECOIN_SOLANA_FAST|MEMECOIN_SOLANA_SENDER|MEMECOIN_SOLANA_JITO|MEMECOIN_EVM_SUBMIT_RPC_URLS|mode.*fanout" sniper scripts
~~~

Expected: hits are confined to the four files named above. If another production caller exists, replace it safely in this task before deletion.

- [ ] Add RED tests:

~~~python
def test_auto_free_is_zero_configuration_default():
    assert solana_submit_config({}).mode == "auto_free"
    assert evm_submit_config({}).mode == "auto_free"
    validate_submission_config({}, funded_evm_chains=("8453", "4663"))

@pytest.mark.parametrize("name,value", [
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "sender"),
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "jito"),
    ("MEMECOIN_SOLANA_SUBMIT_MODE", "fast_fanout"),
    ("MEMECOIN_EVM_SUBMIT_MODE", "fanout"),
])
def test_removed_modes_fail_startup(name, value):
    with pytest.raises(SubmissionConfigError):
        validate_submission_config({name: value}, funded_evm_chains=("8453",))
~~~

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_submission_config.py tests/test_memecoin_submission_backends.py -q
~~~

Expected: pooled_rpc is still default and removed modes remain accepted.

- [ ] Implement exact mode tuples:

~~~python
_SOLANA_MODES = ("auto_free", "pooled_rpc", "rpc")
_EVM_MODES = ("auto_free", "pooled_rpc", "rpc")
~~~

Delete tip, cap, Sender, Jito, arbitrary endpoint-list fields and validation. Preserve current deadline/timeout/warm validation and ordinary SolanaRpcProvider.

- [ ] Run focused GREEN and commit explicit changed paths:

~~~bash
python3 -m pytest tests/test_memecoin_submission_config.py tests/test_memecoin_submission_backends.py tests/test_memecoin_solana_fast_submit.py tests/test_memecoin_fast_submit_integration.py tests/test_memecoin_fast_submit_wiring.py tests/test_memecoin_fast_submit_crash_safety.py -q
git diff --check
git commit -m "refactor(memecoin): make free submission the only fast policy"
~~~

---

### Task 2: Four-state provider outcome

**Files:**
- Modify: sniper/execution/submission_transport.py
- Modify: sniper/persistence/db_memecoin.py
- Modify: tests/test_memecoin_submission_transport.py
- Modify: tests/test_memecoin_submission_binding.py

**Produces:**
- AttemptState.NOT_SENT = "not_sent"
- Aggregate precedence: accepted, ambiguous, any explicit rejection, otherwise ambiguous
- Executor seam remains identity, None, or False

- [ ] Write RED cases for:
  - custody explicit rejection plus optional NOT_SENT => explicit rejection
  - all NOT_SENT => ambiguous
  - pending work cancelled before worker start => NOT_SENT
  - worker started then deadline => ambiguous
  - accepted beats every other state

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_submission_transport.py tests/test_memecoin_submission_binding.py -q
~~~

- [ ] Implement this aggregate:

~~~python
if accepted:
    state = AttemptState.ACCEPTED
elif any(a.state is AttemptState.AMBIGUOUS for a in attempts):
    state = AttemptState.AMBIGUOUS
elif any(a.state is AttemptState.EXPLICIT_REJECTED for a in attempts):
    state = AttemptState.EXPLICIT_REJECTED
else:
    state = AttemptState.AMBIGUOUS
~~~

Track whether a future began. Cancellation is NOT_SENT only when execution provably never started. Update the existing table comment to include not_sent; do not add execution authority or a schema migration.

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_submission_transport.py tests/test_memecoin_submission_binding.py tests/test_memecoin_fast_submit_crash_safety.py -q
git diff --check
git commit -m "fix(memecoin): distinguish optional providers not sent"
~~~

---

### Task 3: Official endpoint registry and health

**Files:**
- Create: sniper/execution/free_endpoint_registry.py
- Create: sniper/execution/submission_health.py
- Create: tests/test_memecoin_free_endpoint_registry.py
- Create: tests/test_memecoin_submission_health.py
- Modify: sniper/settings/memecoin_submission.py

**Produces:**

~~~python
@dataclass(frozen=True)
class FreeEndpoint:
    chain_id: str
    provider_id: str
    url: str
    role: str
    health_method: str
    expected_identity: str

def free_endpoints(chain_id: str) -> tuple[FreeEndpoint, ...]: ...
def dedupe_endpoints(custody_url: str, entries: tuple[FreeEndpoint, ...]) -> tuple[FreeEndpoint, ...]: ...

class HealthRegistry:
    def should_attempt(self, provider_id: str, now: float | None = None) -> bool: ...
    def record_success(self, provider_id: str, identity: str, now: float | None = None) -> None: ...
    def record_failure(self, provider_id: str, reason: str, now: float | None = None) -> None: ...
    def record_wrong_chain(self, provider_id: str, received: str, now: float | None = None) -> None: ...
    def sanitized_snapshot(self) -> dict: ...
~~~

- [ ] Write RED registry tests pinning:
  - Solana: solana:public_rpc => https://api.mainnet-beta.solana.com
  - Base: 8453:base_standard => https://mainnet.base.org
  - Base: 8453:base_flashblocks => https://mainnet-preconf.base.org
  - Robinhood: 4663:public_rpc => https://rpc.mainnet.chain.robinhood.com
  - Robinhood: 4663:sequencer => https://sequencer.mainnet.chain.robinhood.com
  - Base identity 0x2105
  - Robinhood identity 0x1237
  - Solana genesis 5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp

- [ ] Write RED health tests proving:
  - three failures quarantine for 60 seconds
  - success recovers
  - wrong chain remains excluded until a new process registry
  - should_attempt performs no network call
  - snapshot contains no URL or raw error

- [ ] Run RED, implement fixed constants 30.0/3/60.0, then GREEN:

~~~bash
python3 -m pytest tests/test_memecoin_free_endpoint_registry.py tests/test_memecoin_submission_health.py -q
git diff --check
git commit -m "feat(memecoin): add official endpoint health registry"
~~~

Startup checks run concurrently under the existing timeout. Custody failure is fatal. Optional failure excludes and warns once without failing startup.

---

### Task 4: Official EVM direct delivery

**Files:**
- Modify: sniper/execution/evm_fast_submit.py
- Modify: sniper/execution/submission_backends.py
- Modify: sniper/services/memecoin_evm_leg.py
- Modify: tests/test_memecoin_evm_fast_submit.py
- Modify: tests/test_memecoin_submission_backends.py
- Modify: tests/test_memecoin_evm_fast_submit_integration.py

**Produces:**
- build_evm_backend(...) returns auto_free SubmissionTransport, pooled rollback, or None for rpc
- HealthGatedProvider returns NOT_SENT when excluded before any request write
- Fixed registry labels; never URLs or index-only labels

- [ ] Add RED tests proving:
  - Base builds custody + standard + Flashblocks
  - Robinhood builds custody + public + sequencer
  - normalized duplicate custody/official URLs are removed
  - quarantined optional provider receives zero calls
  - blocked optional provider cannot delay first accept
  - all providers receive byte-identical raw_hex
  - chain-id mismatch excludes before send

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_evm_fast_submit.py tests/test_memecoin_submission_backends.py tests/test_memecoin_evm_fast_submit_integration.py -q
~~~

- [ ] Implement with existing EvmRpcProvider only. Add no signing, nonce, approval, or receipt logic.

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_evm_fast_submit.py tests/test_memecoin_submission_backends.py tests/test_memecoin_evm_fast_submit_integration.py tests/test_memecoin_evm_entry_executor.py tests/test_memecoin_evm_exit_executor.py -q
git diff --check
git commit -m "feat(memecoin): fan out EVM sends to official endpoints"
~~~

---

### Task 5: Python TPU boundary and process lifecycle

**Files:**
- Create: sniper/execution/solana_tpu_sidecar.py
- Create: tests/test_memecoin_solana_tpu_sidecar.py
- Modify: sniper/execution/submission_backends.py

**Produces:**

~~~python
class TpuSidecarProvider:
    provider_id = "solana:tpu"
    def submit(self, request: SubmissionRequest) -> SubmissionAttempt: ...

class TpuSidecarProcess:
    def start(self) -> bool: ...
    def health(self) -> dict: ...
    def close(self) -> None: ...
    def sanitized_status(self) -> dict: ...
~~~

Protocol: unsigned 32-bit big-endian JSON length plus UTF-8 JSON; same response framing; maximum 2 MiB. Socket: state_root/data/memecoin-tpu-sidecar.sock, mode 0600.

- [ ] Write RED tests for absent socket, pre-write failure, partial-write ambiguity, accepted identity match, response mismatch, sidecar explicit rejection, frame bound, crash/restart, bounded idempotent close, and secret-free logs.

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_solana_tpu_sidecar.py -q
~~~

- [ ] Implement a manual send loop. Only zero bytes written may be NOT_SENT; any later exception is ambiguous. Pass RPC/WS URLs only through SNIPER_TPU_RPC_URL and SNIPER_TPU_WS_URL child environment variables. Never put them in argv.

- [ ] Register sidecar as an owned closeable in submission_backends.close_all(). Missing manifest means degraded RPC-only auto_free, not runner failure.

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_solana_tpu_sidecar.py tests/test_memecoin_submission_backends.py tests/test_memecoin_submission_transport.py -q
git diff --check
git commit -m "feat(memecoin): add keyless TPU sidecar boundary"
~~~

---

### Task 6: Rust protocol, QUIC delivery, and artifact build

**Files:**
- Create: rust/solana-tpu-sidecar/Cargo.toml
- Create: rust/solana-tpu-sidecar/Cargo.lock
- Create: rust/solana-tpu-sidecar/Dockerfile
- Create: rust/solana-tpu-sidecar/src/protocol.rs
- Create: rust/solana-tpu-sidecar/src/delivery.rs
- Create: rust/solana-tpu-sidecar/src/main.rs
- Create: scripts/build_memecoin_tpu_sidecar.sh
- Create: tests/test_memecoin_tpu_sidecar_build.py

**Produces:**
- Request fields exactly match spec section 8.2
- WireDelivery health/send interface
- AgaveDelivery with fanout_slots=5: the current slot plus four future slots
- Build script signature: build_memecoin_tpu_sidecar.sh release-root artifact-root full-sha
- Artifact: artifact-root/full-sha/memecoin-tpu-sidecar plus metadata.json

- [ ] Create Cargo.toml with exact stable dependencies:

~~~toml
[dependencies]
anyhow = "=1.0.100"
base64 = "=0.22.1"
bs58 = "=0.5.1"
serde = { version = "=1.0.228", features = ["derive"] }
serde_json = "=1.0.149"
sha2 = "=0.10.9"
solana-client = "=4.1.2"
solana-transaction = "=4.1.1"
tokio = { version = "=1.52.3", features = ["full"] }
wincode = { version = "=0.5.3", features = ["derive"] }
~~~

Generate and commit Cargo.lock.

- [ ] Write Rust RED tests for framing, malformed JSON/base64, wrong SHA, full VersionedTransaction decoding, no signature, first-signature mismatch, fake all-fail, fake one-success, 0600 socket, deadline, and no payload/RPC URL in logs.

- [ ] Resolve rust:1.91.1-bookworm to an immutable sha256 digest:

~~~bash
docker buildx imagetools inspect rust:1.91.1-bookworm --format '{{json .Manifest.Digest}}'
~~~

Put the returned digest directly in Dockerfile FROM. No floating production build image.

- [ ] Run RED Docker test stage with CARGO_BUILD_JOBS=4.

- [ ] Implement:
  - wincode full VersionedTransaction decode
  - SHA-256 over decoded wire bytes
  - bs58 first-signature comparison
  - deterministic mismatch => explicit_rejected before delivery
  - solana_client::tpu_client::TpuClient with TpuClientConfig { fanout_slots: 5 }, because Agave's
    range includes the current slot and the policy requires four additional future slots
  - try_send_wire_transaction(payload) exactly once
  - success if at least one leader write succeeds
  - errors after delivery call => ambiguous response
  - no private keys, SQLite, signing, mutation, retries with new bytes, or payload logging

- [ ] Add RED Python build tests using a fake docker executable. Prove short SHA refusal, path confinement, binary mode 0555, metadata fields, no overwrite of mismatch, and no committed binary.

- [ ] Build and test:

~~~bash
docker build --build-arg CARGO_BUILD_JOBS=4 --target test -t sniper-tpu-sidecar-test rust/solana-tpu-sidecar
docker run --rm sniper-tpu-sidecar-test
python3 -m pytest tests/test_memecoin_tpu_sidecar_build.py -q
artifact_root="$(mktemp -d)"
scripts/build_memecoin_tpu_sidecar.sh "$PWD" "$artifact_root" "$(git rev-parse HEAD)"
git diff --check
git commit -m "feat(memecoin): deliver signed Solana bytes over TPU QUIC"
~~~

Do not commit artifact_root.

---

### Task 7: Universal auto_free wiring

**Files:**
- Modify: sniper/execution/submission_backends.py
- Modify: sniper/services/memecoin_real_runner.py
- Modify: sniper/services/memecoin_evm_leg.py
- Modify: scripts/memecoin_runner_loop.py
- Modify: focused integration tests

**Final topology:**
- Solana: solana:custody_rpc, solana:public_rpc, healthy solana:tpu
- Base: 8453:custody_rpc, 8453:base_standard, 8453:base_flashblocks
- Robinhood: 4663:custody_rpc, 4663:public_rpc, 4663:sequencer

- [ ] Write RED production-shaped tests for Solana entry/full exit/partial exit and Base/4663 entry/approval/pre-transaction/full exit/partial exit. Assert exact already-bound payload/hash, first accept, and unchanged executor policy.

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_submission_backends.py tests/test_memecoin_fast_submit_integration.py tests/test_memecoin_evm_fast_submit_integration.py tests/test_memecoin_fast_submit_wiring.py tests/test_memecoin_multi_venue.py -q
~~~

- [ ] Wire auto_free. Do not restore a fast transaction builder: the existing executor signs once, binds ownership, and sends those bytes unchanged. Preserve pooled_rpc and rpc behavior byte-for-byte.

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_submission_backends.py tests/test_memecoin_fast_submit_integration.py tests/test_memecoin_evm_fast_submit_integration.py tests/test_memecoin_fast_submit_wiring.py tests/test_memecoin_multi_venue.py tests/test_memecoin_real_runner.py -q
git diff --check
git commit -m "feat(memecoin): make auto free submission universal"
~~~

---

### Task 8: Sanitized audit and cockpit status

**Files:**
- Create: sniper/services/memecoin_submission_status.py
- Modify: sniper/execution/submission_health.py
- Modify: sniper/routes/api_memecoin.py
- Modify: sniper/persistence/db_memecoin.py
- Create: tests/test_memecoin_submission_status.py
- Modify: tests/test_memecoin_submission_binding.py

**Produces:**
- state_root/data/memecoin_submission_health.json, mode 0600, observability only
- submission_status(conn, state_root) with health and attempt p50/p95/counts
- submission object under /api/memecoin/real/status; no RPC on API polling

- [ ] Write RED tests for active/excluded/quarantined, last health age, last attempt state/latency, counts, rate limits, TPU leader/restart counts, URL/payload/error redaction, audit failure independence, and zero API network calls.

- [ ] Implement atomic same-directory temp write, fsync, chmod 0600, os.replace. Status failure warns only. Query existing memecoin_submission_attempts; add no DB or authority.

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_submission_status.py tests/test_memecoin_submission_binding.py tests/test_engine_cockpit.py -q
git diff --check
git commit -m "feat(memecoin): expose sanitized submission health"
~~~

---

### Task 9: Pinned artifact deployment and smoke

**Files:**
- Modify: scripts/memecoin_runtime_verify.py
- Modify: scripts/deploy_memecoin_runtime.sh
- Create: scripts/memecoin_submission_smoke.py
- Modify: tests/test_memecoin_runtime_verify.py
- Modify: tests/test_memecoin_runtime_deploy.py
- Create: tests/test_memecoin_submission_smoke.py

**Manifest sidecar object:**

~~~json
{
  "path": "/absolute/runtime/artifacts/full-sha/memecoin-tpu-sidecar",
  "sha256": "64 lowercase hex",
  "source_sha": "40 lowercase hex",
  "cargo_lock_sha256": "64 lowercase hex",
  "target": "x86_64-unknown-linux-gnu",
  "build_image_digest": "sha256:..."
}
~~~

- [ ] Add RED verifier/deploy tests for missing metadata, wrong source SHA/hash/lock hash, symlink, path outside artifacts/full-sha, group/world-writable binary, build failure, verifier failure, and post-switch rollback. Prove build/test happens before old runner stop. Keep rollover rollback tests green.

- [ ] Run RED:

~~~bash
python3 -m pytest tests/test_memecoin_runtime_verify.py tests/test_memecoin_runtime_deploy.py tests/test_memecoin_submission_smoke.py -q
~~~

- [ ] Build after release materialization and before verification or process stop. Load metadata into candidate manifest. Verifier recomputes binary and lock hashes and checks artifact path/mode itself.

- [ ] Implement smoke commands:
  - health: read-only Base/Robinhood eth_chainId, Solana getHealth/genesis, sidecar no-send health
  - devnet: temporary keypair, faucet funds, one signed self-transfer, same bytes through devnet RPC and sidecar, one confirmed signature and one balance effect, key deletion
  - refuse devnet mode if any endpoint identifies mainnet
  - never accept a funded key path

- [ ] Run GREEN and commit:

~~~bash
python3 -m pytest tests/test_memecoin_runtime_verify.py tests/test_memecoin_runtime_deploy.py tests/test_memecoin_submission_smoke.py -q
git diff --check
git commit -m "feat(memecoin): bind TPU sidecar to pinned deployments"
~~~

---

### Task 10: Crash matrix, network proof, final audit, and handoff

**Files:**
- Modify: tests/test_memecoin_fast_submit_crash_safety.py
- Modify: docs/superpowers/specs/2026-07-25-memecoin-shared-fast-submission-design.md
- Modify AGENTS.md or CLAUDE.md only if enduring operating doctrine changed

- [ ] Add RED crash cases:
  - crash after custody write
  - crash after public EVM/sequencer write
  - partial local socket write
  - TPU successful send before reply
  - audit flush failure
  - sidecar crash during request
  - runner restart with prior signed owner

Every possibly-sent case reconstructs submitted_unknown, never releases/reclaims, and never calls a signer/provider with new bytes.

- [ ] Run the focused gate:

~~~bash
python3 -m pytest \
  tests/test_memecoin_submission_transport.py \
  tests/test_memecoin_submission_binding.py \
  tests/test_memecoin_submission_config.py \
  tests/test_memecoin_submission_backends.py \
  tests/test_memecoin_free_endpoint_registry.py \
  tests/test_memecoin_submission_health.py \
  tests/test_memecoin_solana_fast_submit.py \
  tests/test_memecoin_solana_tpu_sidecar.py \
  tests/test_memecoin_tpu_sidecar_build.py \
  tests/test_memecoin_evm_fast_submit.py \
  tests/test_memecoin_fast_submit_wiring.py \
  tests/test_memecoin_fast_submit_integration.py \
  tests/test_memecoin_evm_fast_submit_integration.py \
  tests/test_memecoin_fast_submit_crash_safety.py \
  tests/test_memecoin_entry_executor.py \
  tests/test_memecoin_exit_executor.py \
  tests/test_memecoin_evm_entry_executor.py \
  tests/test_memecoin_evm_exit_executor.py \
  tests/test_memecoin_multi_venue.py \
  tests/test_memecoin_real_runner.py \
  tests/test_memecoin_runtime_verify.py \
  tests/test_memecoin_runtime_deploy.py \
  tests/test_memecoin_submission_status.py \
  tests/test_memecoin_submission_smoke.py -q
python3 scripts/validate_schema_refs.py
python3 -m compileall -q sniper scripts/memecoin_runner_loop.py scripts/memecoin_runtime_verify.py scripts/memecoin_submission_smoke.py
docker build --build-arg CARGO_BUILD_JOBS=4 --target test -t sniper-tpu-sidecar-test rust/solana-tpu-sidecar
docker run --rm sniper-tpu-sidecar-test
git diff --check
~~~

If a named Solana entry/exit executor test is absent, locate its exact equivalent with rg --files tests and include it; do not silently drop entry or exit coverage.

- [ ] Run authorized network proof:

~~~bash
python3 scripts/memecoin_submission_smoke.py health
python3 scripts/memecoin_submission_smoke.py devnet
~~~

Required evidence: Base 0x2105, Robinhood 0x1237, correct Solana genesis, TPU no-send health, one devnet signature, one balance effect, identical RPC/TPU payload hash. No funded key or mainnet send.

- [ ] Re-audit all send seams and removed modes:

~~~bash
rg -n "eth_sendRawTransaction|sendTransaction|send_raw|send_fn|submission_transport" sniper/execution sniper/services
rg -n "HeliusSenderProvider|JitoProvider|fast_fanout|MEMECOIN_SOLANA_SENDER|MEMECOIN_SOLANA_JITO|MEMECOIN_EVM_SUBMIT_RPC_URLS|\"fanout\"" sniper scripts tests
~~~

Expected: every funded send is auto_free by default or explicit rpc rollback; paid modes have zero production/config hits.

- [ ] Mark the old shared-fast provider-selection text superseded and link the free-direct spec.

- [ ] Prepare Knowledge Sync text:

~~~text
Knowledge delta: new architecture and workflow. The free-direct submission spec supersedes the paid-mode provider-selection sections of the shared-fast submission spec. Supabase memory upsert is pending explicit per-invocation authorization; no external knowledge store was mutated by the builder.
~~~

- [ ] Commit explicit changed paths. Then fetch origin/main and merge it normally if it advanced. Do not rebase, squash, amend, merge to main, push, deploy, restart, or activate. Rerun the entire focused gate after the merge.

## Required Final Handoff

Return only after the feature branch is clean and include:

- worktree, branch, base SHA, head SHA, ahead/behind versus origin/main
- one-line purpose for every commit
- exact files changed/deleted
- each RED failure and final GREEN command/count
- binary SHA, Cargo lock SHA, image digest, target
- read-only mainnet health results and devnet signature/payload hash
- proof every funded entry/exit/approval/pre-transaction uses auto_free by default
- proof paid modes have no remaining production caller
- proof crash-after-send stays submitted_unknown and cannot double-act
- explicit: no mainnet transaction, funded key use, production DB mutation, service restart, deployment, flag change, or kill-switch removal
- clean git status and git diff --check
- real residual risks only; no unverified perfect or speed-multiplier claim
