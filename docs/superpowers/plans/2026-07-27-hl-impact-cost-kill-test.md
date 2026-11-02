# HL Impact-Cost Kill Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reprice the frozen 24h funding-filtered HL run-up short with a pre-registered liquidity-tier `impactPxs` cost and decide whether realistic depth kills the discovery candidate.

**Architecture:** Add one research module with pure cost-mapping functions and a cache-only CLI. It reads the historical candle/funding cache plus the squeeze tape through SQLite read-only mode, rebuilds the already-frozen event set, and writes one deterministic JSON report. Existing study, recorder, arm, paper, and funded code remain unchanged.

**Tech Stack:** Python 3 standard library, SQLite, pytest, existing `hl_squeeze_study`, `hl_squeeze_residual`, and `hl_funding_study` modules.

## Global Constraints

- Execute `data/research/hl_squeeze_study/PREREG.impact.v1.md` exactly.
- Tape cutoff is `1785154010747`; rows after it are invisible.
- `impact_roundtrip_bps = (impact_ask - impact_bid) / mid_px * 10,000`; never double it.
- Replace only the old 30 bps slippage term; retain the existing 9 bps fees.
- Selected slice is 24h HOLDOUT with `funding_self_pct <= 0.94993`.
- Coin-block bootstrap stays at 5,000 replications and seed 20260727.
- No network, recorder, service, paper, funded, or production DB writes.

---

### Task 1: Pure impact-cost mapping

**Files:**
- Create: `scripts/research/hl_impact_cost_study.py`
- Create: `tests/test_hl_impact_cost_study.py`

**Interfaces:**
- Consumes: tape rows shaped as `coin, ts_ms, mid_px, impact_bid, impact_ask, day_ntl_vlm`; historical series from `hl_squeeze_study._load`.
- Produces: `valid_spread(row) -> float | None`, `build_liquidity_tiers(rows) -> list[dict]`, `event_quote_volume(series, entry_ts) -> float | None`, `assign_tier(volume, tiers) -> dict | None`, and `recharge_event(event, impact_bps) -> dict`.

- [ ] **Step 1: Write failing literal tests**

```python
def test_bid_to_ask_is_one_roundtrip_and_is_not_doubled():
    row = {"mid_px": 100.0, "impact_bid": 99.9, "impact_ask": 100.2}
    assert study.valid_spread(row) == pytest.approx(30.0)

def test_recharge_replaces_thirty_bps_but_keeps_fees_and_every_other_term():
    event = {"residual_full_bps": 190.0}
    assert study.recharge_event(event, 70.0)["impact_recharged_residual_bps"] == 150.0

def test_event_volume_uses_only_twenty_four_completed_pre_entry_bars():
    series = {"t": list(range(25)), "c": [2.0] * 25, "v": [3.0] * 25}
    assert study.event_quote_volume(series, 24) == 144.0
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_hl_impact_cost_study.py -q`

Expected: collection fails because `scripts.research.hl_impact_cost_study` does not exist.

- [ ] **Step 3: Implement the minimum pure functions**

```python
def valid_spread(row):
    mid, bid, ask = row["mid_px"], row["impact_bid"], row["impact_ask"]
    if not all(math.isfinite(float(x)) for x in (mid, bid, ask)):
        return None
    if mid <= 0 or bid <= 0 or ask < bid:
        return None
    return (ask - bid) / mid * 1e4

def recharge_event(event, impact_bps):
    return {**event, "assigned_impact_bps": impact_bps,
            "impact_recharged_residual_bps": event["residual_full_bps"] + 30.0 - impact_bps}
```

`build_liquidity_tiers` must aggregate per coin first, use equal-count quintiles ordered by median volume, and expose coin count, volume minimum/maximum, plus median/p75/p90 of per-coin median spreads. `assign_tier` clamps outside values to tiers 1 and 5. `event_quote_volume` requires 24 completed bars and sums `volume * close` from `entry_index - 24` through `entry_index - 1`.

- [ ] **Step 4: Run GREEN**

Run: `python3 -m pytest tests/test_hl_impact_cost_study.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/research/hl_impact_cost_study.py tests/test_hl_impact_cost_study.py
git commit -m "research(hl): add frozen impact-cost mapper"
```

### Task 2: Cache-only report and decision

**Files:**
- Modify: `scripts/research/hl_impact_cost_study.py`
- Modify: `tests/test_hl_impact_cost_study.py`
- Create: `data/research/hl_squeeze_study/impact_cost.v1.json`
- Modify: `data/research/hl_squeeze_study/MEMO.v1.md`

**Interfaces:**
- Consumes: `load_tape_rows(db_path, cutoff_ms)`, the fixed historical cache path, split file, and funding cache.
- Produces: `run_study(cache_dir, tape_db, cutoff_ms) -> dict` and CLI options `--cache-dir`, `--tape-db`, `--cutoff-ms`, `--output`.

- [ ] **Step 1: Write failing integration tests**

```python
def test_tape_reader_is_cutoff_bounded_and_read_only(tmp_path):
    # Create a tiny real SQLite tape with one row before and one after the cutoff.
    rows = study.load_tape_rows(tmp_path / "tape.db", cutoff_ms=100)
    assert [row["ts_ms"] for row in rows] == [100]

def test_decision_fails_closed_on_coverage_or_small_tier():
    assert study.decision({"coverage": 0.94, "min_tier_coins": 20, "p75_ci_low": 1})["pass"] is False
    assert study.decision({"coverage": 1.0, "min_tier_coins": 19, "p75_ci_low": 1})["pass"] is False
    assert study.decision({"coverage": 1.0, "min_tier_coins": 20, "p75_ci_low": 0})["pass"] is False
```

- [ ] **Step 2: Run RED**

Run: `python3 -m pytest tests/test_hl_impact_cost_study.py -q`

Expected: fails because the reader, study runner, and decision function are absent.

- [ ] **Step 3: Implement the report path**

Open SQLite with `file:<absolute-path>?mode=ro`, filter at the SQL boundary with `ts_ms <= ?`, rebuild the frozen selected HOLDOUT events, charge median/p75/p90 tiers, and summarize each with the existing coin-block bootstrap. The JSON must include source commit, prereg commit, cutoff, source row counts, tape time span, tiers, event coverage, original result, all three recharged results, same-coin diagnostic, decision reasons, and SHA-256 hashes for the manifest, split, funding result, funding signal result, and prereg.

- [ ] **Step 4: Run focused verification and generate the sealed result**

Run:

```bash
python3 -m pytest tests/test_hl_impact_cost_study.py tests/test_hl_squeeze_tape.py -q
python3 scripts/research/hl_impact_cost_study.py \
  --cache-dir /home/bigpapi/Desktop/Sniper/data/research/hl_full_history \
  --tape-db /home/bigpapi/Desktop/Sniper/data/hl_squeeze_tape.db \
  --cutoff-ms 1785154010747 \
  --output data/research/hl_squeeze_study/impact_cost.v1.json
python3 scripts/validate_schema_refs.py
git diff --check
```

Expected: focused tests and schema check pass; output is deterministic on a second run; no file outside the isolated worktree changes.

- [ ] **Step 5: Record the decision without promotion language**

Append the measured result and limitations to `MEMO.v1.md`. If p75 fails, mark the candidate killed. If p75 passes, state only that the historical candidate survived this bounded current-depth proxy and may proceed to a forward prereg; do not create that prereg in this task.

- [ ] **Step 6: Commit**

```bash
git add scripts/research/hl_impact_cost_study.py tests/test_hl_impact_cost_study.py \
  data/research/hl_squeeze_study/impact_cost.v1.json data/research/hl_squeeze_study/MEMO.v1.md
git commit -m "research(hl): run impact-cost kill test"
```
