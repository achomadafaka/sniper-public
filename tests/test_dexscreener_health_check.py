"""The DexScreener recovery watcher's health predicate.

2026-08-24: the data plane died while the static routes stayed up. TLS completed in 16ms and
`api.dexscreener.com/` answered 404 instantly, while `/latest/dex/tokens/{mint}` hung with zero
bytes for the full timeout. So "the host responded" was never the signal, and a checker keyed on
HTTP status would have declared recovery throughout the outage. The signal is the DATA: a pairs
array carrying a usable priceUsd.

This matters beyond the alert. Recovery also stamps the blind-gate cohort closed — the window in
which the mcap ceiling, coin band, revival guard, liquidity screens and the execution-premium cap
were all dark. A false recovery would close that window early and silently mark unscreened fills
as normally-screened evidence.
"""
import subprocess
import sys

SCRIPT = "scripts/dexscreener_health_check.py"


def check(body: str) -> int:
    """Exit status of the predicate for a raw response body."""
    return subprocess.run([sys.executable, SCRIPT], input=body,
                          text=True, capture_output=True).returncode


def test_a_real_payload_is_healthy():
    assert check('{"pairs":[{"priceUsd":"1.5"}]}') == 0


def test_an_empty_pairs_array_is_not_recovery():
    """The shape of a token DexScreener knows nothing about — not the shape of a live API."""
    assert check('{"pairs":[]}') == 1


def test_null_pairs_is_not_recovery():
    assert check('{"pairs":null}') == 1


def test_a_pair_without_a_price_is_not_recovery():
    assert check('{"pairs":[{}]}') == 1


def test_a_zero_price_is_not_recovery():
    """0 is not a price. Treating it as one would also feed a 0 into anything downstream."""
    assert check('{"pairs":[{"priceUsd":"0"}]}') == 1


def test_an_html_error_page_is_not_recovery():
    """A proxy or edge can answer 200 with HTML. Status alone would pass this."""
    assert check("<html>502 Bad Gateway</html>") == 1


def test_an_empty_body_is_not_recovery():
    assert check("") == 1


def test_a_non_object_body_is_not_recovery():
    assert check("[1,2,3]") == 1

def test_a_populated_bare_list_is_healthy():
    """`/latest/dex/tokens/` documents {"pairs": [...]}, but on 2026-08-24 the same route answered a
    BARE LIST for one mint and an empty `pairs` dict for another. A predicate that knew only the dict
    shape would refuse a healthy array forever and the recovery watcher would never fire."""
    assert check('[{"priceUsd": "0.00021"}]') == 0


def test_an_empty_bare_list_is_still_down():
    """Seen live during the partial recovery: HTTP 200 in 53 ms carrying `[]`. Fast and empty is
    still no data."""
    assert check("[]") == 1


def test_a_bare_list_without_a_price_is_still_down():
    assert check('[{"marketCap": 1}]') == 1
