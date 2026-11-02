import json
import math
import os

from sniper.algos.polymarket_crypto5m import regime_vol as RV
from sniper.algos.polymarket_crypto5m import stabilized_z as SZ

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m_brain_v2_window.json")


def test_pipeline_on_a_real_window():
    d = json.load(open(FIX))
    closes = {int(s): float(p) for s, p in d["closes_by_sec"].items()}
    now = max(closes)
    rv = RV.regime_vol(closes, now)
    assert rv is not None and rv["vol_slow"] > 0 and rv["divergence"] > 0
    btc_now = closes[now]
    btc_start = closes[min(closes)]
    sz = SZ.stabilized_z(btc_now, btc_start, d["seconds_left"], rv, d["up_mid"], SZ.S_REF)
    assert sz is not None
    assert math.isfinite(sz["z_v2"]) and -10 < sz["z_v2"] < 10
    assert 0.0 <= sz["weight"] <= 1.0
    secs = sorted(closes)
    rets = [math.log(closes[b] / closes[a]) for a, b in zip(secs, secs[1:]) if closes[a] > 0]
    assert RV.sharp_window_label(rets, d["settlement"]) in {"calm", "sharp_revert", "sharp_continue"}
