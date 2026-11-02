"""PumpSwap sell post reserves (task 4337b015): proven only by the frame's instruction name. 'Sell' sends the
protocol + creator fees out of the quote vault, 'SellV2' only the buyback share. Fixtures are live tape events;
the expected post is the next swap's pre-trade reserves."""
from sniper.services import memecoin_desk_pumpswap as P

AMM = P.AMM_PROGRAM
FIX = {'sell': ('Pi83CqUD3CrOs+FqAAAAADuQDioAAAAAYdcTAAAAAAAroJ+fAQAAAAAAAAAAAAAAVExf1f9YAAD1GSOUMAAAAMvjGAAAAAAAFAAAAAAAAAC/DAAAAAAAAAUAAAAAAAAAMAMAAAAAAAAM1xgAAAAAAIOaGAAAAAAAIoB6tZmGtlqjgNt9gmiEtwpHvzXt/3fsZZXopQ6vLGRNAo88dXqkxDwr9b10O3ggus1Lord4hwoDHN9WPZJsSDYM8eMnBKpC3YbXchS+1fGpQ4Vx+QaFHz8DAvQNbnnYO3IsjUMzMQpzP1ElAiaq4i+wmo69K5Hd/QUxJxsBN0lKwvjQ3Vy8l+MonBl8tQYqVPPZVrnOblEV+WVnqlyz5nfZFZVfiIBzHOtKdaDMlsF0+kCVxOHZlnrPxChFrmeu1TnE/LXeuwGEldfM9P7rloaJa2LDK+G+VDYvyCpSXKtaAAAAAAAAAFk5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAmAEAAAAAAADiz7sXBAAAAAAAAAAAAAAAAVeRemg9hQMAAAAAAAAAAAAAAAAAAAAAABi8XwAAAAAAAAAAAAAAAAA=', '97856525294735', '208642130665'), 'sell_v2': ('Pi83CqUD3CpbtOFqAAAAAJKRo8oEAAAAAAAAAAAAAACSkaPKBAAAAAAAAAAAAAAAEmMgnZJVAABof5WxMgAAAFgnEQMAAAAAFAAAAAAAAAAAkgEAAAAAAAUAAAAAAAAAgGQAAAAAAABYlQ8DAAAAAFmECAMAAAAAIoB6tZmGtlqjgNt9gmiEtwpHvzXt/3fsZZXopQ6vLGTx75ry8ebZTya1gy9lyCnUS1IW6xP0gkr8i9Q/RACsqmgtvr16AZGRlL8DLJyBhCFSL4rzFmjbiOPSHH2o/QnFFl0h7wxQcGsR3E86GCnSPYHoitolJGAT2cdOJ5quQwEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1TnE/LXeuwGEldfM9P7rloaJa2LDK+G+VDYvyCpSXKtVAAAAAAAAAH+sBgAAAAAAAAAAAAAAAAAAAAAAAAAAAIgTAAAAAAAAQDIAAAAAAABlQJYXBAAAAAAAAAAAAAAAAVeRemg9hQMAAAAAAAAAAAAAAAAAAAAAADP0igAAAAAAAAAAAAAAAAA=', '94108769318052', '217676826831')}


def logs(data, name):
    head = ["Program %s invoke [1]" % AMM] + (["Program log: Instruction: " + name] if name else [])
    return head + ["Program data: " + data, "Program %s success" % AMM]


def sell(ix):
    data, base, quote = FIX[ix]
    name = {"sell": "Sell", "sell_v2": "SellV2"}[ix]
    return P.events(logs(data, name))[0][1], base, quote


def test_sell_post_reserves_match_the_next_swap_pre_state():
    for ix in ("sell", "sell_v2"):
        ev, base, quote = sell(ix)
        assert ev["kind"] == "sell" and ev["payload"]["ix_name"] == ix
        assert (ev["post_base_reserve_atomic"], ev["post_quote_reserve_atomic"]) == (base, quote), ix


def test_unknown_instruction_keeps_post_null():
    data = FIX["sell"][0]
    for name in (None, "Swap"):
        ev = P.events(logs(data, name))[0][1]
        assert ev["post_base_reserve_atomic"] is None and ev["post_quote_reserve_atomic"] is None


def test_the_wrong_layout_never_matches_the_proven_state():
    # a neither-row is what a wrong layout gives; readers see post != next pre as a loss, never a price
    data, base, quote = FIX["sell"]
    ev = P.events(logs(data, "SellV2"))[0][1]
    assert (ev["post_base_reserve_atomic"], ev["post_quote_reserve_atomic"]) != (base, quote)


def test_instruction_name_comes_from_the_events_own_frame():
    data = FIX["sell"][0]
    nested = (["Program X invoke [1]", "Program log: Instruction: SellV2", "Program %s invoke [2]" % AMM,
               "Program log: Instruction: Sell", "Program data: " + data, "Program %s success" % AMM,
               "Program X success"])
    assert P.instruction_names(nested) == {4: "Sell"}


# ---- readers: a decoded sell post is an observation; only the next same-pool pre confirms it (Alpha 2026-10-28)
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from test_desk_ps_model_facts import NOW, ROWS, SOL, tape


def _sells(tmp, rows, post):
    """Fixture tape; every sell gets the decoded post `post(sell_index_in_rows, next_pre)`."""
    c, path = tape(tmp, rows)
    with sqlite3.connect(path) as db:
        got = db.execute("SELECT signature, is_buy, pre_base_reserve_atomic, pre_quote_reserve_atomic FROM swaps_ps "
                         "ORDER BY slot").fetchall()
        for i, (sig, buy, _pb, _pq) in enumerate(got):
            if not buy:
                nxt = got[i + 1][2:] if i + 1 < len(got) else None
                b, q = post(nxt)
                db.execute("UPDATE swaps_ps SET post_base_reserve_atomic=?, post_quote_reserve_atomic=? WHERE signature=?",
                           (b, q, sig))
    return c


def _sell_rows(tmp, c):
    rows = PT.load_ps_swaps(tmp, c["mint"], NOW - 200, NOW + 1, SOL)
    return [r for r in rows if not r["is_buy"] and not r.get("price_only")]


def test_a_confirmed_sell_post_prices(tmp_path):
    c = _sells(tmp_path, ROWS, lambda nxt: nxt)
    assert [r["price_usd"] is not None for r in _sell_rows(tmp_path, c)] == [True]


def test_a_mismatched_sell_post_never_prices_and_is_not_a_lost_swap(tmp_path):
    c = _sells(tmp_path, ROWS, lambda nxt: (str(int(nxt[0]) + 1), nxt[1]))
    assert [r["price_usd"] for r in _sell_rows(tmp_path, c)] == [None]
    _, gaps = PT.load_ps_swaps(tmp_path, c["mint"], NOW - 200, NOW + 1, SOL, with_gaps=True)
    assert gaps == []


def test_an_unresolved_sell_post_is_not_consumed_before_the_next_pre_lands(tmp_path):
    rows = ROWS + [(-1, 0.5, 0, 20, "late_seller")]
    c = _sells(tmp_path, rows, lambda nxt: nxt if nxt else ("1000000000000", "5000000000"))
    last = _sell_rows(tmp_path, c)[-1]
    assert last["actor"] == "late_seller" and last["price_usd"] is None


# ---- lessons: a sell state confirmed by a later row is known only from that row's receipt (Delta on fa033462f)

def test_a_sell_state_is_not_known_before_its_confirming_row_arrives(tmp_path):
    import json as _json
    from sniper.services import memecoin_desk_lessons as LS
    from test_desk_lessons_witnesses import MINT
    from test_desk_lessons import T0
    p = PT.tape_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(p)
    db.execute("CREATE TABLE swaps_ps(pool TEXT, mint TEXT, quote_mint TEXT, received_at REAL, slot INTEGER, ordinal INTEGER, "
               "tx_index INTEGER, is_buy INTEGER, pre_base_reserve_atomic TEXT, pre_quote_reserve_atomic TEXT, "
               "post_base_reserve_atomic TEXT, post_quote_reserve_atomic TEXT, lp_fee_bps TEXT, protocol_fee_bps TEXT, "
               "creator_fee_bps TEXT, cashback_fee_bps TEXT, buyback_fee_bps TEXT, payload_json TEXT)")
    db.execute("CREATE TABLE gaps_ps(at REAL, cause TEXT, last_slot INTEGER)")
    db.execute("CREATE INDEX swaps_ps_received ON swaps_ps(received_at)")
    db.execute("CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool,slot)")
    proof = _json.dumps({"ix_name": "buy", "virtual_quote_atomic": "17500000000", "virtual_quote_reserves": "17500000000",
                         "virtual_quote_source": "pumpswap_grpc:buy_event_virtual_quote"})
    pool = PT.canonical_pool(MINT)
    rows = [(T0 - 50, 1, 1, "1000000000000", "10000000000", "1000000000000", "10000000000", proof),   # buy
            (T0 - 40, 2, 0, "1000000000000", "10000000000", "1100000000000", "9100000000", "{}"),    # decoded sell
            (T0 - 5, 3, 1, "1100000000000", "9100000000", "1050000000000", "9500000000", proof)]     # confirms it
    for at, slot, buy, pb, pq, b, q, payload in rows:
        db.execute("INSERT INTO swaps_ps VALUES(?,?,NULL,?,?,0,0,?,?,?,?,?,'95','5','25','0','0',?)",
                   (pool, MINT, at, slot, buy, pb, pq, b, q, payload))
    db.commit()
    db.close()
    states = LS.load_pool(tmp_path, MINT, T0 - 60, T0)
    sell = next(s for s in states if s["t"] == T0 - 40)
    assert sell["known_at"] == T0 - 5
    tape = LS.Tape([], states)
    assert tape.pool_value_at(T0 - 39.5, 1_000_000) is None       # horizon past confirmation; still unknown at the mark
    assert tape.pool_value_at(T0 - 4, 1_000_000) is not None


def test_migration_value_never_uses_a_state_not_yet_known_at_its_time():
    from sniper.services import memecoin_desk_lessons as LS
    t0 = 1_800_000_000.0
    state = {"t": t0 + 10, "base": 1_000_000_000_000, "rq": 10_000_000_000, "vq": 0, "bps": (0,), "known_at": t0 + 40}
    tape = LS.Tape([], [state])
    tape.done_t = t0 + 10 - LS.LAG_S
    assert tape.migration_value(1_000_000) is None
    state["known_at"] = t0 + 10
    tape = LS.Tape([], [state])
    tape.done_t = t0 + 10 - LS.LAG_S
    assert tape.migration_value(1_000_000) is not None
