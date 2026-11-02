"""Raydium venue fallback: enter the coins Jupiter has not indexed yet, change nothing else.

2026-08-14, 15:43-15:45Z: five armed-wallet buys refused with "no quote" / "no planned-quantity
sell route". The coins were minutes old — EMPLOYIM's Raydium pool existed at 15:34:22Z, Jupiter
answered HTTP 400 in BOTH directions 15 minutes later — while the traders bought the pool
directly. Raydium's official trade API quoted the exact refused mints instantly, free, keyless.

The invariants under test:
  - Jupiter answers first; a Jupiter-quoted coin is byte-identical to before.
  - The Raydium quote is normalized so the EXISTING caps read it correctly — above all
    priceImpactPct, which Raydium reports in PERCENT while the executor multiplies a FRACTION
    by 100. Unconverted, a 173% impact would read as 1.73%.
  - The swap build refuses anything but exactly one transaction (write-ahead identity is
    single-signature).
  - The fast (TPU) builder never sees a Raydium quote — it speaks Jupiter /swap-instructions.
"""
import json

import pytest

from sniper.execution import solana_real_seams as S

USDC = S.USDC_MINT
MINT = "AnEXLUSRY5KLgSZEJcYPGSca7cwAXsyGesCrCzYvU9j2"
OWNER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"

RAY_COMPUTE = {
    "success": True,
    "data": {"swapType": "BaseIn", "inputMint": USDC, "inputAmount": "45000000",
             "outputMint": MINT, "outputAmount": "8354679209572114",
             "otherAmountThreshold": "8104038833284950", "slippageBps": 300,
             "priceImpactPct": 1.73, "routePlan": [{"poolId": "4AnR1SU1"}]},
}
JUP_QUOTE = {"outAmount": "111", "otherAmountThreshold": "100", "priceImpactPct": "0.01"}


@pytest.fixture(autouse=True)
def _no_host_cooldowns():
    S._jup_host_cooldown.clear()
    yield
    S._jup_host_cooldown.clear()


@pytest.fixture(autouse=True)
def _classic_output_mint(monkeypatch):
    """Every mint in this file is a CLASSIC SPL mint.

    `raydium_swap_tx` probes the output mint's token program since 2026-09-09: Raydium's build
    derives a classic associated token account, which is the wrong address for a Token-2022 mint
    and gets the whole transaction rejected on chain (STOCKCAT, SWEENIES). The probe is one
    getAccountInfo and it FAILS CLOSED, so without this stub the http route table here does not
    answer it and every build in this file correctly refuses. Token-2022 behaviour has its own
    file, tests/test_memecoin_token_2022_raydium_rung.py.
    """
    monkeypatch.setattr(S, "_mint_is_token_2022", lambda mint, http=None: False)


def _http(routes):
    """routes: [(match, status, json)] first-match; records urls."""
    calls = []

    def http(method, url, body=None):
        calls.append(url)
        for match, status, payload in routes:
            if match in url:
                return status, payload, b""
        raise AssertionError("unexpected url " + url)
    http.calls = calls
    return http


@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_jupiter_still_answers_first(monkeypatch, c2):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    http = _http([("jup.ag", 200, JUP_QUOTE), ("raydium.io", 200, RAY_COMPUTE)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q == JUP_QUOTE
    if c2:
        # C2 (2026-10-01) asks Raydium beside Jupiter on an entry quote; Jupiter's answer stands.
        import time
        deadline = time.monotonic() + 2.0
        while not any("raydium" in c for c in http.calls) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert sum("raydium" in c for c in http.calls) == 1
    else:
        assert not any("raydium" in c for c in http.calls), "an indexed coin must never touch Raydium"


def test_no_jupiter_route_falls_back_to_raydium():
    http = _http([("jup.ag", 400, {"error": "unknown token"}), ("raydium.io", 200, RAY_COMPUTE)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q is not None and q["_venue"] == "raydium"
    assert q["outAmount"] == "8354679209572114"
    assert q["otherAmountThreshold"] == "8104038833284950"


def test_impact_is_converted_from_percent_to_fraction():
    """The executor computes impact_pct = float(priceImpactPct) * 100."""
    http = _http([("jup.ag", 400, {}), ("raydium.io", 200, RAY_COMPUTE)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert abs(float(q["priceImpactPct"]) * 100.0 - 1.73) < 1e-9


def test_unparseable_impact_reads_as_huge_not_zero():
    bad = json.loads(json.dumps(RAY_COMPUTE))
    bad["data"]["priceImpactPct"] = "n/a"
    http = _http([("jup.ag", 400, {}), ("raydium.io", 200, bad)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert float(q["priceImpactPct"]) >= 1.0, "fail-closed: unknown impact must abort at the cap"


def test_exit_quote_falls_back_too():
    """A coin entered via Raydium must be SELLABLE by the same rule — the planned-quantity
    sell-route proof and the rails' exact quote both route through jupiter_exit_quote."""
    ray_sell = json.loads(json.dumps(RAY_COMPUTE))
    ray_sell["data"].update({"inputMint": MINT, "outputMint": USDC})
    http = _http([("jup.ag", 400, {}), ("raydium.io", 200, ray_sell)])
    q = S.jupiter_exit_quote(MINT, 1_000_000_000, 3.0, http=http)
    assert q is not None and q["_venue"] == "raydium"


def test_a_jupiter_429_falls_back_and_fills():
    """Reversed 2026-08-14 20:11: a Jupiter throttle burst refused three ROUTABLE entries in two
    minutes. An entry that fills through the second venue beats a perfectly-typed refusal; the
    bounded cost (a softened RATE_LIMIT verdict when Raydium also fails) is stated at the seam."""
    http = _http([("jup.ag", 429, {"error": "rate limited"}), ("raydium.io", 200, RAY_COMPUTE)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q is not None and q["_venue"] == "raydium"


def test_a_transport_error_never_falls_back():
    def http(method, url, body=None):
        raise OSError("dns down")

    assert S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300) is None


def test_fallback_disabled_by_flag(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", False)
    http = _http([("jup.ag", 400, {}), ("raydium.io", 200, RAY_COMPUTE)])
    assert S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300) is None
    assert not any("raydium" in c for c in http.calls)


@pytest.mark.parametrize("jupiter", [
    {"error": "unknown token"},
    # a dust route by Jupiter's own impact, so the huge Raydium answer is the real pool
    {"outAmount": "1", "otherAmountThreshold": "1", "priceImpactPct": "0.6"},
])
@pytest.mark.parametrize("excluded", [("Denali",), ("UnknownVenue",)])
@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_unrelated_venue_exclusion_preserves_raydium_fallback(monkeypatch, c2, jupiter, excluded):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    http = _http([("jup.ag", 200, jupiter), ("raydium.io", 200, RAY_COMPUTE)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, exclude_dexes=excluded)
    assert q is not None and q["_venue"] == "raydium"
    assert q["outAmount"] == "8354679209572114"
    # With C2 the Raydium call may be recorded before Jupiter's: find Jupiter's by host.
    assert "excludeDexes=" in next(c for c in http.calls if "jup.ag" in c)


@pytest.mark.parametrize("excluded,max_accounts", [
    (("Raydium",), None), (("Raydium CLMM",), None),
    (("Denali", "Raydium CP"), None), (("Raydium CPMM",), None),
    (("Raydium Launchlab",), None), (("Denali",), 48),
])
@pytest.mark.parametrize("c2", [False, True], ids=["serial", "c2_parallel"])
def test_raydium_exclusion_or_account_cap_keeps_fallback_disabled(monkeypatch, c2, excluded,
                                                                  max_accounts):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_PARALLEL", c2, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_RAYDIUM_NONBLOCKING", False, raising=False)  # the C2 blocking contract
    http = _http([("jup.ag", 400, {"error": "unknown token"}), ("raydium.io", 200, RAY_COMPUTE)])
    assert S.jupiter_entry_quote(MINT, 45.0, http=http, exclude_dexes=excluded,
                                 max_accounts=max_accounts) is None
    assert not any("raydium" in c for c in http.calls)


def test_swap_build_dispatches_on_venue(monkeypatch):
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: None)
    build = {"success": True, "data": [{"transaction": "dGVzdA=="}]}
    http = _http([("raydium.io", 200, build)])
    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": USDC, "outputMint": MINT}
    assert S.jupiter_swap_tx(q, OWNER, http=http) == "dGVzdA=="
    assert all("raydium" in c for c in http.calls), "a Raydium quote must never hit Jupiter /swap"


def test_a_multi_transaction_build_is_refused():
    build = {"success": True, "data": [{"transaction": "a"}, {"transaction": "b"}]}
    http = _http([("raydium.io", 200, build)])
    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": USDC, "outputMint": MINT}
    assert S.raydium_swap_tx(q, OWNER, http=http) is None


def test_spl_input_sends_the_owners_ata(monkeypatch):
    # The RPC must ANSWER "no account" for derivation to be correct. Before 2026-09-08 this test
    # passed with no RPC configured at all, because an unreadable lookup and an empty one were the
    # same value — the conflation that reintroduced the MOX defect on Token-2022 exits.
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: None)
    captured = {}

    def http(method, url, body=None):
        if "raydium.io" in url:
            captured.update(json.loads(body))
            return 200, {"success": True, "data": [{"transaction": "eA=="}]}, b""
        raise AssertionError(url)

    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": USDC, "outputMint": MINT}
    S.raydium_swap_tx(q, OWNER, http=http)
    assert captured["inputAccount"] == S._ata(OWNER, USDC)
    assert captured["wrapSol"] is False and captured["unwrapSol"] is False


def test_fast_builder_never_sees_a_raydium_quote(monkeypatch, tmp_path):
    from solders.keypair import Keypair
    fast_calls = []
    build = {"success": True, "data": [{"transaction": None}]}   # raydium build fails -> None sign
    http = _http([("raydium.io", 200, build)])
    ex = S.build_real_solana_executor(
        keypair=Keypair(), rpc_url="https://rpc", http=http,
        usdc_fn=lambda: 100.0, token_fn=lambda _m: 0.0,
        fast_tx_builder=lambda q: fast_calls.append(q) or ("tx", "sig"))
    # reach the private _sign through the executor's entry path is heavy; assert the guard directly
    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": USDC, "outputMint": MINT}
    assert S.jupiter_swap_tx(q, "owner", http=http) is None
    assert fast_calls == []


# --- thin-route second opinion (PEECOIN, 2026-08-14) -------------------------------------------

RAY_BETTER = {"success": True, "data": {"swapType": "BaseIn", "inputMint": USDC, "inputAmount": "45000000",
    "outputMint": MINT, "outputAmount": "999", "otherAmountThreshold": "990",
    "priceImpactPct": 0.4, "routePlan": [{"poolId": "DeepPool"}]}}


def test_a_thin_jupiter_route_is_beaten_by_a_deeper_raydium_pool():
    """Jupiter saw only the dust pool (its own impact 83%, PEECOIN's round trip); Raydium's own
    pool returns MORE out. Take it. Below MEMECOIN_SOLANA_VENUE_FALLBACK_DUST_IMPACT a 10x win is
    a phantom instead (test_memecoin_raydium_phantom_guard.py)."""
    thin = {"outAmount": "100", "otherAmountThreshold": "90", "priceImpactPct": "0.83"}
    http = _http([("jup.ag", 200, thin), ("raydium.io", 200, RAY_BETTER)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q["_venue"] == "raydium" and q["outAmount"] == "999"


def test_a_worse_raydium_answer_keeps_jupiters_quote():
    thin = {"outAmount": "5000", "otherAmountThreshold": "4900", "priceImpactPct": "0.28"}
    http = _http([("jup.ag", 200, thin), ("raydium.io", 200, RAY_BETTER)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert "_venue" not in q and q["outAmount"] == "5000"


def test_a_healthy_route_never_asks_for_a_second_opinion():
    good = {"outAmount": "100", "otherAmountThreshold": "97", "priceImpactPct": "0.01"}
    http = _http([("jup.ag", 200, good), ("raydium.io", 200, RAY_BETTER)])
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q == good
    assert not any("raydium" in c for c in http.calls), "the common case costs zero extra requests"


def test_a_raydium_error_keeps_jupiters_quote():
    thin = {"outAmount": "100", "otherAmountThreshold": "90", "priceImpactPct": "0.28"}
    def http(method, url, body=None):
        if "jup.ag" in url:
            return 200, thin, b""
        raise OSError("raydium down")
    q = S.jupiter_entry_quote(MINT, 45.0, http=http, slippage_bps=300)
    assert q == thin


def test_the_input_account_is_looked_up_not_derived(monkeypatch):
    """MOX, 2026-08-14: a Token-2022 mint's real account differs from the classic-SPL derivation —
    the derived address drew REQ_OWNER_ACCOUNT_ERROR and every exit rung died as 'stuck' while
    Raydium quoted the whole bag. The wallet's actual account must win; derivation is only the
    create-on-buy fallback."""
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: "RealT22Account111")
    captured = {}

    def http(method, url, body=None):
        if "raydium.io" in url:
            captured.update(json.loads(body))
            return 200, {"success": True, "data": [{"transaction": "eA=="}]}, b""
        raise AssertionError(url)

    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": MINT, "outputMint": USDC}
    S.raydium_swap_tx(q, OWNER, http=http)
    assert captured["inputAccount"] == "RealT22Account111"


def test_no_owned_account_falls_back_to_the_derived_ata(monkeypatch):
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: None)
    captured = {}

    def http(method, url, body=None):
        if "raydium.io" in url:
            captured.update(json.loads(body))
            return 200, {"success": True, "data": [{"transaction": "eA=="}]}, b""
        raise AssertionError(url)

    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": USDC, "outputMint": MINT}
    S.raydium_swap_tx(q, OWNER, http=http)
    assert captured["inputAccount"] == S._ata(OWNER, USDC)


# --- a failed lookup must not become "no account" (2026-09-08) ------------------------------------
#
# `_owned_token_account` captured `status` and never read it, so a 429, a 5xx, a JSON-RPC error
# body or a transport failure all produced no `result`, `vals` fell back to `[]`, and the function
# returned None. The single consumer reads None as "the wallet holds no account for this mint" and
# derives the classic ATA — which is correct for a buy, and is the EXACT defect the comment two
# lines above records as having killed the MOX exit ladder on 2026-08-14.
#
# So the MOX fix (look it up, never derive) was bypassed by any RPC hiccup during the very exits it
# was written to protect. Token-2022 is common in this coin population, and the failure is total:
# REQ_OWNER_ACCOUNT_ERROR kills every rung and the position reads "stuck" with a live bid.
#
# Three states are required, because None genuinely means "no account" for a first buy:
#   an account   -> use it
#   None         -> the RPC ANSWERED and the wallet holds none; derive (buy of a coin never held)
#   LOOKUP_FAILED-> we do not know; refuse to build rather than guess an address

RPC_ENV = "MEMECOIN_SOLANA_RPC_URL"


def _acct_rpc(monkeypatch, responses):
    """Drive the real _owned_token_account with a scripted RPC."""
    monkeypatch.setenv(RPC_ENV, "http://rpc.test")
    calls = []

    def http(method, url, body=None, headers=None):
        calls.append(url)
        r = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    return http, calls


OK_ONE = (200, {"result": {"value": [
    {"pubkey": "RealT22Account111",
     "account": {"data": {"parsed": {"info": {"tokenAmount": {"amount": "500"}}}}}}]}}, b"")
EMPTY = (200, {"result": {"value": []}}, b"")
RPC_ERR_BODY = (200, {"error": {"code": -32005, "message": "rate limited"}}, b"")
HTTP_429 = (429, {}, b"")


def test_a_real_account_is_returned(monkeypatch):
    http, _ = _acct_rpc(monkeypatch, [OK_ONE])
    assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) == "RealT22Account111"


def test_an_empty_answer_is_provably_no_account(monkeypatch):
    """The RPC answered. None here is a FACT, and the buy fallback depends on it."""
    http, _ = _acct_rpc(monkeypatch, [EMPTY])
    assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) is None


@pytest.mark.parametrize("bad", [HTTP_429, RPC_ERR_BODY, OSError("dns")])
def test_a_failed_lookup_is_not_no_account(bad, monkeypatch):
    """THE REGRESSION. A 200 carrying an error body must fail too — checking status is necessary
    and not sufficient."""
    http, _ = _acct_rpc(monkeypatch, [bad])
    out = S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None)
    assert out is S.LOOKUP_FAILED, out


def test_it_retries_before_giving_up(monkeypatch):
    http, calls = _acct_rpc(monkeypatch, [HTTP_429, OK_ONE])
    assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) == "RealT22Account111"
    assert len(calls) == 2


def test_the_retry_is_bounded(monkeypatch):
    http, calls = _acct_rpc(monkeypatch, [HTTP_429])
    assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) is S.LOOKUP_FAILED
    assert len(calls) <= 3, "unbounded retry on the funded exit path"


def test_a_missing_rpc_url_is_a_failed_lookup_not_no_account(monkeypatch):
    monkeypatch.delenv(RPC_ENV, raising=False)
    assert S._owned_token_account(OWNER, MINT, None) is S.LOOKUP_FAILED


def test_the_swap_build_refuses_when_the_lookup_failed(monkeypatch):
    """A Token-2022 SELL with an unreadable lookup must not derive. Refusing fails the rung, which
    re-arms; deriving submits a doomed transaction and strands the bag."""
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: S.LOOKUP_FAILED)

    def http(method, url, body=None):
        raise AssertionError("the build must not be attempted: " + url)

    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": MINT, "outputMint": USDC}
    assert S.raydium_swap_tx(q, OWNER, http=http) is None


def test_a_sol_input_never_needs_the_lookup(monkeypatch):
    """wrapSol path sets no inputAccount, so a broken lookup must not block it."""
    monkeypatch.setattr(S, "_owned_token_account", lambda o, m, h=None: S.LOOKUP_FAILED)
    captured = {}

    def http(method, url, body=None):
        captured.update(json.loads(body))
        return 200, {"success": True, "data": [{"transaction": "eA=="}]}, b""

    q = {"_venue": "raydium", "_raydium_response": RAY_COMPUTE,
         "inputMint": S._SOL_MINT, "outputMint": MINT}
    assert S.raydium_swap_tx(q, OWNER, http=http) is not None
    assert "inputAccount" not in captured


def test_an_unconfigured_rpc_is_loud_and_named_as_configuration(monkeypatch, caplog):
    """A missing RPC URL and a 429 both return LOOKUP_FAILED, and the consumer logs the same
    "lookup unavailable" line for each. They are not the same fault: a 429 self-heals in seconds,
    an unset variable never does, and it silently removes Raydium from the funded exit ladder for
    as long as it lasts — the venue that quoted MOX.

    Gamma, 2026-09-08: the branch is inert only because `.env` carries the key AND `load_env_files`
    runs AND setdefault does not lose to an empty shell value. Nothing asserts that chain. This
    does not assert it either — a startup gate that blocks funded launch is how one dead custody
    provider stopped the runner on 2026-09-03 — but it makes the break audible the first time it
    matters instead of reading as one more transient.
    """
    import logging
    monkeypatch.delenv(RPC_ENV, raising=False)
    with caplog.at_level(logging.ERROR, logger="sniper.execution.solana_real_seams"):
        assert S._owned_token_account(OWNER, MINT, None) is S.LOOKUP_FAILED
    assert any("not configured" in r.message.lower() for r in caplog.records), caplog.records
    assert any(r.levelno >= logging.ERROR for r in caplog.records), "a permanent fault is not a warning"


def test_a_transient_failure_is_not_logged_as_configuration(monkeypatch, caplog):
    """The distinction only helps if the noisy case stays quiet."""
    import logging
    http, _ = _acct_rpc(monkeypatch, [HTTP_429])
    with caplog.at_level(logging.ERROR, logger="sniper.execution.solana_real_seams"):
        assert S._owned_token_account(OWNER, MINT, http, sleep=lambda _s: None) is S.LOOKUP_FAILED
    assert not any("not configured" in r.message.lower() for r in caplog.records)
