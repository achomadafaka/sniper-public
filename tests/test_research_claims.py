"""Regressions for published research claims.

Every case here is a claim that appeared in an operator-facing audit document and was wrong.
The tests are offline: a committed CSV fixture, the document text itself, and closed-form
arithmetic. Nothing here reads a production database, a provider or the network.

Ported from the 2026-09-23 Codex engineering review and its recheck
(`output/research/engineering-recheck-20260923/`), which cited a `test_research_claims.py`
that only ever existed in the untracked audit output directory.
"""
import csv
import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs" / "audits"
FROZEN_CSV = REPO / "tests" / "fixtures" / "research" / "2026-09-23-stop-overshoot-frozen.csv"
FROZEN_SHA256 = "dff79eea11ebbabc464516b996d6156f24468111ef0cdc514c334fced3c20d53"

# The stage columns are percentage points of entry price. The published dollars are those
# points weighted by each row's own entry notional, and the confusion between the two is C4.
STAGES = ("g_observe", "g_debounce", "g_decision_to_fill")


def _frozen_rows():
    assert hashlib.sha256(FROZEN_CSV.read_bytes()).hexdigest() == FROZEN_SHA256, (
        "the frozen stop-overshoot CSV changed; the published dollars below describe the "
        "artifact with the recorded hash and cannot be checked against a different one"
    )
    with FROZEN_CSV.open() as fh:
        return list(csv.DictReader(fh))


def _usd(row, field):
    """Percentage points of entry price -> dollars on that row's own entry notional."""
    return float(row[field]) * float(row["entry_notional"]) / 100 if row[field] else None


def test_frozen_csv_reproduces_the_published_overshoot_dollars():
    """C4: the CSV was never overwritten; the amendment summed percentage points as dollars."""
    rows = _frozen_rows()
    priced = {f: [r for r in rows if r[f]] for f in ("g_observe", "g_debounce", "overshoot")}
    usd = {f: sum(_usd(r, f) for r in rs) for f, rs in priced.items()}
    raw = {f: sum(float(r[f]) for r in rs) for f, rs in priced.items()}

    assert [len(priced[f]) for f in ("g_observe", "g_debounce", "overshoot")] == [207, 207, 286]
    assert usd == pytest.approx(
        {"g_observe": -1162.062851, "g_debounce": -439.951160, "overshoot": -1785.652822},
        abs=0.01,
    )
    # The figures the 16:52Z amendment published as "the CSV, re-read" are these, unweighted.
    assert raw == pytest.approx(
        {"g_observe": -1062.760023, "g_debounce": -545.272584, "overshoot": -2002.897239},
        abs=0.01,
    )


def test_overshoot_decomposition_is_an_identity_on_its_common_sample():
    """C4: (0) + (a) + (b+c) == overshoot, exactly, on every row carrying all three legs."""
    rows = _frozen_rows()
    common = [r for r in rows if all(r[f] for f in STAGES) and r["overshoot"]]
    assert len(common) == 207
    for row in common:
        assert sum(_usd(row, f) for f in STAGES) == pytest.approx(_usd(row, "overshoot"), abs=1e-6)

    total = sum(_usd(r, "overshoot") for r in common)
    assert total == pytest.approx(-1350.315819, abs=0.01)
    pre_executor = sum(_usd(r, "g_observe") + _usd(r, "g_debounce") for r in common)
    # Above 100%, not the published 90%: (b+c) is a positive offset inside the same total.
    assert 100 * pre_executor / total == pytest.approx(118.6, abs=0.1)


def test_overshoot_report_publishes_the_unit_correct_totals():
    """C4: the correction must reach the headline, not only the appendix."""
    text = (DOCS / "2026-09-23-stop-overshoot-decomposition.md").read_text()
    assert FROZEN_SHA256 in text, "the dollar figures must name the artifact they came from"
    for figure in ("-$1,350.32", "118.6%"):
        assert figure in text, f"{figure} is the corrected common-sample reading and must be published"


def test_overshoot_report_supersedes_its_bad_quote_guidance_at_the_top():
    """C5: a withdrawal below the conclusion it withdraws does not reach the reader."""
    text = (DOCS / "2026-09-23-stop-overshoot-decomposition.md").read_text()
    banner = text.find("SUPERSEDED")
    guidance = text.lower().find("those are phantom triggers")
    assert banner != -1, "the report needs a superseded banner"
    assert guidance != -1, "the guidance this supersedes must still be findable"
    assert banner < guidance, "the banner must sit above the phantom-trigger guidance, not below it"


def test_impact_gate_report_labels_its_tables_as_a_roundtrip_proxy():
    """C3: the opening conclusion must carry the qualification, not only the appendix."""
    text = (DOCS / "2026-09-23-impact-gate-replay-vs-funded-fills.md").read_text()
    answer = text.split("## The answer")[0]
    assert "roundtrip-cost proxy" in answer, "the proxy label belongs above the conclusion it limits"
    assert "unconditional" not in text.split("CORRECTION")[0], (
        "the withdrawn universal-bound wording must not stand in the body"
    )


def test_third_sweep_table_matches_its_declared_population():
    """C2: the outcome rows are disjoint, so they must sum to the cohort they are drawn from."""
    text = (DOCS / "2026-09-23-third-stop-sweep.md").read_text()
    declared = int(re.search(r"with a 3rd mark.*?\*\*(\d+)\*\*", text).group(1))
    section = text.split("## What each sweep does")[1].split("## The 26")[0]
    counts = [int(s) for s in re.findall(r"\|\s*\*{0,2}(\d+)\*{0,2}\s*\|", section)]
    assert sum(counts) == declared, (
        f"{counts} sums to {sum(counts)} against a declared {declared}-row population"
    )


def test_half_roundtrip_is_not_a_universal_lower_bound_on_entry_impact():
    """C3: the symmetric derivation does not survive an asymmetric route cost.

    Same initial constant-product state for both legs, no buy-side fee, 10% sell-side
    transfer charge. The published amendment claims half the roundtrip is always at or below
    one-way entry impact; here it is 5.8x it.
    """
    reserves, clip, sell_charge = 1000.0, 10.0, 0.10
    acquired = reserves * clip / (reserves + clip)
    delivered = acquired * (1 - sell_charge)
    reverse = reserves * delivered / (reserves + delivered)

    entry_impact_pct = 100 * (clip / acquired - 1)
    half_roundtrip_pct = 100 * (1 - reverse / clip) / 2

    assert entry_impact_pct == pytest.approx(1.0, abs=0.001)
    assert half_roundtrip_pct == pytest.approx(5.839058, abs=0.001)
    assert half_roundtrip_pct > entry_impact_pct, (
        "an asymmetric sell-side charge breaks the lower-bound claim; with a symmetric "
        "fee-free pool the inequality runs the other way, which is why it read as universal"
    )


def test_half_roundtrip_is_a_lower_bound_only_under_the_symmetric_assumptions():
    """C3: the symmetric case the amendment derived is still correct, and only that case."""
    reserves = 1000.0
    for clip in (2.0, 10.0, 50.0, 100.0):
        acquired = reserves * clip / (reserves + clip)
        reverse = reserves * acquired / (reserves + acquired)
        entry_impact_pct = 100 * (clip / acquired - 1)
        half_roundtrip_pct = 100 * (1 - reverse / clip) / 2
        assert half_roundtrip_pct < entry_impact_pct


def test_a_median_of_differences_is_not_a_difference_of_medians():
    """N16: the quantity the wallet report subtracted does not distribute that way."""
    observer = [10.0, 20.0, 100.0]
    advantage = [1.0, 19.0, 50.0]
    difference_of_medians = sorted(observer)[1] - sorted(advantage)[1]
    median_of_differences = sorted(a - b for a, b in zip(observer, advantage))[1]
    assert difference_of_medians == 1.0
    assert median_of_differences == 9.0


def test_wallet_report_withdraws_the_subtracted_latency_headline():
    """N16: the 1.22 s figure must not stand as a measured chain-relative median."""
    text = (DOCS / "2026-09-23-wallet-candidate-scan.md").read_text()
    section = text.split("## H. Feed latency")[1].split("\n---")[0]
    assert "WITHDRAWN" in section, "the J7 latency headline is withdrawn, not merely footnoted"
    assert "1.22" not in section.split("WITHDRAWN")[0], (
        "the withdrawn figure must not appear above its own withdrawal"
    )
