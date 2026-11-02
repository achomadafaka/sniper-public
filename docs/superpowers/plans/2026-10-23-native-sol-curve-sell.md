# Native SOL curve full sell implementation plan

Goal: supply the missing locally priced normal SOL-curve full-sell fallback, partial native
selection and desk coverage beyond the fastlane cap, offline (all three human-approved).
Spec: `docs/superpowers/specs/2026-10-23-native-sol-curve-sell-math.md`.
Architecture: reuse Pump account/PDA helpers and the existing atomic tail assembler; inject
RPC/Jupiter calls. Add one normal SOL branch and one guarded full-exit selection point.
Stack: Python integer/Decimal arithmetic, existing solders, pytest with two workers.

Constraints: 5/$25/$200/-$200 unchanged. No push/deploy/restart/provider/outcome operations.
No new entry-path calls or funded state. Default refuse unsupported modes/layouts. Gamma's
math precheck precedes code. A confirmed frozen sell receipt is a distinct evidence gate.

1. Freeze Gamma's math verdict and receipt limits. Use reviewed Lambda fee-parser code as a
   pinned source dependency if needed; do not duplicate its fee parser.
2. Write failing tests for independent literal gross/fees/minimum, same-state gross reserve
   check, zero/default creator, configurable override, malformed states and unsupported modes.
   Run RED. Add only the pure math/state handling needed to pass.
3. Write a failing ABI/composite test: exact 16 normal legacy sell accounts, amount/minimum
   signed in the transaction, WSOL tail input equal to the guaranteed minimum and USDC floor.
   Reuse existing tail packet-shaping and simulation code; prove existing exotic tests stay green.
4. Add guarded full-sell fallback selection at the existing quote seam. Test new refusal/raise
   retains the old result, successful Jupiter stays unchanged, exact full atomic balance is used,
   and failed simulation/oversize never yields signed material. Beta owns entry integration.
5. Run focused math, builder and seam tests with `-n 2`. Request independent review of money
   arithmetic, current ABI/modes, failure containment and old-path preservation. Fix findings.
6. Commit only this worktree's reviewed source/tests/docs. Send pinned source evidence to the
   crew and record the handoff via the fixed brain CLI. No push or runtime activation.

Review focus: gross versus net solvency, separate ceil fees, Global override gate, exact atomic
identity, signing the conservative floor, tail funding/order, fail-closed unsupported state and
preserving old results under new fallback failure. Partial selection/fastlane coverage are
separately verified parts of the approved scope.

## Execution ledger

- `51b953a71`: pure arithmetic helper, frozen evidence and original math contract. RED absent
  helper, GREEN48 focused tests; Gamma's pure-helper review PASS. Not native sellability proof.
- `36cbee7ff`: default fastlane selection covers every open, unpaused REAL position; explicit
  caller limits and admission rails remain unchanged. RED2, GREEN79 across five focused files.
- Corrected fixture `b50a4404a` independently checked offline: 17 SOL rows plus 22 excluded
  quote-token rows. Initial supported mode rows2/3/4/15; only row3 is legacy16. Global,
  confirmation and privilege provenance limits remain explicit in the fixture receipt.
- Builder RED: missing `native_curve_sell`; implemented one four-account snapshot, reused
  FeeConfig parsing, Global override gate, exact legacy16 sell and shared tail assembly.
  Unsupported modes/layouts, unmodelled fees, malformed tail, failed simulation and oversize
  packets return no executable payload. Supplied state is an entry probe only.
- Seam RED: missing explicit-no-route selection, probe signer guard, native partial selection,
  partial position binding and fresh native intent after unsigned build failure. The ordinary
  quote/build calls are recorded without another request. Per-call fallback state never crosses exits.
- Ruling: retry native after an explicit unsigned Jupiter SOL-curve build refusal only on the
  next existing rung, after the executor resets its intent. Infrastructure, unknown failures,
  generic invalid requests and malformed hops do not request native selection. No native bytes
  sign against a Jupiter floor; a failure on the final rung gets no extra attempt.
- Independent builder review PASS. Seam review found malformed-hop and build-infrastructure
  trigger gaps; eight RED regressions reproduced both. A further HTTP400 simulation-timeout
  control reproduced RED1. Concrete program verdicts now gate failed builds; all findings closed.
- Initial ruling: preserve the original branch base during this offline build. Main independently
  added Beta's exit fee clamp in `_exit_sign`; integration must retain it before activation.
  No push, merge/rebase, deployment, restart, provider/outcome read or trading DB/env write.
  Entry readiness wiring remains Beta's separate scope. Brain reads use the fixed CLI;
  no brain writes are authorized in this chat.
- Final verification: **412 passed**, `python3 -m pytest -n 2 -q` across 16 focused files
  (math, builder, seam, exotic native, PumpSwap, source partials, quote-miss typing, stop quote,
  overlap reads, intent, shared transport, timestamps and fastlane). Schema reference validation,
  five-file source compilation and `git diff --check` pass. No full suite was run.
- Local recorder benchmark (3,000 unnetworked quote/build pairs): direct p50/p90
  **0.403/0.433 microseconds**, recorded **2.438/2.558 microseconds**. No added entry-path
  call/lock/retry; no live latency or sellability claim.

Small reproducible gate (offline; regression tests were observed RED before implementation):

```bash
python3 -m pytest -n 2 -q tests/test_pump_sol_curve_sell{,_builder,_seam}.py
```

## Local main integration (operator-authorized)

The later operator instruction authorizes merging latest local main, resolving conflicts and
getting Gamma's review; push, deployment, restart and live calls remain unauthorized.
Merge parent is local main `071f20d8cfd8ed612b737e36494612014d1f4cd7`, which includes the
exit fee clamp and the documentation commits ahead of local `origin/main` (`7ba977fc6`).
This supersedes the earlier preserve-base ruling; the original native implementation remains
the first parent `e448c474ad0a28fa50e58a107d6851b1320eee99`.

The sole conflict in `_exit_sign` is resolved as verify gate -> failed-build return ->
`_clamp_exit_fee` -> `sign_swap_tx`; main's `sign_timing().exit_fee` metadata is retained.
The clamp implementation, separate other-holdings caller and existing entry behavior are
imported intact from main.

`tests/test_native_sol_sell_fee_clamp.py` builds real native sell bytes and tests both the
pure clamp and the real exit sign seam. A 600k-CU transaction requested at 100M micro-lamports
per CU (60M lamports) is clamped to 5M per CU (3M lamports), with bounded fee metadata, valid
custody signature and unchanged sell/tail instructions, identities and floor. RED before
merge: absent clamp helper; GREEN2 after resolution. Bypassing the clamp in an isolated test
process reproduces RED (`60000000 != 3000000`), without editing production source.

The already approved partial-selection change is funded behavior: de-risk quotes now use
`_exit_quote`, inheriting exotic payout-native/PumpSwap selection and account-cap retry. Source
partials retain exact executor-owned atoms. No extra entry calls, policy edits or live proof
are introduced by this conflict resolution.

Merge verification: **850 passed across 37 focused files**, all with `-n 2` (45.69s).
Coverage includes the original native/partial/fastlane suite, the real native-clamp regression,
main's clamp/fee/entry-footprint tests, Pump fee parsing, desk/native-entry/prequote/cost paths,
other holdings and shared Sender/Nozomi behavior. Static compilation of all 34 imported Python
files, schema references, funded-ledger handle checks and diff whitespace checks pass. Main's
sender/clamp implementation and other-holdings caller are byte-identical to the merge parent.
Gamma's review is requested against the committed merge SHA; no runtime proof is claimed.
