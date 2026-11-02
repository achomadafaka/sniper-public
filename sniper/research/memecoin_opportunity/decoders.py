"""Pure venue-decoder contracts.

Decoders return blockers as data. They never guess through an unknown program,
contract, discriminator, or malformed payload.
"""

from __future__ import annotations

import base64
import struct
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .types import EventKind, LifecycleState


class DecodeBlocker(str, Enum):
    UNKNOWN_VERSION = "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"
    MALFORMED_PAYLOAD = "DECODER_FAILURE"
    MISSING_IDENTITY = "MISSING_TOKEN_OR_CREATOR_IDENTITY"


@dataclass(frozen=True)
class DecodedVenueEvent:
    event_kind: EventKind | None = None
    lifecycle: LifecycleState = LifecycleState.UNRESOLVED
    token_id: str | None = None
    curve_id: str | None = None
    creator_id: str | None = None
    actor_id: str | None = None
    recipient_id: str | None = None
    fee_share_id: str | None = None
    pool_id: str | None = None
    quote_delta_atomic: int | None = None
    token_delta_atomic: int | None = None
    fee_atomic: int | None = None
    virtual_token_reserves: int | None = None
    virtual_quote_reserves: int | None = None
    real_token_reserves: int | None = None
    real_quote_reserves: int | None = None
    progress_bps: int | None = None
    price_atomic: int | None = None
    timestamp_s: int | None = None
    decoder_version: str = ""
    blocker: DecodeBlocker | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)


def blocked(blocker: DecodeBlocker, version: str) -> DecodedVenueEvent:
    return DecodedVenueEvent(blocker=blocker, decoder_version=version)


class BorshReader:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.offset = 0

    def take(self, size: int) -> bytes:
        end = self.offset + size
        if size < 0 or end > len(self.raw):
            raise ValueError("truncated borsh payload")
        value = self.raw[self.offset:end]
        self.offset = end
        return value

    def u8(self) -> int:
        return self.take(1)[0]

    def bool(self) -> bool:
        value = self.u8()
        if value not in (0, 1):
            raise ValueError("invalid borsh bool")
        return bool(value)

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def u128(self) -> int:
        return int.from_bytes(self.take(16), "little")

    def i64(self) -> int:
        return struct.unpack("<q", self.take(8))[0]

    def string(self) -> str:
        return self.take(self.u32()).decode("utf-8")

    def pubkey(self) -> str:
        return base58_encode(self.take(32))


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def base58_encode(raw: bytes) -> str:
    zeros = len(raw) - len(raw.lstrip(b"\0"))
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = _B58[remainder] + encoded
    return "1" * zeros + encoded


def anchor_event_bytes(log_line: str) -> bytes:
    prefix = "Program data: "
    if not isinstance(log_line, str) or not log_line.startswith(prefix):
        raise ValueError("not an Anchor program-data log")
    raw = base64.b64decode(log_line[len(prefix):], validate=True)
    if len(raw) < 8:
        raise ValueError("truncated Anchor event")
    return raw
