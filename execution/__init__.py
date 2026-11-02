"""Compatibility package for execution modules now living under sniper.execution."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "execution")]
