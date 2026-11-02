# Memecoin Thesis Capture + Telegram Latency — Implementation Plan

> **For agentic workers:** TDD, one failing test → minimal impl → green → commit. Steps use `- [ ]`.

**Goal:** Capture curated traders' `thesis_created` feed items into `memecoin_theses` (display-only, stance = objective PnL bucket) and add a fail-closed Telegram alert on curated large-buy detection, instrumented to test front-run latency vs fomo's push.

**Architecture:** Additive on the existing memecoin subsystem. New pure normalization (`normalize_thesis_item`) + new tables via the `ensure_memecoin_tables` migration pattern + new best-effort hooks in `run_once` that never touch the copy/exit path + new read-only API + UI cells.

**Tech Stack:** Python 3, SQLite (WAL), Flask blueprint, vanilla JS. `urllib` for Telegram (injectable `http`).

## Global Constraints
- Thesis capture is **display/context only** — never mutates copy decision, gate, verdict, position, sizing.
- No sentiment/NLP. `stance` = objective bucket of `their_pnl_pct` (percent).
- Telegram is external send: **default off** (`MEMECOIN_TG_ALERTS`), fail-closed on missing config, operator sets the bot token (Claude never handles it).
- All new hooks in `run_once` are `try/except`-wrapped best-effort; a failure must not break the copy path.
- Distinguish `None` (unknown) from `0.0` (known zero) for all their-PnL fields.
- Thesis text is attacker-controlled: HTML-escape in UI, never feed to an LLM, never auto-fetch links.
- Curated-only capture; `signal_source='global_feed'` only for alerts.
- Run `python3 scripts/validate_schema_refs.py` + `brain_index.py generate` before commit; regression test per bug.

---

### Task 1: `memecoin_theses` + `memecoin_alert_latency` tables

**Files:** Modify `sniper/persistence/db_memecoin.py` (`ensure_memecoin_tables`). Test `tests/test_memecoin_theses.py`, `tests/test_memecoin_alert_latency.py`.

- [ ] Test: after `ensure_memecoin_tables`, both tables exist with expected columns (`PRAGMA table_info`); idempotent (call twice, no error).
- [ ] Impl: CREATE IF NOT EXISTS both tables per spec §1.1 / §2.2 + indexes `idx_mc_theses_user (source_user_id, created_at)`, `idx_mc_theses_mint (mint, received_at)`. Add to the existing `ensure_memecoin_tables` body before `conn.commit()`.
- [ ] Green + commit.

### Task 2: `normalize_thesis_item` + `ThesisEvent`

**Files:** Modify `sniper/feeds/fomo_feed.py`. Test `tests/test_memecoin_thesis_normalize.py`.
**Interfaces produced:** `ThesisEvent` dataclass; `normalize_thesis_item(raw, received_iso, server_epoch) -> Optional[ThesisEvent]`; `_numN(v) -> float|None`.

- [ ] Tests: (a) non-`thesis_created` → None; (b) real fixture (top-level `id`/`userId`/`tokenAddress`, `body.comment/commentId/percentageUnrealizedPnl/...`) → populated event, `their_pnl_pct` preserved as percent, `their_notional_usd` set; (c) empty/whitespace comment → None; (d) URL-only comment → kept, `has_link=True`; (e) missing top-level `id` → `synth-` id; (f) missing `percentageUnrealizedPnl` → `their_pnl_pct is None` (not 0.0); (g) `server_epoch` + `createdAt` → `lag_s` computed, else -1.0; (h) missing `userId`/`tokenAddress` → None.
- [ ] Impl: `_numN` (returns None on missing/unparseable), `ThesisEvent`, `normalize_thesis_item` per spec §1.2. Reuse the `hashlib` synth-id idiom and the `lag` computation from `normalize_feed_item`. `has_link` via `re.search(r'https?://', comment)`.
- [ ] Green + commit.

### Task 3: thesis persistence + reads

**Files:** Modify `sniper/persistence/db_memecoin.py`. Test in `tests/test_memecoin_theses.py`.
**Interfaces produced:** `record_thesis(conn, th) -> bool`; `list_theses(conn, uid=None, mint=None, limit=50)`; `latest_thesis_by_trader(conn, uids) -> {uid: row}`; `token_thesis_counts(conn, mints, within_hours=24) -> {mint: int}` (windowed on corrected real-UTC `received_at`).

- [ ] Tests: record returns True first time / False on dup `fomo_thesis_id`; `list_theses` newest-first + uid/mint filters + limit; `latest_thesis_by_trader` returns latest per uid over a batch; `token_thesis_counts` counts within window, excludes older.
- [ ] Impl per spec §1.3 (INSERT OR IGNORE; keep-first). `record_thesis` accepts the `ThesisEvent.__dict__`.
- [ ] Green + commit.

### Task 4: `thesis_stance` classifier

**Files:** Create `sniper/analysis/memecoin_thesis.py`. Test `tests/test_memecoin_thesis_stance.py`.
**Interfaces produced:** `thesis_stance(their_pnl_pct) -> str` (`underwater|flat|winning|unknown`).

- [ ] Tests: None→unknown; ≤ underwater threshold → underwater; ≥ winning → winning; between → flat; boundary values.
- [ ] Impl reads `MEMECOIN_THESIS_UNDERWATER_PCT`/`MEMECOIN_THESIS_WINNING_PCT` from settings.
- [ ] Green + commit.

### Task 5: settings knobs

**Files:** Modify `sniper/settings/memecoin.py`, `.env.example`. Covered by Tasks 4/6/8 tests.

- [ ] Add the 9 knobs from spec §3. `MEMECOIN_TG_BOT_TOKEN`/`CHAT_ID` fall back to `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`. Document in `.env.example` (no secrets).
- [ ] Commit (folded into Task 4 or 6 commit if trivial).

### Task 6: Telegram sender

**Files:** Create `sniper/services/memecoin_telegram.py`. Test `tests/test_memecoin_telegram.py`.
**Interfaces produced:** `send_telegram(text, *, token=None, chat_id=None, http=None, timeout=None) -> (bool, float)`.

- [ ] Tests (injected `http`): success → `(True, ms>=0)` and correct URL/payload (`chat_id`, `text`); missing token or chat → `(False, 0.0)` with **no** http call; http raises → `(False, ms)` swallowed; env resolution prefers `MEMECOIN_TG_*` then `TELEGRAM_*`.
- [ ] Impl per spec §2.3. Never raise. Log-once on missing config.
- [ ] Green + commit.

### Task 7: alert persistence + summary

**Files:** Modify `sniper/persistence/db_memecoin.py`. Test `tests/test_memecoin_alert_latency.py`.
**Interfaces produced:** `record_alert(conn, fields) -> bool` (dedup `fomo_event_id`); `update_alert_sent(conn, fomo_event_id, sent_at, ok, ms)`; `alert_latency_summary(conn) -> dict`.

- [ ] Tests: record dedups; update sets sent fields; summary returns `n`, `median_lag_s`, `median_send_ms`, `send_ok_rate`, `recent` (bounded), correct medians on a known set.
- [ ] Impl per spec §2.2/§2.5.
- [ ] Green + commit.

### Task 8: `run_once` hooks (thesis capture + alert)

**Files:** Modify `sniper/services/memecoin_runner.py`. Test `tests/test_memecoin_runner_thesis_alert.py`.
**Interfaces consumed:** Tasks 2/3/4/6/7 + existing `run_once` shape.

- [ ] Tests (fake feed http returning buys + thesis_created + non-curated items, injected `fill_fn`/`stats_fn`, stub `send_telegram` via a module-level injectable or monkeypatch):
  - thesis capture: curated thesis stored; non-curated thesis NOT stored; `MEMECOIN_THESIS_ENABLED=0` → none stored; a `record_thesis` raising does not break copy (counts unchanged).
  - alert: `MEMECOIN_TG_ALERTS=0` → no send; enabled → send fired once per new curated buy; dedup on re-delivered event (second `run_once` same feed → no second send); per-poll cap respected; sell events / non-curated never alert; a send failure is swallowed (copy counts intact); alert row logged with lag + send fields; thesis attached to message when present.
- [ ] Impl per spec §1.4 + §2.4. Thesis capture after `curated` computed; alert inside the `for ev in mine` new-event branch, **before** copy fan-out, `alerts_this_poll` counter, all wrapped best-effort. Inject the sender as a `run_once` param (`alert_fn=send_telegram`) for testability, mirroring `fill_fn`/`stats_fn`.
- [ ] Green + commit.

### Task 9: API endpoints

**Files:** Modify `sniper/routes/api_memecoin.py`. Test `tests/test_api_memecoin_theses.py`.
**Interfaces consumed:** Tasks 3/4/7.

- [ ] Tests (Flask test client on the blueprint): `/api/memecoin/theses` returns rows with `stance` decorated; `?uid=`/`?mint=` filter; `/api/memecoin/traders/<uid>/theses` aliases; `/api/memecoin/alert-latency` returns summary; payloads are JSON-safe (raw comment preserved for the client to escape).
- [ ] Impl the endpoints per spec §1.6/§2.5; decorate combined/gated rows with `latest_thesis`+`stance`+`thesis_age` (add to `list_curated_with_stats` via `latest_thesis_by_trader`, or in `_decorate_rows`).
- [ ] Green + commit.

### Task 10: UI

**Files:** Modify `static/app.js` (+ minimal CSS in `static/` if needed). Manual verification (JS has no unit harness here).

- [ ] Traders table: Thesis cell (truncated text + `stance` badge + corrected-clock age).
- [ ] Per-trader panel: Theses list (ticker · PnL%@post w/ stance colour · notional · age · comment; links click-through only).
- [ ] Token/position expand: curated theses on the mint + confluence count.
- [ ] All text via `_esc`/textContent (XSS). Verify against the live board after deploy.
- [ ] Commit.

### Task 11: review, verify, ship
- [ ] `python3 -m pytest tests/ -q` (full suite green).
- [ ] `python3 scripts/validate_schema_refs.py` + `brain_index.py generate` (+ commit index if changed).
- [ ] Adversarial review: `platform-reviewer` (code/DB/injection/concurrency/clock) + `research-methodologist` (latency-test methodology honesty). Fix real findings.
- [ ] Live smoke: one `run_once` against the real feed writes a curated thesis; verify a row + stance. Telegram plumbing verified only after operator sets the token (state the manual step).
- [ ] Merge to main; update HANDOFF + MEMORY; KSG block in the commit/PR.

## Self-review
- Spec coverage: every §1–§3 item maps to a task (theses table→T1, normalize→T2, persistence→T3, stance→T4, settings→T5, sender→T6, alert store→T7, run_once→T8, API→T9, UI→T10). ✓
- Type consistency: `normalize_thesis_item`/`ThesisEvent`/`_numN` (T2) consumed by T3/T8; `send_telegram` signature (T6) consumed by T8; `record_alert`/`update_alert_sent`/`alert_latency_summary` (T7) by T8/T9. ✓
- No placeholders. ✓
