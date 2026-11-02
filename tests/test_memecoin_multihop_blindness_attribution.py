"""A multihop rung that could not SEE must not report that no route EXISTS.

qINU, 2026-09-08T11:38:02Z: a CryoPalmar buy on chain 4663 aborted `no_route_all_venues`.
GeckoTerminal cooldowns were logged at 11:38:00 and 11:38:52 — either side of the abort — and the
same route quoted cleanly through `dex_v4_multihop_quote` minutes later, returning 1,237,506 qINU
for 0.05 WETH with no refusal. The route existed the whole time.

The v4 rung has converted "GeckoTerminal was cold and no discovery was observed" into
`blind_reason='rate_limit'` for some time. The v4_multihop rung had no such branch, so its
`blind_reason` was ALWAYS None. `route_refusal` only reports `provider_*` when SOME rung claims
blindness, so a multihop starved of depth data came out as `no_route_all_venues` — byte-identical
to a coin that genuinely has no pool.

That matters beyond one trade. `_pools_with_depth` fails closed when both depth providers go dark,
and depth is precisely what gates this rung, so multihop is the rung MOST exposed to provider
cooldown and was the one unable to say so. Every count of "coins with no route" is contaminated by
blindness until this reports honestly — including the 122 September `no_route` refusals that cost
42 DegenCapitalLLC and 18 CryoPalmar signals.
"""
import ast
import pathlib

import pytest

from _source_guard import code_of
from sniper.execution.evm_real_seams import (QUOTE_MISS_FAILURES, classify_quote_miss,
                                             multihop_blind_reason)

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC_PATH = REPO / "sniper" / "execution" / "evm_real_seams.py"
SRC = SRC_PATH.read_text()


def _rung_code(name):
    """The rung's own CODE, comments removed.

    2026-09-18: these guards used to read the raw source, and the rung carries a long comment that
    names `rate_limit`, `gt_cold` and `last_discovery_observed` in prose. After the condition moved
    into `multihop_blind_reason` the text checks still passed on the comment alone — a guard
    reading its own explanation. They assert behaviour now, and the text check that remains is
    against stripped code.
    """
    tree = ast.parse(code_of(SRC_PATH))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.unparse(node)
    raise AssertionError("rung %s not found" % name)


def test_the_multihop_rung_can_report_rate_limit_blindness():
    """The whole defect: this rung's blind_reason could never be anything but None."""
    code = _rung_code("_quote_v4_multihop")
    assert "multihop_blind_reason" in code, "the rung no longer asks for a blind reason at all"
    assert "gt_cold" in code and "last_discovery_observed" in code, (
        "the rung must pass the cooldown state and the discovery evidence to that decision")
    assert multihop_blind_reason("no_bridge_candidates", gt_cold=True,
                                 discovery_observed=False) == "discovery_cooldown"


def test_it_uses_the_same_test_as_the_v4_rung():
    """Two rungs reading the same providers must not disagree about what blindness is."""
    v4 = _rung_code("_quote_v4")
    for token in ("gt_cold", "last_discovery_observed", "DISCOVERY_COOLDOWN"):
        assert token in v4, token
    # the multihop rung reaches the same verdict through the shared helper
    assert multihop_blind_reason("anything", gt_cold=True,
                                 discovery_observed=False) == "discovery_cooldown"
    assert multihop_blind_reason("anything", gt_cold=True, discovery_observed=True) is None


def test_a_genuine_no_route_is_still_reported_as_a_route_failure():
    """The fix must not relabel every refusal as a provider problem. With providers healthy and no
    local limit, the rung stays silent and the ladder reports a route failure."""
    assert multihop_blind_reason("no_bridge_candidates", gt_cold=False,
                                 discovery_observed=True) is None
    code, _detail = classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "reason": "no_bridge_candidates"}])
    assert code == "no_route"


@pytest.mark.parametrize("note", ["search_stopped:discovery_limit", "search_stopped:request_budget"])
def test_a_search_we_stopped_ourselves_is_blindness_not_absence(note):
    """Audit E4, 2026-09-18. Our own deadline or request budget ended discovery, so nothing about
    the token was measured. Reporting `no_route` there also sends an exit to the LI.FI fallback."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    assert multihop_blind_reason(note, gt_cold=False, discovery_observed=True) == "search_limit"
    code, _detail = classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "blind_reason": "search_limit"}])
    assert code == "provider_search_limit"
    assert QUOTE_MISS_FAILURES[code] is QuoteFailure.PROVIDER_TIMEOUT
    assert QUOTE_MISS_FAILURES[code] is not QuoteFailure.NO_ROUTE


def test_the_round_trip_refusal_is_not_mistaken_for_blindness():
    """`round_trip_over_budget` is a decision, not a failure to see. It must keep blind_reason None
    or an expensive-but-visible route would be excused as a provider outage."""
    code = _rung_code("_quote_v4_multihop")
    idx = code.index("round_trip_over_budget")
    assert "(None, None," in code[max(0, idx - 200):idx], (
        "the over-budget return must pass blind_reason=None")
    # and the shared helper cannot turn that note into blindness either
    assert multihop_blind_reason("round_trip_over_budget:-15.4", gt_cold=False,
                                 discovery_observed=True) is None
    code_miss, _detail = classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "reason": "round_trip_over_budget:-15.4"}])
    assert code_miss == "route_over_budget"


def test_our_own_geckoterminal_cooldown_is_not_a_provider_rate_limit():
    """Alpha, 2026-09-28. The funded GT cooldown makes no HTTP call, so no provider refused us: it
    is a local limit. As `rate_limit` it became QuoteFailure.RATE_LIMIT, and an exit preview with
    that failure holds its provider group's exact-quote budget (every chain before d1ce0ce7). It is
    typed like our own search limit now: transient, nothing measured, never a route verdict."""
    from sniper.analysis.memecoin_quote_failures import QuoteFailure, is_infra
    code, _detail = classify_quote_miss(
        [{"rung": "v4_multihop", "hit": False, "blind_reason": "discovery_cooldown"}])
    assert code == "provider_discovery_cooldown"
    assert QUOTE_MISS_FAILURES[code] is QuoteFailure.PROVIDER_TIMEOUT
    assert QUOTE_MISS_FAILURES[code] is not QuoteFailure.RATE_LIMIT
    assert is_infra(QUOTE_MISS_FAILURES[code]), "it must never advance a token's unsellable count"


def test_the_cooldown_code_keeps_the_rate_limit_retry_timing():
    """Typed `provider_rate_limit` before, it waited the GT cooldown on its first two scheduled
    retries and got the one-shot immediate re-quote. The retype must not change either."""
    import inspect
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    res = {"reason": "no quote (provider_discovery_cooldown: v4 could not read: discovery_cooldown)"}
    old = {"reason": "no quote (provider_rate_limit: v4 could not read: rate_limit)"}
    interval = 10.0 * float(cfg.MEMECOIN_GT_429_COOLDOWN_S) + 1.0     # so the cooldown is shorter
    assert (rr._entry_retry_delay(res, interval, attempts=0, network_id="4663")
            == rr._entry_retry_delay(old, interval, attempts=0, network_id="4663")
            == float(cfg.MEMECOIN_GT_429_COOLDOWN_S))
    source = inspect.getsource(rr)
    immediate = source[source.index('result.get("quote_miss_code") in {'):]
    immediate = immediate[:immediate.index("}):")]
    assert '"provider_rate_limit"' in immediate and '"provider_discovery_cooldown"' in immediate
