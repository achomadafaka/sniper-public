# EVM execution generation: a write-ahead intent fence across processes

Status: design, revision 2 (2026-10-07). Operator decision 2026-10-07 (to beta): automatic, faster
than 60 s, W = 15 s. Build when Alpha queues it.
Author: beta. Source finding: Codex night review 2026-10-06, EVM-08 partial (report section 3.3).

**Revision 2** answers the two acceptance defects in Codex's follow-up review (2026-10-07, report
section 6) before any build:

- (a) One `.pending` mtime cannot represent concurrent outstanding intents. A writes at 10, B at 11,
  A completes and stamps `.gen` at 12; the reader sees pending 11 ≤ gen 12 and admits while B is
  still outstanding. Fixed by one intent record per send, removed only by that send's own
  completion (section 2).
- (b) Custody `pending == latest` does not prove that separately held signed bytes cannot execute.
  The BSC lane sends a private BlockRazor bundle and public copies; an HTTP timeout leaves both
  ambiguous. Fixed by resolving only on nonce consumption (chain truth) or, for a bundle-only
  identity, a provider-bound expiry written into the bundle (section 6). Revision 1's
  "B1: nonce equality" is withdrawn.

Operator-visible consequence of (b): after a crash between an intent and its send, or a public send
that never mines, the lanes on that chain stay off until that nonce is consumed (the ordinary
executor's next send on the chain does it) or the operator clears it. Revision 1 would have
auto-cleared these after W; Codex showed that clear could be wrong.

## 1. Problem

### 2026-10-10 amendment: ordinary BSC snapshot signing

Eligible quote-backed BSC entries now reuse the LIVE lane's local pending nonce under the
existing wallet lock and shared nonce floor. They call the same `lane_generation_fn` before
claim and after the required gas read; fault, outstanding intent or snapshot age/generation
mismatch falls back to the ordinary pending-count read. This adds a reader at shared signing
but changes no intent writer, resolution criterion or signed/ambiguous-send recovery. See
`2026-10-02-memecoin-bsc-56-fast-lane-design.md` for eligibility and unsigned-claim cleanup.

The 4663 and BSC fast lanes sign from a research-host snapshot (balance, nonce, gas) with no read on
the fill path. They trust a snapshot only when it was read after the wallet's execution generation:

    generation = max(mtime(real_exec_evm-<chain>-<addr>.gen), mtime(real_exec_evm-chain-<chain>.gen))
    refuse "snapshot_predates_execution" unless snapshot.read_at > generation + margin (1 s)

(`lane_generation_fn` and `inputs_reason` in `sniper/services/memecoin_rh_fast_lane.py`.)

Every broadcast advances the chain stamp afterwards (`exec_lock.stamp_send`, in a `finally`). When a
stamp write fails, the writer latches `exec_lock._GENERATION_FAULT`, a `threading.Event`. That latch
is local to the writer's own process. Two failure shapes leave another process trusting a snapshot
taken before an unrecorded send:

1. **Both completion stamps fail.** A separate writer process (an operator script, or another
   runner) sends a transaction, and its chain stamp and wallet stamp both raise (EIO, EACCES, a full
   disk). The writer latches locally; the runner's lane still reads the old, readable stamps and
   passes. Codex reproduced this with a forked writer: writer latch True, reader latch False.
2. **Crash after broadcast, before the stamp.** The process dies between the network send and the
   `finally`. Nothing records the send at all.

In both cases the lane can sign with a stale nonce (the send collides or replaces) or a stale
balance (an under-funded or over-sized clip).

## 2. Design: one intent record per send, written before it

One directory per chain, `state_path("data")/exec_intents/<chain>/`, created at process start
(`exist_ok`). Each record is one file per network post, so concurrent sends never share state.

### 2.1 Writer rule (every broadcast post)

`token = exec_lock.intent_send(raw_hex, transport, max_block=None)`, called immediately before
network I/O, outside the `try` whose `finally` completes it:

1. Parse the chain id from the signed bytes (`_raw_chain_id`, as `stamp_send` does).
2. Create `<sha256(raw)[:32]>.<pid>.<thread id>.<counter>` with `O_CREAT | O_EXCL | O_WRONLY`, and
   write one JSON line: `raw` (the signed hex), `transport` (`rpc`, `bundle`), `max_block` (bundle
   only, else null), `written_at`. No fsync (section 4, item 5).
3. If any step raises, **do not send**: raise `IntentUnwritable`, which each sender maps to its own
   definite "not sent" result, and latch the local fault with reason `intent: <error>`.
4. Send.

Completion, in the existing `finally`: `exec_lock.stamp_send(raw_hex, intent=token)`.

1. Stamp the chain `.gen` (and nothing else changes in today's stamping).
2. Only if the stamp succeeded, unlink this send's own record. A failed stamp leaves the record, so
   the send stays fenced across processes. A failed unlink also leaves it, and latches.

A send's completion removes only its own record (defect a). Same bytes posted to several providers
(the BSC `BscSender` members, the `auto_free` fan-out) are several posts: each holds its own record
and completes it in its own `finally`, including the posts that keep running after the caller has
returned on the first acceptance ("detached transports").

### 2.2 Reader rule (the lanes' generation)

`lane_generation_fn` scans the chain's intent directory once per decision:

| Directory | Result |
|---|---|
| missing | no intents (backward compatible; process start creates it) |
| unreadable (EACCES, EIO) | `None`, unknown: latch, as an unreadable `.gen` does today |
| empty | resolved: generation as today |
| any entry | in flight or unrecorded: `None`, the lane refuses `generation_inflight` |

The snapshot rule is unchanged: `read_at > generation + margin`.

While any process has a post in flight on the chain (between its intent and its completion), the
lanes refuse. A snapshot read during another actor's send may already be stale; today it is
accepted.

### 2.3 Posts that must write an intent

Amendment 2026-10-14: the BSC A/B builder was missing from the send fence. Every EVM broadcast
post now goes through one of these five `stamp_send` call sites:

| # | Site | Callers |
|---|---|---|
| 1 | `sniper/execution/evm_real_seams.py` `eth_send_raw` | Funded executors on the custody RPC, the exit rebroadcaster and gas top-up (`sniper/services/memecoin_evm_leg.py`), the flap two-leg exit (`sniper/execution/evm_flap_quote_exit.py`, `scripts/memecoin_flap_quote_exit.py`), and 12 operator scripts: `memecoin_base_usdbc_to_weth`, `memecoin_base_usdc_to_weth`, `memecoin_bridge_arc_to_base`, `memecoin_bridge_arc_to_robinhood`, `memecoin_bridge_base_bsc`, `memecoin_bridge_base_solana`, `memecoin_bridge_robinhood_bsc_gas`, `memecoin_bridge_robinhood_bsc`, `memecoin_bridge_robinhood_solana`, `memecoin_bsc_stable_to_wbnb`, `memecoin_fund_robinhood`, `memecoin_robinhood_wrap_excess_gas` |
| 2 | `sniper/execution/evm_fast_submit.py` `EvmRpcProvider.submit` | The `auto_free` same-bytes fan-out built by `sniper/execution/submission_backends.py`, one record per provider post |
| 3 | `sniper/services/memecoin_rh_fast_lane.py` `SequencerSender.send` | The 4663 fast lane, and the BSC lane's public members (Puissant and the chain owner's free endpoints, `memecoin_bsc_fast_lane.default_members`) |
| 4 | `sniper/services/memecoin_bsc_fast_lane.py` `blockrazor_bundle` `send` | The BSC lane's private bundle (`transport="bundle"`, with `max_block`, section 6.2) |
| 5 | `sniper/execution/bsc_builder_ab.py` `BundleProvider.submit` | One durable intent per background builder post; intent failure refuses HTTP and `finally` completes only that post's intent |

Wallet-generation touches (`serialize_executor`, the 4663 lane's execute path, the flap exit) need
no intent: their sends already pass through sites 1–5.

Solana's `real_exec_sol-*.gen` is out of scope. No Solana lane reads a generation snapshot.

## 3. Measured cost (rule 5.2)

Bench in the live lock directory (`/home/bigpapi/Desktop/Sniper/data`, ext4 on NVMe), funded
runner live, on a scratch subdirectory that no reader watches, N = 3,000, 2026-10-07:

| Operation | p50 | p90 | p99 | max |
|---|---|---|---|---|
| Writer intent: sha256 name + `O_EXCL` create + write 400 B + close (rev. 2) | 11.3 µs | 12.9 µs | 17.1 µs | 150.9 µs |
| Completion: unlink own record (rev. 2, after the existing stamp) | 5.8 µs | 6.7 µs | 9.0 µs | 106.4 µs |
| Reader: `scandir` of the chain directory, one entry (rev. 2) | 4.3 µs | 4.5 µs | 5.4 µs | 90.7 µs |
| Reader: `scandir`, empty (rev. 2, the normal case) | 2.9 µs | 3.1 µs | 3.4 µs | 66.2 µs |
| Rev. 1 writer: `open(path, "a")` + `os.utime` (for comparison) | 6.0 µs | 6.5 µs | 7.9 µs | 50.8 µs |

These are author syscall measurements, not organic-send latency. No money action was taken to
measure them. Acceptance: the build logs `intent_us` per post; report p50/p90 over at least 20
organic sends per active chain on the pinned canary before claiming the cost.

## 4. Edge cases (rule 5)

1. **Money path.** Yes: every EVM broadcast and the lanes' admission. The writer change can only
   remove a send (intent unwritable). The reader change can only add a refusal. The bundle's
   explicit `maxBlockNumber` (6.2) can only shorten how long a private bundle may land.
2. **Speed on the fill path.** About 11 µs before each post and 6 µs after it, plus one ~3 µs scan
   per lane decision (section 3). No network call, no lock. Lanes refuse during other actors' send
   round trips, logged as `generation_inflight` so it can be counted.
3. **Concurrency.** One file per post, named by bytes, process, thread and a per-process counter:
   two posts never share a record, and a completion can only remove its own (defect a). No module
   state is added except the existing fault latch.
4. **Absent vs unknown.** A missing directory means no intents. An unreadable one is unknown, so the
   lanes refuse. An empty one is resolved.
5. **Dependency failures.**
   - Intent unwritable: no send.
   - Completion stamp fails: the record stays; fenced across processes.
   - Crash between intent and send: fenced; resolved only when that nonce is consumed (6.1).
   - Crash after send: fenced; resolved on nonce consumption or bundle expiry.
   - Power loss: without fsync a just-created record can be lost with its process. After a power
     loss every lane restarts and re-reads its snapshot; the gap is a send whose bytes left the host
     in the last instant before power loss. Not handled: fsync on the fill path costs milliseconds.
6. **Units and identity.** Host-clock times for W only. The chain id comes from the signed bytes,
   never the caller's label. Sender and nonce are decoded from the stored raw bytes off the fill
   path, by the sweep.
7. **Sample.** `generation_inflight` refusals describe our own concurrency, not the market.
8. **Rollback.**
   - `MEMECOIN_EXEC_INTENT_STAMP=0`: writers skip intents and the bundle's `maxBlockNumber`;
     readers ignore the directory.
   - Writers on, readers old: records are ignored, today's behaviour.
   - Readers on, writers old (an operator script from an older checkout, a "stale checkout
     writer"): it writes no record, so its sends are only as visible as today. Not handled until the
     script is re-run from the current release.
   - Safe in both half-applied orders.

## 5. Tests (acceptance)

- **Concurrent intents (defect a).** A writes, B writes, A completes. The reader refuses until B
  completes, then accepts a snapshot read after `.gen` + margin.
- **Different senders.** Two processes on the same chain, each with its own record; either one's
  completion leaves the other fenced.
- **Detached posts.** `BscSender` returns on the first acceptance while two member posts are still
  running; the reader refuses until the last post completes.
- **Two processes (Codex's probe).** A forked writer writes its intent, sends (faked), and both
  completion stamps raise EIO. The reader process refuses until the sweep resolves the record.
- **Crash after send.** Writer killed between the post and its `finally`: the reader refuses.
- **Intent unwritable.** Zero provider calls; each sender returns its definite "not sent".
- **Resolution (6.1).** Nonce consumed by the same hash (mined), by another hash (replaced), and not
  consumed: only the first two resolve. Bundle-only past `max_block`: resolves. Bundle plus an
  outstanding public record of the same bytes: does not resolve by expiry.
- **Shared failure and recovery.** The stamp latch, the unreadable directory and the sweep's own RPC
  failure (timeout, wrong chain id, error reply) all leave the fence on, never resolved.
- **Rollback knob.** With the knob off, no records, no `maxBlockNumber`, and readers ignore an
  existing directory.

## 6. Recovery: who resolves a record, and on what proof

**Who:** the runner's existing periodic sweep, the single owner. Never a reader mid-decision. A
record is examined only once it is at least **W = 15 s** old (operator, 2026-10-07): longer than the
10 s custody HTTP timeout plus margin, so a healthy post always completes its own record first.

**On resolution** the sweep touches the chain `.gen` first, then unlinks the record, and logs one
WARNING line: `generation fence resolved chain=<id> tx=<hash> after <s> s basis=<criterion>`. Any
later snapshot must then be read after that `.gen`.

### 6.1 Proof: nonce consumption (every transport)

Decode `sender`, `nonce` and the transaction hash from the record's raw bytes (off the fill path).
On the custody RPC, with the reply's `eth_chainId` equal to the record's chain:

    eth_getTransactionCount(sender, "latest") > nonce   ->  resolved, basis=nonce_consumed

Once the account's mined nonce is past it, these signed bytes can never execute, whoever holds
them. Whether they mined or another transaction replaced them does not matter to the fence: either
way the next snapshot, read after the new `.gen`, reflects the mined state.

`pending == latest` at the custody node is **not** a proof (defect b): a private builder, another
node or a mempool elsewhere can still hold the bytes.

### 6.2 Proof: provider-bound expiry (bundle-only identities)

The BSC lane's bundle carries an explicit `maxBlockNumber = source block + K`, recorded as
`max_block` in its intent. BlockRazor documents `maxBlockNumber` as "the maximum block number for
the bundle to be valid", defaulting to the current block + 100 when omitted
(docs.blockrazor.io, Send Bundle, read 2026-10-07). The lane already carries the source block
(`BscLaneEvent.source_block_number`). K = 20 blocks; confirm the current BSC block time in the
build. With no source block, the bundle is sent without the field, `max_block` is null, and only
6.1 can resolve it.

    every outstanding record of this hash is transport=bundle with max_block set,
    and custody eth_blockNumber > max_block + 2                  ->  resolved, basis=bundle_expired

If any outstanding record of the same bytes is a public post, expiry is not enough: 6.1 only. This
is a provider's documented contract, not a chain proof; it is the provider-bound expiry Codex asks
for, and it is never applied to bytes that also went to a public endpoint without completing.

### 6.3 Unresolved

A public post that never mined and whose nonce stays unconsumed remains fenced. The lanes on that
chain stay off; the ordinary executor is not fenced, and its next send on the chain consumes the
nonce, which resolves the record on the next sweep. After 10 minutes fenced, alert once. The
operator can also consume the nonce with a 0-value self-transfer, or run a `--confirm` clear script
after checking the wallet. Neither the sweep nor a restart clears a whole record on its own.

### 6.4 Torn records (build follow-up, Gamma MEDIUM 2026-10-07)

A post runs only after its record's whole JSON line was written (`os.write` must return the full
length; a short write is an unwritable intent) and closed. A torn record (empty, or not one whole
JSON line ending in a newline) therefore proves its post never ran. The sweep resolves it, with no
network read, as `basis=never_posted` when it is at least W old, its mtime is after this boot's
`/proc/stat` `btime`, and the writer PID in its name is gone. A torn record from before this boot
stays fenced: without fsync, a power loss can empty the record of a post that did leave the host.
A whole record waits for 6.1 or 6.2 whatever its writer's state.

## 7. Runbook

- **Unwritable intent (disk failure only).** No send happens. On `auto_free` every provider reads
  `NOT_SENT`, which the aggregate reads as ambiguous: an entry goes `submitted_unknown`, and a full
  exit holds its claim for a human. The process latches its generation fault, so its fast lanes are
  off until a restart.
- **Other new latches, same class as a failed stamp:** an unreadable intent directory and a failed
  unlink of a completed record.
- **Alert `EVM-08 fence ... unresolved: <why>`** after 10 minutes: `nonce not consumed` (the next
  ordinary send on the chain resolves it), `custody read failed: ...`, `torn record ...`,
  `undecodable record: ...` or `unreadable record`. Check the wallet before clearing a record.
- **Not resolved by the sweep:** transaction types 3 and 4 (production signs legacy and type 2).

## Amendment — required standing-approval identities, 2026-10-07

Standing approvals also reuse this journal and nonce-consumption recovery in the separate
`data/standing_allowance/exec_intents/<chain>/` namespace. Their records live through receipt
polling and unknown outcomes; holdings sells and later standing top-ups inspect that namespace.
Ordinary fast-lane admission still inspects only `data/exec_intents/<chain>/`, so these additional
receipt-lived records do not stop entries. The existing sweep owner checks both namespaces.

The standing namespace uses `required=True`: its writer, reader and recovery remain active when
optional post stamping is disabled. Thus the `MEMECOIN_EXEC_INTENT_STAMP=0` rollback above still
applies to ordinary broadcast-post fences, but does not discard standing approval identities.
Confirmed/failed approval receipts clear their record; otherwise only the existing chain-proof
rules may clear it. Alerts identify holdings sells when this separate namespace stays unresolved.

## Amendment — reciprocal maintenance nonce fencing, 2026-10-08

Accepted private gas wraps/unwraps may remain invisible to custody's pending nonce after the
maintenance receipt-wait budget expires. Their separate durable gas ledger therefore fences
ordinary EVM entry, full exit and partial-exit executors under the existing wallet lock;
Robinhood/BSC fast-lane entries apply the same check before nonce allocation/signing.
Only unsigned blocked entry reservations may release. Existing close and de-risk identities
remain intact. Terminal gas rows and other chains do not block execution.

New maintenance writes reciprocally wait for same-chain unsettled funded close/de-risk
identities. A closed row with outstanding de-risk debt still counts; settled history does not.
Existing maintenance-receipt recovery runs before this new-write guard, so two outstanding
identities cannot prevent receipt reconciliation. These local ledger guards add no provider
read, retry or wallet lock. The ordinary post-stamp journal's recovery policy above is unchanged.
Enable Ethereum wrap maintenance only after these guards are merged and reviewed. Before
removing a chain from maintenance, reconcile its outstanding gas rows to terminal proof;
removing the chain also removes its gas-receipt recovery owner.
