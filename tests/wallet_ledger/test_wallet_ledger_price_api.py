from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.ledger.price_api import (
    PRICE_HISTORY_SOURCE_VERSION,
    BatchPriceRequest,
    fetch_batch_price_history,
)


class _Response:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FailingResponse(_Response):
    def raise_for_status(self) -> None:
        raise RuntimeError("clob down")


class _Client:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, *, json: dict) -> _Response:
        self.calls.append((path, json))
        return self.responses.pop(0)


def _request(token_id: str) -> BatchPriceRequest:
    return BatchPriceRequest(
        token_id=token_id,
        fidelity_minutes=1,
        range_start_ts=1700000000,
        range_end_ts=1700000120,
    )


def test_fetch_batch_price_history_chunks_requests_at_twenty_and_serializes_payload() -> None:
    responses = [
        _Response({"prices": []}),
        _Response({"prices": []}),
    ]
    client = _Client(responses)
    requests = [_request(f"token-{idx}") for idx in range(21)]

    result = fetch_batch_price_history(client, requests, PRICE_HISTORY_SOURCE_VERSION)

    assert result.source_version == PRICE_HISTORY_SOURCE_VERSION
    assert len(client.calls) == 2
    assert client.calls[0][0] == "/batch-prices-history"
    assert len(client.calls[0][1]["requests"]) == 20
    assert len(client.calls[1][1]["requests"]) == 1
    assert client.calls[0][1]["requests"][0] == {
        "token_id": "token-0",
        "fidelity_minutes": 1,
        "range_start_ts": 1700000000,
        "range_end_ts": 1700000120,
    }
    assert result.batch_errors == []


def test_fetch_batch_price_history_parses_points_and_preserves_response_order() -> None:
    client = _Client(
        [
            _Response(
                {
                    "prices": [
                        {"token_id": "token-b", "history": [{"t": 2, "p": 0.2}, {"t": 3, "p": 0.3}]},
                        {"token_id": "token-a", "history": [{"t": 1, "p": 0.1}]},
                    ]
                }
            )
        ]
    )

    result = fetch_batch_price_history(client, [_request("token-a"), _request("token-b")], "source-v1")

    assert [(point.token_id, point.ts, point.price) for point in result.points] == [
        ("token-b", 2, 0.2),
        ("token-b", 3, 0.3),
        ("token-a", 1, 0.1),
    ]
    assert result.empty_requests == []


def test_fetch_batch_price_history_tracks_empty_series_without_fake_points() -> None:
    client = _Client([_Response({"prices": [{"token_id": "token-a", "history": []}]})])

    result = fetch_batch_price_history(client, [_request("token-a")], "source-v1")

    assert result.points == []
    assert [item.token_id for item in result.empty_requests] == ["token-a"]


def test_fetch_batch_price_history_surfaces_api_error_at_batch_level() -> None:
    client = _Client([_FailingResponse({})])

    result = fetch_batch_price_history(client, [_request("token-a")], "source-v1")

    assert result.points == []
    assert result.empty_requests == []
    assert len(result.batch_errors) == 1
    assert result.batch_errors[0].message == "clob down"
    assert [item.token_id for item in result.batch_errors[0].requests] == ["token-a"]


def test_fetch_batch_price_history_rejects_empty_source_version() -> None:
    client = _Client([])

    with pytest.raises(ValueError, match="source_version"):
        fetch_batch_price_history(client, [_request("token-a")], "")
