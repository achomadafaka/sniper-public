"""Wiring + invariance guard for the drift-reach shadow in the active runner.

The runner MUST import cleanly with the new drift deps (the watchdog launches it) and _connect MUST create
the shadow table. Invariance (zero live-decision change) is architectural: scalp_trader.decide() never
receives any drift field — the prediction goes only to the crypto5m_drift_reach table — so the broad
decision-path regression is the full crypto5m suite staying green, not a field this test could toggle."""
import importlib


def test_runner_imports_and_connect_creates_shadow_table(tmp_path):
    mod = importlib.import_module("scripts.run_polymarket_crypto5m_active_trader")
    conn = mod._connect(str(tmp_path / "active.sqlite"))
    try:
        tabs = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "crypto5m_drift_reach" in tabs            # the drift shadow table is wired into _connect
        assert "crypto5m_position_marks" in tabs         # the exit-counterfactual marks table is wired in
        assert "crypto5m_ev_observations" in tabs        # existing wiring intact
        assert "crypto5m_active_positions" in tabs
    finally:
        conn.close()


def test_decide_signature_has_no_drift_field():
    # invariance, structurally: the decision function cannot see a drift field -> it cannot be influenced
    import inspect
    from sniper.algos.polymarket_crypto5m import scalp_trader as ST
    params = set(inspect.signature(ST.decide).parameters)
    assert not (params & {"mu_hat", "drift", "exp_fav_move", "touch_prob", "drift_pred"})
