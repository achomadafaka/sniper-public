"""eth_call_raw gained an optional `value_wei` (2026-09-20, for flap.sh's native-value buy
simulation). Shared-gate check (feedback-fix-shared-gate-check-all-lanes): a parameter added to a
function this many callers share must be proven backward compatible for every existing caller,
not just the one lane that needed it.
"""
import ast
import inspect
import json
import re
from pathlib import Path

from sniper.execution.evm_read_rpc import eth_call_raw

ROOT = Path(__file__).resolve().parent.parent

# Every file with a real (non-test, non-definition) call to eth_call_raw, as of the value_wei
# change. A new caller added later that passes value_wei is fine; one that does NOT pass it must
# keep working exactly as before, which is what this file actually proves via _http below.
_CALLER_FILES = (
    "sniper/services/memecoin_evm_price.py",
    "sniper/execution/evm_real_seams.py",
    "sniper/execution/evm_token_safety.py",
    "sniper/execution/evm_launchpad_curve.py",
    "sniper/execution/evm_flap_curve.py",
)


def test_no_pre_existing_caller_passes_value_wei():
    """AST-parsed, not grepped -- a call must be a real `eth_call_raw(...)` invocation with a
    `value_wei` keyword, not a comment or string mentioning the name."""
    offenders = []
    for rel in _CALLER_FILES:
        if rel == "sniper/execution/evm_flap_curve.py":
            continue                                   # the one caller THIS change is for
        path = ROOT / rel
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "eth_call_raw"):
                if any(kw.arg == "value_wei" for kw in node.keywords):
                    offenders.append(rel)
    assert offenders == [], (
        "a pre-existing eth_call_raw caller now passes value_wei -- that caller's own behaviour "
        "must be reviewed, not assumed unaffected: %s" % offenders)


def _record_calls():
    calls = []

    def http(method, url, headers=None, body=None, timeout=10):
        calls.append(json.loads(body))
        return 200, {"result": "0x" + "1" * 64}, None
    return http, calls


def test_omitting_value_wei_keeps_every_existing_caller_byte_identical():
    """The exact contract every one of the 6 existing callers relies on: no `value_wei` argument
    means no `value` key in the call object at all, not `value=0`."""
    http, calls = _record_calls()
    eth_call_raw("0xto", "0xdata", "https://rpc.example", http=http)
    assert "value" not in calls[0]["params"][0]


def test_value_wei_none_is_the_same_as_omitting_it():
    http, calls = _record_calls()
    eth_call_raw("0xto", "0xdata", "https://rpc.example", http=http, value_wei=None)
    assert "value" not in calls[0]["params"][0]


def test_value_wei_zero_is_also_omitted():
    """0 wei attached is indistinguishable from no value at all -- matches how a real EVM
    client treats a missing `value` field (defaults to 0), so this is not a behaviour change."""
    http, calls = _record_calls()
    eth_call_raw("0xto", "0xdata", "https://rpc.example", http=http, value_wei=0)
    assert "value" not in calls[0]["params"][0]


def test_value_wei_is_hex_encoded():
    http, calls = _record_calls()
    eth_call_raw("0xto", "0xdata", "https://rpc.example", http=http, value_wei=10 ** 16)
    assert calls[0]["params"][0]["value"] == hex(10 ** 16)


def test_value_wei_accepts_a_string_amount():
    """Callers may pass a decimal string (as amount_atomic often arrives) -- must not raise."""
    http, calls = _record_calls()
    eth_call_raw("0xto", "0xdata", "https://rpc.example", http=http, value_wei=str(10 ** 16))
    assert calls[0]["params"][0]["value"] == hex(10 ** 16)


def test_signature_keeps_value_wei_keyword_only():
    """Positional-or-keyword would risk a caller accidentally passing it as `raise_on_error` or
    similar by position -- pins that this stays keyword-only, matching every other optional here."""
    params = inspect.signature(eth_call_raw).parameters
    assert params["value_wei"].kind == inspect.Parameter.KEYWORD_ONLY
