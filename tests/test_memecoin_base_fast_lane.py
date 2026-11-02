"""Base 8453 fast lane, step 1 part B: the runner's lane (plan 2026-10-14). Offline: no host, no
signing key beyond the fixed test account, no send."""
import os
import time
from types import SimpleNamespace

import pytest

from sniper.execution.evm_v4_quote import V4
from sniper.execution.submission_transport import AttemptState, SubmissionTransport
from sniper.services import memecoin_base_fast_lane as base
from sniper.services import memecoin_rh_fast_lane as rh
from sniper.settings import memecoin as cfg

TX = "0x" + "ab" * 32
MINT = "0x" + "cd" * 20
WALLET = "0x" + "ef" * 20
POOL = "0x" + "12" * 32
NATIVE = "0x" + "00" * 20
KEY = {"currency0": NATIVE, "currency1": MINT, "fee": 0, "tick_spacing": 60, "hooks": NATIVE}
HOST = "https://base-lane.example"
FILL = {"proof_version": "wallet-net-fill-v1", "receipt_proof_version": "direct-wallet-paid-v1",
        "network_id": "8453", "tx_hash": TX, "block_number": 36_000_000, "block_hash": "0x" + "34" * 32,
        "wallet": WALLET, "source_user_id": "u1", "mint": MINT, "payment_token": base.WETH,
        "payment_atomic": str(10 ** 15), "acquired_atomic": str(10 ** 18), "payment_decimals": 18,
        "token_decimals": 18, "paid_usd": 2.5, "payment_basis": "wallet_quote_outflow"}


def datagram(**kw):
    return dict(dict(chain="8453", tx_hash=TX, seq=36_000_000, wallet=WALLET, mint=MINT,
                     source_user_id="u1", handle="h", arrival_utc="2026-10-14T00:00:00+00:00",
                     route_kind="v4_native", key=KEY, pool=POOL, cap_num=2, cap_den=1,
                     clip_wei=10 ** 16, min_out=5 * 10 ** 15, post_sqrt=1 << 96, src_in_wei=10 ** 15,
                     src_out=str(10 ** 18), post_wei_num=1, post_wei_den=1, in_is_c0=True,
                     paid_usd=2.5, source_fill=FILL, eth_usd=2500.0, eth_usd_at=1000.0,
                     sent_epoch=1000.0, preconf=False), **kw)


@pytest.mark.parametrize("change,why", [
    ({}, None), ({"chain": "4663"}, "malformed"), ({"route_kind": "v4_bridge"}, "malformed"),
    ({"in_is_c0": 1}, "malformed"), ({"cap_num": True}, "malformed"), ({"paid_usd": float("nan")}, "malformed"),
    ({"pool": "0x12"}, "malformed"), ({"source_fill": None}, "malformed"), ({"preconf": 0}, "malformed")])
def test_only_the_8453_watcher_s_well_formed_datagram_is_heard(change, why):
    payload = datagram(**change)
    assert base.check_sender(payload, 7, os.getuid(), watcher_pid=7) == why
    assert base.check_sender(datagram(), 8, os.getuid(), watcher_pid=7) == "not_the_watcher"
    assert base.check_sender(datagram(), 7, os.getuid(), watcher_pid=None) == "no_watcher"
    missing = datagram()
    del missing["source_fill"]
    assert base.check_sender(missing, 7, os.getuid(), watcher_pid=7) == "malformed"


def test_the_routes_setting_refuses_a_kind_v1_cannot_build():
    assert cfg._base_fast_routes("v4_native, v4_weth") == ("v4_native", "v4_weth")
    assert cfg._base_fast_routes("") == ()
    with pytest.raises(ValueError):
        cfg._base_fast_routes("v4_native,v4_bridge")


@pytest.mark.parametrize("raw,mode", [("runner_shadow", "runner_shadow"), ("live", "live"),
                                      ("step0", "step0"), ("", "off"), ("on", "off")])
def test_the_mode_names_the_two_runner_modes(monkeypatch, raw, mode):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", raw)
    assert cfg.memecoin_base_fast_lane_mode() == mode


def _ctx(**kw):
    return dict({"address": WALLET, "v4_direct_quote_fn": lambda *a, **k: None, "rpc_url": "https://custody",
                 "submission_transport": object()}, **kw)


@pytest.mark.parametrize("mode,ctx,url,on", [
    ("off", _ctx(), HOST, False), ("runner_shadow", None, HOST, False),
    ("runner_shadow", _ctx(), "", False), ("runner_shadow", _ctx(), "http://plain", False),
    ("live", _ctx(submission_transport=None), HOST, False), ("runner_shadow", _ctx(), HOST, True)])
def test_the_lane_builds_only_with_a_context_its_own_host_and_live_transport(tmp_path, monkeypatch,
                                                                            mode, ctx, url, on):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    lines = []
    lane = base.build_base_lane({"8453": ctx} if ctx else {}, log=lines.append, mode=mode, root=tmp_path,
                                url=url, ro_connect=lambda: None, dex_base=lambda *a: None)
    assert (lane is not None) == on, lines
    if on:
        assert lane["dispatch"].shadow.CHAIN_ID == 8453 and lane["live"] is None
        assert lane["address_name"] != rh.lane_address(tmp_path)
        assert lane["snapshot"].weth == base.WETH


def test_a_host_another_role_uses_keeps_the_lane_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_RESEARCH_SOMETHING_URL", HOST)
    lines = []
    assert base.build_base_lane({"8453": _ctx()}, log=lines.append, mode="runner_shadow", root=tmp_path,
                                url=HOST, ro_connect=lambda: None) is None
    assert "refused" in lines[-1] and HOST not in lines[-1]


class _Shadow:
    def __init__(self):
        self.seen = []

    def handle(self, payload, *, recv_epoch, safety_reason=None, stats=None):
        self.seen.append((payload, safety_reason, stats))
        return {"reason": "fixture", "tx_hash": payload["tx_hash"], "mint": payload["mint"]}


def test_a_heard_signal_reaches_the_shadow_once_with_the_lane_s_snapshot(tmp_path, monkeypatch):
    from sniper.services.memecoin_bsc_fast_lane import Seen
    monkeypatch.setattr("sniper.services.memecoin_bsc_fast_lane.watcher_pid", lambda beat, now: 7)
    written = []
    monkeypatch.setattr(rh, "write_record", lambda db_path, rec: written.append(rec))
    shadow, snap = _Shadow(), {"native_wei": 10 ** 17, "weth_wei": 0, "nonce": 3, "gas_price_wei": 1,
                               "read_at": 999.0}
    cache = SimpleNamespace(note_candidates=lambda *a, **k: None,
                            reason=lambda mint, now, evidence=None: "no_safety_verdict",
                            stats=lambda mint, now: None)
    registry = {}
    dispatch = base.BaseDispatch(snapshot=SimpleNamespace(current=lambda: snap), shadow=shadow,
                                 cache=cache, registry=registry, seen=Seen(), drops=rh.Drops(),
                                 db_path=str(tmp_path / "lane.db"), beat="beat", live=None)
    dispatch.on_datagram(datagram(), 7, os.getuid(), 1000.1)
    dispatch.on_datagram(datagram(), 7, os.getuid(), 1000.2)            # the same buy again
    dispatch.on_datagram(datagram(chain="4663"), 7, os.getuid(), 1000.3)
    assert len(shadow.seen) == 1
    payload, why, _stats = shadow.seen[0]
    assert payload["snapshot"] is snap and why == "no_safety_verdict"
    assert registry[MINT]["kind"] == "v4_native" and registry[MINT]["key"] == KEY
    assert [r["mode"] for r in written] == ["runner_shadow"]


def test_the_shadow_judges_on_the_8453_context_with_the_8453_router(monkeypatch):
    seen = {}

    def factory(contexts, stats):
        seen["contexts"] = contexts

        def raw(conn, ev):
            seen["ev"] = ev
            return {"status": "shadow"}
        return SimpleNamespace(raw=raw)
    shadow = base.BaseShadow(lock_key="k", lock_dir=None, taker=WALLET, now_fn=time.time,
                             clip_usd=50.0, generation_fn=lambda: 0.0, host_clock=lambda: 1001.0,
                             require_exact_state=False, facts_fn=lambda mint: (18, 10 ** 27),
                             ctx=_ctx(), contexts={"8453": _ctx()}, hook_factory=factory,
                             ro_connect=lambda: SimpleNamespace(execute=lambda *a: None,
                                                                 rollback=lambda: None))
    shadow.cache = SimpleNamespace(cached_answer=lambda mint, label: (True, label))
    snap = {"native_wei": 10 ** 18, "weth_wei": 0, "nonce": 3, "gas_price_wei": 10 ** 7, "read_at": 1000.5}
    rec = shadow.handle(dict(datagram(), snapshot=snap), recv_epoch=1001.0, safety_reason=None,
                        stats=({"price": 1.0}, 1000.0))
    assert rec["reason"] in ("built", "lock_busy"), rec
    assert set(seen["contexts"]) == {"8453"} and seen["ev"].network_id == "8453"
    assert seen["ev"].fomo_event_id == "rhfast:8453:%s:%s" % (TX, MINT)
    tx, _ = rh.lane_buy_tx(datagram(), 10 ** 16, 2_000_000_000, WALLET, chain="8453")
    assert tx["to"] == V4["8453"]["universal_router"]


def test_a_weth_route_s_safety_quote_reads_the_8453_quoter(monkeypatch):
    from sniper.execution import evm_v4_quote as v4
    chains = []
    monkeypatch.setattr(v4, "_quote_single", lambda chain, *a, **k: chains.append(chain) or None)
    weth_key = dict(KEY, currency0=base.WETH)
    rh.route_quote(lambda *a, **k: None, {"kind": "v4_weth", "key": weth_key, "pool": POOL}, MINT,
                   base.WETH, 10 ** 18, host=HOST, http=None, chain="8453")
    assert chains == ["8453"]


def test_live_submits_the_8453_signed_bytes_through_the_context_transport():
    from test_memecoin_rh_shared_submission import Provider, _native, _send, _signed
    from test_memecoin_rh_fast_live import _ledger
    from sniper.persistence import db_memecoin as db
    conn = _ledger()
    provider = Provider("custody-rpc")
    transport = SubmissionTransport([provider], deadline_s=.5)
    native = _native(conn, transport, signed=_signed(chain=8453), lane_class=base.BaseLive)
    del native._post                                  # the Base lane has no other way to send
    try:
        result, rec, pending_id = _send(native, conn)
        request = provider.requests[0]
        assert result == _signed(chain=8453)[1] and request.chain_id == "8453"
        assert rec["send_reply"] == "accepted"
        assert db.entry_submission_binding(conn, pending_id)["submission_backend"] == "base_fast_lane"
    finally:
        transport.close()
        conn.close()


def test_a_4663_signature_on_the_base_lane_is_never_submitted():
    from test_memecoin_rh_shared_submission import Provider, _native, _send, _signed
    from test_memecoin_rh_fast_live import _ledger
    conn = _ledger()
    provider = Provider("custody-rpc", state=AttemptState.ACCEPTED)
    transport = SubmissionTransport([provider], deadline_s=.5)
    native = _native(conn, transport, signed=_signed(chain=4663), lane_class=base.BaseLive)
    del native._post
    try:
        result, rec, _pending_id = _send(native, conn)
        assert provider.requests == [] and result["status"] == "unknown"
        assert rec["_outcome"] == "signed_identity_invalid"
    finally:
        transport.close()
        conn.close()


def test_the_live_base_lane_holds_the_buy_from_receipt_until_its_verdict(tmp_path, monkeypatch):
    from sniper.services.memecoin_bsc_fast_lane import Seen
    monkeypatch.setattr("sniper.services.memecoin_bsc_fast_lane.watcher_pid", lambda beat, now: 7)
    monkeypatch.setattr(rh, "write_record", lambda *a, **k: None)
    monkeypatch.setattr(rh, "write_live_record", lambda *a, **k: None)
    held = rh.LaneHolds(bound_s=1.0, clock=lambda: 1.0, wake_fn=lambda: None)
    monkeypatch.setattr(rh, "LANE_HOLDS", held)
    during = []

    def handle(payload, **_kw):
        during.append(held.holds(TX, MINT))
        return {"reason": "hook:rejected:fixture"}
    cache = SimpleNamespace(note_candidates=lambda *a, **k: None, reason=lambda *a, **k: None,
                            stats=lambda *a, **k: None)
    dispatch = base.BaseDispatch(snapshot=None, shadow=None, cache=cache, registry={}, seen=Seen(),
                                 drops=rh.Drops(), db_path=str(tmp_path / "x.db"), beat="b",
                                 live=SimpleNamespace(handle=handle))
    dispatch.on_datagram(datagram(), 7, os.getuid(), 1000.1)
    assert during == [True] and not held.holds(TX, MINT)
