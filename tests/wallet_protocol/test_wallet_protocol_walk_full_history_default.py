"""
Failing acceptance tests for Phase A split #1 — A.2 walk_full_history defaults.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md
Acceptance criteria covered: #6, #7

All tests are expected to FAIL on main until the implementation lands.

Failure modes:
- fetch_wallet_data default walk_full_history is False (not True) — assertion fails
- run_wallet_protocol.py has no --bounded-history flag — assertion fails
- fetch_user_activity still has max_total_rows parameter — assertion fails
"""

import inspect
import subprocess
import sys
from pathlib import Path
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# A.2.1 — fetch_wallet_data defaults walk_full_history=True
# ---------------------------------------------------------------------------

class TestFetchWalletDataWalkFullHistoryDefault:
    """Spec AC #6: fetch_wallet_data defaults walk_full_history=True."""

    def test_fetch_wallet_data_walk_full_history_defaults_to_true(self):
        from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
        sig = inspect.signature(fetch_wallet_data)
        assert "walk_full_history" in sig.parameters, (
            "fetch_wallet_data has no walk_full_history parameter"
        )
        default_val = sig.parameters["walk_full_history"].default
        assert default_val is True, (
            f"fetch_wallet_data walk_full_history default is {default_val!r}, expected True"
        )


# ---------------------------------------------------------------------------
# A.2.2 — fetch_user_activity has no max_total_rows parameter
# ---------------------------------------------------------------------------

class TestFetchUserActivityNoMaxTotalRows:
    """Spec AC #7: max_total_rows cap removed from fetch_user_activity."""

    def test_fetch_user_activity_has_no_max_total_rows_parameter(self):
        # There are two fetch_user_activity definitions in polymarket_api.py;
        # only the walk_full_history variant is the hot path. We import from
        # sniper.markets.polymarket_api (the module-level export).
        from sniper.markets.polymarket_api import fetch_user_activity
        sig = inspect.signature(fetch_user_activity)
        assert "max_total_rows" not in sig.parameters, (
            "fetch_user_activity still has max_total_rows parameter — safety cap not removed"
        )

    def test_fetch_user_activity_has_no_row_cap_variants(self):
        from sniper.markets.polymarket_api import fetch_user_activity
        sig = inspect.signature(fetch_user_activity)
        for p in sig.parameters:
            assert "max_total" not in p.lower() and "row_cap" not in p.lower(), (
                f"Unexpected row-cap parameter found on fetch_user_activity: {p!r}"
            )


# ---------------------------------------------------------------------------
# A.2.3 — run_wallet_protocol.py has --bounded-history opt-out flag
# ---------------------------------------------------------------------------

class TestRunWalletProtocolBoundedHistoryFlag:
    """Spec AC #6: --bounded-history opt-out flag exists in run_wallet_protocol.py."""

    def test_bounded_history_flag_exists_in_cli(self):
        """
        --bounded-history must appear when --help is run.
        Currently only --full-history (opt-in) exists.
        """
        result = subprocess.run(
            [sys.executable, "scripts/run_wallet_protocol.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        help_text = result.stdout + result.stderr
        assert "--bounded-history" in help_text, (
            "--bounded-history opt-out flag not found in run_wallet_protocol.py --help output. "
            "Currently only --full-history (opt-in) exists; default must flip to True with "
            "opt-out via --bounded-history."
        )

    def test_full_history_opt_in_flag_removed_or_renamed(self):
        """
        After defaulting walk_full_history=True, the --full-history opt-in flag
        is either removed or the semantics invert to --bounded-history opt-out.
        This test enforces that --full-history is no longer the only entry point.
        Either --bounded-history exists (new) OR --full-history is gone (renamed).
        """
        result = subprocess.run(
            [sys.executable, "scripts/run_wallet_protocol.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            timeout=60,
        )
        help_text = result.stdout + result.stderr
        # At least one of the two conditions must hold:
        # (a) --bounded-history present, or (b) --full-history absent
        has_bounded = "--bounded-history" in help_text
        has_full = "--full-history" in help_text
        assert has_bounded or not has_full, (
            "CLI has --full-history (opt-in) but no --bounded-history (opt-out). "
            "After default flip, the opt-out flag must exist."
        )
