"""An unbuildable movement-only bridge releases the ordinary lane before any fresh read."""
import sqlite3

import pytest

from sniper.services import memecoin_rh_fast_decide as fast
from sniper.services import memecoin_rh_fast_lane as lane
from sniper.settings import memecoin as cfg
from test_memecoin_rh_bridge_fresh_read import BRIDGE_SIG, Reader, SOURCE_B, STALE_A
from test_memecoin_rh_fast_decide import B, CTX
from test_memecoin_rh_fast_routes import A_SQRT, APOOL, BPOOL, USDG, _brow, _btape


@pytest.fixture(autouse=True)
def movement_only(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)


@pytest.mark.parametrize("a_row", [STALE_A, _brow(APOOL, B - 2, "0xa", A_SQRT)])
def test_unbuildable_bridge_stops_before_state_checks_or_rpc(a_row):
    reader, sig = Reader(), dict(BRIDGE_SIG)
    out = fast.decide(sig, tape=_btape(a_row, SOURCE_B), **dict(CTX, fresh_read=reader))

    assert out["reason"] == "bridge_output_state_unbound"
    assert out["tx"] is None and reader.calls == []
    assert (out["route_kind"], out["pool"], out["bridge_pool"], out["bridge"]) == (
        "v4_bridge", BPOOL, APOOL, USDG)
    assert "a_state_kind" not in out and "state_kind" not in out
    assert "bridge_fresh_read_tried" not in sig
    assert out["reason"] not in fast._SOURCE_REASK_REASONS


def test_refusal_is_saved_and_releases_the_feed_hold_without_a_lane_datagram(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMECOIN_RH_FAST_FEED_HOLD", "1")
    sent, dispatched, learned, confirmed = [], [], [], []
    holds = lane.make_feed_hold_sender("live", "\0test", send_fn=lambda _, p: sent.append(p) or True)
    sender = lane.make_lane_sender("live", "\0test", send_fn=lambda _, p: dispatched.append(p))
    sig, reader, path = dict(BRIDGE_SIG, permit_nonce=1), Reader(), str(tmp_path / "decisions.db")
    assert holds.hold(sig)
    out = fast.handle_signal(sig, 0.0, tape=_btape(STALE_A, SOURCE_B),
                             live=dict(CTX, fresh_read=reader), db_path=path, now_fn=lambda: 1000.0,
                             on_decided=sender, learn=learned.append,
                             confirm=lambda *args: confirmed.append(args))

    assert out["reason"] == "bridge_output_state_unbound"
    assert holds.settle(sig, out)
    assert [p["kind"] for p in sent] == [lane.FEED_HOLD_KIND, lane.FEED_RELEASE_KIND]
    assert dispatched == learned == confirmed == reader.calls == []
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT reason, route_kind, pool, bridge_pool, a_fresh_read, a_fresh_read_ms "
                           "FROM rh_fast_decisions").fetchone()
    assert row == ("bridge_output_state_unbound", "v4_bridge", BPOOL, APOOL, None, None)


def test_movement_off_keeps_the_existing_bridge_fresh_read(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)
    reader = Reader()
    out = fast.decide(dict(BRIDGE_SIG), tape=_btape(STALE_A, SOURCE_B), **dict(CTX, fresh_read=reader))
    assert out["reason"] == "would_send" and reader.calls == [(APOOL, B)]
    assert out["a_state_kind"] == "fresh_read" and out["tx"]


def test_deposit_source_pool_bridge_also_releases_before_a_fresh_read(monkeypatch):
    from test_memecoin_rh_deposit_source_pool import _bridged_source, _tape

    monkeypatch.setattr(cfg, "MEMECOIN_RH_FAST_DEPOSIT_SOURCE_POOL", True)
    reader = Reader()
    out = fast.decide(dict(BRIDGE_SIG), tape=_tape(_bridged_source()), **dict(CTX, fresh_read=reader))
    assert out["reason"] == "bridge_output_state_unbound" and out["pool_choice"] == "source_pool"
    assert out["pool"] == BPOOL and reader.calls == []


@pytest.mark.parametrize("kind", ["native", "weth"])
def test_movement_only_keeps_non_bridge_decisions(kind):
    from test_memecoin_rh_fast_decide import META, SIG, TOKEN, ZPOOL, ZUP_IN, ZUP_PRE, _paid_for, _row
    from test_memecoin_rh_fast_routes import COIN, WPOOL2

    if kind == "native":
        tape = fast.TapeState(META.get)
        tape.ingest([_row(ZPOOL, B - 2, "0xn", ZUP_PRE["sqrt"])])
    else:
        tape = _btape(_brow(WPOOL2, B - 2, "0xw", ZUP_PRE["sqrt"], liq=ZUP_PRE["liquidity"]))
    sig = dict(SIG, mint=TOKEN if kind == "native" else COIN, paid=_paid_for(ZUP_IN))
    reader = Reader()
    out = fast.decide(sig, tape=tape, **dict(CTX, fresh_read=reader))
    assert (out["reason"], out["route_kind"]) == ("would_send", "v4_" + kind)
    assert out["tx"] and reader.calls == []


def test_receipt_backed_bridge_source_keeps_its_shared_route():
    from rh_source_fill_fixture import proof_for
    from test_memecoin_rh_source_binding import SIG
    from test_memecoin_rh_fast_routes import B_LIQ, B_SQRT, COIN

    sig = dict(SIG, mint=COIN, payment_token=USDG, paid=10**18)
    tape = _btape(STALE_A, _brow(BPOOL, B, sig["tx_hash"], B_SQRT, liq=B_LIQ, a0=1000, a1=-2000))
    proof = proof_for(sig, got=1800, pool=BPOOL, sqrt=B_SQRT)
    tape.learn_source_fills([proof])
    reader = Reader()
    out = fast.decide(sig, tape=tape, **dict(CTX, allow_estimated_source=False, fresh_read=reader))
    assert (out["reason"], out["route_kind"], out["src_out"]) == ("would_send", "v4_shared", 1800)
    assert out["source_fill"] == proof and reader.calls == []
