# Wallet universe measured size

Operator authorization: obtain the missing data properly; earlier advance approval remains in force.

## Scope

Separate low-priority, read-only observer. No funded callback, signer, quote budget, custody transport,
authority or policy change. The web reads one bounded atomic artifact. The observer alone writes a new
owned measurement ledger, with exact atomic amounts stored as decimal TEXT.

Capital means liquid native, wrapped native and explicitly trusted stable balances on currently proven
addresses, within one chain. It excludes speculative tokens, LPs, lending, staking and unproved satellites.
USD stable par is an explicit valuation assumption; native spot prices carry source and read time.
No profile's funds are assigned to another through an unproved relationship. Arc native USDC and its
ERC20 view are one asset, never added twice. Missing components prevent a complete total.

Flow v6 sizes planets by observed net trade/quote flow in the exact 60 seconds after a
source buy. The before/after change is additional context only when the full prior minute exists;
a launch with no prior minute still has measurable post-buy flow, never a fabricated prior zero.
Preserve each quote asset and decimals, source transaction identity, source/own exclusions,
complete versus partial coverage and the first detection per wallet/coin. EVM scope is registered/source
venues, not every venue on the chain. No minute candle or endpoint-return strategy claim is produced.
Do not mix historical impact-v1, signer-basis v2, or older v3/v4/v5 decoder states into v6. Readiness uses all eligible units, including pending
and failed units, not only successful rows. Ten distinct complete coin reads and at least 75% complete
eligible units are required for a comparable planet size; smaller samples remain inspectable.

Dense Solana pages resume with their cursor after a call cap or pause. No provider error becomes an
empty successful page. EVM source receipts, pool metadata, block order and log hashes must match.
If the receipt index is absent, a canonical block containing the exact transaction plus anchored
successful swap logs and a closing canonical-header check can recover a separately labelled source
observation. A hash-only body requires an exact transaction lookup with matching hash, block,
index, sender and recipient. This never becomes a claimed provider receipt. Relayed sources require
the persisted versioned chain/coin/beneficiary proof and current canonical mint transfers; recipient
proof is not proof of the initiating wallet. If saved Relay proof is absent, separate research reads
may reconstruct the exact transaction envelope and verify both pinned contract bytecode hashes at
the receipt block. Current canonical pool buys and beneficiary mint transfers are still required.
This is destination-beneficiary evidence only: initiation remains unproved and third-party-funded
gifts remain possible. The inspector retains that label. Completed raw reads survive a budget stop;
an unknown-slot resume independently refreshes the exact transaction before historical code reads.
No latest-code substitute or mutable watcher cache grants proof. Missing source clocks may be cached provisionally,
but the complete reader still independently proves the buy and source order.
Incomplete signed flows have no claimed bound. Quote assets stay separate; optional USD comparison
uses current snapshot prices explicitly labelled, not fabricated historical conversion prices.

BSC source receipts/transactions and their exact canonical source header may recover from the
chain-owner archival endpoint only after a primary missing-result response. The endpoint is
checked against configured funded URLs/credentials; its disabled log API is never a flow-history
fallback. The lazy second reader shares the same hard attempt cap, including chain verification.
A restored unknown-slot source cache needs a fresh matching transaction before archival header
recovery. Exact source-block code for the two pinned Relay contracts may also recover after a
primary missing result or explicit empty code body. Other contracts, blocks, nonempty code and
historical eth_call metadata do not use this fallback. No timeout, HTTP error or rate limit triggers
archival recovery. BSC historical contract-state failures remain named coverage gaps; successful
receipt retrieval is not proof that the same endpoint supplies old state.

Censored windows back off from two minutes to one hour without leaving the denominator. New
source proof or method identity retries immediately. Partial page cursors resume without this
backoff. This prevents repeatedly rejected source identities from starving new measurements.
The first source detection remains the cohort origin when a later block clock is recovered.
Initial readiness publishes the local census before any external price request.

Balances remain fresh reads. Each chain's sequential capital phase may reuse a verified identity, one fixed
EVM block anchor and decimals at that exact block for at most 240 seconds; every address closes with
a fresh canonical header. Solana only reuses genesis identity. The clock is the actual completed read,
with a five-second block timestamp tolerance. Freshness is checked again at completion; a long wait
cannot stamp an old anchor fresh. A chain pass has 120 attempted calls by default,
including errors: up to two thirds for balances, then remaining capacity for resumable flow reads.
Up to five independent chain phases run concurrently. Every worker owns its transport, counters,
mutable read/decoder state and connection to the already initialized owned database. The root alone
updates prices, reports and artifacts. Each publication fetches capital and compact flow metadata
under one read snapshot and releases it before Python JSON decoding or disk writes. SQLite projects
the flow metadata without returning saved transaction histories to Python. Scheduling retains only
metadata; a chain worker loads the complete resume history for its selected window only. Workers join before another
cycle or configuration update. The loop waits 120 seconds after each completed pass. This is not a
120-second refresh promise. Actual phase duration, capital ages and resource peaks require live checks.
Keyed dRPC shares the existing account-wide 1.5-request/second pacer; Solana requests are at least
1.2 seconds apart. Separate provider accounts cannot be proved from distinct keys. No account
separation or zero indirect provider-pressure guarantee is made.

The inspector shows completed individual coin windows before comparable sizing is ready.
Unknown observations keep a neutral radius; incomplete, unpriced or obsolete samples cannot grow a
planet. Solana v6 uses authenticated PumpSwap, Meteora DLMM, DAMM v2 and DBC executed instructions,
pool vault identities and exact quote transfers uniformly. Source attribution additionally proves
the signer buy. DBC additionally binds its canonical self-CPI event, pool/config/vault PDAs,
event authority and exact persistent vault movements against the current official IDL. Its
transfer-hook event is not treated as a Pump event log. DBC fees follow the official direction/mode:
base-to-quote fees are on output; quote-to-base permits input or output fees. Retained output fees
are already outside the physical user transfer and are never subtracted from it again.
A routed source may prove an economic
purchase through exact signer payment, target delivery, paired same-invocation conversions and
whole-transaction inventory reconciliation. Prefunded escrows must close unchanged; mixed funders,
extra target transfers, depleted escrows and ambiguous token operations cannot grant source proof.
The protocol payer remains an unowned PDA, never a wallet alias. A later routed buy is excluded
only when the same proof identifies an excluded signer. Unknown routed payers remain explicit.
Truncated text logs may recover only through complete compiled instruction trace metadata with
consistent runtime stack heights, parent indexes and resolved accounts. The visible prefix before
the exact truncation marker must agree with that trace. Later short log messages are not a continuous
suffix. Event CPIs retain program/parent/event-authority/discriminator/identity and physical-vault
checks. Pump events without a complete log require the derived authority and immediate self-CPI.
Any recognized runtime failure anywhere, including after the truncation marker, censors the transaction.
The result records the instruction evidence path. Missing trace metadata, contradictory prefixes,
failed/unreconciled movements and unsupported target programs remain unknown; successful top-level
metadata alone never replaces physical leg reconciliation. This trusts complete RPC metadata, not
an independent proof that the provider omitted no instruction.
Unsupported target programs, incomplete instruction traces, native Pump curves and unknown
Token-2022 extensions remain typed incomplete. Exotic quote mints retain their exact units; they
never become SOL or fabricated USD. Signer profit is never substituted for market quote flow. EVM aliases cannot be
attributed from a router event maker; exclusions are the exact source and known own transactions.

## v7 amendment — 2026-10-05

`wallet-universe-flow-v7` extends this baseline with cumulative exact quote flow
at 5/10/15/30/60 seconds and verified Solana buyer lower bounds. Its inclusive
integer-second Solana endpoint waits until `t0 + 61`; v6 rows cannot supply current
comparisons. Compact projections are computed for the selected decoder result and
survive the metadata-only publication path above. Touched-account balances may
prove an add-on; zero does not prove wallet-wide new-position status. The 14-day
first-observed-buy census remains separate from the 30-day default-entry dossier.
See the local dossier and flow extension in
[the execution research contract](2026-10-04-memecoin-wallet-execution-research-design.md).
Flow at 120/300 seconds, causation and strategy support remain unavailable.

## Edges and limits

- Verified identity may change: current proof joins invalidate obsolete samples.
- Zero is measured only after a valid full balance response; timeout, ban, rate limit, rejection,
  malformed/wrong-chain data, missing prices and partial components have separate states.
- Collection pauses for a funded book on that chain. Research URLs/credentials must be separate;
  account-wide pacing and strict attempt budgets include failed requests. No automatic paid capacity.
- Shared process globals, env, DNS and funded transport pools are never mutated.
- History/provider limits, unknown venues/quotes, missing signatures, insufficient distinct coins,
  full portfolio wealth, bot ownership, causation and profitable strategy conclusions are not solved
  by rendering a larger planet. Coverage names the specific remaining constraint.
- Rollback: stop only this observer and restore the prior web pin. Own data remains readable; no
  production-ledger schema change, backfill mutation or deployment backup.

## Verification

Prove meaningful negative tests before implementation. Check exact units, identities, timestamps,
Arc deduplication, provider failures, resume and honest denominators. Browser tests must prove metric
selection changes radius, unknown stays neutral, and camera/navigation/mobile survive. Verify the live
observer PID/pin, collected values and authenticated endpoint independently of tests.
