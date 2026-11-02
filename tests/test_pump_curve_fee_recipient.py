"""RECEIPT 8bedf5e2: v3 source stores protocol fees and reports the zero recipient.

Our v1 buy must use a prefetched Global member; the system program cannot be writable.
"""
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from solders.pubkey import Pubkey

from sniper.execution import pump_curve_buy as P
from sniper.settings import memecoin as cfg

FIXTURES = Path(__file__).parent / "fixtures" / "memecoin"
FX = json.loads((FIXTURES / "pump_curve_fee_recipient_8bedf5e2.json").read_text())
QFX = json.loads((FIXTURES / "pump_quote_curve_frames.json").read_text())
QFRAME = next(f for f in QFX["frames"] if f["signature"].startswith("2Bha6MmU1s"))
QLOG = next(l for l in QFRAME["logs"] if l.startswith("Program data: ")
            and base64.b64decode(l.removeprefix("Program data: "))[:8] == P.TRADE_EVENT_DISCRIMINATOR)
SNAP = SimpleNamespace(blockhash="1" * 32, last_valid_height=1, sol_usd=110.0)


def _line(line=FX["trade_event_log"], *, recipient=P.SYSTEM_PROGRAM, mayhem=False):
    raw = bytearray(base64.b64decode(line.removeprefix("Program data: ")))
    raw[129:161] = bytes(Pubkey.from_string(recipient))  # TradeEvent.fee_recipient, IDL order
    name = P.parse_trade_event(line)["ix_name"].encode()
    raw[raw.index(name) + len(name)] = int(mayhem)
    return "Program data: " + base64.b64encode(raw).decode()


def _buy(line):
    event = P.parse_trade_event(line)
    return SimpleNamespace(mint=event["mint"], token_program=P.TOKEN_2022_PROGRAM,
                           trade_event_log=line, source_user_id="test")


def _leg(quote=False):
    leg = {"ixs": [], "tables": [], "usdc_atomic": 5_000_000,
           "min_out_lamports": 45_000_000, "out_lamports": 45_000_000,
           "fetched_at": 1000.0, "quote": {}}
    if quote:
        leg.update(min_out_lamports=5904, out_lamports=5904,
                   output_mint=P.parse_trade_event(QLOG)["quote_mint"])
    return leg


def _builder(*, cached=True, quote=False):
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        assert method == "getAccountInfo"
        assert params[0] == str(P._pda([b"global"], P.PUMP_PROGRAM))
        return {"value": copy.deepcopy(FX["global_account"])}

    b = P.SlotLaneCurveBuilder(user=FX["payer"], rpc=rpc,
                              jup_call=lambda *a, **k: pytest.fail("no build-time Jupiter"),
                              clock=lambda: 1000.0)
    if cached:
        b._read_global()
    else:
        b._buyback = FX["buyback_recipient"]
    b._leg = _leg(quote)
    if quote:
        event = P.parse_trade_event(QLOG)
        b._mints = {event["quote_mint"]: (1000.0, copy.deepcopy(QFX["quote_mint_account"])),
                    event["mint"]: (1000.0, copy.deepcopy(QFX["base_mint_account"]))}
    b.rpc = lambda *a, **k: pytest.fail("no build-time RPC")
    return b, calls


@pytest.fixture(autouse=True)
def _caps(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 0.0)


def _fee_account(message, quote=False):
    keys = message.account_keys
    ix = message.instructions[-1]
    index = ix.accounts[6 if quote else 1]
    return str(keys[index]), message.is_maybe_writable(index)


def test_exact_failed_receipt_has_readonly_system_alias():
    event = P.parse_trade_event(FX["trade_event_log"])
    assert event["fee_recipient"] == P.SYSTEM_PROGRAM and event["mayhem_mode"] is False
    assert FX["failed_instruction"]["accounts"][1] == FX["failed_instruction"]["accounts"][7] == P.SYSTEM_PROGRAM
    assert FX["failed_accounts"][0]["writable"] is False
    assert "account: fee_recipient" in FX["failed_logs"][0]


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("mayhem", [False, True])
def test_placeholder_build_uses_prefetched_mode_recipient_without_io(native, mayhem):
    b, calls = _builder()
    line = FX["trade_event_log"] if not mayhem else _line(mayhem=True)
    buy = _buy(line)
    original = buy.trade_event_log
    message, validity = b.build(buy, 5.0, SNAP, **({"funding_lamports": 45_000_000} if native else {}))
    assert message is not None, validity
    assert _fee_account(message) == (FX["mayhem_recipient" if mayhem else "normal_recipient"], True)
    assert buy.trade_event_log == original and P.parse_trade_event(original)["fee_recipient"] == P.SYSTEM_PROGRAM
    assert len(calls) == 1 and len(bytes(message)) < P.SOLANA_PACKET_LIMIT


@pytest.mark.parametrize("mayhem", [False, True])
def test_quote_build_and_packet_check_use_same_prefetched_recipient(monkeypatch, mayhem):
    b, calls = _builder(quote=True)
    buy, leg = _buy(_line(QLOG, mayhem=mayhem)), _leg(True)
    seen = []
    assemble = P._assemble

    def capture(user, ixs, *args):
        seen.append(str(ixs[-1].accounts[6].pubkey))
        return assemble(user, ixs, *args)

    monkeypatch.setattr(P, "_assemble", capture)
    assert b.ondemand_fits(buy, leg)
    quote_usd = 5.0 / (5904 / 1e8)
    message, validity = b.build(buy, 5.0, SNAP, leg=leg, quote=(8, quote_usd, 0, 0))
    assert message is not None, validity
    expected = FX["mayhem_recipient" if mayhem else "normal_recipient"]
    assert seen == [expected, expected] and _fee_account(message, True) == (expected, True)
    assert len(calls) == 1


@pytest.mark.parametrize("quote", [False, True])
def test_placeholder_without_cached_config_is_refused_before_signing(quote):
    b, _ = _builder(cached=False, quote=quote)
    buy = _buy(_line(QLOG) if quote else FX["trade_event_log"])
    args = {"leg": _leg(True), "quote": (8, 85_000.0, 0, 0)} if quote else {}
    assert b.build(buy, 5.0, SNAP, **args) == (None, "no_protocol_fee_recipient")
    if quote:
        assert not b.ondemand_fits(buy, _leg(True))


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("quote", [False, True])
def test_explicit_source_recipient_is_preserved_without_requiring_config(cached, quote):
    explicit = str(Pubkey.new_unique())
    b, _ = _builder(cached=cached, quote=quote)
    buy = _buy(_line(QLOG if quote else FX["trade_event_log"], recipient=explicit))
    args = {"leg": _leg(True), "quote": (8, 5.0 / (5904 / 1e8), 0, 0)} if quote else {}
    message, validity = b.build(buy, 5.0, SNAP, **args)
    assert message is not None, validity
    assert _fee_account(message, quote) == (explicit, True)


@pytest.mark.parametrize("funding", ["native", "usdc", "quote"])
def test_recipient_resolution_preserves_the_enabled_hard_copy_cap(monkeypatch, funding):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    quote = funding == "quote"
    b, _ = _builder(quote=quote)
    source = QLOG if quote else FX["trade_event_log"]
    placeholder = _buy(_line(source))
    explicit = _buy(_line(source, recipient=FX["normal_recipient"]))
    args = {"funding_lamports": 45_000_000} if funding == "native" else {}
    if quote:
        args.update(leg=_leg(True), quote=(8, 5.0 / (5904 / 1e8), 0, 0))
    messages, limits, amounts = [], [], []
    for buy in (placeholder, explicit):
        message, validity = b.build(buy, 5.0, SNAP, **args)
        assert message is not None, validity
        messages.append(message)
        limits.append(validity["pump_price_limit"])
        amounts.append({k: b.last_build[k] for k in (
            "spend_lamports", "expected_tokens_out", "output_floor_atomic", "source_price_floor_atomic",
            "min_tokens_out", "source_average_price_usd")})
    assert limits[0] == limits[1] and amounts[0] == amounts[1]
    assert limits[0]["source_average_price_usd"] > 0
    assert limits[0]["min_tokens_atomic"] >= P.source_price_floor_atomic(
        5_000_000, limits[0]["source_average_price_usd"], 35.0)
    assert bytes(messages[0].instructions[-1].data) == bytes(messages[1].instructions[-1].data)
    assert bytes(messages[0]) == bytes(messages[1])


@pytest.mark.parametrize("mode", [None, 0, 1, "false", "true"])
def test_placeholder_refuses_absent_or_nonboolean_mode(mode):
    b, _ = _builder()
    event = P.parse_trade_event(FX["trade_event_log"])
    if mode is None:
        del event["mayhem_mode"]
    else:
        event["mayhem_mode"] = mode
    with pytest.raises(ValueError, match="no_protocol_fee_recipient"):
        b._resolved_fee_event(event)


@pytest.mark.parametrize("quote", [False, True])
def test_low_level_accounts_refuse_the_zero_fee_recipient(quote):
    event = P.parse_trade_event(FX["trade_event_log"])
    args = dict(user=FX["payer"], mint=event["mint"], creator=event["creator"],
                fee_recipient=P.SYSTEM_PROGRAM, buyback_fee_recipient=FX["buyback_recipient"])
    with pytest.raises(ValueError, match="no_protocol_fee_recipient"):
        if quote:
            P.quote_curve_buy_accounts(**args, quote_mint=P.USDC_MINT,
                                      base_token_program=P.TOKEN_2022_PROGRAM, quote_token_program=P.TOKEN_PROGRAM)
        else:
            P.curve_buy_accounts(**args, token_program=P.TOKEN_2022_PROGRAM)


@pytest.mark.parametrize("damage", ["null", "wrong_owner", "executable", "discriminator", "short",
                                    "uninitialized", "malformed", "missing_data", "wrong_encoding", "non_account",
                                    "zero_normal", "zero_mayhem", "zero_buyback"])
@pytest.mark.parametrize("cached", [False, True])
def test_invalid_global_read_keeps_valid_recipient_snapshot(damage, cached):
    b, _ = _builder(cached=cached)
    original = b._fee_config
    account = copy.deepcopy(FX["global_account"])
    raw = bytearray(base64.b64decode(account["data"][0]))
    if damage == "null":
        account = None
    elif damage == "non_account":
        account = ["invalid account"]
    elif damage == "wrong_owner":
        account["owner"] = P.TOKEN_PROGRAM
    elif damage == "executable":
        account["executable"] = True
    elif damage == "malformed":
        account["data"] = ["bad base64!", "base64"]
    elif damage == "missing_data":
        del account["data"]
    elif damage == "wrong_encoding":
        account["data"][1] = "jsonParsed"
    else:
        if damage == "discriminator":
            raw[0] ^= 1
        elif damage == "short":
            raw = raw[:964]
        elif damage == "uninitialized":
            raw[8] = 0
        else:
            offset = {"zero_normal": 41, "zero_mayhem": 483, "zero_buyback": 933}[damage]
            raw[offset:offset + 32] = bytes(32)
        account["data"][0] = base64.b64encode(raw).decode()
    b.rpc = lambda *args: {"value": account}
    b._read_global()
    assert b._fee_config is original and b._buyback == FX["buyback_recipient"]
    message, validity = b.build(_buy(FX["trade_event_log"]), 5.0, SNAP)
    if not cached:
        assert message is None and validity == "no_protocol_fee_recipient"
        return
    assert message is not None, validity
    assert _fee_account(message) == (FX["normal_recipient"], True)


def test_rotation_publishes_complete_immutable_recipient_snapshot():
    b, _ = _builder()
    before = b._fee_config
    account = copy.deepcopy(FX["global_account"])
    raw = bytearray(base64.b64decode(account["data"][0]))
    rotated = tuple(str(Pubkey.new_unique()) for _ in range(3))
    for offset, recipient in zip((41, 483, 933), rotated):
        raw[offset:offset + 32] = bytes(Pubkey.from_string(recipient))
    account["data"][0] = base64.b64encode(raw).decode()
    b.rpc = lambda *args: {"value": account}
    b._read_global()
    assert isinstance(b._fee_config, tuple) and b._fee_config == rotated
    assert before == (FX["normal_recipient"], FX["mayhem_recipient"], FX["buyback_recipient"])
    for mayhem in (False, True):
        message, validity = b.build(_buy(_line(mayhem=mayhem)), 5.0, SNAP)
        assert message is not None, validity
        assert _fee_account(message) == (rotated[int(mayhem)], True)
