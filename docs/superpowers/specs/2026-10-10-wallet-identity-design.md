# Wallet Map and Universe address identity

The [armed-wallet extension](2026-10-10-armed-wallet-links.md) adds a separate
`wallet_identity_links.json` artifact for all armed chain pairs, confirmed funding
receipts and buyers on both sides of the exact main buy. It includes known roster
peers. The legacy Solana projection and its zero-RPC refresh remain unchanged;
the extension revalidates current account claims and never grants ownership or
funded authority. Its new reads use separate bounded research transport.

Operator approved the in-chat design and execution: “Perfect plan get moving”.

## Outcome

Under each Fomo UID, show current verified account addresses and separately labelled related
addresses. Measure repeated buys before that UID's known source transaction, retain each address
and transaction, and expose independent funding clues. Suspected alternate wallets never acquire
the user's authority, capital, returns, or verified ownership. Handles are labels; UID is identity.

## Architecture

Reuse the current verified-address readers, `memecoin_wallet_upstream_reads`,
`memecoin_wallet_early_buys`, and `memecoin_wallet_funders`. A bounded offline projection writes
one atomic `data/wallet_identity.json`; the existing graph job refreshes it after its work, with
zero additional RPC. A standalone research command can publish the same artifact without running
the graph or any RPC. The live Universe route reads it and revalidates verified addresses against
the current registry. The retired per-chain Universe artifacts are not used.

Add nullable `buy_signature` and `order_proven` fields to the research early-buy table so future
decoded transactions retain their identity and order. Retain failed comparisons in the source
read's `n_order_unknown` counter. Old positive-lead rows remain inspectable with source signature, slot/index,
and an explicit missing candidate signature. Do not synthesize signatures or atomic quantities
from stored floating token amounts. No funded-ledger migration or write is required.

## Claims and relationships

Addresses stay keyed by `(canonical chain ID, address)`: EVM lowercase, Solana case preserved.
Existing verified proof readers supply account claims. Multiple current UID claims for an address
are a conflict; no last-write-wins owner is presented. A related address may be connected to several
users; links never merge those users. Cross-chain key identity is distinct from same-chain activity.

For Solana, use all stored first-source windows in the previous 14 days, independently of coin
returns or upstream candidate verdicts. One denominator unit is `(UID, mint)`, choosing the earliest
source window in the period. A window is usable when status is `ok`, `no_label` or `our_fill` (the
last two lack post-buy outcome labels but retain complete pre-buy reads), its named-source control
passes, and its clock is valid. Failed/unread windows stay in coverage, not the negative denominator.
Every candidate row must match that window's source signature and have lead in [0,60] seconds;
Zero lead requires the recorded `order_proven` flag: earlier known slot, or both known indexes
in the same slot. Legacy zero-lead rows lack this proof and stay unknown; unknown ordering
prevents measured absence counts. `n_order_unknown` retains future comparison failures. Zero
lead is never interpreted as a positive second lead. Duplicate observations of a coin count once. Own custody addresses are excluded.

Report distinct supporting coins, complete eligible coins, complete coins without that candidate,
unknown coins, distinct UTC source days, p50/p90 lead, first/last source time, and at most ten
supporting coin/transaction records. This is descriptive, not a significant lead or trading-edge
claim. No win/PnL predicate participates in discovery.

- `verified_account_address`: current verified claim for this UID.
- `suspected_alternate`: at least three distinct coins and two UTC days, plus a stored resolved
  initial-funding clue with its stored receipt naming one of this UID's verified addresses as funder or recipient. A
  competing verified UID or a hub funder prevents this label. Cached funding attribution is a
  heuristic, explicitly labelled, and never verifies common control.
- `related_trader`: timing evidence alone, shared funder alone, or an address verified to another UID.
- `ownership_conflict`: multiple current verified UID claims. Claims remain inspectable.

Shared funders, shared fee payers, relayers, routers, and bundles cannot independently establish
ownership. Funding-cache failures stay unknown. Hub suppression uses the existing threshold of
more than three resolved addresses, counted over the full cached funder population, not the UI sample.

## Presentation

Universe inspector gains “Account addresses and related wallets”: full verified addresses; related-address
labels; supporting/complete counts; unknown coverage; lead p50/p90; competing known users; funding
clues; and expandable transaction evidence. All text is escaped; transaction links are generated
from validated signatures and chain-specific explorer URLs. Stale research is visibly stale.
Existing repeat-buyer navigation and wallet authority actions retain their contracts.

## Bounds and failures

Read at most 10,001 source windows and 100,001 early-buy rows. A cap reached is `partial` and
the omitted tail is unknown. Serve at most 20 related addresses per UID, strongest descriptive
evidence first, and disclose the omitted count. Artifact maximum is 4 MiB; staleness threshold is
two hours. Atomic replacement preserves the prior file on failure. Missing, malformed, wrong-version,
oversize, or future-dated artifacts yield an unavailable research state while current registry
addresses remain usable. Revalidation prevents a stale artifact from restoring revoked ownership.

## Rule 5

1. Money: no entry/exit/sizing/signing imports or callbacks; no authority inheritance or mutation.
2. Fill speed: no entry-path work. New network calls: zero. Offline duration and API overhead
   will be measured rather than projected as latency improvements.
3. Concurrency: read-only autocommit ledger/graph handles; independent local projection state;
   atomic artifact write; no shared configuration, transport, or module mutation.
4. Missing/zero: failed windows are unknown; a complete coin without an address is a measured miss;
   missing lead/signature/funder stays unknown; zero-second same-slot lead is not absent.
5. Dependencies: missing tables, corrupt artifacts, decoding errors, and failed writes are named;
   no new provider requests, retries, or fallback. Previous artifact survives a failed publication.
6. Identity/units: chain-address keys, UID claims, seconds, slots/index, exact signature; stored
   floating token quantities are not reused as proof of atomic acquisition.
7. Sample: source windows already collected by the existing Solana research worker, which excludes
   known roster addresses from its early-buyer intake. This is not all-chain or all-wallet coverage.
8. Rollback: revert code; the unused additive research column and artifact are harmless. Delete
   the artifact only if the operator wants its research history removed.

## Acceptance and exclusions

### 2026-10-10 amendment: funding verified links

The operator requested inspectable funding links even while common ownership remains unproven.
`data/wallet_funding_receipts.json` separately retains the two confirmed JSON-parsed Solana
funding receipts researched in `output/research/ownership-proof-20261010/`. Version 1 records
`chain`, `commitment`, the aware receipt capture time `observed_at`, and at most 100 transactions
within 4 MiB. This is a locally trusted, curated receipt capture, not arbitrary uploaded proof.
The reader checks successful status, transaction signatures, signer-bound outer System Program
transfers, positive exact lamports and corresponding recipient credit. No network call is added.

`funding_verified_link` proves only the signed funding connection. A conflicting peer retains
`ownership_conflict` alongside the verified funding fact; a competing verified UID remains visible.
Current nonconflicted account addresses are revalidated before attaching either transfer direction.
Revocation removes the account association. Activity artifacts cannot supply verified funding flags.
Funding receipts do not expire with the rolling activity sample or disappear on graph refresh.
Absent timing samples retain unknown lead and absence fields. Missing/malformed receipt files
leave funding unavailable and preserve existing activity evidence.

The inspector exposes exact SOL, direction, receipt and wallet explorer links outside the trade
evidence fold. Repeat buyer addresses also link to their explorer. Neither funding nor repeated
buying proves common ownership or bot identity, merges accounts, or grants authority.
This amendment handles saved outer native-SOL transfers only; token funding, inner transfers,
EVM funding, automatic collection and bot identification remain outside scope. Rollback leaves
the separate receipt artifact intact and unused by the old code.

Focused regressions prove losing-coin inclusion, denominator deduplication, missing-window coverage,
shared-funder nonownership, hub exclusion, cross-user/conflict handling, chain/case isolation,
signature retention, stale/revoked claims, atomic replacement, route integration, HTML escaping,
inspectable evidence, and no inherited funded authority. No full suites.

Not handled: wider pre-entry history, EVM alternate-wallet attribution or funding history, new RPC
collection, physical-person ownership proof, automatic account merging, automatic verification,
funded promotion, or a predictive/profitable trading strategy. Existing related-wallet patterns
are retrospective; they do not become signals that were known at their historical transaction time.
