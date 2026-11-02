"""Compatibility package for MCP tools now living under sniper.mcp_tools."""

from __future__ import annotations

from pathlib import Path


__path__ = [str(Path(__file__).resolve().parent.parent / "sniper" / "mcp_tools")]

from sniper.mcp_tools import *  # noqa: F401,F403
