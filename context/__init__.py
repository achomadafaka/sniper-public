"""Compatibility package for context modules now living under sniper.context."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "context")]

from sniper.context import *  # noqa: F401,F403
