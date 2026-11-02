"""Gamma's pre-registered 4663 pilot stop rule in the read (docs/audits/2026-10-02-rh-fast-lane-pilot-
stop-rule.md): per sent decision est/true (H2), per fill the exact fill from our own receipt against
the source's true post price and average (H1, E1), the running revert share (E2), H3 from the fill
checks and held bookings, and (our_block, our_tx_index) for H4's block reads."""
import sqlite3
from fractions import Fraction
from math import isqrt

import pytest

import scripts.research.rh_fast_lane_read as read
from sniper.services import memecoin_rh_fast_lane as lane

SQRT = 874745060896749537239184449426278          # the decide tests' pool: wei per atomic below
TRUE = Fraction(1 << 192, SQRT ** 2)


def _sqrt_for(price_wei_per_atomic):
    return isqrt(int(Fraction(1 << 192) / price_wei_per_atomic))


def _feed(tmp_path, rows):
    path = str(tmp_path / "feed.db")
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE rh_fast_decisions (tx_hash TEXT, est_post_sqrt TEXT, true_post_sqrt "
               "TEXT, src_in_wei TEXT, src_out TEXT, chase_pct REAL, premium REAL)")
    db.executemany("INSERT INTO rh_fast_decisions VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    return path


def _sent(n, *, reason="filled", fill=None, got=10 ** 24):
    rec = dict(id=n, source_tx="0x%064x" % n, reason=reason, send_reply="accepted",
               our_block=1000 + n, our_tx_index=3)
    if fill is not None:
        rec.update(paid_wei=int(fill * got), got_atomic=got)
    return rec


def _decision(n, *, est=1.0):
    # source average = the true post price (a small buy); chase 30%, premium 1.5
    return ("0x%064x" % n, str(_sqrt_for(TRUE * Fraction(str(est)))), str(SQRT),
            str(int(TRUE * 10 ** 24)), str(10 ** 24), 30.0, 1.5)


def test_a_fill_inside_the_exact_state_cap_passes_and_a_breach_is_a_hard_stop(tmp_path, capsys):
    feed = _feed(tmp_path, [_decision(1), _decision(2)])
    read.pilot_stop_rule([_sent(1, fill=TRUE * Fraction(105, 100)),
                          _sent(2, fill=TRUE * Fraction(132, 100))], [], feed)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("  0x")]
    assert "overpay_post +5.00%" in lines[0] and "overpay_src +5.00%" in lines[0]
    assert "H1 ok" in lines[0] and "block 1001 idx 3" in lines[0]
    assert "H1 BREACH" in lines[1]                    # 1.32 > 1.30 x 1.005
    assert "HARD STOPS: H1 0x0000000000" in out


@pytest.mark.parametrize("est, stop", [(1.015, False), (1.04, True), (0.96, True)])
def test_an_estimate_outside_three_percent_of_the_truth_is_h2(tmp_path, capsys, est, stop):
    feed = _feed(tmp_path, [_decision(1, est=est)])
    read.pilot_stop_rule([_sent(1, reason="reverted")], [], feed)
    out = capsys.readouterr().out
    assert ("est/true %.4f" % est) in out and ("H2" in out) is stop


def test_e1_and_e2_read_at_ten_fills_and_h3_from_the_checks(tmp_path, capsys):
    feed = _feed(tmp_path, [_decision(n) for n in range(1, 22)])
    rows = [_sent(n, fill=TRUE * Fraction(110, 100)) for n in range(1, 11)]
    rows += [_sent(n, reason="reverted") for n in range(11, 22)]
    read.pilot_stop_rule(rows + [dict(_sent(99), send_reply=None, reason="booking_held")],
                         [{"agree": 1}, {"agree": 0}], feed)
    out = capsys.readouterr().out
    assert out.count("E1 at 10 fills: median overpay_src +10.00% vs +9.0%") == 1
    assert "STOP" in out.split("E1 at 10 fills")[1].splitlines()[0]
    assert "E2: reverts 11 of 21 sends (stop at 50%): STOP" in out
    assert "H3 1 held unbooked, 1 fill checks disagree" in out
    assert "reverts 11/21" in out


def test_the_live_row_keeps_our_block_index_and_exact_fill(tmp_path):
    path = str(tmp_path / "lane.db")
    lane.write_live_record(path, dict(source_tx="0xs", tx_hash="0xo", mint="0xm", reason="filled",
                                      send_reply="accepted", our_block=77, our_tx_index=4,
                                      paid_wei=2 * 10 ** 16, got_atomic=5 * 10 ** 24))
    row = sqlite3.connect(path).execute(
        "SELECT our_block, our_tx_index, paid_wei, got_atomic FROM rh_fast_lane_live").fetchone()
    assert row == (77, 4, str(2 * 10 ** 16), str(5 * 10 ** 24))


def test_h1_allows_half_a_percent_of_rounding_over_the_exact_state_cap(tmp_path, capsys):
    feed = _feed(tmp_path, [_decision(1), _decision(2)])
    read.pilot_stop_rule([_sent(1, fill=TRUE * Fraction(1304, 1000)),       # 1.30 x 1.003
                          _sent(2, fill=TRUE * Fraction(1308, 1000))], [], feed)   # 1.30 x 1.006
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("  0x")]
    assert "H1 ok" in lines[0] and "H1 BREACH" in lines[1]


@pytest.mark.parametrize("reverted, verdict", [(10, "STOP"), (9, "pass")])
def test_e2_stops_at_half_the_sends_reverted(tmp_path, capsys, reverted, verdict):
    feed = _feed(tmp_path, [_decision(n) for n in range(1, 21)])
    rows = [_sent(n, reason="reverted") for n in range(1, reverted + 1)]
    rows += [_sent(n, fill=TRUE) for n in range(reverted + 1, 21)]
    read.pilot_stop_rule(rows, [], feed)
    assert ("E2: reverts %d of 20 sends (stop at 50%%): %s" % (reverted, verdict)) in capsys.readouterr().out


def test_a_disagreeing_fill_check_alone_is_h3(tmp_path, capsys):
    feed = _feed(tmp_path, [_decision(1)])
    read.pilot_stop_rule([_sent(1, fill=TRUE)], [{"agree": 0}], feed)
    assert "HARD STOPS: H3 0 held unbooked, 1 fill checks disagree" in capsys.readouterr().out



# ---- Gamma LOWs on 1ba4102eb ----

def test_e1_stops_at_a_median_of_exactly_nine_percent(tmp_path, capsys):
    # the source paid exactly 1e-4 wei per atomic; every fill pays exactly 9% more
    feed = _feed(tmp_path, [("0x%064x" % n, str(SQRT), str(SQRT), str(10 ** 20), str(10 ** 24),
                             30.0, 1.5) for n in range(1, 11)])
    rows = [dict(_sent(n), paid_wei=109 * 10 ** 18, got_atomic=10 ** 24) for n in range(1, 11)]
    read.pilot_stop_rule(rows, [], feed)
    assert "E1 at 10 fills: median overpay_src +9.00% vs +9.0% (stop at or above): STOP" in (
        capsys.readouterr().out)


def _ledger_with(tmp_path, status):
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(str(tmp_path / "ledger.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    pid = db.reserve_pending(conn, mint="0x" + "33" * 20, source_user_id="u", strategy="s",
                             notional=50.0, reserved_at="2026-10-02T00:00:00Z", network_id="4663",
                             signal_source="rh_fast_lane")
    if status != "reserved":
        conn.execute("UPDATE memecoin_pending_entries SET status=? WHERE id=?", (status, pid))
    return conn, pid


@pytest.mark.parametrize("status, age, stop", [("reserved", 900, True), ("reserved", 60, False),
                                               ("confirmed", 900, False)])
def test_a_send_still_unknown_after_recovery_is_h3_and_never_a_revert(tmp_path, capsys, status,
                                                                       age, stop):
    from datetime import datetime, timezone
    ledger, pid = _ledger_with(tmp_path, status)
    feed = _feed(tmp_path, [_decision(1)])
    now = 1_800_000_000.0
    row = dict(_sent(1, reason="submitted_unknown"), pending_id=pid,
               written_at=datetime.fromtimestamp(now - age, timezone.utc).isoformat())
    read.pilot_stop_rule([row], [], feed, ledger=ledger, now=now)
    out = capsys.readouterr().out
    assert "reverts 0/1" in out
    assert ("still submitted_unknown" in out) is stop


# ---- Gamma LOW 2026-10-02: the feed gave up on the truth (post_checked=2); H1 must not pass silently

def _no_truth(n):
    d = list(_decision(n))
    d[2] = None
    return tuple(d)


def test_a_fill_without_truth_prints_h1_unknown_and_still_counts_for_e1(tmp_path, capsys):
    feed = _feed(tmp_path, [_no_truth(1), _decision(2)])
    read.pilot_stop_rule([_sent(1, fill=TRUE * Fraction(105, 100)),
                          _sent(2, fill=TRUE * Fraction(105, 100))], [], feed)
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("  0x")]
    assert "H1 UNKNOWN (no truth)" in lines[0] and "overpay_src +5.00%" in lines[0]
    assert "overpay_post" not in lines[0] and "H1 ok" in lines[1]
    assert "fills with H1 evaluable: 1 of 2" in out
    assert "HARD STOPS: none" in out


def test_a_fill_above_the_source_term_is_an_h1_breach_without_the_truth(tmp_path, capsys):
    # the cap is min(post term, source term) x 1.005: above the source term is above the cap
    feed = _feed(tmp_path, [_no_truth(1)])
    read.pilot_stop_rule([_sent(1, fill=TRUE * Fraction(151, 100))], [], feed)   # > 1.5 x 1.005
    out = capsys.readouterr().out
    assert "H1 BREACH (source term)" in out and "HARD STOPS: H1 0x0000000000" in out
    assert "fills with H1 evaluable: 1 of 1" in out


def test_a_fill_with_no_decision_row_is_counted_unknown(tmp_path, capsys):
    feed = _feed(tmp_path, [])
    read.pilot_stop_rule([_sent(1, fill=TRUE)], [], feed)
    out = capsys.readouterr().out
    assert "H1 UNKNOWN (no decision)" in out and "fills with H1 evaluable: 0 of 1" in out


@pytest.mark.parametrize("total, unknown, flagged", [(10, 3, False), (10, 4, True), (12, 4, False),
                                                     (9, 9, False)])
def test_over_a_third_h1_unknown_at_ten_fills_goes_to_alpha(tmp_path, capsys, total, unknown,
                                                            flagged):
    feed = _feed(tmp_path, [_no_truth(n) if n <= unknown else _decision(n)
                            for n in range(1, total + 1)])
    read.pilot_stop_rule([_sent(n, fill=TRUE) for n in range(1, total + 1)], [], feed)
    out = capsys.readouterr().out
    assert "fills with H1 evaluable: %d of %d" % (total - unknown, total) in out
    assert ("H1 unknown on %d of %d fills, over a third: to Alpha" % (unknown, total)
            in out) is flagged
    assert ("E1 at 10 fills" in out) is (total >= 10)


def test_a_zero_source_average_is_no_source_not_a_crash(tmp_path, capsys):
    d = list(_no_truth(1))
    d[3] = "0"
    read.pilot_stop_rule([_sent(1, fill=TRUE)], [], _feed(tmp_path, [tuple(d)]))
    out = capsys.readouterr().out
    assert "H1 UNKNOWN (no truth)" in out and "overpay_src" not in out
