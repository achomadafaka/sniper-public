# Native SOL curve full sell: math and boundaries

Status: gross/solvency/fee math reviewed; builder, full/partial selection and fastlane coverage
implemented and tested offline. Live sellability remains unproven.
The human approved all three offline changes in this Codex chat: full native sell, native
selection for partial sells, and desk coverage beyond the fastlane cap. The human also approved
building the direct curve sell in Alpha's session. This does not authorize push, deployment,
restart, provider calls, outcome reads or a funded transaction. Desk rails remain 5 open,
$25 clips, $200 daily cash-loss halt and -$200 cumulative cost-inclusive kill.

## Scope

Add the missing normal SOL-quoted Pump curve full-sell fallback. Reuse `pump_native_exit.py`'s
atomic USDC tail, packet shaping, simulation and quote-shaped result. Leave existing successful
Jupiter, exotic-quote and PumpSwap exits intact. Beta owns its separate entry-readiness wiring.
Partial native selection reuses the same quote boundary with its existing partial atomic
quantity and position binding. Source partials retain their executor-supplied exact quantity.
Fastlane coverage now includes every open, unpaused REAL position by default; an explicit
caller limit still applies, and admission limits are unchanged.

The first version accepts a canonical, complete V3 layout (166 bytes), an incomplete curve,
normal SOL quote mint (default public key), and known false Mayhem,
cashback and holder-reward flags. Legacy/unknown layouts and unsupported modes refuse.
No fee recipient or special fee flow is guessed.

## Integer arithmetic

All reserve/output values are SOL lamports or token atoms, never human token floats. For
positive exact token input `q`, virtual SOL `Vsol`, virtual tokens `Vtok`, and real SOL `Rsol`
from the SAME curve account read:

```
G = floor(Vsol * q / (Vtok + q))
require G <= Rsol                         # typed curve_real_sol_short; never cap G
P = ceil(G * protocol_bps / 10000)
C = ceil(G * creator_bps / 10000)
N = G - P - C
require N > 0
b = floor(slippage_pct * 100)             # finite decimal percent; 0 < b < 10000
M = floor(N * (10000 - b) / 10000)
require M > 0
```

`G`, rather than `N`, debits curve reserves. The buyback fee is a share of the protocol fee,
not an additional deduction. Creator fee is zero when creator is the default public key.
Use the sole proven curve fee tier; refuse an unmodelled tier schedule or invalid bps.
The per-coin u64 creator override at byte 115 applies only when Global's
`creator_fee_configurable` bool at byte 1045 is true and the override is nonzero. Otherwise
use the schedule's creator rate; conservatively refuse a nonzero override when that flag is
false or unknown. Require the complete relevant account layouts and valid bools.

Sign legacy Pump `sell(amount=q, min_sol_output=M)`. Jupiter only prices/builds the
WSOL-to-USDC tail, with **input exactly M**, so the atomic sell guarantees the tail funding.
The result's USDC floor comes from that tail. Any better curve fill remains SOL residue;
no output is manufactured from the wallet's pre-existing SOL balance.

Cached SDK slippage uses `N - floor(N * s)`; this design intentionally uses floor of the
retained amount, at most one lamport lower at an exact bps fraction. The difference is explicit.
For an entry round trip, evaluate sell reserves AFTER our own buy: virtual SOL and real SOL
include its net curve deposit and virtual tokens subtract bought atoms. Reading current
pre-buy reserves is not a valid post-buy sell-readiness proof. Beta must preserve this distinction.

## Worked frozen trade

Tracked `tests/fixtures/memecoin/pump_tape_frames.jsonl` lines 1-2 contain consecutive
same-mint public **processed** sell frames in slot 450791133. Use the first event's post-state
as the second sell's pre-state:

```
Vsol = 37,903,103,425
Vtok = 849,270,827,451,215
Rsol = 7,903,103,425
q    = 41,876,174,451
G    = 1,868,848
P    = 17,755       (95 bps, ceil)
C    = 5,607        (30 bps, ceil)
N    = 1,845,486
```

The second event reports that gross and both fees; its virtual SOL, virtual tokens and real
SOL equal the predicted post-state exactly. Artifact SHA256
`a75837ff67261bcdde6c8757d1e53216d072b2e7b4acc3aae25e74cb9ba87311`:
`/home/bigpapi/.cache/codex-native-sol-sell-recon-2026-10-23/processed-sell-math.json`.
These frames are not confirmed/finalized proof. Lambda's corrected fixture `1bc55b823`,
integrated on main at `b50a4404a`, has 17 SOL rows (raw quote mint default) and 22 quote-token rows
in a separate array. Four SOL rows (2/3/4/15) fit the initial mode scope, including one normal
legacy sell (row3). Three other normal/cashback-false rows have holder rewards enabled; the
builder checks raw flags, not `kind` labels. This supersedes the mixed `9fffca2a6` fixture with
eight SOL rows of 17. Raw pre-account bytes and event logs match gross, separate fees and all
four reserve transitions. Confirmation request/status, transaction `meta.err`, raw Global and
the signature bracketing responses were not retained; confirmation is collector provenance,
not independently retained chain proof. Do not label quote-token fee variety as SOL fee variety.

Gamma's corrected 2,000-pair v2 sample supports arithmetic only. Its SOL classification is a
positive-legacy-field proxy with no quote-mint/raw-account field; Gamma reports a separate
100-mint identity spot check, not repeated by Codex. The withdrawn v1 sample contained zero
artifacts and is not retained. Fixture provenance and hashes live alongside the test data.

## Primary cached sources

Existing local official Pump IDL SHA256
`7616681ea557975f9f623ac211080f22a93060781eaf4765b0a488d980997207`, version 0.1.0:
`/home/bigpapi/Documents/Codex/2026-10-14/fix-everything-found-2-p1-defects/logs/pump-official-idl-20261018.private.json`.
Cached official SDK files in the same directory:

- `pool-mode-official-sdk-package_src_bondingCurve.ts:473` gives floor gross;
  `:653` subtracts `getFee`.
- `pool-mode-official-sdk-package_dist_index.js:15413` applies separate fees and zero-creator
  semantics; `:15435` gates the creator override; `:15519` rounds each fee up.
- `pool-mode-official-sdk-package_src_sdk.ts:2510` builds legacy sell: 14 IDL accounts plus
  readonly `bonding_curve_v2` and writable Global-listed buyback recipient. Cashback's extra
  volume account is excluded with cashback itself in this initial scope.

Legacy sell discriminator is `33e685a4017f83ad`; data is discriminator plus two LE u64 values.
Do not copy PumpSwap accounts merely because its discriminator matches. Full layout/PDA and
source hash receipt:
`/home/bigpapi/.cache/codex-native-sol-sell-recon-2026-10-23/official-sol-sell-abi-design.json`,
SHA256 `03ec69eabfc14e893e826f6890c2269620be1fcddf8f22e4799365580b984f40`.

## Failure, concurrency and rollback

- Zero/negative/overflowing atoms, nonfinite/invalid slippage, zero reserves/net/minimum,
  wrong owner/discriminator/encoding/length, partial reads, unsupported token program,
  unmodelled fees and unknown modes return no new executable quote with a named reason.
- Timeout, rejection, rate limit, malformed provider output, missing blockhash/ALT, oversized
  packet or failed simulation never reach signing. No exception from the new fallback may
  replace an existing exit result or strand a reservation.
- The fallback is considered only at the existing full/partial exit quote boundary. No new entry
  synchronous call, thread, shared mutable env/module state, persistent cache or retry loop.
  No latency gain is claimed without measurements. Provider reads are injected in tests.
- The original exit quote calls are recorded without re-quoting. New SOL selection follows
  existing routes only after an explicit Jupiter no-route code. A bare `None`, generic 4xx,
  malformed quote, timeout, rate limit or access failure does not authorize this fallback.
- An explicit unsigned Jupiter SOL-curve build refusal (route code or concrete
  InstructionError/IllegalOwner) may request native selection on the next existing
  ladder rung. This request belongs to one exit invocation. The executor releases/reclaims and
  binds a new quote/intent before signing; no native bytes replace a bound Jupiter quote.
  A successful existing native route is retained; new refusal/exception retains the old result.
- Both full and partial selection use this boundary. A native build that fails on the last
  rung has no later fallback attempt; no rung or retry is added. Entry readiness integration
  remains Beta's separate work; supplied-state builds are probes and the signer refuses them.
- Simulation validates the complete atomic transaction before a full exit can sign it.
  `simulate=False` is a build/readiness probe only, never proof of funded sellability.
- Existing signer, fee/packet guards, durable identity binding, same-bytes transport,
  reconciliation and USDC balance-delta accounting remain authoritative.
- Rollback is a source revert before activation; no schema, settings or live state migration.

## Acceptance

Gamma receives this note before code. First tests must fail against the missing function.
Require fixture-backed formula/fee/account tests, reserve insolvency refusal, tail input equal
signed SOL floor, failed simulation/no payload, packet bounds, and seam failure preserving
existing behavior. Run focused tests with `-n 2`; no full suite or provider calls. Independent
source review remains required before any later operator-authorized live use. The builder
requires slippage at least the first existing exit-ladder rung. Its injected offline simulations
are integration tests, not proof that the live program accepts the transaction or enforces the
net minimum; actual simulation before signing remains mandatory in the runtime path.
