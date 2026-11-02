# Memecoin per-chain evidence reset and Chains workspace — design

**Date:** 2026-07-30
**Status:** proposed
**Supersedes:** nothing. Extends `2026-07-28-memecoin-paper-decision-integrity-design.md`
(measurement-only paper lab) and depends on the executable-fill change shipped
2026-07-30 (`jupiter-executable-route-v1`).

---

## Why

Three operator goals, in the operator's words:

1. See **performance per chain** on paper, for research.
2. Build **different curated wallet lists per chain**.
3. Eventually deploy **chain-specific — and possibly trader-specific — strategies**
   on the funded runner.

None of the three is reachable today, for one reason: until 2026-07-30 the paper
lab priced entries off a DexScreener mid with a modeled impact floor, and where no
DexScreener pair existed it fell back to the fomo feed price. On Robinhood 4663
that produced **13,530 fomo-priced entries and zero usable ones**. Every per-chain
number computed over that history is a measurement artifact, not a chain property.

The operator's own constraint governs this design:

> "You can't tell me different coins should have different strategies on a basis of
> we didn't properly track one or the other."

So the work is in two phases. Phase 1 discards the unusable history and starts a
clean evidence era. Phase 2 builds the surface that reads it per chain.

---

## Phase 1 — Evidence reset (destructive)

### Decision

**Hard delete all paper positions and their dependent rows. No archive.**

This was chosen by the operator over three less destructive options (archive
in place, move to side tables, delete-with-DB-backup) after the trade-offs were
stated. The cost is accepted and recorded here: **the 2026-07-23 combo_f partial
de-risk study, the 2026-07-27 peak_lock_e analysis, and the 2026-07-30 rails
verification become permanently unre-runnable.** Any future question of the form
"was the old conclusion wrong, or only measured wrong?" cannot be answered from
the ledger after this runs.

A pre-operation SQLite backup is still taken. That is not an archive — it is the
standard rollback for a destructive migration, required by the runtime rules in
`CLAUDE.md`, and it is discarded on the normal backup rotation.

### Scope

| Table | Action | Rows | Rationale |
|---|---|---|---|
| `memecoin_positions` | delete where `execution_mode='paper'` | 23,460 | includes the 165 still open |
| `memecoin_fills` | delete rows whose `position_id` is deleted | — | dependent evidence |
| `memecoin_entry_snapshots` | delete rows for deleted positions | — | dependent evidence |

Explicitly **kept**:

| Table | Rows | Why |
|---|---|---|
| `memecoin_positions` where `execution_mode='real'` | 100 | funded money history; never in scope |
| `memecoin_wallets` | 170 | the roster. Deleting it would erase arming state and exclusions |
| `memecoin_events` | 44,211 | the raw signal feed and its cursor domain |

Funded contract audit rows, operator overrides, and the exclusion flags
(`memecoin_wallets.excluded`, permanent per existing governance) are untouched.

### The 165 open positions

The operator chose "force-close all 165 at cutover". Under hard delete this is
**redundant and is not implemented**: a forced close writes an exit row and a fill
that the same transaction then deletes. The positions are deleted directly. The
observable outcome is identical — nothing from the old era is open after cutover —
with two fewer synthetic writes.

If forced closes are later wanted for a reason other than clearing the book (for
example to observe exit behaviour one last time), that is a separate, non-destructive
task and must run *before* the purge.

### Verified preconditions

- `memecoin_positions.id` is `INTEGER PRIMARY KEY AUTOINCREMENT`;
  `sqlite_sequence.seq = 23867`. New rows therefore continue at 23868 and **no id
  is reused**. This matters: `data/memecoin_v6_paper.sqlite` holds a
  `v6_paper_cursors.source_position` cursor at 23728 which tails
  `memecoin_positions.id`. With plain rowids, mass deletion would restart ids below
  the cursor and V6 admission would silently stop forever.
  **`sqlite_sequence` for this table must never be reset.**
- Zero open real positions (verified 2026-07-30).
- Zero pending reservations — re-checked *after* the runner stops, per the funded
  runtime rules.

### Procedure

Paper copy runs inside the funded runner (`memecoin_runner.py:706`,
`memecoin_follow_activity.py:328`), so this is a funded-runtime operation, not a
plain SQL delete.

1. SQLite backup to `data/backups/`.
2. Stop the pinned runner.
3. Re-check zero pending reservations and zero open real positions.
4. Delete, in one transaction, in dependency order: snapshots → fills → positions.
5. Assert post-conditions: paper count 0, real count 100, wallet count 170,
   `sqlite_sequence.seq` unchanged at 23867.
6. Restart the pinned runner; verify heartbeat age < 30s and `auth_ok`.

Rollback is restoring the backup file and restarting.

### What the new era gets for free

Signal attribution — the operator's "register who gave the signal" — **already
exists and needs no work.** Across all 23,460 current paper positions:
`source_user_id` null: 0. `network_id` null: 0. `origin_event_id` null: 139 (0.6%).

Likewise, one event already fans to every arm off a **single shared entry fill**,
with skips shared across arms (`copy_event_to_arms`), so the exit policy is already
the only variable between arms. Phase 1 changes the data, not this behaviour.

---

## Phase 2 — Chains workspace

### Navigation

```
💰 Live │ Chains │ Discovery │ Radar
```

`Chains` opens a chain selector; the funded three are marked:

```
[ ◎ Solana* ] [ 🔵 Base* ] [ 🟡 Robinhood* ] [ BSC ] [ ETH ] [ ⚠ unresolved ]    * funded
```

Removed: the `paper` workspace, the `wallets` workspace, and the `theses` sub-tab.
`Discovery` is promoted from a sub-tab to a top-level tab with its contents
unchanged. `Live` and `Radar` are untouched.

**There is no `All` option, and no view pools chains.** Each chain is its own
story: its own paper results, its own trader ranking, its own arming state. A
pooled number is never computed, never displayed, and never used as a baseline.
This is a deliberate reversal of an earlier draft of this spec, which proposed an
`All` tab as a comparison baseline; the operator rejected pooling outright.

`⚠ unresolved` holds positions whose chain could not be identified
(`network_id='evm-unknown'`, 78 rows pre-reset). It is a data-quality bucket, not
a chain. It is listed separately so those rows are visible rather than silently
dropped, and its contents are never merged into a real chain's numbers.

`Live` continues to show every funded trade across all chains, because it is the
execution cockpit and the operator needs one place to see what is open. It reports
per-chain accounting cards, as it does today, and computes no pooled performance
metric.

### Content per chain

Three stacked tables, all scoped to the selected chain:

1. **Paper results per arm** — `/api/memecoin/paper-report?chain=`, already
   chain-scoped server-side.
2. **Open paper trades** — real positions stay in `Live`; this table is paper only.
3. **Traders** — the wallet × arm matrix, below.

### The traders table

Columns, per the operator's specification:

```
Trader | signals | <one column per arm…> | Total | Armed?
```

Arm columns are built from the arms actually present in the chain's data, not from
a fixed list, so a chain that has never run an arm does not grow an empty column.
`signals` is the count of **distinct `origin_event_id` values** for that trader on
that chain within the matrix's shared-origin set — that is, how many of the
trader's calls produced a comparable across-arm outcome. It is not a count of
positions, which would multiply by the number of arms.

Source is the existing `/api/memecoin/traders/arm-matrix` — which already excludes
fomo-priced fills (`HONEST_COPY_SQL`), already excludes `excluded=1` wallets, and
already sorts by total PnL descending. It needs a `chain` parameter and the
`signals` count.

Two honesty rules:

- **Ranking uses fomo-excluded PnL only.** Ranking 4663 by raw PnL today would put
  a fabricated `$976,791` at the top — the same class of metric inversion fixed in
  `908d3d8c`. Wallets with no honest closes sort last showing
  `no honest evidence (N fomo-priced)`, not a number.
- **Dropped wallets are listed, never silently omitted.** The matrix requires a
  wallet to have ≥2 arms over a *shared origin event*, which is what makes cells
  comparable. Wallets failing that are shown below the table with the reason
  (`only 1 arm`, `no shared origin`). The operator's requirement is every wallet
  copied on that chain appears; the requirement is met without making
  non-comparable numbers look comparable.

`momentum_a` renders as `n/a — no tick tape` on Base, BSC and ETH. It is gated to
Solana and 4663 because a 15-minute arm cannot be priced off DexScreener latency —
it measured $0.00 over 138 closes at 0% WR before the gate. Only those two chains
carry that column.

`Armed?` shows whether the wallet is armed **on this chain**, read from
`memecoin_wallets.real_chains` per Phase 3. Before Phase 3 lands, the column shows
the global approval flag with an explicit `global` marker, so it is never mistaken
for chain-scoped authority.

---

## Phase 3 — Per-chain arming (funded authority)

This is in scope and is the part that must not be got wrong. Today a wallet
approved for Solana is **also armed on 4663 and Base**, and will open real
positions there.

### What is being superseded

`db_memecoin.real_approved_user_ids(conn, network_id=None)` accepts a
`network_id` and deliberately ignores it:

> "Arming is GLOBAL (2026-07-15): an approved wallet is armed on every chain.
> `network_id` is accepted for call-site compatibility but no longer filters —
> routing a buy to a chain we can't execute is refused downstream by the
> venue-context guard in `make_real_hook`, not here."

That 2026-07-15 decision is reversed here. The venue-context guard answers "can we
execute on this chain at all", which is a capability question. It has never
answered "is *this wallet* authorised on *this chain*", which is an authority
question. Capability is not authority.

### Semantics

`memecoin_wallets.real_chains` becomes the authority list: the set of chains on
which that wallet's buys may open a real position.

- **`NULL` or empty means armed nowhere. Fail-closed, no exceptions.** `NULL` must
  never be read as "all chains" — that is the pooled behaviour being removed, and
  a silent default is exactly the confusion this phase exists to eliminate.
- A chain absent from the list is a refusal, logged with a reason, not an error.
- `paused` and `excluded` continue to override everything, unchanged.

### Migration

All 19 currently-armed wallets have `real_chains = NULL` and therefore **disarm on
every chain** at migration. They are re-armed per chain, deliberately, by the
operator.

This is not a side effect to be worked around — it is the only coherent state.
Phase 1 deletes the paper evidence those 19 approvals were justified by. Keeping a
wallet armed on evidence that no longer exists would be arming on nothing.

Operationally it is free right now: `kill_tripped = true` and real open positions
are zero across all three venues, so no funded behaviour changes at the moment the
migration lands.

### Every lane must be checked, not just the entry gate

`real_approved_user_ids` has callers in three distinct classes. Making the shared
function chain-aware changes all of them, so each is classified explicitly:

| Class | Call sites | Required behaviour |
|---|---|---|
| **Authority** — decides whether real money moves | `memecoin_real_runner.py` entry gate and `check_wallet_demotion` | must pass the chain; refuse when absent |
| **Display / telemetry** | `memecoin_universe.py`, `memecoin_entry_signals.py`, `memecoin_position_intel.py`, `memecoin_runner.py` armed context | must show *which* chains, never a bare armed/not-armed |
| **Historical stamp** | `was_real_approved` in `memecoin_capture_v2.py`, `memecoin_shadow_signals.py` | records what was true when written; semantics must not change retroactively |

A caller that cannot supply a chain must fail closed rather than fall back to the
global set.

### Auto-demotion must also be per chain

`check_wallet_demotion` currently revokes `real_approved` outright after a wallet's
real losses cross the threshold. Under per-chain arming it must revoke **only the
chain the losses occurred on**. Otherwise a wallet that fails on 4663 is silently
disarmed on Solana too — pooling reintroduced through the back door, in the one
place where it moves real money.

The demote cohort (`wallet_real_cohort_start`) becomes per (wallet, chain) for the
same reason: it takes `network_id`, and `memecoin_wallet_approvals` gains a `chain`
column so a `chain_revoke` / `chain_arm` pair anchors that chain's cohort. Without it, a
chain re-armed after its own demotion is still judged on the losses that caused the
demotion and re-fires on the very next close.

### Arming is one transactional operation

`set_wallet_chain_armed(conn, wallet, network_id, armed)` is the only correct way to
change funded authority. It canonicalises the chain, adds or removes exactly that one
chain, and writes the approval flag, the authority list and the audit row in a single
transaction.

Two failure modes make this mandatory rather than stylistic:

- **A client-supplied `chains` list replaces the stored list.** Arming a second chain
  silently disarmed the first.
- **Splitting the flag and the list across two commits** can leave `real_approved=1`
  with `real_chains=NULL`. Current code reads that as armed *nowhere*; the pinned funded
  release reads it as armed on *every* chain. A half-written arm must be impossible.

### Chain identity is the funded network id

`real_chains` stores what the entry gate compares against — `solana`, `8453`, `4663` —
not the UI's `robin`/`base` spellings. `canonical_chain_id()` maps any alias to that
identity, applied on write *and* on read so legacy rows still resolve. Storing the UI
spelling made a wallet render as armed while `real_approved_user_ids('4663')` returned
nobody, so it could never execute.

### Out of scope for this phase

Per-*trader* strategy policies — "trader X is good with arm Y, so run arm Y for
their signals". The per-chain trader matrix from Phase 2 is the evidence that
would justify it. Building the policy layer is a later project and must not be
inferred from an empty post-reset ledger.

---

## API work (Phase 2)

| Endpoint | Change |
|---|---|
| `/api/memecoin/traders/arm-matrix` | add `chain`, add per-wallet signal count |
| `/api/memecoin/positions/closed` | add `chain` |
| `/api/memecoin/positions/open` | add `chain` |

`positions/closed` is not optional. It returns the last 200 rows globally, so
client-side filtering gives each chain an arbitrary slice — in the current window
those 200 are `4663:126, solana:64, 56:7, 8453:3`, meaning a Base tab would show
**3 closed trades out of 1,147**.

One canonical chain resolver is required. Today `paper-report` speaks `CHAIN_IDS`
aliases (`robin`, `bsc`) while `wallet_recommendations` matches raw `network_id`
(`4663`, `56`). Passing one vocabulary to the other endpoint returns the wrong
chain's rows with no error.

## Empty and thin states (Phase 2)

After Phase 1 every chain is empty and must say so honestly. A chain with no rows
shows the counts, not a blank table:

```
ROBINHOOD 4663 · no evidence in this era yet
0 closed · 0 wallets comparable · era started 2026-07-30
```

An empty chain is never filled by borrowing another chain's rows.

---

## Out of scope

- Per-trader strategy policies. The Phase 2 matrix is the evidence that would
  justify them; building the policy layer is a later project.
- Tape coverage for Base/BSC/ETH, which is what would un-gate `momentum_a`.
- Any change to `Radar` or to funded execution rails. `Live` changes only insofar
  as it must not present a pooled performance number.

---

## Testing

- **Phase 1** — post-condition asserts inside the migration (paper 0, real 100,
  wallets 170, `sqlite_sequence` unchanged at 23867), plus runner heartbeat
  freshness after restart.
- **Phase 2** — one test per new `chain` parameter proving a chain-scoped read
  returns only that chain; a regression test that `positions/closed?chain=`
  returns a chain's own rows rather than that chain's share of a global 200-row
  window; a test that no endpoint or view accepts an "all chains" scope.
- **Chain resolver** — each accepted alias maps to the same row set on both the
  report and matrix paths.
- **Phase 3** — the critical suite, one test per lane:
  - a wallet armed only on Solana is refused a 4663 entry, and the refusal is
    logged with a reason;
  - `real_chains = NULL` refuses on every chain (fail-closed), and is never read
    as "all";
  - auto-demotion triggered by 4663 losses leaves Solana arming intact;
  - every authority call site fails closed when no chain is supplied, rather than
    falling back to the global set;
  - `was_real_approved` historical stamps are unchanged by the migration.

---

## Amendment — 2026-08-03: Discovery, the funded-authority board, and the arming gate

This spec said `Discovery`'s contents were unchanged and `Live` was untouched. Both
statements are now out of date, and the arming gate did not in fact follow the
per-chain rule this document sets out. Recorded here so the spec and the code agree.

### The gate was still pooled (the defect this spec should have caught)

Phase 3 made *authority* per chain but left the *evidence that grants it* global:
`real_approval_preview()` called `alpha_b_copy_stats()` with no chain, so a wallet
qualified for any chain on its best chain's record. Reproduced: Robinhood armed off
five Solana closes with zero Robinhood closes. That is exactly the pooled baseline
the "no view pools chains" rule forbids — the rule was applied to the displays and
missed on the gate.

**Corrected.** `alpha_b_copy_stats(chain=)` expands to every network id the chain is
stored under, `real_approval_preview(chain=)` scopes the gate, and
`set_wallet_chain_armed` passes the chain being armed. The legacy
`{approved, chains:[...]}` path requires **every** named chain to qualify on its own
evidence. Refusal reasons name the chain.

### Funded authority exists only for funded chains

The selector offered BSC, Ethereum and `unresolved`, and the API accepted every
resolved key, so authority rows could be written for `56`, `1` and `evm-unknown`.
Venue guards blocked execution, which made it look harmless — it is latent
authority that would activate silently if a venue were ever enabled.

**Corrected.** Arming is restricted to `funded_chain_keys()` in both write paths
(`set_wallet_chain_armed`, `set_wallet_real_chains`) and in the board. Disarm is
still allowed on any chain so pre-existing rows can be cleaned up. `unresolved`
remains a data-quality bucket, never an armable chain.

### Configured authority equals effective arming

`armed` (approved AND configured AND not paused AND not excluded) was used as the
button state while `armed_chains` held configured authority, so a paused wallet
already configured for Solana rendered "arm".

**Corrected 2026-08-07.** Pause and exclusion now clear the approval flag and chain list.
The board keeps `armed_here` and `effective` for API compatibility, but they cannot diverge.
A paused wallet shows `resume + arm` and has no stored funded authority.

Note `armed_chains` holds stored **network ids** (`4663`), while the UI and this
spec speak canonical **keys** (`robin`). Comparing the two directly matches only
Solana; `armed_chain_keys` is exposed alongside for labelling.

### Live gains an `Armed` sub-tab; Discovery drops its chain splits

- **Live → Armed** (new, per funded chain): who may open real positions on this
  chain, the chain-scoped gate number that grants it, a descriptive best paper arm,
  the funded record on that chain, and arm/disarm. It lists every curated wallet —
  disarming the last chain clears `real_approved`, and a board filtered on that flag
  dropped the wallet it told you to re-arm.
- **Discovery → Candidates / Curated** are explicitly chain-independent. A candidate
  is a person; which chain they are worth copying on is decided later from per-chain
  evidence. Neither table filters by chain, and the shared chip bar is blanked on
  them so a stale chip cannot read as an applied filter.
- **Curated has no funded-authority control.** It had one that fell back to whichever
  chain the Chains selector last held — invisible from a screen with no chain shown.
  Granting authority now requires an explicit funded chain from the caller.

### Testing (added)

`tests/test_memecoin_funded_authority_scoping.py` — one case per defect above:
cross-chain qualification refused; every spelling of a chain counted; research and
`unresolved` chains unarmable but disarmable; last-chain disarm keeps the wallet
listed and re-armable; pause clears configured and effective authority;
`last_signal` scoped to the requested chain; `armed_here` compared on stored ids.
