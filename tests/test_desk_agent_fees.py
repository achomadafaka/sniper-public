"""Agent-aware sell probe: the fee-free Mayhem agent never defines OUR fee. Numbers shaped like the live tape (19 mayhem tickets:
agent (0,0) was the LAST trade on 17; every regular trader paid (95,30))."""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from sniper.research.memecoin_opportunity import state as research_state
from sniper.services import memecoin_desk_safety as S

AGENT = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
TRADE = {"slot": 990, "virtual_quote": 55123429969, "virtual_token": 583962211444407, "real_token": 304062211444407,
         "fee_bps": 0, "creator_fee_bps": 0}            # the LAST trade is the agent's: fee-free


def probe(trade=TRADE, mayhem=True, **kw):
    args = dict(head_slot=1000, head_known_at=100.0, heartbeat_age_s=1.0, gap_last_slots=[], graduated=False, mayhem=mayhem,
                sol_usd=180.0, evaluated_at=104.0)
    args.update(kw)
    return S.probe_from_tape(trade, **args)


def with_regular(pairs, **extra):
    return {**TRADE, "regular_fees": pairs, **extra}


def test_agent_ids_are_pinned_to_the_research_constant():
    assert S.MAYHEM_AGENT_IDS == research_state.MAYHEM_AGENT_IDS == frozenset({AGENT})


def test_agent_last_on_a_mayhem_coin_is_priced_with_the_regular_fee_not_zero():
    check, ev = probe(with_regular([(95, 30)] * 3))
    assert check["status"] == "pass" and ev["fee_bps"] == [95, 30] and ev["fee_basis"] == "non_agent_trades"
    zero = probe({**TRADE, "regular_fees": None, "fee_bps": 95, "creator_fee_bps": 30}, mayhem=False)[0]   # legacy path for reference
    assert check["round_trip_loss_pct"] == pytest.approx(zero["round_trip_loss_pct"])
    # and it is NOT what a naive last-trade read would say
    naive = S.probe_from_tape({**TRADE}, head_slot=1000, head_known_at=100.0, heartbeat_age_s=1.0, gap_last_slots=[],
                              graduated=False, mayhem=False, sol_usd=180.0, evaluated_at=104.0)[0]
    assert naive["round_trip_loss_pct"] < check["round_trip_loss_pct"]


@pytest.mark.parametrize("pairs", [[], [(95, 30)], [(95, 30), (0, 30), (95, 100)], [(None, None)] * 3, [(95, 30), (None, 30)],
                                   [(95, 30), (95, -1)], [(95, 30), (95, True)], [(95, 30), (10_000, 30)], "x", [(95,)]])
def test_unproven_regular_fees_are_fee_unknown(pairs):
    check, _ = probe(with_regular(pairs))
    assert check == {"status": "unknown", "reason": "fee_unknown"}


def test_an_unread_mayhem_flag_stays_unknown_even_with_regular_fees():
    assert probe(with_regular([(95, 30)] * 3), mayhem=None)[0] == {"status": "unknown", "reason": "fee_unknown"}


@pytest.mark.parametrize("pairs", [[(95, -1)] * 2, [(True, True)] * 2, [(95, 30.0)] * 2, [(95, 10_000)] * 2, [("95", "30")] * 2])
def test_agreeing_but_invalid_values_are_not_a_proof(pairs):
    assert S.regular_fee_pair(pairs) is None


def test_regular_fee_pair_rules():
    assert S.regular_fee_pair([(95, 30), (95, 30)]) == (95, 30)
    assert S.regular_fee_pair([(95, 30), (0, 0), (95, 30)]) == (95, 30)          # 2 of the newest 3 agree; max keeps (95, 30)
    assert S.regular_fee_pair([(95, 30), (95, 30), (95, 100)]) == (95, 100)      # a higher fee in the window raises the estimate
    assert S.regular_fee_pair([(95, 30), (0, 30), (95, 100)]) is None            # no two agree among the newest three
    assert S.regular_fee_pair([(0, 0), (0, 0)]) == (0, 0)                        # a regular zero pair is only as good as its evidence


def test_the_legacy_path_without_regular_fees_still_refuses_mayhem_and_null_fees():
    legacy = {k: v for k, v in TRADE.items()}
    assert probe(legacy, mayhem=True)[0]["reason"] == "fee_unknown"
    assert probe({**legacy, "fee_bps": None}, mayhem=False)[0]["reason"] == "fee_unknown"
    assert probe({**legacy, "fee_bps": 95, "creator_fee_bps": 30}, mayhem=False)[0]["status"] == "pass"


def load_assembler():
    spec = importlib.util.spec_from_file_location("asm_fees", Path(__file__).resolve().parents[1] / "scripts" / "memecoin_desk_assembler.py")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    spec.loader.exec_module(mod)
    return mod


def swaps_db(tmp, rows, day):
    d = tmp / "swaps"
    d.mkdir(exist_ok=True)
    c = sqlite3.connect(str(d / ("%s.db" % day)))
    c.execute("CREATE TABLE swaps(slot INTEGER, signature TEXT, ordinal INTEGER, mint TEXT, actor TEXT, fee_bps INTEGER, "
              "creator_fee_bps INTEGER, PRIMARY KEY(signature, ordinal))")
    c.executemany("INSERT INTO swaps VALUES(?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def test_assembler_hands_over_the_newest_non_agent_pairs_only(tmp_path):
    A = load_assembler()
    now = 1_800_000_000.0
    day = A.utc_day(now)
    rows = [(10 + i, "s%d" % i, 0, "M", AGENT if i % 2 else "w%d" % i, 0 if i % 2 else 95, 0 if i % 2 else 30) for i in range(12)]
    rows += [(5, "other", 0, "OTHERMINT", "w", 1, 1)]
    swaps_db(tmp_path, rows, day)
    got = A.regular_fees(tmp_path, "M", now)
    assert got == [(95, 30)] * 5 and len(got) == S.REGULAR_FEE_TRADES                  # six regular trades exist; newest five, no agent (0,0)
    assert A.regular_fees(tmp_path, "NOPE", now) == []
    assert A.regular_fees(tmp_path / "missing", "M", now) == []


def test_assembler_spans_yesterdays_file_and_a_read_error_is_empty_not_a_guess(tmp_path):
    A = load_assembler()
    now = 1_800_000_000.0
    swaps_db(tmp_path, [(100, "t1", 0, "M", "w1", 95, 30), (101, "t2", 0, "M", AGENT, 0, 0)], A.utc_day(now))
    swaps_db(tmp_path, [(50, "y1", 0, "M", "w2", 95, 30), (51, "y2", 0, "M", "w3", 95, 30)], A.utc_day(now - 86400))
    assert A.regular_fees(tmp_path, "M", now) == [(95, 30)] * 3
    (tmp_path / "swaps" / ("%s.db" % A.utc_day(now))).write_bytes(b"garbage" * 50)
    assert A.regular_fees(tmp_path, "M", now) == []                                    # unreadable today => nothing proven
