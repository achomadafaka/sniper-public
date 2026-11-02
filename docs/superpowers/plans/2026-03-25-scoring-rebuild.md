# Wallet Scoring Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a wallet_quality_score (P&L-first) for wallet selection, keeping position_insider_score untouched for entry timing, behind hard filters (Gate 0 + market maker + evidence floor).

**Architecture:** Add new scoring function `_compute_wallet_quality_score()` in analyze_users.py that runs AFTER the existing `_score_insider()`. Store result in new `wallet_quality_score` column in user_analysis. Paper trader entry gating uses wallet_quality_score for wallet selection, position_insider_score for timing. Three hard filters evaluated before scoring.

**Critical rollout rule:** do **not** encode hard-filter failures as just `wallet_quality_score = 0`. The pipeline must distinguish:
- `NULL` = not scored yet / rollout grace
- numeric score = scored wallet
- explicit hard-filter failure = ineligible regardless of score

Store explicit eligibility/fail state in `user_analysis` and update `wallet_registry.gate0_status` when Gate 0 fails or passes.

**Tech Stack:** Python, SQLite, existing analyze_users.py pipeline.

---

## File Map

| File | Role | Changes |
|------|------|---------|
| `analyze_users.py` | Scoring engine | Add `_compute_wallet_quality_score()`, add market maker detection, add evidence floor check, wire into `run_once()` |
| `db.py` | Schema | Add `wallet_quality_score`, `market_maker_detected`, and explicit wallet-quality eligibility/fail-state columns to `user_analysis`; wire Gate 0 status back to `wallet_registry` |
| `paper_trader.py` | Entry gating | Add wallet_quality_score check alongside position_insider_score |
| `paper_settings.py` | Config | Add `PAPER_MIN_WALLET_QUALITY_SCORE` threshold |
| `web_settings.py` | Config | Add wallet quality thresholds as env vars |
| `strategies/insider_copy_v1.py` | Strategy entry | Add wallet_quality_score awareness to entry decisions |
| `routes/api_core.py` | API | Include wallet_quality_score in leaderboard/wallet endpoints |
| `tests/test_wallet_quality_score.py` | Tests | New test file for the scoring function |
| Shared helper module (preferred) | Market maker detection | Reuse existing logic, but do **not** import directly from a `scripts/` entrypoint if it is not library-safe |

---

### Task 1: Add wallet quality schema columns

**Files:**
- Modify: `db.py` (init_db function or ensure_columns pattern)
- Test: `tests/test_wallet_quality_score.py` (new file)

- [ ] **Step 1: Write tests for schema existence using a temp DB + `init_db()`**

```python
# tests/test_wallet_quality_score.py
import sqlite3
import pytest

def _make_db():
    from db import get_connection, init_db
    conn = get_connection(":memory:")
    init_db(conn)
    return conn

class TestWalletQualityScoreSchema:
    def test_user_analysis_has_wallet_quality_score_column(self):
        """wallet_quality_score column must exist in user_analysis."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "wallet_quality_score" in cols

    def test_user_analysis_has_market_maker_detected_column(self):
        """market_maker_detected column must exist in user_analysis."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "market_maker_detected" in cols

    def test_user_analysis_has_wallet_quality_eligibility_state(self):
        """Rollout must distinguish NULL/unscored from explicit hard-filter failure."""
        conn = _make_db()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_analysis)").fetchall()]
        assert "wallet_quality_eligible" in cols
        assert "wallet_quality_fail_reason" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_wallet_quality_score.py -v`
Expected: FAIL — columns don't exist yet

- [ ] **Step 3: Add columns via ensure_columns pattern**

In `db.py`, find the existing `ensure_paper_trades_slippage_columns` pattern and add:

```python
def ensure_wallet_quality_columns(conn: sqlite3.Connection) -> None:
    """Ensure wallet quality scoring columns exist."""
    with with_db_init_lock(conn):
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_analysis' LIMIT 1"
        ).fetchone()
        if table is None:
            return
        _ensure_column(conn, "user_analysis", "wallet_quality_score", "REAL DEFAULT NULL")
        _ensure_column(conn, "user_analysis", "market_maker_detected", "INTEGER DEFAULT 0")
        _ensure_column(conn, "user_analysis", "wallet_quality_eligible", "INTEGER DEFAULT NULL")
        _ensure_column(conn, "user_analysis", "wallet_quality_fail_reason", "TEXT")
        conn.commit()
```

Call this from `init_db()` after existing ensure calls.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_wallet_quality_score.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_wallet_quality_score.py
git commit -m "schema: add wallet quality scoring columns"
```

---

### Task 2: Implement hard filters and explicit eligibility state

**Files:**
- Modify: `analyze_users.py`
- Test: `tests/test_wallet_quality_score.py`

- [ ] **Step 1: Write tests for hard filters**

```python
class TestHardFilters:
    def test_gate0_fails_negative_lifetime_pnl(self):
        """Wallets with lb_pnl < -10000 should fail Gate 0."""
        from analyze_users import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=-50000, closed_sample_size=100,
            market_maker_detected=False
        )
        assert result["passes"] is False
        assert "gate0" in result["fail_reason"]

    def test_market_maker_override_disqualifies(self):
        """Wallets detected as market makers should be disqualified."""
        from analyze_users import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=100000, closed_sample_size=100,
            market_maker_detected=True
        )
        assert result["passes"] is False
        assert "market_maker" in result["fail_reason"]

    def test_minimum_evidence_floor(self):
        """Wallets with too few closed trades should not be scored."""
        from analyze_users import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=50000, closed_sample_size=3,
            market_maker_detected=False
        )
        assert result["passes"] is False
        assert "evidence" in result["fail_reason"]

    def test_passing_wallet(self):
        """Wallet with good P&L, enough trades, not MM should pass."""
        from analyze_users import _wallet_passes_hard_filters
        result = _wallet_passes_hard_filters(
            lb_pnl=50000, closed_sample_size=50,
            market_maker_detected=False
        )
        assert result["passes"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_wallet_quality_score.py::TestHardFilters -v`
Expected: FAIL — function doesn't exist

- [ ] **Step 3: Implement _wallet_passes_hard_filters**

In `analyze_users.py`, add before `_score_insider`:

```python
# ── Wallet quality hard filters ────────────────────────────────────────
GATE0_MIN_LIFETIME_PNL = float(os.getenv("GATE0_MIN_LIFETIME_PNL", "-10000"))
WALLET_MIN_CLOSED_SAMPLE = int(os.getenv("WALLET_MIN_CLOSED_SAMPLE", "10"))

def _wallet_passes_hard_filters(
    *,
    lb_pnl: float | None,
    closed_sample_size: int,
    market_maker_detected: bool,
) -> Dict[str, Any]:
    """Evaluate hard pass/fail filters before wallet quality scoring."""
    # Gate 0: lifetime P&L floor
    if lb_pnl is not None and lb_pnl < GATE0_MIN_LIFETIME_PNL:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": f"gate0: lb_pnl=${lb_pnl:.0f} < ${GATE0_MIN_LIFETIME_PNL:.0f}",
            "gate0_status": "failed",
        }

    # Market maker override
    if market_maker_detected:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": "market_maker: both-sides detected",
            "gate0_status": "passed",
        }

    # Minimum evidence floor
    if closed_sample_size < WALLET_MIN_CLOSED_SAMPLE:
        return {
            "passes": False,
            "eligible": 0,
            "fail_reason": f"evidence: {closed_sample_size} closed < {WALLET_MIN_CLOSED_SAMPLE} minimum",
            "gate0_status": "passed",
        }

    return {"passes": True, "eligible": 1, "fail_reason": None, "gate0_status": "passed"}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_quality_score.py::TestHardFilters -v`
Expected: PASS

- [ ] **Step 5: Also plan the write-back path**

When Gate 0 fails or passes for a wallet, update `wallet_registry.gate0_status` in the analyzer pipeline. Do not leave registry Gate 0 state stale while `user_analysis` becomes the only place with truth.

- [ ] **Step 6: Commit**

```bash
git add analyze_users.py tests/test_wallet_quality_score.py
git commit -m "feat: wallet hard filters — Gate 0, market maker override, evidence floor"
```

---

### Task 3: Implement wallet_quality_score computation

**Files:**
- Modify: `analyze_users.py`
- Test: `tests/test_wallet_quality_score.py`

- [ ] **Step 1: Write tests for scoring function**

```python
class TestWalletQualityScore:
    def test_high_pnl_wallet_gets_high_score(self):
        """SwissMiss-like wallet: +$956K lb_pnl should score very high."""
        from analyze_users import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=956000, closed_realized_pnl=12500000, open_cash_pnl=240000,
            qualifying_roi=12.9, closed_sample_size=485, total_capital_deployed=88000000
        )
        assert score >= 80.0

    def test_losing_wallet_gets_low_score(self):
        """Talvez10-like wallet: -$379K should score near zero."""
        from analyze_users import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=-379000, closed_realized_pnl=-338000, open_cash_pnl=7000,
            qualifying_roi=-1.9, closed_sample_size=237, total_capital_deployed=19000000
        )
        assert score <= 10.0

    def test_hidden_open_losses_reduce_score(self):
        """How.Dare.You-like: +$466K closed but -$389K open should be penalized."""
        from analyze_users import _compute_wallet_quality_score
        # Without open losses
        score_clean = _compute_wallet_quality_score(
            lb_pnl=76000, closed_realized_pnl=466000, open_cash_pnl=0,
            qualifying_roi=3.4, closed_sample_size=411, total_capital_deployed=3000000
        )
        # With open losses
        score_underwater = _compute_wallet_quality_score(
            lb_pnl=76000, closed_realized_pnl=466000, open_cash_pnl=-389000,
            qualifying_roi=3.4, closed_sample_size=411, total_capital_deployed=3000000
        )
        assert score_underwater < score_clean

    def test_no_penalty_for_high_frequency(self):
        """Systematic/algorithmic behavior should NOT reduce score."""
        from analyze_users import _compute_wallet_quality_score
        score = _compute_wallet_quality_score(
            lb_pnl=200000, closed_realized_pnl=200000, open_cash_pnl=0,
            qualifying_roi=15.0, closed_sample_size=500, total_capital_deployed=1000000
        )
        assert score >= 60.0  # should be well-scored regardless of trade frequency

    def test_small_sample_gets_lower_confidence(self):
        """Wallet with few trades should score lower than same P&L with many trades."""
        from analyze_users import _compute_wallet_quality_score
        score_small = _compute_wallet_quality_score(
            lb_pnl=50000, closed_realized_pnl=50000, open_cash_pnl=0,
            qualifying_roi=20.0, closed_sample_size=15, total_capital_deployed=100000
        )
        score_large = _compute_wallet_quality_score(
            lb_pnl=50000, closed_realized_pnl=50000, open_cash_pnl=0,
            qualifying_roi=20.0, closed_sample_size=200, total_capital_deployed=100000
        )
        assert score_large > score_small

    def test_score_range_is_0_to_100(self):
        """Score must always be in [0, 100]."""
        from analyze_users import _compute_wallet_quality_score
        for lb in [-1000000, 0, 50000, 1000000]:
            score = _compute_wallet_quality_score(
                lb_pnl=lb, closed_realized_pnl=lb, open_cash_pnl=0,
                qualifying_roi=10.0, closed_sample_size=100, total_capital_deployed=500000
            )
            assert 0.0 <= score <= 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_wallet_quality_score.py::TestWalletQualityScore -v`
Expected: FAIL — function doesn't exist

- [ ] **Step 3: Implement _compute_wallet_quality_score**

In `analyze_users.py`:

```python
def _compute_wallet_quality_score(
    *,
    lb_pnl: float | None,
    closed_realized_pnl: float | None,
    open_cash_pnl: float | None,
    qualifying_roi: float | None,
    closed_sample_size: int,
    total_capital_deployed: float | None,
) -> float:
    """Compute wallet quality score (0-100) for wallet selection.

    Weighting:
      45% lb_pnl (lifetime profitability — dominant signal)
      20% net P&L health (closed + open, drawdown correction)
      15% qualifying_roi (capital efficiency)
      20% sample/confidence (sample size, capital deployed)

    This score is used for WALLET SELECTION only.
    Position-level insider_score remains separate for entry timing.
    """
    # ── 1. Lifetime P&L component (45%) ─────────────────────────────────
    pnl_val = lb_pnl if lb_pnl is not None else (closed_realized_pnl or 0)
    # Sigmoid-like mapping: $0 = 0, $50K = 50, $200K+ = ~90, negative = 0
    if pnl_val <= 0:
        pnl_score = 0.0
    elif pnl_val >= 200_000:
        pnl_score = 90.0 + min(10.0, (pnl_val - 200_000) / 100_000)
    elif pnl_val >= 50_000:
        pnl_score = 50.0 + (pnl_val - 50_000) / 150_000 * 40.0
    elif pnl_val >= 10_000:
        pnl_score = 20.0 + (pnl_val - 10_000) / 40_000 * 30.0
    elif pnl_val >= 1_000:
        pnl_score = 5.0 + (pnl_val - 1_000) / 9_000 * 15.0
    else:
        pnl_score = pnl_val / 1_000 * 5.0
    pnl_component = min(100.0, max(0.0, pnl_score)) * 0.45

    # ── 2. Net P&L health adjustment (20%) ──────────────────────────────
    # Penalize wallets with large hidden open losses
    realized = closed_realized_pnl or 0
    open_pnl = open_cash_pnl or 0
    if realized > 0 and open_pnl < 0:
        # What fraction of realized gains are eaten by open losses?
        drawdown_ratio = abs(open_pnl) / max(realized, 1)
        # 0% drawdown = full score, 50% drawdown = half, 100%+ = zero
        health_score = max(0.0, 100.0 * (1.0 - drawdown_ratio))
    elif realized <= 0:
        health_score = max(0.0, min(100.0, 50.0 + open_pnl / 1000))
    else:
        # No open losses — full health
        health_score = 100.0
    health_component = min(100.0, max(0.0, health_score)) * 0.20

    # ── 3. Qualifying ROI (15%) ─────────────────────────────────────────
    roi_val = qualifying_roi or 0
    if roi_val >= 30:
        roi_score = 100.0
    elif roi_val >= 15:
        roi_score = 60.0 + (roi_val - 15) / 15 * 40.0
    elif roi_val >= 5:
        roi_score = 20.0 + (roi_val - 5) / 10 * 40.0
    elif roi_val > 0:
        roi_score = roi_val / 5 * 20.0
    else:
        roi_score = 0.0
    roi_component = min(100.0, max(0.0, roi_score)) * 0.15

    # ── 4. Sample/confidence (20%) ──────────────────────────────────────
    # More closed trades + more capital = higher confidence
    sample_score = 0.0
    if closed_sample_size >= 200:
        sample_score += 50.0
    elif closed_sample_size >= 50:
        sample_score += 20.0 + (closed_sample_size - 50) / 150 * 30.0
    elif closed_sample_size >= 10:
        sample_score += (closed_sample_size - 10) / 40 * 20.0

    capital = total_capital_deployed or 0
    if capital >= 1_000_000:
        sample_score += 50.0
    elif capital >= 100_000:
        sample_score += 20.0 + (capital - 100_000) / 900_000 * 30.0
    elif capital >= 10_000:
        sample_score += (capital - 10_000) / 90_000 * 20.0

    confidence_component = min(100.0, max(0.0, sample_score)) * 0.20

    # ── Final score ─────────────────────────────────────────────────────
    total = pnl_component + health_component + roi_component + confidence_component
    return round(max(0.0, min(100.0, total)), 2)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_wallet_quality_score.py::TestWalletQualityScore -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add analyze_users.py tests/test_wallet_quality_score.py
git commit -m "feat: wallet_quality_score computation — P&L-first formula"
```

---

### Task 4: Wire wallet_quality_score into the analyzer pipeline

**Files:**
- Modify: `analyze_users.py` (run_once / main analysis loop)

- [ ] **Step 1: Find where user_analysis is written and add wallet_quality_score**

In `analyze_users.py`, find the section where the analysis result dict is assembled (around line 2850-2900) and add:

```python
# After existing scoring:
filter_result = _wallet_passes_hard_filters(
    lb_pnl=lb_pnl,
    closed_sample_size=closed_sample_size,
    market_maker_detected=bool(market_maker_detected),
)

if filter_result["passes"]:
    wallet_quality = _compute_wallet_quality_score(
        lb_pnl=lb_pnl,
        closed_realized_pnl=closed_realized_pnl,
        open_cash_pnl=open_cash_pnl,
        qualifying_roi=qualifying_roi,
        closed_sample_size=closed_sample_size,
        total_capital_deployed=total_capital_deployed,
    )
else:
    wallet_quality = 0.0
wallet_quality_eligible = filter_result["eligible"]
wallet_quality_fail_reason = filter_result["fail_reason"]
```

Add `wallet_quality_score`, `wallet_quality_eligible`, `wallet_quality_fail_reason`, and `market_maker_detected` to the result dict and the INSERT/UPDATE SQL. Also update `wallet_registry.gate0_status` from `filter_result["gate0_status"]`.

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests pass (no regressions)

- [ ] **Step 3: Commit**

```bash
git add analyze_users.py
git commit -m "feat: wire wallet_quality_score into analyzer pipeline"
```

---

### Task 5: Add wallet-quality eligibility + score gating to paper trader

**Files:**
- Modify: `paper_settings.py`
- Modify: `paper_trader.py`

- [ ] **Step 1: Add threshold to paper_settings.py**

```python
# Wallet quality score minimum for entry (wallet-level selection, not position timing)
# Wallets must pass this threshold to have their signals copied at all.
# This is separate from PAPER_ENTRY_MIN_INSIDER_SCORE which gates position timing.
PAPER_MIN_WALLET_QUALITY_SCORE = float(os.getenv("PAPER_MIN_WALLET_QUALITY_SCORE", "30"))
```

- [ ] **Step 2: Add wallet_quality_score check to paper_trader entry path**

In `paper_trader.py`, in the signal evaluation function (around line 850-900), BEFORE the insider_score check, add:

```python
# 0a0. Wallet quality gate (wallet-level selection, separate from position timing)
wallet_quality_eligible = signal.get('wallet_quality_eligible')
wallet_quality = signal.get('wallet_quality_score')

if wallet_quality_eligible in (0, "0", False):
    logger.info(
        "Skipping %s... wallet hard-filter fail: %s",
        wallet[:8], signal.get('wallet_quality_fail_reason') or 'ineligible',
    )
    return

if wallet_quality is not None and float(wallet_quality) < PAPER_MIN_WALLET_QUALITY_SCORE:
    logger.info(
        "Skipping %s... wallet_quality_score=%.1f < %.0f threshold",
        wallet[:8], float(wallet_quality), PAPER_MIN_WALLET_QUALITY_SCORE,
    )
    return
```

Note: `NULL` means unscored during rollout and is allowed through temporarily. Explicit ineligible wallets must be blocked via `wallet_quality_eligible`, not by overloading a score of `0`.

- [ ] **Step 3: Make sure the signal payload actually includes the new fields**

If `/api/open-position-signals` does not already expose `wallet_quality_score`, `wallet_quality_eligible`, and `wallet_quality_fail_reason`, add that plumbing before editing the trader gate.

- [ ] **Step 4: Import the new setting**

Add `PAPER_MIN_WALLET_QUALITY_SCORE` to the imports from paper_settings in paper_trader.py.

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests pass

- [ ] **Step 6: Commit**

```bash
git add paper_settings.py paper_trader.py
git commit -m "feat: paper trader gates on wallet_quality_score for wallet selection"
```

---

### Task 6: Update insider_copy_v1 strategy

**Files:**
- Modify: `strategies/insider_copy_v1.py`

- [ ] **Step 1: Add wallet_quality_score awareness**

In `insider_copy_v1.py` `enter()` method, add a check that the source wallet's wallet_quality_score is above a threshold. Read from user_analysis via the conn parameter.

```python
# Wallet quality check (wallet selection, not position timing)
try:
    wq_row = conn.execute(
        "SELECT wallet_quality_score FROM user_analysis WHERE wallet = ? LIMIT 1",
        (signal.wallet,)
    ).fetchone()
    wq_score = float(wq_row[0] or 0) if wq_row and wq_row[0] is not None else None
    min_wq = _to_float(params.get("min_wallet_quality_score"), 30.0)
    if wq_score is not None and wq_score < min_wq:
        return StrategyEntryDecision(
            False, f"wallet_quality_score {wq_score:.1f} < {min_wq:.0f}"
        )
except Exception:
    pass  # graceful fallback if column doesn't exist yet
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add strategies/insider_copy_v1.py
git commit -m "feat: insider_copy_v1 checks wallet_quality_score for entry"
```

---

### Task 7: Update API to expose wallet-quality fields (UI handled separately)

**Files:**
- Modify: `routes/api_core.py` (leaderboard endpoint)

- [ ] **Step 1: Add wallet-quality fields to the relevant API responses**

In `routes/api_core.py`, find the wallet leaderboard and signal queries and add:
- `wallet_quality_score`
- `wallet_quality_eligible`
- `wallet_quality_fail_reason`

Do **not** do UI work in this plan. That belongs to the separate UI lane.

- [ ] **Step 2: Verify API returns the new fields**

```bash
curl -s -H "X-API-Key: ..." http://localhost:8080/api/wallet-followable-leaderboard | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['items'][0].keys())"
```

- [ ] **Step 3: Commit**

```bash
git add routes/api_core.py
git commit -m "feat: include wallet_quality_score in leaderboard API"
```

---

### Task 8: Update documentation and brain entries

**Files:**
- Modify: `CLAUDE.md`
- Run: brain update script

- [ ] **Step 1: Add ADR-007 to CLAUDE.md**

```markdown
### ADR-007: Two-score wallet evaluation system
**Decision:** Wallet evaluation uses TWO separate scores:
  - `wallet_quality_score` (0-100): P&L-first, for wallet SELECTION. 45% lb_pnl, 20% net P&L health, 15% ROI, 20% confidence.
  - `position_insider_score` (0-100): behavior/timing, for entry TIMING within selected wallets.
**Rationale:** March 25 2026 audit proved insider_score inversely correlates with wallet profitability. Score 80+ wallets avg $16K, score 1-19 avg $160K. Position-level timing signal at 90+ still shows 65.5% WR (valid). Wallet selection must use lifetime P&L.
**Consequence:** Paper trader and strategies check BOTH scores: wallet_quality_score >= threshold for wallet selection, position_insider_score >= threshold for entry timing.
```

- [ ] **Step 2: Update brain entries**

```bash
ssh sniper@188.166.91.20 "cd ~/sniper && export $(grep -E '^SUPABASE_' .env | xargs) && .venv/bin/python3 -c '
from bot.supabase_helper import upsert_memory
import json
upsert_memory("knowledge", "scoring/two_score_system",
    "ADR-007: wallet_quality_score (P&L-first, wallet selection) + position_insider_score (timing, entry). Implemented 2026-03-25.",
    json.dumps({"wallet_quality": "45% lb_pnl + 20% health + 15% roi + 20% confidence", "position_insider": "unchanged timing signal", "hard_filters": ["gate0", "market_maker", "evidence_floor"]})
)'"
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: ADR-007 two-score wallet evaluation system"
```

---

### Task 9: Deploy and verify

**Files:** None (deployment only)

- [ ] **Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -q --tb=short
```
Expected: All tests pass (1658+ passing)

- [ ] **Step 2: Deploy to VPS**

```bash
git push origin main
ssh sniper@188.166.91.20 "cd ~/sniper && git pull origin main && systemctl --user restart sniper-web sniper-analyzer"
```

- [ ] **Step 3: Verify wallet_quality_score is being computed**

Wait 5 minutes for analyzer to run, then:

```bash
ssh sniper@188.166.91.20 "cd ~/sniper && .venv/bin/python3 -c '
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
scored = c.execute(\"SELECT COUNT(*) FROM user_analysis WHERE wallet_quality_score IS NOT NULL AND wallet_quality_score > 0\").fetchone()[0]
total = c.execute(\"SELECT COUNT(*) FROM user_analysis\").fetchone()[0]
print(f\"Scored: {scored}/{total}\")
# Verify SwissMiss gets a high score
sm = c.execute(\"SELECT wallet_quality_score, insider_score FROM user_analysis WHERE wallet LIKE \\\"0xdbade4c82f%\\\"\").fetchone()
if sm: print(f\"SwissMiss: quality={sm[0]} insider={sm[1]}\")
'"
```

Expected: SwissMiss wallet_quality_score >> insider_score

- [ ] **Step 4: Verify paper trader uses new score**

Check logs for wallet_quality_score gating messages:

```bash
ssh sniper@188.166.91.20 "journalctl --user -u sniper-paper --since '5 min ago' --no-pager | grep wallet_quality"
```

---

## Execution Notes

- Tasks 1-3 are the core scoring engine — must be done sequentially
- Task 4 wires it into the live analyzer — depends on Tasks 1-3
- Task 5 adds it to the entry pipeline — depends on Task 4
- Task 6 updates the strategy — can be done after Task 4
- Task 7 updates the API — can be done after Task 4
- Task 8 updates docs — can be done anytime
- Task 9 is deployment — after everything else
- Tasks 5-8 can only run in parallel if each worker owns disjoint files

- The rollout is safe because:
  - `wallet_quality_score = NULL` means unscored and is allowed through temporarily during rollout
  - `wallet_quality_eligible = 0` means explicit hard-filter fail and must block immediately
  - numeric score below threshold means scored-but-weak and must block once the field is present

- Position-level insider_score is NOT modified anywhere in this plan. The `_score_insider()` function and `_score_position_insider()` function remain untouched.
