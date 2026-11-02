"""Tape adapter for the structure module: prices from virtual reserves, strict cut, blind reasons, gaps, ours, no raise."""
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_structure as S
from sniper.services import memecoin_desk_structure_tape as ST

NOW = 1_800_000_000.0
DAY = ST.utc_day(NOW)
MINT = "MintStruct"
SOL = 100.0
VT = 1_000_000_000          # 1000 tokens of virtual token reserve


def vq_for(price_usd):
    return int(price_usd / SOL * (VT / 1e6) * 1e9)


# (rel_to_as_of, price_usd, is_buy, usd, actor, slot)
BASE = [(-290, 1.0, 1, 10, "A", 100), (-200, 1.2, 0, 5, "B", 110), (-100, 1.5, 1, 20, "C", 120), (-40, 1.4, 1, 10, "D", 130),
        (-20, 1.6, 1, 30, "E", 140), (-10, 1.8, 1, 40, "F", 150), (-5, 1.7, 0, 8, "G", 160)]


def build(tmp, swaps=BASE, *, blocks=((1000, -3.0),), gaps=(), launch=-1000.0, heartbeat=None):
    desk = tmp / "desk"
    for f in ("swaps", "blocks", "launches", "gaps"):
        (desk / f).mkdir(parents=True)
    hb = {"ts": NOW - 0.5, "connected": True, "sol_usd": SOL}
    hb.update(heartbeat or {})
    (desk / "universe.heartbeat").write_text(json.dumps(hb))
    c = sqlite3.connect(str(desk / "swaps" / (DAY + ".db")))
    c.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, curve TEXT, is_buy INTEGER, "
              "quote_atomic INTEGER, token_atomic INTEGER, actor TEXT, virtual_quote INTEGER, virtual_token INTEGER, "
              "block_time INTEGER, received_at REAL, real_token INTEGER, fee_bps INTEGER, creator_fee_bps INTEGER, "
              "PRIMARY KEY(signature, ordinal))")
    for i, (rel, price, buy, usd, actor, slot) in enumerate(swaps):
        c.execute("INSERT INTO swaps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (slot, "sig%d" % i, 0, MINT, "c", buy, int(usd / SOL * 1e9), 1, actor, vq_for(price), VT, 0, NOW + rel, 1, 0, 0))
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "blocks" / (DAY + ".db")))
    c.execute("CREATE TABLE blocks(slot INTEGER PRIMARY KEY, block_time INTEGER, received_at REAL)")
    c.executemany("INSERT INTO blocks VALUES(?,0,?)", [(s, NOW + r) for s, r in blocks])
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "launches" / (DAY + ".db")))
    c.execute("CREATE TABLE launches(mint TEXT PRIMARY KEY, curve TEXT, creator TEXT, slot INTEGER, signature TEXT, "
              "block_time INTEGER, received_at REAL, symbol TEXT, name TEXT)")
    if launch is not None:
        c.execute("INSERT INTO launches VALUES(?,?,?,?,?,?,?,?,?)", (MINT, "c", "CR", 1, "l", 0, NOW + launch, "", ""))
    c.commit()
    c.close()
    c = sqlite3.connect(str(desk / "gaps" / (DAY + ".db")))
    c.execute("CREATE TABLE gaps(at REAL, cause TEXT, last_slot INTEGER)")
    c.executemany("INSERT INTO gaps VALUES(?,?,?)", [(NOW + g, "x", 0) for g in gaps])
    c.commit()
    c.close()
    return desk


def run(desk, as_of=NOW, **kw):
    kw.setdefault("read_margin_s", 0.0)
    return ST.structure_for(MINT, desk_dir=desk, as_of=as_of, **kw)


def test_end_to_end_numbers_match_the_pure_fixture(tmp_path):
    out = run(build(tmp_path))
    w15, w60 = out["windows"]["15s"], out["windows"]["60s"]
    assert out["tape"] == "ok" and out["price_usd"] == pytest.approx(1.7, rel=1e-6)
    assert (w15["n_swaps"], w15["buy_usd"], w15["sell_usd"]) == (2, 40.0, 8.0)
    assert w60["range_pct"] == pytest.approx(28.5714, abs=1e-3) and w60["pos_in_range_pct"] == pytest.approx(75.0, abs=1e-3)
    assert out["chart"][22][1] == 588 and out["meta"]["launch_known"] is True and out["meta"]["swaps_read"] == 7


def test_cut_slot_is_max_block_slot_plus_one_and_excludes_later_slots(tmp_path):
    swaps = BASE + [(-1, 3.0, 1, 99, "X", 1000)]                          # slot == max block slot 1000: usable (slot < 1001)
    assert run(build(tmp_path, swaps))["price_usd"] == pytest.approx(3.0, rel=1e-6)
    swaps = BASE + [(-1, 3.0, 1, 99, "X", 1001)]                          # slot 1001 not yet seen in blocks: excluded
    assert run(build(tmp_path / "b", swaps))["price_usd"] == pytest.approx(1.7, rel=1e-6)
    assert S.structure([], as_of=NOW, cut_slot=1001)["tape"] == "ok"


def test_read_margin_drops_the_newest_rows(tmp_path):
    desk = build(tmp_path, BASE + [(-1, 3.0, 1, 99, "X", 500)])
    assert run(desk, read_margin_s=0.0)["price_usd"] == pytest.approx(3.0, rel=1e-6)
    out = run(desk, read_margin_s=2.0)
    assert out["price_usd"] == pytest.approx(1.7, rel=1e-6) and out["meta"]["tape_cut"] == NOW - 2.0 and out["meta"]["read_margin_s"] == 2.0


def test_blind_reasons_never_raise_and_carry_no_figures(tmp_path):
    for i, (hb, reason) in enumerate((({"ts": NOW - 30}, "tape_stale"), ({"connected": False}, "tape_stale"),
                                      ({"sol_usd": 0}, "sol_usd_unknown"), ({"sol_usd": None}, "sol_usd_unknown"))):
        out = run(build(tmp_path / str(i), heartbeat=hb))
        assert (out["tape"], out["reason"]) == ("unknown", reason) and "windows" not in out
    out = run(build(tmp_path / "nb", blocks=()))
    assert out["reason"] == "no_block_before_as_of"
    d = build(tmp_path / "bad")
    (d / "universe.heartbeat").write_text("not json")
    assert run(d)["reason"] == "heartbeat_unreadable"
    d = build(tmp_path / "corrupt")
    (d / "swaps" / (DAY + ".db")).write_bytes(b"garbage" * 100)
    assert run(d)["tape"] == "unknown" and run(d)["reason"].startswith("tape_unreadable")


def test_gap_inside_a_window_is_unknown_through_the_adapter(tmp_path):
    out = run(build(tmp_path, gaps=(-30,)))
    assert out["windows"]["15s"]["state"] == "ok"
    assert out["windows"]["60s"]["reason"] == "tape_gap" and out["chart"] is None and out["meta"]["gaps_in_range"] == 1


def test_ours_excluded_by_actor_and_signature(tmp_path):
    base = run(build(tmp_path / "a"))
    swaps = BASE + [(-3, 5.0, 1, 500, "OURS", 170)]
    out = run(build(tmp_path / "b", swaps), ours=frozenset({"OURS"}))
    assert out["windows"] == base["windows"] and out["price_usd"] == base["price_usd"]
    out2 = run(build(tmp_path / "c", swaps), ours_sigs=frozenset({"sig7"}))
    assert out2["windows"] == base["windows"]


def test_unknown_launch_is_flagged(tmp_path):
    out = run(build(tmp_path, launch=None))
    assert out["meta"]["launch_known"] is False and out["tape"] == "ok"


def test_unpriced_swaps_have_no_price(tmp_path):
    desk = build(tmp_path)
    c = sqlite3.connect(str(desk / "swaps" / (DAY + ".db")))
    c.execute("UPDATE swaps SET virtual_token=0")
    c.commit()
    c.close()
    out = run(desk)
    assert out["price_usd"] is None and out["windows"]["15s"]["high"] is None and out["windows"]["15s"]["buy_usd"] == 40.0


def test_fill_sigs_learn_our_wallet_and_drop_every_swap_of_it(tmp_path):
    base = run(build(tmp_path / "a"))
    swaps = BASE + [(-3, 5.0, 1, 500, "OURS", 170), (-2, 0.5, 0, 500, "OURS", 171)]       # sig7 and sig8 are OURS
    unwired = run(build(tmp_path / "b", swaps))
    assert unwired["windows"] != base["windows"]
    out = run(build(tmp_path / "c", swaps), fill_sigs=("sig7",))                            # one fill signature -> the wallet -> both swaps
    assert out["windows"] == base["windows"] and out["price_usd"] == base["price_usd"]


def _cohort():
    return {"policy": {"structure": {"version": S.VERSION, "read_margin_s": 0.0},
                       "rail_bounds": {"sl_pct": [10, 40], "tp_pct": [15, 150], "trail_pct": [8, 30]}}}


def test_judge_structure_fn_handles_old_cohort_pumpswap_bounds_and_drops_meta(tmp_path):
    desk = build(tmp_path)
    fn = ST.judge_structure_fn(desk, lambda t: _cohort(), lambda mint: ())
    cand = {"mint": MINT}
    out = fn({"cohort_id": "c"}, cand, NOW)
    assert out["tape"] == "ok" and out["rails"]["trail_pct"] == 8 and "meta" not in out        # no absolute clocks on the sheet
    assert fn({"cohort_id": "c"}, {**cand, "venue": "pumpswap"}, NOW)["reason"] != "not_applicable"   # read since 10-23
    old = ST.judge_structure_fn(desk, lambda t: {"policy": {}}, lambda mint: ())
    assert old({"cohort_id": "c"}, cand, NOW) is None                                           # a cohort without the policy key: no block


def test_judge_path_excludes_our_funded_fills_through_the_real_function(tmp_path):
    base = run(build(tmp_path / "a"))
    swaps = BASE + [(-3, 5.0, 1, 500, "OURS", 170), (-2, 0.5, 0, 500, "OURS", 171)]            # sig7, sig8 are our copy-lane trades
    desk = build(tmp_path / "b", swaps)
    seen = []
    fn = ST.judge_structure_fn(desk, lambda t: _cohort(), lambda mint: seen.append(mint) or ("sig7",))
    out = fn({"cohort_id": "c"}, {"mint": MINT}, NOW)
    assert seen == [MINT] and out["windows"] == base["windows"] and out["price_usd"] == base["price_usd"]


@pytest.mark.parametrize("fills", [None, "raises"])
def test_judge_path_without_our_fills_is_blind_not_unfiltered(tmp_path, fills):
    desk = build(tmp_path)

    def boom(mint):
        raise RuntimeError("ledger locked")
    fn = ST.judge_structure_fn(desk, lambda t: _cohort(), boom if fills == "raises" else None)
    assert fn({"cohort_id": "c"}, {"mint": MINT}, NOW) == {"version": S.VERSION, "tape": "unknown", "reason": "ours_unknown"}


def test_a_stalled_block_stream_is_blind_not_a_quiet_market(tmp_path):
    out = run(build(tmp_path, blocks=((1000, -10.0),)))                    # newest block 10 s before the read cut
    assert (out["tape"], out["reason"]) == ("unknown", "tape_stale")
    assert run(build(tmp_path / "ok", blocks=((1000, -3.0),)))["tape"] == "ok"             # exactly 3 s is still fresh


def test_a_block_received_exactly_at_the_cut_does_not_count(tmp_path):
    out = run(build(tmp_path, blocks=((1000, -3.0), (2000, 0.0))))         # slot 2000 received at as_of: excluded (strict)
    assert out["tape"] == "ok" and out["cut_slot"] == 1001


def test_a_launch_older_than_the_lookback_makes_only_the_launch_window_unknown(tmp_path):
    out = run(build(tmp_path, launch=-10_000.0), max_lookback_s=3600.0)
    assert out["windows"]["launch"] == {"state": "unknown", "reason": "beyond_lookback"} and out["windows"]["300s"]["state"] == "ok"
    assert out["meta"]["launch_truncated"] is True


def test_structure_work_is_linear_not_quadratic():
    import time
    rows = [{"received_at": NOW - 20_000 + i * 2.5, "slot": 5, "signature": "s%d" % i, "ordinal": 0, "is_buy": i % 2,
             "usd": 10.0, "price_usd": 1.0 + (i % 50) / 100, "actor": "a%d" % (i % 40)} for i in range(8000)]
    t0 = time.perf_counter()
    S.structure(rows, as_of=NOW, cut_slot=10, launched_at=NOW - 20_000)
    assert time.perf_counter() - t0 < 0.5


def test_fill_signature_filter_works_even_when_the_wallet_cannot_be_learned(tmp_path, monkeypatch):
    base = run(build(tmp_path / "a"))
    swaps = BASE + [(-3, 5.0, 1, 500, "ROUTER", 170)]                              # sig7: our tx, executed by a program/router actor
    monkeypatch.setattr(ST, "actors_of_signatures", lambda *a, **k: set())        # the actor lookup finds nothing
    out = run(build(tmp_path / "b", swaps), fill_sigs=("sig7",))
    assert out["windows"] == base["windows"] and out["price_usd"] == base["price_usd"]


def test_the_true_range_fields_reach_only_cohorts_that_read_them(tmp_path):
    desk = build(tmp_path)
    for extra, has in (({}, False), ({"standing_orders": {"noise_basis": "true_range"}}, True),
                       ({"standing_orders": {"drift_k": 2.0}}, True), ({"standing_orders": {"noise_basis": "swing"}}, False)):
        cohort = _cohort()
        cohort["policy"].update(extra)
        out = ST.judge_structure_fn(desk, lambda t: cohort, lambda mint: ())({"cohort_id": "c"}, {"mint": MINT}, NOW)
        v = out["volatility"]
        assert ("median_5s_true_range_pct" in v) is has and ("true_range_bars" in v) is has and "median_5s_swing_pct" in v
