"""Compatibility package for strategy modules now living under sniper.strategies."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "strategies")]

from sniper.strategies import *  # noqa: F401,F403
