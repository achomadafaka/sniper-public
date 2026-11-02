import random
from sniper.algos.polymarket_crypto5m import window_bootstrap as WB


def test_boot_means_is_window_clustered_and_deterministic():
    by_win = {"a": [1.0, 1.0], "b": [3.0], "c": [-1.0, -1.0, -1.0]}
    out1 = WB.boot_means(by_win, random.Random(WB.BOOT_SEED), nb=500)
    out2 = WB.boot_means(by_win, random.Random(WB.BOOT_SEED), nb=500)
    assert out1 == out2                      # deterministic for a fixed seed
    assert out1 == sorted(out1)              # returned sorted
    assert len(out1) == 500
    assert min(by_win["c"]) <= out1[0] and out1[-1] <= max(by_win["b"])


def test_boot_means_empty_and_pctile():
    assert WB.boot_means({}, random.Random(1)) == []
    assert WB.pctile([], 0.5) is None
    assert WB.pctile([0.0, 1.0, 2.0, 3.0], 0.5) in (1.0, 2.0)
