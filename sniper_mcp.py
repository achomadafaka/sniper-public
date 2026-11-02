#!/usr/bin/env python3
"""Sniper MCP Server — thin adapter over existing Sniper codebase.

Run: python3 sniper_mcp.py           (stdio, default)
Config: add to .claude/settings.local.json mcpServers
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Sniper Trading Intelligence", json_response=True)

from sniper.mcp_tools.read_tools import register_read_tools
from sniper.mcp_tools.analyze_tools import register_analyze_tools
from sniper.mcp_tools.write_tools import register_write_tools
from sniper.mcp_tools.replay_tools import register_replay_tools

register_read_tools(mcp)
register_analyze_tools(mcp)
register_write_tools(mcp)
register_replay_tools(mcp)

if __name__ == "__main__":
    mcp.run()  # stdio transport (default)
