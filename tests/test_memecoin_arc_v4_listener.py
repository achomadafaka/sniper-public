"""Arc (5042) V4 pool-key observation lane (2026-09-16).

Reuses the SAME push-lane primitives 4663 already runs in production
(memecoin_onchain_push.initialize_filter / store_initialize / decode_initialize_log, the
memecoin_evm_v4_pool_keys store, scripts/research/onchain_evm_wallet_watch.py's chain-configurable
path) against Arc's real PoolManager. Observation only: no wallet arming, no execution, no
trading authority -- see test_nothing_in_the_push_lane_can_reach_a_funded_hook below.
"""
import json

import pytest

from sniper.execution import evm_v4_quote as V
from sniper.execution.evm_v4_launchpad import pool_id_for_key
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg
from thread_local_time import patch_local  # noqa: E402  (2026-09-29)

ARC = "5042"

# Recorded live 2026-09-16 via eth_subscribe against the operator's dedicated Arc RPC (QuickNode),
# a real Initialize event on Arc's real PoolManager (V4[ARC]["pool_manager"]).
_REAL_ARC_INIT_LOG = {
    "address": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
    "topics": [
        "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438",
        "0x29fa006621f109b2c7f9d702c5aabf224a9597c67292bb3c834dabe76f8cc187",
        "0x000000000000000000000000638623b37514ef5d0189db0665ba1a2d1fada584",
        "0x000000000000000000000000ece5ca8bf9220718e5727754026757512212cb3c",
    ],
    "data": ("0x0000000000000000000000000000000000000000000000000000000000000000"
            "0000000000000000000000000000000000000000000000000000000000000001"
            "00000000000000000000000001ef4b86be3e26cf1ac65f12c9daffaecca62acc0"
            "00000000000000000000000000000000000000025b7ef40e05a85b737d6bb6ff"
            "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffe91c3"),
    "blockNumber": "0x142ec18",
    "blockTimestamp": "0x6aaa96ea",
    "transactionHash": "0x0dbfa313e1c667095cf4d8bbf3cdf026a43ee8b52d6c8274619754f17b775922",
}


# --- 1. decode + hash-verify a RECORDED Arc log -------------------------------------------------

def test_the_recorded_arc_log_decodes_and_hash_verifies():
    key = V.decode_initialize_log(_REAL_ARC_INIT_LOG)
    assert key == {
        "currency0": "0x638623b37514ef5d0189db0665ba1a2d1fada584",
        "currency1": "0xece5ca8bf9220718e5727754026757512212cb3c",
        "fee": 0, "tick_spacing": 1,
        "hooks": "0x01ef4b86be3e26cf1ac65f12c9daffaecca62acc",
    }
    assert pool_id_for_key(key).lower() == _REAL_ARC_INIT_LOG["topics"][1].lower()


# --- 2. store round-trip, chain-scoped -----------------------------------------------------------

class _NoopConn:
    def execute(self, *_a):
        pass

    def close(self):
        pass


def test_store_initialize_writes_the_recorded_arc_key(monkeypatch):
    stored = []
    monkeypatch.setattr("sniper.persistence.db_core.get_connection", lambda *a, **k: _NoopConn())
    monkeypatch.setattr(
        "sniper.persistence.db_memecoin.put_evm_v4_pool_key",
        lambda conn, *, chain_id, pool_id, pool_manager, key, now_iso=None:
            stored.append((chain_id, pool_id, pool_manager, key)))

    assert P.store_initialize(ARC, _REAL_ARC_INIT_LOG) is True
    assert len(stored) == 1
    chain_id, pool_id, pool_manager, key = stored[0]
    assert chain_id == ARC
    assert pool_manager == V.V4[ARC]["pool_manager"]
    assert pool_id.lower() == _REAL_ARC_INIT_LOG["topics"][1].lower()
    assert key["currency1"] == "0xece5ca8bf9220718e5727754026757512212cb3c"


def test_the_read_side_is_scoped_by_chain_not_just_pool_id(monkeypatch):
    """Same shape 4663 already relies on: a stored key is read back keyed on (chain, pool_id,
    pool_manager), so an Arc row and a 4663 row can never collide even on an equal pool_id."""
    seen = []
    monkeypatch.setattr(
        "sniper.persistence.db_core.get_readonly_connection", lambda *a, **k: _NoopConn())
    monkeypatch.setattr(
        "sniper.persistence.db_memecoin.get_evm_v4_pool_key",
        lambda conn, *, chain_id, pool_id, pool_manager:
            seen.append((chain_id, pool_id, pool_manager)) or None)

    V._stored_pool_key(ARC, "0xdead")
    assert seen == [(ARC, "0xdead", V.V4[ARC]["pool_manager"])]


def test_an_initialize_from_a_foreign_pool_manager_stores_nothing_for_arc():
    foreign = dict(_REAL_ARC_INIT_LOG, address="0x" + "ee" * 20)
    assert P.store_initialize(ARC, foreign) is False


# --- 3. observe-only mode --------------------------------------------------------------------

def _boom(*_a, **_k):
    raise AssertionError("_require_addresses must not be called in observe-only mode")


def test_observe_only_never_blocks_on_wallet_readiness(monkeypatch):
    import scripts.research.onchain_evm_wallet_watch as w

    w._configure(ARC)
    monkeypatch.setattr(w, "_require_addresses", _boom)
    monkeypatch.setattr(w, "get_connection", lambda path: _NoopConn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_verified_addresses", lambda c: {})
    monkeypatch.setattr(w, "HEARTBEAT", w.HEARTBEAT)   # keep current path; write is harmless
    started = {}

    def fake_run_push_stream(ws_url, **kwargs):
        started["armed"] = tuple(kwargs["addresses_fn"]() or ())
        started["ws_url"] = ws_url
        return kwargs.get("state")

    monkeypatch.setattr(
        "sniper.services.memecoin_onchain_push.run_push_stream", fake_run_push_stream)
    try:
        w.run_push("wss://arc-test", observe_only=True)
    finally:
        w._configure("4663")   # restore the module default for any test sharing this worker
    assert started["armed"] == ()
    assert started["ws_url"] == "wss://arc-test"


def test_run_refuses_observe_only_without_a_ws_url(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w

    monkeypatch.delenv("MEMECOIN_EVM_WS_URL_5042", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_RPC_POOL_URLS_5042", raising=False)
    try:
        with pytest.raises(SystemExit, match="refusing to start"):
            w.run(chain=ARC, observe_only=True)
    finally:
        w._configure("4663")


def test_a_dark_socket_in_observe_only_mode_never_polls(monkeypatch, capsys):
    """The poll loop has no Initialize handling at all -- falling back to it in observe-only
    mode would do nothing useful and would hit eth_getLogs on a range-capped endpoint."""
    import scripts.research.onchain_evm_wallet_watch as w

    w._configure(ARC)
    monkeypatch.setattr(w, "_require_addresses", _boom)
    monkeypatch.setattr(w, "get_connection", lambda path: _NoopConn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_verified_addresses", lambda c: {})
    monkeypatch.setattr(w, "run_poll", _boom)
    captured = {}

    def fake_run_push_stream(ws_url, **kwargs):
        kwargs["on_fallback"]()
        return kwargs.get("state")

    monkeypatch.setattr(
        "sniper.services.memecoin_onchain_push.run_push_stream", fake_run_push_stream)
    try:
        w.run_push("wss://arc-test", observe_only=True)
    finally:
        w._configure("4663")
    assert "no poll fallback" in capsys.readouterr().out


# --- 4. events_60s is a true rolling window, not a lifetime average ---------------------------

def test_events_60s_ages_out_old_events(monkeypatch, tmp_path):
    import scripts.research.onchain_evm_wallet_watch as w

    w._configure(ARC)
    monkeypatch.setattr(w, "_require_addresses", _boom)
    monkeypatch.setattr(w, "get_connection", lambda path: _NoopConn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_verified_addresses", lambda c: {})
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr("sniper.services.memecoin_onchain_push.store_initialize",
                        lambda chain, log: True)

    # started() + three on_initialize timestamps (0, 10, 70) + _beat's own two monotonic() reads,
    # then plenty of headroom so a clock read past that point never raises StopIteration.
    clock = iter([0.0, 0.0, 10.0, 70.0, 70.0, 70.0] + [70.0] * 100)
    patch_local(monkeypatch, "monotonic", lambda: next(clock))

    captured = {}

    def fake_run_push_stream(ws_url, **kwargs):
        on_init = kwargs["on_initialize"]
        on_init({"address": "x"})   # t=0
        on_init({"address": "x"})   # t=10
        on_init({"address": "x"})   # t=70 -- the t=0 event is now > 60s old
        kwargs["on_head"]()         # writes the heartbeat at t=70 (monotonic reused for beat rate)
        captured["hb"] = json.loads(w.HEARTBEAT.read_text())
        return kwargs.get("state")

    monkeypatch.setattr(
        "sniper.services.memecoin_onchain_push.run_push_stream", fake_run_push_stream)
    try:
        w.run_push("wss://arc-test", observe_only=True)
    finally:
        w._configure("4663")
    # events at t=10 and t=70 are within 60s of t=70; the t=0 event is not.
    assert captured["hb"]["events_60s"] == 2


# --- 5. nothing here can reach a funded hook ----------------------------------------------------

_FORBIDDEN = ("execute_entry_evm", "execute_exit_evm", "build_real_evm_executor",
             "sign_fn", "send_swap_fn", "load_keypair", "private_key", "broadcast")


def test_nothing_in_the_push_lane_can_reach_a_funded_hook():
    """Source-inspection guard: memecoin_onchain_push.py is the module BOTH the transfer-
    detection and pool-discovery push lanes share. It must never import or reference anything
    that signs or submits a transaction -- it only decodes logs and calls the two writers this
    file already covers (store_initialize, and the caller's own detection-row writer)."""
    import inspect
    import sniper.services.memecoin_onchain_push as push_module
    text = inspect.getsource(push_module)
    for term in _FORBIDDEN:
        assert term not in text, f"push lane source mentions {term!r} — it must stay observation-only"


def test_stored_pool_key_read_fails_fast_on_a_busy_database(monkeypatch):
    # 2026-09-16 13:02Z: the runner was killed as sweep_stale inside this cache read. The read has
    # a chain fallback, so it must never inherit the 30s busy wait of a default reader.
    from sniper.execution import evm_v4_quote as V
    from sniper.persistence import db_core, db_memecoin as m
    seen = []

    class Conn:
        def execute(self, sql, *a):
            seen.append(sql)
        def close(self):
            pass

    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: Conn())
    monkeypatch.setattr(m, "get_evm_v4_pool_key", lambda conn, **k: {"ok": True})
    assert V._stored_pool_key("4663", "0x" + "11" * 32) == {"ok": True}
    timeouts = [int(s.split("=")[1].strip(" ;")) for s in seen if "busy_timeout" in s]
    assert timeouts and timeouts[-1] <= 500
