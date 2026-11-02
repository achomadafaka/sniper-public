"""Strategy protocol — what strategies must implement."""
from __future__ import annotations

from typing import Protocol, Any


class Strategy(Protocol):
    def on_episode_start(self, metadata: dict) -> None: ...
    def act(self, ctx: Any) -> None: ...
    def on_episode_end(self, result: dict) -> None: ...
