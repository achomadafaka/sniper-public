import os

from sniper.algos.hl_microstructure.book import BookState
from sniper.algos.hl_microstructure.models import FeatureSnapshot, PolicyDecision
from sniper.algos.hl_microstructure.paper import PaperConfig, PaperEngine
from sniper.algos.hl_microstructure import persistence as P


def _book():
    return BookState.from_l2(
        "BTC",
        levels=[
            [{"px": "100.0", "sz": "10"}],
            [{"px": "100.5", "sz": "10"}],
        ],
        exchange_ts_ms=1_000,
        receive_ts_ms=1_000,
    )


def _snapshot():
    return FeatureSnapshot("BTC", 1_000, "hlm_fv0", {"mid": 100.25, "spread_bps": 5.0}, "ok", [])


def test_paper_engine_persists_entry_decision_fill_and_position(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001))

    result = engine.on_decision(
        PolicyDecision("flow_momentum", "enter", "long", "flow_confirmed", 0.8),
        _snapshot(),
        _book(),
    )

    assert result.status == "entered"
    assert conn.execute("SELECT COUNT(*) FROM hlm_decisions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hlm_positions WHERE state='open'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM hlm_fills WHERE fill_type='entry'").fetchone()[0] == 1


def test_paper_engine_records_skip_when_depth_insufficient(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001))
    thin = BookState.from_l2(
        "BTC",
        levels=[
            [{"px": "100.0", "sz": "0.01"}],
            [{"px": "100.5", "sz": "0.01"}],
        ],
        exchange_ts_ms=1_000,
        receive_ts_ms=1_000,
    )

    result = engine.on_decision(
        PolicyDecision("flow_momentum", "enter", "long", "flow_confirmed", 0.8),
        _snapshot(),
        thin,
    )

    assert result.status == "skipped"
    assert result.reason == "insufficient_depth"
    row = conn.execute("SELECT action, reason, status FROM hlm_decisions").fetchone()
    assert dict(row) == {"action": "skip", "reason": "insufficient_depth", "status": "skipped"}


def test_fusion_entry_persists_thesis_votes_and_dynamic_exit_plan(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001))

    result = engine.on_decision(
        PolicyDecision(
            "fusion_radar", "enter", "long", "fusion_3_source_long", 0.8,
            metadata={
                "thesis": "HOT long: source votes agree",
                "source_votes": {"flow": {"active": True, "side": "long", "score": 0.8}},
                "exit_plan": {"stop_loss_pct": 0.004, "take_profit_pct": 0.008, "time_stop_ms": 180_000},
            },
        ),
        _snapshot(),
        _book(),
    )

    row = conn.execute("SELECT entry_thesis, source_votes_json, exit_plan_json FROM hlm_positions WHERE id=?", (result.position_id,)).fetchone()
    assert row["entry_thesis"] == "HOT long: source votes agree"
    assert '"flow"' in row["source_votes_json"]
    assert '"stop_loss_pct": 0.004' in row["exit_plan_json"]


def test_fusion_position_exits_when_signal_decays(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001, take_profit_pct=1.0, stop_loss_pct=1.0))
    result = engine.on_decision(
        PolicyDecision(
            "fusion_radar", "enter", "long", "fusion_3_source_long", 0.8,
            metadata={"exit_plan": {"stop_loss_pct": 0.02, "take_profit_pct": 0.04, "time_stop_ms": 180_000}},
        ),
        _snapshot(),
        _book(),
    )

    out = engine.on_decision(
        PolicyDecision("fusion_radar", "skip", None, "needs_two_source_families", 0.0),
        FeatureSnapshot("BTC", 32_000, "hlm_fv0", {"mid": 100.25, "spread_bps": 5.0}, "ok", []),
        _book(),
    )

    assert out.status == "exited"
    assert out.reason == "signal_decay"
    row = conn.execute("SELECT state, close_reason FROM hlm_positions WHERE id=?", (result.position_id,)).fetchone()
    assert dict(row) == {"state": "closed", "close_reason": "signal_decay"}


def test_gated_arm_position_exits_on_decay_like_fusion_radar(tmp_path):
    # A/B gated arms must exit identically to the control once open. The engine's decay
    # branch keys on any "fusion*" policy_id, not just fusion_radar.
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001, take_profit_pct=1.0, stop_loss_pct=1.0))
    result = engine.on_decision(
        PolicyDecision(
            "fusion_gated_b1", "enter", "long", "gated_b1_2_fast_long", 0.8,
            metadata={"exit_plan": {"stop_loss_pct": 0.02, "take_profit_pct": 0.04, "time_stop_ms": 180_000}},
        ),
        _snapshot(),
        _book(),
    )

    out = engine.on_decision(
        PolicyDecision("fusion_gated_b1", "skip", None, "needs_two_source_families", 0.0),
        FeatureSnapshot("BTC", 32_000, "hlm_fv0", {"mid": 100.25, "spread_bps": 5.0}, "ok", []),
        _book(),
    )

    assert out.status == "exited"
    assert out.reason == "signal_decay"
    row = conn.execute("SELECT state, close_reason FROM hlm_positions WHERE id=?", (result.position_id,)).fetchone()
    assert dict(row) == {"state": "closed", "close_reason": "signal_decay"}


def test_fusion_signal_decay_holds_during_min_decay_window(tmp_path):
    db = os.path.join(str(tmp_path), "hlm.sqlite")
    conn = P.connect(db)
    P.ensure_schema(conn)
    engine = PaperEngine(conn, PaperConfig(notional_usdc=25.0, fee_rate=0.001))
    result = engine.on_decision(
        PolicyDecision(
            "fusion_radar", "enter", "long", "fusion_3_source_long", 0.8,
            metadata={"exit_plan": {"min_signal_decay_hold_ms": 30_000, "stop_loss_pct": 0.02, "take_profit_pct": 0.04}},
        ),
        _snapshot(),
        _book(),
    )

    early = engine.on_decision(
        PolicyDecision("fusion_radar", "skip", None, "needs_two_source_families", 0.0),
        FeatureSnapshot("BTC", 2_000, "hlm_fv0", {"mid": 100.25, "spread_bps": 5.0}, "ok", []),
        _book(),
    )
    late = engine.on_decision(
        PolicyDecision("fusion_radar", "skip", None, "needs_two_source_families", 0.0),
        FeatureSnapshot("BTC", 32_000, "hlm_fv0", {"mid": 100.25, "spread_bps": 5.0}, "ok", []),
        _book(),
    )

    assert early.status == "held"
    assert early.reason == "signal_decay_grace"
    assert late.status == "exited"
    assert late.reason == "signal_decay"
    row = conn.execute("SELECT state, close_reason FROM hlm_positions WHERE id=?", (result.position_id,)).fetchone()
    assert dict(row) == {"state": "closed", "close_reason": "signal_decay"}
