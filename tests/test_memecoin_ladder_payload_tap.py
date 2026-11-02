"""The route ladder records WHAT IT SENT, so the payloads can be replayed against other endpoints.

The 4663 ladder never recorded its eth_call payloads, so "publicnode answers a proxy method in
80 ms against the primary's 205 ms, n=20" could not be retested with the real calls — only with a
stand-in method that may not be representative. Each rung now carries a `calls` list on its ledger
record: host asked, JSON-RPC method, exact params (calldata + block tag), start, RTT and an outcome
class.

RECORDING ONLY. The tap is installed per rung, the RPC answer is delegated untouched, an exception
propagates after the record is written, and a recording failure is swallowed. With no tap installed
— every other caller of `_rpc`, including the funded exit sweep and the tickstream — it costs one
`getattr` and records nothing.

PER FUTURE, NEVER A SHARED LIST: the rungs run in a ThreadPoolExecutor, so the tap is thread-local
and no lock is involved. The pool REUSES threads, so starting a tap replaces the list rather than
clearing it; otherwise a rung that never started one would inherit the previous ladder's calls.
"""
import threading

import pytest

from sniper.execution import evm_wallet as w


# ⚠️ THE RECORDED ENDPOINT MUST NOT CARRY A CREDENTIAL (2026-09-24, found by Codex reviewing
# 7f4cbf02). 12 of the 26 configured RPC URLs hold a key in the path or the query, including the
# entry ladder's own pools for 4663, 5042 and 8453, so the first version of this tap would have
# written API keys into `memecoin_entry_attempts.trace_json` -- the funded ledger, backed up
# nightly. Both shapes are pinned because a fix that only handles the path leaves the Helius and
# Shyft URLs, which carry theirs in the query.
@pytest.mark.parametrize("url,expected", [
    ("https://prov.example.com/AbCdEf0123456789SECRET", "https://prov.example.com"),
    ("https://prov.example.com/?api-key=SECRET1234567890", "https://prov.example.com"),
    ("https://prov.example.com/path?api-key=SECRET1234567890#frag", "https://prov.example.com"),
    ("https://user:password@prov.example.com/p", "https://prov.example.com"),
    ("http://prov.example.com:8545", "http://prov.example.com:8545"),
    ("https://[2001:db8::1]:8545/key", "https://[2001:db8::1]:8545"),
    # A value we cannot decompose is exactly the one not to record.
    ("wss://prov.example.com", "unknown"),
    ("not a url", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_the_recorded_endpoint_never_carries_a_credential(monkeypatch, url, expected):
    monkeypatch.setattr(w, "_rpc_impl", lambda u, method, params, http=None: {"result": "0x"})
    w.start_rpc_tap()
    w._rpc(url, "eth_call", [{"to": "0x1"}, "latest"])
    recorded = w.take_rpc_tap()[0]["url_requested"]
    assert recorded == expected
    for secret in ("SECRET1234567890", "AbCdEf0123456789SECRET", "password"):
        assert secret not in str(recorded)


def test_the_real_configured_urls_are_reduced(monkeypatch):
    """The live shapes, not invented ones: every configured RPC URL must reduce to host only."""
    import re
    from dotenv import dotenv_values

    values = dotenv_values("/home/bigpapi/Desktop/Sniper/.env")
    urls = [part.strip() for v in values.values() if v and str(v).startswith("http")
            for part in str(v).split(",") if part.strip().startswith("http")]
    if not urls:
        pytest.skip(".env not readable from this checkout")
    for url in urls:
        reduced = w._tap_endpoint(url)
        assert reduced == "unknown" or re.fullmatch(r"https?://[^/?#]+", reduced), reduced
        tail = url.split("://", 1)[1]
        for chunk in re.findall(r"[/=]([A-Za-z0-9_\-]{16,})", tail):
            assert chunk not in reduced, "a credential survived the reduction"


def test_a_call_is_recorded_with_host_method_params_and_rtt(monkeypatch):
    monkeypatch.setattr(w, "_rpc_impl", lambda url, method, params, http=None: {"result": "0x1"})
    calls = w.start_rpc_tap()
    assert w._rpc("https://rpc.primary", "eth_call",
                  [{"to": "0xpool", "data": "0xdeadbeef"}, "latest"]) == {"result": "0x1"}
    got = w.take_rpc_tap()
    assert got is calls and len(got) == 1
    one = got[0]
    assert one["url_requested"] == "https://rpc.primary"      # already host-only
    assert one["method"] == "eth_call"
    assert one["params"] == [{"to": "0xpool", "data": "0xdeadbeef"}, "latest"]
    assert one["rtt_ms"] >= 0.0 and one["started_at"] > 0
    assert one["outcome"] == "ok"


def test_no_tap_means_no_recording_and_no_change(monkeypatch):
    """Every other caller of `_rpc` is this path: the exit sweep, balances, the tickstream."""
    seen = []
    monkeypatch.setattr(w, "_rpc_impl",
                        lambda url, method, params, http=None: seen.append(method) or {"result": 1})
    w.take_rpc_tap()                       # ensure nothing is installed on this thread
    assert w._rpc("https://rpc", "eth_getBalance", []) == {"result": 1}
    assert seen == ["eth_getBalance"]
    assert w.take_rpc_tap() is None


def test_a_raising_call_is_still_recorded_with_its_class(monkeypatch):
    """A rung that failed still made the calls that failed, and those are the ones worth replaying."""
    def boom(url, method, params, http=None):
        raise TimeoutError("slow")

    monkeypatch.setattr(w, "_rpc_impl", boom)
    w.start_rpc_tap()
    with pytest.raises(TimeoutError):
        w._rpc("https://rpc", "eth_call", [{"to": "0x1"}, "latest"])
    got = w.take_rpc_tap()
    assert len(got) == 1 and got[0]["outcome"] == "timeout"
    assert got[0]["rtt_ms"] >= 0.0, "a failure still took time and the time is the measurement"


def test_a_json_rpc_error_body_is_its_own_outcome_not_a_success(monkeypatch):
    monkeypatch.setattr(w, "_rpc_impl", lambda url, method, params, http=None: {
        "error": {"code": -32701, "message": "Please specify an address in your request"}})
    w.start_rpc_tap()
    w._rpc("https://rpc", "eth_getLogs", [{}])
    got = w.take_rpc_tap()
    assert got[0]["outcome"] == "rpc_error:-32701"


def test_two_threads_do_not_see_each_other_s_calls(monkeypatch):
    """The ladder runs its rungs in a ThreadPoolExecutor; a shared list would need a lock."""
    monkeypatch.setattr(w, "_rpc_impl", lambda url, method, params, http=None: {"result": "0x"})
    out = {}

    def rung(name, n):
        w.start_rpc_tap()
        for i in range(n):
            w._rpc("https://rpc", "eth_call", [{"to": name, "data": "0x%d" % i}, "latest"])
        out[name] = w.take_rpc_tap()

    threads = [threading.Thread(target=rung, args=(n, k)) for n, k in (("a", 3), ("b", 5))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out["a"]) == 3 and len(out["b"]) == 5
    assert all(c["params"][0]["to"] == "a" for c in out["a"])
    assert all(c["params"][0]["to"] == "b" for c in out["b"])


def test_a_reused_thread_does_not_inherit_the_previous_ladder_s_calls(monkeypatch):
    """ThreadPoolExecutor reuses threads, so starting a tap REPLACES rather than clears."""
    monkeypatch.setattr(w, "_rpc_impl", lambda url, method, params, http=None: {"result": "0x"})
    w.start_rpc_tap()
    w._rpc("https://rpc", "eth_call", [{"to": "0xold"}, "latest"])
    w.take_rpc_tap()
    w.start_rpc_tap()                      # the same thread, a second ladder
    w._rpc("https://rpc", "eth_call", [{"to": "0xnew"}, "latest"])
    got = w.take_rpc_tap()
    assert len(got) == 1 and got[0]["params"][0]["to"] == "0xnew"

    # AND WITHOUT AN INTERVENING TAKE, which is the case that actually distinguishes replacing
    # from clearing: a rung that raised before taking its tap leaves one installed, and the next
    # rung on that pooled thread must not inherit it. Asserting only the take/start order proved
    # nothing here — a version that reused the existing list passed it.
    w.start_rpc_tap()
    w._rpc("https://rpc", "eth_call", [{"to": "0xfirst"}, "latest"])
    second = w.start_rpc_tap()             # no take between the two
    assert second == [], "the new tap inherited the abandoned one"
    w._rpc("https://rpc", "eth_call", [{"to": "0xsecond"}, "latest"])
    assert [c["params"][0]["to"] for c in w.take_rpc_tap()] == ["0xsecond"]


def _ladder_with(rungs, v3_fn, chain="4663"):
    """The REAL ladder, not a restatement of it.

    `_ladder` in the sibling file closes over its `v3` as the quote VALUE, so a function passed
    there is returned as if it were the quote and never runs — which is exactly what happened on
    the first attempt at this test, and it PASSED the ladder while proving nothing.
    """
    from sniper.execution.evm_real_seams import venue_quote_with_v2_fallback
    from test_memecoin_evm_quote_miss_attribution import WETH

    return venue_quote_with_v2_fallback(
        v3_fn, chain=chain, quote_token=WETH, taker="0xtaker",
        acct=object(), rpc_url="https://rpc.4663", on_rung=rungs.append)


def test_the_ladder_puts_the_calls_on_the_rung_record(monkeypatch):
    """End to end through the real ladder."""
    from test_memecoin_evm_quote_miss_attribution import QUOTE, WETH, MINT
    from sniper.execution import evm_real_seams as seams

    monkeypatch.setattr(w, "_rpc_impl",
                        lambda url, method, params, http=None: {"result": "0x" + "0" * 64})
    rungs = []

    def v3_fn(sell, buy, amount, slippage_bps=None, **kw):
        # A rung that READS, which is the whole point of the tap.
        seams._rpc("https://rpc.4663", "eth_call", [{"to": "0xpool", "data": "0xfeed"}, "latest"])
        return QUOTE

    _ladder_with(rungs, v3_fn)(WETH, MINT, 10 ** 18)
    carried = [r for r in rungs if r.get("calls")]
    assert carried, "no rung carried its calls: %s" % [r.get("rung") for r in rungs]
    assert carried[0]["rung"] == "v3"
    call = carried[0]["calls"][0]
    assert call["method"] == "eth_call"
    assert call["params"] == [{"to": "0xpool", "data": "0xfeed"}, "latest"]
    assert call["rtt_ms"] >= 0.0 and call["url_requested"] == "https://rpc.4663"


def test_a_rung_that_sent_nothing_carries_no_calls_key(monkeypatch):
    """Absent, not empty: "asked nothing" and "asked and got nothing" are different rows.

    A SKIPPED rung is the case — it never ran, so it has nothing to record. My first version of
    this asserted no rung anywhere carried calls, which was wrong about the ladder rather than
    about the code: with a stub v3 the other rungs still issue their own real reads.
    """
    from test_memecoin_evm_quote_miss_attribution import QUOTE, WETH, MINT

    monkeypatch.setattr(w, "_rpc_impl", lambda url, method, params, http=None: {"result": "0x"})
    rungs = []
    _ladder_with(rungs, lambda *a, **k: QUOTE)(WETH, MINT, 10 ** 18)
    skipped = [r for r in rungs if r.get("skipped")]
    assert skipped, "no rung was skipped in this ladder: %s" % [r["rung"] for r in rungs]
    assert all("calls" not in r for r in skipped)
