"""The overnight watch must page on what is actionable and stay quiet on what is configured.

This file runs from the repo checkout on a 15-minute cron, so a commit is LIVE with no deploy gate
between. That makes two failure modes expensive: a page that fires on correct behaviour trains the
operator to ignore the channel, and a section that throws takes the whole report down at the hour
nobody is watching.

No network, no live database.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "overnight_watch", ROOT / "scripts" / "memecoin_overnight_watch.py")
W = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(W)

TRADED = {"4663", "8453", "solana", "56"}


# --- the classifier -----------------------------------------------------------------------------

def test_an_absolute_floor_refusal_is_POLICY_not_UNKNOWN():
    """`source buy $89 under absolute floor $198` is the operator's minimum size doing its job. Its
    MCAP sibling already landed in POLICY, so only this form paged."""
    assert W.classify("source buy $89 under absolute floor $198", "4663", TRADED) == "POLICY"


def test_the_mcap_floor_sibling_still_classifies_POLICY():
    assert W.classify("source buy $69 (0.217%) under mcap floor 0.500% ($160)",
                      "4663", TRADED) == "POLICY"


def test_an_unanticipated_refusal_shape_is_still_UNKNOWN():
    """The whole value of the split is that a shape nobody listed is surfaced, not buried."""
    assert W.classify("the router emitted something nobody has seen", "4663", TRADED) == "UNKNOWN"


def test_proven_slot_expiry_is_known_but_still_pages_for_spent_fees():
    assert W.classify('slot_window_expired', 'solana', TRADED) == 'SYSTEM'
    row = ('entry', '2026-10-19T10:21:36Z', 'coin', 'solana', 'sig', 'slot_window_expired', 'wallet')
    assert W.failed_pages([row])
    assert not W.is_min_out(row)
    assert W.classify('slot_window_expired_unknown', 'solana', TRADED) == 'UNKNOWN'


@pytest.mark.parametrize("reason,network", [
    ("first_source_buy_missed", "solana"),
    ("route quote is 1.24x the post-trade price (cap 1.20x)", "4663"),
])
def test_source_buy_policy_and_post_trade_cap_do_not_page(reason, network):
    assert W.classify(reason, network, TRADED) == "POLICY"


@pytest.mark.parametrize("reason", [
    "first_source_buy_missed_unknown",
    "source_first_buy_unknown",
    "source_buy_identity_unknown",
    "source_buy_cohort_unknown",
    "source_buy_claim_unknown",
    "source_buy_slot_unknown",
    "source_buy_slot_mismatch",
    "missing_receipt_proof",
    "receipt_identity_mismatch",
    "invalid_funded_contract: first_source_buy_missed",
    "route quote is unknownx the post-trade price (cap 1.20x)",
    "route quote is 1.24x the post-trade price (cap unknown)",
    "route quote is 1.24x the post-trade price",
    "unexpected: route quote is 1.24x the post-trade price (cap 1.20x)",
    "route quote is 1.24x the post-trade price (cap 1.20x): unexpected",
])
def test_source_buy_policy_and_post_trade_cap_keep_unknowns_visible(reason):
    assert W.classify(reason, "solana", TRADED) == "UNKNOWN"


@pytest.mark.parametrize("reason", [
    "first_source_buy_missed: provider_timeout",
    "route quote is 1.24x the post-trade price (cap 1.20x): rpc_transport",
    "j7_receipt_state_unavailable",
])
def test_source_buy_policy_and_post_trade_cap_keep_infrastructure_visible(reason):
    assert W.classify(reason, "solana", TRADED) == "SYSTEM"


# --- the fomo/J7 gate ---------------------------------------------------------------------------

def _hb(tmp_path, **over):
    body = {"ts": 1_790_000_000, "auth_ok": True,
            "started_at": "2020-01-01T00:00:00+00:00",
            "fomo_activity_ws": {"status": "disabled", "last_payload_at": None,
                                 "reconnect_count": 0},
            "j7_feed": {"status": "connected", "last_event_at": "2026-09-21T14:13:20+00:00"}}
    body.update(over)
    path = tmp_path / "hb.json"
    path.write_text(json.dumps(body))
    return path


def _silence_alarms(alarms):
    return [a for a in alarms if "SIGNAL FEED SILENT" in a]


def test_a_disabled_fomo_feed_does_not_page_while_J7_delivers(tmp_path):
    """THE PROPERTY. The fomo account is banned and signals arrive over J7, so this is the
    configured state, not a fault."""
    alarms = W.runner_health(_hb(tmp_path), now_s=1_790_000_001)
    assert _silence_alarms(alarms) == [], alarms


def test_BOTH_feeds_silent_still_pages(tmp_path):
    """Suppressing it unconditionally would hide a total signal outage, which is the one case that
    matters most."""
    path = _hb(tmp_path, j7_feed={"status": "disconnected", "last_event_at": None})
    alarms = _silence_alarms(W.runner_health(path, now_s=1_790_000_001))
    assert alarms, "a total outage must page"
    assert "j7_feed" in alarms[0] and "fomo_activity_ws" not in alarms[0]


@pytest.mark.parametrize("accounts,last_event,should_page", [
    ([{"status": "auth_error"}, {"status": "connected"}], "2026-09-21T14:13:20+00:00", False),
    ([{"status": "connected"}, {"status": "auth_error"}], "2026-09-21T14:13:20+00:00", False),
    ([{"status": "auth_error"}, {"status": "disconnected"}], "2026-09-21T14:13:20+00:00", True),
    ([{"status": "connected"}], None, True),
])
def test_disabled_fomo_uses_actual_j7_accounts(tmp_path, accounts, last_event, should_page):
    path = _hb(tmp_path, j7_feed={"status": "degraded", "accounts": accounts,
                                "last_event_at": last_event})
    alarms = _silence_alarms(W.runner_health(path, now_s=1_790_000_001))
    assert bool(alarms) is should_page
    if alarms:
        assert "j7_feed" in alarms[0] and "fomo_activity_ws" not in alarms[0]


@pytest.mark.parametrize("status,accounts", [
    ("connected", None),
    ("degraded", [{"status": "auth_error"}, {"status": "connected"}]),
])
def test_j7_old_delivery_cannot_hide_a_stalled_feed(tmp_path, status, accounts):
    j7 = {"status": status, "last_event_at": "2026-09-21T13:13:20+00:00"}
    if accounts is not None:
        j7["accounts"] = accounts
    alarms = W.runner_health(_hb(tmp_path, j7_feed=j7), now_s=1_790_000_001)
    assert len(alarms) == 1 and "SIGNAL FEED STALE" in alarms[0]
    assert "j7_feed" in alarms[0] and "fomo_activity_ws" not in alarms[0]


def test_a_connected_fomo_feed_with_no_payload_still_pages(tmp_path):
    """Only the DISABLED state is configured. A feed that claims connected and has delivered
    nothing is the publicnode 2026-08-07 case and stays a fault."""
    path = _hb(tmp_path, fomo_activity_ws={"status": "connected", "last_payload_at": None,
                                           "reconnect_count": 3})
    assert _silence_alarms(W.runner_health(path, now_s=1_790_000_001))


# --- outcomes and the loss streak ---------------------------------------------------------------

def _book(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions(network_id TEXT, execution_mode TEXT, "
              "closed_at TEXT, realized_pnl REAL)")
    c.executemany("INSERT INTO memecoin_positions VALUES(?,?,?,?)",
                  [(n, "real", t, p) for n, t, p in rows])
    return c


def test_three_consecutive_losses_on_one_chain_are_counted():
    """The live case this was written against: AI -75.37, BOBCOIN -70.17, Quagsire -2.03."""
    c = _book([("solana", "2026-09-22T19:58:53", -75.37),
               ("solana", "2026-09-22T20:19:30", -70.17),
               ("solana", "2026-09-22T22:29:13", -2.03)])
    _win, _day, run = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    assert run["solana"] == 3


def test_a_win_RESETS_the_streak():
    """A run broken by a win is not a streak. Counting losses in a window instead would page on a
    profitable night that happened to contain three losers."""
    c = _book([("solana", "2026-09-22T19:00:00", -10.0),
               ("solana", "2026-09-22T19:30:00", -10.0),
               ("solana", "2026-09-22T20:00:00", +50.0),
               ("solana", "2026-09-22T20:30:00", -10.0)])
    _w, _d, run = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    assert run["solana"] == 1


def test_streaks_are_per_chain_and_do_not_interleave():
    """Losses on two chains alternating are not a streak on either."""
    c = _book([("solana", "2026-09-22T19:00:00", -10.0),
               ("4663", "2026-09-22T19:10:00", -10.0),
               ("solana", "2026-09-22T19:20:00", -10.0),
               ("4663", "2026-09-22T19:30:00", -10.0)])
    _w, _d, run = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    assert run["solana"] == 2 and run["4663"] == 2


def test_the_24h_net_is_summed_across_chains():
    c = _book([("solana", "2026-09-22T19:00:00", -200.0),
               ("4663", "2026-09-22T19:10:00", +40.0)])
    _w, day, _r = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    assert round(sum(v["pnl"] for v in day.values()), 2) == -160.0, day
    assert day["solana"]["l"] == 1 and day["4663"]["w"] == 1


def test_a_zero_pnl_close_is_neither_a_win_nor_a_loss():
    """A flat close must not extend a loss streak or inflate a win rate."""
    c = _book([("solana", "2026-09-22T19:00:00", -5.0),
               ("solana", "2026-09-22T19:10:00", 0.0)])
    _w, day, run = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    assert day["solana"]["w"] == 0 and day["solana"]["l"] == 1
    assert run["solana"] == 0, "a flat close breaks the run rather than extending it"


# --- tracebacks ---------------------------------------------------------------------------------

def test_only_NEW_tracebacks_are_reported(tmp_path):
    log = tmp_path / "runner.log"
    state = tmp_path / "watch.state"
    log.write_text("fine\nTraceback (most recent call last):\n  File x\nValueError: boom\n")
    first, errs = W.new_tracebacks([str(log)], str(state))
    assert len(first) == 1 and "File x" in first[0][1] and not errs
    again, _ = W.new_tracebacks([str(log)], str(state))
    assert again == [], "the same traceback must not page twice"
    with open(log, "a") as fh:
        fh.write("Traceback (most recent call last):\n  File y\n")
    third, _ = W.new_tracebacks([str(log)], str(state))
    assert len(third) == 1 and "File y" in third[0][1]


def test_a_rotated_log_is_read_from_the_start_not_skipped(tmp_path):
    """After rotation the file is SHORTER than the stored offset. Seeking to it would read nothing
    and a crash in the fresh file would never be seen."""
    log = tmp_path / "runner.log"
    state = tmp_path / "watch.state"
    log.write_text("x" * 5000 + "\n")
    W.new_tracebacks([str(log)], str(state))
    log.write_text("Traceback (most recent call last):\n  File fresh\n")
    found, _ = W.new_tracebacks([str(log)], str(state))
    assert len(found) == 1 and "fresh" in found[0][1]


def test_a_missing_log_is_not_an_error(tmp_path):
    found, errs = W.new_tracebacks([str(tmp_path / "absent.log")], str(tmp_path / "s.json"))
    assert found == [] and errs == []


# --- the loss-streak latch ----------------------------------------------------------------------
#
# "3 consecutive losses on solana" paged on EVERY 15-minute run from 23:30Z. A dozen copies of one
# fact teaches the operator to ignore the channel, which costs more than the missing page would.

def _streak_book(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_positions(network_id TEXT, execution_mode TEXT, "
              "closed_at TEXT, realized_pnl REAL)")
    c.executemany("INSERT INTO memecoin_positions VALUES(?,?,?,?)",
                  [(n, "real", t, p) for n, t, p in rows])
    return c


THREE = [("solana", "2026-09-22T19:58:53", -75.37),
         ("solana", "2026-09-22T20:19:30", -70.17),
         ("solana", "2026-09-22T22:29:13", -2.03)]


def _pages(c, state):
    _w, _d, run = W.outcomes(c, "2026-09-22T00:00:00", "2026-09-22T00:00:00")
    return W.streak_pages(run, W.loss_streak_starts(c, "2026-09-22T00:00:00"), str(state))


def test_a_formed_streak_pages_once_then_stays_silent(tmp_path):
    """THE PROPERTY THIS EXISTS FOR."""
    state = tmp_path / "watch.state"
    c = _streak_book(THREE)
    assert _pages(c, state) == ["3 consecutive losses on solana"]
    assert _pages(c, state) == [], "the same streak must not page twice"
    assert _pages(c, state) == []


def test_a_growing_streak_pages_again(tmp_path):
    state = tmp_path / "watch.state"
    assert _pages(_streak_book(THREE), state) == ["3 consecutive losses on solana"]
    grown = _streak_book(THREE + [("solana", "2026-09-22T22:45:00", -11.0)])
    assert _pages(grown, state) == ["4 consecutive losses on solana"]


def test_a_streak_that_breaks_and_rebuilds_pages_again(tmp_path):
    """Latching on LENGTH alone would silence this forever. Three losses, a win, three more losses
    is a new event, not a repeat -- so the latch key is the streak's first loss, not its size."""
    state = tmp_path / "watch.state"
    assert _pages(_streak_book(THREE), state) == ["3 consecutive losses on solana"]
    rebuilt = _streak_book(THREE + [
        ("solana", "2026-09-22T22:40:00", +40.0),
        ("solana", "2026-09-22T22:50:00", -5.0),
        ("solana", "2026-09-22T22:55:00", -6.0),
        ("solana", "2026-09-22T23:00:00", -7.0)])
    assert _pages(rebuilt, state) == ["3 consecutive losses on solana"], \
        "a rebuilt streak is a different streak"


def test_a_chain_under_the_threshold_never_pages(tmp_path):
    state = tmp_path / "watch.state"
    c = _streak_book(THREE[:2])
    assert _pages(c, state) == []


def test_the_latch_keeps_the_traceback_offsets_in_the_shared_state(tmp_path):
    """Both readers load-modify-save the same file; neither may drop the other's keys."""
    state = tmp_path / "watch.state"
    log = tmp_path / "runner.log"
    log.write_text("Traceback (most recent call last):\n  File x\n")
    W.new_tracebacks([str(log)], str(state))
    _pages(_streak_book(THREE), state)
    assert json.loads(state.read_text()).get(str(log)) is not None, "offset survived the latch"
    again, _ = W.new_tracebacks([str(log)], str(state))
    assert again == [], "the traceback offset still suppresses a repeat"


def test_quiet_hours_is_POLICY_not_UNKNOWN():
    """The operator's configured trading window refusing a signal outside it."""
    assert W.classify("quiet_hours", "56", TRADED) == "POLICY"


# --- the classifier's underscore blind spot, and the nine phrases it hid ------------------------
#
# Found by enumeration on 2026-09-23: every refusal shape the live book produced in 7 days, run
# through classify(). 23 landed in UNKNOWN, and UNKNOWN feeds the ACTIONABLE telegram page, so all
# of them were paging.

@pytest.mark.parametrize("reason", [
    "no quote (provider_poolkey_read_failed: v3 could not read: poolkey_read_failed)",
    "no quote (provider_rpc_rate_limit: v3 could not read: rpc_rate_limit)",
    "no_route_all_venues",
])
def test_an_underscore_joined_fault_is_SYSTEM_not_UNKNOWN(reason):
    """THE BUG. `\\b` treats '_' as a word character, so `\\bprovider\\b` never matched
    `provider_poolkey_read_failed` and 33 real faults a week reported as unexplained."""
    assert W.classify(reason, "4663", TRADED) == "SYSTEM"


def test_the_existing_underscore_workaround_still_classifies():
    """`rpc_transport` was in SYSTEM as a literal token -- a workaround for this same anchor. The
    fix must not regress what the workaround was covering."""
    assert W.classify("no quote (provider_rpc_transport: v3 could not read: rpc_transport)",
                      "4663", TRADED) == "SYSTEM"


@pytest.mark.parametrize("reason", [
    "round trip 12% over 10% cap (sell back $88 of $100)",
    "no quote (route_over_budget: v3 priced this route and declined it: round_trip_over_budget:-16)",
    "drawdown pause: today's realized PnL fell $214 below the day's high; automatic entries resume",
    "kill_switch_tripped",
    "route quote is 3.06x the chart mid (cap 1.95x)",
    "entry older than 60s",
    "coin_too_young: 41s old, floor 120s",
    "price impact 14% over 10% cap",
    "insufficient quote balance for chain's native-is-quote gas reserve (short $2 of the $9 req)",
])
def test_an_operator_configured_limit_is_POLICY(reason):
    """Each names its own configured ceiling or rail. Paging on these trains the operator to
    ignore the channel -- 173 occurrences in one week."""
    assert W.classify(reason, "4663", TRADED) == "POLICY"


@pytest.mark.parametrize("reason", [
    "route build refused: swap build returned nothing",
    "entry_simulation_reverted: exact route reverted; detail=Call failed",
    "sell_sim_min_out_repeated: fresh funded sell simulation still hit router min-out",
])
def test_a_genuine_fault_shape_is_still_surfaced(reason):
    """The bucket exists to surface what nobody anticipated. Widening POLICY must not swallow
    these -- they stay visible as UNKNOWN or SYSTEM, never as policy."""
    assert W.classify(reason, "4663", TRADED) != "POLICY"


def test_policy_patterns_with_underscores_still_match_after_the_normalisation():
    """POLICY matches the RAW string on purpose: normalising underscores there would break every
    pattern that contains one."""
    for reason in ("not_approved", "quiet_hours", "coin_too_young: 41s old, floor 120s",
                   "kill_switch_tripped"):
        assert W.classify(reason, "4663", TRADED) == "POLICY", reason
