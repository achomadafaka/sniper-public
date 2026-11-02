"""The refresh runner's statistics. Every one of these fails quietly if wrong.

A binomial tail that is subtly off still returns a probability; a BH implementation that rejects
too much still returns a roster. The gate's whole purpose is to admit ~5% false positives instead
of the sealed rule's 58%, and nothing downstream would notice if it admitted 58% anyway.
"""
import importlib.util
import pathlib

import pytest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research"
          / "golden_roster_refresh_v2.py")
_spec = importlib.util.spec_from_file_location("golden_refresh", SCRIPT)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)

BASE = 0.186          # the forward base rate the pre-registration measured


def test_the_sealed_rules_chance_pass_rates_reproduce():
    """The numbers that justify replacing the sealed rule. 2-in-3 at the forward base rate clears
    by chance 9.1% of the time and 2-in-5 clears 23.5% — that is why a 16,000-candidate pool turns
    it into a coin flip. If these drift, the case for the BH gate drifts with them."""
    assert g.binom_tail(2, 3, BASE) == pytest.approx(0.091, abs=0.002)
    assert g.binom_tail(2, 5, BASE) == pytest.approx(0.235, abs=0.002)
    # and the contrast that makes BH safe without a coin minimum
    assert g.binom_tail(3, 3, BASE) == pytest.approx(0.0064, abs=0.0005)


def test_the_tail_survives_a_wallet_seen_in_thousands_of_coins():
    """REGRESSION. A factorial-based implementation raises OverflowError here, and these are the
    wallets the gate has the MOST evidence about — it failed on the rows that matter most."""
    assert 0.0 <= g.binom_tail(400, 4000, BASE) <= 1.0
    assert g.binom_tail(4000, 4000, BASE) == pytest.approx(0.0, abs=1e-12)


def test_both_tails_are_consistent_with_each_other():
    for n, k in ((10, 3), (25, 5), (7, 7), (7, 0)):
        upper = g.binom_tail(k, n, BASE, upper=True)
        lower = g.binom_tail(k - 1, n, BASE, upper=False)
        assert upper + lower == pytest.approx(1.0, abs=1e-9), (n, k)


def test_bh_rejects_the_textbook_set_and_not_more():
    """Classic worked example: p = .01 .02 .03 .04 .05 at q=.05 rejects the first four."""
    ps = [(p, i) for i, p in enumerate([0.01, 0.02, 0.03, 0.04, 0.05])]
    assert g.benjamini_hochberg(ps, 0.05) == {0, 1, 2, 3, 4}
    ps = [(p, i) for i, p in enumerate([0.01, 0.20, 0.30, 0.40, 0.50])]
    assert g.benjamini_hochberg(ps, 0.05) == {0}


def test_bh_is_not_just_a_threshold():
    """The property that distinguishes it: a p-value below q can still be REJECTED-as-null when
    enough others are worse, and one below q survives on rank. A naive `p < q` filter would admit
    the same set regardless of how many tests ran, which is the sealed rule's whole failure."""
    few = [(0.04, "a")]
    many = [(0.04, "a")] + [(0.9, i) for i in range(100)]
    assert g.benjamini_hochberg(few, 0.05) == {0}
    assert g.benjamini_hochberg(many, 0.05) == set(), "multiplicity must bite as tests grow"


def test_holm_is_strictly_more_conservative_than_bh():
    ps = [(p, i) for i, p in enumerate([0.001, 0.02, 0.03, 0.049, 0.2])]
    assert g.holm(ps, 0.05) <= g.benjamini_hochberg(ps, 0.05)


def test_the_output_is_never_the_sealed_roster():
    """Every live lane threshold was calibrated on the 290-wallet v1 population, so overwriting it
    changes what those thresholds mean underneath them."""
    assert g.V2 != g.V1
    assert g.V1.name == "runner_golden_wallets.json"
    assert "v2" in g.V2.name


def test_the_registered_constants_match_the_preregistration():
    assert g.ADMIT_Q == 0.05
    assert g.DEMOTE_Q == 0.10
    assert g.DEMOTE_MIN_COINS == 5
    assert g.CANDIDATE_MIN_COINS == 3
    assert g.WIN_MCAP == 40_000.0


def test_the_weekly_refresh_runs_with_the_archived_research_manifest_present(tmp_path):
    """Research archived 2026-10-02: data/research_active.json stays as data (state=stopped), and
    fresh_recordings.legacy_reader_guard() exited every caller while that file existed. The weekly
    golden roster cron (Mon 05:30) would have exited on 10-05 doing nothing."""
    import json
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "research_active.json").write_text(json.dumps({"id": "fresh-x", "state": "stopped"}))
    r = subprocess.run([sys.executable, str(root / "scripts" / "research" / "golden_roster_refresh_v2.py"),
                        "--diagnostic-only", "--help"],
                       capture_output=True, text=True, timeout=60,
                       env={"PATH": "/usr/bin:/bin", "SNIPER_STATE_ROOT": str(tmp_path), "PYTHONPATH": str(root)})
    assert r.returncode == 0 and "--write" in r.stdout, r.stderr[-400:]
    for script in ("scripts/research/golden_roster_refresh_v2.py",
                   "scripts/memecoin_scalp_probe.py", "scripts/research/refusal_family_read1.py"):
        text = (root / script).read_text()
        assert "legacy_reader_guard" not in text and "active_manifest" not in text, script


@pytest.mark.parametrize("script", ["scripts/research/golden_roster_refresh_v2.py", "scripts/memecoin_scalp_probe.py"])
def test_a_bare_launch_publishes_nothing(tmp_path, script):
    # Codex W11: both escaped the publication quarantine; a bare launch reached the roster write/print.
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / script), "--write", "--shadow"], capture_output=True, text=True,
                       timeout=60, env={"PATH": "/usr/bin:/bin", "SNIPER_STATE_ROOT": str(tmp_path),
                                        "PYTHONPATH": str(root)})
    assert r.returncode != 0 and "Research blocked" in r.stderr and r.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_the_v1_golden_publisher_is_archived():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert not (root / "scripts" / "research" / "refresh_golden_wallets.py").exists()
