# One Solana address registry for both on-chain lanes

Beta, 2026-09-26. Status: DESIGN, awaiting Alpha. Source: Codex,
`docs/audits/2026-09-26-alert-753be289-and-address-coverage.md` items 4-8, and
`output/research/alert-753be289/armed-address-coverage.csv`.

## Problem, measured

Two Solana on-chain lanes decide which wallets they watch from two different sources:

| lane | source today | keyed by | refresh |
|---|---|---|---|
| slot lane (processed, funded S/S+1) | `data/solana_roster_owners.jsonl` (70 rows) plus the address inside a `pump:<address>` profile id | HANDLE, lowercased, joined to `memecoin_wallets.handle` | every 10 s |
| confirmed watcher (`scripts/research/onchain_wallet_watch.py`, feeds `onchain_watch`) | `memecoin_wallet_addresses` rows with `verify_status IN ('fomoscan_confirmed','live_confirmed')` | `source_user_id` | at startup only; the cron watchdog restarts it on roster drift |

Of 47 wallets armed on Solana: the slot lane watches 39, the watcher 16, and 8 have no address in either
(Thokani, attherate, origin, ser_albert, yayayayayey, CryoPalmar, Ew4rdo, LingardSol). Those 8 are J7-only.

`memecoin_wallet_addresses` cannot hold the truth: its primary key is `source_user_id` alone, so a profile
has at most one address on one chain, and `axiom_address_verify.py` writes it with `INSERT OR REPLACE`, which
erases a refutation when a new address arrives. It is also missing from the brain index (created outside
`init_db`). Its 41 rows today: solana REFUTED 15 (`fomo_profile_solana` 12, `holder_intersection` 3),
`live_confirmed` 22 (`forward_signal_verify` 18, `pump_id_onchain_decode` 4), `fomoscan_confirmed` 2; 4663
`unverified` 2.

## Design

### Table

<!-- schema:external=memecoin_address_registry -->
<!-- schema:external=memecoin_address_registry_events -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_address_registry (
    source_user_id TEXT NOT NULL,      -- stable profile id; the handle is display only
    network_id     TEXT NOT NULL,      -- canonical: 'solana' (EVM chain ids later, same table)
    address        TEXT NOT NULL,      -- exact case; base58 validated as a 32-byte pubkey
    status         TEXT NOT NULL,      -- 'verified' | 'candidate' | 'refuted'
    evidence_kind  TEXT NOT NULL,      -- see the evidence table below
    evidence_json  TEXT,               -- signatures, mints, counts, source row, read_at
    handle_seen    TEXT,               -- the handle when last seen, never a key
    first_seen_at  TEXT NOT NULL,
    status_at      TEXT NOT NULL,
    PRIMARY KEY (source_user_id, network_id, address)
);
CREATE TABLE IF NOT EXISTS memecoin_address_registry_events (   -- append-only history
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_user_id TEXT NOT NULL, network_id TEXT NOT NULL, address TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL, evidence_kind TEXT NOT NULL,
    evidence_json TEXT, actor TEXT NOT NULL, at TEXT NOT NULL
);
```

A profile may hold several addresses per chain (rotations, a second wallet). A refutation belongs to ONE
address: refuting A never refutes B (the `change` and `397397` case). Every status change writes an event
row; nothing is deleted or overwritten out of history. A new table, not an ALTER, so an older release that
inserts positionally into the old table keeps working.

### Evidence and the status it earns

| evidence_kind | from | status |
|---|---|---|
| `pump_profile_id` | a `pump:<address>` source_user_id whose suffix parses as a pubkey | verified: the id IS the address |
| `j7_trader_receipt` | a JSONL row: J7's event id named the trader (lowercased) and one `getTransaction` on that signature returned the exact-case owner | verified, with the profile id taken from the `memecoin_events` row carrying that signature, never from the handle; a handle/id mismatch makes it a candidate |
| `live_confirmed` / `fomoscan_confirmed` | the old table's confirmed rows, method kept in `evidence_json` | verified |
| `REFUTED` rows | the old table | refuted, method kept |
| anything else | the old table (`unverified`, ...) | candidate |
| `forward_buy_match` | the separate read-only resolver for the 8 (below) | verified only at >= 3 exact buys on >= 3 distinct mints, owner = recipient = funder, side = buy |

Conflicts: the same (profile, chain, address) marked verified by one source and refuted by another reads
`refuted` (the funded slot lane must not copy an address that ever failed a check), and the coverage
surface shows it as `conflict`. Shared infrastructure never counts: a relayer, a signer that is not the
owner, a router, a token account, a holder-intersection overlap or an airdrop cannot establish identity.

### Reconcile, then resolve

1. **Reconcile (this build).** An idempotent function mirrors the three existing sources into the
   registry: pump ids, the JSONL, and the old table. It never deletes, never downgrades `verified` to
   `candidate`, and writes an event only on a real change. It runs from a script now and on a bounded
   off-path cadence afterwards, so tools that still write the old table keep flowing in until they are
   switched to the registry.
2. **Resolve the 8 (separate, read-only, later).** Reuse `scripts/research/memecoin_forward_address_verify.py`
   after auditing its method (current holdings do not prove who executed a past buy). Candidates only
   until the >= 3 buys / >= 3 mints rule holds. No write to the registry without Alpha's go.

### Readers pick up changes without a restart

- **Slot lane.** `owners_from_roster` reads verified Solana rows for armed profiles, keyed by
  `source_user_id`, on its existing 10 s roster refresh. If the registry read fails, it keeps the last
  good roster (never an empty one); if the table does not exist yet, it falls back to today's JSONL +
  pump-id path. That makes the rollback free.
- **Confirmed watcher.** A bounded refresh (60 s) diffs the verified set against its live subscriptions:
  `logsSubscribe` new addresses, `logsUnsubscribe` removed ones, and count acknowledgements. A failed read
  keeps the current set. The cron roster-drift restart stays as the backstop.

### Coverage surface

The runner heartbeat gains `address_coverage` for Solana: counts of armed, verified, slot-subscribed,
watcher-subscribed, `j7_only` (armed with no verified address), `candidate_only`, `refuted_only` and
`conflict`, plus the `j7_only` handles by name. The dashboard shows one row per armed Solana wallet:
armed / verified addresses / slot acked / watcher acked / J7 tracked / label. Labels: `on-chain + J7`,
`J7 only`, `candidate mapping`, `refuted mapping`, `subscription unavailable`. Readiness never changes
authority: nothing is disarmed, no admission gate is added, J7 stays.

## Rule 5

1. **Money path: yes.** The slot lane's owner set decides whose buys become funded S/S+1 entries. Today:
   JSONL + pump ids. After: the registry's verified set, i.e. the JSONL and pump ids (same evidence) plus
   the old table's confirmed rows, which already fund entries through the watcher. Net effect: the slot
   lane starts watching wallets that are already armed and already watched by the confirmed lane; the
   watcher gains the JSONL and pump-id wallets. Every entry gate still runs; the existing duplicate guard
   between J7, the slot lane and the watcher is unchanged.
2. **Speed:** registry reads stay on the 10 s and 60 s refresh threads, never on the frame or entry
   path. Zero added latency on the fill path.
3. **Concurrency:** writers are the reconcile function and the verification tools; readers use read-only
   connections. The slot lane swaps its roster dict whole, as today.
4. **Absent vs unknown:** no verified row means not watched and labelled `J7 only`; an unreadable
   registry keeps the last good set; a missing table means the legacy path.
5. **Dependencies:** no RPC in reconcile. Resolving the 8 later reads chain history on the research key
   only, with no real Solana position open.
6. **Identity:** keyed by `source_user_id`, never by handle. Solana addresses are case-exact and must
   parse as pubkeys. JSONL rows join through the signature's `memecoin_events` row.
7. **Sample:** coverage counts describe our registry and subscriptions, not what the wallets trade.
   `J7 only` means no verified address, not "J7 delivers everything".
8. **Rollback:** previous release. The table is additive; readers fall back to the legacy sources when it
   is absent.

Not handled here: EVM (profile rows are candidates, and per-chain verification is separate), the
multi-hop source valuation (Codex item 1), and alert metadata (item 3).

## Checks this build leaves behind

- A reconcile over fixtures of all three sources: a pump id is verified; a JSONL row joins by signature,
  not handle, and a handle/id mismatch is a candidate; REFUTED stays refuted; a second address for a
  refuted profile stays verified; re-running writes no events.
- The slot lane's owners come from the registry by `source_user_id`; a read failure keeps the last roster;
  a missing table uses the legacy path.
- The watcher's refresh subscribes an added address and unsubscribes a removed one without a restart; a
  failed read keeps the current set.
- The heartbeat labels an armed wallet with no verified address `J7 only`.

## 2026-10-20 operator receipt identity amendment

An explicit operator enrollment may use `evidence_kind=operator_receipt_identity` through the
existing generic `record_address_evidence` writer. This is an audited human identity attestation,
not automatic `fomo_buy_receipt_match`, Axiom, FomoScan, J7, wallet-payment or signed-order promotion.
Their existing proof rules and thresholds remain intact. The record retains the actual receipts,
original source-identity facts, operator order and proof limits; its JSON requires a nonempty `audit`
document reference and a nonempty `receipts` list of objects with full EVM `tx` hashes.

The EVM background reader explicitly opts in with
`evm_watched_owners(conn, chain, include_operator=True)`. It accepts only the exact configured
canonical EVM chain, a valid nonzero address, `status=verified`, no conflict, and an exact matching
append-only verification event with the same UID/chain/address/status time/kind/JSON and a nonempty
`operator:` actor. A competing registry owner, conflicting/refuted address, foreign Axiom claim or
legacy confirmed owner excludes enrollment on that chain. Existing accepted proofs take precedence;
the normal per-chain arm, pause and exclusion filters still apply. No record grants a cross-chain
proof basis or establishes the initiation/payment of a later transaction.

Only the EVM watcher and Robinhood feed writer's background roster refresh opt in. Default
`evm_watched_owners` and `watcher_address_owners` remain unchanged for entry-side J7 coverage: no new
synchronous entry read, lock, retry, RPC, shared cache or automatic fallback authority. The requested
4663 setup retains its existing J7 data-only policy. Optional missing registry tables preserve the
legacy reader; other read errors reach existing keep-last-good background handling. Rollback
withdraws recognition of operator enrollments without deleting their history or changing automatic
proofs. Live activation must separately verify the external delivery list, arm state and watcher
acknowledgement; green tests are not organic execution proof.
