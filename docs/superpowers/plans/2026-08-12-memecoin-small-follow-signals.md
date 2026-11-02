# Small Follow Signal Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send armed-wallet follow buys of at least `$100` to the funded hook without changing discovery or market-cap limits.

**Architecture:** Add one follow-only environment setting. Keep the existing explicit `min_usd` parameter as the test and caller override.

**Tech Stack:** Python 3.13, pytest, SQLite, shell deployment script.

## Global Constraints

- Keep `MEMECOIN_DISCOVERY_MIN_USD=499`.
- Keep `MEMECOIN_REAL_MAX_MCAP_USD=1000000`.
- Keep all funded safety, route, stale, chase, exposure, and wallet-authority controls.
- Do not create a deployment backup.
- Do not change the database schema.

---

### Task 1: Add the follow-only source minimum

**Files:**
- Modify: `sniper/settings/memecoin.py`
- Modify: `sniper/services/memecoin_follow_activity.py`
- Modify: `tests/test_memecoin_follow_activity.py`
- Modify: `.env`

**Interfaces:**
- Consumes: `MEMECOIN_DISCOVERY_MIN_USD: float`.
- Produces: `MEMECOIN_FOLLOW_ACTIVITY_MIN_USD: float`.
- Preserves: `follow_activity_pass(..., min_usd=None, ...)`.

- [ ] **Step 1: Write the failing funded-hook test**

Add this test with the existing follow-activity fixtures:

```python
def test_default_follow_minimum_sends_one_hundred_dollar_buy_to_funded_hook(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", 100.0, raising=False)
    calls = []

    _run(
        _conn(),
        [_swap("MINT", 100, 20000, "2026-07-06T11:55:00Z", "small-100")],
        real_hook=lambda _conn, ev: calls.append(ev) or {"status": "filled"},
    )

    assert len(calls) == 1
```

- [ ] **Step 2: Run the test and confirm the expected failure**

Run:

```bash
python3 -m pytest tests/test_memecoin_follow_activity.py::test_default_follow_minimum_sends_one_hundred_dollar_buy_to_funded_hook -q
```

Expected: `len(calls)` is `0` because the follow path still uses the `$499` discovery minimum.

- [ ] **Step 3: Add the follow-only setting and use it**

Add the setting after `MEMECOIN_DISCOVERY_MIN_USD`:

```python
MEMECOIN_FOLLOW_ACTIVITY_MIN_USD = float(os.getenv(
    "MEMECOIN_FOLLOW_ACTIVITY_MIN_USD", str(MEMECOIN_DISCOVERY_MIN_USD)))
```

Import this setting in `follow_activity_pass()` and use it when `min_usd` is `None`.

- [ ] **Step 4: Run the failing test and confirm it passes**

Run the Step 2 command.

Expected: `1 passed`.

- [ ] **Step 5: Add boundary and override tests**

Add one test that rejects `$99.99` with the setting at `$100`.

Add one test that rejects `$100` when the explicit `min_usd` value is `$500`.

- [ ] **Step 6: Set the operator value**

Add this setting to `.env`:

```dotenv
MEMECOIN_FOLLOW_ACTIVITY_MIN_USD=100
```

Do not change `MEMECOIN_DISCOVERY_MIN_USD` or `MEMECOIN_REAL_MAX_MCAP_USD`.

- [ ] **Step 7: Run focused verification**

Run:

```bash
python3 -m pytest tests/test_memecoin_follow_activity.py tests/test_memecoin_follow_fast_lane.py tests/test_memecoin_real_mcap_gate.py -q
python3 scripts/validate_schema_refs.py
git diff --check
```

Expected: all tests pass, schema validation passes, and the diff check is empty.

- [ ] **Step 8: Commit the implementation**

Commit only the settings, follow service, test, plan, and environment change.

Use this commit message:

```text
fix(memecoin): accept smaller armed-wallet buys
```

### Task 2: Deploy and verify the pinned runtime

**Files:**
- Read: `scripts/deploy_memecoin_runtime.sh`
- Read: `data/memecoin_deploy.json`
- Read: `data/memecoin_runner.heartbeat`

**Interfaces:**
- Consumes: a full commit SHA reachable from `origin/main`.
- Produces: one funded runner from the exact content-addressed release.

- [ ] **Step 1: Integrate without a pull request**

Push the implementation commit to `origin/main` only under the operator's prior no-PR approval.

- [ ] **Step 2: Confirm zero pending reservations**

Use a read-only SQLite connection.

Require zero rows with `status='reserved'` in `memecoin_pending_entries`.

- [ ] **Step 3: Deploy without a backup**

Run:

```bash
scripts/deploy_memecoin_runtime.sh <full-sha> --restart --defer-backup
```

- [ ] **Step 4: Verify the active runtime**

Confirm these facts separately:

- The manifest SHA equals the release SHA.
- `.runtime/memecoin/current` resolves to that release.
- Exactly one runner uses that release path.
- The heartbeat is fresh.
- The authenticated health and real-status endpoints are healthy.
- The kill switch is clear.
- Pending reservations remain zero.
- The runner environment has `MEMECOIN_FOLLOW_ACTIVITY_MIN_USD=100`.
- The runner environment has `MEMECOIN_REAL_MAX_MCAP_USD=1000000`.

- [ ] **Step 5: Report the exact scope**

Report that the new follow threshold is live.

Report that the market-cap limit did not change.

Report that no backup was created.
