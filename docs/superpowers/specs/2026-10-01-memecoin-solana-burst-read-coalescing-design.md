# Funded Solana RPC under same-mint bursts: measurement and a read-coalescing proposal (spec)

Date: 2026-10-01. Author: Lambda. Order: Alpha, after Gamma traced the 09:26:24Z `http_429`. That 429
rotated the funded pool off index 0, and Gamma traced it to our own burst: Roveryaps laddered about
15 CURVEPILL buys in 7 s.
**Status: PROPOSED. Read-only measurement; nothing is built. The operator decides any change on the
money path. Alpha 2026-10-01: build order is the instrument, then R3, R1, R2. Each ships default off,
and any money-path activation goes to the operator with measured p50/p90.**

## 1. What was measured (7 days, 2026-09-24T09:50Z to 2026-10-01T09:48Z, no provider call)

**Sources.**
- `logs/memecoin_runner.log` and its 09-29 rotation, for the slot-lane, on-chain-lane and mirror
  lines and the pool's rotation lines.
- `memecoin_entry_attempts` (read-only), for the J7 decisions and the stages each decision reached.
- `memecoin_fills`, for our own spends.

The pool's rotation lines carry no timestamp. Each is placed between its stamped neighbours. Those
brackets are wide: p50 12.4 s, p90 36.5 s, with only 4.7% within 2 s. The three `http_429` brackets
are 1.5, 3.0 and 0.15 s.

**What is NOT recorded anywhere.** Funded RPC calls per decision. `RotatingSolanaRpcHttp` counts
calls per endpoint in memory only (`key_status`), with no method, no lane and no decision, and the
heartbeat carries only the pool snapshot. So §3 counts calls per decision from the code path, and §5
proposes the counter that would measure them.

**Decisions.**
- **Total:** 8,242 Solana decisions:
  - J7 (`memecoin_entry_attempts`): `j7_wallets` 4,612, `j7_shadow` 444, `j7_pump` 194;
  - on-chain lane (log lines): 2,013;
  - slot lane (log lines): 979.
- **Mirror watches:** 195 more, which read on the keyless read RPC (`OwnerAccountWatch`), not on
  the funded pool. They are out of this count.

**Bursts.** A burst is a run of decisions with the same source and the same mint prefix, each within
10 s of the one before.
- 485 bursts hold 3 or more decisions: 2,154 decisions, 26.1% of the total.
- The largest:

| start (UTC) | source | coin | decisions | span |
|---|---|---|---|---|
| 09-30 23:36:16 | Roveryaps | `Gm7CtUKd` | 90 | 28.2 s |
| 10-01 09:26:19 | Roveryaps | `AdcGLpuj` | 18 | 6.6 s |
| 09-27 01:25:31 | Roveryaps | `aqhpFfoG` | 17 | 9.2 s |
| 09-26 12:09:49 | DexGemsReal | `HgcxVs6k` | 16 | 33.1 s |
| 09-28 20:35:47 | frankdegods | `CARDSccU` | 16 | 26.3 s |

**Cross-check (Gamma).** Counting from `memecoin_entry_attempts` alone (J7 and on-chain attempts, by
`source_user_id` and full mint) gives a different share: 253 bursts of 3+ holding 1,159 of 6,592
decisions, 17.6% against 26.1% above. The gap is real, not a bug:
- the 979 slot-lane decisions are log-only (no attempt row), arrive about every 0.5 s in a burst, and
  the slot lane's `slot_lane_not_pump_mint` reject makes 0 funded calls;
- on-chain lane outcomes that never wrote an attempt row.

**For funded calls, 17.6% is the relevant share.**

**Rotations and where they sit.** 886 pool events:
- 514 `rotated:transport_error`;
- 369 `unavailable:transport_error`, where no healthy endpoint was left;
- 3 `rotated:http_429`.

**http_429:** 3 in 7 days.

| time (UTC) | decisions in the prior 10 s |
|---|---|
| 09-26 15:23:19 | 1 |
| 09-29 19:31:16 | 1 |
| 10-01 09:26:23 | 18, the Roveryaps burst |

**Only one of the three is a burst.**

**transport_error is not burst-driven.** Two tests:
- **Decisions in the 10 s before each rotation**, against 20,000 random 10 s windows. Zero decisions:
  88.8% against 91.2%. Three or more: 1.0% against 1.0%.
- **Bracket-aware, because a quiet log both widens a bracket and logs fewer decisions.** Count the
  decisions in [bracket start - 10 s, bracket end], against random windows of the same widths. Three
  or more: 1.7% against 3.6%. At least one: 19.6% against 18.9%.

These errors cluster by hour (12, 15, 18 and 21 UTC carry 521 of 883) and by day:

| day | 09-24 | 09-25 | 09-26 | 09-27 | 09-28 | 09-29 | 09-30 | 10-01 |
|---|---|---|---|---|---|---|---|---|
| rotations | 28 | 184 | 134 | 241 | 203 | 58 | 32 | 6 |

244 of them are "no healthy alternative" on index 4, the last endpoint: every endpoint failed at
once. That pattern looks like network loss, not a provider limit.

None of our crons explains it. Alpha checked the crontab on 2026-10-01: nothing runs 3-hourly, and
the */4 local jobs land at 0, 4, 8, 12, 16 and 20 UTC. It is recorded as provider-side or
network-side, **unexplained**. Out of scope here.

## 2. Repeat decisions inside bursts (J7 and on-chain attempts)

Over 7 days, 1,199 decisions repeated a decision on the same source and mint within 10 s. 76.1% of
them ended with the first decision's own reason. Where they stopped:

| stage reached | decisions | top reasons |
|---|---|---|
| refused before market data | 741 | `not_approved` 495 (wallet not armed) |
| refused at market data | 357 | mcap over ceiling 294, floors |
| reservation / safety / quote | 80 | exact quote above source price 40 |
| signed | 21 | 14 on-chain fills inside a burst |

A later signal in a burst can FILL. For example, the first is refused under the floor and a larger
second buy passes it. So skipping later signals would change entries; that is why §4 coalesces
READS, never decisions.

## 3. Funded calls per decision (from the code; release e8671e6e0)

| stage | funded Solana RPC (pool) | other funded provider |
|---|---|---|
| before market data (roster, `not_approved`, stale) | 0 | 0 |
| market data, Solana, any lane, shadow included | 3, started beside the GET by `_start_entry_prereads`: USDC balance, SOL balance (`_gate_balances`, always live), mint account (`mint_read_fn` -> `read_mint_account`, no cache) | early Jupiter quote (`_start_early_entry_quote`), not on `j7_shadow`, cheap prescreen first |
| reservation, safety, quote | 0 new: they consume the prereads (`token_safety_fn(pre_read=...)`) | Jupiter quote and the reverse quote |
| signing to confirmation | executor reads: balances, blockhash, send, status, receipt | the build |

The preread flags are ON in the running process: `MEMECOIN_ENTRY_EARLY_GATE_READS` defaults to 1,
`MEMECOIN_REAL_TOKEN_SAFETY_ENABLED=1` and `MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain` set `read_mint`.

**Measured load from prereads.**
- 2,116 decisions reached market data, about **6,348 funded calls in 7 days (about 907 a day)**.
  This is an upper bound: 142 of them are slot-lane rows, which may take another path.
- **Mint read:** 906 of 2,116 (42.8%) repeat a read of the same mint taken within 10 s.
- **Balance pair:** a decision repeats one read within W s with no funded fill between on 447
  (21.1%) at 1 s, 573 (27.1%) at 2 s, 759 (35.9%) at 5 s and 918 (43.4%) at 10 s.
- **Shadow:** `j7_shadow` made 333 of the 2,116, about 999 calls, though a shadow decision never
  reserves. Its balance pair is read and never used.
- **The 09:26 burst:** 16 decisions reached market data in 15 s, about 48 preread calls, about 3 a
  second on top of the exit path's own reads.

**Not measured:** the share of all funded calls this is. The exit path's previews and balance reads
per open position are probably larger. Only the counter in §5 can say.

## 4. Proposal: coalesce the reads, never the decision

**The rule.** The first signal's path is unchanged: it makes the same reads, at the same time, on
the same thread, and so gets no added latency. Only a later read that would repeat state already
read is served from that read.

**R1. Mint account: per-mint single flight plus a 10 s TTL.**
- `mint_read_fn` keeps `{mint: (read_at_mono, result)}` and an in-flight future per mint.
- A later decision gets the stored result if it is under 10 s old. If a read for that mint is in
  flight, it joins it.
- Only a successful read is stored. An exception or None is never stored and never served.
- **Saving:** about 906 calls a week (42.8% of mint reads), and almost all of them in a burst.
- **Store only a mint that can no longer change (Alpha 2026-10-01; Gamma MEDIUM).** A read is stored
  only when one of these holds:
  1. the owner program is SPL Token and both `mintAuthority` and `freezeAuthority` are None;
  2. the owner is Token-2022, `mintAuthority` and `freezeAuthority` are None, and every extension
     present is on an explicit allowlist with every authority it carries None. The allowlist:
     - `metadataPointer` and `tokenMetadata` (the metadata update authority is harmless);
     - `transferFeeConfig` with both its config and withdraw-withheld authorities None;
     - `transferHook` with authority None and program None;
     - `permanentDelegate` absent, or present with delegate None.

  Never stored:
  - any other extension, including `pausable` (a pause authority can stop transfers, so we could
    not sell), `mintCloseAuthority`, `interestBearingConfig` and `scaledUiAmountConfig` (rate
    authorities);
  - any extension type the parser does not know.

  Such a mint is read fresh by every decision, exactly as today.
  - **Why it is safe:** an authority that is None can never be set again, so nothing on a stored mint
    can act inside the 10 s.
  - **What a stored read CAN hide** is supply, from a burn. With `mintAuthority` None, supply can only
    fall, so a stale read overstates mcap. That refuses more at the mcap ceiling and at the
    source-share floor, never less.
  - **Tests at build:** a Pausable mint is never stored; a mint with an unknown extension type is
    never stored; a mint with a live freeze authority is never stored; a fully immutable Token-2022
    mint with metadata is stored.

**R2. Gate balances: reuse within the spend generation and 2 s.**
- `_gate_balances` already keeps `_gate_balance_generation`.
- **Where it bumps today (release e8671e6e0), not at the fill:**
  - Solana: `_gate_balance_invalidate("solana")` at `memecoin_real_runner.py:3877`, after the J7-shadow
    return and before `real_entry` routes to the reservation and the leg.
  - EVM: in `_enter`, `:2056`, before `evm_real_entry`.
- **Required (Alpha):** the bump stays at the decision to spend, before the RESERVATION, never at the
  fill. Two concurrent decisions must not both size off one read once either has decided to spend.
  - A read is reused only if the generation is unchanged at the moment of reuse.
  - The reservation's own exposure check over the ledger's reserved and pending totals stays the
    authority on bankroll.
  - Test on the PRODUCTION threaded path: two decision threads through the real hook, the second
    joining after the first has passed :3877. The second must read fresh. A hand-built context does
    not count.
- **Outflows the generation does not see (Gamma LOW 2).** The generation counts only our own spends.
  An external outflow inside the 2 s TTL is not seen: an operator transfer, or a Solana rent or
  gas top-up path if one exists. The 2 s bounds that window.
  - A stale-HIGH balance can only make a send fail on insufficient funds; it can never over-spend.
  - The reservation's ledger exposure check stays the authority on bankroll, and the executor's
    own pre/post reads stay live.
- A read is reused only when it succeeded, the generation is unchanged and it is at most 2 s old. A
  later decision also joins one already in flight.
- The executor's own pre/post balance reads stay live, as today's docstring requires.
- **Saving:** about 573 pairs, about 1,146 calls a week, at 2 s.

**R3. No balance pair for a Solana `j7_shadow` decision.**
- The same rule EVM already has: "a shadow EVM decision reads nothing it will not use".
- Shadow keeps the mint read, which feeds its on-chain band check.
- **What consumes the pair on the shadow path today (Gamma LOW 1, release e8671e6e0).** The Solana
  gate read (about `:3829`) runs for every decision. The shadow returns at `:3871`, and nothing
  between reads `sol_usdc` or `sol_native`. Their only shadow consumer is the trace substage
  `timing_gate_balance_read` (measurement).
  - The early quote already skips shadow (`_start_early_entry_quote` `:1938`; the band's early-quote
    path `:9501`). So `_early_quote_clip`, the only other reader of `pre.balances()`, never runs for
    shadow.
  - Under R3 the shadow trace records that substage as `skipped: j7_shadow`, so a timing study drops
    shadow rows explicitly instead of reading a gap.
  - `_settle_early_mint_read`, which sits in the same block, must still run for shadow.
- **Saving:** 333 pairs, about 666 calls a week. This overlaps R2.

The three overlap, so their sum is not the saving. Together they cut most of a burst's prereads to
the first decision's three calls. None of them touches a decision's inputs beyond the read's age:
2 s for balances in one spend generation, and 10 s for a mint account.

**Not proposed:** skipping or merging later signals. Later signals filled 21 times inside bursts
(§2), and the cheap refusals already make 0 funded calls.

## 5. Instrument (needed first, and on its own if R1-R3 are declined)

A per-label, per-method call counter in the pool's own call path: one dict increment under the
pool's existing lock. The label comes from a thread-local the decision sets (`entry:<lane>`,
`entry_preread:<lane>`, `exit:<lane>`); an unlabelled thread counts as `thread:<thread class>`. The
heartbeat's `solana_rpc_pool` block gets:

- `calls_by_label: {label: {method: n}}` since process start, counting JSON-RPC methods (a batch
  counts once per member);
- `calls_per_minute: [[minute_epoch_s, {lane_class: n}]]` for the last 60 minutes, counting HTTP
  requests, with `lane_class` one of `entry`, `entry_preread`, `exit`, `other` (as built, amended
  2026-10-01: a ring keyed by full label doubled the heartbeat, Alpha trimmed it to lane classes);
- `call_count_failures`: counts dropped by a broken counter, so an empty `calls_by_label` with
  failures above 0 reads as unknown, not zero.

This answers "funded calls per decision" directly and shows the exit path's share. It is also the
before/after measure for R1-R3.

## 6. Edge cases (CLAUDE.md rule 5)

1. **Money path.**
   - R1: the mint read feeds token safety, which can refuse an entry.
   - R2: the balances feed reservation sizing.
   - Both are money-adjacent. The operator decides, and nothing here touches signing or the executor.
2. **Speed.**
   - **First decision:** unchanged.
   - **Later decision:** a cache hit is faster, and a join waits at most for the first read, which
     it would otherwise have repeated.
   - Lookup cost: p50/p90 measured at build over 10,000 lookups, next to `market_data_ms` and
     `reservation_ms` before and after, from `memecoin_entry_attempts`.
3. **Concurrency.**
   - Decisions run on several threads; caches and in-flight futures sit under one lock per cache.
   - R2's generation is checked when the read starts and again when it is reused. A spend in between
     makes the reuse a miss.
   - No module attribute is reassigned after start, and there is no capture-then-restore.
   - A join waits on the future's own deadline: the gate's `_GATE_BALANCE_DEADLINE_S` for
     balances, and the mint read's timeout.
4. **Absent, zero, unknown.**
   - A failed read is never stored.
   - A stored balance of 0 is a read that answered 0, served as 0.
   - A joined read that fails gives each joiner "unavailable", which takes today's cache-fallback
     path. It does not trigger N new reads.
5. **Dependencies.**
   - A 429 or a timeout on the first read fails its joiners together: one request, not N. That
     fewer requests is the point.
   - A rotation between reads is invisible to the cache: the pool rotates under it.
6. **Units and identity.**
   - Balances keep `_gate_balances`' units.
   - The mint key is the full mint, never the 8-character log prefix.
   - Solana only: EVM has its own reads and is untouched.
7. **The sample.** Our own decisions over 7 days. Bursts are a property of the sources we follow:
   Roveryaps has 3 of the 5 largest.
8. **Rollback.** Each of R1, R2 and R3 is behind its own knob, default off. Off is today's path.

## 7. Decisions for the operator

1. Build the instrument (§5)? It is telemetry only.
2. R1 and R2, behind knobs, measured with the instrument for a week before going on?
3. R3, which removes reads a shadow decision never uses?
4. The transport-error clusters at 12, 15, 18 and 21 UTC, 09-25..09-28: investigate the host
   network separately?
