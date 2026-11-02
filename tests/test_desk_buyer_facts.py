"""Desk buyers fact (cohort C prereg b90ddeb59 1a, Amendment 1 226c0144e): as-of joins only, unknown is a reason,
never zero; lower bound unless the population window is proven captured."""
from datetime import datetime, timezone

from sniper.services import memecoin_desk_buyer_facts as B
from sniper.services import memecoin_desk_roster_history as R
from test_desk_ps_model_facts import CUT, NOW, SOL, tape

ARMED = (0, 0, 1, "solana")


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def setup(tmp_path, *, polls=(NOW - 300,), roster=None, registry=None, closes=None, create_slot=5):
    import sqlite3
    from sniper.services import memecoin_desk_ps_tape as PT
    c, path = tape(tmp_path)
    if create_slot is not None:            # the canonical create_pool row: the exact population floor
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE pools(pool TEXT, mint TEXT, quote_mint TEXT, canonical INTEGER, received_at REAL, slot INTEGER)")
            db.execute("INSERT INTO pools VALUES(?,?,?,1,?,?)", (c["pool"], c["mint"], PT.WSOL_MINT, NOW - 3600, create_slot))
    store = R.open_store(tmp_path / "roster.db")
    last = None
    for at in polls:
        last = R.poll_once(store, roster or {"uid1": ARMED}, at, last)
    ledger = {"registry": registry if registry is not None else [("buyer", "uid1", "verified", iso(NOW - 400))],
              "closes": closes if closes is not None else {"uid1": [(iso(NOW - 500), "other", 1.0)] * 5}}
    return c, store, ledger


def facts(tmp_path, c, store, ledger, *, created=NOW - 3600):
    return B.buyers_for(mint=c["mint"], pool=c["pool"], receipt_cut=NOW, cut_slot=CUT, sol_usd=SOL,   # fixture slots 10..15
                        pool_created_at=created, desk_dir=tmp_path, ledger=ledger, roster_store=store,
                        ours=frozenset(("ours",)))


def test_population_exclusions_roster_and_grade(tmp_path):
    c, store, ledger = setup(tmp_path, polls=[NOW - 300 + 30 * k for k in range(10)])
    f = facts(tmp_path, c, store, ledger)
    assert f["state"] == "known" and f["definition_sha256"] == B.DEFINITION_SHA256
    assert f["buyers_n"] == 3 and f["excluded_n"] == 2          # old, buyer, buyer2; agent and ours excluded
    assert f["roster"] == {"armed": 1, "followed": 0, "excluded": 0, "state_unknown": 0, "unknown": 2}
    assert f["real_pnl_grade"] == {"pos": 1, "neg": 0, "insufficient": 0} and f["graded_n"] == 1
    assert f["followed_n"] == f["known_n"] == 1 and f["first_seen_followed_s"] == 3600 - 55
    assert f["lower_bound"] is False and f["roster_verified_n"] == 1


def test_cut_before_the_roster_history_or_in_a_poll_gap_is_unknown_for_the_whole_fact(tmp_path):
    c, store, ledger = setup(tmp_path, polls=(NOW + 1,))
    assert facts(tmp_path, c, store, ledger) == {"state": "unknown", "reason": "grade_asof_unknown", "version": B.VERSION}
    c, store, ledger = setup(tmp_path / "gap", polls=(NOW - 300,))
    assert facts(tmp_path / "gap", c, store, ledger)["reason"] == "roster_unknown"


def test_a_registry_verification_or_a_close_after_the_cut_is_not_used(tmp_path):
    polls = [NOW - 300 + 30 * k for k in range(10)]
    late = [(iso(NOW + 10), "other", 1.0)] * 5 + [(iso(NOW - 500), None, 1.0)] * 4
    c, store, ledger = setup(tmp_path, polls=polls, registry=[("buyer", "uid1", "verified", iso(NOW + 5))])
    assert facts(tmp_path, c, store, ledger)["roster"]["unknown"] == 3
    c, store, ledger = setup(tmp_path / "b", polls=polls, closes={"uid1": late})
    assert facts(tmp_path / "b", c, store, ledger)["real_pnl_grade"]["insufficient"] == 1


def test_this_mints_own_closes_never_grade_the_wallet(tmp_path):
    c, _ = tape(tmp_path / "probe")
    polls = [NOW - 300 + 30 * k for k in range(10)]
    c, store, ledger = setup(tmp_path, polls=polls, closes={"uid1": [(iso(NOW - 500), c["mint"], 1.0)] * 5})
    assert facts(tmp_path, c, store, ledger)["real_pnl_grade"]["insufficient"] == 1


def test_unreadable_ledger_and_unknown_create_are_never_zero(tmp_path):
    c, store, ledger = setup(tmp_path, polls=[NOW - 300 + 30 * k for k in range(10)])
    assert facts(tmp_path, c, store, OSError("busy"))["reason"] == "ledger_unreadable"
    f = facts(tmp_path, c, store, ledger, created=None)
    assert f["lower_bound"] is True and f["first_seen_followed_s"] is None and f["buyers_n"] == 3


def test_an_older_pool_measures_first_seen_from_the_window_start(tmp_path):
    c, store, ledger = setup(tmp_path, polls=[NOW - 300 + 30 * k for k in range(10)])
    f = facts(tmp_path, c, store, ledger, created=NOW - 7200)
    assert f["lower_bound"] is True and f["window_s"] == B.MAX_SPAN_S
    assert f["first_seen_basis"] == "window_start" and f["first_seen_followed_s"] == 3600 - 55
    young = facts(tmp_path, c, store, ledger, created=NOW - 100)
    assert young["first_seen_basis"] == "create_block_time" and young["first_seen_followed_s"] == 100 - 55



def test_without_a_create_slot_the_heuristic_floor_is_only_a_lower_bound(tmp_path):
    c, store, ledger = setup(tmp_path, polls=[NOW - 300 + 30 * k for k in range(10)], create_slot=None)
    assert facts(tmp_path, c, store, ledger)["lower_bound"] is True


def test_a_buyer_far_below_the_heuristic_floor_is_counted_or_censored(tmp_path):
    # Delta witness on 2233b7723: a fresh receipt with an old slot was dropped while lower_bound stayed False
    import sqlite3
    for create_slot in (5, None):
        d = tmp_path / str(create_slot)
        c, store, ledger = setup(d, polls=[NOW - 300 + 30 * k for k in range(10)], create_slot=create_slot)
        with sqlite3.connect(d / "pumpswap" / "tape.db") as db:
            db.execute("UPDATE swaps_ps SET slot=slot+99000")
            db.execute("UPDATE swaps_ps SET slot=80000 WHERE actor='buyer'")
        f = B.buyers_for(mint=c["mint"], pool=c["pool"], receipt_cut=NOW, cut_slot=100000, sol_usd=SOL,
                         pool_created_at=NOW - 3600, desk_dir=d, ledger=ledger, roster_store=store, ours=frozenset(("ours",)))
        assert f["buyers_n"] == 3 if create_slot is not None else f["lower_bound"] is True, f
