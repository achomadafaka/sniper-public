# Memecoin real exit brain — Solana unfreeze PRE-REGISTRATION (v2, frozen)

**Date frozen:** 2026-07-24 · **Status:** FROZEN (Revision B, same day — see §0.1) ·
**Engine:** `exit-brain-v2` · **Features:** `v2`
**Companions:** `2026-07-23-memecoin-real-exit-brain-prereg.md` (v1 — stays authoritative for EVM)
and the dated 2026-07-24 amendment in `docs/superpowers/specs/2026-07-23-memecoin-real-exit-brain-design.md`.

## 0. Attestation and reason for unfreezing

- **Reason (required by v1 §0, and it is not "the numbers would look better"):** Solana is the
  chain where most funded fills occur, and the data gap that forced it to legacy — no swap
  amounts, no slot→time — was closed by read-only capture verified against live mainnet
  (`sniper/services/memecoin_solana_blocktime.py`, `sniper/services/memecoin_solana_amounts.py`;
  commits `7dc9d641`, `bb112f20`). This is a capability unlock, not an outcome-driven retune.
- **No outcome has been read to choose anything below.** No Solana feature has ever been computed
  into a state; no shadow/replay/funded result informed any constant. Funded PnL known to the
  operator was not an input to any definition here. Every constant is `[inherited]` verbatim from
  v1, `[mechanical]`, or `[bound]` (a round factor fixed before Gate 0 runs).
- **EVM definitions are UNCHANGED between v1 and v2.** The global `feature_version` stamp becomes
  `v2` at the implementing PR so rows are totally ordered, and v1-vs-v2 EVM rows remain directly
  comparable because their definitions are attested identical.
- **Merging this file arms nothing.** It does not enable any worker, flag, or feature. The
  activation ladder is §6; every enable stays an operator STOP condition.
- **Unfreezing later:** editing any constant here is a protocol violation — new dated prereg +
  version bump, same as v1 §0.

### 0.1 Revision B (2026-07-24, same day, BEFORE any Gate-0/shadow/replay run existed)

Two changes, both grounded in live mainnet verification performed after Revision A froze and
before any v2 state was ever computed or read (attested: the exit-state tables contain zero
v2 rows at revision time):

1. **S8 depth is REGISTERED, not deferred.** The blocker was "delta-sign rule unverified live."
   It is now verified on mainnet (2026-07-24, read-only endpoint): 13/13 opposite-sign vault-delta
   events carried `Buy`/`Sell`/`BuyExactQuoteIn` instruction logs (pump AMM); 1/1 same-sign event
   carried `RemoveLiquidityByRange2` + `ClosePositionIfEmpty` (a real LP pull on a −68% coin, on a
   different AMM program — the rule is program-agnostic as designed); 43 fee/ATA ops were
   zero/one-sided and correctly excluded. No false positives, no false negatives in the scanned
   windows. Verification classified by log-message ground truth, not by our decoder's own output.
2. **Capture re-registered as PULL enumeration (S12 simplification).** Revision A assumed the
   push subscription; the implementation enumerates `getSignaturesForAddress` on the position's
   coin VAULT with an `until` cursor instead. Live verification also found the mint-keyed
   resolver counts OUR OWN wallet as a pool candidate on our own fills — the vault-keyed lane
   (`resolve_pool(exclude_owner=payer)`, `vault_rows`) removes both that and the multi-hop
   ambiguity, since per-vault deltas are exact.

Neither change was informed by any brain outcome; both were forced by verification evidence of
the kind Revision A itself demanded.

## 1. Frozen Solana feature definitions (v2)

Tape shape: `pool_ticks` rows `kind='sol_swap' | 'sol_sync' | 'sol_mint' | 'sol_burn'`,
`pool=<coin VAULT address>` (Revision B — the enumeration key; one coin can have many pools, and
the vault is unique per pool), `block_no=<slot>`, `amount0=coin`, `amount1=quote` (so
`quote_side=1`), positive = INTO the pool — the exact shape the EVM tape and `compute_state`
already consume. Slot times live in `block_times` keyed `chain_id='solana'` and come free from
each `getTransaction`'s `blockTime`.

| # | Feature | Frozen definition | Constant | Basis |
|---|---|---|---|---|
| S1 | price path | per-SLOT size-weighted VWAP `Σ\|quote\|/Σ\|base\|` of pool-vault deltas, atomic ratio | — | `[mechanical]` same basis as v1 F1; the slot is Solana's finest honest event-time granularity |
| S2 | slot→time | `getBlockTime(slot)`, immutable, fail-closed: an event whose slot is absent from `block_times` makes the window incomplete → `DATA_INSUFFICIENT` | — | `[inherited]` v1 fail-closed doctrine + spec §3.5 (ingest-time windows banned) |
| S3 | flow | pool-vault signed net QUOTE delta from `getTransaction` pre/post token balances. The pool is resolved STRUCTURALLY: the single owner holding both the coin and a registered quote asset in the tx. 0 or >1 candidate pools → that tx contributes NO flow (fail closed, never guessed). Same-sign coin+quote deltas → LP op, excluded from flow. `wash_filter=disabled_router_ambiguity` | `DUST_ATOMIC=0` | `[mechanical]` resolution + exclusion rules; `[inherited]` v1 F5 wash stance (Solana relayers/PDAs make actor filtering strictly worse than EVM routers) |
| S4 | quote assets | `QUOTE_MINTS = {wSOL, USDC}`; a pool quoted in anything else is unknown → fail closed | — | `[inherited]` capture config |
| S5 | realized vol | v1 F2 estimator (zero-mean RMS of log returns), unchanged | `VOL_WINDOW_S=300` | `[inherited]` |
| S6 | confirmed pivots / structure | v1 F3/F4 causal detector, unchanged | `PIVOT_CONFIRM=2` | `[inherited]` |
| S7 | arrival rate / accel | v1 F6/F7 fixed closed windows on slot-time | `ARRIVAL_WINDOW_S=60` | `[inherited]` |
| S8 | depth | **REGISTERED (Revision B).** `sol_sync` = the pool's POST vault balances per touched tx (reserves — the Solana analog of `v2_sync`, and strictly richer: true levels, not echoes). `depth_pull_flag` = a `sol_burn` in the window, where same-sign coin+quote vault deltas classify `sol_mint`/`sol_burn` (LP add/remove) and opposite-sign classify `sol_swap`. One-sided events emit `sol_sync` only — reserve moves are captured, directional flow is never fabricated. Delta-sign rule LIVE-VERIFIED 2026-07-24 against instruction-log ground truth (§0.1) | — | `[mechanical]` CPMM arithmetic + live verification |
| S9 | executable drawdown | still permanently `None` — no executable series exists | — | `[inherited]` v1 F10 |
| S10 | warmup floor | v1 F11, per-slot points | `MIN_STRUCTURE_POINTS=5` | `[inherited]` |
| S11 | staleness | v1 F12 per-family freshness | `STALE_S=180` | `[inherited]` |

### S12 — Solana coverage gates (Revision B: pull model, completeness by construction)

The tape is a PULL enumeration of `getSignaturesForAddress` on the position's coin vault — the
chain's own per-account index — with an `until` cursor:

- **(a) Cursor advances only on a fully-resolved batch.** One failed `getTransaction` aborts the
  batch with no cursor move and no partial rows, so the tape up to the cursor is complete by
  construction. A stalled cursor surfaces as family staleness (S11) → the state is not trusted.
  This replaces Revision A's per-signature resolution ledger — the property it enforced now holds
  structurally.
- **(b) Subscription liveness is MOOT** under pull: enumeration of the chain's index cannot
  silently miss events inside the cursored range. (Revision A's liveness gate applied to the push
  design that was not built.)
- **(c) Recall is by construction** (the index IS the ground truth Revision A's Gate-0 recall
  bound compared against). Gate 0 (§4) retains the decode-yield and log-agreement checks; its
  recall bound is satisfied structurally and reported as such.

## 2. Trust contract (v2 profile table delta)

| profile | required families | depth |
|---|---|---|
| **Solana / sol tape** | `price, flow, vol, depth` | required (Revision B: `sol_sync` reserves come free with every decoded tx, exactly as V2 `Sync` does — symmetric with the 4663/8453-V2 profile) |

All v1 §1.1 fail-closed cases inherited, plus S12(a)/(b)/(c). Unknown-chain / unknown-kind rows
unchanged from v1.

## 3. Candidate decision rules (v2 delta)

- v1 §2.1 precondition 2 — "Solana ⇒ DEFER_TO_LEGACY always" — is **removed** for states that are
  candidate-capable under §2 of THIS file. That is the only rule change.
- Inherited verbatim, no exceptions: absolute capability precondition (v1 §2.1.1), **monotonic
  risk-reduction only** (v1 §2.1.3, enforced as a property test), nomination→confirmation with
  SIGN-ONLY flow (v1 §2.2), vol-scaled trail hard-bounded to `[0.5·T, T]` against the position's
  **live Solana** funded policy trail (v1 §2.3; anchors: nano/micro trail 30%, mid 35%),
  catastrophe guard untouchable (v1 §2.4), execution mapping through existing funded reasons and
  `executable_exit_allowed` — which on Solana is the exact whole-balance route preview (v1 §2.5).
- Because a §2 rule changed, the implementing PR bumps `ENGINE_VERSION` to `exit-brain-v2` and
  `FEATURE_VERSION` to `v2` together (v1 §5 change control).

## 4. Gate 0 — Solana capture fidelity (pre-registered; must PASS before any Gate-2 read or
canary — the zero-authority shadow may accumulate meanwhile)

Ground truth is constructible offline: per pool VAULT, `getSignaturesForAddress` yields the
complete signature list, and per-vault balance deltas are exact — keyed by vault there is no
multi-hop ambiguity. All on the READ-ONLY endpoint.

Sample: **≥ 3 distinct pools, ≥ 30 classified transactions total** `[bound]`, pools drawn from
coins with an open/recent funded position or candidate (the population the brain will actually
watch).

Pre-registered PASS requires ALL THREE:

1. **Capture recall** — **satisfied by construction under Revision B** (the tape enumerates the
   same index the ground truth would; S12(a) forbids partial windows). Reported as structural in
   the Gate-0 write-up, not measured.
2. **Decode yield** — flow-resolved (decoded + explicitly-excluded) / enumerated **≥ 0.90**
   overall `[bound]`. Vault-keyed decode removes multi-hop ambiguity, so failures here mean
   malformed/unfetchable transactions; if yield fails, the branch is an enrichment fix, never a
   lowered bound.
3. **Classification fidelity** — per-tx class (swap / LP / excluded) agrees with instruction-log
   ground truth in **≥ 0.95** of classified txs `[bound]`, extending the 2026-07-24 live
   verification (§0.1) to n ≥ 30 across ≥ 3 pools. This is the bound the sign-only confirmation
   rule (v1 §2.2) and `depth_pull_flag` actually depend on.

**Branches (frozen):** PASS all three → Solana states may be trusted; proceed to the v1 ladder.
FAIL any → capture/enrichment redesign, then re-run Gate 0 **in full**. Thresholds may never be
lowered, and a partial re-run (only the failed metric) is not permitted.

## 5. Explicitly NOT registered (and therefore not permitted)

Actor concentration; ingest-time feature windows; any executable
price derived from mid + haircut; any learned model touching parameters or money; any threshold
fitted on cohort, shadow, or funded outcomes; and **any observation-lane code touching
`MEMECOIN_SOLANA_RPC_URL`** — the read lane is `MEMECOIN_SOLANA_READ_RPC_URL` only (the custody
var arms the real leg; a test enforces the separation).

## 6. Change control and activation ladder

| Step | Gate |
|---|---|
| implementing PR | **SHIPPED 2026-07-24 (Revision B, same commit):** Solana profile in `profile_capability`/`compute_state`, vault-keyed pull tape (`memecoin_solana_tape.py`, cursor-held-on-failure per S12a), `exit-brain-v2`/`v2` bumps, fail-closed tests incl. live-verified fixtures. Merging it arms nothing — the tape runs inside the log-only exit-state worker |
| Gate 0 (§4) | must PASS before any Solana state is trusted. NOTE: the tape produces states labeled TRENDING etc. immediately, but no read/action happens before Gate 0 is written up — the shadow logger records, nothing consumes |
| shadow | v1 Gate 2 verbatim, chain-split (never pooled with EVM — design principle 6), **n ≥ 30 divergent Solana decisions** before any read |
| canary → sealed read | v1 Gate 3/4 verbatim, chain-split |
| any constant edit here | new dated prereg + version bump |
| worker/flag enables | operator STOP conditions, unchanged |
