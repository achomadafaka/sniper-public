import copy

from sniper.hyperlive import copy_sim as CS


def test_mirror_actions_opens_new_holds_same_closes_exited():
    target = {"ETH": {"side": "long"}, "BTC": {"side": "short"}}
    book = {"ETH": {"side": "long", "entry_mark": 2000.0},
            "SOL": {"side": "long", "entry_mark": 150.0}}      # target no longer holds SOL
    marks = {"ETH": 2040.0, "BTC": 60000.0, "SOL": 148.0}
    a = CS.mirror_actions(target, book, marks, fee_bps=9.0)
    assert [o["coin"] for o in a["open"]] == ["BTC"] and a["open"][0]["side"] == "short"
    assert a["open"][0]["entry_mark"] == 60000.0
    assert [h["coin"] for h in a["hold"]] == ["ETH"]
    cl = a["close"]
    assert len(cl) == 1 and cl[0]["coin"] == "SOL"
    assert cl[0]["pnl_bps"] == round((148.0 - 150.0) / 150.0 * 1e4 - 9.0, 2)   # long, net of fee


def test_mirror_actions_flip_closes_then_opens():
    target = {"ETH": {"side": "short"}}                        # target flipped to short
    book = {"ETH": {"side": "long", "entry_mark": 2000.0}}
    marks = {"ETH": 2010.0}
    a = CS.mirror_actions(target, book, marks)
    assert [c["coin"] for c in a["close"]] == ["ETH"]          # close the long
    assert [o["coin"] for o in a["open"]] == ["ETH"] and a["open"][0]["side"] == "short"


def test_mirror_actions_skips_open_without_a_mark():
    target = {"WIF": {"side": "long"}}
    a = CS.mirror_actions(target, {}, marks={})                # no mark for WIF
    assert a["open"] == [] and a["close"] == [] and a["hold"] == []


def test_mirror_actions_matches_target_book_case_insensitively():
    target = {"kPEPE": {"side": "short"}}
    book = {"KPEPE": {"side": "short", "entry_mark": 10.0}}

    result = CS.mirror_actions(target, book, {"KPEPE": 9.0})

    assert result == {
        "open": [],
        "close": [],
        "hold": [{"coin": "kPEPE", "side": "short"}],
    }


def test_mirror_actions_closes_with_case_insensitive_mark_and_book_spelling():
    result = CS.mirror_actions(
        {},
        {"kSHIB": {"side": "short", "entry_mark": 10.0}},
        {"KSHIB": 9.0},
    )

    assert result["close"][0]["coin"] == "kSHIB"
    assert result["close"][0]["exit_mark"] == 9.0


def test_mirror_actions_opens_with_target_spelling_and_case_insensitive_mark():
    result = CS.mirror_actions(
        {"kBONK": {"side": "short"}}, {}, {"KBONK": 2.0}
    )

    assert result["open"] == [
        {"coin": "kBONK", "side": "short", "entry_mark": 2.0}
    ]


def test_mirror_actions_side_flip_preserves_each_source_spelling():
    result = CS.mirror_actions(
        {"KPEPE": {"side": "short"}},
        {"kPEPE": {"side": "long", "entry_mark": 10.0}},
        {"KPEPE": 9.0},
    )

    assert result["open"][0]["coin"] == "KPEPE"
    assert result["close"][0]["coin"] == "kPEPE"


def test_mirror_actions_keeps_native_uppercase_ticker():
    result = CS.mirror_actions(
        {"KAITO": {"side": "long"}}, {}, {"KAITO": 1.5}
    )

    assert result["open"][0]["coin"] == "KAITO"


def test_mirror_actions_case_collision_fails_closed_for_ambiguous_key():
    target = {
        "kPEPE": {"side": "short"},
        "KPEPE": {"side": "short"},
    }

    result = CS.mirror_actions(target, {}, {"KPEPE": 9.0})

    assert result == {"open": [], "close": [], "hold": []}


def test_mirror_actions_does_not_mutate_inputs():
    target = {"kFLOKI": {"side": "short"}}
    book = {"KFLOKI": {"side": "long", "entry_mark": 10.0}}
    marks = {"KFLOKI": 9.0}
    before = copy.deepcopy((target, book, marks))

    CS.mirror_actions(target, book, marks)

    assert (target, book, marks) == before


def test_book_unrealized_bps_sums_mark_to_market():
    book = {"ETH": {"side": "long", "entry_mark": 2000.0},
            "BTC": {"side": "short", "entry_mark": 60000.0}}
    marks = {"ETH": 2040.0, "BTC": 59400.0}                    # ETH +200bps, BTC short +100bps (gross)
    u = CS.book_unrealized_bps(book, marks)
    # gross (no fee on open mark-to-market): +200 and +100
    assert u["per_coin"]["ETH"] == 200.0 and u["per_coin"]["BTC"] == 100.0
    assert u["total_bps"] == 300.0 and u["n"] == 2


def test_book_unrealized_matches_provider_marks_without_rewriting_identity():
    result = CS.book_unrealized_bps(
        {"KPEPE": {"side": "short", "entry_mark": 10.0}},
        {"kPEPE": 9.0},
    )

    assert result == {"per_coin": {"KPEPE": 1000.0}, "total_bps": 1000.0, "n": 1}
