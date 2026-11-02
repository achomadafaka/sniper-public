"""Behavioural tests for `selectRealisticCohort`, the shipped cohort picker in app.js.

WHAT WENT AND WHY (2026-09-22). This file used to be the Launch-radar UI contract: 30 tests
asserting the radar's panels, sweep animation, dot keying, live region and result copy. The
operator archived that surface and the sub-tab, its markup, its CSS and its render code were
removed, so those tests asserted against code that no longer exists — 26 failed outright and
three more ("does not show control results", "removes the old insight copy", "results panel does
not show progress") passed VACUOUSLY, which is worse: a green test proving nothing.

`selectRealisticCohort` was NOT removed. It still ships in app.js and still decides which
realistic cohort a reader is shown, so its four behavioural tests stay, and they still execute
the real shipped helper under node rather than a copy.
"""
import json
import shutil
import subprocess
from pathlib import Path


APP = Path("static/app.js").read_text()


def _run_cohort_picker(cases):
    """Run the `selectRealisticCohort` function from app.js with Node.js."""
    node = shutil.which("node")
    # HARD FAIL, never skip. These are the only behavioural tests for
    # `selectRealisticCohort`. A skip on a node-less box would report green while
    # cohort selection went completely untested, which is the failure mode these
    # tests exist to prevent. (`tests/test_world_map.py` also runs shipped JavaScript
    # under node, so this is not the suite's only JavaScript execution.)
    assert node is not None, (
        "node is required: it executes the shipped selectRealisticCohort helper, "
        "the only behavioural test for selectRealisticCohort"
    )

    start = APP.index("  const REALISTIC_COHORT_PRIORITY")
    end = APP.index("  function esc(v) {")
    helper = APP[start:end]
    assert "function selectRealisticCohort" in helper

    script = helper + "\nconsole.log(JSON.stringify(%s.map(selectRealisticCohort)));" % (
        json.dumps(cases)
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(out.stdout.strip())


V2 = {"activated_at": "2026-08-07T06:25:27Z"}
V3_LATER = {"activated_at": "2026-08-20T10:00:00Z"}
OFF = {"activated_at": None}
SAME = "2026-08-07T06:25:27Z"


def test_cohort_picker_executes_and_ignores_key_order():
    forward = {"radar-realistic-v2": V2, "radar-realistic-v3": V3_LATER, "radar-adaptive-v3": OFF}
    reversed_order = dict(reversed(list(forward.items())))
    assert _run_cohort_picker([forward, reversed_order]) == [
        "radar-realistic-v3",
        "radar-realistic-v3",
    ]


def test_cohort_picker_breaks_an_equal_activation_tie_toward_v3():
    """A shared activation time must select the v3 cohort."""
    tie = {
        "radar-realistic-v2": {"activated_at": SAME},
        "radar-realistic-v3": {"activated_at": SAME},
    }
    assert _run_cohort_picker([tie, dict(reversed(list(tie.items())))]) == [
        "radar-realistic-v3",
        "radar-realistic-v3",
    ]


def test_cohort_picker_never_returns_the_adaptive_cohort():
    """Adaptive rows have no `target_first` field and cannot use this panel."""
    adaptive_newest = {
        "radar-realistic-v2": V2,
        "radar-adaptive-v3": {"activated_at": "2027-01-01T00:00:00Z"},
    }
    only_adaptive = {"radar-adaptive-v3": {"activated_at": "2027-01-01T00:00:00Z"}}
    assert _run_cohort_picker([adaptive_newest, only_adaptive]) == [
        "radar-realistic-v2",
        "",
    ]


def test_cohort_picker_survives_malformed_empty_and_unactivated_input():
    cases = [
        {},
        {"radar-realistic-v2": OFF, "radar-adaptive-v3": OFF},
        {"radar-realistic-v2": {"activated_at": "not-a-date"}, "radar-realistic-v3": V3_LATER},
        {"radar-realistic-v2": None},
    ]
    assert _run_cohort_picker(cases) == [
        "",
        "radar-realistic-v2",
        "radar-realistic-v3",
        "",
    ]
