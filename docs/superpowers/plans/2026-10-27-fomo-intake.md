# Fomo Intake Implementation Plan

> **For agentic workers:** Use subagent-driven development for the pure adapter; Codex owns the browser CLI and integration. Work in the shared checkout, preserving other authors' files.

**Goal:** Collect five Fomo discovery lists into the existing intake store without trading or exporting authentication.

**Architecture:** One process owns one private persistent Chrome profile and one discovery tab. It reads one list every 15 seconds through the page; current verified transport is the rendered DOM, explicitly partial. A JSON source is not trusted until its exact public-list response shape and list binding are verified; no guessed endpoints or arbitrary account responses.

**Tech Stack:** Existing Python Playwright, standard library, Beta's intake store.

**Spec:** Alpha's authenticated operator assignment and approved dedicated-profile design, 2026-10-27; the approved scope below is authoritative.

## Approved scope and constraints

- `feed=fomo`; filters `trending24h`, `blue_chips`, `most_holders`, `graduated`, `bonding`.
- Capture our receipt clock and offset before parsing; unknown offset means unknown corrected time.
- Preserve raw fields, chain identity and Solana case; reject conflicting known identities. Never treat visible prices as execution marks.
- Use `open_day(out_dir, 'fomo', at)` and `record_poll(conn, 'fomo', filter, at, rows, offset_s=offset, error=reason)` from reviewed `beta/desk-intake` (`fb24883da` or successor). Codex does not edit that module.
- Profile outside checkout, mode 0700; operator performs one-time login. No copied cookies, credential extraction, account actions, `.env` changes, production ledger access, cron, restart, or money actions.
- Login/challenge/HTTP 401/403/429 writes one named error and durable halt, then stops. No automatic restart through a saved halt.
- One process/profile lock; bounded navigation/selector timeouts and row count; no parallel provider reads or catch-up bursts. Playwright renderer evaluation has no hard execution deadline.
- Scope excludes full-list completeness through virtualized DOM, JSON mapping without verified fixtures, market-edge claims and trading authority.

## Review focus

- Wrong list or account action: target only observed list buttons; verify selected state before capture.
- Auth failure during navigation: record typed refusal before any fallback success or later poll.
- Missing versus zero: malformed/unknown fields stay unknown; empty DOM without an explicit empty-state proof is an error.
- Restart/midnight: saved halt blocks browser launch; shared first sightings persist across daily files.
- Credentials and privacy: never inspect or log request headers, cookies, local storage, authentication payloads or account balances.

## Task 1: Pure Fomo adapter

**Owned files:** `sniper/feeds/fomo_intake.py`, `tests/test_memecoin_desk_fomo_intake.py`.

**Interfaces:** `parse_dom_rows(rows)` returns generic-writer records or `None` per malformed item; `halt_reason(url, text, authenticated, status=None)` returns a typed refusal or `None`. Constants define five filter names and observed Spanish labels, with separately verified labels added only when known.

- [x] Write tests for route/row-id identity, chain case, malformed items, raw/partial metadata and auth/challenge/HTTP refusal; confirm RED.
- [x] Implement the smallest pure adapter using existing address validators and no auth client or funded hooks.
- [x] Run focused tests with `-n 2`; root reviews output. Adapter plus normalization checks: 102 passed. Root review added RED/GREEN cases for preserving unmapped chains and rejecting unexpected origins.

## Task 2: Dedicated-profile collector

**Owned files:** `scripts/memecoin_desk_fomo_intake.py`, `tests/test_memecoin_desk_fomo_worker.py`, this plan.

- [x] Write RED tests for profile isolation/0700, one process, receipt clocks, list cadence, partial DOM attribution and stop-before-next-poll on refusals.
- [x] Implement explicit profile initialization and operator login command, plus bounded collection/optional loop. No browser starts during import or tests.
- [x] Exercise real temporary intake DBs; validate no empty-on-error, global first sightings and typed halt/restart behavior.
- [x] Run focused tests and schema checks: 140 passed after the independent review repair; no schema violations. Commit only owned paths with `scripts/crew_commit.sh`.
- [x] Send exact SHA to Zeta for independent review. Create the empty private profile and give Alpha the operator login command only after offline checks; no collection cron before PASS.

## Execution ledger

- Approved design: Alpha explicitly approved the dedicated profile, bounded collector, 15-second list cadence, partial DOM fallback and Zeta review.
- Ruling: current browser network inspection is unavailable. Ship the approved, honestly partial DOM fallback; do not invent a JSON endpoint or consume arbitrary account JSON.
- Foundation merged by Alpha as `6a4cbc7d6` (reviewed `7ccd63482`); integration uses shared main source without copying Beta's files.
- No live intake writes precede Gamma's preregistration/freeze gate. Browser reconnaissance is not an enrolled intake dataset.
- Root review confirmed RED/GREEN repairs for API-subdomain refusals, navigation backdating and non-exact list selectors. Refusal receipt clocks are captured in the callback; successful receipt clocks are frozen immediately before DOM extraction.
- Transport limits: the original one-second settle could refuse a still-hydrating header; the approved follow-up replaces it with a positive-readiness window. A header arriving after that window may still be conservatively refused. Renderer evaluation can stall despite bounded navigation/selector operations. No response-freshness claim is made.
- Independent review found and reproduced loss of an HTTP refusal during an interrupted paced wait. The first refusal callback now fsyncs the halt and commits one canonical error poll immediately; the next loop cannot duplicate it or click another list. Interrupt and browser-error regressions pass, and an interrupted refused run returns a failure status.
- Zeta independently passed the scoped repair at `8c914b63fbac43a5c7f168b2916a1dca2b22a81e`, including three targeted interrupt/error/exit-status checks. The integrated 140-test result is Codex's evidence. At initialization the private profile was mode 0700 with only the ownership marker; Codex launched no live browser or collector.

## Operator handoff

The dedicated profile is outside the checkout at `~/.local/share/sniper/fomo_intake_profile` and must be mode 0700. Initializing it starts no browser and writes no intake rows:

```bash
python3 scripts/memecoin_desk_fomo_intake.py --init-profile
```

The operator reports Google refuses the automation-launched `--login` window. End that command and close its dedicated window first. Alpha supplies the operator this ordinary Chrome command; Google sign-in is completed manually:

```bash
/usr/bin/google-chrome \
  --user-data-dir=/home/bigpapi/.local/share/sniper/fomo_intake_profile \
  --profile-directory=Default \
  --disable-gpu \
  --ozone-platform=x11 \
  --password-store=basic \
  https://fomo.family
```

Fully close this dedicated Chrome instance after Fomo shows the signed-in account. Plain Chrome does not acquire `collector.lock`, so it must never share the profile concurrently with collection. Native Chrome Singleton locks are released by closing Chrome; do not auto-delete them.

The installed ordinary Chrome command and Playwright's `channel="chrome"` both use `/opt/google/chrome/chrome` (observed version 156.0.8078.47 on 2026-10-27). The collector accepts the same initialized user-data root/Default profile. The installed Playwright `chromiumSwitches.js:81` includes `--password-store=basic`; Alpha chose to match that store in the manual login command, keeping bot keyring access disabled. No credentials were inspected. This mismatch is a plausible explanation for the failed session reuse, not a proved cause. Reuse after the matching-store login remains unverified.

The collector remains stopped until the operator finishes login and Alpha confirms Gamma's freeze. After the readiness deadline, explicit login evidence becomes `login_required` and missing evidence becomes `auth_unproven`; an HTTP 401 remains an immediate `login_expired` refusal. A saved `halt.json` requires operator review and removal before browser launch; login never silently clears it. No cron is installed by this change.

## Approved authentication follow-up

Alpha approved these repairs after the first live refusal and one authorized headed, no-click diagnostic. That diagnostic showed a visible login control from 0.569 seconds after DOMContentLoaded, no account header through ten seconds, and no rendered list. It established no usable session in that browser context; persistence versus rejected reuse remained unproved.

- [x] Classify explicit login evidence as `login_required` and unknown authentication evidence as `auth_unproven`; retain immediate HTTP 401/403/429, challenge and origin refusals. Adapter and normalization checks: 108 passed after verified RED cases.
- [x] Make `--login` print the ordinary-Chrome command and exit without browser launch, input wait, held collector lock or halt removal.
- [x] Wait up to ten seconds for positive header readiness before the first capture, withhold authentication-only classification during hydration, and preserve immediate durable infrastructure/challenge refusals.
- [x] Run focused integration and schema checks: 154 passed, no schema violations. A real-DOM offline fixture first failed on a hidden-first/localized login button and passed after the visible-button probe repair; no private profile or live provider was used.
- [x] Commit only owned files: `713abfd8c220555e60ffbc3fb8777a21821dd86a`.
- [x] Repair Zeta's CSS visibility finding: a `visibility:hidden` account link still had client rectangles and falsely beat a visible login control. Both probes now require visible computed style and positive dimensions. Actual-DOM cases proved RED/GREEN for inherited hidden style, collapse, display-none and zero-size controls; integrated checks remain 154 passed.
- [x] Obtain Zeta's scoped review of the visibility repair: `45ada675c4fe45689380c9e413ccb00bdf16086e` PASS, with ten independent targeted checks. This is code/test proof only.
- [x] Perform Alpha's authorized headed/no-click diagnostic after PASS. No positive header in 41 samples from 0.040 through 10.003 seconds after DOMContentLoaded; final reason `login_required`. No HTTP 401/403/429 observed, no clicks or enrollment; all five list counts remain unobserved. The browser closed and original halt stayed in place. Result retained as `/home/bigpapi/.cache/codex/fomo-headed-diagnostic-20261027-before-basic-result.json`.

This changes collector readiness and attribution only. It adds no provider request, login automation, trading authority or credential export; missing sessions still halt. Renderer execution itself remains without a hard deadline.

## Approved GPU and password-store follow-up

Alpha reported a host freeze during a Chrome launch and ordered GPU-disabled collector launches plus X11 for headed Chrome. That instruction arrived after the diagnostic above completed. No further live browser run is authorized until the new handoff and review gates pass.

- [x] Add `--disable-gpu` to collector launches and the offline DOM fixture; printed manual login also includes `--ozone-platform=x11` and `--password-store=basic`. Three focused checks failed before the repair; 31 worker tests and 154 integrated checks pass afterward. Keep Playwright defaults; do not read credentials or request keyring access.
- [x] Obtain Zeta's scoped review of this launch-flag repair: `e0b73236ea0f3e349a3d03c1136ebead944f46f9` PASS. Three targeted checks and installed Playwright defaults independently verified; no live/profile/credential inspection. Alpha received the exact manual command.
- [x] Alpha supplies the new command, confirms operator login/reload and closed Chrome, then authorizes one new headed/no-click diagnostic with GPU disabled and X11. Header first appeared at 1.070 seconds after DOMContentLoaded and stayed present through 10.004 seconds. Trending 24h had 15 rendered/parsed rows, zero malformed; other lists were untouched. No refusal, click or enrollment; owned context closed and saved halt preserved. Result: `/home/bigpapi/.cache/codex/fomo-headed-diagnostic-20261027-result.json`.

This proves headed session reuse for that one run. Headless reuse and sustained GPU stability remain unproved.

## Proposed five-list pass and continuous collection

Alpha explicitly authorized Task 3 after Zeta's plan review, then the offline Task 4 build. Zeta required profile-bound process identity for that build. Cron activation and further live runs remain separately gated.

### Task 3: One normal five-list pass

**Files:** Existing `scripts/memecoin_desk_fomo_intake.py`; research state under `data/desk/intake/fomo/`. No new collector code is needed for this pass.

- [x] Obtain Alpha's explicit go for one headless five-list pass, including archiving the saved halt once. Dedicated-profile process/native guards were absent; no native guard was deleted or operator process stopped.
- [x] Verify a finite clock offset (0.05194091796875 seconds at preflight) and Gamma's frozen `feed=fomo` receipt cutoff (`61de43949`, 2026-10-27 10:50:08 UTC). All stored snapshots remain partial; unknown offset cannot create an analysable corrected first sighting.
- [x] Retain the previous halt inside the private profile under a unique dated name, then run exactly once from reviewed source:

```bash
SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper \
  /home/bigpapi/.pyenv/versions/3.13.0/bin/python3 \
  /home/bigpapi/Desktop/Sniper/scripts/memecoin_desk_fomo_intake.py \
  --profile-dir /home/bigpapi/.local/share/sniper/fomo_intake_profile --polls 5
```

- [x] Report each filter's persisted evidence. The pass stopped after 11.099 seconds with exit 2: `trending24h` poll id 2 was `error=auth_unproven`, rows/malformed NULL; the other four filters were unattempted with unknown counts. Zero new intake rows/first sightings. Refusal halt retained, owned context closed/native guards absent. Evidence: `/home/bigpapi/.cache/codex/fomo-five-list-report.json` and `fomo-five-list-result.json`.
- [x] Stop without retry. Headless reuse remains unproved. The saved reports contain no title, URL, visible text length or interstitial snapshot, so they cannot distinguish slow rendering from a different page. Alpha instructed no detection tuning and requested headed operation on Xvfb instead.

### Task 4: Minimal restartable cron owner, after separate build authorization

**Files:** Create `scripts/memecoin_desk_fomo_intake_watchdog.sh` and `tests/test_memecoin_desk_fomo_watchdog.py`; modify the Fomo CLI/worker tests for graceful termination. Coordinate optional registry additions in `scripts/memecoin_desk_preflight.py` and `scripts/memecoin_desk_stop.sh` with their owners before editing; cover them in `tests/test_memecoin_desk_preflight.py` and `tests/test_memecoin_desk_stop.py`. No `.env` change or new enable knob: the cron entry is this collector's activation switch.

**Interfaces:** Reuse `scripts/lib/desk_proc_match.sh` Python/script/state-root identity and additionally bind the resolved profile from argv/default HOME/cwd before treating a process as alive or terminating it. Another profile is untouched; unknown identity is never authority. Start the Fomo CLI with explicit profile, `--headed --loop` under `xvfb-run -a`; it owns the profile lock, five-filter rotation and 15-second list cadence. The DexScreener wrapper cannot launch Fomo. Headed Chrome keeps `--disable-gpu --ozone-platform=x11`; auth/readiness and partial-evidence checks stay unchanged.

- [x] Prove RED on standalone termination during paced wait, startup and durable refusal. Default SIGTERM bypassed cleanup; a direct `KeyboardInterrupt` handler then hung real Playwright `context.close()` while inside its event pump. A local Chrome fixture exposed this second defect; no operator profile or live Fomo was used. Inherited nohup SIGINT was separately reproduced.
- [x] Implement standalone-only cooperative SIGTERM/SIGINT flags, consumed at safe Python boundaries with at most 250-millisecond paced-wait chunks. Existing cleanup and normal/refused exit codes remain. All 37 worker tests pass, including actual local GPU-disabled Chrome PID exit, context/manager cleanup, profile-lock release and no next capture. Imported helpers install no handlers or shared stop state. Playwright manager entry/startup completion and renderer IPC retain no hard shutdown deadline.
- [x] Implement the serialized profile-bound watchdog; all 26 fake-process/display tests pass after behavioral RED cases. It handles halt/native guards, duplicate/superseded/foreign/partial process records and resolved explicit/default/relative/symlink profiles, rechecks identity before TERM and ownership before replacement, and never force-kills. Pending `xvfb-run` identity is also matched so slow display startup cannot launch a second display. The recorded launcher PID is advisory and never signal authority; child lock descriptors are closed.
- [x] Add `--headed` and test both launch modes without detection tuning. The watchdog requires `Xvfb`, `xvfb-run` and `xauth`; virtual screen is 1280x1024x24 with TCP disabled. No Fomo credential export or auth storage read. Xvfb installation remains an operator action, and real Xvfb/session integration is unproved. The [Debian xvfb-run manual](https://manpages.debian.org/bookworm/xvfb/xvfb-run.1.en.html) documents wrapper cleanup after its child exits; runtime proof is still required.
- [x] Add one optional preflight line and one empty-knob stop entry, preserving Delta's existing entries. Two registry regressions proved RED before the change; all 13 registry tests passed afterward using fake inputs. Alpha confirmed Epsilon owns no overlapping paths. No live `.env` read or stop command. Tests follow `Path.iterdir()` plus filtering, no `.glob()`, `.rglob()` or `os.walk()` unless explicitly registered as a core repo-walking test.
- [x] Run focused Fomo/store/watchdog/registry/Dex-watchdog checks and Alpha's `test_memecoin_deploy_core_treewalk.py` gate together: 207 passed with `-n 2`. Shell syntax and scoped diff checks pass; worker schema checks found no violations.
- [x] Commit only owned paths: `135aef159c50bb0da24a6f472c87afb259920f5e`. Zeta scoped offline CODE/TEST PASS: 15 independent lifecycle/watchdog checks, including isolated Chrome cleanup and profile/pending-launcher identity. Real Xvfb/session integration and hard renderer shutdown deadlines remain unproved.
- [x] Acting lead Codex selected `e92d1f2b324d284c3e33309a8823282957b35da4`, containing the reviewed Fomo, Dex and shared intake dependencies. Fomo opens/closes the corrected day independently per poll. The collector pin is independent of the funded runtime; see the activation record below.
- [x] Operator explicitly authorized `sudo apt install xvfb` in the Codex chat. Installed `xvfb` and its matching `xserver-common` dependency, version `2:21.1.22-1ubuntu1.3`, after normal system authentication. Package status is installed; `Xvfb`, `xvfb-run` and `xauth` are available. No browser/display validation or collection was started by installation.
- [x] Alpha explicitly authorizes one headed/Xvfb five-list validation on reviewed `135aef159`, archiving `auth_unproven` once. Run completed in 63.192 seconds: `trending24h` was `empty_dom` with unknown row/malformed counts; `blue_chips=16`, `most_holders=16`, `graduated=15`, `bonding=15`, all zero malformed. Stored 62 rows and 61 new first-sighting records. Header first appeared 1.340096 seconds after DOMContentLoaded and was present at the final 61.778909-second check. No auth/challenge/401/403/429 refusal recorded. CLI exit 0 with one error is partial validation, not an all-five PASS.
- [x] Verify cleanup: Chrome PID 402214 and Xvfb PID 402171 exited; display `:99` socket/lock and native profile guards are absent, collector lock is free. Restore `operator_hold_after_validation`; no retry/cron. Evidence: `/home/bigpapi/.cache/codex/fomo-xvfb-five-list-report.json` plus sibling result/metrics/before files. Initial list data lag is a hypothesis, not a proved cause of `empty_dom`.
- [x] Acting lead Codex installed the minute-level watchdog and reboot entries after the reviewed readiness repair, successful pinned-release validation and the operator's takeover authorization. The worker performs list pacing; cron does not launch a new browser per list. Installed entries:

```cron
* * * * * SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/Desktop/Sniper/.runtime/memecoin/releases/e92d1f2b324d284c3e33309a8823282957b35da4/scripts/memecoin_desk_fomo_intake_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_fomo_intake_watchdog.log 2>&1
@reboot SNIPER_STATE_ROOT=/home/bigpapi/Desktop/Sniper /home/bigpapi/Desktop/Sniper/.runtime/memecoin/releases/e92d1f2b324d284c3e33309a8823282957b35da4/scripts/memecoin_desk_fomo_intake_watchdog.sh >> /home/bigpapi/Desktop/Sniper/logs/memecoin_desk_fomo_intake_watchdog.log 2>&1
```

- [x] Activation proof at 2026-10-27 14:15 UTC: one exact pinned worker and owned Chrome/Xvfb tree, external research DB/log/PID paths, all five filters `ok` with 15–16 rows, zero malformed rows and no errors. Latest per-filter receipt ages were 4.6–64.9 seconds. Refusal-halt behavior retains the reviewed offline proof; no provider failure was induced. A partial snapshot never proves provider-wide absence or full-list coverage.
- [ ] Rollback: Alpha disables only this cron entry and gracefully stops this collector using the proved handler; verify owned Chrome/profile ownership is gone. Retain intake/history/halt evidence. No money, trading-service changes or `.env` edits.

Excluded: JSON response mapping, full virtualized-list completeness, trading/model enrollment, sustained GPU-health claims and automatic login/halt clearing. A normal five-list pass and cron activation are separate operator gates.

## Assigned first-list row-readiness repair

Alpha assigned an offline repair after the partial Xvfb run: wait up to ten seconds for first-list rows after authentication, retain `empty_dom` if still absent, and obtain Zeta review. Alpha owns deployment and any later cron go. No new live pass is authorized.

**Owned files:** `scripts/memecoin_desk_fomo_intake.py`, `tests/test_memecoin_desk_fomo_worker.py`, this plan.

- [x] Prove RED with a fully local, GPU-disabled Chrome fixture whose account header precedes visible list rows. Cover rows never appearing, already-present rows, hard refusal/shutdown while waiting and no readiness delay on later lists. A separate on-count HTTP 429 witness first failed, then passed after guarding the browser metadata return. Malformed-row attribution is preserved; 44 worker tests pass.
- [x] Add a first-poll-only visible-row readiness check after selected-list/24h controls. It reuses the monotonic ten-second deadline, auth guard before/after metadata counts and cooperative waits; no provider request or retry. Already-present rows add no wait; later-list behavior/cadence is unchanged. The successful receipt clock remains immediately before extraction, after readiness; timeout stays typed `empty_dom` with NULL counts.
- [x] Run combined Fomo/store/watchdog/registry/Dex-watchdog and Alpha treewalk checks: 214 passed with `-n 2` in 21.66 seconds; scoped schema/diff checks pass.
- [x] Commit only owned paths: `615418714995f619b145e7657b8c43b1fc29acad`. Zeta scoped source/test PASS with eight independent checks, including the real local delayed-visible DOM, timeout, no retry, on-count refusal and SIGTERM row-wait cleanup. Alpha owns deployment and subsequent cron go; no live rerun was made by Codex.
- [x] Send Gamma the first corrected stored-row time for `feed=fomo`: epoch `1793105532.70739`, UTC `2026-10-27T12:52:12.707390Z`. Gamma confirmed it matches the record; the earlier empty Trending poll is not a stored-row start. Partial DOM, missing steady cadence and unenrolled Trending remain explicit.

This repairs one reproducible readiness failure mode; it does not establish the live cause of `empty_dom`, prove a truly empty provider list, full-list completeness or response-version freshness. Renderer IPC still has no hard execution deadline.

## Current activation record — 2026-10-27 14:15 UTC

The operator delegated Alpha's role to Codex for two hours and allowed isolated builds while keeping main clean. The separate collector pin was disclosed before staging so the authenticated-funded LI.FI canary and funded services could stay unchanged. Release `e92d1f2b3` passed 1,448 required tests, release validation and funded-contract reconciliation with `--dry-run --defer-backup`; the deploy log records `lifi_reactivated=0` and `backup=not-created`.

The release worktree is locked against retention pruning. Codex archived only `operator_hold_after_validation` under the collector lock after verifying native profile guards were absent, then started the pinned watchdog and installed the entries above. The previous crontab is retained at `data/crontab.before-fomo-j7-only-20261027T140927Z.txt`; existing entries were preserved. The research DB has a complete successful five-filter rotation. Earlier headless and partial-Xvfb observations remain historical evidence, not the current activation verdict.

Funded runner and web remain pinned to `a2afb031c29a18c733c0a6c22ccecfb8c95baa0b`, with fresh authenticated heartbeat. No production `.env` write, LI.FI activation, funded restart or money action occurred. This activates collection only; Fomo stays outside the Pump.fun-only desk cohort. Renderer IPC shutdown bounds and long-term GPU/session health remain unproved.
