"""Offline witnesses against frozen risk-sb-closes9c0ab6805 / regime56a26d806."""
import json
import math
from datetime import datetime, timezone

from test_desk_risk_break_v2 import W, R, L, FILL, P, pre_history, path
from test_desk_regime import RG, TS, make_tape, day_of, bk
from test_memecoin_desk_risk_live import POLICY, BOUNDS


# test_exit_for_structure_break_requires_the_confirmed_signal: owned by CORTEX-root's evidence-gated live.validate (not in this branch; stacks on it).


def test_the_last_second_of_an_open_five_second_bucket_cannot_confirm_a_close(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "SB_CLOSES", True)
    monkeypatch.setattr(W, "pre_fill_bars", lambda *a: pre_history())
    pos = {"mint": "M", "opened_at": datetime.fromtimestamp(FILL, timezone.utc).isoformat()}
    rows = path([P] * 5 + [P * .7] * 5, start=FILL, buy=1, sell=30)
    assert W.structure_break_of(pos, rows, tmp_path, FILL + 9.2, 0.0) is None


def test_a_pre_fill_break_cannot_be_reported_as_the_current_positions_break(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "SB_CLOSES", True)
    stale = pre_history() + path([P * .7] * 10, start=FILL, buy=1, sell=30)
    assert R.sb_closes(stale) is not None
    monkeypatch.setattr(W, "pre_fill_bars", lambda *a: stale)
    pos = {"mint": "M", "opened_at": datetime.fromtimestamp(FILL + 20, timezone.utc).isoformat()}
    assert W.structure_break_of(pos, [], tmp_path, FILL + 120, 0.0) is None


def test_structure_break_uses_log_noise_in_log_distance_units():
    start = FILL - 300
    rows = [{"t": start+i, "o": 1.1, "h": 1.2, "l": 1.0, "c": 1.1,
             "buy_usd": 1., "sell_usd": 1.} for i in range(300)]
    rows += [{"t": FILL+i, "o": .67, "h": .67, "l": .67, "c": .67,
              "buy_usd": 1., "sell_usd": 10.} for i in range(5)]
    assert math.log(1/.67) > 2 * math.log(1.2)
    assert R.sb_closes(rows, k=2) is not None


def test_empty_coin_rows_with_a_capture_gap_are_unknown(tmp_path):
    t = 1_792_000_005.0
    desk = make_tape(tmp_path, "OTHER", [(t-30, 1e-9, True)], day=day_of(t), gaps=(t-20,))
    out = RG.regime_for("M", desk_dir=desk, as_of=t+2)
    assert out["regime"] == "unknown", out


def test_an_old_graduation_does_not_become_quiet(tmp_path):
    t = 1_792_000_005.0
    desk = make_tape(tmp_path, "M", [(t-400, 1e-9, True)], day=day_of(t), real_quote=85_000_000_000)
    out = RG.regime_for("M", desk_dir=desk, as_of=t+2)
    assert out["regime"] == "unknown" and out["reason"] == "graduated", out


def test_a_strong_interbucket_trend_is_not_claimed_to_be_flat():
    end = 1000
    rows = [bk(i, 1.05 ** (i-(end-60)), 1.05 ** (i-(end-60)),
               1.05 ** (i-(end-60)), 1.05 ** (i-(end-60))) for i in range(end-60, end)]
    noise, why = TS.range5_detail(rows, end, end-60)
    out = RG.classify(rows, end, noise, end-60, noise_why=why)
    assert out["regime"] != "quiet", out


def test_rollback_knobs_loaded_from_the_state_env_take_effect(tmp_path, monkeypatch):
    from sniper.util import envfiles
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RISK_WARM_SPARE", "0")
    monkeypatch.setenv("MEMECOIN_DESK_RISK_WATCHER_ENABLED", "1")
    monkeypatch.setattr(W, "SB_CLOSES", True)
    monkeypatch.setattr(W, "REGIME", True)
    monkeypatch.setattr(W, "SB_K", 2.)
    ledger = tmp_path / "ledger.db"
    ledger.touch()
    def load(_root):
        monkeypatch.setenv("MEMECOIN_DESK_RISK_SB_CLOSES", "0")
        monkeypatch.setenv("MEMECOIN_DESK_RISK_REGIME", "0")
        monkeypatch.setenv("MEMECOIN_DESK_RISK_SB_K", "3.0")
    monkeypatch.setattr(envfiles, "load_env_files", load)
    seen = []
    def tick(*args, **kw):
        seen.append((W.SB_CLOSES, W.REGIME, W.SB_K))
        return {"calls": 0, "cycle": False}
    monkeypatch.setattr(W, "run_pass", tick)
    W.main(["--once", "--ledger-db", str(ledger)])
    assert seen == [(False, False, 3.)], seen


def test_an_elapsed_bucket_does_not_require_a_trade_in_its_last_second():
    sparse = [b for b in pre_history() if b["t"] % 5 != 4]
    rows = sparse + path([P * .7] * 10, start=FILL, buy=1, sell=30)
    assert R.sb_closes(rows) is not None
