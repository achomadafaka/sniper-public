"""Tests for Goldsky-public Polymarket subgraph reader.

Closes wallet-protocol-bulletproofing C2 (>20K-row activity backfill) and C1
(capital ledger via splits/merges/redemptions). Reader paginates by id_gt
cursor on the public Goldsky endpoints — no auth, free, no row cap.

HTTP is mocked at the `requests.Session.post` boundary so these tests are
hermetic.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from sniper.analysis.wallet_protocol.subgraph_reader import (
    ACTIVITY_ENDPOINT,
    ORDERBOOK_ENDPOINT,
    PAGE_SIZE,
    fetch_merges,
    fetch_order_filled_events,
    fetch_redemptions,
    fetch_splits,
)


def _mock_session(responses: list[dict]) -> MagicMock:
    """Build a session that returns the given JSON payloads in order on .post()."""
    session = MagicMock()
    response_mocks = []
    for r in responses:
        m = MagicMock()
        m.status_code = 200
        m.json.return_value = r
        m.raise_for_status.return_value = None
        response_mocks.append(m)
    session.post.side_effect = response_mocks
    return session


# ---------------------------------------------------------------------------
# orderFilledEvents — C2 path
# ---------------------------------------------------------------------------


def test_order_filled_events_single_page_returns_rows():
    rows = [
        {"id": "0xaa_0x01", "timestamp": "1700000000",
         "maker": "0xwallet", "taker": "0xother",
         "makerAssetId": "0", "takerAssetId": "tok1",
         "makerAmountFilled": "1000000", "takerAmountFilled": "2000000",
         "fee": "0", "transactionHash": "0xaa"},
    ]
    sess = _mock_session([
        {"data": {"orderFilledEvents": rows}},
        {"data": {"orderFilledEvents": []}},  # taker side empty
    ])
    out = fetch_order_filled_events("0xWallet", session=sess)
    assert len(out) == 1
    assert out[0]["id"] == "0xaa_0x01"
    # Endpoint correct
    assert sess.post.call_args_list[0][0][0] == ORDERBOOK_ENDPOINT


def test_order_filled_events_paginates_by_id_until_empty():
    """Two pages on the maker side, then taker side empty → 2 rows total."""
    page1 = [{"id": f"0x_{i:03d}", "timestamp": f"{1700000000 + i}",
              "maker": "0xwallet", "taker": "0xother",
              "makerAssetId": "0", "takerAssetId": "tok",
              "makerAmountFilled": "1", "takerAmountFilled": "1",
              "fee": "0", "transactionHash": f"0x_{i:03d}"}
             for i in range(2)]
    sess = _mock_session([
        {"data": {"orderFilledEvents": page1}},      # maker page 1 (2 rows)
        {"data": {"orderFilledEvents": []}},          # maker page 2 — empty, stop
        {"data": {"orderFilledEvents": []}},          # taker side empty
    ])
    out = fetch_order_filled_events(
        "0xWallet", session=sess, page_size=2,
    )
    assert len(out) == 2
    # 3 calls total: 2 maker, 1 taker
    assert sess.post.call_count == 3


def test_order_filled_events_lowercases_wallet_address():
    """GraphQL where-clauses are case-sensitive; wallet input must be normalized."""
    sess = _mock_session([
        {"data": {"orderFilledEvents": []}},
        {"data": {"orderFilledEvents": []}},
    ])
    fetch_order_filled_events("0xABCDEF", session=sess)
    body = sess.post.call_args_list[0][1]["json"]
    assert "0xabcdef" in body["query"]


def test_order_filled_events_dedupes_maker_and_taker_overlap():
    """Same fill can appear as maker AND taker (rare, but possible);
    union must dedupe by id."""
    row = {"id": "0x1_0x1", "timestamp": "1700000000",
           "maker": "0xwallet", "taker": "0xwallet",
           "makerAssetId": "0", "takerAssetId": "tok",
           "makerAmountFilled": "1", "takerAmountFilled": "1",
           "fee": "0", "transactionHash": "0x1"}
    sess = _mock_session([
        {"data": {"orderFilledEvents": [row]}},   # maker
        {"data": {"orderFilledEvents": []}},
        {"data": {"orderFilledEvents": [row]}},   # taker (same)
        {"data": {"orderFilledEvents": []}},
    ])
    out = fetch_order_filled_events(
        "0xwallet", session=sess, page_size=1,
    )
    assert len(out) == 1


def test_order_filled_events_max_rows_caps_returns():
    """max_rows truncates without re-paginating."""
    rows = [{"id": f"0x{i}", "timestamp": "1", "maker": "0xw",
             "taker": "0xo", "makerAssetId": "0", "takerAssetId": "t",
             "makerAmountFilled": "1", "takerAmountFilled": "1",
             "fee": "0", "transactionHash": f"0x{i}"} for i in range(5)]
    sess = _mock_session([
        {"data": {"orderFilledEvents": rows}},
        {"data": {"orderFilledEvents": []}},
    ])
    out = fetch_order_filled_events(
        "0xw", session=sess, max_rows=2, side="maker",
    )
    assert len(out) == 2


def test_order_filled_events_side_filter():
    """side='maker' must only query maker endpoint, never taker."""
    sess = _mock_session([{"data": {"orderFilledEvents": []}}])
    fetch_order_filled_events("0xw", session=sess, side="maker")
    assert sess.post.call_count == 1
    body = sess.post.call_args_list[0][1]["json"]
    assert "maker:" in body["query"]
    assert "taker:" not in body["query"]


def test_order_filled_events_raises_on_http_error():
    sess = MagicMock()
    err_resp = MagicMock()
    err_resp.status_code = 502
    err_resp.raise_for_status.side_effect = Exception("502 Bad Gateway")
    sess.post.return_value = err_resp
    with pytest.raises(Exception, match="502"):
        fetch_order_filled_events("0xw", session=sess)


def test_order_filled_events_default_page_size_is_max():
    sess = _mock_session([
        {"data": {"orderFilledEvents": []}},
        {"data": {"orderFilledEvents": []}},
    ])
    fetch_order_filled_events("0xw", session=sess)
    body = sess.post.call_args_list[0][1]["json"]
    assert f"first: {PAGE_SIZE}" in body["query"]


# ---------------------------------------------------------------------------
# splits / merges / redemptions — C1 path
# ---------------------------------------------------------------------------


def test_splits_paginates_and_returns_normalized_rows():
    rows = [
        {"id": "0xa_0x1", "timestamp": "1700000000",
         "stakeholder": "0xwallet",
         "condition": "0xcid", "amount": "125000000000"},
        {"id": "0xb_0x2", "timestamp": "1700000100",
         "stakeholder": "0xwallet",
         "condition": "0xcid", "amount": "75000000000"},
    ]
    sess = _mock_session([
        {"data": {"splits": rows}},
        {"data": {"splits": []}},
    ])
    out = fetch_splits("0xWallet", session=sess)
    assert len(out) == 2
    assert out[0]["amount"] == "125000000000"
    # Endpoint correct
    assert sess.post.call_args_list[0][0][0] == ACTIVITY_ENDPOINT


def test_merges_endpoint_and_entity_correct():
    sess = _mock_session([{"data": {"merges": []}}])
    fetch_merges("0xw", session=sess)
    body = sess.post.call_args_list[0][1]["json"]
    assert "merges(" in body["query"]
    assert sess.post.call_args_list[0][0][0] == ACTIVITY_ENDPOINT


def test_redemptions_endpoint_and_entity_correct():
    sess = _mock_session([{"data": {"redemptions": []}}])
    fetch_redemptions("0xw", session=sess)
    body = sess.post.call_args_list[0][1]["json"]
    assert "redemptions(" in body["query"]


def test_splits_lowercases_wallet():
    sess = _mock_session([{"data": {"splits": []}}])
    fetch_splits("0xABCDEF", session=sess)
    body = sess.post.call_args_list[0][1]["json"]
    assert "0xabcdef" in body["query"]


def test_splits_id_cursor_advances():
    """After page 1, second query must include id_gt = last id of page 1."""
    rows = [{"id": "0xa_0x01", "timestamp": "1", "stakeholder": "0xw",
             "condition": "0xc", "amount": "1"}]
    sess = _mock_session([
        {"data": {"splits": rows}},
        {"data": {"splits": []}},
    ])
    fetch_splits("0xw", session=sess, page_size=1)
    second = sess.post.call_args_list[1][1]["json"]
    assert "0xa_0x01" in second["query"]


# ---------------------------------------------------------------------------
# GraphQL-error handling (2026-05-28 regression)
# ---------------------------------------------------------------------------
#
# Root cause of the 0x20ad75 activity_rows=0 capture: Goldsky returns HTTP 200
# with a top-level `errors` array (Postgres statement timeout on a high-volume
# wallet). The old _paginate read the absent `data` field as "no more rows" and
# silently returned an empty/truncated result. These tests pin the fix:
# detect the errors payload, shrink the page size and retry on timeout, and
# raise loudly if the timeout survives.

from sniper.analysis.wallet_protocol.subgraph_reader import (  # noqa: E402
    MIN_PAGE_SIZE,
    SubgraphQueryError,
    _post_checked,
)


def _timeout_payload() -> dict:
    return {
        "errors": [
            {"message": "Failed to get entities from store: canceling statement "
                        "due to statement timeout"}
        ]
    }


def test_post_checked_raises_on_errors_payload():
    sess = _mock_session([_timeout_payload()])
    with pytest.raises(SubgraphQueryError) as exc:
        _post_checked(ORDERBOOK_ENDPOINT, "{ x }", session=sess)
    assert "statement timeout" in str(exc.value).lower()


def test_errors_payload_not_silently_treated_as_empty():
    """The core bug: an errors payload must NOT be read as 'no rows'. With the
    page already at the floor, a persistent timeout raises rather than returning
    []."""
    # Every response is a timeout error → exhausts retries → raises.
    sess = _mock_session([_timeout_payload() for _ in range(10)])
    with pytest.raises(SubgraphQueryError):
        fetch_order_filled_events("0xWallet", session=sess, side="maker",
                                  page_size=MIN_PAGE_SIZE)


def test_paginate_shrinks_page_and_recovers_on_timeout():
    """First page at 1000 times out; after shrink+retry the page succeeds and
    rows are returned (not silently dropped)."""
    good_rows = [
        {"id": "0xaa_0x01", "timestamp": "1", "maker": "0xwallet", "taker": "0xo",
         "makerAssetId": "0", "takerAssetId": "t", "makerAmountFilled": "1",
         "takerAmountFilled": "2", "fee": "0", "transactionHash": "0xaa"},
    ]
    sess = _mock_session([
        _timeout_payload(),                              # first attempt at 1000 — timeout
        {"data": {"orderFilledEvents": good_rows}},      # retry at 500 — succeeds
        {"data": {"orderFilledEvents": []}},             # next page empty → done
    ])
    out = fetch_order_filled_events("0xWallet", session=sess, side="maker", page_size=1000)
    assert len(out) == 1
    assert out[0]["id"] == "0xaa_0x01"
    # The retry query must have used a smaller page (≤ 500 after one halving)
    retry_query = sess.post.call_args_list[1][1]["json"]["query"]
    assert "first: 500" in retry_query


def test_paginate_shrinks_progressively_to_floor():
    """Repeated timeouts halve the page 1000→500→250→125→MIN, then succeed."""
    good_rows = [
        {"id": "0xbb_0x01", "timestamp": "1", "maker": "0xw", "taker": "0xo",
         "makerAssetId": "0", "takerAssetId": "t", "makerAmountFilled": "1",
         "takerAmountFilled": "2", "fee": "0", "transactionHash": "0xbb"},
    ]
    sess = _mock_session([
        _timeout_payload(),  # 1000 -> shrink to 500
        _timeout_payload(),  # 500  -> shrink to 250
        _timeout_payload(),  # 250  -> shrink to 125
        {"data": {"orderFilledEvents": good_rows}},  # 125 succeeds (above MIN_PAGE_SIZE=50)
        {"data": {"orderFilledEvents": []}},
    ])
    out = fetch_order_filled_events("0xWallet", session=sess, side="maker", page_size=1000)
    assert len(out) == 1
    # 4th call (index 3) is the success — its page should be 125
    success_query = sess.post.call_args_list[3][1]["json"]["query"]
    assert "first: 125" in success_query


def test_non_timeout_error_raises_immediately():
    """A non-timeout GraphQL error (e.g. bad field) must raise without retry —
    shrinking won't help a malformed query."""
    bad = {"errors": [{"message": "Type `OrderFilledEvent` has no field `bogus`"}]}
    sess = _mock_session([bad])
    with pytest.raises(SubgraphQueryError) as exc:
        fetch_order_filled_events("0xWallet", session=sess, side="maker", page_size=1000)
    assert "bogus" in str(exc.value).lower()
    # Only one call — no retry on a non-timeout error
    assert sess.post.call_count == 1
