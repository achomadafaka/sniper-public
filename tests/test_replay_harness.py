"""
Replay harness for deterministic scoring regression detection (Track A).

Design:
- Golden fixtures live in tests/fixtures/scoring_golden.json.
- Each fixture captures the inputs + expected output bounds for a pure scoring
  function (_score_insider, _score_speculator, _classify_wallet).
- On each run the harness re-executes the live function and compares outputs.
- When a score drifts outside expected bounds the test fails with a diff showing
  the exact delta, so an engineer can judge intentional vs accidental changes.
- To regenerate golden fixtures after an intentional scoring change, run:
    PYTHONPATH=. python tests/test_replay_harness.py --regenerate
  This re-executes all fixtures and updates "actual_*" fields in the JSON for
  inspection, but does NOT overwrite "expected" fields automatically — that
  must be a deliberate human edit to prevent silent threshold drift.

Fixture schema (per entry in scoring_golden.json):
  _id          : str   — unique stable identifier
  _description : str   — human-readable intent
  fn           : str   — one of: _score_insider | _score_speculator | _classify_wallet
  inputs       : dict  — kwargs passed to the function verbatim
  expected     : dict  — assertions (see _check_expectations below)

Expected assertion keys:
  score_gte        : float  — returned score must be >= this value
  score_lte        : float  — returned score must be <= this value
  classification   : str    — _classify_wallet must return this classification exactly
  classification_not : str  — _classify_wallet must NOT return this classification
  matches_criteria : bool   — _classify_wallet 2nd return value must equal this
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
import textwrap
import unittest
from sniper.settings.scoring_env_defaults import pin_scoring_env

# ── fixture location ──────────────────────────────────────────────────────────
_FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "scoring_golden.json"

# ── scoring determinism ───────────────────────────────────────────────────────
# Force-pin all scoring env vars to canonical defaults before importing
# analyze_users so module-level constants are baked from known values.
pin_scoring_env(force=True)

# ── import target functions (after env pinning) ───────────────────────────────
import analyze_users as _au
# Reload so module-level constants re-read from our now-pinned env vars.
importlib.reload(_au)

_score_insider = _au._score_insider
_score_speculator = _au._score_speculator
_classify_wallet = _au._classify_wallet

_FN_MAP = {
    "_score_insider": _score_insider,
    "_score_speculator": _score_speculator,
    "_classify_wallet": _classify_wallet,
}


# ── assertion engine ──────────────────────────────────────────────────────────

def _check_expectations(
    fixture_id: str,
    fn_name: str,
    result,
    expected: dict,
) -> list[str]:
    """Return a list of failure messages (empty = all assertions pass)."""
    failures = []

    if fn_name in ("_score_insider", "_score_speculator"):
        # result is (score, reasons)
        score, reasons = result

        if "score_gte" in expected and score < expected["score_gte"]:
            failures.append(
                f"[{fixture_id}] score {score:.2f} < expected gte {expected['score_gte']:.2f}"
                f"  (reasons: {reasons})"
            )
        if "score_lte" in expected and score > expected["score_lte"]:
            failures.append(
                f"[{fixture_id}] score {score:.2f} > expected lte {expected['score_lte']:.2f}"
                f"  (reasons: {reasons})"
            )

    elif fn_name == "_classify_wallet":
        # result is (classification, matches_criteria, checks_dict, tier_data)
        classification, matches_criteria, checks, _tier_data = result

        if "classification" in expected and classification != expected["classification"]:
            failures.append(
                f"[{fixture_id}] classification={classification!r}"
                f" != expected {expected['classification']!r}"
                f"\n  checks: {_failed_checks(checks)}"
            )
        if "classification_not" in expected and classification == expected["classification_not"]:
            failures.append(
                f"[{fixture_id}] classification={classification!r}"
                f" must NOT be {expected['classification_not']!r}"
                f"\n  checks: {_failed_checks(checks)}"
            )
        # classification_not_2 allows a second exclusion without a separate fixture
        if "classification_not_2" in expected and classification == expected["classification_not_2"]:
            failures.append(
                f"[{fixture_id}] classification={classification!r}"
                f" must NOT be {expected['classification_not_2']!r}"
                f"\n  checks: {_failed_checks(checks)}"
            )
        if "matches_criteria" in expected and matches_criteria != expected["matches_criteria"]:
            failures.append(
                f"[{fixture_id}] matches_criteria={matches_criteria}"
                f" != expected {expected['matches_criteria']}"
                f"\n  classification={classification!r}"
                f"\n  checks: {_failed_checks(checks)}"
            )

    return failures


def _failed_checks(checks: dict) -> str:
    failed = {k: v for k, v in checks.items() if not v}
    return json.dumps(failed, indent=2) if failed else "(all passed)"


# ── harness runner ────────────────────────────────────────────────────────────

def _load_fixtures() -> list[dict]:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _run_fixture(fixture: dict) -> tuple[bool, list[str], object]:
    """Run a single fixture. Returns (passed, failure_messages, raw_result)."""
    fn_name = fixture["fn"]
    fn = _FN_MAP[fn_name]
    inputs = fixture["inputs"]
    expected = fixture["expected"]

    result = fn(**inputs)
    failures = _check_expectations(fixture["_id"], fn_name, result, expected)
    return (len(failures) == 0), failures, result


# ── pytest-compatible test class ─────────────────────────────────────────────

def _make_test_method(fixture: dict):
    """Factory that creates a bound test method for one fixture."""
    def test_method(self):
        passed, failures, result = _run_fixture(fixture)
        if not passed:
            msg = "\n\nScoring regression detected:\n" + "\n".join(failures)
            msg += textwrap.dedent(f"""

            Fixture: {fixture['_id']}
            Description: {fixture['_description']}
            Function: {fixture['fn']}
            Inputs: {json.dumps(fixture['inputs'], indent=2)}
            Expected: {json.dumps(fixture['expected'], indent=2)}

            To update golden fixtures after an INTENTIONAL scoring change, manually
            edit tests/fixtures/scoring_golden.json then commit the diff for review.
            """)
            self.fail(msg)

    test_method.__name__ = f"test_replay_{fixture['_id']}"
    test_method.__doc__ = fixture.get("_description", "")
    return test_method


class ReplayHarness(unittest.TestCase):
    """Dynamically-generated replay tests — one per golden fixture."""


# Inject one test_ method per fixture at class definition time
_fixtures = _load_fixtures()
for _fx in _fixtures:
    _method = _make_test_method(_fx)
    setattr(ReplayHarness, _method.__name__, _method)


# ── meta-tests (harness self-validation) ─────────────────────────────────────

class TestReplayHarnessMeta(unittest.TestCase):
    """Harness infrastructure sanity checks."""

    def test_fixture_file_exists(self):
        self.assertTrue(_FIXTURE_PATH.exists(), f"Fixture file not found: {_FIXTURE_PATH}")

    def test_fixture_file_parses(self):
        fixtures = _load_fixtures()
        self.assertIsInstance(fixtures, list)
        self.assertGreater(len(fixtures), 0, "Fixture file is empty")

    def test_all_fixtures_have_required_keys(self):
        fixtures = _load_fixtures()
        required = {"_id", "fn", "inputs", "expected"}
        for fx in fixtures:
            missing = required - set(fx.keys())
            self.assertEqual(set(), missing, f"Fixture {fx.get('_id', '??')} missing keys: {missing}")

    def test_all_fixture_ids_unique(self):
        fixtures = _load_fixtures()
        ids = [fx["_id"] for fx in fixtures]
        self.assertEqual(len(ids), len(set(ids)), f"Duplicate fixture IDs: {[x for x in ids if ids.count(x) > 1]}")

    def test_all_fixture_fns_known(self):
        fixtures = _load_fixtures()
        for fx in fixtures:
            self.assertIn(
                fx["fn"],
                _FN_MAP,
                f"Fixture {fx['_id']}: unknown fn {fx['fn']!r}. Must be one of {list(_FN_MAP)}",
            )

    def test_fixture_count(self):
        """Fixture count must be >= 12 (current baseline). Add fixtures, never remove."""
        fixtures = _load_fixtures()
        self.assertGreaterEqual(
            len(fixtures),
            12,
            f"Expected >= 12 fixtures, got {len(fixtures)}. Do not remove fixtures.",
        )

    def test_score_insider_returns_tuple(self):
        """_score_insider returns (float, list) — harness depends on this contract."""
        result = _FN_MAP["_score_insider"](
            position_count=1, pnl=0.0, max_position_value=5000.0, total_open_value=5000.0,
            low_buy_hits_14d=3, avg_low_buy_notional_14d=2000.0, market_diversity_14d=1,
            concentration_ratio=0.9, strategy_group="conviction_bet", strategy_confidence=70.0,
            max_trades_60s=1, rapid_burst_trade_ratio=0.0, top_theme_open_share=1.0,
            top_theme_market_count=1, is_fresh_account=True, win_rate=0.0,
            closed_sample_size=0, high_winrate_qualified=False, speculator_score=5.0,
            serious_trade_count_14d=3, median_trade_notional_14d=2000.0, max_trade_notional_14d=5000.0,
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        score, reasons = result
        self.assertIsInstance(score, float)
        self.assertIsInstance(reasons, list)

    def test_score_speculator_returns_tuple(self):
        result = _FN_MAP["_score_speculator"](
            trade_count=10, strategy_group="conviction_bet", strategy_confidence=50.0,
            max_trades_60s=2, rapid_burst_trade_ratio=0.1, rapid_cluster_count=0,
            rapid_cluster_trades=0, median_gap_seconds=300.0,
        )
        self.assertIsInstance(result, tuple)
        score, reasons = result
        self.assertIsInstance(score, float)
        self.assertIsInstance(reasons, list)

    def test_classify_wallet_returns_triple(self):
        result = _FN_MAP["_classify_wallet"](
            insider_score=30.0, speculator_score=20.0, position_count=1,
            pnl=5000.0, max_position_value=3000.0, total_open_value=3000.0,
            low_buy_hits_14d=1, serious_trade_count_14d=1, max_trade_notional_14d=2000.0,
            high_winrate_qualified=False,
        )
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 4)
        classification, matches_criteria, checks, tier_data = result
        self.assertIsInstance(classification, str)
        self.assertIsInstance(matches_criteria, bool)
        self.assertIsInstance(checks, dict)
        self.assertIsInstance(tier_data, dict)

    def test_check_expectations_score_bounds(self):
        """Unit test for _check_expectations scoring path."""
        failures = _check_expectations(
            "test", "_score_insider", (50.0, []), {"score_gte": 40.0, "score_lte": 60.0}
        )
        self.assertEqual(failures, [])

        failures = _check_expectations(
            "test", "_score_insider", (30.0, []), {"score_gte": 40.0}
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("30.00", failures[0])

    def test_check_expectations_classification(self):
        """Unit test for _check_expectations classification path."""
        mock_checks = {"insider_score_gte_threshold": True}
        mock_tier = {"wallet_tier": 3, "wallet_confidence_score": 50.0}
        failures = _check_expectations(
            "test", "_classify_wallet",
            ("confirmed_insider", True, mock_checks, mock_tier),
            {"classification": "confirmed_insider", "matches_criteria": True},
        )
        self.assertEqual(failures, [])

        failures = _check_expectations(
            "test", "_classify_wallet",
            ("low_signal", False, mock_checks, mock_tier),
            {"classification": "confirmed_insider"},
        )
        self.assertEqual(len(failures), 1)
        self.assertIn("low_signal", failures[0])


# ── CLI regeneration helper ───────────────────────────────────────────────────

def _regenerate():
    """Print a report of actual vs expected for each fixture (for manual review).

    Does NOT overwrite the expected fields — that must be a deliberate human edit.
    Run with: PYTHONPATH=. python tests/test_replay_harness.py --regenerate
    """
    fixtures = _load_fixtures()
    print(f"\nReplay harness — regenerate report ({len(fixtures)} fixtures)\n" + "=" * 60)
    all_pass = True
    for fx in fixtures:
        passed, failures, result = _run_fixture(fx)
        fn_name = fx["fn"]
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"\n{status}  {fx['_id']}")
        print(f"  fn: {fn_name}")
        if fn_name in ("_score_insider", "_score_speculator"):
            score, reasons = result
            print(f"  actual score : {score:.4f}")
            print(f"  expected     : {fx['expected']}")
            if reasons:
                print(f"  reasons      : {reasons}")
        elif fn_name == "_classify_wallet":
            classification, matches_criteria, checks = result
            print(f"  actual classification  : {classification!r}")
            print(f"  actual matches_criteria: {matches_criteria}")
            print(f"  expected               : {fx['expected']}")
            failed_c = {k: v for k, v in checks.items() if not v}
            if failed_c:
                print(f"  failed checks: {failed_c}")
        if not passed:
            all_pass = False
            for f in failures:
                print(f"  → {f}")

    print("\n" + "=" * 60)
    print(f"Result: {'ALL PASS' if all_pass else 'FAILURES DETECTED'}")
    print("\nTo update expected bounds after an intentional change:")
    print("  Edit tests/fixtures/scoring_golden.json manually, then commit the diff.")
    return 0 if all_pass else 1


if __name__ == "__main__":
    if "--regenerate" in sys.argv:
        sys.exit(_regenerate())
    unittest.main(verbosity=2)
