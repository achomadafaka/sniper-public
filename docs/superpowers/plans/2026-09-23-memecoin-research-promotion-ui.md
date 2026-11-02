# Memecoin Research promotion screen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this
> plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This repo forbids spawning
> subagents (`feedback-no-agents-no-worker-fleets`), so subagent-driven execution is NOT available
> here.

**Goal:** Replace the memecoin Research workspace's three gate-less paper subtabs with one
per-wallet promotion screen fed by `memecoin_promotion_lane`.

**Architecture:** A rollup function in `db_memecoin` aggregates the lane table per wallet and pools
its refusals by family. One read-only Flask endpoint serves that plus each wallet's funded record.
One `app.js` section renders a funnel table, a family panel and a caveat header. The three retired
sections and their renderers are deleted last, after the replacement renders, so Research is never
blank.

**Tech Stack:** Python 3.13, Flask blueprint (`sniper/routes/api_memecoin.py`), SQLite, vanilla JS
(`static/app.js`), pytest, Playwright for the runtime UI test.

**Spec:** `docs/superpowers/specs/2026-09-23-memecoin-research-promotion-ui-design.md`

## Global Constraints

- Table is `memecoin_promotion_lane`; its columns are fixed by `PROMOTION_LANE_COLUMNS` in
  `sniper/persistence/db_memecoin.py`. Run `python3 scripts/brain_index.py schema
  memecoin_promotion_lane` before naming a column.
- `verdict` is exactly one of `admitted`, `refused`, `unreadable`. `unreadable` is NEVER folded into
  `refused`, at any level of any aggregate.
- The denominator of every share is all evaluated rows. A share whose denominator depends on our own
  retention is a defect, not a rounding choice.
- A censored row is `exit_reason='open_at_horizon'`; it counts toward n and the censored count and
  never toward a closed statistic, and it carries NO `exit_price` by design.
- Median leads every outcome; the mean is shown beside it and never alone.
- No raw SQL outside `db.py` / `sniper/persistence/*` (CLAUDE.md §5).
- Reads use `get_readonly_connection()`; this screen writes nothing.
- Chains are never pooled. `resolve_chain` validates the `chain` argument, matching
  `/api/memecoin/traders/arm-matrix`.
- Tests run as `python3 -m pytest <files> -q -n 6` at most, gated on the exit code in the same
  command, never on a grep for " passed".
- Commit by explicit pathspec in ONE command. Never `git add -A`, never `git commit -a`.

---

### Task 1: Per-wallet rollup in the persistence layer

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (add beside `record_promotion_lane_row`)
- Test: `tests/test_memecoin_promotion_rollup.py` (create)

**Interfaces:**
- Consumes: `ensure_promotion_lane_table(conn)`, `memecoin_promotion_lane` columns.
- Produces: `promotion_lane_rollup(conn, *, chain=None, since=None) -> dict` with keys
  `wallets` (list of dicts), `families` (list of dicts), `lag` (dict), `window` (dict).
  Each wallet dict: `source_user_id`, `source_handle`, `network_id`, `seen`, `admitted`,
  `refused`, `unreadable`, `admit_pct`, `top_family`, `closed`, `censored`, `median_pnl`,
  `mean_pnl`, `sum_pnl`, `win_pct`.
  Each family dict: `family`, `n`, `share_pct`.
  `lag`: `p50_s`, `p90_s`, `n`.

- [ ] **Step 1: Write the failing test**

```python
"""The promotion rollup counts what the gates saw, not what we kept."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_promotion_lane_table(conn)
    return conn


def _row(**over):
    base = dict(attempt_id="a", network_id="solana", mint="M", source_user_id="u1",
                source_handle="alice", verdict="admitted",
                decision_started_at="2026-09-23T12:00:00+00:00",
                evaluated_at="2026-09-23T12:01:00+00:00", eval_lag_s=60.0,
                created_at="2026-09-23T12:01:00+00:00")
    base.update(over)
    return base


def test_unreadable_is_never_counted_as_refused():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", verdict="refused",
                                           refused_family="source_buy_floor"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", verdict="unreadable"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["seen"] == 2
    assert wallet["refused"] == 1
    assert wallet["unreadable"] == 1


def test_a_censored_row_counts_as_admitted_and_never_as_a_close():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", exit_reason="open_at_horizon"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", exit_reason="tp", pnl_usd=10.0))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["admitted"] == 2
    assert wallet["censored"] == 1
    assert wallet["closed"] == 1
    assert wallet["sum_pnl"] == pytest.approx(10.0)


def test_the_denominator_is_every_evaluated_row():
    conn = _conn()
    for i, verdict in enumerate(("admitted", "refused", "unreadable", "refused")):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, verdict=verdict,
                                               refused_family="source_buy_floor"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["seen"] == 4
    assert wallet["admit_pct"] == pytest.approx(25.0)


def test_the_median_leads_and_the_mean_is_beside_it():
    conn = _conn()
    for i, pnl in enumerate((-1.0, 2.0, 300.0)):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, exit_reason="tp",
                                               pnl_usd=pnl))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["median_pnl"] == pytest.approx(2.0)
    assert wallet["mean_pnl"] == pytest.approx(100.33333, rel=1e-4)


def test_the_family_panel_pools_refusals_across_wallets():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", source_user_id="u1",
                                           verdict="refused", refused_family="coin_band_unreservable"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", source_user_id="u2",
                                           verdict="refused", refused_family="coin_band_unreservable"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a3", source_user_id="u2",
                                           verdict="refused", refused_family="source_buy_floor"))
    families = {f["family"]: f for f in m.promotion_lane_rollup(conn)["families"]}
    assert families["coin_band_unreservable"]["n"] == 2
    assert families["source_buy_floor"]["n"] == 1


def test_the_lag_is_reported_so_no_gate_result_is_quoted_without_it():
    conn = _conn()
    for i, lag in enumerate((10.0, 60.0, 900.0)):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, eval_lag_s=lag))
    lag = m.promotion_lane_rollup(conn)["lag"]
    assert lag["n"] == 3
    assert lag["p50_s"] == pytest.approx(60.0)


def test_an_empty_table_reports_nothing_rather_than_zeroes_that_read_as_a_measurement():
    out = m.promotion_lane_rollup(_conn())
    assert out["wallets"] == [] and out["families"] == []
    assert out["lag"]["n"] == 0 and out["lag"]["p50_s"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_promotion_rollup.py -q`
Expected: FAIL, `AttributeError: module 'sniper.persistence.db_memecoin' has no attribute
'promotion_lane_rollup'`

- [ ] **Step 3: Write minimal implementation**

Add to `sniper/persistence/db_memecoin.py`, directly after `record_promotion_lane_row`:

```python
def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def promotion_lane_rollup(conn, *, chain=None, since=None):
    """Per-wallet promotion evidence plus the lane's own refusal funnel.

    THE DENOMINATOR IS EVERY EVALUATED ROW. A share computed over the rows we kept would describe
    our own behaviour rather than the wallet's, which is the failure this whole lane exists to
    remove. `unreadable` is its own bucket at every level: a market read we could not make is never
    the gates saying no. A censored row (`open_at_horizon`) counts as admitted and as censored, and
    never as a close -- it has no exit price by design.
    """
    import statistics

    where, args = ["1=1"], []
    if chain:
        where.append("network_id=?")
        args.append(str(chain))
    if since:
        where.append("decision_started_at>=?")
        args.append(str(since))
    clause = " AND ".join(where)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT source_user_id, source_handle, network_id, verdict, refused_family, "
            "       exit_reason, pnl_usd, eval_lag_s "
            f"  FROM memecoin_promotion_lane WHERE {clause}", tuple(args))]
    except Exception:
        logger.exception("promotion rollup: read failed")
        return {"wallets": [], "families": [], "lag": {"p50_s": None, "p90_s": None, "n": 0},
                "window": {"chain": chain, "since": since}}

    by_wallet, families = {}, {}
    for row in rows:
        key = row["source_user_id"]
        bucket = by_wallet.setdefault(key, {
            "source_user_id": key, "source_handle": row.get("source_handle"),
            "network_id": row.get("network_id"), "seen": 0, "admitted": 0, "refused": 0,
            "unreadable": 0, "censored": 0, "_closed_pnl": [], "_families": {}})
        bucket["seen"] += 1
        verdict = row.get("verdict")
        if verdict == "admitted":
            bucket["admitted"] += 1
            if row.get("exit_reason") == "open_at_horizon" or row.get("pnl_usd") is None:
                bucket["censored"] += 1
            else:
                bucket["_closed_pnl"].append(float(row["pnl_usd"]))
        elif verdict == "unreadable":
            bucket["unreadable"] += 1
        else:
            bucket["refused"] += 1
            family = row.get("refused_family") or "unnamed"
            bucket["_families"][family] = bucket["_families"].get(family, 0) + 1
            families[family] = families.get(family, 0) + 1

    wallets = []
    for bucket in by_wallet.values():
        closed = bucket.pop("_closed_pnl")
        own = bucket.pop("_families")
        bucket["closed"] = len(closed)
        bucket["median_pnl"] = statistics.median(closed) if closed else None
        bucket["mean_pnl"] = (sum(closed) / len(closed)) if closed else None
        bucket["sum_pnl"] = sum(closed) if closed else None
        bucket["win_pct"] = (100.0 * sum(1 for p in closed if p > 0) / len(closed)) if closed else None
        bucket["admit_pct"] = 100.0 * bucket["admitted"] / bucket["seen"] if bucket["seen"] else None
        bucket["top_family"] = max(own, key=own.get) if own else None
        wallets.append(bucket)
    wallets.sort(key=lambda w: (-(w["admitted"] or 0), -(w["seen"] or 0)))

    total_refused = sum(families.values())
    family_rows = [{"family": name, "n": n,
                    "share_pct": (100.0 * n / total_refused) if total_refused else None}
                   for name, n in sorted(families.items(), key=lambda kv: -kv[1])]
    lags = [float(r["eval_lag_s"]) for r in rows if r.get("eval_lag_s") is not None]
    return {"wallets": wallets, "families": family_rows,
            "lag": {"p50_s": _percentile(lags, 0.5), "p90_s": _percentile(lags, 0.9),
                    "n": len(lags)},
            "window": {"chain": chain, "since": since}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_promotion_rollup.py -q`
Expected: 7 passed, rc=0

- [ ] **Step 5: Run every test file importing the module, gated on the exit code**

Run: `python3 -m pytest $(grep -rl "db_memecoin" tests/ | tr '\n' ' ') -q -n 6`
Expected: rc=0. If any test fails, fix before committing.

- [ ] **Step 6: Commit**

```bash
git add tests/test_memecoin_promotion_rollup.py
git commit -F- -- sniper/persistence/db_memecoin.py tests/test_memecoin_promotion_rollup.py <<'EOF'
Roll the promotion lane up per wallet without letting our own retention set the denominator

The rollup counts every evaluated row, so a wallet's admitted share describes the gates rather than
what we chose to keep. `unreadable` stays its own bucket at every level because a market read we
could not make is not the gates saying no, and a censored row counts as admitted and censored but
never as a close -- it carries no exit price by design. The median leads each wallet's outcome and
the mean sits beside it, never alone.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 2: The read-only endpoint

**Files:**
- Modify: `sniper/routes/api_memecoin.py` (add beside `traders_arm_matrix`, near line 3804)
- Test: `tests/test_memecoin_promotion_endpoint.py` (create)

**Interfaces:**
- Consumes: `m.promotion_lane_rollup(conn, chain=..., since=...)` from Task 1.
- Produces: `GET /api/memecoin/promotion?chain=<chain>&days=<float>` returning
  `{"wallets": [...], "families": [...], "lag": {...}, "window": {...}, "funded": {uid: {...}}}`.
  The `funded` map carries `{"closed": int, "pnl": float}` per `source_user_id`.

- [ ] **Step 1: Write the failing test**

```python
"""The promotion endpoint serves the rollup and the funded record beside it."""
import sqlite3

import pytest

flask = pytest.importorskip("flask")

from sniper.persistence import db_memecoin as m


@pytest.fixture
def client(monkeypatch):
    from sniper.routes import api_memecoin

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_promotion_lane_table(conn)
    conn.execute("CREATE TABLE memecoin_positions (source_user_id TEXT, execution_mode TEXT, "
                 "realized_pnl REAL, closed_at TEXT)")
    conn.execute("INSERT INTO memecoin_positions VALUES ('u1','real',-223.0,'2026-09-22T00:00:00+00:00')")
    conn.commit()
    m.record_promotion_lane_row(conn, dict(
        attempt_id="a1", network_id="solana", mint="M", source_user_id="u1",
        source_handle="alice", verdict="admitted", exit_reason="tp", pnl_usd=4.0,
        decision_started_at="2026-09-23T12:00:00+00:00",
        evaluated_at="2026-09-23T12:01:00+00:00", eval_lag_s=60.0,
        created_at="2026-09-23T12:01:00+00:00"))
    app = flask.Flask(__name__)
    app.register_blueprint(api_memecoin.build_blueprint(conn))
    return app.test_client()


def test_the_endpoint_returns_the_rollup_and_the_funded_record(client):
    body = client.get("/api/memecoin/promotion?chain=solana").get_json()
    assert body["wallets"][0]["source_handle"] == "alice"
    assert body["wallets"][0]["admitted"] == 1
    assert body["funded"]["u1"]["closed"] == 1
    assert body["funded"]["u1"]["pnl"] == pytest.approx(-223.0)


def test_a_chain_is_required_because_chains_are_never_pooled(client):
    assert client.get("/api/memecoin/promotion").status_code == 400
```

⚠️ **Before writing this test, confirm how the blueprint is constructed.** Run
`grep -nE "def build_blueprint|^def |bp = Blueprint" sniper/routes/api_memecoin.py | head` and use
the real factory name and signature. If the blueprint is not built by a function taking `conn`,
adapt the fixture to the real pattern rather than inventing one.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_promotion_endpoint.py -q`
Expected: FAIL with 404 on the route.

- [ ] **Step 3: Write minimal implementation**

Add beside `traders_arm_matrix` in `sniper/routes/api_memecoin.py`:

```python
    @bp.get("/api/memecoin/promotion")
    def promotion_lane():
        # Lane A evidence per wallet: of the signals this wallet sent, how many the funded gates
        # would have admitted, why the rest were refused, and what the admitted ones did. The
        # wallet's REAL record rides alongside because the two disagree and the operator has to see
        # both: paper measures coin selection, funded measures money.
        from sniper.services.memecoin_chains import resolve_chain
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required",
                            "detail": "chains are never pooled; name one"}), 400
        try:
            days = float(request.args.get("days") or 7.0)
        except (TypeError, ValueError):
            days = 7.0
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        out = m.promotion_lane_rollup(conn, chain=chain, since=since)
        uids = [w["source_user_id"] for w in out["wallets"] if w.get("source_user_id")]
        funded = {}
        if uids:
            marks = ",".join("?" * len(uids))
            for row in conn.execute(
                    "SELECT source_user_id, COUNT(*) n, SUM(realized_pnl) pnl "
                    "  FROM memecoin_positions "
                    f" WHERE execution_mode='real' AND realized_pnl IS NOT NULL "
                    f"   AND source_user_id IN ({marks}) GROUP BY source_user_id", tuple(uids)):
                funded[row["source_user_id"]] = {"closed": int(row["n"] or 0),
                                                 "pnl": float(row["pnl"] or 0.0)}
        out["funded"] = funded
        return jsonify(out)
```

If `datetime`, `timezone` or `timedelta` are not already imported in that module, add them to the
existing `import` block at the top rather than importing inside the function.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_promotion_endpoint.py -q`
Expected: 2 passed, rc=0

- [ ] **Step 5: Run the route module's test files**

Run: `python3 -m pytest $(grep -rl "api_memecoin" tests/ | tr '\n' ' ') -q -n 6`
Expected: rc=0

- [ ] **Step 6: Commit**

```bash
git add tests/test_memecoin_promotion_endpoint.py
git commit -F- -- sniper/routes/api_memecoin.py tests/test_memecoin_promotion_endpoint.py <<'EOF'
Serve the promotion rollup with each wallet's funded record beside it

Paper measures coin selection and the funded ledger measures money, and on the disarmed cohort the
two point opposite ways -- +$1,901 of paper against -$2,508 of real. Putting them in one response
means the screen cannot show one without the other. Chains are never pooled, so the endpoint
refuses a request that does not name one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 3: The promotion section in the board

**Files:**
- Modify: `static/app.js` — subtab nav near line 3242, section markup near line 3283
- Test: `tests/test_memecoin_promotion_ui.py` (create)

**Interfaces:**
- Consumes: `GET /api/memecoin/promotion` from Task 2.
- Produces: a `mc-section` with `data-section="promotion"`, a subtab button
  `data-workspace="research" data-subtab="promotion"`, and a renderer writing into
  `#mc-promotion`, `#mc-promotion-families` and `#mc-promotion-caveats`.

- [ ] **Step 1: Write the failing test**

```python
"""The promotion screen states its limits and separates unreadable from refused."""
from pathlib import Path

APP_JS = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_the_research_workspace_offers_the_promotion_subtab():
    assert 'data-workspace="research" data-subtab="promotion"' in APP_JS
    assert 'data-section="promotion"' in APP_JS


def test_the_caveats_are_in_the_header_not_left_to_the_reader():
    for phrase in ("looser than funded", "ceiling", "under-trigger"):
        assert phrase in APP_JS, phrase


def test_unreadable_is_a_column_of_its_own():
    assert "mc-promotion" in APP_JS
    assert "unreadable" in APP_JS


def test_the_empty_state_says_what_would_fill_it():
    assert "No evaluated signals yet" in APP_JS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_promotion_ui.py -q`
Expected: FAIL on the first assertion.

- [ ] **Step 3: Add the subtab button**

In the `mc-subtab` nav (near line 3242), REPLACE these three lines:

```html
            <button class="mc-subtab" data-workspace="research" data-subtab="closed">Paper results</button>
            <button class="mc-subtab" data-workspace="research" data-subtab="positions">Paper positions</button>
            <button class="mc-subtab" data-workspace="research" data-subtab="performance">Traders (paper)</button>
```

with:

```html
            <button class="mc-subtab" data-workspace="research" data-subtab="promotion">Promotion</button>
```

- [ ] **Step 4: Add the section markup**

Add a new `mc-section` beside the others (the retired ones are removed in Task 4):

```html
          <div class="mc-section" data-section="promotion">
          <div class="card" style="padding:12px 16px;margin-bottom:14px">
            <div style="font-weight:600;font-size:14px;margin-bottom:2px">Promotion <span class="text-muted" style="font-size:11px;font-weight:400">— of the signals this trader sent, how many the funded gates would have taken</span></div>
            <div id="mc-promotion-caveats" class="text-muted" style="font-size:11px;margin-bottom:8px"></div>
            <div id="mc-promotion" class="table-wrapper"></div>
          </div>
          <div class="card" style="padding:12px 16px;margin-bottom:14px">
            <div style="font-weight:600;font-size:14px;margin-bottom:8px">Why the lane refuses <span class="text-muted" style="font-size:11px;font-weight:400">— every trader pooled, so a gate that sits inert is visible</span></div>
            <div id="mc-promotion-families" class="table-wrapper"></div>
          </div></div>
```

- [ ] **Step 5: Add the renderer**

Add a method beside the other `mc-` renderers (the file's existing style: `async _renderX()` using
`fetchJson`, `esc`, `this._money`, `this._col`):

```javascript
    async _renderPromotion() {
      const el = document.getElementById('mc-promotion');
      if (!el) return;
      const chain = this._chainFilter;
      let d;
      try { d = await fetchJson('/api/memecoin/promotion?chain=' + encodeURIComponent(chain),
        { suppressErrorToast: true }); }
      catch (e) { el.innerHTML = `<div class="text-muted">Promotion evidence unavailable: ${esc(String(e && e.message || e))}</div>`; return; }
      const lag = d.lag || {}, wallets = d.wallets || [], funded = d.funded || {};
      const cav = document.getElementById('mc-promotion-caveats');
      if (cav) {
        cav.innerHTML = `Admission here is <b>looser than funded</b> — the chase gates cannot be `
          + `replayed after the fact. Paper pays no impact and no route failure, so every number is `
          + `a <b>ceiling</b>. Exits walk a one-minute series against a rail that sweeps in seconds, `
          + `so the stop and the take-profit both <b>under-trigger</b>. `
          + (lag.n ? `Evaluation lag p50 ${Math.round(lag.p50_s)}s · p90 ${Math.round(lag.p90_s)}s over ${lag.n} rows.`
                   : 'No evaluated signals yet.');
      }
      if (!wallets.length) {
        el.innerHTML = `<div class="mc-empty">No evaluated signals yet on ${esc(chain)}. `
          + `This fills when the promotion lane runs: one row per trader J7 relays for a wallet `
          + `that is not armed, with the gates replayed a minute later.</div>`;
      } else {
        el.innerHTML = `<table class="table"><thead><tr>`
          + `<th>Trader</th><th>Signals</th><th>Would take</th><th>Top refusal</th>`
          + `<th>Unreadable</th><th>Closed</th><th>Median</th><th>Mean</th><th>Sum</th>`
          + `<th>Win%</th><th>Censored</th><th>Funded (real)</th></tr></thead><tbody>`
          + wallets.map(w => {
            const f = funded[w.source_user_id];
            const money = v => v == null ? '<span class="text-muted">—</span>'
              : `<span style="color:${this._col(v)}">${this._money(v)}</span>`;
            return `<tr><td>${esc(w.source_handle || w.source_user_id || '')}</td>`
              + `<td>${w.seen}</td>`
              + `<td><b>${w.admitted}</b> <span class="text-muted">${w.admit_pct == null ? '' : w.admit_pct.toFixed(0) + '%'}</span></td>`
              + `<td class="text-muted">${esc(w.top_family || '—')}</td>`
              + `<td>${w.unreadable || 0}</td>`
              + `<td>${w.closed}</td><td>${money(w.median_pnl)}</td><td>${money(w.mean_pnl)}</td>`
              + `<td>${money(w.sum_pnl)}</td>`
              + `<td>${w.win_pct == null ? '—' : w.win_pct.toFixed(0) + '%'}</td>`
              + `<td>${w.censored || 0}</td>`
              + `<td>${f ? `${money(f.pnl)} <span class="text-muted">/${f.closed}c</span>` : '<span class="text-muted">never funded</span>'}</td></tr>`;
          }).join('') + `</tbody></table>`;
      }
      const fam = document.getElementById('mc-promotion-families');
      if (fam) {
        const rows = d.families || [];
        fam.innerHTML = rows.length
          ? `<table class="table"><thead><tr><th>Gate family</th><th>Refusals</th><th>Share</th></tr></thead><tbody>`
            + rows.map(r => `<tr><td>${esc(r.family)}</td><td>${r.n}</td>`
              + `<td>${r.share_pct == null ? '—' : r.share_pct.toFixed(0) + '%'}</td></tr>`).join('')
            + `</tbody></table>`
          : `<div class="mc-empty">No refusals recorded yet.</div>`;
      }
    }
```

Wire it into the subtab dispatcher exactly as the retired `performance` subtab was wired: find the
`switch`/map that calls `_renderTraders` for `data-subtab="performance"` and add a `promotion` case
calling `_renderPromotion`. Run `grep -n "_renderTraders" static/app.js` to find every call site.

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_promotion_ui.py -q`
Expected: 4 passed, rc=0

- [ ] **Step 7: Commit**

```bash
git add tests/test_memecoin_promotion_ui.py
git commit -F- -- static/app.js tests/test_memecoin_promotion_ui.py <<'EOF'
Add a Promotion screen that reads as a funnel instead of a scoreboard

One row per trader, left to right: signals seen, how many the funded gates would have taken, the
family that refused the rest, unreadable kept separate, then the outcome on the admitted ones with
the median first and the real funded record beside it. A second panel pools the refusals so a gate
that sits inert is visible as one.

The header states the limits once rather than leaving them to the reader: admission is looser than
funded because the chase gates cannot be replayed, every number is a ceiling because paper pays no
impact, and the one-minute exit series under-triggers both bounds.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

### Task 4: Retire the three gate-less subtabs

**Files:**
- Modify: `static/app.js` — remove sections `closed`, `positions`, `performance` and their renderers
- Test: `tests/test_memecoin_promotion_ui.py` (extend)

**Interfaces:**
- Consumes: Task 3's promotion section must already render.
- Produces: no new interface; removes `_renderTraders`, the `mc-stats` / `mc-closed` /
  `mc-open` / `mc-paper-summary` / `mc-chart` / `mc-traders` renderers and their call sites.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_memecoin_promotion_ui.py`:

```python
def test_the_gate_less_paper_subtabs_are_gone():
    """They answered 'were its coins good' while reading as 'would we have made this'."""
    for gone in ('data-subtab="closed"', 'data-subtab="positions"', 'data-subtab="performance"'):
        assert gone not in APP_JS, gone


def test_retiring_them_did_not_take_the_arm_control_with_it():
    """The retired matrix carried a live per-chain arm control; Funded authority carries it too,
    and a UI cleanup must never remove a way to arm a wallet as a side effect."""
    assert 'data-action="chainarm"' in APP_JS
    assert 'data-action="wexempt"' in APP_JS
    assert 'data-section="armed"' in APP_JS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_promotion_ui.py -q`
Expected: FAIL on `data-subtab="closed"` still being present.

- [ ] **Step 3: Delete the three sections**

Remove the `mc-section` blocks for `data-section="closed"`, `data-section="positions"` and
`data-section="performance"` from the board markup (near lines 3283-3303). Leave `real`, `armed`,
`theses`, `activity`, `decisions` and the new `promotion` untouched.

- [ ] **Step 4: Delete the renderers and their call sites**

Run `grep -n "mc-traders\|mc-stats\|mc-closed\|mc-open'\|mc-paper-summary" static/app.js` and remove
each renderer whose only consumer was a deleted section, plus its dispatcher entry. Do NOT remove a
function another workspace still calls — check each with `grep -n "<name>" static/app.js` first.

- [ ] **Step 5: Run the UI tests and the whole memecoin UI suite**

Run: `python3 -m pytest $(grep -rl "app.js" tests/ | tr '\n' ' ') -q -n 6`
Expected: rc=0. A failure here means a removed renderer still had a consumer — restore it rather
than deleting the test.

- [ ] **Step 6: Commit**

```bash
git commit -F- -- static/app.js tests/test_memecoin_promotion_ui.py <<'EOF'
Retire the three gate-less paper subtabs from Research

They read `memecoin_positions` rows written by lanes that opened a position for every signal
regardless of the gates: 2,242 of 2,346 in the week to 2026-09-23 came from attempts the funded path
had REFUSED. Every per-wallet cell therefore answered "were this trader's coins good" while it was
read as "would we have made this money", and on the disarmed cohort those two disagreed by the whole
result.

The rows keep being written and `scripts/paper_promotion_read.py` keeps consuming them for the
2026-08-28 prereg; only the screens go. Verified first, and pinned by a test: the retired matrix
carried a live per-chain arm and gate-exemption control, and Funded authority carries the identical
ones, so nothing here removes a way to arm a wallet.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
```

---

## Self-review

**Spec coverage.** Columns 1-7 → Task 1 rollup + Task 3 renderer. Column 8 (promotion state) is
spec'd as inert until Gamma's rule lands and is deliberately absent from the table rather than
rendered as a placeholder verdict; the spec's "Open" section says so. Header caveats → Task 3 Step
5. Family funnel → Tasks 1 and 3. Endpoint → Task 2. Aggregation rules → Task 1 tests. Deletions and
the arm-control dependency → Task 4. Empty state → Task 3.

**Placeholders.** None: every code step carries the code, every test step carries the test, and the
two places where the plan cannot know the repo's shape (the blueprint factory in Task 2, the
dispatcher wiring in Task 3) name the exact `grep` to run instead of guessing.

**Type consistency.** `promotion_lane_rollup` returns `wallets`/`families`/`lag`/`window` in Task 1;
Task 2 adds `funded` and Task 3 reads exactly those keys. Wallet keys used in the renderer
(`seen`, `admitted`, `admit_pct`, `top_family`, `unreadable`, `closed`, `median_pnl`, `mean_pnl`,
`sum_pnl`, `win_pct`, `censored`, `source_handle`, `source_user_id`) all exist in Task 1's output.
