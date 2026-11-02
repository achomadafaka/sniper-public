# Solana vol_death: a mint-wide witness before any fire (`mint_moving`)

Status: approved by Alpha 2026-09-28 (signature-only veto; Lambda's and Gamma's reviews folded in).
Money path, exit side. Author: Beta, 2026-09-28. Operator order,
via Gamma: "fake volume death, fix". Evidence: Gamma, `scripts/research/voldeath_lull_check.py`
(01e1dc4c), fixture `tests/fixtures/memecoin/voldeath_mint_window_frames.json` (d9d93786).

## Problem

In 4 of the 21 funded Solana vol_death fires since 2026-09-22, the silence was fake: 63382, 64492,
65090 and 65543. The coin kept trading on OTHER pools of the same mint while we watched ONE pool.
`sol_position_pools` holds one row per position, and the on-chain proof
(`probe_position_activity`) reads only that pool's coin vault. Mint-level transactions in the
30 s before each fire, against the pool's own: 3 vs 12, 7 vs 44, 3 vs 6 and 3 vs 3. It is the
Solana copy of the 4663 "bind every pool" defect of 2026-09-04.

## Change

After the existing vault probe answers `still`, the SAME background probe
(`SolanaSwapMarkCache.onchain_witness` -> `run_probe`, already off the exit-sweep thread) makes one
mint-level read:

    getSignaturesForAddress(<mint>, {"limit": 100, "commitment": "confirmed"})   newest first

The page is 100, not 20 (Lambda's review). On a busy mint, twenty rows newer than the window's upper
edge would fill a 20-row page, read as unknown and fire. The verdict does not depend on row order.

A row counts when all of these hold:
- `err` is null;
- the signature is not one of the position's own (`memecoin_fills.tx_sig` for that position);
- `blockTime` lies in `[now - (exit_s - 2), now - 1]`, which is `[fire - 28, fire - 1]` at the
  live `exit_s = 30`.

Any counting row makes the verdict `moving:mint`, carrying the newest time, the newest
SIGNATURE and the count, and `armed_exit_decision` returns `(False, silence, "mint_moving")`. Each
veto read logs one stamped line with the newest signature, so a later getTransaction pass can
grade spurious vetoes (Alpha). Otherwise the verdict stays `still`, and the fire proceeds exactly
as today.

The margins come from Gamma's fit on all five fixtures. `blockTime` is whole validator seconds.
The window's lower edge is tied to `exit_s`, not to the measured silence: a silence-based edge
(`now - silence + 1`, 32.4 s back for PHISH 66467) vetoes PHISH wrongly on the last trade before
its silence (14:42:54). `exit_s - 1` and `exit_s - 2` both fit the five fixtures; `exit_s - 2`
keeps one more second of margin for whole-second blockTime. The fire's own second is
excluded, because PHISH vetoes on trades that resumed at 14:43:26. On all 22 fires the rule
vetoes 8: 5 with a decoded trade inside (the 4 above plus letsgopenguin 65511) and 3 on
undecoded-only transactions (63166, 64636, 66131). That is conservative, and stated below.

RPC: the vol_death proof chain, minus the funded read lane. That is `MEMECOIN_VOLDEATH_SOLANA_PROOF_RPC_URL`
(unset today), else `ONCHAIN_WATCH_HELIUS_RPC_URL` (key id 84046934), else
`MEMECOIN_SOL_H1_ACTIVITY_RPC_URL` (shyft): the first one set, one attempt. It is never the custody
RPC, never `solana_read_rpc()` (the funded read lane), and never the research key.

## Edge cases (CLAUDE.md rule 5)

1. **Money path: yes, the exit side.** The change can only HOLD a vol_death sell that would fire
   today. It never forces a sell and never touches entry, sizing or signing. A wrong veto keeps us
   in a coin the rail would have sold. The other rails (b_stop, TP, trail, catastrophe, mirror)
   are unchanged and still close it. A wrong fire is today's behaviour.
2. **Speed.** The fill path is untouched. The exit sweep gets no new synchronous call: the read
   runs inside the existing background probe, whose verdict the rail already waits for
   (`onchain_pending`). What it adds is the read's duration to the probe's answer, so a fire
   arrives that much later. The wall-clock bound is 0.4 s through a dedicated 2-worker pool
   (`future.result(timeout=0.4)`); urllib's socket timeout does not bound getaddrinfo, the
   2026-09-25 trap. Measured by Lambda on key 84046934 at a flat book (2026-09-28
   15:00:02-15:01:03Z, n=20 calls at limit 20 over 8 recent mints, 0 errors or 429s): p50 34.3 ms,
   p90 46.0 ms, max 119.4 ms. The first call pays TLS setup (119 ms). This client opens a new
   connection per read, so budget about 120 ms. A 100-row page is not measured. It was a quiet
   minute, so there is no data under load.
3. **Concurrency.** The probe pool and the new 2-worker read pool are created at import and never
   mutated. The veto cache is a dict on the `SolanaSwapMarkCache` instance behind its existing
   `self._lock`. No module attribute, env var, resolver or shared connection is written.
4. **Absent vs zero vs unknown.**
   - No counting row in the window means `still`, a measured zero.
   - A timeout, HTTP error, JSON-RPC `error`, a result that is not a list, or a malformed row
     means UNKNOWN, which fires as today (fail-open to the exit, the operator's rule). It is
     logged (stamped WARNING `vol_death mint witness unknown: pos=.. cause=..`) and written into
     the vol_death audit `detail` (`mint=unknown:<cause>`).
   - A row with `blockTime` null (not yet assigned) is unknown for that row and is skipped, not
     counted. If every row of the page is inside the window and failed (`err` set), the page
     cannot see further back, and it counts as unknown.
5. **Dependency failures.** Timeout, 429 (including a quota body), 5xx, a partial page and a
   wrong answer all map to unknown, which fires as today. A wrong answer that shows a spurious
   transaction inside the window can only hold. The read is one call with no retry, so it can
   never extend the hold loop.
6. **Units and identity.** `blockTime` is whole validator seconds; `now` is the sweep's
   corrected-UTC ISO stamp (`real_utc_now_iso`, about 0.05 s from the host clock). The 2 s and 1 s
   margins absorb both. The mint is `pos["mint"]`, the base58 SPL mint; Solana only (`network_id
   == 'solana'`). EVM chains are untouched: the 4663 path has its own pool binding.
7. **The sample.** Gamma's 22 fires since 2026-09-22 are every funded Solana vol_death fire in the
   window, not a selection. The veto count (8) includes 3 undecoded-only windows, so the rule is
   conservative by about 3 of 22. Those three can only have held a coin that the other rails still
   manage.
8. **Rollback.** The knob `MEMECOIN_VOLDEATH_MINT_WITNESS` defaults to 0 (Alpha, Lambda F4); it
   is activated by an explicit .env line on the operator's yes at a flat book. 0 plus a runner
   restart gives exactly today's behaviour. Half-applied states do not arise: the knob is read per decision.
   Rolling back the release also restores today's behaviour.

**Review fixes (Lambda, 2026-09-28):**
- F1: a veto episode longer than `MEMECOIN_VOLDEATH_MINT_VETO_MAX_S` (120 s) gives up and FIRES,
  with a stamped line and `mint=unknown:veto_cap_120s`. Otherwise a mint trading every 27 s or
  less on another pool would hold until the 4 h max_hold.
- F2: our own signatures are every funded position's fills on the mint, plus de-risk and close
  sigs, plus pending-entry tx_sig and cleanup_tx_sig. They are read in the background probe from
  the read-only ledger (DB_PATH, else SNIPER_STATE_ROOT, never the cwd), never on the sweep. An
  unreadable list is unknown and fires.
- F3: at most two reads are in flight; a third is refused as `busy` (unknown, fires) rather than
  queued, and a timed-out read is cancelled if it has not started. No backlog bursts into the key
  on recovery.
- F7: the cache keeps the newest time per mint, and each caller computes the expiry with its own
  exit_s.
- F10: the runner's HELD warning for mint_moving logs once per veto episode, not every sweep.

**Load bound, "one read per fire, not per tick".** A `moving:mint` verdict with newest time T is
cached on the cache instance until `T + (exit_s - 2)`. No re-read happens before the newest
transaction leaves the window, which is at most one read per about 28 s per mint while a veto
holds. A `still` or `unknown` verdict fires, which ends the loop. The vault probe's own cadence
(every about 3 s while pending) is unchanged.

**Not handled, stated:**
- A non-trade mint transaction (a transfer, an ATA creation, a metadata change) inside the window
  vetoes. Gamma read the 3 undecoded-only vetoes in full: 63166 and 64636 each held a real swap
  that trade_of could not decode; 66131 was an ATA create only. So 1 of the 8 vetoes on the 22
  fires would be spurious, and it only delays the exit to the next silence window. Decoding
  (getTransaction per in-window signature, +100-200 ms) was not chosen (Alpha).
- A mint so busy that 100 signatures (the page) never reach back into the window, and none of
  them counts, is unknown and fires. A counting row anywhere in the page vetoes, whatever the
  order.

## The runnable check left behind

`tests/test_memecoin_voldeath_mint_witness.py`:
- the pure rule on all five fixture positions (4 `mint_moving`, PHISH `fire`), plus the margins as
  mutants (a silence-based lower edge must break PHISH; including the fire's own second must break
  PHISH);
- own-signature exclusion; failed rows skipped; null `blockTime` skipped; an all-failed page is
  unknown;
- every failure mode maps to unknown and FIRES;
- the veto cache holds the verdict until `T + exit_s - 2` with no second read;
- `armed_exit_decision` returns `mint_moving` through the real `onchain_witness`, and
  `mint_moving` is in POST_THRESHOLD_REASONS and HELD_WITNESS_REASONS (the reason-guard test);
- the knob off gives today's result.
