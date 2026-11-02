"""Tests for edge decomposition (sniper/hyperlive/copy_decomp.py)."""
from sniper.hyperlive import copy_decomp as D


def _r(coin, side, pnl, detect, exit_=None):
    return {"coin": coin, "side": side, "pnl_bps": pnl, "detect_ts_ms": detect, "exit_ts_ms": exit_}


def test_by_coin_and_side():
    rows = [_r("ETH", "long", 50, 0), _r("ETH", "long", 30, 1), _r("BTC", "short", -20, 2)]
    bc = D.by_coin(rows)
    assert bc["ETH"]["n"] == 2 and bc["ETH"]["mean"] == 40.0
    bs = D.by_side(rows)
    assert bs["long"]["n"] == 2 and bs["short"]["mean"] == -20.0


def test_major_vs_alt_split():
    rows = [_r("BTC", "long", 10, 0), _r("BNB", "long", 20, 1),
            _r("GRASS", "long", 100, 2), _r("MORPHO", "short", -50, 3)]
    m = D.major_vs_alt(rows)
    assert m["major"]["n"] == 1 and m["alt"]["n"] == 3


def test_annotate_consensus_counts_overlap():
    # two ETH longs overlap in time, one BTC long alone
    rows = [_r("ETH", "long", 10, 100, 500), _r("ETH", "long", 20, 200, 600), _r("BTC", "long", 5, 300, 700)]
    ann = D.annotate_consensus(rows, now_ms=1000)
    by_detect = {r["detect_ts_ms"]: r["consensus_n"] for r in ann}
    assert by_detect[200] == 2       # second ETH long sees the first still open
    assert by_detect[100] == 1       # first ETH long at t=100, second not opened yet
    assert by_detect[300] == 1       # BTC alone


def test_annotate_consensus_open_extends_to_now():
    rows = [_r("ETH", "long", None, 100, None),   # open, extends to now
            _r("ETH", "long", 20, 500, 900)]
    ann = D.annotate_consensus(rows, now_ms=1000)
    second = [r for r in ann if r["detect_ts_ms"] == 500][0]
    assert second["consensus_n"] == 2             # sees the still-open first position


def test_consensus_buckets():
    rows = [{"pnl_bps": 10, "consensus_n": 1}, {"pnl_bps": 20, "consensus_n": 3},
            {"pnl_bps": -30, "consensus_n": 5}]
    b = D.consensus_buckets(rows)
    assert b["solo(1)"]["n"] == 1 and b["small(2-3)"]["mean"] == 20.0 and b["crowd(4+)"]["mean"] == -30.0


def test_top_contributors_ranked_by_sum():
    rows = [_r("ETH", "long", 50, 0), _r("ETH", "long", 60, 1), _r("BTC", "long", -20, 2)]
    tc = D.top_contributors(rows, n=2)
    assert tc[0]["coin"] == "ETH" and tc[0]["sum"] == 110.0
