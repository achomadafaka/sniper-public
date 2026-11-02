"""W6.4 — calibration harness (v3 — post-fetch-rebuild replay baseline).

v3 reframe (2026-05-13): re-baselined against the completed Phase A fetch
rebuild (PRs #35/#36/#37). The v2 fixtures captured truncated wallet state
(client-side pagination caps + ASC sort dropped the most recent positions);
v3 fixtures are recaptured with the new fetcher: DESC sort, no client caps,
walk_full_history=True default. Hypothesis-test verdicts may differ from v2
because the input population is now complete — that's correctness, not
regression.

This set remains a deterministic protocol-replay regression baseline for
the captured fixture surface, NOT an operator-prior agreement panel.
A failing assertion means the protocol code or the fixture changed since
the v3 baseline was written. Either is a regression worth investigating.

Match semantics (v3, unchanged from v2):

* Every label is ``label_type: exact`` with an ``expected`` overall_verdict
  matching what the protocol returns for the captured fixture under the
  calibration harness flags.
* ``operator_prior`` (nested) preserves the W6.3 reputational reading for
  audit context. The harness does NOT compare against the prior.

A wallet whose fixture is missing skips with reason ``fixture_missing``.

Fixture path convention (v3):
    tests/calibration/fixtures/<wallet>_2026-05-13.json

Plan ref: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §29-32.
v2→v3 rationale: every analyzer run on a high-volume wallet pre-Phase-A
operated on the oldest 5,000 closed positions — the most-recent ~5 months
were invisible. v3 baselines reflect the analyzer seeing complete data.
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from sniper.analysis.strategy_pattern_audit import compute_strategy_pattern_audit
from sniper.analysis.wallet_protocol.types import ProtocolReport

# Calibration replays the full protocol incl. flipper permutation work
# (n_iter=2000). Largest fixtures (0x6a72f618, 0x22e4248b, 0x1cc16713)
# clock ~90s per case — the global 60s timeout from pytest.ini is too
# tight here. Module-level mark lifts the ceiling to 240s for this file
# only, leaving the rest of the suite under the strict default budget.
pytestmark = pytest.mark.timeout(240)

REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = REPO_ROOT / "tests" / "calibration" / "labels.yaml"
FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures"

# yaml is part of pytest's transitive deps via ruamel/PyYAML; if missing,
# the harness file fails import-time, which is the right signal.
import yaml

# Allowed operator_prior label_type values (preserved from v1 vocabulary).
_PRIOR_LABEL_TYPES = ("exact", "band")


def _load_payload() -> dict:
    return yaml.safe_load(LABELS_PATH.read_text())


def _load_labels() -> list[dict]:
    payload = _load_payload()
    return list(payload.get("labels", []))


def _fixture_path(wallet: str) -> Path:
    return FIXTURE_DIR / f"{wallet.lower()}_2026-05-13.json"


def _run_protocol_from_fixture(wallet: str, fixture: Path) -> dict:
    """Invoke run_wallet_protocol.main() in fixture mode and return the
    parsed JSON ProtocolReport.
    """
    from contextlib import redirect_stdout
    import scripts.run_wallet_protocol as mod
    argv = [
        wallet,
        "--from-fixture", str(fixture),
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
        "--seed", "1", "--n-iter", "2000",
        "--json", "--no-persist",
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(f"run_wallet_protocol returned {rc} for {wallet}")
    text = buf.getvalue().strip()
    return json.loads(text)


_LABELS = _load_labels()
_ACTIVITY_SURFACE_WALLETS = {
    "0x1cc16713196d456f86fa9c7387dd326a7f73b8df",
    "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
    "0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
    "0x5bffcf561bcae83af680ad600cb99f1184d6ffbe",
}


def _assert_activity_surfaces(report: dict, n_activity: int) -> None:
    assert report["activity_rows_fetched"] == n_activity
    assert report["data_completeness"]["activity_capture"][
        "activity_rows_fetched"
    ] == n_activity
    position = report["position_building_surface"]
    assert position["generated_from"]["activity_rows"] == n_activity
    assert position["wallet_summary"]["n_positions"] > 0
    assert position["wallet_summary"]["n_reconstructed_positions"] > 0
    assert position["wallet_summary"]["observation_window_coverage"] > 0

    protocol_report = ProtocolReport(
        wallet=report["wallet"],
        overall_verdict=report["overall_verdict"],
        gate_result=report["gate_result"],
        trading_mode=report["trading_mode"],
        n_closed_positions=report["n_closed_positions"],
        archetype_layer1=report.get("archetype_layer1"),
    )
    for field in [
        "outcome_side_breakdown",
        "exit_attribution",
        "data_completeness",
        "timing_surface",
        "position_building_surface",
        "extractor_features_summary",
        "execution_quality_summary",
    ]:
        setattr(protocol_report, field, report.get(field))

    audit = compute_strategy_pattern_audit([protocol_report])
    assert audit is not None
    assert audit["generated_from"]["n_reports"] == 1
    quality = audit["population_quality"]["surface_quality_distribution"]
    assert quality["position_building_surface"]
    assert quality["data_completeness"]


@pytest.mark.parametrize(
    "label",
    _LABELS,
    ids=lambda lab: lab["wallet"][:10],
)
def test_calibration_label(label):
    """v2 baseline assertion: protocol replay must match expected verdict."""
    wallet = label["wallet"]
    fixture = _fixture_path(wallet)
    if not fixture.exists():
        pytest.skip(f"fixture_missing: {fixture.name}")

    report = _run_protocol_from_fixture(wallet, fixture)
    actual_verdict = report.get("overall_verdict")

    label_type = label["label_type"]
    if label_type != "exact":
        pytest.fail(
            f"{wallet}: v2 calibration set requires label_type=exact "
            f"(deterministic baseline); got {label_type!r}"
        )
    expected = label["expected"]
    assert actual_verdict == expected, (
        f"{wallet}: expected={expected!r} actual={actual_verdict!r} — "
        f"protocol or fixture changed since v2 baseline. Investigate before "
        f"updating expected. See docs/audits/2026-05-01-calibration-result-mismatch.md."
    )
    if wallet in _ACTIVITY_SURFACE_WALLETS:
        payload = json.loads(fixture.read_text())
        n_activity = len(payload.get("activity_rows") or [])
        assert n_activity > 0
        _assert_activity_surfaces(report, n_activity)


def test_calibration_version_is_v3():
    """v3 re-baseline locked in (post-Phase-A fetch rebuild)."""
    payload = _load_payload()
    assert payload.get("calibration_version") == "3", (
        f"calibration_version={payload.get('calibration_version')!r} != '3'"
    )


def test_label_count_is_ten():
    """The calibration set is fixed at 10 wallets per the plan; growing
    or shrinking it requires a separate plan + audit."""
    assert len(_LABELS) == 10, f"calibration set size {len(_LABELS)} != 10"


def test_all_labels_are_exact():
    """v2: every label is the deterministic baseline (exact); bands are
    not part of the regression contract."""
    for lab in _LABELS:
        assert lab["label_type"] == "exact", (
            f"{lab['wallet']}: v2 requires label_type='exact'; got "
            f"{lab['label_type']!r}"
        )


def test_each_label_has_rationale():
    """Every wallet must explain the deterministic verdict (which
    hypotheses fired and why)."""
    for lab in _LABELS:
        assert lab.get("rationale", "").strip(), (
            f"{lab['wallet']}: missing rationale"
        )


def test_each_label_has_at_least_one_evidence():
    """Evidence must point to the reproducing fixture and report at
    minimum, so a future regression has a paper trail to follow."""
    for lab in _LABELS:
        ev = lab.get("evidence") or []
        assert len(ev) >= 1, f"{lab['wallet']}: no evidence sources"


def test_each_label_evidence_cites_fixture(tmp_path_factory):
    """Every label must point at its captured fixture as evidence — that
    fixture is the deterministic input the baseline depends on."""
    for lab in _LABELS:
        sources = [ev.get("source", "") for ev in (lab.get("evidence") or [])]
        wallet = lab["wallet"]
        expected_path = (
            f"tests/calibration/fixtures/{wallet.lower()}_2026-05-13.json"
        )
        assert any(expected_path in s for s in sources), (
            f"{wallet}: evidence must cite {expected_path!r}; got {sources!r}"
        )


def test_exact_labels_have_expected_field():
    for lab in _LABELS:
        if lab["label_type"] == "exact":
            assert lab.get("expected"), f"{lab['wallet']}: exact missing 'expected'"


def test_each_label_preserves_operator_prior():
    """v2 reframe rule: preserve every operator prior so the human read is
    not erased. Each label must carry an `operator_prior` block with its
    label_type (exact|band), the matching value field, and a rationale."""
    for lab in _LABELS:
        wallet = lab["wallet"]
        prior = lab.get("operator_prior")
        assert isinstance(prior, dict), (
            f"{wallet}: missing operator_prior block (v2 requires this — "
            f"see docs/runbooks/calibration-labeling-protocol.md)"
        )
        assert prior.get("label_type") in _PRIOR_LABEL_TYPES, (
            f"{wallet}: operator_prior.label_type must be one of "
            f"{_PRIOR_LABEL_TYPES}; got {prior.get('label_type')!r}"
        )
        if prior["label_type"] == "exact":
            assert prior.get("expected"), (
                f"{wallet}: operator_prior.label_type='exact' requires "
                f"'expected' field"
            )
        else:
            band = prior.get("accepted_band") or []
            assert band, (
                f"{wallet}: operator_prior.label_type='band' requires "
                f"non-empty 'accepted_band'"
            )
        assert (prior.get("rationale") or "").strip(), (
            f"{wallet}: operator_prior must include a non-empty rationale"
        )
