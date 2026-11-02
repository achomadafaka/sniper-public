"""End-to-end algorithm-discovery run on a committed real-shape fixture.

Integration-marked (opt-in): stage_protocol shells out to run_wallet_protocol,
which is heavy. Run with: pytest -m integration tests/algo_discovery/test_e2e.py

Methodology / observed result documented inline.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sniper.analysis.algo_discovery.pipeline import run_discovery

REPO = Path(__file__).resolve().parents[2]
WALLET = "0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf"
ACTIVATION_FIXTURE = (
    REPO / "tests" / "calibration" / "fixtures"
    / "0xf67f72df65797a85dc9fd44d57adfe5b7b4a31cf_2026-05-27.activation.json"
)


@pytest.mark.integration
def test_e2e_full_chain_runs_and_routes(tmp_path):
    """The v2 chain (fetch→protocol→dossier→classify→rule→route) runs on a
    real-shape fixture WITH redemptions and returns a coherent route + all
    stage artifacts, without crashing. Backtest runs as an optional diagnostic.

    Observed 2026-05-29: this wallet classifies `algorithmic / rebuild_rule`
    (bursty, ~13s between trades, 24/24h coverage; only variable sizing
    dissents → confidence ~0.5). The OOS backtest diagnostic shows 22/22 wins
    at avg entry ~0.98 — i.e. an edgeless favorite-buyer by the price-implied
    null — which is exactly why v2 routes on BEHAVIOR and proves edge via
    forward paper, not via the backtest. (Route not hard-pinned to avoid
    n_iter/fixture brittleness; the point is the chain runs and routes.)
    """
    assert ACTIVATION_FIXTURE.exists(), ACTIVATION_FIXTURE
    fixture = json.loads(ACTIVATION_FIXTURE.read_text())
    res = run_discovery(WALLET, run_root=tmp_path, from_fixture=fixture, n_iter=200, run_backtest=True)

    assert res["route"] in {"copy_mirror", "rebuild_rule", "human_review"}
    assert "classification" in res and "routing" in res
    rd = Path(res["run_dir"])
    for art in ("01_wallet_data", "02_protocol_report", "03_dossier",
                "04_classification", "05_wallet_rule", "06_route"):
        assert (rd / f"{art}.json").exists(), f"missing artifact {art}"
