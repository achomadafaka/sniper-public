"""Compatibility package for simulation modules now living under sniper.sim."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "sim")]

from sniper.sim import *  # noqa: F401,F403
