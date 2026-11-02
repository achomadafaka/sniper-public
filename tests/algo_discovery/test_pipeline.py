"""Tests for the v2 algo-discovery pipeline driver + stage adapters.

v2 flow: fetch → protocol → dossier → classify → rule → route. The driver calls
module-level stage_* functions, so we monkeypatch stages to exercise control
flow without live API / heavy analysis.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import sniper.analysis.algo_discovery.pipeline as pl

WALLET = "0x" + "a" * 40

_RULE_OK = {
    "rejection_reason": None,
    "exit_rule": {"mode": "held"},
    "source_grain": {"n_episodes": 40, "grain_id": "g1"},
    "market_filter": {
        "categories": ["sports"], "market_types": ["binary"],
        "outcome_roles": ["binary_named_outcome"], "entry_price_range": [0.50, 0.75],
    },
    "sizing": {"median_usdc": 30.0},
    "timing": {"preferred_hours_utc": [14, 15]},
}
_RULE_EMPTY = {"rejection_reason": "no_interpretable_grain", "exit_rule": None}

_ALGO = {"label": "algorithmic", "route_hint": "rebuild_rule", "confidence": 0.8, "net_score": 3, "signals": {}}
_DISC = {"label": "discretionary", "route_hint": "copy_mirror", "confidence": 0.8, "net_score": -3, "signals": {}}
_AMBIG = {"label": "ambiguous", "route_hint": "human_review", "confidence": 0.0, "net_score": 0, "signals": {}}


def _stub(monkeypatch, *, classification, rule):
    monkeypatch.setattr(pl, "stage_fetch", lambda w, ctx: {"wallet": w, "closed_positions": [], "activity_rows": []})
    monkeypatch.setattr(pl, "stage_protocol", lambda w, wd, ctx: {"wallet": w, "gate_result": "pass"})
    monkeypatch.setattr(pl, "stage_dossier", lambda w, wd, r, ctx: {"descriptor_cadence": []})
    monkeypatch.setattr(pl, "stage_classify", lambda t, ctx, **kw: classification)
    monkeypatch.setattr(pl, "stage_rule", lambda t, ctx: rule)


# ===== stage_route =====

def test_route_copy_mirror_builds_shadow_copy_enrollment():
    out = pl.stage_route(WALLET, _RULE_OK, _DISC, None)
    assert out["route"] == "copy_mirror" and out["deployable"]
    assert out["enrollment"]["strategy_type"] == "shadow_copy_v1"
    assert out["enrollment"]["params"]["target_wallets"] == [WALLET]
    assert out["enrollment"]["params"]["max_position_size_usdc"] == 25.0  # capped


def test_route_rebuild_rule_builds_independent_spec():
    out = pl.stage_route(WALLET, _RULE_OK, _ALGO, None)
    assert out["route"] == "rebuild_rule" and out["deployable"]
    assert out["enrollment"]["strategy_type"] == "wallet_rule_v1"
    assert out["enrollment"]["status"] == "plugin_pending"
    assert out["enrollment"]["params"]["source_wallet"] == WALLET


def test_route_algorithmic_without_extractable_rule_goes_human():
    out = pl.stage_route(WALLET, _RULE_EMPTY, _ALGO, None)
    assert out["route"] == "human_review" and not out["deployable"]
    assert out["reason"] == "algorithmic_but_no_extractable_rule"
    assert out["enrollment"] is None


def test_route_ambiguous_goes_human():
    out = pl.stage_route(WALLET, _RULE_OK, _AMBIG, None)
    assert out["route"] == "human_review" and not out["deployable"]


def test_route_copy_mirror_tolerates_missing_rule():
    out = pl.stage_route(WALLET, None, _DISC, None)
    assert out["route"] == "copy_mirror"
    assert out["enrollment"]["params"]["market_filter"]["categories"] == []


# ===== stage_fetch =====

def test_stage_fetch_fixture_passthrough():
    assert pl.stage_fetch(WALLET, pl.Ctx(from_fixture={"wallet": WALLET, "x": 1})) == {"wallet": WALLET, "x": 1}


def test_stage_fetch_live_not_wired():
    with pytest.raises(NotImplementedError):
        pl.stage_fetch(WALLET, pl.Ctx(from_fixture=None))


# ===== driver =====

def test_run_discovery_algorithmic_routes_rebuild(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_ALGO, rule=_RULE_OK)
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET})
    assert res["route"] == "rebuild_rule"
    rd = Path(res["run_dir"])
    for a in ("01_wallet_data", "02_protocol_report", "03_dossier",
              "04_classification", "05_wallet_rule", "06_route"):
        assert (rd / f"{a}.json").exists(), a
    assert res["routing"]["enrollment"]["strategy_type"] == "wallet_rule_v1"


def test_run_discovery_discretionary_routes_copy_mirror(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_DISC, rule=_RULE_OK)
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET})
    assert res["route"] == "copy_mirror"
    assert res["routing"]["enrollment"]["strategy_type"] == "shadow_copy_v1"


def test_run_discovery_ambiguous_routes_human(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_AMBIG, rule=_RULE_OK)
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET})
    assert res["route"] == "human_review"


def test_run_discovery_algorithmic_no_rule_routes_human(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_ALGO, rule=_RULE_EMPTY)
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET})
    assert res["route"] == "human_review"
    assert res["routing"]["reason"] == "algorithmic_but_no_extractable_rule"


def test_run_discovery_backtest_is_optional_diagnostic(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_ALGO, rule=_RULE_OK)
    monkeypatch.setattr(pl, "stage_backtest", lambda rule, wd, ctx, *, slippage_pct: {"oos_match_count": 40})
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET}, run_backtest=True)
    assert (Path(res["run_dir"]) / "07_backtest_diagnostic.json").exists()
    assert res["backtest"]["oos_match_count"] == 40


def test_run_discovery_no_backtest_by_default(tmp_path, monkeypatch):
    _stub(monkeypatch, classification=_ALGO, rule=_RULE_OK)
    res = pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": WALLET})
    assert "backtest" not in res
    assert not (Path(res["run_dir"]) / "07_backtest_diagnostic.json").exists()


def test_run_discovery_fixture_wallet_mismatch_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "stage_fetch", lambda w, ctx: {"wallet": "0x" + "b" * 40})
    with pytest.raises(ValueError, match="refusing to analyze the wrong wallet"):
        pl.run_discovery(WALLET, run_root=tmp_path, from_fixture={"wallet": "0x" + "b" * 40})


# ===== CLI =====

def test_cli_prints_route_and_exits_zero(monkeypatch, capsys, tmp_path):
    import scripts.run_algo_discovery as cli
    monkeypatch.setattr(cli, "run_discovery", lambda *a, **k: {
        "route": "rebuild_rule",
        "classification": {"label": "algorithmic", "confidence": 0.8},
        "routing": {"route": "rebuild_rule", "deployable": True,
                    "enrollment": {"strategy_type": "wallet_rule_v1"}, "reason": None},
        "run_dir": str(tmp_path),
    })
    fx = tmp_path / "wd.json"
    fx.write_text('{"wallet": "0xabc"}')
    rc = cli.main(["--wallet", "0xabc", "--from-fixture", str(fx)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "route: rebuild_rule" in out
    assert "algorithmic" in out


def test_cli_live_not_wired_exits_2(monkeypatch):
    import scripts.run_algo_discovery as cli

    def _raise(*a, **k):
        raise NotImplementedError("live fetch not wired")

    monkeypatch.setattr(cli, "run_discovery", _raise)
    assert cli.main(["--wallet", "0xabc"]) == 2
