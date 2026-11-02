# Slot-lane routed pairs that expire: name the cause, then pair what is worth pairing

Lambda, 2026-10-07. Alpha's item 2 from the 29-row late-fill breakdown. Status: SPEC, step 0 only is
proposed for build. Nothing here changes what the funded runner buys until step 1 is approved.

## What we know

`RoutedPairer` (sniper/services/memecoin_slot_signal.py) turns an owner's processed logs frame plus
its token-account notifications into one `RoutedBuy` when, in one slot: exactly one coin account
rose, the owner's USDC fell, no other quote or stable moved, the lane did not claim the frame as its
own pump buy, and exactly one logs frame names the slot. Anything else waits `ROUTED_PAIR_WAIT_S`
(0.4 s) and expires; the watcher (confirmed, ~1.5 s) or J7 then owns the buy.

7 of the 29 slot-lane skip/fail late fills since 10-02 are `pair_expired` (median entry +13.6% over
the source, PnL -$49; on-time lane fills cost +7.9%). The fill-timing note says only
`pair_expired`, with no reason. `expire()` writes that note for EVERY pair that never emitted:
- a SOL-paid route (`usdc >= 0`: lamports are not a token account, so the pairer never sees them);
- a route that moved another quote or stable through the owner's own accounts (`refused =
  "other_quote"`, e.g. a temporary wSOL hop);
- a frame the lane claimed as its own pump buy (`refused = "lane"`): the lane decided it, so a
  `pair_expired` note is wrong there;
- two or more coins, or a coin that fell, or two signatures in the slot (that one is noted
  `ambiguous` separately).

Two research reads (book flat, 2026-10-07) already show the 7 are not what the first breakdown
assumed:
- 5tV3Lfx8 (OmakaseOnly, 10-02 21:39Z) was USDC-paid, $2,500 through the FLASHX router
  (FLASHX8Dr...), not SOL-paid. Its owner's USDC fell and one coin rose; the likeliest cause is a
  temporary wSOL hop (`other_quote`), unproven.
- 8qfyGBXh (DegenCapitalLLC, 10-05 08:18Z) is a DFlow route (BisonFi, Tessera, PumpSwap) whose fee
  payer is a co-signer (AgmLJBMD...), not the owner.

So the class counts the build should be sized on do not exist yet.

## Step 0 (proposed for build): name every expiry

`expire()` passes the reason to `_skip_note(sig, "pair_expired", why)` and counts it in the
socket status (`routed.expired_by`):
- `why = pair["refused"]` when set (`other_quote`, `lane`);
- else `sold` when USDC did not fall, no coin rose, and USDC rose or a coin fell (a sell paid in
  USDC or native SOL; added 2026-10-07 after review, so a sell no longer reads as
  `usdc_not_spent`). A plain USDC transfer in, or a plain coin transfer out, also reads `sold`;
- else the first failed `_check` condition: `usdc_not_spent`, `coins=<n>`, `coin_fell`, `no_logs`.

The outcome stays `pair_expired`: a new outcome would rank as a lane verdict in
memecoin_slot_skips' precedence and in the Telegram wording. A `lane`-refused pair reads
`pair_expired` / `lane`, which names the lane's own decision.

Edge map (CLAUDE.md rule 5):
1. Money path: no. Telemetry only; `_check`'s emit decision is untouched.
2. Fill-path latency: none on the entry path. One string choice per expired pair, on the socket
   thread, in the existing expiry sweep.
3. Concurrency: the pairer is single-threaded (the socket's); `_skip_note` is the existing deque.
4. Absent vs unknown: a pair with no signature still has no note (nothing to join), counted only.
5. Dependencies: none.
6. Units: n/a.
7. Sample: every routed owner's expired pairs, not just funded ones; the count describes our
   watched roster.
8. Rollback: revert; the note's `reason` column goes back to NULL.
Check left behind: one test per reason, through the real `RoutedPairer` (`on_token` / `on_logs` /
`expire`), asserting the note.

## Step 1 (not proposed yet): pair what step 0 shows is worth it

Decided after a day of named expiries, joined to late fills and their cost. Candidates:

a. **Temporary wSOL hop** (`other_quote` with the wSOL account created and closed in the slot):
   accept a wSOL movement whose account nets to zero. The token notification for a closed account
   arrives unparsed, so the pairer needs the create/close shape from the logs frame, not the
   balance stream. Risk: a real SOL leg disguised as a hop; bound it by requiring the owner's USDC
   to fall and the wSOL account to end closed.
b. **SOL-paid routes** (`usdc_not_spent` with lamports down): one `accountSubscribe` on the owner
   (+1 subscription per owner: today 23 owners, 46 program subscriptions on the socket's key;
   check the key's limit first). The lamport fall includes the network fee, priority fee, any tip
   and new-account rent, none of which the processed notification separates. Admission must not
   price on it: the confirmed proof's route payment does that through the existing receipt resume.
   A `RoutedBuy` grows `quote_mint` and `quote_spent_atomic` (lamports), and
   `RoutedHandoff.offer`'s `payment` check must compare lamports to lamports with an explicit fee
   bound, never USDC to SOL.
c. **Split routes** (`impact_unknown: no_whole_leg`, 2 of the 7): already resumable on their proven
   venue leg since ca9376244; what is missing is a lane venue for the leg the route used. Size it
   from the named step-0 notes plus `pool_of`'s `split_route` reason.

Each of a/b/c gets its own edge map, red tests and Gamma review before any code.
