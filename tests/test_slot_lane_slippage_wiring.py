"""The cached impossibility check uses the actual build and never reaches the signer on refusal."""
import json
import struct
from types import SimpleNamespace

import pytest

import test_memecoin_slot_lane as TL
import test_pump_curve_buy as TC
import test_pumpswap_buy as TA
from test_memecoin_slot_lane import conn
from sniper.execution import pump_curve_buy as C, pumpswap_buy as A


@pytest.mark.parametrize("fixture,program,venue", [(TC, C.PUMP_PROGRAM, "pump_curve"),
                                                (TA, A.AMM_PROGRAM, "pump_amm")])
@pytest.mark.parametrize("native", [False, True])
def test_slippage_metadata_matches_the_compiled_buy(fixture, program, venue, native, monkeypatch):
    monkeypatch.setattr(TL.lane.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    builder = fixture._builder(fixture._Clock())
    buy = fixture._buy(signature="source-sig", slot=100)
    message, detail = builder.build(buy, 5.0, fixture.SNAP,
                                    **({"funding_lamports": 28_000_000} if native else {}))
    assert message is not None, detail
    instruction = next(ix for ix in message.instructions
                       if str(message.account_keys[ix.program_id_index]) == program)
    spend, minimum = struct.unpack_from("<QQ", bytes(instruction.data), 8)
    proof = detail["entry_slippage"]
    assert proof["spend_atomic"] == spend
    assert proof["min_tokens_atomic"] == minimum
    assert proof["venue"] == venue and proof["program"] == program
    assert proof["mint"] == detail["target_mint"]
    assert proof["source_sig"] == buy.signature and proof["source_slot"] == buy.slot
    assert proof["quote_mint"] == C.WSOL_MINT
    if venue == "pump_curve":
        assert proof["instruction_discriminator"] == bytes(instruction.data)[:8].hex()
        assert proof["instruction_discriminator"] == C.BUY_EXACT_SOL_IN_DISCRIMINATOR.hex()
    if venue == "pump_amm":
        assert proof["pool"] == fixture._event()["pool"]
        assert proof["pool_facts"]["base_mint"] == proof["mint"]
        assert proof["pool_facts"]["quote_mint"] == proof["quote_mint"]


@pytest.mark.parametrize("state", ["refuse", "unknown", "pass", "throws"])
def test_live_lane_uses_cached_guard_before_the_executor(conn, tmp_path, monkeypatch, state):
    lane, sent = TL._live_lane(conn, tmp_path, monkeypatch)
    checked = []

    def check(buy, validity):
        checked.append((buy.signature, validity))
        if state == "throws":
            raise ValueError("unavailable cached state")
        return {"state": state, "reason": "cached_price_beyond_limit",
                "basis": "fee_free_upper_bound", "max_tokens_atomic": 9,
                "min_tokens_atomic": 10}

    lane.rabbit_backstop = SimpleNamespace(slippage_guard=check)
    result = lane.handle(TL._buy())
    assert len(checked) == 1
    pending = conn.execute("SELECT * FROM memecoin_pending_entries").fetchone()
    assert checked[0][1]["entry_sol_budget"]["pending_id"] == pending["id"]
    assert checked[0][1]["entry_rent_lamports"] == 6_117_840
    if state == "refuse":
        assert result["status"] == "aborted" and result["reason"] == "cached_price_beyond_limit"
        assert sent == [] and pending["status"] != "reserved" and pending["tx_sig"] is None
    else:
        assert result["status"] == "filled" and sent == [1]
    events = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])[
        "substage_events"]
    assert any(e["name"] == "slot_slippage_check" for e in events)


def test_market_refusal_is_final_policy_before_signing(conn, tmp_path, monkeypatch):
    lane, sent = TL._live_lane(conn, tmp_path, monkeypatch)
    monkeypatch.setattr(TL.lane.cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)
    lane.rabbit_backstop = SimpleNamespace(slippage_guard=lambda *_: {
        "state": "refuse", "reason": "intervening_market_cap",
        "market_movement": {"state": "refuse", "movement_pct": 36, "cap_pct": 35,
                            "basis": "intervening_market"}})
    result = lane.handle(TL._buy())
    assert result["reason"] == "intervening_market_cap" and sent == []
    assert TL.lane.policy_refusal(result["reason"])
    pending = conn.execute("SELECT * FROM memecoin_pending_entries").fetchone()
    assert pending["status"] != "reserved" and pending["tx_sig"] is None
