"""Cohort D Amendments 31-32 (Gamma): the PumpSwap venue releases a pass as `ps_partial_measured` when the coin's OWN pool window has no blocking hole, instead of requiring zero holes anywhere.
The curve venue is unchanged. Offline, temp files only."""
import json
import math
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_dump_group_cache as C
from sniper.services import memecoin_desk_dump_group_source as S
from sniper.services import memecoin_desk_dump_group_worker as W

CUT = 2_000_000.0
CURVE_OLD = {"complete": False, "retired": True, "retired_since": CUT - C.RETIRED_AGE_OUT_S - 1.0}
CURVE_YOUNG = {"complete": False, "retired": True, "retired_since": CUT - 3600.0}
PS_SOUND = {"complete": False, "sound": True, "n_holes": 40, "loss_fraction_24h": 0.0093, "loss_hours": 24}
WIN = {"created_known": True, "window_from": CUT - 600.0, "holes_any": 3, "holes_blocking": 0}


def run(tmp_path, curve=CURVE_OLD, ps=PS_SOUND, score=None, venue="ps"):
    base = {"pool": "P", "mint": "M", "status": "pass", "reason": "below_threshold", "venue": venue, "buyer_share": 0.0, "buy_sol_share": 0.0, "ps_window": dict(WIN)}
    base.update(score or {})
    out = tmp_path / "c.json"
    C.publish_cache(out, {"P": base}, coverage={"curve": curve, "ps": ps}, cutoff=CUT, published_at=CUT + 0.5)
    return json.loads(out.read_text())["scores"]["P"]


def test_a_ps_coin_with_no_blocking_hole_in_its_own_window_passes_with_the_measured_stratum_label(tmp_path):
    got = run(tmp_path)
    assert got["status"] == "pass"
    assert got["dump_group_coverage"] == "ps_partial_measured" and got["ps_loss_fraction_24h"] == 0.0093 and got["ps_window_holes"] == 3 and got["ps_window_blocking_holes"] == 0
    assert got["curve_venue"] == "aged_out"


def test_a_complete_ps_venue_is_labelled_complete_not_partial(tmp_path):
    got = run(tmp_path, ps={"complete": True, "sound": True})
    assert got["status"] == "pass" and got["dump_group_coverage"] == "complete"


@pytest.mark.parametrize("win", [
    {"created_known": True, "window_from": CUT - 600.0, "holes_any": 4, "holes_blocking": 1},        # an unknown-scope or own-pool hole inside the window
    {"created_known": False, "window_from": None, "holes_any": 0, "holes_blocking": 0},              # the coin's creation is unknown: the window start is unknown
    None,
])
def test_a_blocking_hole_or_an_unknown_window_stays_unknown(tmp_path, win):
    got = run(tmp_path, score={"ps_window": win})
    assert got["status"] == "unknown" and got["reason"] == "dump_group_unknown" and got["detail"] in ("ps_window_hole", "partial_history")


@pytest.mark.parametrize("ps", [
    {"complete": False, "sound": False, "loss_fraction_24h": 0.0093},                                  # capture dead / exclusions invalid / stale cutoff / short memory
    {"complete": False, "sound": True},                                                                # loss fraction not measured
    {"complete": False, "sound": True, "loss_fraction_24h": float("nan")},
    {"complete": False, "sound": True, "loss_fraction_24h": -0.1},
    {"complete": False, "sound": True, "loss_fraction_24h": 1.5},
    {"complete": False, "sound": True, "loss_fraction_24h": True},
])
def test_the_pass_needs_a_sound_venue_and_a_measured_finite_loss_fraction(tmp_path, ps):
    assert run(tmp_path, ps=ps)["status"] == "unknown"


def test_the_curve_venue_rule_is_unchanged(tmp_path):
    assert run(tmp_path, curve=CURVE_YOUNG)["status"] == "unknown"                                      # retired < 24 h + 300 s: every pass still downgrades
    assert run(tmp_path, curve={"complete": False})["status"] == "unknown"                              # incomplete and not retired
    assert run(tmp_path, curve={"complete": True})["curve_venue"] == "complete"


def test_a_curve_coin_still_needs_both_venues_complete(tmp_path):
    assert run(tmp_path, venue="curve")["status"] == "unknown"
    assert run(tmp_path, venue="curve", curve={"complete": True}, ps={"complete": True})["status"] == "pass"


def test_a_positive_block_is_never_turned_into_a_pass_and_stays_a_kill(tmp_path):
    kill = {"status": "kill", "reason": "dump_group_buyers", "buyer_share": 0.5, "buy_sol_share": 0.8, "matched_buyers": 4, "buyers": 8,
            "positive_proven": True, "target_complete": True, "exclusions_valid": True}
    assert run(tmp_path, score=kill)["status"] == "kill"


def test_the_new_fields_reach_the_ticket_through_the_cache_lookup(tmp_path):
    path = tmp_path / "cache.json"
    C.publish_cache(path, {"P": {"pool": "P", "mint": "M", "status": "pass", "reason": "below_threshold", "venue": "ps", "buyer_share": 0.0, "buy_sol_share": 0.0, "ps_window": dict(WIN)}},
                    coverage={"curve": CURVE_OLD, "ps": PS_SOUND}, cutoff=CUT, published_at=CUT + 0.5)
    cache = C.SnapshotCache(path)
    cache.reload()
    got = cache.lookup(mint="M", pool="P", now=CUT + 0.6)
    assert got["status"] == "pass" and got["dump_group_coverage"] == "ps_partial_measured" and got["ps_loss_fraction_24h"] == 0.0093 and got["ps_window_holes"] == 3 and got["curve_venue"] == "aged_out"


# ------------------------------------------------------------------ worker side: the per-coin window and the measured loss fraction
def test_window_holes_split_into_any_and_blocking_by_scope_and_time():
    created, asof = 1000.0, 1600.0
    gaps = [(500.0, 510.0, "far_before", None, "ps"),                                       # before [created-60, asof]: not counted
            (950.0, 951.0, "gap:worker_start", None, "ps"),                                 # unknown scope inside the window: blocking
            (1100.0, 1101.0, "late_row", "OTHER", "ps"),                                    # proven for another pool: counted, not blocking
            (1200.0, 1201.0, "late_row", "P", "ps"),                                        # proven for this pool: blocking
            (1300.0, 1301.0, "gap:x", None, "curve"),                                       # the curve venue's hole: not a ps hole
            (1700.0, 1701.0, "after", None, "ps")]                                          # after asof
    got = S.ps_window_holes(gaps, "P", created, asof)
    assert got == {"created_known": True, "window_from": 940.0, "holes_any": 3, "holes_blocking": 2}
    assert S.ps_window_holes(gaps[2:3], "P", created, asof)["holes_blocking"] == 0


def test_an_unknown_creation_time_makes_the_window_unknown():
    got = S.ps_window_holes([], "P", None, 1600.0)
    assert got["created_known"] is False and got["holes_blocking"] == 0


def _census(path, now, n=24, swaps=455_000, gaps=4_250):
    rows = [{"hour": int(now // 3600 * 3600 - 3600 * (i + 1)), "venue_swaps": swaps, "gaps": gaps} for i in range(n)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_ps_loss_fraction_is_loss_receipts_over_swaps_plus_receipts_over_24_hours(tmp_path):
    now = 1_800_000_000.0
    f = tmp_path / "pumpswap" / "census_hourly.jsonl"
    f.parent.mkdir()
    _census(f, now)
    got = S.ps_loss_fraction(tmp_path, now)
    assert got["hours"] == 24 and got["fraction"] == pytest.approx(4_250 / (455_000 + 4_250))


def test_too_few_hours_or_no_file_is_unknown_not_zero(tmp_path):
    now = 1_800_000_000.0
    assert S.ps_loss_fraction(tmp_path, now)["fraction"] is None
    f = tmp_path / "pumpswap" / "census_hourly.jsonl"
    f.parent.mkdir()
    _census(f, now, n=10)
    assert S.ps_loss_fraction(tmp_path, now)["fraction"] is None
    f.write_text("not json\n")
    assert S.ps_loss_fraction(tmp_path, now)["fraction"] is None
    _census(f, now, n=24, swaps=0, gaps=0)
    assert S.ps_loss_fraction(tmp_path, now)["fraction"] is None                           # 0 / 0 is not a measurement


def test_venue_coverage_reports_sound_separately_from_complete():
    class Excl:
        valid = True
        def state(self): return {}
    class St:
        errors = 0
    class Tail:
        late_rows = rows_fed = db_errors = 0
        class norm:
            dropped = {}
    hole = (CUT - 100.0, CUT - 99.0, "gap:x", None, "ps")
    kw = dict(warm=True, cutoff=CUT, now=CUT + 1.0, state=St(), tail=Tail(), exclusions=Excl(), capture_alive=True, history_start=CUT - 86400.0, venue="ps")
    with_hole = S.venue_coverage(holes=[hole], **kw)
    assert with_hole["complete"] is False and with_hole["sound"] is True
    assert S.venue_coverage(holes=[], **kw)["complete"] is True
    assert S.venue_coverage(holes=[hole], **{**kw, "capture_alive": False})["sound"] is False
    assert S.venue_coverage(holes=[hole], **{**kw, "warm": False})["sound"] is False
    assert S.venue_coverage(holes=[hole], **{**kw, "history_start": CUT - 3600.0})["sound"] is False


# ------------------------------------------------------------------ end to end through the worker
import sqlite3                                                                              # noqa: E402
import sys                                                                                  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_desk_dump_group_worker as TW                                                    # noqa: E402


def _ps_world(tmp_path, monkeypatch, *, created=None, own_hole=False, other_hole=False, unknown_hole=False, census=True):
    desk = tmp_path / "desk"
    TW.write_tape(desk, [TW.Sim("TARGET2")])
    q = TW.PSSim("QPOOL", "QMINT")
    q.buy(TW.T + 4, "psbuyer", 5_000_000_000, 90)
    for i in range(6):
        q.buy(TW.T + 12 + i, "quiet%d" % i, 100_000_000, 120 + i)
    TW.write_ps(desk, [q])
    con = sqlite3.connect(desk / "pumpswap" / "tape.db")
    con.execute("create table pools(pool text, received_at real)")
    con.execute("create index pools_pool on pools(pool)")
    if created is not False:
        con.execute("insert into pools values('QPOOL', ?)", (TW.T - 100 if created is None else created,))
    if unknown_hole:
        con.execute("insert into gaps_ps values(?, 'worker_start', 1)", (TW.T + 8,))
    con.commit()
    con.close()
    if census:
        now = TW.T + 30
        rows = [{"hour": int(now // 3600 * 3600 - 3600 * (i + 1)), "venue_swaps": 455_000, "gaps": 4_250} for i in range(24)]
        (desk / "pumpswap" / "census_hourly.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    w, clock, pub = TW.make_worker_at(tmp_path, desk)
    monkeypatch.setattr(W, "curve_capture_off", lambda d, e=None, now=None: True)
    monkeypatch.setattr(W, "producer_retired_since", lambda d, now=None: TW.T - 90000.0)
    TW.run_until_warm(w, clock)
    if own_hole:
        w.state.add_gap(TW.T + 9, TW.T + 9.5, "late_row", "QPOOL", "ps")
    if other_hole:
        w.state.add_gap(TW.T + 9, TW.T + 9.5, "late_row", "ELSEWHERE", "ps")
    for _ in range(2):
        clock["t"] += 1.0
        w.cycle()
    return w, clock


def _fact(w, clock, tmp_path):
    cache = C.SnapshotCache(tmp_path / "dg" / "cache.json")
    cache.reload()
    return cache.lookup(mint="QMINT", pool="QPOOL", now=json.loads((tmp_path / "dg" / "cache.json").read_text())["published_at"] + 0.1)


def test_end_to_end_a_quiet_ps_coin_with_a_hole_elsewhere_passes_as_ps_partial_measured(tmp_path, monkeypatch):
    w, clock = _ps_world(tmp_path, monkeypatch, other_hole=True)
    got = _fact(w, clock, tmp_path)
    assert got["status"] == "pass", got
    assert got["dump_group_coverage"] == "ps_partial_measured" and got["ps_window_holes"] >= 1 and got["ps_window_blocking_holes"] == 0
    assert got["ps_loss_fraction_24h"] == pytest.approx(4_250 / (455_000 + 4_250)) and got["curve_venue"] == "aged_out"


def test_end_to_end_a_hole_on_the_coins_own_pool_inside_the_window_stays_unknown(tmp_path, monkeypatch):
    w, clock = _ps_world(tmp_path, monkeypatch, own_hole=True)
    got = _fact(w, clock, tmp_path)
    assert got["status"] == "unknown" and got["detail"] == "ps_window_hole"


def test_end_to_end_a_producer_hole_of_unknown_scope_inside_the_window_stays_unknown(tmp_path, monkeypatch):
    w, clock = _ps_world(tmp_path, monkeypatch, unknown_hole=True)
    got = _fact(w, clock, tmp_path)
    assert got["status"] == "unknown" and got["detail"] == "ps_window_hole"


def test_end_to_end_an_unknown_creation_time_or_missing_census_stays_unknown(tmp_path, monkeypatch):
    w, clock = _ps_world(tmp_path, monkeypatch, created=False, other_hole=True)
    assert _fact(w, clock, tmp_path)["status"] == "unknown"
    tmp2 = tmp_path / "second"
    tmp2.mkdir()
    w, clock = _ps_world(tmp2, monkeypatch, census=False, other_hole=True)
    assert _fact(w, clock, tmp2)["status"] == "unknown"


def test_a_hole_that_ended_before_the_coin_was_created_does_not_count():
    hole = [(TW.T + 8, TW.T + 8.1, "gap:worker_start", None, "ps")]
    assert S.ps_window_holes(hole, "QPOOL", TW.T + 100, TW.T + 130)["holes_any"] == 0                  # window starts at created - 60 s, after the hole
    assert S.ps_window_holes(hole, "QPOOL", TW.T + 40, TW.T + 130) == {"created_known": True, "window_from": TW.T - 20.0, "holes_any": 1, "holes_blocking": 1}      # created - 60 s = T - 20 < hole: inside
