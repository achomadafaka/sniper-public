"""Slice 6 Task 8 — calibration re-baseline against ledger fixtures.

Sibling of `test_calibration_set.py`. The existing v2 harness asserts
the live-API fixture replay; this runner asserts the **ledger** replay
of the same 10 wallets against:

* Axis A — ledger vs live fixture (in-process). Detailed fields.
* Axis B — ledger vs `labels.yaml` `expected.overall_verdict`.

The two axes are separate `pytest` parameterisations so a divergence
on one does not mask the other.

Ledger envelope path convention:
``tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json``

Per D4 / Slice 6 design, ledger envelopes are seeded by exporting from
real wallet warehouses; that capture is operator-supervised work and
sits outside Slice 6. When the envelopes are missing, every Axis A /
Axis B case skips cleanly with `fixture_missing`. This is the documented
expected state until the operator runs the seeding step.

Harness flag mirroring (audit fix #9):
`_apply_calibration_harness_suppression(bundle)` nulls the surfaces
the live harness suppresses with `--no-tags --no-peers --no-exec
--no-history --no-archetype` before splatting into `analyze_wallet`.
"""
from __future__ import annotations

import dataclasses
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest
import yaml

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.fixture_export import (
    load_calibration_fixture,
)
from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
)
from sniper.analysis.wallet_protocol.types import WalletData


REPO_ROOT = Path(__file__).resolve().parents[2]
LABELS_PATH = REPO_ROOT / "tests" / "calibration" / "labels.yaml"
LIVE_FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures"
LEDGER_FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures_ledger"

_HARNESS_SEED = 1
_HARNESS_N_ITER = 2000


# ---------------------------------------------------------------------------
# Harness adapter
# ---------------------------------------------------------------------------


def _apply_calibration_harness_suppression(
    bundle: MaterializedWalletData,
) -> dict[str, Any]:
    """Mirror the live calibration harness flags before splatting.

    Live harness suppresses:
      --no-tags     -> leafs_by_cid=None, nav_lookup=None
      --no-peers    -> peer_baseline_fn=None
      --no-history  -> price_history_fn=None
      --no-exec     -> WalletData.execution_profile=None
      --no-archetype-> WalletData.archetype_layer1=None

    Slice 5's bundle defaults already null peer/leaf/nav and leave
    WalletData.execution_profile / archetype_layer1 as None at materialize
    time. Override `price_history_fn` explicitly because Task 7's loader
    rebuilds it from persisted bars; without the override H6/FH6 would
    run, contradicting `--no-history`.

    The exec / archetype overrides are kept so a future bundle-default
    change cannot silently re-enable a suppressed surface.
    """
    kwargs = bundle.to_analyze_wallet_kwargs()
    kwargs["price_history_fn"] = None       # --no-history
    kwargs["peer_baseline_fn"] = None       # --no-peers
    kwargs["leafs_by_cid"] = None           # --no-tags
    kwargs["nav_lookup"] = None             # --no-tags

    wd: WalletData = kwargs["pre_fetched_data"]
    assert wd.execution_profile is None, (
        "calibration harness expects --no-exec; bundle smuggled in "
        "execution_profile — fix the materializer or the harness "
        "adapter, not the assertion."
    )
    assert wd.archetype_layer1 is None, (
        "calibration harness expects --no-archetype; bundle smuggled "
        "in archetype_layer1 — fix the materializer or the harness "
        "adapter, not the assertion."
    )
    return kwargs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_labels() -> list[dict]:
    payload = yaml.safe_load(LABELS_PATH.read_text())
    return list(payload.get("labels", []))


def _ledger_fixture_path(wallet: str) -> Path:
    return LEDGER_FIXTURE_DIR / f"{wallet.lower()}_2026-04-30.json"


def _live_fixture_path(wallet: str) -> Path:
    return LIVE_FIXTURE_DIR / f"{wallet.lower()}_2026-04-30.json"


def _run_protocol_from_live_fixture(wallet: str, fixture: Path) -> dict:
    """Mirror `tests/calibration/test_calibration_set.py`'s helper."""
    import scripts.run_wallet_protocol as mod

    argv = [
        wallet,
        "--from-fixture",
        str(fixture),
        "--no-tags",
        "--no-peers",
        "--no-exec",
        "--no-history",
        "--no-archetype",
        "--seed",
        str(_HARNESS_SEED),
        "--n-iter",
        str(_HARNESS_N_ITER),
        "--json",
        "--no-persist",
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(
            f"run_wallet_protocol returned {rc} for {wallet}"
        )
    return json.loads(buf.getvalue().strip())


def _run_protocol_from_ledger_envelope(
    wallet: str, envelope: Path
) -> dict:
    bundle = load_calibration_fixture(envelope)
    kwargs = _apply_calibration_harness_suppression(bundle)
    report = analyze_wallet(
        bundle.wallet_data.wallet,
        seed=_HARNESS_SEED,
        n_iter=_HARNESS_N_ITER,
        **kwargs,
    )
    return dataclasses.asdict(report)


_LABELS = _load_labels()


# ---------------------------------------------------------------------------
# Harness-adapter unit test (deterministic; no fixtures needed)
# ---------------------------------------------------------------------------


def test_calibration_harness_adapter_suppresses_history_surface() -> None:
    from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow

    def _phf_marker(token_id: str, ts: int) -> float | None:
        return 0.5

    wallet_data = WalletData(
        wallet="0xa",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=0,
    )
    bundle = MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=_phf_marker,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=AnalysisWindow(
            window_id="w1",
            capture_run_ids=("run-a",),
            start_ts=0,
            end_ts=86400,
            source_versions=(),
        ),
    )

    kwargs = _apply_calibration_harness_suppression(bundle)
    assert kwargs["price_history_fn"] is None
    assert kwargs["peer_baseline_fn"] is None
    assert kwargs["leafs_by_cid"] is None
    assert kwargs["nav_lookup"] is None


def test_calibration_harness_adapter_rejects_smuggled_execution_profile() -> None:
    from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow

    wallet_data = WalletData(
        wallet="0xa",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile={"slip_bps_p50": 12.0},  # smuggled
        fetched_at=0,
    )
    bundle = MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=None,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=AnalysisWindow(
            window_id="w1",
            capture_run_ids=("run-a",),
            start_ts=0,
            end_ts=86400,
            source_versions=(),
        ),
    )
    with pytest.raises(AssertionError):
        _apply_calibration_harness_suppression(bundle)


def test_calibration_harness_adapter_rejects_smuggled_archetype() -> None:
    from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow

    wallet_data = WalletData(
        wallet="0xa",
        closed_positions=[],
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1="paired_outcome_arb",  # smuggled
        execution_profile=None,
        fetched_at=0,
    )
    bundle = MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=None,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=AnalysisWindow(
            window_id="w1",
            capture_run_ids=("run-a",),
            start_ts=0,
            end_ts=86400,
            source_versions=(),
        ),
    )
    with pytest.raises(AssertionError):
        _apply_calibration_harness_suppression(bundle)


# ---------------------------------------------------------------------------
# Axis A — ledger vs live (detailed-field comparison)
# ---------------------------------------------------------------------------


_AXIS_A_DETAIL_FIELDS = (
    "gate_result",
    "n_closed_positions",
    "trading_mode",
)


@pytest.mark.parametrize(
    "label",
    _LABELS,
    ids=lambda lab: lab["wallet"][:10],
)
def test_axis_a_ledger_matches_live_fixture(label: dict) -> None:
    wallet = label["wallet"]
    ledger_path = _ledger_fixture_path(wallet)
    live_path = _live_fixture_path(wallet)
    if not ledger_path.exists():
        pytest.skip(
            f"fixture_missing: {ledger_path.name} — operator must seed "
            f"the ledger warehouse for {wallet} and run "
            f"export_calibration_fixture before this assertion runs."
        )
    if not live_path.exists():
        pytest.skip(f"fixture_missing: {live_path.name}")

    live_report = _run_protocol_from_live_fixture(wallet, live_path)
    ledger_report = _run_protocol_from_ledger_envelope(wallet, ledger_path)

    for fld in _AXIS_A_DETAIL_FIELDS:
        assert ledger_report.get(fld) == live_report.get(fld), (
            f"{wallet}: Axis A divergence on {fld}: "
            f"ledger={ledger_report.get(fld)!r} live={live_report.get(fld)!r}"
        )

    # Held-analyst branch activity match — flagged separately because it
    # is a nested dict.
    ledger_branch = (ledger_report.get("held_analyst_branch") or {}).get(
        "is_active"
    )
    live_branch = (live_report.get("held_analyst_branch") or {}).get(
        "is_active"
    )
    assert ledger_branch == live_branch, (
        f"{wallet}: held_analyst_branch.is_active diverged: "
        f"ledger={ledger_branch!r} live={live_branch!r}"
    )


# ---------------------------------------------------------------------------
# Axis B — ledger vs labels.yaml expected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label",
    _LABELS,
    ids=lambda lab: lab["wallet"][:10],
)
def test_axis_b_ledger_matches_labels_yaml(label: dict) -> None:
    wallet = label["wallet"]
    ledger_path = _ledger_fixture_path(wallet)
    if not ledger_path.exists():
        pytest.skip(
            f"fixture_missing: {ledger_path.name} — operator must seed "
            f"the ledger warehouse for {wallet} and run "
            f"export_calibration_fixture before this assertion runs."
        )

    if label["label_type"] != "exact":
        pytest.skip("Axis B only runs against label_type=exact rows")

    expected = label["expected"]
    ledger_report = _run_protocol_from_ledger_envelope(wallet, ledger_path)
    actual = ledger_report.get("overall_verdict")
    assert actual == expected, (
        f"{wallet}: Axis B divergence — labels.yaml expected={expected!r} "
        f"ledger={actual!r}. Investigate before silently updating "
        f"labels.yaml; log the divergence in "
        f"tests/calibration/fixtures_ledger/divergences.md."
    )


# ---------------------------------------------------------------------------
# Wiring sanity
# ---------------------------------------------------------------------------


def test_ten_calibration_wallets_are_loaded() -> None:
    assert len(_LABELS) == 10
