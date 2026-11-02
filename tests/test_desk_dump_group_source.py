"""dump-group source: keyset tailers, normalizers. Fixture tapes only; each rule fails when its code is removed."""
import sqlite3

import pytest

from sniper.services import memecoin_desk_dump_group_source as G

T0 = 1_793_000_000.0


def curve_db(path, day, rows, extra_cols=True):
    (path / "swaps").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path / "swaps" / ("%s.db" % day))
    ex = ", quote_mint text, cashback_fee_bps integer, real_quote integer" if extra_cols else ""
    con.execute("create table swaps(slot integer, signature text, ordinal integer, mint text, curve text, is_buy integer, quote_atomic integer, token_atomic integer, "
                "actor text, virtual_quote integer, virtual_token integer, block_time integer, received_at real, real_token integer, fee_bps integer, "
                "creator_fee_bps integer, tx_index integer%s)" % ex)
    con.execute("create index swaps_slot on swaps(slot)")
    add_curve(con, rows)
    con.commit()
    return con


def add_curve(con, rows):
    for r in rows:
        con.execute("insert into swaps(slot, signature, ordinal, mint, curve, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, block_time, received_at, "
                    "real_token, fee_bps, creator_fee_bps, tx_index, quote_mint, real_quote) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()


def crow(slot, n, recv, mint="M", buy=1, actor="a", qa=1_000_000_000, ta=30_000_000_000_000, vq=31_000_000_000, vb=1_043_000_000_000_000, sig=None, tx=None, qm="1" * 32, rq=1_000_000_000):
    return (slot, sig or "sig%05d" % n, 0, mint, "pool_" + mint, buy, qa, ta, actor, vq, vb, int(recv), recv, 793_000_000_000_000, 95, 30, tx, qm, rq)


def test_keyset_pages_a_single_slot_larger_than_the_batch_without_loss_or_repeat(tmp_path):
    rows = [crow(1000, i, T0 + i * 1e-3) for i in range(25)] + [crow(1001, 100 + i, T0 + 1 + i * 1e-3) for i in range(5)]
    curve_db(tmp_path, "2026-10-30", rows)
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=7)
    got, ex = [], []
    for _ in range(10):
        r = tail.poll()
        got += r["rows"]
        ex.append(r["exhausted"])
        if r["exhausted"]:
            break
    assert [x["signature"] for x in got] == [x[1] for x in rows] and ex[-1] is True and ex[:-1] == [False] * (len(ex) - 1)
    again = tail.poll()
    assert again["rows"] == [] and again["exhausted"] is True                        # nothing is returned twice


def test_new_rows_arriving_in_the_cursor_slot_after_a_page_are_not_skipped(tmp_path):
    con = curve_db(tmp_path, "2026-10-30", [crow(1000, i, T0 + i * 1e-3) for i in range(3)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=100)
    first = tail.poll()["rows"]
    add_curve(con, [crow(1000, 50, T0 + 0.5), crow(1000, 51, T0 + 0.6), crow(1001, 52, T0 + 1.0)])      # same slot, later received_at: keyset-after
    second = tail.poll()["rows"]
    assert [x["signature"] for x in first] == ["sig00000", "sig00001", "sig00002"] and [x["signature"] for x in second] == ["sig00050", "sig00051", "sig00052"]


def test_a_row_inserted_behind_the_cursor_is_a_late_row_counted_and_never_fed(tmp_path):
    con = curve_db(tmp_path, "2026-10-30", [crow(1000, i, T0 + i * 0.01) for i in range(5)] + [crow(1200, 9, T0 + 3)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=100, recheck_slots=500)
    assert len(tail.poll()["rows"]) == 6
    add_curve(con, [crow(1100, 77, T0 + 3.5)])                                       # slot 1100 < cursor slot 1200: written late
    r = tail.poll()
    assert r["rows"] == [] and tail.late_rows == 0
    tail.recheck()
    assert tail.late_rows == 1 and tail.poll()["rows"] == []                         # counted once, still not fed
    tail.recheck()
    assert tail.late_rows == 1


def test_day_rollover_finishes_the_old_day_before_opening_the_next(tmp_path):
    curve_db(tmp_path, "2026-10-30", [crow(1000, 1, T0), crow(1001, 2, T0 + 1)])
    curve_db(tmp_path, "2026-10-31", [crow(2000, 3, T0 + 86400)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=1, today=lambda: "2026-10-31")
    got = []
    for _ in range(10):
        got += tail.poll()["rows"]
    assert [x["signature"] for x in got] == ["sig00001", "sig00002", "sig00003"] and tail.day == "2026-10-31"


def test_a_missing_day_is_a_recorded_hole_not_a_silent_skip(tmp_path):
    curve_db(tmp_path, "2026-10-30", [crow(1000, 1, T0)])
    curve_db(tmp_path, "2026-11-01", [crow(3000, 3, T0 + 2 * 86400)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=10, today=lambda: "2026-11-01")
    for _ in range(6):
        tail.poll()
    assert tail.day == "2026-11-01" and tail.holes == [("2026-10-31", "day_missing")]


def test_the_normalizer_maps_exact_curve_fields_and_drops_bad_rows_with_a_reason(tmp_path):
    n = G.CurveNormalizer()
    # first swap of the coin: standard start (30 SOL / 1.073e15 before the buy), SOL quote
    buy = (1000, "s1", 0, "M", "poolM", 1, 1_000_000_000, 30_000_000_000_000, "a", 31_000_000_000, 1_043_000_000_000_000, T0, 793_000_000_000_000, 95, 30, None, "1" * 32, 1_000_000_000)
    row, why = n.normalize(buy)
    assert why is None and row["venue"] == "curve" and row["pool"] == "poolM" and row["is_buy"] is True and row["known_at"] == T0
    assert row["quote_sol"] == 1.0 and row["price_sol"] == pytest.approx(31e9 / 1.043e15 * 1e-3) and row["price_before_sol"] == pytest.approx(30e9 / 1.073e15 * 1e-3)
    assert row["pre_real_quote_sol"] == 0.0                                           # real_quote after the buy 1.0 SOL minus the net 1.0 SOL bought = 0 before
    sell = (1001, "s2", 0, "M", "poolM", 0, 500_000_000, 10_000_000_000_000, "b", 30_500_000_000, 1_053_000_000_000_000, T0 + 1, 800_000_000_000_000, 95, 30, None, "1" * 32, 1_000_000_000 - 500_000_000)
    row2, _ = n.normalize(sell)
    assert row2["is_buy"] is False and row2["pre_real_quote_sol"] == pytest.approx(1.0)       # after the sell 0.5, plus the gross 0.5 paid out and row2["quote_sol"] == 0.5
    assert n.normalize((1002, "s3", 0, "N", "poolN", 1, 1, 1, "c", 31_000_000_000, 1_043_000_000_000_000, T0, 1, 95, 30, None, "OtherQuote111", None))[1] == "non_sol_quote"
    assert n.normalize((1003, "s4", 0, "O", "poolO", 1, 1_000_000_000, 30_000_000_000_000, "d", 61_000_000_000, 1_043_000_000_000_000, T0, 1, 95, 30, None, "1" * 32, None))[1] == "nonstandard_start"
    assert n.normalize((1004, "s5", 0, "M", "poolM", 1, 1, 1, "", 31_000_000_000, 1_043_000_000_000_000, T0, 1, 95, 30, None, "1" * 32, None))[1] == "no_actor"
    assert n.normalize((1005, "s6", 0, "M", "poolM", 1, 1, 1, "e", 0, 0, T0, 1, 95, 30, None, "1" * 32, None))[1] == "bad_reserves"
    assert n.dropped["non_sol_quote"] == 1 and n.dropped["nonstandard_start"] == 1


def test_unknown_real_quote_is_none_never_a_clamped_zero():
    n = G.CurveNormalizer()
    row, _ = n.normalize((1000, "s1", 0, "M", "poolM", 1, 1_000_000_000, 30_000_000_000_000, "a", 31_000_000_000, 1_043_000_000_000_000, T0, 1, 95, 30, None, "1" * 32, None))
    assert row["pre_real_quote_sol"] is None
    neg, _ = n.normalize((1001, "s2", 0, "M", "poolM", 1, 5_000_000_000, 1, "a", 31_000_000_000, 1_043_000_000_000_000, T0 + 1, 1, 95, 30, None, "1" * 32, 1_000_000_000))
    assert neg["pre_real_quote_sol"] is None                                          # post real 1.0 SOL cannot follow a 5.0 SOL buy: inconsistent => unknown, not 0


# --- group state against the real pure detector -----------------------------------------------------------------------
from sniper.services.memecoin_desk_dump_group import BurstDetector  # noqa: E402


def sell_row(i, at, slot, pool="P", mint="M", actor=None, price=None, before=None):
    return {"venue": "curve", "pool": pool, "mint": mint, "signature": "sg%d_%s" % (i, pool), "ordinal": 0, "slot": slot, "known_at": at, "is_buy": False,
            "actor": actor or "s%d" % i, "quote_sol": 1.0, "price_sol": price, "price_before_sol": before, "pre_real_quote_sol": 100.0}


def buy_row(i, at, slot, actor, pool="Q", mint="N", sol=1.0):
    return {"venue": "curve", "pool": pool, "mint": mint, "signature": "bg%d_%s" % (i, pool), "ordinal": 0, "slot": slot, "known_at": at, "is_buy": True,
            "actor": actor, "quote_sol": sol, "price_sol": 1e-6, "price_before_sol": 1e-6, "pre_real_quote_sol": 100.0}


def burst_rows(start=T0):
    rows, price = [], 1e-6
    for i in range(20):
        nxt = price * (1 - 0.04 * (i + 1) / 2) if i < 19 else 0.4e-6
        rows.append(sell_row(i, start + i * 0.01, 100 + i // 7, price=nxt, before=price))
        price = nxt
    return rows


def make_state(own=("OURS",)):
    return G.GroupState(BurstDetector(), G.Exclusions(own))


def test_burst_participants_are_flagged_and_a_later_buyer_group_is_killed_with_proof():
    st = make_state()
    for r in burst_rows():
        st.feed(r)
    assert len(st.episodes) == 1 and len(next(iter(st.episodes.values()))["participants"]) == 20
    t = T0 + 5
    for i, a in enumerate(["s1", "s2", "s3", "x1", "x2", "x3", "x4", "x5"]):
        st.feed(buy_row(i, t + i * 0.1, 200 + i, a))
    sc = st.scores(asof=t + 5)
    q = sc["Q"]
    assert q["status"] == "kill" and q["positive_proven"] is True and q["target_complete"] is True and q["exclusions_valid"] is True
    assert q["matched_buyers"] == 3 and q["buyers"] == 8 and q["buyer_share"] == pytest.approx(3 / 8)


def test_a_kill_without_a_complete_target_window_or_valid_exclusions_is_unknown_not_a_kill():
    for kind in ("gap", "excl"):
        st = make_state(own=("OURS",) if kind == "gap" else ())
        for r in burst_rows():
            st.feed(r)
        t = T0 + 5
        for i, a in enumerate(["s1", "s2", "s3", "x1", "x2", "x3", "x4", "x5"]):
            st.feed(buy_row(i, t + i * 0.1, 200 + i, a))
        if kind == "gap":
            st.note_gap(T0 + 8, T0 + 8.5, "worker_start")      # inside the target window, away from the burst
        q = st.scores(asof=t + 5)["Q"]
        assert q["status"] == "unknown" and q["positive_proven"] is False and q["would_block"] is True and q["reason"] == "kill_not_proven"


def test_own_wallet_never_counts_as_a_dump_participant_or_buyer():
    st = make_state(own=("OURS", "s2"))                       # 20 sellers, one of them ours: 19 distinct non-own sellers is not a burst
    for r in burst_rows():
        st.feed(r)
    assert not st.episodes
    st2 = make_state()
    for r in burst_rows():
        st2.feed(r)
    assert len(st2.episodes) == 1                              # the same rows without the exclusion are a burst: the exclusion did the work


def test_a_tape_silence_between_rows_becomes_a_coverage_gap():
    st = make_state()
    st.feed(buy_row(1, T0, 1, "a"))
    st.feed(buy_row(2, T0 + 25, 90, "b"))
    assert st.gaps == [(T0, T0 + 25, "tape_silence", None, "curve")] and st.target_complete(T0 + 30) is False and st.target_complete(T0 + 100) is True


def test_pools_with_no_recent_buys_publish_no_score_and_state_is_pruned():
    st = make_state()
    st.feed(buy_row(1, T0, 1, "a"))
    assert "Q" in st.scores(asof=T0 + 1) and "Q" not in st.scores(asof=T0 + 200)
    st.prune(T0 + 1000)
    assert not st.buys


def test_exclusion_refresh_failure_keeps_the_last_signature_set_and_reports_it():
    calls = []

    def ledger():
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("db locked")
        return {"sigA"}
    ex = G.Exclusions(("W",), ledger=ledger, refresh_s=0.0)
    ex.maybe_refresh()
    ex.maybe_refresh()
    assert "sigA" in ex.excluded() and "W" in ex.excluded() and ex.sig_refresh_ok is False and ex.valid is True


def test_a_refreshed_own_signature_reaches_the_detector_exclusions():
    ex = G.Exclusions(("OURS",), ledger=lambda: {"sg3_P"}, refresh_s=0.0)
    st = G.GroupState(BurstDetector(), ex)
    rows = burst_rows()
    for r in rows[:2]:
        st.feed(r)
    ex.maybe_refresh()                                        # the ledger now names the signature of row 3 (a sell of this burst) before that row is fed
    for r in rows[2:]:
        st.feed(r)
    assert not st.episodes                                    # 19 distinct non-own sellers: no burst. (A row fed BEFORE a refresh keeps the old set: own wallet addresses are the primary guard.)
    st2 = G.GroupState(BurstDetector(), G.Exclusions(("OURS",)))
    for r in rows:
        st2.feed(r)
    assert len(st2.episodes) == 1


# --- Zeta's P1 witnesses on ba5aaa30d -------------------------------------------------------------------------------------
def test_day_rollover_with_a_reduced_page_is_not_exhausted_and_loses_no_rows(tmp_path):
    curve_db(tmp_path, "2026-10-30", [crow(100, 1, T0)])
    curve_db(tmp_path, "2026-10-31", [crow(200 + i, 10 + i, T0 + 86400 + i) for i in range(5)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=3, today=lambda: "2026-10-31")
    r = tail.poll()
    assert r["exhausted"] is False and len(r["rows"]) == 3
    got = list(r["rows"])
    for _ in range(5):
        got += tail.poll()["rows"]
    assert [x["signature"] for x in got] == ["sig00001"] + ["sig%05d" % (10 + i) for i in range(5)]


def test_a_db_error_is_a_typed_failure_never_an_exhausted_page(tmp_path):
    curve_db(tmp_path, "2026-10-30", [crow(100, 1, T0)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=10, today=lambda: "2026-10-30")
    (tmp_path / "swaps" / "2026-10-30.db").write_bytes(b"not a database" * 100)
    r = tail.poll()
    assert r["failed"] is True and r["exhausted"] is False and tail.db_errors == 1 and tail.recheck() in ([], None)


def test_a_late_row_marks_that_pools_target_window_incomplete(tmp_path):
    con = curve_db(tmp_path, "2026-10-30", [crow(100, 1, T0), crow(200, 2, T0 + 1)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=100, today=lambda: "2026-10-30", recheck_slots=150)
    st = make_state()
    for r in tail.poll()["rows"]:
        st.feed(r)
    add_curve(con, [crow(150, 3, T0 + 0.5, buy=1, actor="unmatched")])
    late = tail.recheck()
    assert tail.late_rows == 1 and late == [(T0 + 0.5, "pool_M")]
    for recv, pool in late:
        st.add_gap(recv - 0.5, recv + 0.5, "late_row", pool)
    assert st.target_complete(T0 + 2, "pool_M") is False and st.target_complete(T0 + 2, "pool_OTHER") is True


def test_a_gap_inside_a_burst_retracts_it_and_the_aged_out_gap_does_not_resurrect_it():
    st = make_state()
    st.add_gap(T0 + 0.05, T0 + 0.06, "missing_buy_or_state")                      # ahead of the cursor: applied when the feed reaches it
    for r in burst_rows():
        st.feed(r)
    assert not st.episodes                                                          # the burst formed across the gap: not counted
    for i in range(20):
        st.feed(buy_row(100 + i, T0 + 5 + i * 5, 300 + i, "unrelated", pool="FILL", mint="F"))
    st.feed(buy_row(900, T0 + 100, 900, "s0", pool="TARGETP", mint="T"))
    got = st.scores(T0 + 100)["TARGETP"]
    assert got["positive_proven"] is False and got["status"] != "kill" and got["matched_buyers"] == 0


def test_a_gap_noted_after_the_burst_was_fed_retracts_it(tmp_path):
    st = make_state()
    for r in burst_rows():
        st.feed(r)
    assert len(st.episodes) == 1
    st.add_gap(T0 + 0.05, T0 + 0.06, "late_producer_gap")                         # behind the cursor: retroactive retraction
    assert not st.episodes and len(st.retracted) == 1


def test_a_dropped_relevant_row_marks_the_pools_window_unknown(tmp_path):
    curve_db(tmp_path, "2026-10-30", [crow(100, 1, T0), crow(101, 2, T0 + 1, actor=""), crow(102, 3, T0 + 2)])
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=10, today=lambda: "2026-10-30")
    r = tail.poll()
    assert [i[0] for i in r["items"]] == ["row", "gap", "row"] and r["items"][1][1][2] == "dropped_no_actor" and r["items"][1][1][3] == "pool_M"


# --- PumpSwap tail + normalizer ---------------------------------------------------------------------------------------
import json  # noqa: E402

VQ_SRC = "pumpswap_grpc:buy_event_virtual_quote"


def ps_db(desk, rows):
    (desk / "pumpswap").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(desk / "pumpswap" / "tape.db")
    con.execute("create table swaps_ps(pool text, mint text, quote_mint text, slot integer, signature text, ordinal integer, tx_index integer, is_buy integer, actor text, "
                "coin_creator text, base_atomic text, quote_atomic text, pre_base_reserve_atomic text, pre_quote_reserve_atomic text, post_base_reserve_atomic text, "
                "post_quote_reserve_atomic text, received_at real, processed_at real, payload_json text)")
    con.execute("create index swaps_ps_received on swaps_ps(received_at)")
    con.execute("create table gaps_ps(at real, cause text, last_slot integer)")
    add_ps(con, rows)
    return con


def add_ps(con, rows):
    for r in rows:
        con.execute("insert into swaps_ps(pool, mint, quote_mint, slot, signature, ordinal, tx_index, is_buy, actor, base_atomic, quote_atomic, pre_base_reserve_atomic, "
                    "pre_quote_reserve_atomic, post_base_reserve_atomic, post_quote_reserve_atomic, received_at, processed_at, payload_json) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", r)
    con.commit()


def proof(vq):
    return json.dumps({"virtual_quote_atomic": str(vq), "virtual_quote_source": VQ_SRC, "ix_name": "buy", "virtual_quote_reserves": vq})


def prow(n, recv, *, pool="PP", mint="PM", buy=0, actor="a", pre=("1000000000000000", "80000000000"), post=(None, None), proc=None, slot=None, tx=0, base="1000", quote="1000",
         payload=None, qm=G.WSOL):
    return (pool, mint, qm, slot or 1000 + n, "psig%05d" % n, 0, tx, buy, actor, base, quote, pre[0], pre[1], post[0], post[1], recv, recv if proc is None else proc, payload)


def test_ps_price_uses_only_the_already_known_proven_virtual_quote_and_none_otherwise():
    n = G.PSNormalizer()
    r0, why, _ = n.normalize(prow(0, T0, buy=1, payload=proof(30_000_000_000)))
    assert r0["price_before_sol"] is None and r0["price_sol"] is None            # the row's OWN proof does not price its own pre-state: no prior VQ yet
    r1, _, _ = n.normalize(prow(1, T0 + 1))
    assert r1["price_before_sol"] == pytest.approx((80e9 + 30e9) / 1e15 * 0.001)
    assert r1["price_known_at"] == T0 + 1 and r1["price_sol"] == r1["price_before_sol"]
    # a different pool has no carried proof: None, never zero
    r2, _, _ = n.normalize(prow(2, T0 + 2, pool="OTHER"))
    assert r2["price_before_sol"] is None


def test_ps_known_at_is_the_later_of_received_and_processed_and_pre_real_quote_is_the_pre_state():
    n = G.PSNormalizer()
    r, _, _ = n.normalize(prow(0, T0, proc=T0 + 0.7))
    assert r["known_at"] == T0 + 0.7 and r["pre_real_quote_sol"] == pytest.approx(80.0) and r["quote_sol"] == pytest.approx(1000 / 1e9)


def test_a_pool_state_that_moved_without_a_swap_we_hold_is_a_pool_gap():
    n = G.PSNormalizer()
    assert n.normalize(prow(0, T0, buy=1, base="100", pre=("1000", "50"), post=("900", "60")))[2] == []
    assert n.normalize(prow(1, T0 + 1, base="50", pre=("900", "60")))[2] == []                       # a SELL states no post: it adds exactly base_atomic (50) to the base reserve
    assert n.normalize(prow(2, T0 + 2, buy=1, base="50", pre=("950", "55"), post=("900", "65")))[2] == []
    assert n.normalize(prow(3, T0 + 3, base="10", pre=("900", "65")))[2] == []                       # matches the buy's stated post
    gaps = n.normalize(prow(4, T0 + 4, pre=("700", "80")))[2]                                         # row 3 (a sell of 10) must leave 910: this pre differs => a swap was lost
    assert len(gaps) == 1 and gaps[0][2] == "ps_lost_swap" and gaps[0][3] == "PP" and gaps[0][4] == "ps"


def test_ps_rows_for_unmapped_or_non_wsol_pools_are_dropped_with_a_reason():
    n = G.PSNormalizer({"POOLX": "MINTX"})
    assert n.normalize(prow(0, T0, mint=None))[1] == "unmapped_pool"
    assert n.normalize(prow(1, T0, pool="POOLX", mint=None))[0]["mint"] == "MINTX"
    assert n.normalize(prow(2, T0, qm="OtherQuote"))[1] == "non_sol_quote"
    row, why, gaps = n.normalize(prow(3, T0, actor=""))
    assert row is None and why == "bad_row" and gaps[0][2] == "dropped_bad_row"


def test_ps_tail_pages_a_tie_group_larger_than_the_batch_without_loss(tmp_path):
    rows = [prow(i, T0, slot=1000 + i % 7) for i in range(25)] + [prow(100 + i, T0 + 1 + i * 0.01) for i in range(10)]
    ps_db(tmp_path, rows)
    tail = G.PSTail(str(tmp_path), T0 - 1, batch=8)
    got, last = [], None
    for _ in range(12):
        r = tail.poll()
        got += r["rows"]
        last = r
        if r["exhausted"]:
            break
    assert sorted(x["signature"] for x in got) == sorted(x[4] for x in rows) and last["exhausted"] is True
    assert tail.poll()["rows"] == []                                             # nothing twice


def test_ps_tail_late_rows_are_reported_with_their_pool_and_a_db_error_is_typed(tmp_path):
    con = ps_db(tmp_path, [prow(i, T0 + i) for i in range(5)])
    tail = G.PSTail(str(tmp_path), T0 - 1, batch=100)
    assert len(tail.poll()["rows"]) == 5
    add_ps(con, [prow(50, T0 + 2.5, pool="LATEP")])
    assert tail.recheck() == [(T0 + 2.5, "LATEP")] and tail.late_rows == 1 and tail.recheck() == []
    (tmp_path / "pumpswap" / "tape.db").write_bytes(b"x" * 2000)
    bad = tail.poll()
    assert bad["failed"] is True and bad["exhausted"] is False


def test_rows_ahead_of_the_cursor_in_the_same_slot_are_not_late(tmp_path):
    rows = [crow(1000, i, T0 + i * 1e-3) for i in range(25)] + [crow(1001, 100 + i, T0 + 1 + i * 1e-3) for i in range(5)]
    curve_db(tmp_path, "2026-10-30", rows)
    tail = G.CurveTail(str(tmp_path), "2026-10-30", batch=7)
    tail.poll()                                                                   # the page ends inside slot 1000
    assert tail.recheck() == [] and tail.late_rows == 0


def test_a_gap_noted_BEFORE_the_burst_rejects_the_new_episode_at_insertion_zeta():
    st = make_state()
    st.note_gap(T0 + 0.05, T0 + 0.06, "missing_buy_or_state")                      # stored first; no episode exists yet to retract
    for r in burst_rows():
        st.feed(r)
    assert not st.episodes and st.rejected_episodes >= 1
    for i in range(20):                                                              # long after the gap aged out of any 60 s window
        st.feed(buy_row(100 + i, T0 + 5 + i * 5, 300 + i, "unrelated", pool="FILL", mint="F"))
    st.feed(buy_row(900, T0 + 100, 900, "s0", pool="TARGETP", mint="T"))
    got = st.scores(T0 + 100)["TARGETP"]
    assert got["positive_proven"] is False and got["status"] != "kill" and got["matched_buyers"] == 0
    ok = make_state()                                                                # the same burst with the gap far away is a normal burst
    ok.note_gap(T0 + 50, T0 + 51, "elsewhere")
    for r in burst_rows():
        ok.feed(r)
    assert len(ok.episodes) == 1 and ok.rejected_episodes == 0


def test_a_gap_scheduled_after_the_last_fed_row_blocks_proof_at_a_later_cutoff():
    """Zeta P1 on 031fea06c: add_gap queued the gap ahead of the cursor and scores() ignored it, so a restart/quiet gap published false proof."""
    import os
    import sys
    sys.path.insert(0, os.path.dirname(__file__))
    from test_desk_dump_group import dump, swap
    st = G.GroupState(BurstDetector(), G.Exclusions(["OURS"]))
    for r in dump():
        st.feed(r)
    st.feed(swap(1000, actor="a0", buy=True, t=105, mint="TARGET", slot=500))
    st.add_gap(106, 106, "worker_start", None, "curve")
    got = st.scores(107)["TARGET"]
    assert got["target_complete"] is False and got["positive_proven"] is False and got["status"] != "kill"
    assert st.gaps and not st.pending
