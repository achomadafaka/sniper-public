"""V4 pool discovery must not be single-homed.

`_onchain_pools_for_mint` is, by its own comment, "the ONLY source that can see a pool younger than
the public indexers' lag, so its failure is the whole difference between 'no route' and 'could not
look'". It drew its endpoints from `_archive_rpc_urls`, which dedupes (primary, *archive); chain
4663 configures ONE archive endpoint and it is the same host as the primary. So discovery had no
fallback while a three-endpoint read pool sat configured and healthy beside it.

On 2026-09-08 that host failed for ~50s and took six consecutive attempts on 0x894917b3 with it —
17:37:23 through 17:38:13, every one `discovery_rpc_failed`, no position opened.

The asymmetry is deliberate and is what these tests pin: the 8192-block window (thirteen minutes on
4663) may use any healthy node, and the full-range `0x0`->`latest` scan may not, because a pruned
node answers that with a SHORT LIST rather than an error and a short list reads as proof of absence.
"""
import pytest

from sniper.execution import evm_v4_quote as Q

CHAIN = "4663"
PRIMARY = "https://primary.example"
POOL2 = "https://second.example"
POOL3 = "https://third.example"
MINT = "0x894917b3f43091ae15fbb8c655e70ba61846909d"
POOL_ID = "0x" + "ab" * 32


@pytest.fixture()
def wiring(monkeypatch):
    """Primary is the only archive; the read pool has three."""
    monkeypatch.setattr(Q, "_archive_rpc_urls", lambda c, u: (PRIMARY,))
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.pool_for",
                        lambda u, cfg=None: [PRIMARY, POOL2, POOL3])
    monkeypatch.setattr(Q, "_archive_is_chain", lambda u, c, http=None: True)
    monkeypatch.setattr(Q, "_is_v4_pool_id", lambda p: p == POOL_ID)
    return None


def _rpc_factory(behaviour):
    """behaviour: {url: 'ok' | 'raise' | 'empty'} -> a _rpc stand-in recording its calls."""
    calls = []

    def _rpc(url, method, params, http=None):
        calls.append((url, method, params[0].get("fromBlock") if params and isinstance(params[0], dict) else None))
        mode = behaviour.get(url, "raise")
        if mode == "raise":
            raise RuntimeError(f"transport down: {url}")
        if method == "eth_blockNumber":
            return {"result": hex(1_000_000)}
        if mode == "empty":
            return {"result": []}
        return {"result": [{"topics": ["0xinit", POOL_ID]}]}

    _rpc.calls = calls
    return _rpc


def test_the_window_falls_back_to_the_read_pool_when_the_primary_is_down(monkeypatch, wiring):
    """THE DEFECT. Primary down, a pool endpoint healthy: discovery must still find the pool."""
    rpc = _rpc_factory({PRIMARY: "raise", POOL2: "ok", POOL3: "ok"})
    monkeypatch.setattr(Q, "_rpc", rpc)
    pools, failed = Q._onchain_pools_for_mint(CHAIN, MINT, PRIMARY)
    assert pools == [POOL_ID], "a healthy read endpoint must answer when the archive host is down"
    assert failed is False, "found a pool, so this is not a blind read"
    assert any(u == POOL2 for u, _m, _b in rpc.calls), "the read pool was never tried"


def test_every_endpoint_down_still_reports_blind_not_absent(monkeypatch, wiring):
    rpc = _rpc_factory({PRIMARY: "raise", POOL2: "raise", POOL3: "raise"})
    monkeypatch.setattr(Q, "_rpc", rpc)
    pools, failed = Q._onchain_pools_for_mint(CHAIN, MINT, PRIMARY)
    assert pools == []
    assert failed is True, "a total transport failure must never read as proof of absence"


def test_the_full_range_scan_is_archive_only(monkeypatch, wiring):
    """A non-archive node must never serve `0x0`->`latest`.

    It would answer a pruned SHORT LIST instead of raising, and an empty full-range answer is
    treated as real evidence of absence.

    The archive host must FAIL the full range here, not merely answer it. The loop returns on the
    first success, so a primary that answers never reaches the others and the test passes whatever
    the endpoint list contains — mutation-checked 2026-09-08, and the first version of this test
    was blind for exactly that reason.
    """
    calls = []

    def _rpc(url, method, params, http=None):
        frm = params[0].get("fromBlock") if params and isinstance(params[0], dict) else None
        calls.append((url, method, frm))
        if method == "eth_blockNumber":
            if url == PRIMARY:
                return {"result": hex(1_000_000)}
            raise RuntimeError("window head unavailable off-archive")
        if url == PRIMARY and frm == "0x0":
            raise RuntimeError("archive full-range down")   # forces the loop to continue
        return {"result": []}                               # window: empty, so the full range runs

    monkeypatch.setattr(Q, "_rpc", _rpc)
    Q._onchain_pools_for_mint(CHAIN, MINT, PRIMARY)
    full = {u for u, m, b in calls if m == "eth_getLogs" and b == "0x0"}
    assert full, "the full-range fallback should have run after an empty window"
    assert full == {PRIMARY}, f"full range must be archive-only, saw {sorted(full)}"


def test_a_read_pool_lookup_failure_does_not_cost_us_the_primary(monkeypatch, wiring):
    monkeypatch.setattr("sniper.execution.evm_rpc_pool.pool_for",
                        lambda u, cfg=None: (_ for _ in ()).throw(RuntimeError("pool config bad")))
    rpc = _rpc_factory({PRIMARY: "ok"})
    monkeypatch.setattr(Q, "_rpc", rpc)
    pools, failed = Q._onchain_pools_for_mint(CHAIN, MINT, PRIMARY)
    assert pools == [POOL_ID] and failed is False
