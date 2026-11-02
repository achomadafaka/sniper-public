"""Desk U safety evidence: real mint/curve/FeeConfig accounts (2026-09-28 fixture), synthetic holders."""
import base64
import json
import struct
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_safety as S

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_fee_config_frames.json").read_text())
MINT = "14YMUuZfdm8uUTtbJbMbszxme2M2xHbnteSZqUkRpump"
LIVE_CURVE = FIX["curve_accounts_b64"]["4Px7VL8zi721aTYhd7osQKL2EYaVML8BW9Ak1p48scHR"]
DONE_CURVE = FIX["curve_accounts_b64"]["4YC6gwi6Gmw7JqrjgUGwyDKg7PpDWy1oaPKPcJuppump"]
CREATOR = "5B2ZqdaizTPPNHuyKqCSVxqjwznFCTs7LRubGpn4VdFL"
SUPPLY = 993447947628303


def acc(b64, owner):
    return {"data": [b64, "base64"], "owner": owner}


def tok(mint, owner, amount):
    from solders.pubkey import Pubkey
    raw = bytes(Pubkey.from_string(mint)) + bytes(Pubkey.from_string(owner)) + struct.pack("<Q", amount) + bytes(93)
    return acc(base64.b64encode(raw).decode(), S.TOKEN_2022_PROGRAM)


def owner(i):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(bytes([i]) * 32))


def cand(slot=1000):
    return {"mint": MINT, "snapshot": {"fields": {"mcap_usd": {"slot": slot}}}}


def run(*, curve=LIVE_CURVE, mint_acc=None, holders=None, ctx_slot=1010, slot=1000, sol_usd=180.0):
    holders = holders if holders is not None else [
        (S.curve_address(MINT), SUPPLY // 2), (CREATOR, SUPPLY // 50), (owner(3), SUPPLY // 100)]
    largest = [{"address": owner(100 + i)} for i in range(len(holders))]
    accounts = [mint_acc or acc(FIX["mint_accounts_b64"][MINT], S.TOKEN_2022_PROGRAM),
                acc(curve, S.PUMP_PROGRAM), acc(FIX["fee_config_curve"]["data_b64"], S.FEE_PROGRAM)]
    accounts += [h if isinstance(h, dict) or h is None else tok(MINT, *h) for h in holders]
    return S.evaluate(cand(slot), largest=largest, accounts=accounts, ctx_slot=ctx_slot, sol_usd=sol_usd)


def test_live_curve_passes_all_four_with_curve_math_probe():
    checks, meta = run()
    assert {k: v["status"] for k, v in checks.items()} == dict.fromkeys(
        ("token_safety", "market_data", "sell_probe", "holders"), "pass")
    probe = checks["sell_probe"]
    assert probe["reason"] == "curve_math" and probe["exact_quantity"] is True
    assert 2.4 < probe["round_trip_loss_pct"] < 2.7             # 2 x (95 + 30) bps plus tiny impact
    assert meta["fee_bps"] == [95, 30] and meta["slot_lag"] == 10 and meta["tokens_out"] > 0
    assert meta["mayhem"] is False
    h = checks["holders"]
    assert h["exclusions_verified"] is True and h["creator_pct"] == pytest.approx(2.0, abs=0.01)
    assert h["top10_pct"] == pytest.approx(3.0, abs=0.01)       # the curve's own half is excluded


@pytest.mark.parametrize("kw,reason", [({"curve": DONE_CURVE}, "curve_complete"),
                                       ({"ctx_slot": 1026}, "reserves_stale"),
                                       ({"ctx_slot": 990}, "reserves_stale"),
                                       ({"sol_usd": None}, "sol_usd_missing")])
def test_probe_is_never_a_pass_on_stale_complete_or_unpriced(kw, reason):
    probe = run(**kw)[0]["sell_probe"]
    assert probe == {"status": "unknown", "reason": reason}


def test_open_authority_and_unknown_extension():
    raw = bytearray(base64.b64decode(FIX["mint_accounts_b64"][MINT]))
    raw[0] = 1                                                   # mint authority COption = Some
    assert run(mint_acc=acc(base64.b64encode(bytes(raw)).decode(), S.TOKEN_2022_PROGRAM))[0][
        "token_safety"]["authority_mint"] == "open"
    raw = bytearray(base64.b64decode(FIX["mint_accounts_b64"][MINT]))
    struct.pack_into("<H", raw, 166, 12)                         # first extension -> PermanentDelegate
    assert run(mint_acc=acc(base64.b64encode(bytes(raw)).decode(), S.TOKEN_2022_PROGRAM))[0][
        "token_safety"] == {"status": "unknown", "reason": "extension_12"}


def test_unresolved_holder_account_is_not_verified_and_rpc_failure_is_unknown():
    checks = run(holders=[(CREATOR, SUPPLY // 50), None])[0]
    assert checks["holders"]["exclusions_verified"] is False
    failed = S.evaluate(cand(), largest=None, accounts=None, ctx_slot=None, sol_usd=180.0)[0]
    assert {v["status"] for v in failed.values()} == {"unknown"}


@pytest.mark.parametrize("cap,status", [(None, "pass"), (0.5, "pass"), (5.0, "pass")])
def test_creator_outside_a_full_top20_is_unknown_never_a_kill(cap, status):
    holders = [(owner(10 + i), SUPPLY // 100 - i) for i in range(20)]
    largest = [{"address": owner(100 + i)} for i in range(20)]
    accounts = [acc(FIX["mint_accounts_b64"][MINT], S.TOKEN_2022_PROGRAM), acc(LIVE_CURVE, S.PUMP_PROGRAM),
                acc(FIX["fee_config_curve"]["data_b64"], S.FEE_PROGRAM)] + [tok(MINT, *h) for h in holders]
    checks, meta = S.evaluate(cand(), largest=largest, accounts=accounts, ctx_slot=1010, sol_usd=180.0,
                              creator_pct_max=cap)
    assert checks["holders"]["status"] == status and meta["creator_pct_basis"] == "below_page"
    assert checks["holders"]["creator_pct"] is None
    assert checks["holders"]["reason"] == "creator_unresolved_below_page"
    assert "creator_pct_bound" not in meta
    assert checks["holders"]["creator_basis"] == "below_page"


TRADE = {"slot": 990, "virtual_quote": 55123429969, "virtual_token": 583962211444407,
         "real_token": 304062211444407, "fee_bps": 95,
         "creator_fee_bps": 30}


def tape(**kw):
    args = dict(head_slot=1000, head_known_at=100.0, heartbeat_age_s=1.0, gap_last_slots=[], graduated=False, mayhem=False,
                sol_usd=180.0, evaluated_at=104.0)
    args.update(kw)
    return S.probe_from_tape(kw.pop("trade", TRADE), **{k: v for k, v in args.items() if k != "trade"})


def test_tape_probe_matches_the_rpc_curve_probe_on_the_same_reserves():
    check, ev = tape()
    rpc = run()[0]["sell_probe"]
    assert check == rpc and ev["reserves_slot"] == 1000 and ev["trade_slot"] == 990 and ev["fee_bps"] == [95, 30]


@pytest.mark.parametrize("kw,reason", [
    ({"heartbeat_age_s": 11.0}, "tape_not_current"), ({"heartbeat_age_s": None}, "tape_not_current"),
    ({"evaluated_at": 110.5}, "tape_not_current"), ({"gap_last_slots": [995]}, "tape_not_current"),
    ({"graduated": True}, "curve_complete"), ({"trade": None}, "no_trade_state"),
    ({"trade": {**TRADE, "fee_bps": None}}, "fee_unknown"), ({"mayhem": True}, "fee_unknown"),
    ({"mayhem": None}, "fee_unknown"),
    ({"trade": {**TRADE, "real_token": TRADE["real_token"] - 1}}, "reserves_mismatch"),
    ({"trade": {**TRADE, "real_token": None}}, "reserves_unreadable"), ({"sol_usd": 0}, "sol_usd_missing")])
def test_tape_probe_is_never_a_pass_off_current_state(kw, reason):
    assert tape(**kw)[0] == {"status": "unknown", "reason": reason}


def test_a_gap_before_the_trade_does_not_block():
    assert tape(gap_last_slots=[900])[0]["status"] == "pass"


def emission(at=1000.0):
    from datetime import datetime, timezone
    from sniper.services import memecoin_desk_universe as U
    snap = U.build_snapshot(emission_at=at, fields={"mcap_usd": U.Field(41000.0, "pumpfun_grpc:curve_reserves", at, 7,
                                                                         "complete")})
    sig = "5" * 88
    return {"schema": "desk-candidate/v1", "producer": "U", "network_id": "solana", "mint": MINT,
            "pool": S.curve_address(MINT), "band_entry_slot": 7, "trigger_signature": sig, "trigger_ordinal": 0,
            "ticket_id": U.ticket_id("solana", MINT, 7, sig, 0), "gate_state": None,
            "emitted_at": datetime.fromtimestamp(at, timezone.utc).isoformat(), "snapshot": snap}


def test_genuine_fresh_emission_passes():
    assert S.verify_u_emission(emission(), now=1005.0, max_age_s=60)["ticket_id"].startswith("desk:U:")


@pytest.mark.parametrize("change,reason", [
    (lambda c: c.update(ticket_id="desk:U:" + "0" * 24), "emission_ticket_id"),
    (lambda c: c["snapshot"].update(hash="0" * 64), "emission_snapshot_hash"),
    (lambda c: c["snapshot"]["fields"]["mcap_usd"].update(value=99000.0), "emission_snapshot_hash"),
    (lambda c: c.update(schema="desk-candidate/v0"), "emission_schema"),
    (lambda c: c.update(pool=MINT), "emission_pool"),
    (lambda c: c.pop("snapshot"), "emission_shape")])
def test_forged_or_malformed_emission_is_refused(change, reason):
    c = emission()
    change(c)
    with pytest.raises(ValueError, match=reason):
        S.verify_u_emission(c, now=1005.0, max_age_s=60)


@pytest.mark.parametrize("now", [1061.0, 999.0, 1e12])
def test_stale_or_future_emission_is_refused(now):
    with pytest.raises(ValueError, match="emission_stale"):
        S.verify_u_emission(emission(), now=now, max_age_s=60)
