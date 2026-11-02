"""Test the get_hl_copy_scorecard MCP tool: registration + impl reuses the endpoint builder."""
import os

from sniper.hyperlive import intel_store as IS
from sniper.mcp_tools import read_tools


class _StubMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def test_tool_is_registered():
    stub = _StubMCP()
    read_tools.register_read_tools(stub)
    assert "get_hl_copy_scorecard" in stub.tools


def test_impl_returns_payload(tmp_path, monkeypatch):
    db = os.path.join(str(tmp_path), "hl_intel.sqlite")
    c = IS.connect(db)
    IS.ensure_schema(c)
    cid = IS.open_copy(c, "0xa", "ETH", "long", 2000.0, ts_ms=1000)
    IS.close_copy(c, cid, exit_mark=2040.0, pnl_bps=191.0, ts_ms=int(3.6e6))
    IS.open_copy(c, "0xa", "BTC", "long", 100.0, ts_ms=2000)
    c.close()
    from sniper.routes import api_hyperlive as APIHL
    from sniper.markets import hyperliquid_api as HL
    monkeypatch.setattr(APIHL, "INTEL_DB", db)
    monkeypatch.setattr(HL, "fetch_marks", lambda: {"BTC": 101.0, "ETH": 2000.0})
    monkeypatch.setattr(HL, "fetch_candles", lambda *a, **k: [])
    out = read_tools.hl_copy_scorecard(holdout=False)
    assert out["available"] is True and out["realized"]["n"] == 1
    assert out["total_book"]["open_n"] == 1
