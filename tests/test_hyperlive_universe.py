from sniper.hyperlive import universe as U


def _ctx(name, vol=1e8, mark=100, oracle=100, mid=100, funding=0.0, premium=0.0, prev=100, oi=1e6):
    return {"name": name, "ctx": {"dayNtlVlm": str(vol), "markPx": str(mark), "oraclePx": str(oracle),
                                  "midPx": str(mid), "funding": str(funding), "premium": str(premium),
                                  "prevDayPx": str(prev), "openInterest": str(oi)}}


def test_dislocated_market_outscores_calm_high_volume_one():
    calm = U.opportunity(_ctx("CALM", vol=2e9)["ctx"])
    dislocated = U.opportunity(_ctx("DISL", vol=1e8, mark=101, oracle=100)["ctx"])  # 100bps mark-oracle
    assert dislocated["score"] > calm["score"]
    assert dislocated["lead_reason"] == "mark-oracle dislocation"


def test_volume_floor_excludes_untradeable_even_if_dislocated():
    rows = U.select_universe([
        _ctx("TINY", vol=1e5, mark=110, oracle=100),     # huge dislocation but illiquid
        _ctx("LIQ", vol=5e8, funding=0.0001),
    ], max_assets=12, min_volume=5_000_000)
    assert [r["asset"] for r in rows] == ["LIQ"]          # TINY gated out


def test_oi_change_raises_score():
    base = U.opportunity(_ctx("X", oi=1e6)["ctx"], prev_oi=1e6)        # no change
    grew = U.opportunity(_ctx("X", oi=1.3e6)["ctx"], prev_oi=1e6)      # +30% OI
    assert grew["score"] > base["score"]
    assert grew["components"]["oi_change_pct"] > 0


def test_select_universe_ranks_by_state_and_caps():
    ctxs = [
        _ctx("A", vol=1e8, funding=0.0002),               # funding extreme
        _ctx("B", vol=1e8, mark=102, oracle=100),         # big dislocation -> top
        _ctx("C", vol=2e9),                               # calm but liquid
        _ctx("D", vol=1e8, premium=-0.004),               # premium extreme
    ]
    rows = U.select_universe(ctxs, max_assets=3)
    assert rows[0]["asset"] == "B"                         # most dislocated leads
    assert len(rows) == 3                                  # capped
    assert all(r["tradeable"] for r in rows)


def test_oi_map_snapshots_open_interest():
    m = U.oi_map([_ctx("BTC", oi=30000), _ctx("ETH", oi=None and 0 or 50000)])
    assert m["BTC"] == 30000 and m["ETH"] == 50000


def test_default_weights_match_hand_weights():
    # Omitting weights must reproduce the hand-set baseline exactly (no behavior drift).
    ctx = _ctx("X", mark=101, oracle=100, funding=0.0001)["ctx"]
    assert U.opportunity(ctx)["score"] == U.opportunity(ctx, weights=U.HAND_WEIGHTS)["score"]


def test_learned_weights_reorder_selection():
    # A move-heavy market and a dislocation-heavy market. Hand weights tie roughly; a learned
    # weight set that zeroes everything but move_24h must rank the mover first.
    ctxs = [
        _ctx("MOVER", vol=1e8, mid=110, prev=100),          # +10% 24h move, no dislocation
        _ctx("DISL", vol=1e8, mark=101, oracle=100),        # 100bps dislocation, no move
    ]
    move_only = {"dislocation": 0.0, "funding": 0.0, "oi_shift": 0.0, "premium": 0.0,
                 "move_24h": 5.0, "volume": 0.0}
    rows = U.select_universe(ctxs, max_assets=2, weights=move_only)
    assert rows[0]["asset"] == "MOVER"
    disl_only = {"dislocation": 5.0, "funding": 0.0, "oi_shift": 0.0, "premium": 0.0,
                 "move_24h": 0.0, "volume": 0.0}
    rows2 = U.select_universe(ctxs, max_assets=2, weights=disl_only)
    assert rows2[0]["asset"] == "DISL"


def test_save_and_load_weights_roundtrip(tmp_path):
    p = str(tmp_path / "w.json")
    w = {"dislocation": 1.1, "funding": 0.4, "oi_shift": 0.9, "premium": 0.7,
         "move_24h": 1.5, "volume": 0.5}
    U.save_weights(w, meta={"movers": 42}, path=p)
    got = U.load_weights(p)
    assert got == {k: round(v, 4) for k, v in w.items()}


def test_load_weights_missing_or_bad_returns_none(tmp_path):
    assert U.load_weights(str(tmp_path / "nope.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json")
    assert U.load_weights(str(bad)) is None
    empty = tmp_path / "empty.json"
    empty.write_text('{"weights": {}}')
    assert U.load_weights(str(empty)) is None       # empty → None → caller uses HAND_WEIGHTS
