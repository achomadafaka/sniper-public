"""Compatibility package for replay modules now living under sniper.replay."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "replay")]

from sniper.replay import *  # noqa: F401,F403
