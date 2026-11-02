"""Failure taxonomy for wallet protocol batch runs."""

from __future__ import annotations

import requests

FailureCategory = str

ALLOWED_FAILURE_CATEGORIES = (
    "timeout",
    "data_cap_hit",
    "api_error",
    "protocol_bug",
    "gate_skip",
)


def classify(error: BaseException) -> FailureCategory:
    message = str(error).lower()
    if isinstance(error, TimeoutError) or "timed out" in message or "timeout" in message:
        return "timeout"
    if "activity cap" in message or "data cap" in message or "cap hit" in message:
        return "data_cap_hit"
    if isinstance(error, requests.RequestException) or "http" in message or "api" in message:
        return "api_error"
    if "gate_skip" in message or "gate skip" in message:
        return "gate_skip"
    return "protocol_bug"


__all__ = ["ALLOWED_FAILURE_CATEGORIES", "classify"]
