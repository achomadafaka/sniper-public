"""The copier-fee study's source matching and source-relative numbers (Codex review S27-06/07/08).

S27-06: the heuristic source window compared ISO text holding 'T' with SQLite datetime() text
holding a space, so any same-day detection passed its lower bound (an 00:00 detection matched an
08:00 fill). S27-07: a missing source became our own slot and "0 slots behind". S27-08: buyers
must be decoded buys, and the reported CU price is an observation, not a leading price.
"""
import sqlite3

import pytest

from scripts.research import copier_crowd_and_fees as C

FILL = "2026-09-27T08:00:00.000000+00:00"


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE onchain_wallet_detections (signature TEXT, source_user_id TEXT, "
              "mint TEXT, detected_at TEXT)")
    yield c
    c.close()


def _det(conn, sig, detected_at, mint="M", uid="u"):
    conn.execute("INSERT INTO onchain_wallet_detections VALUES (?,?,?,?)", (sig, uid, mint, detected_at))


def test_an_eight_hour_old_same_day_detection_is_not_in_a_180_second_window(conn):
    _det(conn, "old", "2026-09-27T00:00:00.000000+00:00")
    assert C.heuristic_source(conn, "M", "u", FILL) == (None, 0)


def test_the_window_is_inclusive_at_exactly_180_seconds_and_excludes_181(conn):
    _det(conn, "edge", "2026-09-27T07:57:00.000000+00:00")
    _det(conn, "out", "2026-09-27T07:56:59.000000+00:00")
    assert C.heuristic_source(conn, "M", "u", FILL) == ("edge", 1)


def test_a_window_across_midnight(conn):
    _det(conn, "late", "2026-09-26T23:59:30+00:00")
    assert C.heuristic_source(conn, "M", "u", "2026-09-27T00:01:00+00:00") == ("late", 1)


def test_equivalent_offsets_and_a_space_separator_compare_as_instants(conn):
    _det(conn, "cet", "2026-09-27T09:59:00+02:00")            # 07:59:00Z
    _det(conn, "naive", "2026-09-27 07:58:30")                  # SQLite datetime('now') shape, UTC
    sig, n = C.heuristic_source(conn, "M", "u", FILL)
    assert (sig, n) == ("cet", 2)


def test_missing_and_malformed_timestamps_never_match(conn):
    _det(conn, "bad", "not a time")
    _det(conn, "empty", "")
    assert C.heuristic_source(conn, "M", "u", FILL) == (None, 0)
    assert C.heuristic_source(conn, "M", "u", None) == (None, 0)


def test_two_matching_buys_pick_the_latest_and_say_there_were_two(conn):
    _det(conn, "first", "2026-09-27T07:58:00+00:00")
    _det(conn, "second", "2026-09-27T07:59:30+00:00")
    assert C.heuristic_source(conn, "M", "u", FILL) == ("second", 2)


def test_a_detection_after_our_fill_is_not_its_source(conn):
    _det(conn, "after", "2026-09-27T08:00:01+00:00")
    assert C.heuristic_source(conn, "M", "u", FILL) == (None, 0)


def test_other_wallets_and_other_mints_do_not_match(conn):
    _det(conn, "w", "2026-09-27T07:59:00+00:00", uid="someone_else")
    _det(conn, "m", "2026-09-27T07:59:00+00:00", mint="OTHER")
    assert C.heuristic_source(conn, "M", "u", FILL) == (None, 0)


# --- S27-07 / S27-08: fees_one ---------------------------------------------------------------------

def _tx(sig, slot, index, err=None):
    return {"slot": slot, "transactionIndex": index, "transaction": {"signatures": [sig]},
            "meta": {"err": err}}


@pytest.fixture
def chain(monkeypatch):
    state = {"txs": {}, "window": [], "sides": {}}
    monkeypatch.setattr(C.dfx, "get_tx", lambda sig: state["txs"].get(sig))
    monkeypatch.setattr(C, "mint_txs", lambda mint, lo, hi: [
        t for t in state["window"] if lo <= t["slot"] <= hi])
    monkeypatch.setattr(C.dfx, "trade_of", lambda tx, mint, usd: (
        {"side": state["sides"][tx["transaction"]["signatures"][0]], "sol": 1.0}
        if tx["transaction"]["signatures"][0] in state["sides"] else None))
    monkeypatch.setattr(C, "fee_facts", lambda tx: {"cu_price": 1000, "tip_lamports": 0})
    return state


def _fill(source):
    return {"position_id": 1, "mint": "M", "ours": "us", "source": source}


def test_no_identified_source_reports_no_lag_and_no_crowd(chain):
    chain["txs"]["us"] = {"slot": 100}
    chain["window"] = [_tx("x", 100, 1), _tx("us", 100, 5)]
    chain["sides"]["x"] = "buy"
    r = C.fees_one(_fill(None))
    assert r["status"] == "source_not_identified"
    assert r["slots_behind"] is None and r["source_slot"] is None and r["ahead_n"] is None
    assert r["our_slot"] == 100 and r["our_index"] == 5 and r["same_slot_buys_before_us_n"] == 1


def test_an_identified_source_that_cannot_be_read_is_not_zero_lag(chain):
    chain["txs"]["us"] = {"slot": 100}
    chain["window"] = [_tx("us", 100, 5)]
    r = C.fees_one(_fill("src"))
    assert r["status"] == "source_not_found" and r["slots_behind"] is None


def test_a_real_same_slot_fill_keeps_its_zero(chain):
    chain["txs"].update(us={"slot": 100}, src={"slot": 100})
    chain["window"] = [_tx("src", 100, 2), _tx("c", 100, 3), _tx("us", 100, 9)]
    chain["sides"]["c"] = "buy"
    r = C.fees_one(_fill("src"))
    assert r["status"] == "ok" and r["slots_behind"] == 0 and r["ahead_n"] == 1


def test_a_source_that_landed_after_our_fill_is_refused(chain):
    chain["txs"].update(us={"slot": 100}, src={"slot": 101})
    chain["window"] = [_tx("us", 100, 5)]
    assert C.fees_one(_fill("src"))["status"] == "source_after_ours"


def test_an_empty_window_is_not_a_source(chain):
    chain["txs"].update(us={"slot": 100}, src={"slot": 99})
    chain["window"] = []
    r = C.fees_one(_fill("src"))
    assert r["status"] == "source_not_in_window" and r["ahead_n"] is None


def test_a_provider_failure_stops_the_run(chain, monkeypatch):
    def boom(sig):
        raise C.fet.Stop("HTTP 500")
    monkeypatch.setattr(C.dfx, "get_tx", boom)
    with pytest.raises(C.fet.Stop):
        C.fees_one(_fill("src"))


def test_buyers_before_us_are_decoded_buys_not_every_transaction(chain):
    chain["txs"].update(us={"slot": 100}, src={"slot": 99})
    chain["window"] = [_tx("src", 99, 4), _tx("b", 100, 1), _tx("s", 100, 2), _tx("n", 100, 3),
                       _tx("us", 100, 7)]
    chain["sides"].update(b="buy", s="sell")
    r = C.fees_one(_fill("src"))
    assert r["same_slot_txs_before_us_n"] == 3 and r["same_slot_buys_before_us_n"] == 1
    assert "min_cu_price_to_lead_slot" not in r
    assert r["highest_observed_cu_price_before_us"] == 1000


def test_a_non_sol_trade_never_fills_a_sol_column(monkeypatch):
    """Gamma LOW 2026-10-01: a USDC-priced trade_of result keeps side and tokens, never its USD amount
    in the study's SOL columns."""
    usdc = {"trader": "B", "side": "buy", "quote": "USDC", "sol": 20.0, "tokens": 100.0, "price": 0.2,
            "creator": None, "kind": "pool"}
    sol = dict(usdc, quote="SOL", sol=0.1, price=0.001)
    monkeypatch.setattr(C.dfx, "trade_of", lambda tx, mint, sol_usd: dict(tx))
    t = C._sol_trade(usdc, "m")
    assert (t["side"], t["tokens"], t["sol"], t["price"]) == ("buy", 100.0, None, None)
    assert C._sol_trade(sol, "m") == sol
