# Solana clip ceiling 200 above nano

Operator order 2026-10-08 ~14:00Z, via Alpha: "Raise the Solana clip size to 200 on entries above
50k mcap."

## Today

A Solana automatic clip is bankroll sizing (sizing equity / chain slots), then the Solana ceiling
`MEMECOIN_SOLANA_MAX_CLIP_USD` (175 in `.env`), the regime scale, the caller's per-wallet cap
(`entry_cap_usd`: full-cap and proven wallets `MEMECOIN_REAL_MAX_ENTRY_USD` = 175, unproven 50,
trial 20) and the global hard cap `global_cap_usd()` (= 175 while wallet caps are on). The floor,
the spendable clamp and route-capacity reduction apply as before.

## Rule

A Solana entry whose classification resolved the coin's entry mcap at $50K or more gets a ceiling
of `MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO` (default 200, operator order; 0 = today) for the
Solana max clip, the global hard cap, and every per-wallet cap that equals the global (full-cap
wallets and proven cells). "Above nano" is band micro, small or mid, or out_of_scope with a resolved
mcap (>= $2M; the operator said above 50k). Nano, assumed_nano and unresolved mcap keep 175:
absent is not above. The raise never lowers a ceiling (a larger configured value wins, and a
disabled ceiling stays disabled), never raises unproven (50) or trial (20) caps, and never touches
an EVM chain. It is a ceiling, never a target.

## Where it resolves

`memecoin_risk.solana_clip_ceiling_usd(classification)` names the raised ceiling or None. The
classification is the input, so the band is the one the entry is already admitted and exited on.

- `reserve_entry` and `preflight_entry` take it from their classification and pass it to the
  builder (`clip_ceiling_usd`) and to `entry_cap_usd(..., ceiling_usd=)`.
- `real_entry_hook` applies `entry_cap_usd` before the leg (`_size_cap`), so it passes the same
  ceiling; otherwise its 175 would bind the reservation.
- Slot lane (Alpha call 1): the USDC leg is prefetched before the coin is known (`leg_plan`,
  roster clip) and a leg must be 90-100% of the reserved clip, so a 200 reservation would refuse
  its own 175 leg. The slot lane keeps today's clip on the USDC leg. The raise is passed only on its
  SOL-funded try, and only when native SOL after gas and `MEMECOIN_SOL_EXEC_BUFFER_CAP_USD` both
  cover the raised clip; otherwise the SOL try refuses and falls back to USDC, which would be
  slower than today's SOL fill at 175. The buffer cap is 175 live, so no slot-lane entry is raised
  until the operator changes it. That knob is NOT only a ceiling: the SOL buffer worker
  (`buffer_topup_once`) buys SOL with USDC up to it, and startup validation needs
  `MEMECOIN_SOL_EXEC_BUFFER_MIN_USDC_USD >= CAP` (175 live). Reaching USDC-leg slot-lane entries
  needs a second prefetched leg at the raised clip (sized separately, operator decision).
- Early quote (`_early_quote_clip`, Alpha call 2): onchain_watch events carry no source mcap (56
  of 56 since 10-01), so the preview cannot know the band. The primary stays today's clip; the
  raised clip is the first alternate, today's regime clip the second (at most 2). The alternates
  run on their own thread beside the primary, and each alternate now quotes on its own thread, so
  the second never waits a round-trip for the first. Live today: a used early quote made the
  executor wait p50 0 ms / p90 0 ms (n=36, 10-06..10-08). Simulated with 100-250 ms quotes (n=40):
  primary done p50 184 / p90 226 ms with one alternate, 175 / 247 with two (noise); last alternate
  done 164 / 232 with one, 199 / 235 with two (one after another it would be ~350).
- Displays: `api_memecoin` next-clip preview and `memecoin_wallet_map.funnel_clips` show the
  ceiling without a coin; they keep today's 175 and gain the above-nano ceiling beside it.

Alpha calls (2026-10-08): proven cells follow the raised global; unproven 50 and trial 20 stay;
out_of_scope resolved at $2M or more gets 200.

## Edge cases (CLAUDE.md rule 5)

1. Money path: yes, funded Solana sizing. Larger clips on above-nano coins when equity allows.
2. Fill path: no network call, lock or retry on the reservation path. The early quote gains one
   alt Jupiter quote on its own alternates' thread (off the critical path) while the bankroll
   clip exceeds 175: one more funded-seam quote per onchain/J7 entry.
3. Concurrency: no shared state; the ceiling is a pure function of the classification and a
   settings constant.
4. Absent, zero, unknown: effective_mcap None (unresolved, assumed_nano) is not above nano. The
   knob at 0 is off. A non-finite or bool mcap is not above.
5. Dependencies: none new. A classification-less caller (manual, legacy) gets None: today's
   ceiling.
6. Units: USD clip; mcap in USD on the band basis (`MEMECOIN_SOLANA_BAND_MCAP_BASIS=onchain`).
7. Sample: 146 Solana automatic entries since 10-01: 79 above nano, 67 nano. Slot lane 90 (65
   USDC leg, 25 SOL), onchain_watch 56. Base clips reached 175 in 19 of the last 25 entries, so
   the 175 ceiling binds often. 200 needs Solana sizing equity of at least $600 at 3 slots (3
   slots from $300); below that the raise does not bind. Exposure: 3 Solana slots at 200 is $600
   of the $700 global `MEMECOIN_MAX_TOTAL_EXPOSURE_USD` (was $525), so the global cap binds
   sooner across chains. The SOL exit reserve is per position, not per dollar, so it is
   unchanged.
8. Rollback: `MEMECOIN_SOLANA_MAX_CLIP_USD_ABOVE_NANO=0` and a restart (read at import).

## Addendum: a threshold knob, and 0 = every Solana entry (operator ~15:00Z via Alpha)

Operator, after the second-leg cost: "make the entries larger, whatever works". The second leg is
dropped. `MEMECOIN_SOLANA_RAISED_CLIP_MIN_MCAP_USD` (default 50000 = the rule above) is the resolved
mcap at or above which a Solana entry gets the raised ceiling. 0 = every Solana entry, nano and
unknown mcap included. A negative or non-finite value is treated as 50000 (a typo never widens).

- `solana_clip_ceiling_usd(classification, network_id)`: the raised ceiling when the entry is
  Solana and either the threshold is 0 or its resolved mcap is at or above the threshold. With a
  threshold above 0 this is the band rule (micro and up, out_of_scope resolved) at 50000.
- `solana_flat_clip_ceiling_usd()`: the raised ceiling when the threshold is 0, else None. It is
  for callers that size before the coin is known: the slot lane's `leg_plan` roster clip and its
  USDC reservation and wallet cap, the early quote's primary, and the dashboard.
- Slot lane at threshold 0: the single prefetched USDC leg sizes at the raised ceiling and the
  USDC reservation passes the same ceiling, so the 90-100% leg check holds as it does today. At a
  threshold above 0 the USDC leg and reservation stay at today's clip (unchanged).
- Early quote at threshold 0: the primary sizes at the raised ceiling; the raised alternate is
  not built (it would equal the primary); the regime alternate sizes at the raised ceiling too.
- Unproven 50 and trial 20: unchanged. Today a wallet-capped or trial clip does not match the
  prefetched leg either: the slot lane funds it with SOL when SOL covers it, else hands it to the
  on-demand leg worker (or refuses `slot_lane_scaled_clip`). A 200 leg changes none of that.
- The hook's per-wallet cap now passes the entry's network, so an EVM entry never gets the raise
  at threshold 0.

SOL buffer at cap 200 (`buffer_topup_once`): while the SOL buffer's available USD is under the cap,
one pass buys min(TOPUP_USD, target - held) of SOL with USDC (TOPUP 175), only if free USDC minus
that amount stays at or above MIN_USDC_USD, and only within DAILY_CAP_USD (1750) per UTC day. At
200/200 a refill therefore needs free USDC of at least $200 plus the refill amount.

Edge cases for this addendum (rule 5):
1. Money path: yes. At threshold 0 every Solana automatic entry can size to 200 when equity allows.
2. Fill path: no new call, lock or retry. The early quote fetches one fewer alternate at threshold 0.
3. Concurrency: pure functions of settings constants; nothing shared.
4. Absent: at threshold 0 an unknown mcap gets the raise (the operator's "whatever works"); at a
   threshold above 0 it does not. No classification on a Solana path gets it only at threshold 0.
5. Dependencies: none new.
6. Units: USD mcap on the band basis; the threshold compares resolved `effective_mcap`.
7. Sample: as above (146 entries since 10-01, 67 nano). At threshold 0 nano entries (46%) are also
   raised. Exposure: 3 Solana slots x 200 = 600 of the 700 global cap.
8. Rollback: threshold back to 50000 (or the ceiling knob to 0) and a restart.

## Addendum 2: only the best wallets (operator ~14:20Z via Alpha)

"Raise to 200 only the best wallets on Solana." `MEMECOIN_SOLANA_RAISED_CLIP_WALLET_IDS`, its own
list in the full-cap list's format (bare id = every chain, `id@chain`), read by the same 30 s
operator refresh. Unset or empty: every wallet, per the mcap threshold (as before). Set: only the
listed wallets take the raised ceiling, still subject to the threshold (0 live); every other wallet
is back at 175. All-malformed lifts nobody. Sizing never reads the re-entry list. Unproven 50 and
trial 20 unchanged. The operator's list: Degen fb0c112f, Thokani e55a4d6f, Pasterniq b55de48d,
jpegkite 64db98ca, fmpumpguy 6e6d68f3, ResellCalendar ff03cead.

`solana_clip_ceiling_usd(classification, network_id, source_user_id)` returns None for a wallet
the list does not name. A caller that passes no wallet gets None while the list is set (it never
widens). `solana_flat_clip_ceiling_usd()` (the ceiling for every entry, used by callers that size
before the wallet is known) exists only at threshold 0 with the list unset.

Slot lane: one prefetched USDC leg cannot match both 175 and 200, and a second leg is out (Jupiter
load). Measured, funded Solana slot-lane entries 10-01T14:20Z..10-08 (n=87): listed 32 (10
SOL-funded, 22 USDC leg; Degen 12, jpegkite 10, ResellCalendar 6, fmpumpguy 2, Pasterniq 2, Thokani
0); unlisted 55 (13 SOL, 42 USDC leg). The leg stays sized for the unlisted majority (175: no flat
ceiling while the list is set). A listed wallet takes 200 on the SOL try when native SOL and the
buffer cap cover 200; otherwise it falls back to today's path at 175, the prefetched leg or SOL at
175, never the on-demand leg (Alpha call (a): speed over $25). The trace names that fallback
(`raised_clip_fallback`, with the reason). Simulated on the 87 entries (buffer 200, refill
min(175, 200 - held) per 30 s pass, daily 1750, bankroll >= 200): listed 31 of 32 at 200 on SOL, 1
falls back (3%); unlisted unaffected. The on-demand leg the fallback avoids costs pump leg_ms p50
148-179 / p90 199-225 ms (n=35) plus a requeue and a second gate pass; the prefetched leg reaches
send_call p50 6 / p90 20 ms after the receipt (n=59).

Early quote: the primary is the wallet's own clip: at threshold 0 a listed wallet's primary carries
the raised ceiling and an unlisted one's does not; above 0 the raised alternate is built only for a
wallet the list allows.

Edge cases (rule 5):
1. Money path: yes, sizing. It narrows the raise to six wallets.
2. Fill path: no network call, lock or retry. One frozenset membership per sizing call.
3. Concurrency: the refresh thread swaps one tuple; readers read it once.
4. Absent, empty, malformed: unset or empty = every wallet (threshold rule); all-malformed = nobody;
   no wallet id while set = no raise.
5. Dependencies: a failed refresh keeps the last list; before the first refresh, the startup value.
6. Identity: roster ids, exact; `id@solana` scopes to Solana; EVM never takes the raise anyway.
7. Sample: listed wallets were 32 of 87 slot-lane entries and the SOL buffer is shared with
   unlisted SOL-funded entries; a burst of listed entries inside a refill gap falls back to 175.
8. Rollback: empty the list in `.env` (live within 30 s), or the ceiling knob to 0 and a restart.
