"""v23 qualify / reentry facts: absent is None (never 0 or infinite), strict windows, gaps blind a window."""
from sniper.services import memecoin_desk_v23_facts as F

T = 10_000.0


def sw(t, actor, buy=True, usd=10.0, tokens=1000.0, price=1.0):
    return {"received_at": t, "actor": actor, "is_buy": int(buy), "usd": usd, "tokens": tokens, "price_usd": price}


def q(rows, **kw):
    return F.qualify(rows, as_of=T, covered_from=kw.pop("covered_from", T - 3600), **kw)


def test_zero_prior_buyers_gives_null_growth_never_infinite():
    out = q([sw(T - 10, "a"), sw(T - 20, "b")])
    assert out["buyer_growth_60"] is None


def test_growth_counts_distinct_buyers():
    rows = [sw(T - 90, "a"), sw(T - 30, "b"), sw(T - 20, "c"), sw(T - 25, "c"), sw(T - 15, "d", buy=False)]
    assert q(rows)["buyer_growth_60"] == 2.0


def test_buy_share_and_crowd():
    rows = [sw(T - 30, "a", usd=30), sw(T - 20, "b", buy=False, usd=10), sw(T - 200, "c")]
    out = q(rows)
    assert out["buy_share_usd_60"] == 75.0 and out["crowd_60"] == 2


def test_quiet_minute_is_a_known_zero_crowd_but_null_share():
    out = q([sw(T - 200, "a")])
    assert out["crowd_60"] == 0 and out["buy_share_usd_60"] is None


def test_crowd_300_max_follows_gammas_w60():
    rows = [sw(T - 250 + i, "w%d" % i) for i in range(5)] + [sw(T - 5, "z")]
    assert q(rows)["crowd_300_max"] == 5


def test_gap_or_short_coverage_blinds_the_window():
    rows = [sw(T - 30, "a")]
    assert q(rows, gaps=(T - 40,))["crowd_60"] is None
    assert q(rows, covered_from=T - 100)["crowd_300_max"] is None and q(rows, covered_from=T - 100)["crowd_60"] == 1
    assert q(rows, covered_from=None)["crowd_60"] is None


def test_creator_sold_share_null_unless_known_whole_life():
    rows = [sw(T - 500, "dev", tokens=1000), sw(T - 50, "dev", buy=False, tokens=400)]
    assert q(rows, creator=None, creator_whole_life=True)["creator_sold_share"] is None        # creator unknown
    assert q(rows, creator="dev", creator_whole_life=False)["creator_sold_share"] is None      # partial origin
    assert q(rows, creator="dev", creator_whole_life=True)["creator_sold_share"] == 0.4


def test_creator_known_not_sold_is_zero():
    assert q([sw(T - 500, "dev")], creator="dev", creator_whole_life=True)["creator_sold_share"] == 0.0


def test_creator_unreadable_swap_is_unknown():
    rows = [sw(T - 500, "dev"), {**sw(T - 50, "dev", buy=False), "tokens": None}]
    assert q(rows, creator="dev", creator_whole_life=True)["creator_sold_share"] is None


CLOSE = {"realized_pnl": 2.56, "exit_price_usd": 1.0, "entry_fill_price_usd": 0.8, "closed_at_s": T - 100}


def test_reentry_post_exit_path():
    rows = [sw(T - 150, "a", price=5.0), sw(T - 90, "a", price=1.2), sw(T - 50, "a", price=0.9), sw(T - 10, "a", price=1.1)]
    out = F.reentry(rows, CLOSE, as_of=T, range5_pct=4.0, covered_from=T - 3600)
    assert out["kind"] == "win" and out["since_exit_s"] == 100.0
    assert out["post_exit_high_pct"] == 20.0 and out["post_exit_low_pct"] == -10.0 and out["now_vs_exit_pct"] == 10.0
    assert round(out["high_vs_now_pct"], 3) == 9.091 and out["pullback_range5"] == round(9.0909 / 4, 3)


def test_reentry_unknowns():
    rows = [sw(T - 50, "a", price=1.1)]
    cov = T - 3600
    assert F.reentry(rows, CLOSE, as_of=T, gaps=(T - 60,), covered_from=cov)["post_exit_high_pct"] is None
    assert F.reentry([], CLOSE, as_of=T, covered_from=cov)["high_vs_now_pct"] is None
    assert F.reentry(rows, {**CLOSE, "realized_pnl": -1.0}, as_of=T, covered_from=cov)["kind"] == "soft_loss"
    assert F.reentry(rows, CLOSE, as_of=T, covered_from=cov)["pullback_range5"] is None   # unknown noise
    assert F.reentry(rows, CLOSE, as_of=T, covered_from=T - 50)["high_vs_now_pct"] is None   # close before the window (eCat)
    assert F.reentry(rows, CLOSE, as_of=T, covered_from=None)["high_vs_now_pct"] is None     # venue switch: no one identity


# --- through the real tape adapter ------------------------------------------------------------------------------
from test_desk_structure_tape import build, _cohort, MINT, NOW, BASE  # noqa: E402
from sniper.services import memecoin_desk_contract as contract  # noqa: E402
from sniper.services import memecoin_desk_structure_tape as ST  # noqa: E402


def v23_cohort():
    c = _cohort()
    c["policy"]["facts"] = F.VERSION
    return c


def test_version_pin():
    assert ST.V23_FACTS_VERSION == F.VERSION


def test_v22_cohort_gets_no_v23_facts(tmp_path):
    out = ST.judge_structure_fn(build(tmp_path), lambda t: _cohort(), lambda m: ())({"cohort_id": "c"}, {"mint": MINT}, NOW)
    assert "qualify" not in out and "reentry" not in out


def test_v23_cohort_gets_qualify_and_passes_the_sheet_validator(tmp_path):
    fn = ST.judge_structure_fn(build(tmp_path), lambda t: v23_cohort(), lambda m: ())
    out = fn({"cohort_id": "c"}, {"mint": MINT}, NOW)
    qf = out["qualify"]
    assert qf["crowd_60"] == 4 and qf["buy_share_usd_60"] == round(80 / 88 * 100, 4)
    assert qf["creator_sold_share"] is None              # creator CR never traded: no position, unknown
    assert "reentry" not in out
    assert contract.validate_structure(out)["qualify"] == qf


def test_reentry_ticket_gets_the_post_exit_block(tmp_path):
    close = {"realized_pnl": 1.0, "exit_price_usd": 1.5, "entry_fill_price_usd": 1.2, "closed_at_s": NOW - 50}
    fn = ST.judge_structure_fn(build(tmp_path), lambda t: v23_cohort(), lambda m: (), close_of=lambda pid: close)
    out = fn({"cohort_id": "c"}, {"mint": MINT, "reentry": {"prev_position_id": 9}}, NOW)
    re = out["reentry"]
    assert re["kind"] == "win" and re["post_exit_high_pct"] == 20.0 and re["now_vs_exit_pct"] == round((1.7 / 1.5 - 1) * 100, 4)
    contract.validate_structure(out)


def test_reentry_close_read_failure_is_all_unknown(tmp_path):
    def boom(pid):
        raise RuntimeError("db")
    fn = ST.judge_structure_fn(build(tmp_path), lambda t: v23_cohort(), lambda m: (), close_of=boom)
    re = fn({"cohort_id": "c"}, {"mint": MINT, "reentry": {"prev_position_id": 9}}, NOW)["reentry"]
    assert re["kind"] is None and re["post_exit_high_pct"] is None


def test_entry_state_ports_gammas_state():
    rows = [sw(T - 290 + i * 10, "a", price=1.0 + 0.001 * (i % 2), usd=1.0) for i in range(20)]      # a tight base
    rows += [sw(T - 60, "b", price=1.5, usd=50.0), sw(T - 30, "c", price=1.3, usd=10.0)]
    out = F.entry_state(rows, as_of=T, covered_from=T - 3600)
    assert out["breakout_age_s"] == 60.0 and out["pos300"] == round((1.3 - 1.0) / 0.5, 4)
    assert out["base_s"] == 1800.0                   # as Gamma: the whole retained pre-high life sits in the 15 % base
    assert out["breakout_vol"] is not None


def test_entry_state_unknowns():
    rows = [sw(T - 30, "a", price=1.0), sw(T - 20, "a", price=1.1)]
    assert F.entry_state(rows, as_of=T, covered_from=None) == {"pos300": None, "breakout_age_s": None, "base_s": None,
                                                                "breakout_vol": None}
    assert F.entry_state(rows, as_of=T, covered_from=T - 100)["pos300"] is None            # 300 s not covered
    got = F.entry_state(rows, as_of=T, covered_from=T - 400)
    assert got["pos300"] == 1.0 and got["base_s"] is None                                 # base span beyond coverage
