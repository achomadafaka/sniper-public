"""Model diagnostics retain their inputs after fill without becoming executable quotes."""
import json
from pathlib import Path

import pytest

from sniper.execution import solana_slot_executor as slot
from sniper.persistence import db_memecoin as m
from test_memecoin_solana_slot_executor import conn, _reserve
from test_pump_curve_buy import _Clock, _builder, _buy, SNAP


FRAMES = json.loads((Path(__file__).parent / "fixtures/memecoin/position_73924_price_frames.json").read_text())


class Trace:
    def __init__(self, fail=False):
        self.events, self.fail = [], fail

    def mark(self, _name, **_fields):
        pass

    def stage_completed_at(self, _name):
        return None

    def mark_substage(self, name, **fields):
        if self.fail and name == "slot_model_benchmark":
            raise RuntimeError("diagnostic only")
        self.events.append((name, fields))


@pytest.mark.parametrize("estimated", [False, True])
def test_curve_builder_freezes_source_event_and_reference_basis(monkeypatch, estimated):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    buy = _buy(signature="source-signature", slot=123,
               source_initiation={"payment_basis": "pre_execution_estimate"} if estimated else None)
    _message, validity = _builder(_Clock()).build(buy, 5.0, SNAP)
    frame = validity["pump_source_frame"]
    assert frame["event_log"] == buy.trade_event_log
    assert frame["reference_basis"] == ("pre_execution_estimate" if estimated else "source_trade_event")
    assert frame["source_sig"] == "source-signature" and frame["source_slot"] == 123


@pytest.mark.parametrize("diagnostic_failure", [False, True])
def test_booked_fill_emits_model_benchmark_without_claiming_a_quote(conn, diagnostic_failure):
    trace = Trace(fail=diagnostic_failure)
    result = slot.execute_prebuilt_entry(
        conn, pending=_reserve(conn), signed_b64="AA==", sig="SigSlot1",
        validity={"pump_price_limit": FRAMES["price_limit"]},
        send_fn=lambda _wire: True, confirm_fn=lambda _sig: "confirmed",
        receipt_fill_fn=lambda *_a: (19.9, 150000.0, 6),
        now_iso="2026-09-26T20:00:01+00:00", trace=trace, sleep_fn=lambda _s: None)
    assert result["status"] == "filled"
    row = conn.execute("SELECT entry_notional,quote_price,price_attribution FROM memecoin_positions").fetchone()
    assert row["entry_notional"] == 19.9 and row["quote_price"] is None
    assert row["price_attribution"] == "exact_event"
    benchmarks = [fields for name, fields in trace.events if name == "slot_model_benchmark"]
    if not diagnostic_failure:
        assert len(benchmarks) == 1 and benchmarks[0]["basis"] == "source_reserve_model"


def test_cached_receipt_and_frozen_source_logs_reach_postfill_trace(conn, monkeypatch):
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import MessageV0
    from sniper.execution import solana_real_seams as seams, pump_curve_buy as curve
    from sniper.services.memecoin_rabbit_signal import encode_trade_event

    kp, trace, reads = Keypair(), Trace(), []
    owner = str(kp.pubkey())
    event = curve.parse_trade_event(FRAMES["entry"]["trade_event_log"])
    event["user"] = owner
    own_log = encode_trade_event(event)
    mint = event["mint"]
    pid = m.reserve_pending(conn, mint=mint, source_user_id="wallet", strategy="alpha_b",
                            notional=50.0, reserved_at="2026-10-17T15:40:18Z", network_id="solana",
                            funding_asset="sol", funding_amount_atomic=500_000_000, funding_sol_usd=100.0)

    def cache(_get_tx, **_kwargs):
        def receipt(sig):
            reads.append(sig)
            return {"slot": 458089627, "transaction": {"signatures": [sig], "message": {
                "accountKeys": [owner, "coin-ata"], "header": {"numRequiredSignatures": 1}}},
                "meta": {"err": None, "fee": 100_000, "preBalances": [1_000_000_000, 0],
                         "postBalances": [516_194_850, 2_039_280], "preTokenBalances": [],
                         "postTokenBalances": [{"accountIndex": 1, "mint": mint, "owner": owner,
                             "uiTokenAmount": {"amount": str(event["token_amount"]), "decimals": 6}}],
                         "logMessages": [f"Program {curve.PUMP_PROGRAM} invoke [1]", own_log,
                                         f"Program {curve.PUMP_PROGRAM} success"]}}
        return receipt

    monkeypatch.setattr(seams, "successful_receipt_cache", cache)
    monkeypatch.setattr(slot, "execute_prebuilt_entry", lambda _conn, **kw: {"status": "filled", "sig": kw["sig"]})
    seam = seams.build_real_solana_executor(keypair=kp, rpc_url="http://unused")
    source = {"event_log": FRAMES["source"]["trade_event_log"], "reference_basis": "source_trade_event",
              "source_sig": FRAMES["source"]["signature"], "source_slot": 458089626}
    result = seam["slot_execute_fn"](
        conn, m.pending_row(conn, pid), MessageV0.try_compile(kp.pubkey(), [], [], Hash.default()),
        validity={"funding_asset": "sol", "funding_amount_atomic": 500_000_000,
                  "pump_source_frame": source}, now_iso="2026-10-17T15:40:19Z", trace=trace)
    assert result["status"] == "filled" and len(reads) == 1
    evidence = next(fields for name, fields in trace.events if name == "slot_curve_frames")
    assert evidence["source"] == source
    assert evidence["landing"]["event_log"] == own_log
    assert evidence["landing"]["basis"] == "confirmed_receipt_trade_event"
