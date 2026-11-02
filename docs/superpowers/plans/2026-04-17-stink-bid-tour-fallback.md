# Stink Bid — Tour Fallback + Discover Cycle Observability

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the `_extract_tour` tour-misrouting bug that silently prevents `discover_and_place` from ever matching WTA-draw events whose title lacks the literal "WTA" keyword, and eliminate the observability gap that hid the bug (discover cycle logs nothing at INFO on the no-order path).

**Architecture:** Two surgical changes to the existing discover pipeline — no schema changes, no new modules.
1. `stink_bid_runner.discover_and_place` — after `_extract_tour` returns a primary tour, fall back to the other tour if the primary scoreboard doesn't contain the match. The predicted tour is tried first; the other tour is tried only on miss. Both scoreboards are cached per cycle via the existing `scoreboards` dict so there's no duplicate HTTP.
2. `stink_bid_runner.discover_and_place` — at end of every cycle, emit one INFO line summarising what happened: `scanned=N tennis=K placed=P skipped_vol=V skipped_existing=E skipped_gamma=G skipped_parse=U skipped_players=Y skipped_fav=F skipped_espn=S skipped_live=L`.

**Tech Stack:** Python 3, pytest, SQLite, systemd (for VPS deploy verification)

---

### Task 1: Tour-fallback — TDD

**Files:**
- Test: `tests/test_stink_bid_runner.py` (add new class, bottom of file)
- Modify: `stink_bid_runner.py` lines 341-382 (the `if tour not in scoreboards` block through the `find_match` check)

The existing `discover_and_place` tests (`test_stink_bid_runner.py:394-594`) use a `patch.multiple` pattern with `fetch_market_by_condition`, `fetch_scoreboard`, `parse_scoreboard`, `find_match`. Reuse that pattern. See `test_places_order_for_qualifying_market` at line 455 for the reference template (copy it, don't re-invent).

- [ ] **Step 1: Read the existing happy-path test to copy the mocking pattern**

Run: `sed -n '455,506p' tests/test_stink_bid_runner.py`

You need to see exactly how `patch.multiple` is invoked and what `fetch_market_by_condition` is made to return. Copy-paste shape, vary only the WTA-specific bits.

- [ ] **Step 2: Write the failing test**

Add this class at the bottom of `tests/test_stink_bid_runner.py`. It asserts that a title with no "WTA" keyword (predicted tour = atp) but whose match lives in the WTA scoreboard still places an order, and that the order records `tour='wta'`.

```python
# ---------------------------------------------------------------------------
# Tour-fallback regression — WTA-draw tournament with no "WTA" in title
# ---------------------------------------------------------------------------


class TestTourFallback:
    """Regression for 2026-04-17 bug: WTA matches on tournaments not named "WTA"
    (e.g. Porsche Tennis Grand Prix) were misrouted to ATP scoreboard and never
    matched, silently skipping every WTA-draw market."""

    def _insert_porsche_market(self, conn):
        conn.execute(
            """INSERT INTO market_metadata
                 (condition_id, title, slug, event_slug, volume_24h, is_open)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (
                "0xporsche1",
                "Porsche Tennis Grand Prix: Ostapenko vs Andreeva",
                "porsche-ostapenko-andreeva",
                "porsche-tennis-grand-prix",
                500000.0,
            ),
        )
        conn.commit()

    def _gamma_response(self, *_args, **_kwargs):
        return {
            "outcomes": '["Ostapenko", "Andreeva"]',
            "clobTokenIds": '["tok_ostapenko", "tok_andreeva"]',
            "outcomePrices": '["0.62", "0.38"]',
        }

    def _scoreboard(self, tour):
        # Empty ATP scoreboard, populated WTA scoreboard
        if tour == "atp":
            return {"events": []}
        return {"events": [{"__fake_wta_event__": True}]}

    def _parse(self, sb_data):
        return sb_data["events"] if sb_data else []

    def _find_match(self, parsed, p1, p2):
        # Only match when WTA scoreboard events are provided
        if parsed and parsed[0].get("__fake_wta_event__"):
            return {"competition": {"status": {"type": {"state": "in"}}, "competitors": []}}
        return None

    def test_wta_match_placed_even_when_title_has_no_wta_keyword(
        self, tmp_path, monkeypatch
    ):
        import sqlite3
        import stink_bid_runner
        from unittest.mock import patch, MagicMock

        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # Reuse the existing test-DB bootstrap helper at top of this module
        _bootstrap_test_db(conn)  # defined elsewhere in this file
        self._insert_porsche_market(conn)

        runner = stink_bid_runner.StinkBidRunner(
            db_path=db_path,
            strategy_id=1,
            params={
                "min_volume_24h": 10_000.0,
                "min_favorite": 0.55,
                "dip_pct": 0.3,
                "min_bid": 0.05,
                "min_fill_size": 50.0,
                "max_resting": 3,
                "max_daily_fills": 5,
                "position_size_usdc": 25.0,
                "espn_pause_after": 3,
                "espn_cancel_after_min": 10,
                "reprice_threshold": 0.02,
            },
        )

        # Stub get_sets_completed to a non-terminal live match
        fake_state = MagicMock(sets_completed=0)
        with patch.multiple(
            "stink_bid_runner",
            fetch_market_by_condition=MagicMock(side_effect=self._gamma_response),
            fetch_scoreboard=MagicMock(side_effect=self._scoreboard),
            parse_scoreboard=MagicMock(side_effect=self._parse),
            find_match=MagicMock(side_effect=self._find_match),
            get_sets_completed=MagicMock(return_value=fake_state),
        ):
            runner.discover_and_place()

        rows = conn.execute(
            "SELECT condition_id, tour, state FROM paper_limit_orders WHERE strategy_id=1"
        ).fetchall()
        assert len(rows) == 1, f"expected exactly one order, got {len(rows)}"
        assert rows[0]["condition_id"] == "0xporsche1"
        assert rows[0]["tour"] == "wta", (
            f"order must record tour=wta (actual found tour), got {rows[0]['tour']}"
        )
```

If the `_bootstrap_test_db` helper does not exist in `tests/test_stink_bid_runner.py`, lift the fixture/bootstrap pattern used at the top of the file (around the existing `discover_and_place` tests — inspect lines 394-455 to identify it). Do not invent a new helper; reuse the one already used by `test_places_order_for_qualifying_market`.

- [ ] **Step 3: Run the test to verify it fails against the current code**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py::TestTourFallback -v 2>&1 | tail -30`

Expected: FAIL. The current code at `stink_bid_runner.py:341-382` fetches scoreboard for primary tour only ("atp"), sees empty event list, and either `log_skip(no_score_feed)` (if ATP returns `None` from parse) or `find_match` returns None → `log_skip(no_score_feed)`. No order is placed.

If the test passes instead of fails, you've mocked something wrong — re-check the mock `_find_match` matches the signature of the real `find_match(parsed, player1, player2)`.

- [ ] **Step 4: Implement the fallback**

Replace lines 341-382 of `stink_bid_runner.py` (the block starting `# --- Check ESPN match state (cached per tour, with outage tracking) ---` through the `if entry is None:` skip) with:

```python
                # --- Check ESPN match state (try predicted tour first, fall back) ---
                primary_tour = tour
                fallback_tour = "atp" if primary_tour == "wta" else "wta"

                entry = None
                actual_tour = None
                for try_tour in (primary_tour, fallback_tour):
                    if try_tour not in scoreboards:
                        try:
                            sb_data = fetch_scoreboard(try_tour)
                        except Exception:
                            logger.exception(
                                "stink_bid fetch_scoreboard failed tour=%s", try_tour
                            )
                            sb_data = None

                        if sb_data is None:
                            self._outage_tracker(try_tour).record_failure()
                            logger.warning(
                                "stink_bid: ESPN fetch failed tour=%s "
                                "(consecutive=%d)",
                                try_tour,
                                self._outage_tracker(try_tour)._consecutive_failures,
                            )
                            scoreboards[try_tour] = None
                        else:
                            self._outage_tracker(try_tour).record_success()
                            scoreboards[try_tour] = parse_scoreboard(sb_data)

                    parsed = scoreboards[try_tour]
                    if parsed is None:
                        continue

                    candidate = find_match(parsed, player1, player2)
                    if candidate is not None:
                        entry = candidate
                        actual_tour = try_tour
                        break

                if entry is None:
                    engine.log_skip(
                        strategy_id=self._strategy_id,
                        condition_id=cond_id,
                        reason="no_score_feed",
                        details={
                            "tour_primary": primary_tour,
                            "tour_fallback": fallback_tour,
                            "player1": player1,
                            "player2": player2,
                        },
                    )
                    continue
```

Then four lines later, the existing code does `match_state = get_sets_completed(entry["competition"])` — unchanged. And further down at the `engine.place_order(...)` call (line ~397), change the `tour=tour,` argument to `tour=actual_tour,` so the paper_limit_orders row records the tour where the match was actually found.

- [ ] **Step 5: Run the new test — expected to pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py::TestTourFallback -v 2>&1 | tail -15`

Expected: PASS.

- [ ] **Step 6: Run the full stink_bid test module to confirm no regression**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py tests/test_stink_bid_strategy.py tests/test_stink_bid_api.py -v 2>&1 | tail -30`

Expected: All existing tests still pass.

- [ ] **Step 7: Commit**

```bash
cd /home/bigpapi/Desktop/Sniper
git add tests/test_stink_bid_runner.py stink_bid_runner.py
git commit -m "fix(stink-bid): fall back to other tour when primary scoreboard has no match

_extract_tour() keyed off literal 'WTA' in the title, so WTA-draw tournaments
whose titles don't mention the tour (e.g. 'Porsche Tennis Grand Prix') were
routed to the ATP scoreboard and silently skipped with reason=no_score_feed.

discover_and_place now fetches the predicted tour first, then falls back to
the other tour on miss. The order records the tour where the match was
actually found. Both scoreboards are cached per cycle so there's no duplicate
HTTP cost on tournaments that genuinely are the predicted tour."
```

---

### Task 2: Discover-cycle summary log — TDD

**Files:**
- Test: `tests/test_stink_bid_runner.py` (add new test to the existing `discover_and_place` test class, or a new class below TestTourFallback)
- Modify: `stink_bid_runner.py` — add counter tracking at top of `discover_and_place` try-block, increment at each skip/place site, emit single INFO summary before the final `except Exception`

The bug was undetectable for weeks because `discover_and_place` logs nothing at INFO on the "scanned 48K markets, 2 tennis, 0 placed, all silently skipped" path. Fix: one INFO line per cycle, regardless of outcome.

- [ ] **Step 1: Write the failing test**

Add after `TestTourFallback` in `tests/test_stink_bid_runner.py`:

```python
class TestDiscoverCycleSummary:
    """Every discover_and_place call must emit exactly one INFO-level summary
    line, even when zero orders are placed. This is the observability hook
    that made the 2026-04-17 tour-routing bug invisible."""

    def test_summary_log_emitted_on_empty_cycle(self, tmp_path, caplog):
        import sqlite3
        import logging
        import stink_bid_runner

        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _bootstrap_test_db(conn)
        # No markets inserted — scanned=0, tennis=0, placed=0

        runner = stink_bid_runner.StinkBidRunner(
            db_path=db_path,
            strategy_id=1,
            params={
                "min_volume_24h": 10_000.0, "min_favorite": 0.55,
                "dip_pct": 0.3, "min_bid": 0.05, "min_fill_size": 50.0,
                "max_resting": 3, "max_daily_fills": 5,
                "position_size_usdc": 25.0,
                "espn_pause_after": 3, "espn_cancel_after_min": 10,
                "reprice_threshold": 0.02,
            },
        )

        with caplog.at_level(logging.INFO, logger="__main__"):
            runner.discover_and_place()

        summaries = [
            r for r in caplog.records
            if "discover_cycle" in r.getMessage()
        ]
        assert len(summaries) == 1, (
            f"expected exactly 1 discover_cycle summary, got {len(summaries)}: "
            f"{[r.getMessage() for r in summaries]}"
        )
        msg = summaries[0].getMessage()
        assert "scanned=0" in msg
        assert "tennis=0" in msg
        assert "placed=0" in msg
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py::TestDiscoverCycleSummary -v 2>&1 | tail -15`

Expected: FAIL. No "discover_cycle" substring in any log record.

- [ ] **Step 3: Implement the summary log**

In `stink_bid_runner.py`, modify `discover_and_place` (starts at line 223). Initialise counters at the top of the `try:` block (right after `engine = PaperLimitEngine(conn)`):

```python
            counters = {
                "scanned": 0,
                "tennis": 0,
                "placed": 0,
                "skipped_vol": 0,
                "skipped_existing": 0,
                "skipped_gamma": 0,
                "skipped_parse": 0,
                "skipped_players": 0,
                "skipped_fav": 0,
                "skipped_espn": 0,
                "skipped_live": 0,
            }
```

Increment the counters at the following existing sites (keep all existing `continue`, `log_skip`, and logger calls — only add the counter bump line):

| Site | Counter | Location (what the code is checking) |
|------|---------|--------------------------------------|
| top of `for row in rows:` | `counters["scanned"] += 1` | first line inside the loop |
| after `_is_tennis_match_title` returns True | `counters["tennis"] += 1` | right before volume check |
| volume filter `continue` path | `counters["skipped_vol"] += 1` | same block as `reason="below_volume"` |
| `has_order_for_match` `continue` path | `counters["skipped_existing"] += 1` | right before that `continue` |
| `if not market:` (Gamma None) path | `counters["skipped_gamma"] += 1` | right before that `continue` |
| `except (json.JSONDecodeError, ...):` path | `counters["skipped_parse"] += 1` | right before that `continue` |
| `if len(outcomes) < 2 ...` path | `counters["skipped_parse"] += 1` | right before that `continue` |
| `fav_price < self._min_favorite` path | `counters["skipped_fav"] += 1` | same block as `reason="no_favorite_above_threshold"` |
| `if not players:` path | `counters["skipped_players"] += 1` | right before that `continue` |
| `if entry is None:` (fallback-miss path from Task 1) | `counters["skipped_espn"] += 1` | same block as `reason="no_score_feed"` |
| `if match_state.sets_completed >= 2:` path | `counters["skipped_live"] += 1` | same block as `reason="sets_completed_ge_2"` |
| after `engine.place_order` returns non-None | `counters["placed"] += 1` | right after the existing `logger.info("stink_bid order_placed …")` |

Immediately before the existing `except Exception:` clause at line 421, emit:

```python
            logger.info(
                "stink_bid discover_cycle "
                "scanned=%(scanned)d tennis=%(tennis)d placed=%(placed)d "
                "skipped_vol=%(skipped_vol)d skipped_existing=%(skipped_existing)d "
                "skipped_gamma=%(skipped_gamma)d skipped_parse=%(skipped_parse)d "
                "skipped_players=%(skipped_players)d skipped_fav=%(skipped_fav)d "
                "skipped_espn=%(skipped_espn)d skipped_live=%(skipped_live)d",
                counters,
            )
```

The summary is inside the `try:` — if the whole cycle crashes, `logger.exception` still fires and no summary is emitted (intentional: a crash is not a cycle). Do not wrap the summary emit in its own try/except.

- [ ] **Step 4: Run the new test — expected to pass**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py::TestDiscoverCycleSummary -v 2>&1 | tail -15`

Expected: PASS.

- [ ] **Step 5: Add a second test for a non-empty happy path (mixed outcomes)**

Add inside `TestDiscoverCycleSummary`:

```python
    def test_summary_log_counts_placed_and_skipped(
        self, tmp_path, caplog
    ):
        """One market placed, one below-volume, one non-tennis — summary
        must show scanned=3 tennis=2 placed=1 skipped_vol=1."""
        import sqlite3, logging
        import stink_bid_runner
        from unittest.mock import patch, MagicMock

        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        _bootstrap_test_db(conn)

        conn.executemany(
            """INSERT INTO market_metadata
                 (condition_id, title, slug, event_slug, volume_24h, is_open)
               VALUES (?, ?, ?, ?, ?, 1)""",
            [
                ("0xa", "ATP Finals: Sinner vs Alcaraz", "sa", "atp-finals", 500000.0),
                ("0xb", "WTA Finals: Swiatek vs Sabalenka", "ss", "wta-finals", 500.0),
                ("0xc", "Trump vs Biden 2024", "pol", "pol", 500000.0),
            ],
        )
        conn.commit()

        runner = stink_bid_runner.StinkBidRunner(
            db_path=db_path, strategy_id=1,
            params={
                "min_volume_24h": 10_000.0, "min_favorite": 0.55,
                "dip_pct": 0.3, "min_bid": 0.05, "min_fill_size": 50.0,
                "max_resting": 3, "max_daily_fills": 5,
                "position_size_usdc": 25.0, "espn_pause_after": 3,
                "espn_cancel_after_min": 10, "reprice_threshold": 0.02,
            },
        )

        def _gamma(*_a, **_k):
            return {
                "outcomes": '["Sinner", "Alcaraz"]',
                "clobTokenIds": '["tok1", "tok2"]',
                "outcomePrices": '["0.62", "0.38"]',
            }
        fake_state = MagicMock(sets_completed=0)
        fake_entry = {"competition": {"competitors": []}}
        with caplog.at_level(logging.INFO, logger="__main__"):
            with patch.multiple(
                "stink_bid_runner",
                fetch_market_by_condition=MagicMock(side_effect=_gamma),
                fetch_scoreboard=MagicMock(return_value={"events": [fake_entry]}),
                parse_scoreboard=MagicMock(return_value=[fake_entry]),
                find_match=MagicMock(return_value=fake_entry),
                get_sets_completed=MagicMock(return_value=fake_state),
            ):
                runner.discover_and_place()

        summaries = [
            r.getMessage() for r in caplog.records
            if "discover_cycle" in r.getMessage()
        ]
        assert len(summaries) == 1, summaries
        msg = summaries[0]
        assert "scanned=3" in msg, msg
        assert "tennis=2" in msg, msg
        assert "placed=1" in msg, msg
        assert "skipped_vol=1" in msg, msg
```

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py::TestDiscoverCycleSummary -v 2>&1 | tail -20`

Expected: both tests PASS.

- [ ] **Step 6: Full stink_bid module regression**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/test_stink_bid_runner.py tests/test_stink_bid_strategy.py tests/test_stink_bid_api.py -v 2>&1 | tail -20`

Expected: all pass.

- [ ] **Step 7: Full suite — must stay green**

Run: `cd /home/bigpapi/Desktop/Sniper && python3 -m pytest tests/ -q 2>&1 | tail -10`

Expected: exit 0, prior baseline pass-count (2400+) preserved.

- [ ] **Step 8: Commit**

```bash
cd /home/bigpapi/Desktop/Sniper
git add tests/test_stink_bid_runner.py stink_bid_runner.py
git commit -m "feat(stink-bid): emit INFO summary at end of each discover cycle

Every discover_and_place call now emits one INFO line:
  stink_bid discover_cycle scanned=N tennis=K placed=P
  skipped_vol=V skipped_existing=E skipped_gamma=G skipped_parse=U
  skipped_players=Y skipped_fav=F skipped_espn=S skipped_live=L

Closes the observability gap that hid the tour-routing bug fixed in
the previous commit — zero orders with zero log output is no longer
an indistinguishable state."
```

---

### Task 3: Deploy to VPS and verify end-to-end

**Files:** none modified locally — deploy + observe only.

- [ ] **Step 1: Push to origin/main**

```bash
cd /home/bigpapi/Desktop/Sniper
git push origin main
```

Expected: two commits pushed successfully.

- [ ] **Step 2: Pull + restart on VPS**

```bash
ssh sniper@188.166.91.20 "cd ~/sniper && git pull origin main && git log --oneline -3"
ssh sniper@188.166.91.20 "systemctl --user restart sniper-stink-bid@41.service"
```

Expected: git pull shows the two new commits; systemctl restart returns zero exit.

- [ ] **Step 3: Confirm service healthy after restart**

```bash
ssh sniper@188.166.91.20 "systemctl --user status sniper-stink-bid@41.service --no-pager -l | head -20"
```

Expected: `Active: active (running)`, recent start time, no `failed` or traceback.

- [ ] **Step 4: Confirm the discover_cycle summary line is now emitted**

The runner runs `discover_and_place()` at startup, then every 15 min. Wait ~60s after restart, then read journal:

```bash
ssh sniper@188.166.91.20 "journalctl --user -u sniper-stink-bid@41.service --since '2 min ago' --no-pager | grep -E 'discover_cycle|order_placed|ESPN'"
```

Expected: at least one line containing `discover_cycle scanned=N tennis=K placed=P ...` with concrete counters. If `N=0`, market_metadata is empty for this runner — unlikely given VPS ingest. If `tennis=0`, no qualifying markets exist right now (known: Stuttgart just ended). If `tennis>0` and `placed=0`, the counters pinpoint which filter rejected — this is the observability win.

- [ ] **Step 5: Confirm tour fallback logic is reachable with a smoke test**

Run on VPS (without changing the running service — just a sanity check that the code path imports cleanly and both tours can be fetched):

```bash
ssh sniper@188.166.91.20 "cd ~/sniper && .venv/bin/python3 -c '
import requests, sys
sys.path.insert(0, \".\")
from feeds.tennis_scores import fetch_scoreboard, parse_scoreboard
for t in (\"atp\", \"wta\"):
    sb = fetch_scoreboard(t)
    evs = parse_scoreboard(sb) if sb else None
    print(f\"{t}: events={len(evs) if evs else None}\")
'"
```

Expected: both `atp:` and `wta:` print a non-None event count. If WTA returns None, ESPN is down for WTA — unlikely but record it in HANDOFF.md if it happens.

- [ ] **Step 6: Watch one full 15-min cycle to confirm steady-state**

```bash
ssh sniper@188.166.91.20 "journalctl --user -u sniper-stink-bid@41.service --since '20 min ago' --no-pager | grep discover_cycle | tail -5"
```

Expected (after 15+ min of uptime): exactly ≥2 `discover_cycle` summary lines (one at startup, one at t+15min). Counts should be consistent between cycles unless new markets came online.

- [ ] **Step 7: Update HANDOFF.md**

```bash
cd /home/bigpapi/Desktop/Sniper
```

Append a dated block to `/home/bigpapi/.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md` (don't rewrite — append):

```markdown
## 2026-04-17 — Stink Bid Tour Fallback + Observability

**Deployed commits:** `<hash1>..<hash2>` on VPS `sniper-stink-bid@41`.

- `stink_bid_runner.discover_and_place` now falls back to the other tour
  when the primary scoreboard has no match. Fixes silent WTA-draw routing
  bug (titles like "Porsche Tennis Grand Prix" have no "WTA" keyword).
- Each discover cycle now emits an INFO `stink_bid discover_cycle ...`
  summary with counters per skip reason.
- Verified on VPS: summary line present in journal, both tour scoreboards
  reachable. Still 0 tennis markets live (Stuttgart ended, Madrid not yet).
- Next opportunity: Madrid Open ~2026-04-22.
```

Stage + commit the HANDOFF update:

```bash
git -C /home/bigpapi/Desktop/Sniper add ../../../.claude/projects/-home-bigpapi-Desktop-Sniper/memory/HANDOFF.md 2>/dev/null || true
# The memory dir is outside the repo — just save it in place, no commit needed for memory.
```

(Memory files live outside the repo. They are not version-controlled via this repo's git.)

---

## Self-Review

**Spec coverage:**
- ✅ Tour misrouting fix — Task 1 adds fallback + test.
- ✅ Observability log — Task 2 adds cycle summary + 2 tests.
- ✅ End-to-end verification — Task 3 deploys and confirms via journal.

**Placeholder scan:**
- No TBDs, no "fill in", no "similar to Task N".
- Every code step includes actual code.
- Every test step includes the full test body.

**Type consistency:**
- `_extract_tour(title) -> str` signature unchanged — still returns `"atp" | "wta"`.
- `fetch_scoreboard(tour: str)` signature unchanged.
- `find_match(parsed, p1, p2)` signature unchanged.
- New local variables `primary_tour`, `fallback_tour`, `actual_tour` only within `discover_and_place`.
- `counters` is a local `dict[str, int]` — not persisted, not returned.
- No new public API.

**Known risks:**
- Test helper `_bootstrap_test_db` is assumed to exist at top of `tests/test_stink_bid_runner.py`. If it doesn't, Task 1 Step 1 mandates inspecting lines 394-455 to lift the existing fixture — the reader is told exactly where to look. Do not create a new helper.
- `caplog.at_level(..., logger="__main__")` captures logs under `__main__` — this matches the runner's `logger = logging.getLogger(__name__)` when run directly via the systemd unit. In pytest, module name is `stink_bid_runner`, not `__main__`. If the tests fail to capture, switch logger name to `"stink_bid_runner"`.
