"""Compatibility package for feed modules now living under sniper.feeds."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "feeds")]

from sniper.feeds import *  # noqa: F401,F403
