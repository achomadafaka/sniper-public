"""The funded Jupiter key: sent as a header, spent only by the funded path, and never logged.

WHY THERE IS A KEY. Measured 2026-08-27 from the heartbeat sampler: 510 of 1,933 funded Jupiter
quotes were rate-limited — 26.4% — and all of them fell inside one 36-minute window where funded
quoted 1.4-1.6/s and took ~1.0 429/s. Keyless capacity does not cover the funded exit path alone.

THE THREE THINGS THAT MUST HOLD.
  1. Unset key -> byte-identical to the keyless behaviour, including host order.
  2. The key travels in a HEADER, never a query parameter: a key in the URL reaches logs, exception
     text and any persisted request line.
  3. A measurement lane never spends the funded key. That budget belongs to the exit brain, and a
     research process on a funded quota is the 2026-08-21 failure repeated.
"""
import pathlib
import re

import pytest

from _source_guard import code_of
from sniper.execution import solana_real_seams as S

QUOTE = {"outAmount": "12345", "inAmount": "1000", "routePlan": [{"swapInfo": {}}]}
FAKE_KEY = "test-key-not-a-real-one"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    S._jup_host_cooldown.clear()
    S.JUPITER_LOAD["by_host"] = {}
    monkeypatch.setattr(S, "_KEY_VAR", S.JUP_API_KEY_VAR)
    monkeypatch.delenv(S.JUP_API_KEY_VAR, raising=False)
    monkeypatch.delenv(S.CAPTURE_JUP_API_KEY_VAR, raising=False)
    yield
    S._jup_host_cooldown.clear()


def _http(responses, calls, *, headers_ok=True):
    """A transport seam that records url AND headers. headers_ok=False mimics an older fake."""
    if headers_ok:
        def http(method, url, headers=None, body=None):
            calls.append((url, dict(headers or {})))
            for base, resp in responses.items():
                if url.startswith(base):
                    return resp[0], resp[1], b""
            raise AssertionError("unexpected url " + url)
    else:
        def http(method, url, body=None):
            calls.append((url, None))
            for base, resp in responses.items():
                if url.startswith(base):
                    return resp[0], resp[1], b""
            raise AssertionError("unexpected url " + url)
    return http


# ---------------------------------------------------------------- unset == today

def test_without_a_key_no_header_is_sent_and_the_host_order_is_unchanged():
    calls = []
    http = _http({S.JUP_BASE: (200, QUOTE)}, calls)
    assert S._jup_quote("A", "B", 1000, 300, http) == QUOTE
    url, headers = calls[0]
    assert url.startswith(S.JUP_BASE), "keyless must still try lite-api first"
    assert "x-api-key" not in {k.lower() for k in headers}


def test_an_old_transport_seam_without_headers_still_works(monkeypatch):
    """Injected fakes predate headers. They must not break, keyed or not."""
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls, headers_ok=False)
    assert S._jup_quote("A", "B", 1000, 300, http) == QUOTE


# ---------------------------------------------------------------- keyed behaviour

def test_the_key_goes_in_a_header_and_the_keyed_host_leads(monkeypatch):
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls)
    assert S._jup_quote("A", "B", 1000, 300, http) == QUOTE
    url, headers = calls[0]
    assert url.startswith(S.JUP_FALLBACK_BASE), "the key is only honoured on the keyed host"
    assert headers.get("x-api-key") == FAKE_KEY


def test_the_key_never_appears_in_any_url(monkeypatch):
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (200, QUOTE), S.JUP_BASE: (200, QUOTE)}, calls)
    S._jup_quote("A", "B", 1000, 300, http)
    for url, _h in calls:
        assert FAKE_KEY not in url, "a key in the URL reaches logs and persisted request lines"


def test_the_swap_build_post_carries_the_key_too(monkeypatch):
    """Both legs, or the quota split does nothing: a quote and a swap build are separate calls."""
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (200, {"swapTransaction": "b64"})}, calls)
    status, _j = S._jup_call("POST", "/swap", body="{}", http=http)
    assert status == 200
    _url, headers = calls[0]
    assert headers.get("x-api-key") == FAKE_KEY


def test_a_bad_key_degrades_to_keyless_and_never_worse(monkeypatch):
    """An expired key 429s the keyed host; lite-api must still answer. Failover is unchanged."""
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (429, {}), S.JUP_BASE: (200, QUOTE)}, calls)
    assert S._jup_quote("A", "B", 1000, 300, http) == QUOTE
    assert calls[0][0].startswith(S.JUP_FALLBACK_BASE)
    assert calls[1][0].startswith(S.JUP_BASE)


def test_a_non_429_still_answers_the_request_and_does_not_failover(monkeypatch):
    """The failover contract is 429-only; a 400 is about the REQUEST, not the host."""
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: (400, {"error": "unindexed"})}, calls)
    status, _j = S._jup_call("GET", "/quote?x=1", http=http)
    assert status == 400 and len(calls) == 1, "a bad mint must not spend the other host's budget"


# ---------------------------------------------------------------- host attribution

def test_a_429_is_attributed_to_the_host_that_answered_it():
    """The 26.4% measured on 2026-08-27 could not be attributed to a host, so 'did the key help'
    was unanswerable. by_host closes that."""
    calls = []
    http = _http({S.JUP_BASE: (429, {}), S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls)
    S._jup_quote("A", "B", 1000, 300, http)
    assert S.JUPITER_LOAD["by_host"].get("lite-api") == 1
    assert "api" not in S.JUPITER_LOAD["by_host"]


def test_the_snapshot_copies_by_host_instead_of_aliasing_it():
    S.JUPITER_LOAD["by_host"]["lite-api"] = 3
    snap = S.jupiter_load_snapshot()
    S.JUPITER_LOAD["by_host"]["lite-api"] = 99
    assert snap["by_host"]["lite-api"] == 3, "a reader must not see the counter move mid-read"


# ---------------------------------------------------------------- budget separation

def test_a_measurement_process_spends_the_measurement_key(monkeypatch):
    monkeypatch.setenv(S.JUP_API_KEY_VAR, "funded-key")
    monkeypatch.setenv(S.CAPTURE_JUP_API_KEY_VAR, "capture-key")
    assert S._jup_api_key() == "funded-key"
    S.declare_measurement_process()
    assert S._jup_api_key() == "capture-key", "research must never spend the exit brain's budget"


def test_the_capture_loop_declares_itself_before_it_can_quote():
    body = pathlib.Path("scripts/memecoin_refusal_capture_loop.py").read_text()
    found = DECLARES.search(body)
    assert found
    # The CALL, not the first textual mention — the name also appears in the module docstring,
    # and comparing against that compared a comment to code.
    assert found.start() < body.index("cap.assert_pool_disjoint("), \
        "declare the budget before anything can issue a quote"


MEASUREMENT_MODULES = (
    "sniper/services/memecoin_refusal_capture.py",
    "sniper/services/memecoin_quotes.py",
    "sniper/services/memecoin_signal_copy_quotes.py",
    "scripts/memecoin_refusal_capture_loop.py",
    "scripts/memecoin_copy_quote_worker.py",
    "scripts/memecoin_scalp_probe.py",
)

# The declaration, however it is spelled. Matching the literal `declare_measurement_process()`
# missed every caller that names its budget — the probe declares
# `declare_measurement_process(PAPER_JUP_API_KEY_VAR)`, which is MORE specific than the bare call
# and would have read as undeclared. A guard that only recognises one spelling of compliance
# reports the most careful caller as the violation.
DECLARES = re.compile(r"declare_measurement_process\s*\(")


def test_no_measurement_module_reads_the_funded_key():
    """The funded key is the exit brain's budget. A measurement lane names its own var or none.

    Scans CODE ONLY, per the AGENTS.md source-guard rule: a docstring that names the funded
    var to FORBID it (memecoin_quotes._paper_jupiter_key does exactly that) must not
    read as a violation — the guard blocked a clean deploy on its own rule's prose 2026-08-28.
    The word-boundary guard keeps MEMECOIN_PAPER_JUPITER_API_KEY (a different var whose name
    embeds nothing of the funded one) from ever mattering here if the names later converge."""
    offenders = []
    for rel in MEASUREMENT_MODULES:
        p = pathlib.Path(rel)
        if not p.exists():
            continue
        for i, line in enumerate(code_of(p).splitlines(), 1):
            if re.search(r"(?<![A-Z_])MEMECOIN_JUPITER_API_KEY", line):
                offenders.append(f"{rel} (code stmt {i})")
    assert not offenders, ("measurement code reading the funded Jupiter key: "
                           + ", ".join(offenders))


def test_the_runner_never_declares_itself_a_measurement_process():
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "declare_measurement_process" not in body, \
        "the funded runner declaring the measurement budget would silently unfund its own quotes"


# The funded quote entry points. A module that names one of these can spend a Jupiter budget.
QUOTE_FNS = ("jupiter_entry_quote", "jupiter_exit_quote", "_typed_jupiter_exit_quote", "_jup_call")


def test_the_declaration_guard_rejects_a_module_that_never_declares(tmp_path):
    """NEGATIVE CONTROL for the guard itself. A regex that matched nothing would let the whole
    list pass silently, and a guard that cannot fail is not a guard — the same defect this file
    already corrected once, when a source scan was fooled by a comment.
    """
    bad = tmp_path / "undeclared.py"
    bad.write_text("if __name__ == '__main__':\n    jupiter_entry_quote('m', 100.0)\n")
    assert not DECLARES.search(code_of(bad))
    good = tmp_path / "declared.py"
    good.write_text("if __name__ == '__main__':\n"
                    "    declare_measurement_process(PAPER_JUP_API_KEY_VAR)\n"
                    "    jupiter_entry_quote('m', 100.0)\n")
    assert DECLARES.search(code_of(good))


def test_every_measurement_module_that_can_quote_declares_its_budget():
    """The grep-for-the-funded-var-name check cannot catch a DEFAULTED role, and that is the hole
    it left. `memecoin_copy_quote_worker` named no key at all and imported the funded quote
    functions, so with MEMECOIN_COPY_QUOTE_ENABLED=1 it spent the funded key by omission — found in
    review 2026-08-27, on an existing module rather than a hypothetical future one.

    Naming no key is the DEFAULT, and the default is funded. So the test is not "does it avoid the
    funded var" but "does it say which budget it spends".
    """
    missing = []
    for rel in MEASUREMENT_MODULES:
        p = pathlib.Path(rel)
        if not p.exists():
            continue
        code = code_of(p)
        # A ROLE BELONGS TO A PROCESS, so only an entry point can declare one. A library cannot —
        # it does not own the interpreter it is imported into, and requiring a declare there would
        # force every importer to inherit a role it may not want. Libraries are covered by the
        # entry points that run them, which this list also carries.
        if "__main__" not in code:
            continue
        if any(fn in code for fn in QUOTE_FNS) and not DECLARES.search(code):
            missing.append(rel)
    assert not missing, (
        "measurement modules that can issue a Jupiter quote but never declare their budget, so "
        "they spend the funded key by default: " + ", ".join(missing))
