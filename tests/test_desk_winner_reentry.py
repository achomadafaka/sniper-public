"""Desk winner re-entry (operator 2026-10-23): only a verified winning close may skip the cooldown."""
import json

import pytest

from sniper.persistence import db_memecoin_desk as desk
from sniper.services import memecoin_desk as service
from sniper.services.memecoin_desk_universe import WinnerReentry
from test_memecoin_desk_store import at, candidate, close_stage, opening, stage

EXIT = 3.25374346007519e-05
SIG = "close-sig"


RULE = {"version": "winner-reentry-v1", "per_hour": 2, "window_s": 1800}


@pytest.fixture
def conn(tmp_path):
    c = desk.connect(tmp_path / "desk.sqlite")
    desk.register_cohort(c, cohort_id="cohort-U", producer="U", prompt_version="desk-judge-v6",
                         policy={"clip_usd": 25, "max": 5, "winner_reentry": RULE}, created_at=at(0))
    yield c
    c.close()


def closed(conn, *, pnl=2.56, exit_price=EXIT):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    close_stage(conn, output={"state": "closed", "position_id": 42, "is_open": 0, "closed_at": at(100),
                              "realized_pnl": pnl, "exit_price_usd": exit_price})


def reentry(conn, key, seconds, *, pid=42, price=EXIT, enabled=True, now=None):
    cand = {**candidate(key, seconds=seconds), "reentry": {"prev_position_id": pid, "exit_price_usd": price,
                                                          "trigger": "above_exit"}}
    return desk.record_candidate(conn, cand, cohort_id="cohort-U", now_iso=at(seconds if now is None else now),
                                 cooldown_s=900, reentry_enabled=enabled)


def test_winning_close_skips_cooldown_once(conn):
    closed(conn)
    assert opening(conn, "u-plain", seconds=120)["reason"] == "cooldown"
    assert reentry(conn, "u-re", 120)["is_primary"] is True


@pytest.mark.parametrize("pnl", [-1.0, 0.0, None])
def test_losing_flat_or_unknown_close_keeps_cooldown(conn, pnl):
    closed(conn, pnl=pnl)
    assert reentry(conn, "u-re", 120)["is_primary"] is False


def test_open_position_never_rearms(conn):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    assert reentry(conn, "u-re", 120)["is_primary"] is False


@pytest.mark.parametrize("kwargs", [{"pid": 41}, {"price": EXIT * 0.9}])
def test_claim_must_match_the_recorded_close(conn, kwargs):
    closed(conn)
    assert reentry(conn, "u-re", 120, **kwargs)["reason"] == "reentry_unproven"


@pytest.mark.parametrize("seconds", [90, 100, 100 + 1801])   # before close, at close, past the window
def test_trigger_must_follow_the_close_inside_the_window(conn, seconds):
    closed(conn)
    assert reentry(conn, "u-re", seconds, now=max(seconds, 101))["reason"] in ("reentry_window", "active_ticket")


@pytest.mark.parametrize("seconds", [120, 1100])          # inside and after the ordinary cooldown
def test_knob_off_refuses_a_tagged_candidate(conn, seconds):
    closed(conn)
    assert reentry(conn, "u-re", seconds, enabled=False)["reason"] == "reentry_disabled"


def test_cohort_without_frozen_rule_refuses(tmp_path):
    c = desk.connect(tmp_path / "plain.sqlite")
    desk.register_cohort(c, cohort_id="cohort-U", producer="U", prompt_version="desk-judge-v6",
                         policy={"clip_usd": 25, "max": 5}, created_at=at(0))
    closed(c)
    assert reentry(c, "u-re", 120)["reason"] == "reentry_disabled"


def test_cap_counts_reentry_admissions_from_the_frozen_rule(conn):
    closed(conn)
    first = reentry(conn, "u-re", 120)
    assert first["is_primary"] is True
    rows = conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions WHERE is_primary=1 "
                        "AND json_extract(candidate_json,'$.reentry') IS NOT NULL").fetchone()[0]
    assert rows == 1


def test_tagged_candidate_without_a_prior_ticket_is_refused(conn):
    with pytest.raises(ValueError, match="reentry_no_prior"):
        reentry(conn, "u-re", 120)


def test_untagged_candidate_is_unchanged(conn):
    closed(conn)
    assert opening(conn, "u-plain", seconds=1100)["is_primary"] is True


def test_one_chance_per_close_survives_restart(conn):
    closed(conn)
    first = reentry(conn, "u-re", 120)
    stage(conn, first["ticket_id"], stage="hard_filter", seat="code", verdict="KILL", reason="x",
          ts=at(121), response_at=None, ticket_state="refused")
    assert reentry(conn, "u-re2", 200)["reason"] in ("reentry_consumed", "reentry_not_closed")


def fills(price=EXIT, sig=SIG, kind="bracket_exit", extra=(), qty=847008.970995, entry_qty=847008.970995,
          source="chain"):
    return [{"kind": "entry", "price": 2.9e-5, "tx_sig": "buy", "qty": entry_qty, "notional": 25.0,
             "fill_source": "chain"},
            {"kind": kind, "price": price, "tx_sig": sig, "qty": qty, "notional": 27.559499,
             "fill_source": source}, *extra]


def position(**kw):
    return {"realized_pnl": 2.56, "close_sig": SIG, "close_pending_at": None, "close_claim_id": None,
            "derisk_pending_at": None, "derisk_claim_id": None, "derisked_at": None, **kw}


def test_exit_price_is_the_bound_full_exit_fill():
    assert service._close_outcome(position(), fills())["exit_price_usd"] == EXIT


@pytest.mark.parametrize("pos,fs", [
    (position(), fills(sig="other")),                                     # orphan / unbound fill
    (position(), fills(price=None)),                                      # unpriced
    (position(), fills(price=0.0)),
    (position(close_sig=None), fills()),
    (position(), fills(extra=[{"kind": "derisk", "price": 4e-5, "tx_sig": "p"}])),   # partial history
    (position(derisked_at="t"), fills()),
    (position(close_claim_id="c"), fills()),                              # in-flight claim
    (position(), [{"kind": "entry", "price": 2.9e-5, "tx_sig": "buy"}]),  # no exit fill
    (position(), fills(qty=400000.0)),                                    # exit retires part of the bag
    (position(), fills(qty=None)),
    (position(), fills(source="manual")),                                 # not a confirmed chain fill
    (position(), fills(entry_qty=None)),
])
def test_unsupported_exit_histories_never_arm(pos, fs):
    assert service._close_outcome(pos, fs)["exit_price_usd"] is None


def test_arm_fires_once_above_exit_and_fails_closed():
    arm = WinnerReentry(window_s=1800, stale_s=30)
    close = '{"realized_pnl": 2.56, "exit_price_usd": %r, "position_id": 42, ' \
            '"closed_at": "2026-10-23T12:45:44+00:00"}' % EXIT
    t = 1792759544.0 + 10
    arm.load([("M", close)], t)
    after, before = t, 1792759544.0 - 5
    assert arm.check("M", EXIT, after) is None                # equal is not above
    assert arm.check("M", EXIT * 1.5, before) is None         # pre-close trade seen late never fires
    claim = arm.check("M", EXIT * 1.01, after)
    assert claim == {"prev_position_id": 42, "exit_price_usd": EXIT, "trigger": "above_exit"}
    assert arm.check("M", EXIT * 2, after) is None            # spent
    arm.load([("N", close.replace("2.56", "-1.0"))], t)
    assert arm.pending("N") is None                           # loser never arms
    arm.load([("N", close)], t)
    arm.failed(t + 31)
    assert arm.pending("N") is None                           # stale read empties the list


def prior_reentries(conn, *ages):
    """Earlier re-entry admissions on the mint (other closes), received `age` seconds before t=120."""
    conn.execute("INSERT OR IGNORE INTO desk_snapshots VALUES ('h', '{}')")
    for i, age in enumerate(ages):
        conn.execute("INSERT INTO desk_candidate_emissions (opportunity_id,ticket_id,producer,cohort_id,network_id,"
                     "mint,pool,emitted_at,received_at,snapshot_hash,candidate_json,is_primary,reason) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,1,NULL)",
                     (f"old-{i}", "u-1", "U", "cohort-U", "solana", "MintA", "CurveA", at(120 - age),
                      at(120 - age), "h", json.dumps({"reentry": {"prev_position_id": 900 + i}})))
    conn.commit()


@pytest.mark.parametrize("ages,primary", [((3000,), True), ((3000, 1000), False), ((3601, 3700), True),
                                          ((3599, 3601), True)])
def test_two_per_rolling_hour(conn, ages, primary):
    closed(conn)
    prior_reentries(conn, *ages)
    result = reentry(conn, "u-re", 120)
    assert result["is_primary"] is primary
    if not primary:
        assert result["reason"] == "reentry_cap"


RULE2 = {"version": "winner-reentry-v2", "per_hour": 2, "window_s": 1800, "loss_soft": True}
ENTRY = 1.67e-05


def soft_conn(tmp_path, rule=RULE2):
    c = desk.connect(tmp_path / "soft.sqlite")
    desk.register_cohort(c, cohort_id="cohort-U", producer="U", prompt_version="desk-judge-v6",
                         policy={"clip_usd": 25, "max": 5, "winner_reentry": rule}, created_at=at(0))
    return c


def soft_close(conn, *, reason="desk_risk_exit", pnl=-2.47):
    opening(conn)
    stage(conn, stage="fill", seat="engine", verdict="PASS", output={"state": "filled"}, position_id=42)
    close_stage(conn, output={"state": "closed", "position_id": 42, "is_open": 0, "closed_at": at(100),
                              "realized_pnl": pnl, "exit_price_usd": ENTRY * 0.92, "exit_reason": reason,
                              "entry_fill_price_usd": ENTRY})


def test_soft_loss_reclaim_of_our_entry_rearms_under_v2(tmp_path):
    """Operator 2026-10-23 (74159: desk_risk_exit -2.47, back above the 16.7k entry with net buy flow)."""
    c = soft_conn(tmp_path)
    soft_close(c)
    assert reentry(c, "u-re", 120, price=ENTRY)["is_primary"] is True


@pytest.mark.parametrize("reason", ["b_stop", "catastrophe", "vol_death", None])
def test_hard_loss_never_rearms(tmp_path, reason):
    c = soft_conn(tmp_path)
    soft_close(c, reason=reason)
    assert reentry(c, "u-re", 120, price=ENTRY)["reason"] == "reentry_not_winner"


def test_v1_cohort_refuses_a_soft_loss(tmp_path):
    c = soft_conn(tmp_path, rule=RULE)
    soft_close(c)
    assert reentry(c, "u-re", 120, price=ENTRY)["reason"] == "reentry_not_winner"


def test_soft_arm_needs_positive_known_net_flow():
    arm = WinnerReentry(window_s=1800)
    close = json.dumps({"realized_pnl": -2.47, "exit_price_usd": ENTRY * 0.92, "position_id": 42,
                        "closed_at": "2026-10-23T12:45:44+00:00", "exit_reason": "desk_risk_exit",
                        "entry_fill_price_usd": ENTRY})
    t = 1792759544.0 + 10
    arm.load([("M", close)], t)
    assert arm.check("M", ENTRY * 1.01, t, None) is None and arm.check("M", ENTRY * 1.01, t, -5.0) is None
    assert arm.check("M", ENTRY * 1.01, t, 120.0)["exit_price_usd"] == ENTRY
    arm.load([("N", close.replace("desk_risk_exit", "b_stop"))], t)
    assert arm.pending("N") is None


def test_tripwire_trips_on_ten_bad_loss_reentries(tmp_path, monkeypatch):
    c = soft_conn(tmp_path)
    monkeypatch.setattr(desk, "_tripwire_tripped", lambda conn, cid: True)
    soft_close(c)
    assert reentry(c, "u-re", 120, price=ENTRY)["reason"] == "reentry_tripwire"


def test_tripwire_math(monkeypatch):
    pnl = {"f0": 2.0, "f1": 2.0, "f2": 2.0, "loss": -3.0, **{"r%d" % i: -4.0 for i in range(10)}}
    monkeypatch.setattr(desk, "_close_pnl", lambda conn, tid: pnl.get(tid))
    rows = [("f%d" % i, None) for i in range(3)] + [("r%d" % i, 500 + i) for i in range(10)]

    class Conn:
        def execute(self, sql, args=()):
            class R:
                def __init__(self, v): self.v = v
                def fetchall(self): return self.v
                def fetchone(self): return self.v[0] if self.v else None
            return R(rows if "JOIN desk_candidate_emissions" in sql else [("loss",)])
    assert desk._tripwire_tripped(Conn(), "cohort-U") is True      # mean -4.0 < first mean 2.0 - 4.5
    pnl.update({"r%d" % i: 0.0 for i in range(10)})
    assert desk._tripwire_tripped(Conn(), "cohort-U") is False     # 0.0 >= -2.5
    pnl.update({"r9": None})
    assert desk._tripwire_tripped(Conn(), "cohort-U") is False     # 9 closed: below the minimum
