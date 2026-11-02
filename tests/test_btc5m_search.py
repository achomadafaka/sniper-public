"""Walk-forward search core: fold splitting, per-config evaluation, TRAIN-only selection."""
from __future__ import annotations

from sniper.algos.btc5m import search as SR, signals as S


def _bars(closes):
    return [{"open_ms": i * 300000, "open": c, "high": c, "low": c, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)]


def test_walk_forward_folds_chronological_expanding():
    bars = _bars(list(range(100, 200)))          # 100 bars
    folds = SR.walk_forward_folds(bars, k=4)
    assert len(folds) == 4
    for train, oos in folds:
        assert train and oos
        assert train[-1]["open_ms"] < oos[0]["open_ms"]      # train strictly before oos
    # expanding: each fold's train is longer than the previous
    assert len(folds[0][0]) < len(folds[-1][0])


def test_evaluate_config_returns_train_and_oos_aggregates():
    bars = _bars([100 + (i % 7) for i in range(200)])
    r = SR.evaluate_config(bars, S.SIGNALS["meanrev_zscore"], {"n": 10, "entry": 1.0, "exit_z": 0.3}, k=3)
    for key in ("signal", "params", "train_sharpe", "oos_sharpe", "oos_sharpe_min",
                "oos_trades", "oos_trades_min", "train_trades_min", "oos_pf", "oos_ret"):
        assert key in r


def test_pooled_sharpe_is_stream_based_not_mean_of_folds():
    # positive drift stream -> positive Sharpe; flat -> 0; sign comes from the concatenated stream
    assert SR._pooled_sharpe([0.002, 0.0005, 0.0015, 0.001]) > 0   # positive mean WITH variance
    assert SR._pooled_sharpe([0.002, -0.003, 0.001, -0.004]) < 0   # negative mean
    assert SR._pooled_sharpe([0.001, 0.001, 0.001]) == 0.0         # zero variance -> 0, no div0
    assert SR._pooled_sharpe([]) == 0.0


def test_select_best_per_fold_trade_floor_rejects_single_regime():
    # sum clears min_trades(30) but one fold barely traded -> per-fold floor rejects it
    results = [{"signal": "lop", "params": {}, "train_sharpe": 5.0, "train_pf": 2.0,
                "train_trades": 50, "train_trades_min": 2,     # concentrated in one fold
                "oos_sharpe": 3.0, "oos_sharpe_min": 1.0, "oos_pf": 2.0, "oos_trades": 40,
                "oos_trades_min": 1}]
    assert SR.select_best(results, min_trades=30, min_fold_trades=10) is None


def test_select_best_gates_and_ranks_on_train_only():
    results = [
        {"signal": "a", "params": {}, "train_sharpe": 3.0, "train_pf": 1.5, "train_trades": 50,
         "oos_sharpe": -9.0, "oos_sharpe_min": -9.0, "oos_pf": 0.2, "oos_trades": 40, "oos_ret": -0.5},
        {"signal": "b", "params": {}, "train_sharpe": 1.0, "train_pf": 1.2, "train_trades": 50,
         "oos_sharpe": 2.0, "oos_sharpe_min": 1.0, "oos_pf": 1.4, "oos_trades": 40, "oos_ret": 0.1},
    ]
    best = SR.select_best(results, min_trades=30)
    # selection is by TRAIN sharpe -> picks 'a' (3.0) even though its OOS is terrible; the OOS check
    # is REPORTED, not used to pick (picking by OOS would be the leak we forbid).
    assert best["signal"] == "a"
    assert best["confirmed"] is False           # OOS did not hold -> honest "not confirmed"


def test_select_best_confirmed_when_oos_holds():
    results = [{"signal": "b", "params": {}, "train_sharpe": 1.0, "train_pf": 1.2, "train_trades": 50,
                "oos_sharpe": 2.0, "oos_sharpe_min": 0.5, "oos_pf": 1.4, "oos_trades": 40, "oos_ret": 0.1}]
    best = SR.select_best(results, min_trades=30)
    assert best["signal"] == "b" and best["confirmed"] is True


def test_select_best_none_when_no_train_survivor():
    results = [{"signal": "x", "params": {}, "train_sharpe": -1.0, "train_pf": 0.5, "train_trades": 50,
                "oos_sharpe": 5.0, "oos_sharpe_min": 5.0, "oos_pf": 9.0, "oos_trades": 40, "oos_ret": 1.0}]
    assert SR.select_best(results, min_trades=30) is None     # great OOS can't rescue a bad TRAIN
