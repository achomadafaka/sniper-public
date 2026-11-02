# Wallet identity implementation plan

> Execute inline with `superpowers:executing-plans`. Operator approved the design and requested
> execution; preserve their no-full-suite instruction and do not add another approval cycle.

**Goal:** Show proven account addresses and evidence-labelled possible alternate wallets in Universe.

**Architecture:** Read existing proof and research ledgers, build an offline bounded identity artifact,
then attach it to the live Universe response under current proof revalidation. Preserve address nodes.

**Tech Stack:** Existing Python/SQLite, atomic JSON publication, vanilla JS, pytest and Playwright.

**Spec:** `docs/superpowers/specs/2026-10-10-wallet-identity-design.md`

## Global constraints

- No full suite; every pytest invocation explicitly uses `-n 2`.
- Zero added RPC and zero funded-ledger or authority writes.
- 14-day Solana first-source windows; 60-second observed pre-window.
- Three distinct coins/two UTC days plus a nonhub direct funding clue for suspected-alternate label.
- Existing hub threshold: more than three resolved addresses.
- 10,001 windows / 100,001 early rows; 20 related addresses per UID; ten evidence coins.
- 4 MiB artifact limit; two-hour staleness; current registry revalidation on reads.

## Review focus

- A popular-coin overlap or common exchange funder must remain related, never verified ownership.
- A losing/unpriced coin must stay in the identity denominator independently of profit labels.
- A revoked or conflicting claim must not reappear as verified through an old artifact.
- Legacy missing transaction signatures must remain unknown; new reads retain the actual signature.
- A missing/capped graph input must be visibly unavailable/partial, never measured as no links.

### Task 1: Read-only projection and artifact

Files: new `sniper/persistence/db_wallet_identity.py`, `sniper/services/wallet_identity.py`,
`scripts/research/wallet_identity.py`, `tests/test_wallet_identity.py`.

Interfaces: `load_identity_inputs(ledger, graph, *, since_iso, now_iso)` returns current claims,
windows, early buys, cached funders and coverage; `build_identity_report(inputs, *, now_iso,
own_addresses=())` returns versioned JSON; `read_identity(path, owners, *, chain, now_iso)` returns
per-UID current addresses and research relations; `publish_identity(...)` writes atomically.

- [x] Add literal fixtures for three coins over two days, one losing coin, a duplicate coin,
  one failed window, a direct funding clue, another UID's address, and a shared hub.
- [x] Run `python3 -m pytest -q -n 2 tests/test_wallet_identity.py`; verify missing behavior fails.
- [x] Implement only the interfaces above, with no network imports and bounded readonly queries.
- [x] Verify related/alternate/conflict labels, hand-derived 3/4 support and one unknown,
  revoked proof filtering, unavailable/partial states, and atomic write failure retention.

### Task 2: Preserve receipt identity and refresh research

Files: `scripts/research/wallet_upstream.py`, `scripts/research/wallet_link_graph.py`,
`tests/test_wallet_identity.py`, existing upstream/graph focused tests.

- [x] Add a regression storing an early buy with candidate signature, then load the same signature;
  a legacy row without one must stay inspectable with `None`.
- [x] Confirm red, then add nullable research receipt/order columns and the source comparison counter and populate it from decoded `view()['sig']`.
- [x] Refresh the identity artifact at the end of the existing graph job via the standalone
  publisher; catch/report failure and retain the previous artifact without disturbing graph output.
- [x] Run the identity tests plus `tests/test_wallet_link_graph.py` and
  `tests/test_wallet_link_graph_funders.py`; do not invoke graph live RPC reads.

### Task 3: Live Universe and inspector

Files: `sniper/routes/api_memecoin.py`, `static/wallet_universe.js`,
`tests/test_wallet_identity_route.py`, `tests/test_wallet_universe_navigation.py`.

- [x] Add route coverage proving current verified addresses plus artifact relations reach the
  response, while a suspect address receives no arm state, funded metric or capital attribution.
- [x] Add a browser regression selecting a user and inspecting full addresses, counts, missing
  receipt labels, funding clues and escaped evidence; use the actual production JS.
- [x] Confirm red, attach the identity reader inside the existing current-proof read and render
  one inspector section with expandable evidence. Keep existing graph and authority behavior.
- [x] Run the new focused tests and directly affected Universe/navigation/graph regressions.
- [x] Run the offline CLI against live input DBs to a scratch output; verify no RPC or funded writes,
  count/cap disclosure, and measure projection and API-reader p50/p90.
- [x] Review the whole diff, record validation and limits, and commit the scoped feature.

## Execution ledger

Baseline: `tests/test_wallet_universe.py`: 8 passed, two workers.
Ruling: execute the operator-approved proposal inline; skill stage prompts do not override
“Perfect plan get moving”. Full-suite recommendations are overridden by the operator's explicit ban.

Task 1/2/3 implementation: 236 focused regressions passed in 18.04 s, two workers; no full suite.
Red checks observed: missing projection/persistence (17), unsupported label states (2),
nested corruption/current route/browser wiring (11), invalid evidence clock (1).
Ruling: `no_label` / `our_fill` retain complete pre-buy windows; price outcomes cannot select
identity evidence. Unknown-clock duplicates censor the first coin; capped early-buy intake
reports absences unknown. Funding clues without receipts cannot label a suspected alternate.
Two fixture corrections: patch the service publisher where it is imported; age a valid artifact
by moving the read clock forward instead of fabricating an artifact preceding its own evidence.
Offline current input: 99 users, 946 related addresses, two suspected alternates, 694 source coins,
no caps, 956051 bytes, zero RPC. Five bounded reads: projection p50/p90 295.63/332.79 ms;
current proof + artifact reader p50/p90 29.57/30.61 ms. No entry-path work.
Fresh whole-branch review: five findings, all fixed after red regressions. Registry refutations
veto legacy claims; Pump UID conflicts remain separate; absent/capped samples stay unknown;
nested caps/funding addresses are validated; missing indexes cannot manufacture earlier order.
Persist nullable order_proven plus n_order_unknown. Legacy zero-second rows are censored and
uncertain ordering prevents absence counts. This expands research schema only, never funded state.
Final focused validation: 276 passed in 18.56 s, two workers. Reviewer recheck: no blockers.
Current offline sample: 99 users, 947 relations, two suspected alternates. Roveryaps has six
positive-lead supporting coins after censoring legacy same-second rows; redemptionjizzy has three.
Ruling: integrate and activate the approved end-to-end feature; no extra skill menu or full suite.
Web release and the existing research cron get the new code; funded runtime and authority stay
untouched. The graph cron retains its exact schedule, lock, RPC budgets and projection command.

Live activation: feature commit 6f82add8b merged/pushed, pinned web deployed and authenticated
Universe responses verified across all seven supported chains. The research cron code path
switched to the web pin; its schedule/lock/budgets stayed unchanged. Funded pin, PID/create-time
and all 55 armed authority rows matched the pre-activation snapshot. No funded restart.
Final benchmark: publisher p50/p90 324.03/360.66 ms; proof + reader 34.31/35.71 ms (n=5).
Production verification: docs/audits/2026-10-10-wallet-identity-live-verification.md.
No full suite or live provider calls. Next organic hourly research run has not been observed.

### Funding-link follow-up, 2026-10-10

Operator requested a usable link while common ownership remains unproven. The dated spec
amendment adds a separate curated confirmed-receipt artifact and clickable funding/repeat-buyer
addresses, with exact amounts, current account-claim revalidation and no authority writes.
Red checks observed for missing verified funding (three), absent UI links (one), and an activity
artifact impersonating receipt verification (one). 94 focused identity/route/navigation tests
passed in 13.09 s; 23 follow-up checks passed in 2.11 s, including two added direction/fraction
and stale-sample regressions. Two workers throughout; no full suite.
The two actual saved receipts validate offline: redemptionjizzy 1 SOL, Roveryaps 14 SOL.
Both link to their current verified main address with ownership unproven. Receipt artifact:
4,087 bytes. Current-proof + reader p50/p90 28.87/62.82 ms (n=5), compared with 27.02/66.32 ms
before this patch. No fill-path work or new provider request. Activation evidence belongs in
`output/research/funding-wallet-links-20261010/`; web pin only, funded runner preserved.

Knowledge Sync audit: doctrine n; module lifecycle n; analyzer behavior n; focused validation y;
new workflow n; existing spec amended y; semantic separation y; Supabase surfaces n; funding
finding y; agent instructions n; legacy status n. No known brain row is superseded; the dated
spec amendment preserves the original activity contract. Proposed finding only; audit mode
does not write Supabase.
Final reviewer found two issues; five failing regressions reproduced them. Deduplication now
retains distinct directed transfers within one receipt, and malformed omission counts censor
activity while preserving current account addresses and independent funding proof. Final backend
validation: 63 focused tests passed in 1.88 s; reviewer recheck has no blockers.
