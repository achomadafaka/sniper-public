# Memecoin winner re-entry size ladder (rule-5 spec)

Date: 2026-09-28. Author: Beta. Order: operator via Alpha (20:4xZ): re-enter a coin we closed at a
profit when the same or any other armed wallet buys it again, each re-entry sized down.
Ladder DECIDED by Alpha under the operator's delegation (20:5xZ): 1st re-entry 0.5x clip, 2nd
0.25x, at most 2 re-entries per coin per risk day, only after a closed WIN; the losing-re-entry
block stays. Knob default OFF; Alpha activates after Lambda's review, with Gamma's STOP line.

## 1. What exists today (read from code and the live `.env`)

Most of this feature already exists and has been switched off since 2026-09-16.

- `MEMECOIN_REAL_REENTRY_COOLDOWN_S` (code default 86400, `.env` 86400). The risk gates
  (`memecoin_risk.py` Solana, `memecoin_evm_risk.py` EVM) refuse "re-entry cooldown" for any coin
  whose most recent REAL close is inside the window. The set comes from
  `db_memecoin.recently_closed_real_coin_ids`.
- `MEMECOIN_REENTRY_WINNER_UNBLOCKED` (code default 1, `.env` **0** since 2026-09-16 17:56Z,
  operator: "can be coin flipping"). Reader: `recently_closed_real_coin_ids`. When 1, a coin whose
  most recent real close had `realized_pnl > 0` leaves the cooldown set. It also enables the two
  winner guards in `real_entry_hook` (`memecoin_real_runner.py` ~:3464):
  - `MEMECOIN_REENTRY_WINNER_DAILY_CAP` (default 2): refuses when the coin already has that many
    real positions opened in the current UTC day, the first entry included.
  - `MEMECOIN_REENTRY_WINNER_MAX_ABOVE_EXIT_PCT` (default 10): refuses when the chart price is
    more than 10% above our route-realized exit unit price.
- `MEMECOIN_REAL_ONE_POSITION_PER_COIN=1`: refuses while the coin is held or reserved. It blocks
  concurrent holds only, never a sequential re-entry. The INSERT-level unique index in
  `reserve_entry` stays on regardless.
- `MEMECOIN_REAL_BLOCK_LOSING_REENTRY` (default 1) with `MEMECOIN_REAL_LOSING_REENTRY_HOURS=24`:
  refuses "losing re-entry" when the most recent real close lost. **Solana only**: the EVM gate
  has no losing set. On EVM the 24 h cooldown covers losers instead, because
  `recently_closed_real_coin_ids` keeps every coin whose last close did not win. Unchanged by
  this spec.

So `MEMECOIN_REENTRY_WINNER_UNBLOCKED=1` alone restores full-size winner re-entries (the
08-27..09-16 regime). The only missing piece is the size ladder.

## 2. Evidence, both directions (in-sample, before the current rails)

Funded history, 07-13..09-16, automatic entries only (manual excluded; 111 of the 187 raw winner
re-entries were manual). A winner re-entry = a real position whose coin had a real close in the
24 h before its `opened_at`, the most recent of which won. Scratchpad
`winner_reentry_ladder.py`, read-only, 2026-09-28 20:4xZ:

- All automatic winner re-entries: n=76, +$265.53, 95% CI [-$201.25, +$759.70], 45/76 wins,
  +4.4% of notional. First entries in the same span: Solana +3.0% (n=587), 4663 +1.2% (n=280),
  56 +17.6% (n=13).
- By chain: Solana n=48 +$436.43 [+$55.01, +$833.07] (+11.5%); 4663 n=26 -$97.02
  [-$333.61, +$114.56] (-4.6%); 56 n=2 -$73.89.
- By wallet: same wallet n=38 +$8.08 (+0.2%); other wallet n=38 +$257.45 [-$37.87, +$591.19]
  (+10.0%).
- By rank: 1st n=66 +$264.58; 2nd n=7 +$16.30; 3rd+ n=3 -$15.35.
- The same rows with the ladder applied linearly (0.5x, 0.25x, 3rd refused): +$136.36.
- Loser re-entries (blocked by this design anyway): n=16, -$234.18 [-$413.63, -$78.28].

Against it:
- Gamma, refusal captures: after our win, different wallet, n=8, mean -4.3% [-23.1, +13.7].
- Lambda 2026-09-28 10:41Z, Solana `reentry_cooldown` refusal captures whose last close WON:
  n=46, +0.6% [-6.7, +7.3]. The captures enter ~6 pp cheaper than our real fills.
- Operator's 09-16 evidence (manual included): winner re-entries Robinhood -$248/34, BSC -$74/2,
  Solana +$250/170.

None of these CIs excludes zero except Solana's funded row, which is in-sample under old rails.
The linear ladder number ignores that a smaller clip also has less price impact, which favours it
slightly.

## 3. Design

### 3.1 Knob

`MEMECOIN_REENTRY_WINNER_SIZE_LADDER`, code default `""`.

- `""` (unset): no ladder. Behaviour is exactly today's: winners are cooled while
  `MEMECOIN_REENTRY_WINNER_UNBLOCKED=0`, and re-enter at full size if someone sets it to 1.
- `"0.5,0.25"`: the decided ladder. Factor k applies to the k-th re-entry. A coin at rank
  `len(ladder)+1` or higher is refused: "winner re-entry ladder exhausted".
- Malformed (a non-number, a factor <= 0 or > 1, an empty item): the ladder has no steps, so
  every winner re-entry is refused as exhausted. It fails CLOSED to today's cooled behaviour. It
  never falls back to full size. The parse error logs once at import.

Activation writes BOTH lines, ladder first:
`MEMECOIN_REENTRY_WINNER_SIZE_LADDER=0.5,0.25`, then `MEMECOIN_REENTRY_WINNER_UNBLOCKED=1`, plus
`MEMECOIN_REENTRY_WINNER_DAILY_CAP=3` (see 3.3). Rollback: `MEMECOIN_REENTRY_WINNER_UNBLOCKED=0`
plus a restart. Half-applied, ladder present and UNBLOCKED=0: inert, winners stay cooled.

The dashboard overlay (`memecoin_entry_knobs.py`, group "Re-entry") can flip
`MEMECOIN_REENTRY_WINNER_UNBLOCKED`. With the ladder line absent that toggle restores full-size
re-entries, which is why activation writes the ladder first. The ladder is NOT added to the
overlay.

### 3.2 Win, rank, and the window

- **Win** = the most recent REAL close on the coin identity (`coin_identity(network_id, mint)`)
  has `realized_pnl > 0`. This is the definition the cooldown already uses. `0.0` is breakeven,
  not a win. `NULL` (unpriced close) is unknown, not a win: the coin stays cooled. Manual closes
  count: they are real money on the coin.
- **Whole campaign vs last position.** Under the current knobs they are the same thing. A losing
  close blocks the coin for 24 h (cooldown on EVM, cooldown plus losing block on Solana), so every
  close inside the window before a permitted re-entry is a win. The spec therefore keeps the
  existing last-close rule, and a test pins the equivalence.
- **Partial exits.** A de-risk partial exit leaves the position open (`is_open=1`), so
  one-per-coin refuses any re-entry until the whole position has closed. The full close writes
  `realized_pnl` for the whole position, partial legs included. "Win" is judged on that total.
- **Rank** = the number of our REAL positions on the coin that closed inside
  `MEMECOIN_REAL_REENTRY_COOLDOWN_S` (86400 s) before the decision. Rank 0 = a first entry, which
  keeps full size and today's gates. Rank k >= 1 is the k-th re-entry.
- **"Per risk day".** The ladder counts on the ROLLING 24 h window, which is the same window
  that decides whether an entry is a re-entry at all, and it has no midnight reset. A re-entry at
  00:05Z after a 23:55Z win is rank 1, not a fresh first entry of the new day. The UTC-day belt
  is the existing `MEMECOIN_REENTRY_WINNER_DAILY_CAP`, set to 3 at activation (1 first entry + 2
  re-entries per UTC day). Corner, NOT handled: a chain of wins longer than 24 h lets the oldest
  close age out of the rolling window, so a further 0.25x re-entry is admitted, still under the
  UTC-day cap.

Query (one index range, `idx_mc_pos_mode_open_closed`, plan verified):

```sql
SELECT realized_pnl, closed_at FROM memecoin_positions p
WHERE p.execution_mode='real' AND p.is_open=0 AND p.closed_at >= ? AND p.mint=?
  AND IFNULL(<coin_network_sql p.>, '')=IFNULL(?, '') ORDER BY p.closed_at
```

The threshold is built in Python, never with SQLite `datetime()` (the space-separator trap).

### 3.3 Where the factor applies (one seam, already live)

The ladder multiplies the funded **risk_scale**. The market regime has used that seam since
2026-07-18 (`regime_risk_scale`, `scaled_funded_clip`, contract `sizing.risk_scale`), and every
thin or narrow-regime entry already exercises it on both chains.

1. `classify_funded_entry` (`memecoin_market_state.py`) computes
   `risk_scale = regime_risk_scale(...) x ladder_factor` and records `reentry_rank` and
   `reentry_factor` in `facts`. It does this only when the ladder is configured,
   `MEMECOIN_REENTRY_WINNER_UNBLOCKED=1` and the signal is not `manual`; the two facts are written
   only for rank >= 1 with a winning last close. Otherwise the factor is 1.0, and with the knob off
   the rank query does not run and the classification JSON is byte-identical to today's. The funded
   contract then seals the scaled clip with no contract-shape change:
   `approved_clip_usd <= max(base x risk_scale, floor)` already holds.
2. `reserve_entry` and `reserve_entry_evm` recompute the rank UNDER the `BEGIN IMMEDIATE` lock
   (policy path only, `include_reentry_policy=True`):
   - rank > ladder length: refuse "winner re-entry ladder exhausted: N re-entries in the window
     (max M)";
   - the factor differs from the classification's `reentry_factor`: refuse "winner re-entry rank
     changed since classification". This is the race where a held position on the coin closes
     between classification and reservation.
3. The slot lane needs no new code. Its prefetched leg is sized at the market scale, and a
   re-entry's `classification.risk_scale` differs from that scale. The existing check returns
   `SCALED_CLIP_REASON` before reserving. `slot_lane_outcome` reads that as `failed`, so the
   on-chain lane gets ONE ordinary attempt through `real_entry_hook`. The winner price guard and
   the daily cap then apply. Cost: a slot-lane-sourced re-entry fills at the on-chain lane's
   speed, not S+1.
4. `_early_quote_clip` (`memecoin_real_runner.py`) sizes its early quote with the same factor, so
   a re-entry does not pay a second quote.
5. `real_entry_hook`'s winner guard runs `last_real_close_for_coin` (measured p50 1.007 ms, p90
   1.060 ms) on EVERY non-manual entry once UNBLOCKED=1, first entries included. It is gated on
   `classification.facts["reentry_rank"] >= 1`, so first entries pay nothing for it.

### 3.4 Clip floor

`scaled_funded_clip` returns `max(MEMECOIN_MIN_TRADE_FLOOR_USD, base x scale)`, floored to the
cent (floor $5, code default). A ladder clip below $5 is raised to $5, never refused, the same
as the regime scale. At the Solana cap ($175) the 2nd re-entry is $43.75. A base clip already
below the floor still refuses with "below min-trade floor", as today. EVM per-chain clip caps and
the route-capacity clip apply before the scale, so they shrink the re-entry further and never
grow it.

## 4. Edge cases (CLAUDE.md rule 5, in order)

1. **Money path: YES.** It changes entry admission and size on every funded chain. Hence this
   spec before code, default off, Alpha's approval and Lambda's review.
2. **Speed on the fill path.** Knob off: zero added work, the rank query is skipped. Knob on: one
   rank query per funded classification, only for coins whose last close is in the window,
   measured on the live ledger read-only, 400 calls: **p50 0.013 ms, p90 0.014 ms**, max
   0.072 ms. The same query runs again under the reservation lock, same cost. Step 3.3.5 REMOVES
   an existing +1.0 ms (p50 1.007, p90 1.060) that UNBLOCKED=1 alone would add to every entry.
   Cost on re-entries only: slot-lane re-entries move to the on-chain lane (see 3.3.3).
3. **Concurrency and global state.** Two lanes re-entering the same coin: both classify at the
   same rank, `BEGIN IMMEDIATE` serialises them, and the partial unique index lets one reserve.
   The other gets "one position per coin: lost reservation race" or the held refusal. The rank
   is re-read under the lock (3.3.2). No module attribute is mutated. The ladder is parsed once
   at import, an immutable tuple.
4. **Absent vs zero vs unknown.** `realized_pnl` NULL = unknown = not a win (stays cooled). 0.0 =
   breakeven = not a win. Ladder `""` = not configured (today). Malformed = zero steps (refuse),
   never full size. `reentry_rank` absent from facts (classification from an older release) is
   read as rank 0 by the hook guard and as "re-read under the lock" by the reservation, which
   refuses on mismatch.
5. **Dependency failures.** One local SQLite read, no provider. If the rank query raises inside
   classification, the classification read fails as a whole (`classification_read_failed`, as
   today). Under the lock, a raise rolls back and refuses. Nothing swallows an error into rank 0.
6. **Units and identity.** Coin identity = `coin_identity(network_id, mint)` through
   `coin_network_sql`, the same as the cooldown set. Rank counts positions, not fills. The window
   is in seconds, `MEMECOIN_REAL_REENTRY_COOLDOWN_S`. Factors are fractions (0.5), not percent.
7. **The sample.** Section 2's re-entry rows exist only because we entered and won first, so they
   describe coins we already liked. It is in-sample, under rails that have changed since (chase
   cap 30, band policies, coin-age and turnover floors). Gamma owns the STOP line on forward
   ladder fills.
8. **Rollback.** `MEMECOIN_REENTRY_WINNER_UNBLOCKED=0` plus a restart restores today's
   behaviour. Open re-entry positions keep their sealed contract and rails; nothing re-sizes. A
   half-applied `.env` (ladder only) is inert.

Also mapped:
- **Which sell mirrors a re-entry.** The re-entry position's `source_user_id` is the wallet whose
  buy triggered it. `real_mirror_on_sell` matches `(source_user_id, mint)`, so only that wallet's
  sells mirror it. The first round's source does not, unless it is the same wallet.
  NOT handled: a SELL from the same wallet's earlier round that the feed delivers AFTER our
  re-entry fill would mirror-close the new position. Measured over the 76 automatic winner
  re-entries: 3 mirror exits, none within 30 s of the fill.
- **Re-entry while a position is open.** One-per-coin refuses it (held set), unchanged. The
  ladder never runs.
- **Daily loss latch and drawdown pause.** Both run first in the gate and refuse every automatic
  entry, re-entries included. Unchanged.
- **Other gates.** Chase, mcap, volume/turnover, coin-age, sell-share, post-peak and the winner
  price guard (+10% over our exit) all run as for any entry. The ladder sits at the reservation,
  after them.
- **Manual.** Manual entries pass `include_reentry_policy=False` and signal `manual`: no ladder,
  no rank query, the operator's size.
- **Refusal families.** `memecoin_refusal_capture` gains `winner_reentry_ladder` for the two new
  reasons, so captures do not file them under `other`.

## 5. Checks left behind

`tests/test_memecoin_winner_reentry_ladder.py`, red on today's code:
- rank 1 reserves at 0.5x and rank 2 at 0.25x of the unscaled clip; rank 3 refuses "ladder
  exhausted"; a losing last close stays refused; `manual` is unscaled;
- a malformed ladder refuses and never sizes full;
- a close landing between classification and reservation refuses "rank changed";
- an unset ladder with UNBLOCKED=0 leaves today's cooldown refusal byte-identical;
- the rank query's SQL uses `idx_mc_pos_mode_open_closed` (EXPLAIN QUERY PLAN), so a later
  schema change cannot turn it into a scan on the fill path.

The ladder constant is built from `os.getenv` at import, which is empty under pytest (the known
trap). The tests set it through `monkeypatch` on `sniper.settings.memecoin` and assert the value
is live before asserting behaviour.

## 6. Amendments (Alpha, 2026-09-28, approved with the build)

- **A. Chain scope.** `MEMECOIN_REENTRY_WINNER_CHAINS` (csv of canonical chain ids, aliases
  accepted; empty = no chain). A winner is unblocked, ranked and laddered only on a listed chain.
  Every other chain keeps today's 24 h cooldown byte for byte, whatever
  `MEMECOIN_REENTRY_WINNER_UNBLOCKED` says. The check lives in `recently_closed_real_coin_ids`
  and `winner_reentry_step`. Alpha activates `solana` only; EVM re-entries showed 4663 n=26
  -$97 and 56 n=2 -$74.
- **B. Stale mirror.** A SELL mirrors a re-entry position (contract
  `classification.facts.reentry_rank >= 1`) only if the sell's own `created_at` is not older than
  the position's decision start (`opened_at`). An older sell returns `stale_sell` and the rails
  stay active. First entries are untouched. An unreadable time on either side mirrors as today.
  Known cost: a round-2 sell the source made BEFORE our decision start (we were slower than the
  source's dump) no longer mirrors either; the stop and the other rails handle that position.
- **C.** The rolling 24 h rank plus `MEMECOIN_REENTRY_WINNER_DAILY_CAP=3` stand as written.
- **As built (Beta):** `reentry_rank`/`reentry_factor` are recorded for EVERY in-scope winner
  re-entry, with factor 1.0 when no ladder is configured. Section 3.3.1 said "only when the ladder
  is configured"; recording them always keeps the hook's price guard and daily cap running for an
  unladdered winner re-entry, because that guard is now gated on the mark (3.3.5).

Activation order: `MEMECOIN_REENTRY_WINNER_SIZE_LADDER=0.5,0.25`,
`MEMECOIN_REENTRY_WINNER_CHAINS=solana`, `MEMECOIN_REENTRY_WINNER_DAILY_CAP=3`, then
`MEMECOIN_REENTRY_WINNER_UNBLOCKED=1`. Rollback: `MEMECOIN_REENTRY_WINNER_UNBLOCKED=0` plus a
restart.

## 7. Review fixes (Lambda F1-F4, Alpha's order)

- **F1.** The stale-sell guard compares the sell's `created_at` with the ORIGIN BUY the re-entry
  copied (`memecoin_entry_attempts.source_created_at` via the position's `attempt_id`, the same
  feed clock), not with our `opened_at`. A round-2 sell made between the source's round-2 buy and
  our decision start therefore still mirrors, which removes the cost stated in amendment B. A
  missing attempt or time mirrors as today.
- **F2.** The ladder refusal under the reservation lock runs whatever `include_reentry_policy`
  says. A cooldown-exempt wallet (`memecoin_real_leg` `_apply_cooldown=False`) is sized by
  classification like any other, so an exhausted or malformed ladder must refuse it too, or it
  re-enters at full size. Manual entries are excluded inside `winner_reentry_step`. An operator
  override (`override_risk_caps`) skips it, like every other cap.
- **F3, intended.** The hook's price guard and daily cap now run only on a classification marked
  `reentry_rank`, which means a winning close inside the cooldown window on an in-scope chain.
  Before, with UNBLOCKED=1 they also fired on a coin whose last close won days ago. Outside the
  window an entry is a first entry by this spec's definition, so neither guard applies.
- **F4.** `test_the_early_quote_clip_is_the_ladder_clip`: at rank 1 the early quote is sized at
  0.5x, the clip the reservation approves.
