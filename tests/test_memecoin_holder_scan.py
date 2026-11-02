"""Item 11b — advisory Solana holder-concentration scan. Native RPC (getTokenSupply +
getTokenLargestAccounts). MUST fail-OPEN (return None, never raise) — a fail-closed rug scanner
would silently block every entry and look like a working gate (the 2026-07-14 honeypot lesson)."""
import json

from sniper.execution.solana_holder_scan import (
    concentration_flag,
    holder_concentration,
    mechanism_ok,
)


def _http(supply_ui, largest):
    """Scripted RPC: getTokenSupply -> supply_ui, getTokenLargestAccounts -> largest (list of uiAmount)."""
    def http(method, url, headers=None, body=None, timeout=10):
        m = json.loads(body)["method"]
        if m == "getTokenSupply":
            return 200, {"result": {"value": {"uiAmount": supply_ui}}}, None   # nested under value
        if m == "getTokenLargestAccounts":
            return 200, {"result": {"value": [{"uiAmount": a} for a in largest]}}, None
        return 200, {"result": None}, None
    return http


def test_concentration_math():
    # supply 1000; largest holders 500/200/100/40/... -> top1 50%, top5, top10
    largest = [500, 200, 100, 40, 30, 20, 10, 5, 3, 2, 1]
    snap = holder_concentration("Mint", "rpc", http=_http(1000.0, largest))
    assert snap["top1_pct"] == 50.0
    assert snap["top5_pct"] == round((500 + 200 + 100 + 40 + 30) / 1000 * 100, 2)   # 87.0
    assert snap["top10_pct"] == round(sum(largest[:10]) / 1000 * 100, 2)             # 91.0
    assert snap["largest_n"] == 11


def test_flag_bands():
    assert concentration_flag({"top10_pct": 95.0}) == "high"
    assert concentration_flag({"top10_pct": 85.0}) == "warn"
    assert concentration_flag({"top10_pct": 40.0}) == "ok"
    assert concentration_flag(None) == "unknown"
    assert concentration_flag({"top10_pct": None}) == "unknown"


def test_mechanism_ok_canary():
    def good(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": {"uiAmount": 5.0e9}}}, None       # USDC supply present
    assert mechanism_ok("rpc", http=good) is True

    def down(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("rpc down")
    assert mechanism_ok("rpc", http=down) is False                          # never raises -> False

    def zero(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": {"uiAmount": 0}}}, None
    assert mechanism_ok("rpc", http=zero) is False


def test_fail_open_on_rpc_error():
    def boom(method, url, headers=None, body=None, timeout=10):
        raise RuntimeError("rpc down")
    assert holder_concentration("Mint", "rpc", http=boom) is None       # never raises


def test_fail_open_on_zero_supply():
    assert holder_concentration("Mint", "rpc", http=_http(0.0, [1, 2, 3])) is None


def test_fail_open_on_empty_accounts():
    assert holder_concentration("Mint", "rpc", http=_http(1000.0, [])) is None


def test_amount_decimals_fallback():
    # an account reporting raw amount+decimals (no uiAmount) is still counted
    def http(method, url, headers=None, body=None, timeout=10):
        m = json.loads(body)["method"]
        if m == "getTokenSupply":
            return 200, {"result": {"value": {"uiAmount": 1000.0}}}, None
        return 200, {"result": {"value": [{"amount": "500000000", "decimals": 6}]}}, None  # 500.0
    snap = holder_concentration("Mint", "rpc", http=http)
    assert snap["top1_pct"] == 50.0


def test_retry_once_on_index_overloaded_then_none():
    # -32603 'account index service overloaded' is transient -> retried; if it persists, fail-open
    calls = {"n": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        calls["n"] += 1
        return 200, {"error": {"code": -32603, "message": "account index service overloaded"}}, None
    assert holder_concentration("Mint", "rpc", http=http) is None
    assert calls["n"] >= 2                                # retried at least once


def test_non_overload_error_fails_open_without_retry():
    calls = {"n": 0}

    def http(method, url, headers=None, body=None, timeout=10):
        calls["n"] += 1
        return 200, {"error": {"code": -32602, "message": "Invalid param: WrongSize"}}, None
    assert holder_concentration("Mint", "rpc", http=http) is None
    assert calls["n"] == 1                                # a hard error is NOT retried
