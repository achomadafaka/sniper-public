"""source_entry_live: the forward collector's rules, one fixture tape per rule (each case fails when its rule is removed)."""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))
import source_entry_live as L  # noqa: E402

DAY = "2026-10-30"
T = lambda hms, day=DAY: datetime.fromisoformat("%sT%sZ" % (day, hms)).timestamp()  # noqa: E731
MINT = "CoinMint1pump"


SOL = "1" * 32


def make_tape(desk, day=DAY, coins=(), filler=(T("11:00:00"), T("13:00:00")), skip=(), null_bt=False, legacy=False, quote=None, cashback=None):
    """coins: (mint, [(t, is_buy, rt)]). quote: {mint: quote_mint}; cashback: {mint: cashback}; legacy: a day db without quote_mint/cashback columns."""
    (desk / "swaps").mkdir(parents=True, exist_ok=True)
    (desk / "gaps").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(desk / "swaps" / ("%s.db" % day))
    extra = "" if legacy else ", quote_mint text, cashback_fee_bps integer, cashback integer"
    con.execute("create table swaps(slot integer, signature text, ordinal integer, mint text, curve text, is_buy integer, quote_atomic integer, token_atomic integer, "
                "actor text, virtual_quote integer, virtual_token integer, block_time integer, received_at real, real_token integer, fee_bps integer, "
                "creator_fee_bps integer, tx_index integer, real_quote integer%s)" % extra)
    con.execute("create index swaps_mint_slot on swaps(mint, slot)")
    con.execute("create index swaps_slot on swaps(slot)")
    rows, t = [], filler[0]
    while t <= filler[1]:
        if not any(a <= t < b for a, b in skip):
            rows.append((t, "filler", 0, 1, 1, 1, "x", 1, 1, 1, 0, 0))
        t += 2
    for mint, swaps in coins:                                     # swaps: (t, is_buy, rt)
        q, b = 30e9, 1.073e15
        for i, (t, buy, rt) in enumerate(swaps):
            qa, ta = 1e9, 3e13
            q, b = (q + qa, b - ta) if buy else (q - qa, b + ta)
            rows.append((t, mint, i, int(buy), int(qa), int(ta), "a%d" % (i % 3), int(q), int(b), rt, 100, 30))
    for n, (t, mint, i, buy, qa, ta, actor, vq, vb, rt, fee, cfee) in enumerate(sorted(rows, key=lambda r: r[0]), 1):   # slot follows time, as on the real tape
        vals = [n, "s%d" % n, i, mint, "c", buy, qa, ta, actor, vq, vb, None if (null_bt and mint != "filler") else int(t), t, rt, fee, cfee, None, None]
        if not legacy:
            vals += [(quote or {}).get(mint, SOL), 0, (cashback or {}).get(mint, 0)]
        con.execute("insert into swaps values(%s)" % ",".join("?" * len(vals)), vals)
    con.commit()
    con.close()


def ledger(events):
    con = sqlite3.connect(":memory:")
    con.execute("create table memecoin_events(id integer primary key, network_id text, mint text, side text, created_at text, clock_domain text, source_user_id text)")
    for e in events:
        con.execute("insert into memecoin_events values(?,?,?,?,?,?,?)", e)
    return con


def good_swaps():
    return [(T("11:55:00") + i * 20, 1, 7e14) for i in range(15)] + [(T("12:00:20") + i * 30, 1, 7e14) for i in range(10)]


def run(tmp, events, now=None, **kw):
    out = tmp / "out"
    st = L.collect(ledger(events), desk=str(tmp / "desk"), out_dir=str(out), now=T("14:00:00") if now is None else now, since="2026-10-01T00:00:00Z", **kw)
    rows = [json.loads(x) for p in sorted(out.glob("rows-2*.jsonl")) for x in open(p)]
    return st, rows, out


EV = [(1, "solana", MINT, "buy", "2026-10-30T12:00:00Z", "observed_real_utc", "u1")]


def test_ok_row_has_full_history_and_forward(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    ev = EV + [(2, "solana", MINT, "buy", "2026-10-30T12:01:00Z", "x", "u2")]
    st, rows, _ = run(tmp_path, ev)
    r = rows[0]
    assert (r["reason"], r["in_support"], r["fwd_complete"]) == ("ok", True, True) and len(rows) == 1
    assert r["swaps_pre"][0][0] == int(T("11:55:00")) and all(x[0] < int(T("12:00:00")) for x in r["swaps_pre"])   # FULL history t0..tc, fill second excluded
    assert r["swaps_fwd"] and all(x[0] > int(T("12:00:00")) for x in r["swaps_fwd"])
    assert r["later_buys_120s"][0]["event_id"] == 2 and r["feats_hash"] == L.FEATS_HASH and r["selected_wallet"] is None


def test_silence_overlap_is_censored_not_dropped(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], skip=[(T("12:10:00"), T("12:10:30"))])
    _, rows, _ = run(tmp_path, EV)
    assert rows[0]["reason"] == "tape_silence" and rows[0]["fwd_complete"] is False and rows[0]["in_support"] is True


def test_gap_row_overlap_is_censored(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    g = sqlite3.connect(tmp_path / "desk" / "gaps" / ("%s.db" % DAY))
    g.execute("create table gaps(at real, cause text, last_slot integer)")
    g.execute("insert into gaps values(?,?,?)", (T("12:05:00"), "worker_start", None))
    g.commit()
    g.close()
    assert run(tmp_path, EV)[1][0]["reason"] == "tape_silence"


def test_not_ready_stays_pending_then_times_out(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], filler=(T("11:00:00"), T("12:20:00")))
    st, rows, _ = run(tmp_path, EV, now=T("12:30:00"))
    assert st["pending"] == 1 and rows == []
    st, rows, _ = run(tmp_path, EV, now=T("12:00:00") + 6 * 3600 + 1)
    assert rows[0]["reason"] == "tape_silence" and rows[0]["fwd_complete"] is False


def test_missing_next_day_waits_then_tape_missing(tmp_path):
    sw = [(T("23:45:00") + i * 20, 1, 7e14) for i in range(20)]
    make_tape(tmp_path / "desk", coins=[(MINT, sw)], filler=(T("23:00:00"), T("23:59:58")))
    ev = [(1, "solana", MINT, "buy", "2026-10-30T23:50:00Z", "x", "u1")]
    assert run(tmp_path, ev, now=T("23:59:59"))[0]["pending"] == 1
    _, rows, _ = run(tmp_path, ev, now=T("23:50:00") + 6 * 3600 + 1)
    assert rows[0]["reason"] == "tape_missing" and rows[0]["fwd_complete"] is False and rows[0]["missing_days"] == ["2026-10-31"]


def test_not_on_tape_and_non_solana(tmp_path):
    make_tape(tmp_path / "desk")
    ev = [(1, "solana", "Ghost", "buy", "2026-10-30T12:00:00Z", "x", "u"), (2, "8453", "0xabc", "buy", "2026-10-30T12:00:00Z", "x", "u")]
    _, rows, _ = run(tmp_path, ev)
    assert {r["mint"]: r["reason"] for r in rows} == {"Ghost": "not_on_tape", "0xabc": "non_solana"}


def test_precedence_nonstandard_beats_mcap_and_age(tmp_path):
    sw = [(T("10:00:00") + i * 60, 1, 7e14) for i in range(120)]            # old (age > 1800) coin
    make_tape(tmp_path / "desk", coins=[("Odd", sw), (MINT, good_swaps())], filler=(T("09:00:00"), T("13:00:00")))
    con = sqlite3.connect(tmp_path / "desk" / "swaps" / ("%s.db" % DAY))
    con.execute("update swaps set virtual_quote = virtual_quote + 5000000000 where mint='Odd' and ordinal = 0")      # first swap pre-state off the 30 SOL start
    con.commit()
    con.close()
    ev = [(1, "solana", "Odd", "buy", "2026-10-30T12:00:00Z", "x", "u")]
    r = run(tmp_path, ev)[1][0]
    assert r["flags"]["nonstandard_start"] and r["flags"]["out_of_support_age"] and r["reason"] == "nonstandard_start"
    ev2 = [(1, "solana", MINT, "buy", "2026-10-30T12:00:00Z", "x", "u")]
    sw2 = [(T("09:30:00") + i * 60, 1, 7e14) for i in range(150)]
    make_tape(tmp_path / "d2" / "desk", coins=[(MINT, sw2)], filler=(T("09:00:00"), T("13:00:00")))
    out = tmp_path / "o2"
    L.collect(ledger(ev2), desk=str(tmp_path / "d2" / "desk"), out_dir=str(out), now=T("14:00:00"), since="2026-10-01T00:00:00Z")
    assert json.loads(open(next(out.glob("rows-2*.jsonl"))).readline())["reason"] == "out_of_support_age"


def test_not_curve_at_tc_beats_age(tmp_path):
    sw = [(T("11:55:00") + i * 20, 1, 7e14) for i in range(14)] + [(T("11:59:50"), 1, 0)]
    make_tape(tmp_path / "desk", coins=[(MINT, sw)])
    r = run(tmp_path, EV)[1][0]
    assert r["reason"] == "not_curve_at_tc" and r["in_support"] is False and "swaps_pre" not in r


def test_null_block_time_flagged_recv(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], null_bt=True)
    r = run(tmp_path, EV)[1][0]
    assert r["clock_recv_any"] is True and r["swaps_pre"][0][14] == 1


def test_rerun_is_idempotent_and_first_changed_is_logged_once(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    run(tmp_path, EV)
    st, rows, out = run(tmp_path, EV)
    assert st["written"] == 0 and len(rows) == 1
    early = EV + [(3, "solana", MINT, "buy", "2026-10-30T11:58:00Z", "x", "u9")]
    assert run(tmp_path, early)[0]["first_changed"] == 1
    assert run(tmp_path, early)[0]["first_changed"] == 0
    assert len(open(out / "rows-corrections.jsonl").readlines()) == 1 and len(rows) == 1


def test_selected_wallet_flag(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    assert run(tmp_path, EV, selected={"u1"})[1][0]["selected_wallet"] is True


def test_precedence_not_curve_beats_age_and_age_beats_mcap(tmp_path):
    old = [(T("11:00:00") + i * 60, 1, 7e14) for i in range(60)] + [(T("11:59:50"), 1, 0)]       # age 3590 s AND the curve completed before tc
    make_tape(tmp_path / "desk", coins=[(MINT, old)], filler=(T("10:00:00"), T("13:00:00")))
    r = run(tmp_path, EV)[1][0]
    assert r["flags"]["not_curve_at_tc"] and r["flags"]["out_of_support_age"] and r["reason"] == "not_curve_at_tc"
    assert L.REASONS.index("out_of_support_age") < L.REASONS.index("out_of_support_mcap")


def test_fill_second_swaps_are_dropped_on_both_sides(tmp_path):
    sw = good_swaps() + [(T("12:00:00") + 0.2, 1, 7e14), (T("12:00:00") + 0.8, 1, 7e14)]
    make_tape(tmp_path / "desk", coins=[(MINT, sorted(sw))])
    r = run(tmp_path, EV)[1][0]
    assert all(x[0] != int(T("12:00:00")) for x in r["swaps_pre"] + r["swaps_fwd"])


def test_selected_list_loader_reads_the_frozen_document(tmp_path):
    p = tmp_path / "w.jsonl"
    p.write_text(json.dumps({"wallets": [{"uid": "u1"}, {"uid": "u2"}]}, indent=1))
    uids, sha = L.load_selected(p)
    assert uids == {"u1", "u2"} and len(sha) == 16


def test_non_sol_quote_with_standard_looking_reserves_is_nonstandard(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], quote={MINT: "pumpCmXqMfrsAkQ5r49WcJnRayYRqmXz6ae8H7H9Dfn"})
    r = run(tmp_path, EV)[1][0]
    assert r["flags"]["nonstandard_start"] and r["reason"] == "nonstandard_start" and r["in_support"] is False


def test_legacy_day_without_quote_column_reads_as_sol_and_says_so(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], legacy=True)
    r = run(tmp_path, EV)[1][0]
    assert r["reason"] == "ok" and r["quote_col_absent_days"] == [DAY]


def test_cashback_is_stored_and_flagged(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())], cashback={MINT: 5})
    r = run(tmp_path, EV)[1][0]
    assert r["cashback_nonzero"] is True and r["swaps_pre"][0][17] == 5 and "cashback" in r["swap_cols"]


def test_selected_wallet_gets_its_own_row_when_not_pair_first(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    ev = [(1, "solana", MINT, "buy", "2026-10-30T12:00:00Z", "x", "u1"), (2, "solana", MINT, "buy", "2026-10-30T12:00:30Z", "x", "u2")]
    st, rows, _ = run(tmp_path, ev, selected={"u2"})
    by = {(r["source_user_id"], r["pair_first"]): r for r in rows}
    assert set(by) == {("u1", True), ("u2", False)} and by[("u2", False)]["selected_wallet"] is True and by[("u1", True)]["selected_wallet"] is False
    assert run(tmp_path, ev, selected={"u2"})[0]["written"] == 0                                   # idempotent per key
    assert len([r for r in rows if r["pair_first"]]) == 1


def test_selected_pair_first_wallet_gets_one_row_only(tmp_path):
    make_tape(tmp_path / "desk", coins=[(MINT, good_swaps())])
    ev = [(1, "solana", MINT, "buy", "2026-10-30T12:00:00Z", "x", "u1"), (2, "solana", MINT, "buy", "2026-10-30T12:00:30Z", "x", "u2")]
    assert [r["source_user_id"] for r in run(tmp_path, ev, selected={"u1"})[1]] == ["u1"]


def test_missing_day_with_empty_history_is_tape_missing_not_not_on_tape(tmp_path):
    make_tape(tmp_path / "desk", filler=(T("23:00:00"), T("23:59:58")))
    ev = [(1, "solana", "Ghost", "buy", "2026-10-30T23:50:00Z", "x", "u1")]
    r = run(tmp_path, ev, now=T("23:50:00") + 6 * 3600 + 1)[1][0]
    assert r["reason"] == "tape_missing" and r["flags"]["not_on_tape"] is False


def test_whole_tape_gap_over_midnight_is_seen(tmp_path):
    make_tape(tmp_path / "desk", day="2026-10-30", filler=(T("23:59:00"), T("23:59:50")))
    make_tape(tmp_path / "desk", day="2026-10-31", filler=(T("00:00:30", "2026-10-31"), T("00:01:00", "2026-10-31")))
    st1 = {}
    L.day_silences(str(tmp_path / "desk" / "swaps" / "2026-10-30.db"), st1)
    sil = L.day_silences(str(tmp_path / "desk" / "swaps" / "2026-10-31.db"), {}, st1["high"])
    assert sil and sil[0][0] == st1["high"]


def test_non_iso_created_at_is_refused(tmp_path):
    make_tape(tmp_path / "desk")
    try:
        run(tmp_path, [(1, "solana", "M", "buy", "2026-10-30 12:00:00", "x", "u")])
    except ValueError:
        return
    raise AssertionError("space-form created_at must be refused")


def test_collect_sees_a_gap_that_spans_midnight(tmp_path):
    d2 = "2026-10-31"
    sw = [(T("23:45:00") + i * 20, 1, 7e14) for i in range(20)]
    make_tape(tmp_path / "desk", day=DAY, coins=[(MINT, sw)], filler=(T("23:00:00"), T("23:59:50")))
    make_tape(tmp_path / "desk", day=d2, filler=(T("00:00:30", d2), T("00:40:00", d2)))
    ev = [(1, "solana", MINT, "buy", "2026-10-30T23:50:00Z", "x", "u1")]
    r = run(tmp_path, ev, now=T("01:00:00", d2))[1][0]
    assert r["reason"] == "tape_silence" and r["fwd_complete"] is False
