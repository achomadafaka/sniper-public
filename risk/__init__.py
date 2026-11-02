"""Compatibility package for risk modules now living under sniper.risk."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "risk")]

from sniper.risk import *  # noqa: F401,F403
