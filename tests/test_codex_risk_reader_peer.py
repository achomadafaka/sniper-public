"""Independent offline witnesses for the RISK evidence-reader review."""

import math

import pytest

from sniper.services import memecoin_desk_risk as R
from test_desk_risk_evidence_reader import NOW, SOL_QUOTE, curve, position, read, reader


def test_peer_reader_keeps_proven_prefill_anchor_after_six_minutes(reader, tmp_path):
    curve(tmp_path, [(-501, "anchor", 1, 1, 1.0, SOL_QUOTE, 5e9),
                     (-499, "first-post", 1, 1, 1.1, SOL_QUOTE, 5e9),
                     (-10, "latest", 1, 1, 1.2, SOL_QUOTE, 5e9)])
    e = read(reader, tmp_path, pos=position(age=500))
    assert e["fresh"] is True
    assert e["anchor_price"] == pytest.approx(1.0)
    assert e["high_since_fill"] == pytest.approx(1.2)


def test_peer_noise_never_reports_nonfinite_as_known():
    end = 10_000.0
    rows = [{"received_at": end - 300 + i * 5 + j, "signature": "%s:%s" % (i, j),
             "actor": "external", "is_buy": 1, "quote_atomic": 1,
             "price_sol": px}
            for i in range(60) for j, px in enumerate((1e-300, 1e300))]
    e = R.risk_evidence(rows, as_of=end + 2, opened_ts=end - 300,
                        entry_price=1, sb=0, coverage={"from": end - 300, "through": end},
                        ours_sigs=("our-entry",), ours_complete=True)
    assert e["n5"] is None or math.isfinite(e["n5"])
    if e["n5"] is None:
        assert "n5" in e["unknown"]
