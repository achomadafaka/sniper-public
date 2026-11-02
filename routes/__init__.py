"""Compatibility package for route modules now living under sniper.routes."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "routes")]
