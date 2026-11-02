"""Stop-width forward replication: frozen window and arms, and the winners-cut / losses-saved rule."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("swf", ROOT / "scripts/research/stop_width_forward.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def test_window_and_arms_are_the_preregistered_ones():
    assert (S.G.START, S.G.SEAL_AT) == ("2026-10-16T06:00:00+00:00", "2026-10-22T00:00:00+00:00")
    assert S.G.ARMS == ((25, 15, None), (25, 20, None), (25, 25, None), (25, 30, None), (25, 35, None))
    assert S.G.CONTROL == (25, 35, None) and S.SEED == 20261021


def test_a_tighter_stop_on_a_tp_winner_is_a_winner_cut():
    assert S.classify("tp", 62.5, "stop", 40.0, True) == "winner_cut"


def test_a_tighter_stop_above_the_control_stop_is_a_loss_saved():
    assert S.classify("stop", 30.0, "stop", 38.0, True) == "loss_saved"
    assert S.classify("stop", 30.0, "stop", 29.0, True) is None      # stopped lower: not a save


def test_an_unfired_or_non_stop_arm_counts_nowhere():
    assert S.classify("tp", 62.5, None, None, False) is None
    assert S.classify("tp", 62.5, "tp", 62.5, True) is None


def test_the_simulated_stop_fires_first_at_the_tighter_level():
    series = [(1.0, 45.0), (2.0, 39.0), (3.0, 34.0), (4.0, 70.0)]   # notional 50; -32% never reaches the 35% stop
    tight = S.G.simulate(series, 50.0, **S.G.grid_rules((25, 15, None), 80.0))
    ctrl = S.G.simulate(series, 50.0, **S.G.grid_rules((25, 35, None), 80.0))
    assert tight[2] == "stop" and tight[1] == 39.0
    assert ctrl[2] == "tp" and ctrl[1] == 70.0
