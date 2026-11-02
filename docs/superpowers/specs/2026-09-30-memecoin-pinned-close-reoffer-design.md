# Pinned-close re-offer — design

Date: 2026-09-30. Builder: Beta. Order: Alpha. Review: Gamma.
Status: BUILT, **OFF**. Activation is the operator's decision, per chain.

## What it is

A funded sell that was signed and sent, and that the chain answers `not_found` for, keeps its
close claim: the position cannot be sold again until the sell is proven dead or lands. The
re-offer sends that sell's **own stored bytes** again while the claim is held.

It was written on 2026-09-02 (`64ca8da61`) and never ran: the reader asked the chain context for
`_send` and `_confirm_once`, and no context carried them. Wired on 2026-09-30 (`0bc521735`), then
moved off the sweep thread, then bounded by one absolute deadline (round 3).

Base rate (Gamma, 14 days to 2026-09-30): 8 dropped closes. Solana 5, 4663 2, 5042 1.

## The switch

`MEMECOIN_PINNED_CLOSE_REOFFER_CHAINS`: comma-separated network ids, `solana` or a decimal chain
id. Read at call time. Empty by default; a malformed value is empty. The position's network is
normalized (`normalized_coin_network`) before the test, so a Solana row with a NULL network id is
still Solana.

With the switch empty the close recovery does exactly what it did before: no copy is sent.

## What a copy is

- The intent stores the payload as the text the executor first sent: `0x` hex on EVM, base64 on
  Solana. The re-offer hands that same text to the chain's baseline send (`rpc_send` on Solana,
  `eth_send_raw` on EVM). Nothing is decoded into a transaction, re-encoded or re-signed.
- Same bytes means the same nonce and hash on EVM and the same signature on Solana. A chain
  includes it once. **A double sell is impossible by construction.**
- When the first copy lands late, the node refuses the re-offer (`nonce too low`, `already
  processed`). That refusal uses one attempt, and the next sweep reads the receipt and the
  existing recovery settles the close.

## When a copy goes

| rule | value |
|---|---|
| first copy | the sell is 20 s old |
| second copy | at least 15 s after the first: 35 s for a sell watched from the start |
| copies per submission | 2 |
| state required | the sweep's receipt read says `not_found`, and the copy's own read says it again |
| never | a receipt exists; either read failed (`unknown`); no stored payload; the sell is provably dead |

- **Release comes first.** A sell that is provably dead is released and the loop moves on; it is
  never offered beside the re-armed exit's new sell.
- There is no third copy. A copy near EVM's 60 s release buys nothing worth racing it (8 dropped
  closes in 14 days).
- One copy per submission is in flight at a time. A sweep that finds one still running starts
  nothing.

### A copy never arrives after its claim was released (EVM)

The never-admitted proof releases a claim whose nonce is still FREE at 60 s
(`MEMECOIN_EVM_DROPPED_CLOSE_MIN_AGE_S`), and the re-armed exit signs a new sell on that nonce. A
detached copy sent after that would compete with it: if the old bytes mined, the bag would be
sold by a transaction the ledger has forgotten, and the position would stay open against an empty
bag. Gamma found this race three times, each behind the previous fix:

1. a detached copy had no latest dispatch age;
2. a per-request timeout does not bound a copy: the RPC layer rotates across pool members, each
   with its own timeout, and probes a member's identity first;
3. the dispatch age came from the sweep's `now_iso`, stamped before the sweep's own reads.

So the bound is one absolute deadline and a belt:

| bound | value |
|---|---|
| last send time | release age - 17 s after the submission: **43 s**, on the real clock |
| the copy's deadline | 8 s after its real dispatch, and never past the last send time |
| a copy starts only with | 4 s of budget: one receipt read and one send at their 2 s caps |
| a request starts only with | its own full timeout (2 s) left before the deadline |
| the release | waits while a copy of that sell may still send |

- **The real clock.** The budget is the last send time less `real_utc_now()` at the dispatch,
  from `close_submitted_at`. A sweep stamped 36 s whose own reads took 5 s is at 41 s: nothing is
  started.
- **Every request of the copy is gated.** The copy's transport (`short_timeout_http`) refuses to
  START a request with less than its timeout left: the receipt read, the send, each rotation to
  another pool member and each identity probe. So no request of a copy is in the air after its
  deadline, and none is cut short.
- **The refusal is `RequestNotSent`.** The RPC pool stops rotating on it and records nothing: a
  member is never quarantined for our deadline. (The token-safety deadline scope was the other
  candidate. It cuts a request in flight and raises a `TimeoutError`, which the pool reads as
  the member's failure: a copy would have quarantined healthy funded pool members.)
- **A copy whose receipt read left less than one send's timeout is dropped** before the send,
  counted `late`, and uses an attempt.
- **The belt.** The EVM release asks whether a copy of that sell is in flight and waits for the
  next sweep if so (`release_deferred` in the heartbeat). The wait ends by itself one send
  timeout after the copy's deadline: a copy thread that never returns cannot hold a claim.
- With the release at 60 s, copies go at 20 and 35 s; a sell first seen after 39 s (a restart)
  gets none. At 60 s it is released or, if the node holds it, it needs none.
- The release age is read from the setting. A lower setting moves the last send time with it.
- **Solana is unchanged:** no deadline and no belt. Past its blockhash a copy cannot land, and
  nothing is released while the sell can still land.

## Nothing may slow an exit sweep

`recover_unverified_closes` runs at the top of the real-exit pass. A sell is pinned when its node
is in trouble, so a synchronous send there would hold every other position's exit check.

- The sweep decides on its own thread with no network I/O (age, gap, count, payload present).
  For an EVM copy that is due it reads the corrected clock once (`real_utc_now`, a small local
  file): p50 7.7 us, p90 7.9 us over 2,000 reads. At most twice per submission.
- The release belt is a lookup in an in-memory map.
- The copy's receipt read and its send run on a daemon thread.
- The send has its own 2 s transport timeout, under the funded EVM read transport's 3 s and
  Solana's 10 s default.
- Measured on the production context of each chain (solana, 4663, 56, 8453, 5042) with a node
  fake whose send sleeps 1,500 ms, 15 sweeps each way, one run on 2026-09-30:

  | | sweep p50 | sweep p90 |
  |---|---|---|
  | switch off | 0.12-0.17 ms | 0.19-0.20 ms |
  | a re-offer pending | 0.13-0.19 ms | 0.20-0.28 ms |

  The slowest single sweep with a re-offer pending must stay under 500 ms (the test's bound);
  the send's 1,500 ms never appears in a sweep. The fake's reads are instant, so this measures
  the added wait and not a live sweep's duration.

## The bound is in memory

The count of copies per submission lives in the runner process. **A restart forgets it and allows
two more copies of the same signed bytes.** They are the same transaction: no double sell, only
more refusals from the node. The 15 s gap applies to them too.

## Observability

- One WARNING when a copy starts, one when the node accepts it, and one per refused copy with the
  transaction, the attempt (`1/2`) and the node's own words.
- The runner heartbeat carries `pinned_reoffer`: eligible, resent, send_failed, exhausted,
  too_young, not_due, too_late, late, in_flight, release_deferred, receipt_present, tracked,
  in_flight_now.

## Edge cases (CLAUDE.md rule 5)

1. **Money path:** an exit send. Off by default.
2. **Fill path:** entries are untouched. The exit sweep gains no wait (above).
3. **Concurrency:** the counters, the attempt count and the in-flight map are shared by the sweep
   thread and the copy threads under one lock. The attempt is reserved before the send. The
   copy's deadline is thread-local: only the copy's own thread is gated by it.
4. **Absent, zero, unknown:** no payload, an unreadable intent or timestamp, a failed receipt
   read, any state but `not_found`: nothing is sent.
5. **Dependencies:** a send that raises, times out or is refused uses an attempt. A thread that
   cannot start is logged and the in-flight mark is cleared. A pool member that answers 429 or
   times out inside its own 2 s is penalized as on any other request; a member the copy never
   reached is not.
6. **Units and identity:** ages in seconds on the caller's corrected clock; the payload as text.
7. **Sample:** none.
8. **Rollback:** take the chain out of the switch and restart the runner.

## Not covered

- Chain 1 (Ethereum) is not on main. Its sends are private-only; `eth_send_raw` refuses chain-1
  bytes to custody, so on that branch the re-offer sends nothing until it is given the private
  transport. That is a task for the chain-1 branch.
- The counters are not persisted.
