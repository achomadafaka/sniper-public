# Memecoin free-direct submission backend (2026-07-25)

Status: **approved design; not implemented or activated**.

> 2026-10-01 exception (operator YES via Alpha), for one test only: the slot lane's Helius Sender
> SWQoS-only A/B, `docs/superpowers/specs/2026-10-01-memecoin-slot-lane-sender-ab-design.md`. The
> paid submit modes still fail startup; everything else here stands.

## 1. Goal

Make every funded memecoin send use the fastest safe submission paths available without a paid
provider, provider account, API key, operator-supplied endpoint, or full node:

```text
one exact action
-> one signed transaction identity persisted before network I/O
-> concurrent delivery of those exact bytes
-> first matching submission receipt returns
-> chain confirmation and balance reconciliation remain authoritative
```

This applies uniformly to:

- Solana entry, full exit, and partial exit;
- Base and Robinhood Chain entry, approvals, pre-transactions, full exit, and partial exit.

Normal chain gas and transaction fees are unavoidable. This design adds no subscription, API
credit, provider charge, Jito tip, Helius tip, or operator-managed infrastructure.

## 2. Current baseline

The implemented shared backend already provides:

- process-lifetime pooled HTTP connections;
- bounded same-signed-bytes fanout;
- first-matching-accept return;
- typed accepted / ambiguous / explicit-rejection outcomes;
- signed identity persistence before broadcast;
- `submitted_unknown` recovery that never blindly replaces an uncertain transaction;
- one backend shared by entry, full exit, partial exit, EVM approvals, and EVM pre-transactions;
- append-only per-provider attempt telemetry.

The runtime default is currently `pooled_rpc`: one warm custody RPC per chain. Helius Sender, Jito,
and arbitrary EVM fanout are implemented but require configuration. They are not part of this design.

## 3. Decisions

### 3.1 One production policy

Add `auto_free` and make it the only normal production submission policy. Keep only two explicit
rollback policies:

- `pooled_rpc`: current one-provider pooled backend;
- `rpc`: pre-shared-backend legacy seam.

Remove `sender`, `jito`, `fast_fanout`, and arbitrary configured EVM `fanout` from accepted runtime
configuration. Remove their provider/build code if the post-change caller audit finds no remaining
production or recovery caller. Tests and documentation must not imply that a dormant paid path is
part of the live system.

No submit-mode variable is required. Missing mode means `auto_free`.

### 3.2 Free paths only

`auto_free` uses only:

- the already-configured custody RPC;
- chain-owner public endpoints listed in section 5;
- a local Solana TPU sidecar built from pinned open-source dependencies.

No provider signup, key, token, dashboard, or billing account is allowed.

### 3.3 Tips are excluded

Helius Sender and Jito are intentionally excluded from automatic operation. Their public services
do not require a subscription, but their fast paths require an extra on-chain tip. The user's
constraint is no additional paid service or tip, so the transaction bytes must not be modified to
add either.

Existing chain-mandated gas, base fees, and any pre-existing policy-controlled priority fee remain
unchanged. Submission transport never changes economic transaction fields.

### 3.4 No full nodes

Do not run a Base, Robinhood, Solana RPC, or validator node. The host has 30 GiB RAM and 142 GiB free
disk; Robinhood documents 64 GiB RAM minimum and several terabytes of local NVMe for a full node.
Running a node would be slower to ship, operationally heavier, and outside the zero-maintenance goal.

## 4. Invariants

These are load-bearing and apply to every provider.

1. **Sign once.** Every provider receives byte-identical signed payload bytes.
2. **Bind before network.** Reservation or `ExitIntent` owns the transaction identity, payload hash,
   and validity window before any provider can receive bytes.
3. **No provider signs.** Public endpoints and the TPU sidecar can only deliver existing bytes.
4. **Identity match.** A receipt accepts only the locally derived signature/hash. Missing or
   mismatched identity is ambiguous.
5. **No blind replacement.** A possible network write becomes `submitted_unknown`; recovery checks
   chain status and balance before any new transaction can exist.
6. **First accept returns.** Slow public endpoints cannot delay a faster custody, sequencer, or TPU
   receipt.
7. **Secondary paths cannot block startup.** Wrong-chain or unavailable free endpoints are excluded
   and alerted; custody RPC remains required.
8. **No silent downgrade.** Runtime status exposes `auto_free`, active provider labels, excluded
   labels, and sidecar health without exposing URLs or credentials.
9. **No execution-policy change.** Entry selection, V5 authority, sizing, exact quote, post-peak,
   claims, slippage, confirmation, and finalization behavior are unchanged.
10. **No DB handles on provider threads.** Attempt audit flush stays on the caller thread.

## 5. Provider topology

### 5.1 Solana

Every signed Solana action is offered concurrently to:

| Provider label | Path | Role |
|---|---|---|
| `solana:custody_rpc` | existing custody RPC URL | required warm baseline |
| `solana:public_rpc` | `https://api.mainnet-beta.solana.com` | best-effort public backup |
| `solana:tpu` | local Unix socket -> TPU QUIC sidecar | direct upcoming-leader delivery |

The public RPC is rate-limited and never treated as a production SLA. Its failure cannot block or
slow the other paths.

The TPU sidecar uses the keyless public RPC/WebSocket by default (isolated read-source exception
below), never custody credentials, only for cluster nodes, leader schedule,
recent slot, and connection maintenance. It delivers the already-signed wire transaction directly
to the current and next leaders through the official Solana TPU client implementation.

### 5.2 Base (`8453`)

Every raw signed EVM action is offered concurrently to a de-duplicated list:

| Provider label | Endpoint |
|---|---|
| `8453:custody_rpc` | existing custody RPC URL |
| `8453:base_standard` | `https://mainnet.base.org` |
| `8453:base_flashblocks` | `https://mainnet-preconf.base.org` |

Base documents both endpoints as rate-limited. They are best-effort additional front doors, not a
replacement for custody. Both expose standard `eth_*` methods, including `eth_sendRawTransaction`.

### 5.3 Robinhood Chain (`4663`)

Every raw signed EVM action is offered concurrently to a de-duplicated list:

| Provider label | Endpoint |
|---|---|
| `4663:custody_rpc` | existing custody RPC URL |
| `4663:public_rpc` | `https://rpc.mainnet.chain.robinhood.com` |
| `4663:sequencer` | `https://sequencer.mainnet.chain.robinhood.com` |

Robinhood documents first-come, first-served sequencing, so the direct sequencer path is the
highest-value free EVM addition. The public RPC is rate-limited and remains best effort.

### 5.4 Endpoint registry

Official endpoints live in one immutable, chain-keyed registry in code. Each entry carries:

- chain ID;
- URL;
- redacted provider label;
- role (`public_rpc`, `preconfirm_rpc`, or `sequencer`);
- expected JSON-RPC chain identity;
- health method and interval.

Do not read endpoint lists from mutable remote documents. Do not accept operator-supplied free
endpoint lists. A code review and pinned deploy are required to change this registry.

Custody and official URLs are normalized and de-duplicated before provider construction. Provider
labels, never URLs, are persisted or logged.

## 6. Internal outcome model

Provider attempts gain one internal state:

```text
NOT_SENT
```

This means the provider proved that no transaction bytes crossed its boundary: excluded by a
wrong-chain health check, quarantined before submit, sidecar unavailable before the local socket
write, or another provider-local unavailability detected before payload handling. A deterministic
payload-hash or transaction-identity mismatch is `EXPLICIT_REJECTED`, not `NOT_SENT`.

The executor-facing seam remains tri-state:

```text
any ACCEPTED                           -> tx identity
else any AMBIGUOUS                     -> None / submitted_unknown
else any EXPLICIT_REJECTED             -> False / safe release path
else (all NOT_SENT or zero providers)  -> None / submitted_unknown
```

`NOT_SENT` is neutral when at least one real provider attempted the transaction. It prevents an
unhealthy optional provider from creating false ambiguity while still refusing to fabricate a safe
rejection when nobody attempted delivery.

Examples:

| Attempts | Aggregate |
|---|---|
| custody accepted; TPU still running | accepted |
| sequencer timed out after write; custody rejected | ambiguous |
| custody intrinsically rejected; public endpoint quarantined before send | explicit rejected |
| every provider excluded before send | ambiguous |
| TPU socket accepted bytes; sidecar reply lost | ambiguous |
| TPU delivered to one leader and returned matching identity | accepted |

## 7. Endpoint health and quarantine

Health checks never sign or submit.

### 7.1 Startup checks

- EVM endpoints must return the exact configured `eth_chainId`.
- Solana public RPC must answer `getHealth` and `getGenesisHash`; the genesis hash must match the
  mainnet-beta value registered in code.
- TPU sidecar must resolve the current slot, current leader, and at least one leader TPU QUIC
  address without sending transaction bytes.
- Custody validation failure blocks funded context construction.
- Free secondary failure excludes that provider and emits one bounded warning; startup continues.

### 7.2 Runtime checks

- Custody connection warming retains its current interval.
- Free public endpoints are checked every 30 seconds to avoid creating their own rate-limit load.
- Three consecutive failures quarantine a free endpoint for 60 seconds.
- A successful identity check ends quarantine.
- Wrong-chain identity quarantines until process restart and emits a high-severity alert.
- An action never waits for a health check; it uses the last completed health snapshot.
- The TPU sidecar maintains its own leader connections continuously and publishes health through
  the local socket.

Health state is process-local observability. It never becomes execution authority and is fully
reconstructed after restart.

## 8. Solana TPU sidecar

### 8.1 Boundary

Create a small Rust binary with one responsibility: deliver signed Solana wire transactions to
upcoming leaders over QUIC.

It must not:

- read a private key or seed phrase;
- construct, sign, mutate, retry with a new blockhash, or re-price a transaction;
- open SQLite;
- call Jupiter;
- persist payload bytes;
- log payloads, wallet addresses, RPC URLs, or validator addresses.

### 8.2 Local protocol

The runner owns the sidecar process. Communication uses a Unix domain socket under the state root,
mode `0600`, with length-prefixed JSON messages.

Request fields:

```text
request_id
tx_identity
signed_payload_base64
signed_payload_sha256
last_valid_block_height
```

Response fields:

```text
request_id
state = accepted | ambiguous | explicit_rejected | not_sent
tx_identity
leader_count
reason_code
latency_ms
```

The sidecar independently decodes the transaction, verifies payload SHA-256, and verifies that the
first transaction signature equals `tx_identity`. Any mismatch is `explicit_rejected` before
network I/O. A successful QUIC write to at least one leader is `accepted`; zero completed writes
after bytes may have entered a socket is `ambiguous`.

### 8.3 Leader delivery

- Use the official Agave/Solana TPU client crates with one mutually compatible version set pinned
  exactly in `Cargo.lock`.
- Use QUIC, not deprecated UDP TPU ports.
- Target the current leader and four upcoming leader slots.
- Reuse leader connections for the sidecar lifetime.
- A single request is handed to the TPU client once. The sidecar does not create an independent
  unbounded retry loop.
- Sidecar request deadline is shorter than the outer `SubmissionTransport` deadline.

### 8.4 Build and release

No system-wide Rust install is required.

- Build in a pinned Docker Rust image with a checked-in lockfile and Dockerfile.
- Limit Cargo build parallelism to four jobs because host swap is already saturated.
- Copy only the stripped release binary out of the build container.
- Store the artifact under `.runtime/memecoin/artifacts/<git-sha>/`.
- Record binary SHA-256, source Git SHA, Cargo lock hash, target triple, and build image digest in
  the deployment manifest.
- `memecoin_runtime_verify.py` must reject a missing, mutable, wrong-hash, or wrong-SHA sidecar.
- Build and self-test the artifact before stopping the old runner.
- Rollback restores the previous release and its previous sidecar artifact atomically.

Docker is a build-time dependency only. The funded runtime never needs Docker or Cargo.

### 8.5 Lifecycle

- Runner starts the exact manifest-bound sidecar before constructing the Solana funded context.
- Sidecar receives the existing custody RPC URL through its environment; the URL is never placed on
  its command line or in status output.
- Runner waits for a bounded no-send health response.
- A missing or manifest-invalid artifact blocks deployment. If a valid artifact later fails its
  runtime health check, `auto_free` continues with custody and public RPC and reports degraded
  status.
- Sidecar crash cannot kill the runner. The runner restarts it with a bounded backoff while other
  providers continue.
- Normal runner shutdown closes the local socket and terminates the owned sidecar.

## 9. EVM direct delivery

Reuse `EvmRpcProvider`; do not create chain-specific signing or nonce code.

- The exact raw transaction and hash are frozen before fanout.
- Provider workers call only `eth_sendRawTransaction`.
- The first returned hash matching the local hash is accepted.
- `already known` / `already imported` is accepted for the same local hash.
- Nonce, fee, balance, timeout, transport, HTTP, rate-limit, and unclassified errors remain
  ambiguous because another path may already have accepted the bytes.
- Only transaction-intrinsic failures remain explicit rejection.
- Direct sequencer and Flashblocks endpoints receive no special trust in confirmation; normal chain
  receipt and balance reconciliation stay authoritative.

## 10. Runtime status and audit

Startup emits one URL-free line:

```text
submit=auto_free solana=[custody_rpc,public_rpc,tpu]
8453=[custody_rpc,base_standard,base_flashblocks]
4663=[custody_rpc,public_rpc,sequencer]
excluded=[]
```

The existing `memecoin_submission_attempts` table remains the attempt ledger. Persist:

- provider label;
- `accepted`, `ambiguous`, `explicit_rejected`, or `not_sent`;
- latency;
- response/reason code;
- the existing action owner and signed-payload hash.

Do not add a new analytics database or let audit failure alter execution outcome.

Cockpit/status output reports, per chain and provider label:

- active/excluded/quarantined;
- last health age;
- last attempt state and latency;
- rolling p50/p95 submission latency once observations exist;
- accepted count;
- ambiguous count;
- rate-limit count;
- sidecar leader count and restart count.

No endpoint URL, signed payload, API key, wallet secret, or raw error string may be exposed.

## 11. Configuration

Accepted modes after implementation:

```text
MEMECOIN_SOLANA_SUBMIT_MODE = auto_free | pooled_rpc | rpc
MEMECOIN_EVM_SUBMIT_MODE    = auto_free | pooled_rpc | rpc
```

Defaults are `auto_free`. No other new setting is required.

Fixed safety constants live in `sniper/settings/memecoin_submission.py` and are validated:

```text
free endpoint health interval = 30 s
free endpoint failure threshold = 3
free endpoint quarantine = 60 s
TPU future leader slots = 4
TPU local socket mode = 0600
```

Existing bounded per-chain submission deadlines remain authoritative unless production telemetry
later justifies a separately reviewed change.

## 12. Testing and proof

### 12.1 Python RED-first tests

Cover:

- `auto_free` defaults with zero new environment variables;
- exact endpoint registry and chain identity validation;
- custody URL de-duplication;
- free endpoint health, quarantine, recovery, and wrong-chain exclusion;
- `NOT_SENT` aggregation cases;
- byte-identical concurrent delivery;
- first matching accept returns without waiting for slow providers;
- Base standard + Flashblocks provider construction;
- Robinhood public RPC + sequencer construction;
- every entry/exit/approval/pre-transaction seam uses the shared backend;
- attempt audit and URL/secret redaction;
- sidecar absent, pre-write failure, post-write ambiguity, matching accept, identity mismatch, crash,
  and restart;
- crash-after-provider-write remains `submitted_unknown` and cannot be reclaimed or double-sold;
- rollback modes remain byte-identical to their pre-change behavior.

### 12.2 Rust tests

Cover:

- framed local protocol;
- payload hash and signature identity validation;
- no-write deterministic rejection;
- one-or-more leader write acceptance;
- partial write / lost acknowledgement ambiguity;
- request deadline;
- no payload in logs;
- clean shutdown and socket permissions.

Use fake QUIC/leader interfaces for unit tests. Tests must not require mainnet.

### 12.3 Production-shaped integration

Under a temporary state root and fake providers, exercise real Solana and EVM entry, full-exit,
partial-exit, approval, and pre-transaction executors. Preserve the existing crash-window suite and
extend it; do not replace it with transport-only tests.

### 12.4 Network smoke

Before funded deployment:

1. Run read-only `eth_chainId` checks against Base and Robinhood official endpoints.
2. Run Solana public `getHealth` + genesis identity checks.
3. Start the TPU sidecar against devnet, generate an ephemeral devnet-only keypair, obtain faucet
   funds, and submit one self-transfer through TPU plus RPC using the same signed bytes.
4. Confirm one signature and prove only one balance effect.
5. Destroy the ephemeral devnet key material.

Never use the funded wallet for the smoke. Never submit a mainnet test transaction.

### 12.5 Focused verification

Run the existing shared-submission, entry, exit, recovery, funded-runner, and multi-venue test files,
plus all new free-direct tests; then schema validation, Python compile, Rust format/lint/test, binary
self-test, and `git diff --check`. Do not run the unrelated full repository suite.

## 13. Deployment

1. Merge and push one reviewed SHA to `origin/main`.
2. Confirm the funded kill switch is present.
3. Confirm zero unresolved reservations and reconcile funded contracts.
4. Build and verify the SHA-bound sidecar artifact before stopping the runner.
5. Take the normal consistent SQLite backup.
6. Deploy through `scripts/deploy_memecoin_runtime.sh <sha> --restart`.
7. Verify pinned SHA, manifest, binary hash, one runner, fresh authenticated heartbeat, and
   `submit=auto_free` provider status.
8. Run only no-send endpoint and sidecar health probes on mainnet.
9. Leave the kill switch present. Removing it remains a separate explicit funded action.

Rollback sets both modes to `pooled_rpc` and restarts the same pinned release, or restores the prior
release through the existing atomic deployment rollback.

## 14. Failure matrix

| Failure | Required behavior |
|---|---|
| custody accepts first | return accepted immediately |
| direct sequencer accepts first | return accepted immediately |
| TPU delivers first | return accepted immediately |
| public endpoint returns 429 | ambiguous for that attempt; other paths continue; health may quarantine later |
| free endpoint wrong chain | exclude before send; alert; `NOT_SENT` |
| sidecar unavailable before payload write | `NOT_SENT`; RPC paths continue |
| sidecar socket write completed, reply lost | ambiguous |
| sidecar returns different identity | ambiguous and high-severity alert |
| every optional provider excluded | custody path still operates |
| every provider unavailable before send | aggregate ambiguous; never fabricate rejection |
| one provider possibly accepted, all others reject | aggregate ambiguous unless a matching accept arrives |
| runner crashes after any send | signed owner remains; recovery checks chain; no replacement |
| attempt-audit write fails | execution result unchanged; local DB transaction rolled back |
| sidecar artifact mismatch | deployment refused before switch |

## 15. Explicit non-goals

- No paid provider, provider account, API key, sales contact, or operator-supplied endpoint.
- No Helius Sender, Jito, bundle auction, or extra tip.
- No full node or validator.
- No pool-family-specific Solana instruction builder; that is the next independent latency project.
- No quote, route, entry-signal, V5, sizing, or sell-policy changes.
- No mainnet test order.
- No new re-sign, replacement, or automatic same-action transaction creation.
- No claim that public endpoints provide production SLAs.
- No claim of a speed multiplier before funded attempt telemetry exists.

## 16. Acceptance

The work is complete only when:

- `auto_free` needs no new environment key;
- all funded send types use it by default;
- official free endpoints are identity-checked, bounded, and non-blocking;
- the TPU sidecar is manifest-bound, keyless, locally isolated, and independently tested;
- identical signed bytes are proven across all providers;
- crash-after-send cannot release, reclaim, re-sign, double-enter, or double-sell;
- read-only mainnet health checks pass;
- one devnet TPU same-bytes smoke confirms one balance effect;
- the exact merged release deploys under the kill switch and reports healthy `auto_free` status;
- no paid service, provider account, secret, tip, or provider-related user action was introduced.

## 17. Authoritative external references

- Solana transaction delivery and TPU client behavior:
  `https://solana.com/id/developers/guides/advanced/retry`
- Agave implementation and build requirements: `https://github.com/anza-xyz/agave`
- Base official endpoints and Flashblocks RPC behavior:
  `https://docs.base.org/base-chain/api-reference/rpc-overview`
- Base public-endpoint limitations: `https://docs.base.org/base-chain/quickstart/connecting-to-base`
- Robinhood official public RPC, sequencer, and feed endpoints:
  `https://docs.robinhood.com/chain/connecting/`
- Robinhood first-come-first-served sequencing: `https://docs.robinhood.com/chain/`
- Robinhood full-node requirements: `https://docs.robinhood.com/chain/run-a-full-node/`
- Helius Sender mandatory tip/priority-fee contract, explaining exclusion:
  `https://www.helius.dev/docs/sending-transactions/sender`
- Jito public limits and minimum tip, explaining exclusion:
  `https://docs.jito.wtf/lowlatencytxnsend/`

### Isolated TPU read-source exception (2026-10-11)

`MEMECOIN_TPU_RPC_URL` may select a separate same-provider/region HTTP+WS read credential and capacity after the manifest-bound health-only control verifies endpoint-reported mainnet genesis, SDK schedule bootstrap, cached leaders and clock advance. Never reuse custody/watch credentials or quota. Rust derives WS through the existing SDK; no separate WS setting is required. Genesis does not independently verify bank/epoch/schedule correctness; organic native-head evidence must establish the two-slot relation. Isolated errors, 429, shared capacity or a frozen schedule block activation. Tape and other public-fallback policies remain unchanged; this grants no paid submission, tips or funded authority.

### Shyft sole-funded amendment (2026-10-12)

The operator retired research and authorized a shared sole-funded Shyft account. Under the explicit Shyft profile, Solana `auto_free` constructs only the configured authenticated Europe custody RPC and manifest-bound direct TPU; public RPC, Helius, Sender and Nozomi are refused. TPU/proof/tape reads use configured Europe Shyft roles within the account-wide quota. This condition supersedes the isolated-read exception and public-source requirements above; legacy Solana and EVM behavior remain unchanged. Shared quota is not provider-enforced isolation. Nine steady gRPC streams and phased watcher handover must fit the purchased ten-connection limit. Preserve signed identity, ambiguous-send claims and existing no-tip transport. Deployment uses `--defer-backup`; no database backup without an explicit request.

The migration specification governs exact caller inventory, capabilities, role acknowledgements and rollback: `docs/superpowers/specs/2026-10-11-shyft-yellowstone-migration-design.md`. Successful same-slot acquisition and <=200 ms execution remain separate organic evidence requirements.

### Closed TPU connection repair (2026-10-20)

The pinned Agave 4.1.2 QUIC client is vendored as one crate to expose background connection refresh. Each warm tick rechecks retained wanted peers. Snapshot and installation use try-locks; the handshake runs outside the funded sender's client mutex. Installation requires the same cached connection identity and close epoch, so a concurrent send or retirement wins and only the unpublished replacement may be closed. Busy refreshes skip without waiting; failures retain bounded backoff and clear the warm flag. No transaction, probe payload, entry-path network call or new send retry is added.

Ready means the local QUIC connection has no observed close reason. It does not guarantee validator inclusion or prevent a later remote eviction. Existing byte identity, slot limits, retirement rules, provider policy and ambiguous-send ownership remain authoritative.
