# Memecoin X Source Graph Rollout and Readiness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to execute this gate sequentially. Do not delegate live browser/account actions. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the X source graph is reliable, display-only, reconciled, and restartable before enabling its read models, notifications, or optional LLM worker.

**Architecture:** Roll out in gates: capture evidence, human card audit, disposable correlation, read-only dashboard, deterministic notification dry-run, then optional LLM dry-run. Each gate can stop the rollout without affecting Fomo capture or funded trading.

**Tech Stack:** Existing userscript/Tampermonkey capture, local receiver, SQLite reports, pytest, systemd user services, Flask dashboard.

## Implementation Status — 2026-07-14

- Code complete: capture/normalization/store, adaptive v1.5 browser core, forward correlator,
  rolling read models, read-only API/UI, deterministic events, disabled-by-default notifications,
  optional tool-free LLM jobs, audit sampler, and restartable user-unit definitions.
- Fixture gate: 284 focused Python tests and 24 browser-core tests pass; schema and JavaScript
  syntax checks pass.
- Not installed/enabled: the new user units, live notification sender, and live LLM provider.
- Operational status: X recovered without a forced retry. The restarted receiver accepted
  parser-v2 runs at `2026-07-14T18:34:49Z` and `18:42:23Z`; both were corrected-clock `ok`
  passes with valid pacing metadata.
- Acceptance consequence: a fresh 24-hour/90-run window started at the first healthy parser-v2
  pass. Its first two runs contain six cards, zero coverage gaps, zero invalid pacing rows, and
  no secret/forbidden-source hit. Gate 1 remains pending only on elapsed time and run count; the
  human audit and disposable reconciliation still follow it in order.

## Global Constraints

- This rollout does not alter `.runtime/memecoin/current`, funded runner configuration, positions, execution, wallets, or capital.
- Robinhood `4663` support here means source extraction/correlation/display only. It does not implement Robinhood wallet funding, routing, entry, exit, or custody.
- Never delete bad capture rows. Quarantine them through the report window and preserve diagnostics.
- Never call the X API, auto-follow, post, reply, like, or bypass a challenge.
- Service installation/enablement and live notification/LLM calls require explicit operator approval.

---

### Gate 1: Complete the 24-hour capture acceptance

- [ ] Keep the dedicated burner Chrome open on `Siguiendo`, machine awake, receiver active, and corrected clock snapshot fresh.
- [ ] Wait until the start marker has at least 24 corrected hours and at least 90 finished runs.
- [ ] Run:

```bash
SINCE=$(sed -n '1p' data/.mcx_capture_start)
python3 scripts/report_memecoin_x_capture.py \
  --db data/memecoin_x.db \
  --since "$SINCE" \
  --clock-offset-file /home/bigpapi/Desktop/Sniper/data/clock_offset.json \
  --json
```

- [ ] Require `acceptance=true`, `healthy_clock_rate>=0.95`, `coverage_gap_rate<=0.05`, `max_gap_seconds<=960`, no stale run, no operator state, no profile lock, parse failure rate `<=0.01`, and no secret/forbidden-source hit. The 90-run/960-second pair matches adaptive 10-15 minute idle cadence while allowing at most 60 seconds of bounded pass overhead; the earlier 300-run/900-second gate was mathematically incompatible with v1.5 pacing.
- [ ] If false, retain the report, fix the named blocker under TDD, and start a new acceptance window only when the blocker invalidates the old window.

---

### Gate 2: Human-audit raw cards against stored facts

**Files to create during implementation:**
- `scripts/sample_memecoin_x_audit.py`
- `tests/test_memecoin_x_audit_sample.py`

- [x] Implement a read-only stratified sampler that emits JSON/CSV rows containing post URL, canonical ID, author, actor, relationship, observed time/domain, full text, extracted URLs, network, mint, ticker, eligibility, parser version, and diagnostic codes.
- [ ] Generate the gate sample from the fresh window only: `python3 scripts/sample_memecoin_x_audit.py --db data/memecoin_x.db --limit 30 --parser-version 2 --since "$SINCE" --output .logs/memecoin-x-audit.json`.
- [ ] Sample at least 30 observations. Include direct/repost/quote/reply when available; include Solana, Robinhood `4663`, and another EVM network when available. Categories may overlap.
- [ ] A human opens the stored post URL in the burner and checks canonical ID, author/actor attribution, relationship, visible text, contract, and network. Deleted posts remain auditable from raw stored cards and are marked unavailable live.
- [ ] Require zero wrong author/actor/relationship/network/mint facts. Text truncation is allowed only when `text_complete=0` and the mention remains ineligible.
- [ ] Store the signed audit artifact under `.logs/`; do not commit captured post text or HTML.

---

### Gate 3: Run Phase 3 against disposable state

- [ ] Copy `data/memecoin_x.db` to a temporary DB.
- [ ] Open `/home/bigpapi/Desktop/Sniper/sniper_tracker.db` read-only.
- [ ] Run the Phase-3 correlator against the copy.
- [ ] Re-run it and require zero duplicate logical links.
- [ ] Run the source report and reconcile each count to direct SQL.
- [ ] Explicitly query `4663`, `8453`, `1`, and `56`; identical `0x` addresses must remain separate by network.
- [ ] Only after reconciliation, run one incremental correlator pass against the real isolated X DB. This writes only `memecoin_x_*` tables.

---

### Gate 4: Install restartable capture/correlation services

**Files to create during implementation:**
- `systemd/sniper-memecoin-x-receiver.service`
- `systemd/sniper-memecoin-x-correlator.service`
- `systemd/sniper-memecoin-x-correlator.timer`
- `systemd/sniper-memecoin-x-signals.service`
- `systemd/sniper-memecoin-x-signals.timer`
- `scripts/install_memecoin_x_services.sh`
- `tests/test_memecoin_x_systemd_units.py`

- [x] Receiver service binds only `127.0.0.1:8787`, uses the explicit corrected-clock file, restarts on failure, and never launches Chrome.
- [x] Correlator/signals are one-shot services controlled by one-minute timers. Systemd unit activation prevents overlap; audited `running` rows expose interrupted passes and completed cursors remain idempotent.
- [x] Installer rewrites repo root like `scripts/install_user_services.sh`, installs units, runs `daemon-reload`, but does not enable notification or LLM services.
- [x] Unit tests assert loopback binding, explicit DB paths, absence of funded-runtime commands, and default-disabled notification/LLM units.
- [ ] Operator separately verifies reboot behavior: services return; Chrome/Tampermonkey still require the logged-in desktop session. Missing Chrome produces coverage gaps, never fake healthy capture.

---

### Gate 5: Enable read-only API/UI

- [ ] Start the web server with `MEMECOIN_X_DB_PATH` pointing to the isolated X DB.
- [ ] Verify unavailable, healthy-empty, and populated states.
- [ ] Reconcile UI 24h/7d/30d totals to the CLI report.
- [ ] Confirm official context is separate and suggestions have no follow action.
- [ ] Confirm no X API route or UI control can buy, sell, size, veto, follow, or mutate execution state.

---

### Gate 6: Dry-run deterministic notifications

- [ ] Run signal builder and notification worker on a disposable DB.
- [x] `--dry-run` renders pending Telegram previews from a read-only connection and does not mutate attempts or outbox state.
- [ ] Review at least 20 rendered messages or all messages if fewer exist.
- [ ] Require exact evidence tuple, correct 60-minute consensus, bounded duplicates, display-only wording, and no forecast/safety/action language.
- [ ] Live Telegram remains disabled until the operator explicitly approves the dry-run and sets both the enable flag and credentials.

---

### Gate 7: Dry-run optional LLM enrichment

- [ ] Use frozen fixtures and a fake provider first.
- [ ] Run an adversarial batch containing prompt injection, malformed identity, stale evidence, duplicate jobs, timeout, invalid schema, over-budget, and unsupported claims.
- [ ] Review accepted annotations against every cited evidence ID.
- [ ] Enable event, digest, and on-request modes independently. One mode passing does not enable the others.
- [ ] Provider failure must leave deterministic events/notifications unchanged.

---

### Gate 8: Branch close and knowledge sync

- [ ] Run the complete focused X suite, `node --check static/app.js`, schema generation/validation, and `git diff --check`.
- [ ] Run the Knowledge Sync Gate. This adds a new implemented subsystem and plans, so update the existing architecture memory rather than claiming `Delta: none`.
- [ ] PR body ends with the 11-question Knowledge Delta Report.
- [ ] Do not merge, deploy, restart persistent services, or enable live notifications/LLM without operator approval.

---

## Readiness Definition

The X source graph is ready when Gates 1-5 pass. Deterministic Telegram readiness additionally requires Gate 6. LLM readiness is optional and separately requires Gate 7.

Even after all gates pass, the lane remains display-only. Any proposal to affect entries, sizing, vetoes, exits, capital allocation, or Robinhood execution requires a new design, explicit approval, paper validation, at least 100 clock-valid forward links, and a preregistered held-out evaluation beating both flat and agreed on-chain baselines.
