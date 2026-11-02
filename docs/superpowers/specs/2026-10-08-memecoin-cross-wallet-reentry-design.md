# Memecoin cross-wallet re-entry for full-cap wallets

Operator order 2026-10-08 ~10:00Z (brain 30822fa2), scoped by Alpha. No study; operator judgment.

## Case

LILMET `D75KtdgF…` on Solana. Degen (`fb0c112f`) position 71958 closed +$43.97 at 09:19:50Z.
ResellCalendar (`ff03cead`) bought at 09:29:37Z. The on-chain lane refused it with "re-entry
cooldown: a real position on this coin closed within the window" (`memecoin_risk.py:486`, set from
`recently_closed_real_coin_ids`, 86400 s window). The operator wants that buy taken. Degen buying
its own coin again stays blocked.

## Rule

Let W be the wallet whose buy is entering, and the chain the entry's chain (`solana` or the EVM
network id). W is listed when `MEMECOIN_REAL_FULL_CAP_WALLET_IDS` names W bare (every chain) or as
`W@chain` (that chain only). This is the same parser and the same live 30 s operator cache
(`memecoin_wallet_caps.CACHE`) as the full-cap clip.

When W is listed and `MEMECOIN_REAL_CROSS_WALLET_REENTRY` is on (default on):

- Re-entry cooldown: refuse only when W's own real close on the coin is inside the window. Closes
  by other wallets do not count. A manual-buy position (`source_user_id` NULL, e.g. 71961) counts
  as another wallet. An operator's manual close of Degen's position still carries Degen's id, so
  it still blocks Degen.
- Losing re-entry: refuse only when the coin's most recent real close lost and that close is W's.
  This is never stricter than today. A W loss followed by another wallet's win was not blocked
  before and is not blocked now.

A wallet that is not listed is unchanged. The knob off is unchanged for everyone.

Identity is the roster id (`source_user_id`), compared exactly, never lowercased: `pump:<base58>`
ids are case-sensitive and the list parser only strips. Live data since 09-01: real positions
carry lowercase uuids, NULL (manual buys) and `pump:<addr>` ids; no address in
`memecoin_wallet_addresses` is held by two roster ids, so W's own close always carries W's id.
Only `execution_mode='real'` closes count; `funded_clone` paper rows never block and never count
as another wallet's close.

Money note (operator-accepted, Gamma): the losing family loosens on Solana and 57073. After
another wallet is stopped out at a loss, a listed wallet may buy the same coin minutes later;
today that refuses everyone.

## Where it resolves

The risk modules stay blind to who asked. The leg, the caller that owns policy, resolves listing
into one value: `reentry_closes_by=W` when W is listed, else `None`. The builders
(`build_memecoin_risk_context`, `build_evm_risk_context`) and `preflight_entry`, `reserve_entry`
and `reserve_entry_evm` pass it through. When it is set, the builders keep only the coins W itself
closed (cooldown) or last lost on (losing).

The leg passes a scope, not a finished set. The reservation rebuilds the context under
`BEGIN IMMEDIATE`. A set computed in the leg would be a pre-lock snapshot: if W's own position on
the coin closed between preflight and reservation, a precomputed exempt set would still admit W on
its own coin. A scope is evaluated on the closes read under the lock.

The two closes queries read `id` and `source_user_id` as well (same rows, same index range) and
take `closes_by`: the cooldown read builds coin -> {source_user_id} from its window rows, and the
losing read compares the id on each coin's last close. `recently_closed_real_coin_ids` and
`losing_real_coin_ids` keep their return types; without `closes_by` they return what they return
today.

## Grading marker (Gamma add 1)

An entry the scope admitted says so. Each closes read fills a `lifted` map with the close the
scope removed (the coin's latest close in the window for the cooldown, its last close for the
losing family). The reservation attaches `{family: {position_id, source_user_id, realized_pnl,
closed_at}}` for the entering coin to `RiskDecision.reentry_lift`, and the Solana leg, the EVM leg
and the slot lane write it to the attempt trace as substage `reentry_cross_wallet` (with
`closes_by`). No extra read: the marker comes from the rows the reservation already read. A trace
fault is logged and never blocks the entry.

## Lanes

Taken (every funded lane that applies the cooldown):

- Solana `memecoin_real_leg.real_entry` (preflight + reservation): J7, the on-chain lane,
  follow activity, dip retry, every Solana lane through `real_entry_hook`.
- EVM `memecoin_evm_leg.evm_real_entry` (`reserve_entry_evm`): the RH fast lane (via
  `make_real_hook`), the BSC fast lane, Ethereum chain 1, Base 8453, Ink 57073, every EVM chain.
- Slot lane `memecoin_slot_lane` (its own `reserve_entry` call).
- The runner's early-quote previews (`_early_quote_clip`, `_early_evm_quote_clip`). They evaluate
  the same envelope off the critical path. Without the scope an admitted entry would lose its early
  quote and fill slower.

Left alone, by name:

- Base fast lane step 0 (`memecoin_base_fast_decide`): records only, applies no cooldown.
- The promotion shadow (`memecoin_promotion_lane_loop.reentry_state`): it shadows DISARMED
  wallets. Full-cap wallets are armed. A listed wallet that is disarmed on one chain would read
  stricter in the shadow than funded would be. This is a measurement-only bias, stated here.
- `api_memecoin._setup_exclusion_keys`: a display filter that hides coins from the manual-buy
  setup list. Manual buys skip the re-entry policy already, and a consensus setup has no single
  entering wallet.
- Winner re-entry (`winner_reentry_rank`, `winner_reentry_step`): knobs unchanged (off).

Existing gap, not fixed here: the EVM leg does not apply per-wallet `reentry_cooldown`
exemption rows (`entry_gate_exemptions`); only the Solana leg does.

## Edge cases (CLAUDE.md rule 5)

1. Money path: yes. Entry admission on every funded chain. It widens admission for listed wallets
   only. The operator ordered it; Gamma reviews before merge.
2. Fill-path speed: no network call, no lock, no retry. The list read is one in-memory tuple
   read. The SQL change adds one column to the window read. Measured context build on the live
   ledger before the change (read-only connection, n=200): Solana p50 6.34 ms / p90 16.26 ms,
   EVM 4663 p50 5.83 / p90 15.44, EVM 57073 p50 5.84 / p90 10.49. The after numbers are in the
   build commit.
3. Concurrency: the cap cache's refresh thread replaces `operator_snapshot` as one tuple; readers
   read it once, with no lock. Nothing mutates a module attribute, env var or shared connection.
   The scope is evaluated under the reservation's `BEGIN IMMEDIATE` (see above).
4. Absent, zero, unknown: a NULL `source_user_id` on a close is another wallet. An entering event
   with no `source_user_id` gets no scope, which is today's full blocking. `realized_pnl` NULL is
   not a loss (unchanged). An empty-string id is treated as an id and never equals a real uuid.
5. Dependency failures: a failed list refresh keeps the last authorized ids (existing cache
   behaviour). Before the first refresh the startup value from settings is used. A DB error in
   the builder raises as today, and the reservation fails closed.
6. Units and identity: the chain key is `solana` or the EVM network id string, the same key
   `entry_cap_usd` uses. Coin identity is `(network, mint)` via `coin_identity`, so a close of the
   same mint on another chain does not count (unchanged). `W@4663` does not lift on Solana.
7. Sample: no number is produced. The ceiling is stated, not built: there is no per-coin daily
   cap. Each listed wallet can enter a coin once per 24 h window (its own close blocks it after),
   so N listed wallets can make up to N entries on one coin in 24 h. Manual buys come on top.
8. Rollback: `MEMECOIN_REAL_CROSS_WALLET_REENTRY=0` in `.env`, then a runner restart (the knob is
   read at import). Half-applied: each process reads the knob once at start, so a process
   applies the rule fully or not at all.

Kept unchanged: one position per coin (an open position by any wallet still blocks a second
wallet), the reservation's INSERT-level dedup guard, the override and manual paths, wallet
exemption rows, the winner re-entry knobs (off), and EVM computing the losing set only on 57073.

## Check left behind

`tests/test_memecoin_cross_wallet_reentry.py`: the LILMET case is admitted; Degen buying its own
coin is refused; a wallet that is not listed is refused; `W@4663` does not lift on Solana; a
manual-buy position does not block; an open position still blocks; the losing family follows
the same split; the knob off restores today's refusal; every lane passes the scope; the reservation
names the close it lifted and every reserving lane writes it to the trace; a paper close neither
blocks nor counts; ids compare exactly. 16 mutants of the rule, the wiring and the marker each fail
the file.

## Addendum: its own wallet list (operator order 2026-10-08 ~13:10Z, via Alpha)

The cross-wallet re-entry gets its own list: the operator's per-chain funded top 5 by percent
return plus Degen on every chain. New knob `MEMECOIN_REAL_CROSS_WALLET_REENTRY_WALLET_IDS`, in the
full-cap list's format (bare id = every chain, `id@chain` = that chain; the chain token is
`solana` or the EVM network id, the same token `full_cap_listed` receives from the legs).

- Set and non-empty: `cross_wallet_reentry_closes_by` lists wallets from it INSTEAD of the
  full-cap list. Unset, empty or whitespace: the full-cap list, as today.
- Read through the same 30 s operator refresh (`WalletCapCache.refresh_operator_overrides`), so
  an `.env` change is live without a restart. That refresh runs only while
  `MEMECOIN_REAL_WALLET_CAPS_ENABLED` is on (the runner starts the cache only then; it is on
  live); with it off, the value read at startup holds until a restart.
- Clip sizing never reads it: `cap()` and `entry_cap_usd` keep the full-cap list.
- A malformed entry is dropped with a WARNING (the runner logs WARNING and above) and listed in
  the cache status the heartbeat carries. Valid: no whitespace, at most one `@`, a non-empty id,
  and a chain token `solana` or digits. Dropping only narrows. A knob whose every entry is
  malformed lifts nobody; it never falls back to the full-cap list, which would widen it.

Edge cases (rule 5) for this change:
1. Money path: funded admission, every chain. It changes which wallets get the lift, nothing
   else.
2. Fill path: the lookup is one in-memory frozenset membership; parsing happens at refresh and
   once per distinct value (cached), never per entry. No network call, lock or retry.
3. Concurrency: the refresh thread replaces one attribute (an immutable tuple); readers read it
   once.
4. Absent, empty, malformed: unset and empty fall back to the full-cap list; set-but-all-malformed
   lifts nobody. These are three distinct states.
5. Dependencies: a failed refresh keeps the last list (as the full-cap list does). Before the
   first refresh the startup value is used.
6. Identity: ids compare exactly (case-sensitive `pump:` ids). `Thokani@solana` does not lift on
   4663. An uppercase chain token is malformed and dropped, not silently non-matching.
7. Sample: the cohort marker (trace substage `reentry_cross_wallet`) is unchanged; the list in
   force goes in the heartbeat status, so a graded entry can be tied to the list.
8. Rollback: empty the knob in `.env` (live within 30 s) to return to the full-cap list, or set
   `MEMECOIN_REAL_CROSS_WALLET_REENTRY=0` and restart to switch the lift off.
