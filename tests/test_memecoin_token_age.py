"""A derived entry age must be honest about what it does not know.

entry_token_age_s comes from DexScreener's pairCreatedAt, and a LaunchLab curve is not indexed
before it graduates: 2 of 32 funded LaunchLab closes since 2026-09-01 carry an age, against 93% on
Raydium CPMM. The chain can answer instead — but only when the walk actually reaches the bottom,
and a partial answer here is worse than none, because it would make an old coin look fresh.
"""
import pytest

from sniper.services.memecoin_token_age import (
    SOURCE_MINT_FIRST_SLOT, SOURCE_PAIR, age_seconds, mint_first_block_time)


def _pager(pages):
    """A getSignaturesForAddress double. `pages` is a list of signature lists, oldest last."""
    calls = []

    def get_sigs(address, params):
        calls.append(params)
        index = len(calls) - 1
        return pages[index] if index < len(pages) else []
    get_sigs.calls = calls
    return get_sigs


def _sig(name, ts):
    return {"signature": name, "blockTime": ts}


def test_a_short_page_is_the_bottom_and_dates_the_mint():
    pager = _pager([[_sig("c", 300), _sig("b", 200), _sig("a", 100)]])
    assert mint_first_block_time("M", get_sigs_fn=pager, page_limit=1000) == 100


def test_it_pages_backwards_using_before():
    full = [_sig("s%d" % i, 1000 + i) for i in range(3)]
    pager = _pager([full, [_sig("old", 50)]])
    assert mint_first_block_time("M", get_sigs_fn=pager, page_limit=3) == 50
    assert pager.calls[0].get("before") is None, "the first page must not be anchored"
    assert pager.calls[1]["before"] == "s2", "the next page anchors on the last signature seen"


def test_a_walk_that_never_reaches_the_bottom_returns_NOTHING():
    """THE PROPERTY THIS FILE EXISTS FOR. The oldest signature we happened to see is an UPPER
    bound on creation time, so using it would understate the age and make an old coin read as
    fresh — the most consequential direction this field has."""
    page = [_sig("s%d" % i, 9000 + i) for i in range(4)]
    pager = _pager([page] * 10)
    assert mint_first_block_time("M", get_sigs_fn=pager, max_pages=3, page_limit=4) is None


def test_an_empty_history_is_no_answer():
    assert mint_first_block_time("M", get_sigs_fn=_pager([[]])) is None


def test_an_rpc_failure_is_no_answer_not_a_zero():
    def boom(address, params):
        raise RuntimeError("rpc down")
    assert mint_first_block_time("M", get_sigs_fn=boom) is None


def test_signatures_without_a_blocktime_are_skipped():
    pager = _pager([[{"signature": "x"}, _sig("a", 100)]])
    assert mint_first_block_time("M", get_sigs_fn=pager, page_limit=1000) == 100


# --- the age itself ------------------------------------------------------------------------------

def test_age_is_entry_minus_creation():
    age = age_seconds("2026-09-22T18:23:24+00:00", 1790000000)
    assert age == pytest.approx(1790101404.0 - 1790000000, abs=1)


def test_a_naive_timestamp_is_refused():
    """An entry time with no offset cannot be compared to a chain epoch without inventing a zone."""
    assert age_seconds("2026-09-22T18:23:24", 1790000000) is None


def test_a_negative_age_is_refused_rather_than_clamped():
    """Impossible, therefore evidence the walk was wrong. Clamping to zero would publish the coin
    as brand new, which is exactly the reading that matters most."""
    assert age_seconds("2020-01-01T00:00:00+00:00", 1790000000) is None


def test_no_creation_time_is_no_age():
    assert age_seconds("2026-09-22T18:23:24+00:00", None) is None


def test_the_two_sources_are_distinct_labels():
    """A derived inference and a venue's own pair timestamp must never be read as the same fact."""
    assert SOURCE_PAIR != SOURCE_MINT_FIRST_SLOT
    assert SOURCE_PAIR == "dexscreener_pair"
    assert SOURCE_MINT_FIRST_SLOT == "mint_first_slot"
