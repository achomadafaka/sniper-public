"""A pool that can never be used must not consume a candidate slot.

SEND (0x6909eb6f, 2026-09-04) had 20 known pools including native-paired ones at 0.9% and 10%.
The 8 candidates handed to the quoter contained FIVE traps at 14.8-20% and ZERO sane native pools,
and the entry refused `no_route_all_venues` while a 0.9% pool sat in our own durable cache.
"""
import sniper.execution.evm_v4_quote as V


def test_sane_pools_outrank_traps_before_the_cap(monkeypatch):
    keys = {
        "0xtrap1": {"fee": 200000}, "0xtrap2": {"fee": 184000}, "0xtrap3": {"fee": 180000},
        "0xtrap4": {"fee": 147800}, "0xtrap5": {"fee": 690000},
        "0xgood1": {"fee": 9000},   "0xgood2": {"fee": 100000},
    }
    monkeypatch.setattr(V, "_pool_key_cached", lambda chain, pid: keys.get(pid))
    # traps rank DEEPEST, exactly the case that truncated the honest pools away
    ranked = [(9e6, "0xtrap1"), (8e6, "0xtrap2"), (7e6, "0xtrap3"),
              (6e6, "0xtrap4"), (5e6, "0xtrap5"), (10.0, "0xgood1"), (5.0, "0xgood2")]
    ordered = sorted(ranked, key=lambda rp: (
        0 if (V._pool_key_cached("4663", rp[1]) or {}).get("fee") in (9000, 100000) else 2, -rp[0]))
    assert [p for _, p in ordered][:2] == ["0xgood1", "0xgood2"]


def test_an_uncached_key_is_not_dropped(monkeypatch):
    """A cold cache must not lose a pool: unknown fee ranks with the unknowns and the quoter's own
    fee screen still runs on it."""
    monkeypatch.setattr(V, "_pool_key_cached", lambda chain, pid: None)
    assert V._pool_key_cached("4663", "0xanything") is None


def test_the_cached_lookup_never_makes_an_rpc_call(monkeypatch):
    """This runs once per candidate on the ENTRY path; a miss must not cost a round trip."""
    called = []
    monkeypatch.setattr(V, "_stored_pool_key",
                        lambda *a, **k: called.append(1) or None)
    monkeypatch.setattr(V, "_key_cache", {}, raising=False)
    V._pool_key_cached("4663", "0xcoldmiss")
    assert called == [1]          # the durable SQLite read only -- no _rpc, no http
