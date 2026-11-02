from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


PRICE_HISTORY_SOURCE_VERSION = "batch-prices-history-v1"
MAX_BATCH_PRICE_REQUESTS = 20
BATCH_PRICE_HISTORY_PATH = "/batch-prices-history"


@dataclass(frozen=True)
class BatchPriceRequest:
    token_id: str
    fidelity_minutes: int
    range_start_ts: int
    range_end_ts: int


@dataclass(frozen=True)
class PricePoint:
    token_id: str
    fidelity_minutes: int
    ts: int
    price: float


@dataclass(frozen=True)
class BatchPriceError:
    requests: list[BatchPriceRequest]
    message: str


@dataclass(frozen=True)
class BatchPriceResult:
    source_version: str
    points: list[PricePoint]
    empty_requests: list[BatchPriceRequest]
    batch_errors: list[BatchPriceError]


class _HttpResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> Any: ...


class _HttpClient(Protocol):
    def post(self, path: str, *, json: dict[str, Any]) -> _HttpResponse: ...


def _chunks(items: list[BatchPriceRequest], size: int) -> list[list[BatchPriceRequest]]:
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def _request_payload(request: BatchPriceRequest) -> dict[str, int | str]:
    return {
        "token_id": request.token_id,
        "fidelity_minutes": request.fidelity_minutes,
        "range_start_ts": request.range_start_ts,
        "range_end_ts": request.range_end_ts,
    }


def _response_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        items = payload.get("prices", [])
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("batch price response must contain a prices list")
    return items


def _history_items(item: dict[str, Any]) -> list[dict[str, Any]]:
    history = item.get("history", item.get("prices", []))
    if history is None:
        return []
    if not isinstance(history, list):
        raise ValueError("batch price history entry must be a list")
    return history


def fetch_batch_price_history(
    client: _HttpClient,
    requests: list[BatchPriceRequest],
    source_version: str = PRICE_HISTORY_SOURCE_VERSION,
) -> BatchPriceResult:
    """Fetch shared CLOB price history in batches.

    Bump `PRICE_HISTORY_SOURCE_VERSION` whenever request payload shape,
    response parsing, or normalization changes.
    """
    if not source_version:
        raise ValueError("source_version is required")

    points: list[PricePoint] = []
    empty_requests: list[BatchPriceRequest] = []
    batch_errors: list[BatchPriceError] = []

    for batch in _chunks(requests, MAX_BATCH_PRICE_REQUESTS):
        payload = {"requests": [_request_payload(request) for request in batch]}
        try:
            response = client.post(BATCH_PRICE_HISTORY_PATH, json=payload)
            response.raise_for_status()
            items = _response_items(response.json())
        except Exception as exc:  # noqa: BLE001 - callers need batch-level API errors.
            batch_errors.append(BatchPriceError(batch, str(exc)))
            continue

        requests_by_token = {request.token_id: request for request in batch}
        for item in items:
            token_id = str(item["token_id"])
            request = requests_by_token[token_id]
            history = _history_items(item)
            if not history:
                empty_requests.append(request)
                continue
            for point in history:
                points.append(
                    PricePoint(
                        token_id=token_id,
                        fidelity_minutes=request.fidelity_minutes,
                        ts=int(point.get("t", point.get("ts"))),
                        price=float(point.get("p", point.get("price"))),
                    )
                )

    return BatchPriceResult(
        source_version=source_version,
        points=points,
        empty_requests=empty_requests,
        batch_errors=batch_errors,
    )
