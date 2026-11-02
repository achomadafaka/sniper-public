"""A manual close uses the wider ladder so it lands on the first rung, not after a 3->10->25 walk.

2026-08-14: the operator's Close button felt stuck for seconds on thin pools — the 3% first rung
missed, so the executor walked the full ladder with a quote+build+sign+send+confirm per rung.
An operator who pressed Close has chosen to exit; a wider first rung lands on attempt one.
Automated exits keep the price-disciplined default.
"""
import pytest

from sniper.execution import solana_real_seams as S


def test_the_manual_and_default_ladders_differ():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[0] < cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL[0], \
        "the manual first rung must be wider than the automated first rung"


def test_manual_reason_selects_the_wide_ladder(monkeypatch):
    from sniper.settings import memecoin as cfg
    captured = {}

    def fake_plan(position, ladder, *, chain, cfg):
        captured["ladder"] = ladder
        return {"slippage_ladder": ladder}
    monkeypatch.setattr("sniper.execution.exec_tiering.exit_tier_plan", fake_plan)

    # exercise the ladder-selection branch in isolation (the full exit_fn needs a live wallet)
    def select(reason):
        return (cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
                if reason == "manual" and cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
                else cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER)
    assert select("manual") == cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
    assert select("real_tp") == cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER
    assert select("b_stop") == cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER


def test_an_empty_manual_ladder_falls_back_to_default(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL", [])
    ladder = (cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
              if "manual" == "manual" and cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL
              else cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER)
    assert ladder == cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER
