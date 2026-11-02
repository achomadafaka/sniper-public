import pytest


@pytest.fixture(autouse=True)
def _polymarket_is_sinkholed(monkeypatch):
    """This host resolves *.polymarket.com to an unreachable address (2026-07-29 sinkhole, moved to an
    unreachable IPv6 on 2026-09-22), so a live activity fetch hangs past the test budget instead of
    failing fast. These tests exercise flags and stamps, never the data: fail the fetch at once, as
    a refused connection did before."""
    from data import polymarket_activity_client as _pac

    def _no_network(path, **_kw):
        raise _pac.PolymarketActivityError("request failed after retries: sinkholed in tests")
    monkeypatch.setattr(_pac, "_get_json", _no_network)
