"""Paced, read-only Solana round-trip quote evidence for the opportunity Radar."""

from __future__ import annotations

import heapq
import json
import queue
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from typing import Callable

from sniper.util.clock import real_utc_now_iso

from .engine import RemoteRoundTripProbe
from .paper_completion import PaperQuoteCompletion
from .state import PUMP_QUOTE_MINTS, PUMP_SOL_QUOTE_MINT
from .types import VenueId
from .venue_policy import scoreable_venues


NATIVE_SOL_MINT = "So11111111111111111111111111111111111111112"
JUPITER_QUOTE_URL = "https://api.jup.ag/swap/v1/quote"
SOLANA_VENUES = frozenset({VenueId.PUMPFUN_SOLANA} & scoreable_venues())


def parse_jupiter_quote(payload: object) -> int:
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("JUPITER_PROVIDER_UNAVAILABLE")
    try:
        amount = int(payload["outAmount"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("JUPITER_MALFORMED_QUOTE") from exc
    if amount <= 0 or not isinstance(payload.get("routePlan"), list) or not payload["routePlan"]:
        raise ValueError("JUPITER_ROUTE_ABSENT")
    return amount


def jupiter_quote(
    input_mint: str,
    output_mint: str,
    amount_atomic: int,
    *,
    slippage_bps: int = 300,
    timeout_s: float = 3.0,
) -> dict[str, object]:
    params = urllib.parse.urlencode(
        {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": int(amount_atomic),
            "slippageBps": int(slippage_bps),
            "swapMode": "ExactIn",
        }
    )
    request = urllib.request.Request(
        f"{JUPITER_QUOTE_URL}?{params}",
        headers={"Accept": "application/json", "User-Agent": "Sniper-Opportunity-Radar/2"},
    )
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        payload = json.loads(response.read())
    if not isinstance(payload, dict):
        raise ValueError("JUPITER_MALFORMED_QUOTE")
    return payload


@dataclass(frozen=True, order=True)
class _Request:
    priority_distance_bps: int
    negative_inward_velocity: int
    sequence: int
    key: tuple[object, ...] = field(compare=False)
    venue_id: VenueId = field(compare=False)
    launch_id: str = field(compare=False)
    token_id: str = field(compare=False)
    quote_in_atomic: int = field(compare=False)
    quote_mint: str = field(compare=False)
    state_fingerprint: str = field(compare=False)
    queued_at_s: float = field(compare=False)
    queued_at: str = field(compare=False)
    kind: str = field(default="roundtrip", compare=False)
    input_decimals: int | None = field(default=None, compare=False)
    output_decimals: int | None = field(default=None, compare=False)
    emit_paper_completion: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class JupiterQuoteReceipt:
    expected_out_atomic: int
    minimum_out_atomic: int


def parse_jupiter_quote_receipt(payload: object) -> JupiterQuoteReceipt:
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("JUPITER_PROVIDER_UNAVAILABLE")
    try:
        expected = int(payload["outAmount"])
        minimum = int(payload["otherAmountThreshold"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("JUPITER_MALFORMED_QUOTE") from exc
    if (
        expected <= 0
        or minimum <= 0
        or minimum > expected
        or not isinstance(payload.get("routePlan"), list)
        or not payload["routePlan"]
    ):
        raise ValueError("JUPITER_ROUTE_ABSENT")
    return JupiterQuoteReceipt(expected, minimum)


class OpportunitySolanaProbe:
    """Non-blocking callable backed by one bounded quote worker.

    The first call enqueues work and returns pending. A later engine observation
    consumes the cached exact entry/acquired-quantity exit result.
    """

    def __init__(
        self,
        *,
        quote_fn: Callable[[str, str, int], dict[str, object]] = jupiter_quote,
        min_call_interval_s: float = 2.0,
        cache_ttl_s: float = 5.0,
        queue_max: int = 1,
        paper_queue_max: int = 4,
        paper_starvation_bound_s: float = 5.0,
        paper_max_queue_age_ms: int = 30_000,
        max_queue_age_ms: int = 2_000,
        max_pair_age_ms: int = 3_500,
        max_completion_age_ms: int = 1_000,
        cache_max: int = 256,
        completion_max: int = 256,
        now_fn: Callable[[], float] = time.monotonic,
        utc_now_fn: Callable[[], str] = real_utc_now_iso,
        sleep_fn: Callable[[float], None] = time.sleep,
        start_worker: bool = True,
    ) -> None:
        if (
            min_call_interval_s < 0
            or cache_ttl_s <= 0
            or queue_max <= 0
            or paper_queue_max <= 0
            or paper_starvation_bound_s <= 0
            or paper_max_queue_age_ms <= 0
            or max_queue_age_ms <= 0
            or max_pair_age_ms <= 0
            or max_completion_age_ms <= 0
            or cache_max <= 0
            or completion_max <= 0
        ):
            raise ValueError("invalid Solana probe bounds")
        self.quote_fn = quote_fn
        self.min_call_interval_s = float(min_call_interval_s)
        self.cache_ttl_s = float(cache_ttl_s)
        self.max_queue_age_ms = int(max_queue_age_ms)
        self.max_pair_age_ms = int(max_pair_age_ms)
        self.max_completion_age_ms = int(max_completion_age_ms)
        self.cache_max = int(cache_max)
        self.completion_max = int(completion_max)
        self.now_fn = now_fn
        self.utc_now_fn = utc_now_fn
        self.sleep_fn = sleep_fn
        self.paper_starvation_bound_s = float(paper_starvation_bound_s)
        # Paper freshness is governed by the fill request's OWN 30s expiry (drive_paper_adapter
        # re-checks it every ~0.1s pass before booking a fill), not by capture's 2s bound — a
        # 2s bound here would convert every starvation-bound pull into a stale_quote rejection.
        self.paper_max_queue_age_ms = int(paper_max_queue_age_ms)
        self._queue: queue.PriorityQueue[_Request] = queue.PriorityQueue(maxsize=queue_max)
        # Paper fill requests get their OWN small FIFO. In the shared one-slot priority queue
        # they entered at priority 20_000, where the capture replacement path evicted them on
        # the next capture quote — under a busy tape (exactly when v5 alerts fire) every paper
        # request starved to its 30s `capacity_expired` receipt (0-for-2, 2026-08-05/06).
        self._paper_queue: queue.Queue[_Request] = queue.Queue(maxsize=paper_queue_max)
        self._lock = threading.Lock()
        self._pending: set[tuple[object, ...]] = set()
        self._cache: OrderedDict[
            tuple[object, ...], tuple[float, RemoteRoundTripProbe]
        ] = OrderedDict()
        self._paper_results: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._paper_completions: deque[PaperQuoteCompletion] = deque()
        self._completions: deque[RemoteRoundTripProbe] = deque()
        self._sequence = 0
        self._last_call_at: float | None = None
        self._closed = False
        self._completed = 0
        self._failed = 0
        self._requested = 0
        self._last_request_at: str | None = None
        self._last_success_at: str | None = None
        self._last_failure_at: str | None = None
        self._last_failure: str | None = None
        self._stale_queue_discards = 0
        self._cache_evictions = 0
        self._completion_drops = 0
        self._paper_completion_drops = 0
        self._queue_replacements = 0
        self._thread: threading.Thread | None = None
        if start_worker:
            self._thread = threading.Thread(
                target=self._run,
                name="opportunity-solana-quote",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _blocked(
        code: str,
        *,
        state=None,
        state_fingerprint: str | None = None,
        queued_at: str | None = None,
        completed_at: str | None = None,
        queue_delay_ms: int | None = None,
        pair_age_ms: int | None = None,
        result_age_ms: int | None = None,
    ) -> RemoteRoundTripProbe:
        return RemoteRoundTripProbe(
            False,
            False,
            "jupiter_quote_v3",
            0,
            code,
            launch_id=getattr(state, "launch_id", None),
            venue_id=(getattr(getattr(state, "venue_id", None), "value", None)),
            token_id=getattr(state, "token_id", None),
            state_fingerprint=state_fingerprint,
            queued_at=queued_at,
            completed_at=completed_at,
            queue_delay_ms=queue_delay_ms,
            pair_age_ms=pair_age_ms,
            result_age_ms=result_age_ms,
        )

    def _prune_cache_locked(self, now: float) -> None:
        expired = [
            key for key, (stored_at, _result) in self._cache.items()
            if now - stored_at > self.cache_ttl_s
        ]
        for key in expired:
            self._cache.pop(key, None)
            self._cache_evictions += 1

    def _append_completion_locked(self, result: RemoteRoundTripProbe) -> None:
        if len(self._completions) >= self.completion_max:
            self._completions.popleft()
            self._completion_drops += 1
        self._completions.append(result)

    def __call__(self, state, local_quote) -> RemoteRoundTripProbe:
        if state.venue_id not in SOLANA_VENUES:
            return self._blocked("REMOTE_SIMULATION_UNAVAILABLE", state=state)
        quote_in = int(getattr(local_quote, "quote_in_atomic", 0) or 0)
        if quote_in <= 0 or not state.token_id:
            return self._blocked("REMOTE_QUOTE_INPUT_MISSING", state=state)
        quote_mint = str(
            getattr(local_quote, "quote_mint", None) or state.quote_mint or ""
        )
        if quote_mint not in PUMP_QUOTE_MINTS:
            return self._blocked("REMOTE_QUOTE_ASSET_UNSUPPORTED", state=state)
        key = ("roundtrip", state.venue_id.value, state.token_id, quote_mint, quote_in)
        now = self.now_fn()
        with self._lock:
            self._prune_cache_locked(now)
            cached = self._cache.get(key)
            if cached:
                result_age_ms = max(0, int((now - cached[0]) * 1_000))
                if result_age_ms <= self.max_completion_age_ms:
                    self._cache.move_to_end(key)
                    return replace(
                        cached[1],
                        consumed_at=self.utc_now_fn(),
                        result_age_ms=result_age_ms,
                    )
                self._cache.pop(key, None)
                self._cache_evictions += 1
                return self._blocked(
                    "REMOTE_QUOTE_RESULT_STALE",
                    state=state,
                    state_fingerprint=getattr(local_quote, "state_fingerprint", None),
                    completed_at=cached[1].completed_at,
                    result_age_ms=result_age_ms,
                )
            if key in self._pending:
                return self._blocked(
                    "REMOTE_QUOTE_PENDING",
                    state=state,
                    state_fingerprint=getattr(local_quote, "state_fingerprint", None),
                )
            self._sequence += 1
            request = _Request(
                int(getattr(local_quote, "priority_distance_bps", 10_000)),
                -int(getattr(local_quote, "inward_velocity_bps_per_s", 0)),
                self._sequence,
                key,
                state.venue_id,
                str(getattr(local_quote, "launch_id", None) or state.launch_id),
                state.token_id,
                quote_in,
                quote_mint,
                str(getattr(local_quote, "state_fingerprint", None) or ""),
                now,
                self.utc_now_fn(),
            )
            try:
                self._queue.put_nowait(request)
            except queue.Full:
                replaced = False
                with self._queue.mutex:
                    queued = self._queue.queue[0] if self._queue.queue else None
                    if queued is not None and request < queued:
                        self._queue.queue[0] = request
                        heapq.heapify(self._queue.queue)
                        self._pending.discard(queued.key)
                        self._queue_replacements += 1
                        replaced = True
                if not replaced:
                    return self._blocked("REMOTE_QUOTE_QUEUE_FULL", state=state)
            self._pending.add(key)
            self._requested += 1
            self._last_request_at = request.queued_at
        return self._blocked(
            "REMOTE_QUOTE_PENDING",
            state=state,
            state_fingerprint=request.state_fingerprint,
            queued_at=request.queued_at,
        )

    def paper_quote(
        self,
        *,
        request_id: str,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        input_decimals: int,
        output_decimals: int,
    ) -> dict[str, object]:
        """Poll or enqueue one lower-priority exact-input BUY quote."""
        if not request_id or not output_mint:
            return {"status": "rejected", "reason": "identity_invalid"}
        if input_mint not in PUMP_QUOTE_MINTS:
            return {"status": "rejected", "reason": "identity_invalid"}
        if (
            isinstance(amount_atomic, bool)
            or not isinstance(amount_atomic, int)
            or amount_atomic <= 0
            or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30
                for value in (input_decimals, output_decimals)
            )
        ):
            return {"status": "rejected", "reason": "identity_invalid"}
        key = ("paper", request_id)
        now = self.now_fn()
        with self._lock:
            completed = self._paper_results.get(request_id)
            if completed is not None:
                self._paper_results.move_to_end(request_id)
                return dict(completed)
            if key in self._pending:
                return {"status": "pending"}
            self._sequence += 1
            request = _Request(
                20_000,
                0,
                self._sequence,
                key,
                VenueId.PUMPFUN_SOLANA,
                request_id,
                output_mint,
                amount_atomic,
                input_mint,
                request_id,
                now,
                self.utc_now_fn(),
                kind="paper",
                input_decimals=input_decimals,
                output_decimals=output_decimals,
            )
            try:
                self._paper_queue.put_nowait(request)
            except queue.Full:
                return {"status": "pending"}
            self._pending.add(key)
            self._requested += 1
            self._last_request_at = request.queued_at
        return {"status": "pending"}

    def paper_exit_quote(
        self,
        *,
        request_id: str,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        input_decimals: int,
        output_decimals: int,
    ) -> dict[str, object]:
        """Poll or enqueue one lower-priority exact-input sell quote."""
        if output_mint not in PUMP_QUOTE_MINTS or not input_mint:
            return {"status": "rejected", "reason": "identity_invalid"}
        return self._paper_exact_input_quote(
            request_id=request_id,
            input_mint=input_mint,
            output_mint=output_mint,
            amount_atomic=amount_atomic,
            input_decimals=input_decimals,
            output_decimals=output_decimals,
        )

    def paper_request_pending(self, request_id: str) -> bool:
        """Return true only when the probe owns this paper request."""
        with self._lock:
            return ("paper", str(request_id)) in self._pending

    def _paper_exact_input_quote(
        self,
        *,
        request_id: str,
        input_mint: str,
        output_mint: str,
        amount_atomic: int,
        input_decimals: int,
        output_decimals: int,
    ) -> dict[str, object]:
        if (
            not request_id
            or not input_mint
            or not output_mint
            or isinstance(amount_atomic, bool)
            or not isinstance(amount_atomic, int)
            or amount_atomic <= 0
            or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 30
                for value in (input_decimals, output_decimals)
            )
        ):
            return {"status": "rejected", "reason": "identity_invalid"}
        key = ("paper", request_id)
        now = self.now_fn()
        with self._lock:
            completed = self._paper_results.get(request_id)
            if completed is not None:
                self._paper_results.move_to_end(request_id)
                return dict(completed)
            if key in self._pending:
                return {"status": "pending"}
            self._sequence += 1
            request = _Request(
                20_000,
                0,
                self._sequence,
                key,
                VenueId.PUMPFUN_SOLANA,
                request_id,
                output_mint,
                amount_atomic,
                input_mint,
                request_id,
                now,
                self.utc_now_fn(),
                kind="paper",
                input_decimals=input_decimals,
                output_decimals=output_decimals,
                emit_paper_completion=True,
            )
            try:
                self._paper_queue.put_nowait(request)
            except queue.Full:
                return {"status": "pending"}
            self._pending.add(key)
            self._requested += 1
            self._last_request_at = request.queued_at
        return {"status": "pending"}

    def _pace(self) -> None:
        if self._last_call_at is not None:
            delay = self.min_call_interval_s - (self.now_fn() - self._last_call_at)
            if delay > 0:
                self.sleep_fn(delay)
        self._last_call_at = self.now_fn()

    def _process_paper(self, request: _Request) -> dict[str, object]:
        started_s = self.now_fn()
        if (started_s - request.queued_at_s) * 1_000 > self.paper_max_queue_age_ms:
            return {"status": "rejected", "reason": "stale_quote"}
        started_at = self.utc_now_fn()
        try:
            provider_input_mint = (
                NATIVE_SOL_MINT if request.quote_mint == PUMP_SOL_QUOTE_MINT else request.quote_mint
            )
            provider_output_mint = (
                NATIVE_SOL_MINT if request.token_id == PUMP_SOL_QUOTE_MINT else request.token_id
            )
            self._pace()
            payload = self.quote_fn(
                provider_input_mint, provider_output_mint, request.quote_in_atomic
            )
            receipt = parse_jupiter_quote_receipt(payload)
            try:
                provider_in = int(payload["inAmount"])
                slippage_bps = int(payload["slippageBps"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("JUPITER_MALFORMED_QUOTE") from exc
            if provider_in != request.quote_in_atomic or slippage_bps < 0:
                raise ValueError("JUPITER_MALFORMED_QUOTE")
            completed_at = self.utc_now_fn()
            return {
                "status": "filled",
                "economics": {
                    "expected_out_atomic": receipt.expected_out_atomic,
                    "floor_out_atomic": receipt.minimum_out_atomic,
                    "quote_provider": "jupiter_quote_v3",
                    "quote_in_asset": provider_input_mint,
                    "quote_in_atomic": request.quote_in_atomic,
                    "quote_in_decimals": request.input_decimals,
                    "quote_out_asset": provider_output_mint,
                    "quote_out_decimals": request.output_decimals,
                    "slippage_bps": slippage_bps,
                    "route_binding": {"route_plan": payload["routePlan"]},
                    "quote_payload": payload,
                    "quote_started_at": started_at,
                    "quote_completed_at": completed_at,
                },
            }
        except Exception as exc:
            code = str(exc).strip()
            reason = {
                "JUPITER_MALFORMED_QUOTE": "malformed_quote",
                "JUPITER_ROUTE_ABSENT": "no_route",
            }.get(code, "quote_unavailable")
            return {"status": "rejected", "reason": reason}

    def _process(self, request: _Request) -> RemoteRoundTripProbe | dict[str, object]:
        if request.kind == "paper":
            return self._process_paper(request)
        started = self.now_fn()
        queue_delay_ms = max(0, int((started - request.queued_at_s) * 1_000))
        state_view = type(
            "RequestState",
            (),
            {
                "launch_id": request.launch_id,
                "venue_id": request.venue_id,
                "token_id": request.token_id,
            },
        )()
        if (started - request.queued_at_s) * 1_000 > self.max_queue_age_ms:
            return self._blocked(
                "REMOTE_QUOTE_QUEUE_STALE",
                state=state_view,
                state_fingerprint=request.state_fingerprint,
                queued_at=request.queued_at,
                completed_at=self.utc_now_fn(),
                queue_delay_ms=queue_delay_ms,
            )
        try:
            provider_quote_mint = (
                NATIVE_SOL_MINT
                if request.quote_mint == PUMP_SOL_QUOTE_MINT
                else request.quote_mint
            )
            self._pace()
            entry_payload = self.quote_fn(
                provider_quote_mint, request.token_id, request.quote_in_atomic
            )
            entry_receipt = parse_jupiter_quote_receipt(entry_payload)
            tokens_out = entry_receipt.expected_out_atomic
            entry_done_s = self.now_fn()
            entry_quoted_at = self.utc_now_fn()
            self._pace()
            exit_payload = self.quote_fn(request.token_id, provider_quote_mint, tokens_out)
            exit_receipt = parse_jupiter_quote_receipt(exit_payload)
            exit_out = exit_receipt.expected_out_atomic
            completed_s = self.now_fn()
            exit_quoted_at = self.utc_now_fn()
            pair_age_ms = max(0, int((completed_s - entry_done_s) * 1_000))
            latency_ms = max(0, int((completed_s - started) * 1_000))
            pair_stale = pair_age_ms > self.max_pair_age_ms
            return RemoteRoundTripProbe(
                not pair_stale,
                not pair_stale,
                "jupiter_quote_v3",
                latency_ms,
                "REMOTE_QUOTE_PAIR_STALE" if pair_stale else None,
                quote_in_atomic=request.quote_in_atomic,
                quote_mint=request.quote_mint,
                tokens_out_atomic=tokens_out,
                immediate_exit_quote_atomic=exit_out,
                entry_min_out_atomic=entry_receipt.minimum_out_atomic,
                immediate_exit_min_quote_atomic=exit_receipt.minimum_out_atomic,
                round_trip_return_bps=(
                    (exit_out - request.quote_in_atomic) * 10_000
                    // request.quote_in_atomic
                ),
                minimum_round_trip_return_bps=(
                    (exit_receipt.minimum_out_atomic - request.quote_in_atomic) * 10_000
                    // request.quote_in_atomic
                ),
                launch_id=request.launch_id,
                venue_id=request.venue_id.value,
                token_id=request.token_id,
                state_fingerprint=request.state_fingerprint,
                queued_at=request.queued_at,
                entry_quoted_at=entry_quoted_at,
                exit_quoted_at=exit_quoted_at,
                completed_at=exit_quoted_at,
                queue_delay_ms=queue_delay_ms,
                pair_age_ms=pair_age_ms,
            )
        except Exception as exc:
            message = str(exc).strip()
            code = message if message.startswith("JUPITER_") else "REMOTE_QUOTE_PROVIDER_ERROR"
            return RemoteRoundTripProbe(
                False,
                False,
                "jupiter_quote_v3",
                max(0, int((self.now_fn() - started) * 1_000)),
                code[:120],
                launch_id=request.launch_id,
                venue_id=request.venue_id.value,
                token_id=request.token_id,
                state_fingerprint=request.state_fingerprint,
                queued_at=request.queued_at,
                completed_at=self.utc_now_fn(),
                queue_delay_ms=queue_delay_ms,
            )

    def _finish(self, request: _Request, result: RemoteRoundTripProbe) -> None:
        with self._lock:
            self._pending.discard(request.key)
            self._completed += 1
            if result.blocker_code == "REMOTE_QUOTE_QUEUE_STALE":
                self._stale_queue_discards += 1
            elif not (result.entry_quote_ok and result.acquired_quantity_sell_ok):
                self._failed += 1
                self._last_failure_at = result.completed_at or self.utc_now_fn()
                self._last_failure = result.blocker_code or "quote_unavailable"
            if result.entry_quote_ok and result.acquired_quantity_sell_ok:
                self._last_success_at = result.completed_at or self.utc_now_fn()
                self._cache[request.key] = (self.now_fn(), result)
                self._cache.move_to_end(request.key)
                while len(self._cache) > self.cache_max:
                    self._cache.popitem(last=False)
                    self._cache_evictions += 1
            self._append_completion_locked(result)

    def _finish_paper(self, request: _Request, result: dict[str, object]) -> None:
        with self._lock:
            self._pending.discard(request.key)
            self._completed += 1
            if result.get("status") != "filled":
                self._failed += 1
                completed_at = self.utc_now_fn()
                self._last_failure_at = completed_at
                self._last_failure = str(
                    result.get("reason") or "quote_unavailable"
                )
            else:
                economics = result.get("economics")
                completed_at = (
                    economics.get("quote_completed_at")
                    if isinstance(economics, dict)
                    else None
                ) or self.utc_now_fn()
                self._last_success_at = completed_at
            if result.get("reason") == "stale_quote":
                self._stale_queue_discards += 1
            self._paper_results[request.launch_id] = dict(result)
            self._paper_results.move_to_end(request.launch_id)
            while len(self._paper_results) > self.cache_max:
                self._paper_results.popitem(last=False)
                self._cache_evictions += 1
            economics = result.get("economics")
            if request.emit_paper_completion:
                if len(self._paper_completions) >= self.completion_max:
                    self._paper_completions.popleft()
                    self._paper_completion_drops += 1
                self._paper_completions.append(PaperQuoteCompletion(
                    observation_id=request.launch_id,
                    venue_id=request.venue_id.value,
                    status=str(result.get("status") or "rejected"),
                    economics=(
                        dict(economics) if isinstance(economics, dict) else None
                    ),
                    failure_reason=(
                        str(result.get("reason") or "quote_unavailable")
                        if result.get("status") != "filled"
                        else None
                    ),
                    requested_at=request.queued_at,
                    completed_at=str(completed_at),
                ))

    def _next_request(self, *, timeout: float | None = None) -> _Request | None:
        """Next request for the single paced worker, or None.

        Capture keeps strict priority, with two exceptions for the paper lane: a paper
        request older than ``paper_starvation_bound_s`` takes the next slot, and an idle
        capture queue services paper immediately. Paper volume is a handful of requests
        per day, so the capture cost is one delayed call per request — while the
        alternative (strict priority everywhere) starved every paper request to its 30s
        ``capacity_expired`` receipt precisely during the busy tape v5 alerts select for.
        """
        head = self._paper_queue.queue[0] if self._paper_queue.queue else None
        if (head is not None
                and self.now_fn() - head.queued_at_s >= self.paper_starvation_bound_s):
            try:
                return self._paper_queue.get_nowait()
            except queue.Empty:
                pass
        try:
            if timeout is None:
                return self._queue.get_nowait()
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            try:
                return self._paper_queue.get_nowait()
            except queue.Empty:
                return None

    def _service(self, request: _Request) -> None:
        """Process + dispatch ONE request — the single body both the worker thread and
        ``drain_once`` execute. ``_run`` used to call ``_finish`` unconditionally; a paper
        result is a dict, so the first serviced paper request raised AttributeError inside
        the worker thread and killed it. Kind dispatch is structural now, not per-caller."""
        result = self._process(request)
        if request.kind == "paper":
            self._finish_paper(request, result)
            self._paper_queue.task_done()
        else:
            self._finish(request, result)
            self._queue.task_done()

    def drain_once(self) -> bool:
        request = self._next_request()
        if request is None:
            return False
        self._service(request)
        return True

    def _run(self) -> None:
        while not self._closed:
            request = self._next_request(timeout=0.2)
            if request is None:
                continue
            self._service(request)

    def drain_completions(self, limit: int = 32) -> tuple[RemoteRoundTripProbe, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            values = []
            while self._completions and len(values) < limit:
                values.append(self._completions.popleft())
            return tuple(values)

    def drain_paper_completions(
        self, limit: int = 32
    ) -> tuple[PaperQuoteCompletion, ...]:
        if limit <= 0:
            return ()
        with self._lock:
            values = []
            while self._paper_completions and len(values) < limit:
                values.append(self._paper_completions.popleft())
            return tuple(values)

    def metrics(self) -> dict[str, object]:
        with self._lock:
            self._prune_cache_locked(self.now_fn())
            return {
                "queued": self._queue.qsize(),
                "paper_queued": self._paper_queue.qsize(),
                "pending": len(self._pending),
                "cached": len(self._cache),
                "completed": self._completed,
                "failed": self._failed,
                "requested": self._requested,
                "last_request_at": self._last_request_at,
                "last_success_at": self._last_success_at,
                "last_failure_at": self._last_failure_at,
                "last_failure": self._last_failure,
                "stale_queue_discards": self._stale_queue_discards,
                "cache_evictions": self._cache_evictions,
                "completion_drops": self._completion_drops,
                "queue_replacements": self._queue_replacements,
                "completions": len(self._completions),
                "paper_completions": len(self._paper_completions),
                "paper_completion_drops": self._paper_completion_drops,
                "min_call_interval_s": self.min_call_interval_s,
            }

    def close(self) -> None:
        self._closed = True
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
