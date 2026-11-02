# Memecoin V6 Paper Decision Cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw V6 paper card dump with a readable decision cockpit that shows live-paper health, modeled price performance, V6's reason, and the exact rule facts it is watching.

**Architecture:** Keep V6 paper separate from the funded book. Add a deterministic, read-only presentation model to `memecoin_v6_paper_report.py`, then render it through the existing V6 endpoints in a semantic CSS cockpit with lazy detail loading. The engine, capture, quote cadence, entry path, modeled-fill path, and funded runtime remain unchanged.

**Tech Stack:** Python 3, SQLite read-only reporting, Flask JSON endpoints, vanilla JavaScript, CSS, pytest, Playwright browser verification.

## Global Constraints

- Read `docs/superpowers/specs/2026-07-26-memecoin-v6-paper-decision-cockpit-design.md` first.
- Use RED-first TDD and one logical commit per task.
- Do not alter `step(state, observation) -> V6Decision`, rule precedence, thresholds, capture, quote cadence, entry selection, or modeled fills.
- Do not merge V6 rows into `/api/memecoin/real/positions`.
- Do not add funded controls, funded mutation URLs, V5, replay, or strategy-arm comparisons.
- All fills remain labeled `modeled_live_quote`; net PnL remains unavailable while cost components are unknown.
- The only V6 operator mutation remains censoring an open paper position with an audit reason.
- Escape every server-derived string before inserting it into HTML.
- Do not run the full suite, call live APIs, write production databases, restart funded services, deploy, or push.
- Preserve unrelated Radar, Hyperliquid, and runtime-data work.

## File Map

- Modify `sniper/services/memecoin_v6_paper_report.py`: derive bounded cockpit facts and deterministic explanations from persisted rows.
- Modify `tests/test_memecoin_v6_paper_report.py`: prove return math, rule explanations, missing-data honesty, and urgency order inputs.
- Modify `tests/test_memecoin_v6_paper_api.py`: prove the V6 JSON endpoint exposes the cockpit contract without funded keys.
- Modify `static/app.js`: render health, compact rows, deterministic evidence, and lazy details.
- Modify `static/styles.css`: provide responsive semantic cockpit styling.
- Modify `tests/test_memecoin_v6_paper_ui.py`: lock the readable/censor-only UI contract.

---

### Task 1: Build the Read-Only Cockpit Presentation Model

**Files:**
- Modify: `tests/test_memecoin_v6_paper_report.py`
- Modify: `tests/test_memecoin_v6_paper_api.py`
- Modify: `sniper/services/memecoin_v6_paper_report.py`

**Interfaces:**
- Consumes: latest persisted observation, decision, rule results, engine state, entry price, inventory, and active intent.
- Produces: `_price_return(current, entry) -> float | None`, `_decision_context(...) -> dict`, and these new card keys: `latest_observed_at`, `modeled_price_return_frac`, `modeled_high_water_return_frac`, `inventory`, `market_facts`, `decision_context`.
- Exposes: those card keys unchanged through `GET /api/memecoin/v6-paper/positions`; `sniper/routes/api_memecoin.py` remains unchanged unless its V6 scrubber blocks a required field.

- [ ] **Step 1: Add failing price and inventory tests**

Add fixtures with a positive entry price, healthy executable price, high water, partial inventory,
missing executable price, zero price, and non-finite text. Assert:

```python
card = {c["entry_id"]: c for c in report.list_positions(conn)}["open1"]
assert card["modeled_price_return_frac"] == pytest.approx(0.8 / 1.0 - 1)
assert card["modeled_high_water_return_frac"] is not None
assert card["inventory"]["remaining_fraction"] == pytest.approx(
    card["remaining_qty_atomic"] / card["entry_qty_atomic"]
)
assert missing_card["modeled_price_return_frac"] is None
```

Use realistic matched entry/executable prices in the new fixture so the expected return is human-readable. Invalid, non-positive, missing, or non-finite prices must return `None`, never zero.

- [ ] **Step 2: Add failing deterministic explanation tests**

Drive or directly fixture each of `lp_loss`, `catastrophe`, `route_loss`, `time_expiry`, and
`staged_break` through `triggered`, `not_triggered`, and `insufficient` results. Assert:

```python
ctx = card["decision_context"]
assert ctx["action"] == "HOLD"
assert ctx["why"] == "Holding: no V6 exit rule is triggered; executable quote is healthy."
assert {item["rule_id"] for item in ctx["watching"]} == {
    "lp_loss", "catastrophe", "route_loss", "time_expiry", "staged_break"
}
assert ctx["missing_facts"] == sorted(set(ctx["missing_facts"]))
```

Add an unknown rule fixture and assert its stable id/status appear instead of being dropped. Add an active-intent fixture and assert `severity="urgent"` with an explicit pending/unfilled action explanation.

- [ ] **Step 3: Add the failing V6 endpoint contract**

Extend the populated-book API test:

```python
card = client.get("/api/memecoin/v6-paper/positions").get_json()["positions"][0]
assert set(("latest_observed_at", "modeled_price_return_frac", "inventory",
            "market_facts", "decision_context")) <= card.keys()
assert set(("action", "severity", "why", "watching", "missing_facts")) <= \
    card["decision_context"].keys()
assert all("rule_id" in item and "state" in item and "label" in item
           for item in card["decision_context"]["watching"])
```

Retain the recursive forbidden-key assertions and separate funded-route contract.

- [ ] **Step 4: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py -q
```

Expected: failures for missing cockpit fields and explanation helper behavior.

- [ ] **Step 5: Implement bounded helpers**

Add:

```python
RULE_ORDER = ("staged_break", "catastrophe", "route_loss", "time_expiry", "lp_loss")

def _positive_decimal(value): ...
def _price_return(current, entry): ...
def _market_facts(payload): ...
def _decision_context(*, decision, rules, payload, engine_state, active_intent,
                      price_return): ...
```

`_positive_decimal` accepts finite positive numbers only. `_market_facts` copies only the spec's latest-observation fields. `_decision_context` renders fixed labels from rule status, missing fields, reason codes, thresholds already defined by the engine (`-80%`, `4`, `60s`, `43,200s`), route-absence state, and break episode. It must not make a decision or reorder engine precedence.

- [ ] **Step 6: Expose the new card fields**

In `_card`, parse the latest payload defensively, call the helpers, and return:

```python
"latest_observed_at": obs["observed_at"] if obs else None,
"modeled_price_return_frac": _price_return(payload.get("executable_price"), entry["entry_price"]),
"modeled_high_water_return_frac": _price_return(
    payload.get("executable_high_water"), entry["entry_price"]),
"inventory": {"remaining_fraction": remaining / entry_qty if entry_qty else None},
"market_facts": _market_facts(payload),
"decision_context": decision_context,
```

Keep all existing keys for API compatibility.

- [ ] **Step 7: Run GREEN and commit**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py -q
git diff --check
git add sniper/services/memecoin_v6_paper_report.py \
  tests/test_memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_api.py
git commit -m "feat: explain v6 paper decisions"
```

### Task 2: Render the Decision Cockpit

**Files:**
- Modify: `tests/test_memecoin_v6_paper_ui.py`
- Modify: `static/app.js`
- Modify: `static/styles.css`

**Interfaces:**
- Consumes: V6 positions response, summary response, runtime health, and lazy position-detail response.
- Produces: `_v6Health(summary, runtime, rows)`, `_v6PaperRow(row)`, `_v6ToggleDetail(positionId, button)`, `_v6PaperDetail(detail)`, and `.v6-cockpit-*` semantic CSS.

- [ ] **Step 1: Add failing source-contract tests**

Assert the V6 block and stylesheet contain:

```python
for token in ("Why", "Watching next", "modeled_price_return_frac", "decision_context",
              "data-v6-detail", "v6-cockpit-row", "v6-action-chip", "v6-health-strip"):
    assert token in JS + CSS
assert "/api/memecoin/v6-paper/positions/" in block
assert "esc(" in block
```

Keep the existing no-funded-control test. Add an assertion that collapsed markup uses `ticker` before shortened `mint`, and that raw atomic quantity labels do not appear in the collapsed-row function.

- [ ] **Step 2: Run RED**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_ui.py -q
```

Expected: failures for missing cockpit markup, semantic CSS, and lazy detail loading.

- [ ] **Step 3: Implement formatting and health helpers**

Add V6-local helpers that never coalesce missing facts to zero:

```javascript
_v6Pct(value) { return value == null || !Number.isFinite(Number(value)) ? 'n/a' : `${Number(value) >= 0 ? '+' : ''}${(Number(value) * 100).toFixed(1)}%`; },
_v6Age(seconds) { /* compact d/h/m/s rendering, null -> n/a */ },
_v6Price(value) { /* 4 significant digits, null -> n/a */ },
_v6Health(summary, runtime, rows) { /* NO DATA / STALE / DEGRADED / HEALTHY from persisted states */ },
```

Use the fetched summary rather than discarding it. Preserve the existing latency facts without treating `>1s` as automatic failure.

- [ ] **Step 4: Replace cards with compact urgency-sorted rows**

Sort a copy of the response rows by action/degradation, absolute modeled return, and latest observation. Render ticker, chain, short mint, action chip, entry/current/return/high-water/drawdown/age/quote-age metrics, escaped `Why`, all escaped `Watching next` labels, source, and the paper-only honesty line. Show `Executable quote unavailable` for a null executable price.

- [ ] **Step 5: Add lazy evidence details**

Bind `data-v6-detail` buttons after each render. `_v6ToggleDetail` fetches only:

```javascript
`/api/memecoin/v6-paper/positions/${encodeURIComponent(positionId)}`
```

Render latest market facts, decision timeline, modeled fills, and a closed `Technical details` disclosure. Cache by `positionId + latest_observed_at`; show an inline error on fetch failure. Preserve expanded details across compact polls.

- [ ] **Step 6: Add semantic responsive styles**

Add `.v6-cockpit`, `.v6-health-strip`, `.v6-cockpit-row`, `.v6-position-metrics`, `.v6-decision`, `.v6-watch-list`, `.v6-action-chip`, and state modifier classes to `static/styles.css`. At `max-width: 767px`, stack identity, metrics, and decision; keep `Why` and `Watching next` wrap-safe. Use labels and borders as well as color.

- [ ] **Step 7: Run GREEN and static checks**

```bash
python3 -m pytest tests/test_memecoin_v6_paper_ui.py tests/test_memecoin_v6_paper_api.py -q
node --check static/app.js
git diff --check
```

- [ ] **Step 8: Commit**

```bash
git add static/app.js static/styles.css tests/test_memecoin_v6_paper_ui.py
git commit -m "feat: render v6 paper decision cockpit"
```

### Task 3: Focused Integration and Browser Verification

**Files:**
- Inspect: `docs/superpowers/specs/2026-07-26-memecoin-v6-paper-decision-cockpit-design.md`
- Inspect: `.github/pull_request_template.md`
- Inspect: `docs/superpowers/specs/2026-05-15-branch-close-knowledge-sync-design.md`
- Modify doctrine only if the implemented contract changes it.

**Interfaces:**
- Produces: verified local-main-ready cockpit and knowledge-sync evidence; no push, deploy, activation, production write, or funded restart.

- [ ] **Step 1: Run focused automated verification**

```bash
python3 -m pytest \
  tests/test_memecoin_v6_paper_report.py \
  tests/test_memecoin_v6_paper_api.py \
  tests/test_memecoin_v6_paper_ui.py \
  tests/test_memecoin_v6_runtime_telemetry.py \
  tests/test_memecoin_v6_engine.py -q
node --check static/app.js
python3 scripts/validate_schema_refs.py
git diff --check
```

- [ ] **Step 2: Verify the real local UI**

Use the Playwright skill against the authenticated local app. Open the Real Positions tab and confirm:

- four simultaneous positions are scannable without expansion;
- each row shows ticker, rounded return, action, `Why`, and every `Watching next` rule;
- one detail row expands and loads its timeline without affecting other rows;
- desktop and a narrow viewport require no horizontal scroll to read the decision;
- no funded control appears in the V6 section.

Do not restart the funded runner or mutate V6 data to manufacture a state.

- [ ] **Step 3: Complete branch-close checks**

Read the full change diff, confirm the implementation matches the approved spec, and run the Knowledge Sync Gate. Expected knowledge delta: the committed cockpit spec and implementation plan already document the operator-surface behavior; no Supabase write is authorized.

- [ ] **Step 4: Request code review and handle certain findings**

Use `superpowers:requesting-code-review` only if the user explicitly authorizes a Codex subagent. Otherwise perform the required inline diff review because repo instructions prohibit unrequested subagents. Fix Critical and Important issues with RED-first focused tests.

- [ ] **Step 5: Integrate locally without pushing**

The user has already directed that completed V6 work land on local `main` and remain unpushed. Merge the isolated feature branch only after focused verification passes; preserve all unrelated runtime files and concurrent Radar/HL changes. Re-run the focused UI/report tests on local `main` after merge.
