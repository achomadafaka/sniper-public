"""Pump curves quoted in a token other than SOL go through the slot lane (operator "Fix" on
probablyinsider DVypDG8A 43624d3d and STC BiR7qY3f 2dab63c2, 2026-10-07, via Alpha).

DVypDG8A is a pump.fun bonding curve quoted in WBTC (Portal 3NZ9JMVB, 8 decimals). Its TradeEvent
carries quote_mint / quote_amount / virtual_quote_reserves with every SOL field zero, so the lane
refused slot_lane_price_unreadable at 0 ms; 13 coins and 38 refusals since 09-26, a different quote
token almost every time. The buy is pump's buy_exact_quote_in_v2 (27 accounts, on-chain IDL read
2026-10-07). With "pump_curve" on MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES the lane prices it from the
event's quote fields and the on-demand leg's quote USD, and builds leg + coin ATA + buy in one
transaction. Off the list: the refusal names the quote mint.

Frames are probablyinsider's four real buys (tests/fixtures/memecoin/pump_quote_curve_frames.json:
logs, the pump instruction's data and accounts, both mint accounts), read on the research key.
"""
import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as P
from sniper.services import memecoin_slot_lane as L
from sniper.services import memecoin_source_initiation as SI

FX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pump_quote_curve_frames.json").read_text())
FRAMES = {f["signature"][:10]: f for f in FX["frames"]}
EXACT_IN = FRAMES["2Bha6MmU1s"]            # buy_exact_quote_in (Jupiter)
TOKENS_OUT = FRAMES["2wdkTgtNTx"]          # buy (exact tokens, DFlow), the $500 buy
WBTC = "3NZ9JMVBmGAqocybic2c7LQCJScmgsAZ6vQqTDzcqmJh"
MINT = "DVypDG8AjMo8Jv8m9PAwe1TPMki4xaiHyGYN7269aFZH"
OWNER = "7XUAo3ugnHFtUgUamYB2rxhXPDCgz8zSbTausvriAs3p"
USER = "3EKhb6kyrKQSzzYgR3oHVBtGwHKo6YzDxSjgEz2Bfpg6"
BLOCKHASH = "11111111111111111111111111111111"
SNAP = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=150.0)
BTC_USD = 85_600.0


def _event_line(frame):
    for line in frame["logs"]:
        if line.startswith("Program data: "):
            try:
                P.parse_trade_event(line)
                return line
            except ValueError:
                continue
    raise AssertionError("no TradeEvent")


def _event(frame):
    return P.parse_trade_event(_event_line(frame))


@pytest.fixture
def curve_listed(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset({"pump_curve"}),
                        raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SOLANA_SLOT_LANE_MODE", "live")


def _slot_buy(frame, **over):
    from sniper.services.memecoin_slot_signal import SlotBuy
    ev = _event(frame)
    fields = dict(venue="pump_curve", source_user_id="u1", handle="probablyinsider", owner=OWNER,
                  signature=frame["signature"], slot=frame["slot"], mint=MINT, pool=None,
                  sol_amount=0, token_amount=ev["token_amount"], virtual_sol_reserves=0,
                  virtual_token_reserves=ev["virtual_token_reserves"], real_sol_reserves=0,
                  real_token_reserves=ev["real_token_reserves"],
                  token_program=P.token_program_from_logs(frame["logs"]),
                  trade_event_log=_event_line(frame), recv_monotonic=time.monotonic(), recv_utc="t")
    fields.update(over)
    return SlotBuy(**fields)


# --- the instruction, against the real frames -----------------------------------------------------

@pytest.mark.parametrize("frame", FX["frames"], ids=lambda f: f["signature"][:10])
def test_the_27_accounts_match_every_real_buy(frame):
    ev, accounts = _event(frame), frame["pump_ix_accounts"]
    metas = P.quote_curve_buy_accounts(
        user=OWNER, mint=MINT, quote_mint=WBTC, base_token_program=P.TOKEN_2022_PROGRAM,
        quote_token_program=P.TOKEN_PROGRAM, creator=ev["creator"], fee_recipient=ev["fee_recipient"],
        buyback_fee_recipient=accounts[8])
    assert [str(k) for k, _s, _w in metas] == accounts
    signers = [str(k) for k, s, _w in metas if s]
    assert signers == [OWNER] and OWNER in frame["signers"]       # the user signs the real tx too


def test_the_exact_quote_in_output_is_exact_on_the_real_buy():
    """Fees off the spend, ONE ATOM LESS priced, as the SOL curve (2Bha6MmU1s: 118,090 WBTC atoms
    -> 3,126,491,284,716 tokens against its own pre-trade reserves)."""
    ev = _event(EXACT_IN)
    pre = dict(ev, virtual_quote_reserves=ev["virtual_quote_reserves"] - ev["quote_amount"],
               virtual_token_reserves=ev["virtual_token_reserves"] + ev["token_amount"],
               real_token_reserves=ev["real_token_reserves"] + ev["token_amount"])
    assert P.quote_curve_tokens_out(118_090, pre) == ev["token_amount"] == 3_126_491_284_716
    data = bytes.fromhex(EXACT_IN["pump_ix_data_hex"])
    assert data[:8] == P.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR
    ours = P.buy_exact_quote_in_v2_data(118_090, 1)
    assert ours[:24] == data[:24] and len(ours) == 24           # the IDL's two u64 args


# --- admission ----------------------------------------------------------------------------------

def test_off_the_list_the_refusal_names_the_quote_mint(monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_EXOTIC_LIVE_VENUES", frozenset(), raising=False)
    reason, facts = L.curve_market_facts(_slot_buy(TOKENS_OUT), 150.0)
    assert reason == "slot_lane_curve_quote_not_sol" and facts["quote_mint"] == WBTC
    line = L.result_log_line(_slot_buy(TOKENS_OUT), {"status": "rejected", "reason": reason, **facts})
    assert "quote=3NZ9JMVB" in line


def test_listed_it_goes_to_the_worker_then_prices_with_the_legs_quote(curve_listed):
    buy = _slot_buy(TOKENS_OUT)
    assert L.curve_market_facts(buy, 150.0)[0] == "slot_lane_curve_quote_not_supported"
    assert "slot_lane_curve_quote_not_supported" in L.ONDEMAND_QUOTE_REASONS
    reason, facts = L.curve_market_facts(buy, 150.0, quote=(8, BTC_USD))
    assert reason is None, reason
    ev = _event(TOKENS_OUT)
    price = ev["virtual_quote_reserves"] / 1e8 / (ev["virtual_token_reserves"] / 1e6) * BTC_USD
    assert facts["price_usd"] == pytest.approx(price) and facts["quote_mint"] == WBTC
    gross = ev["quote_amount"] + ev["fee"] + ev["creator_fee"]
    assert facts["their_usd"] == pytest.approx(gross / 1e8 * BTC_USD)    # ~ the $500 the wallet paid
    assert facts["mcap_usd"] == pytest.approx(price * L.PUMP_TOTAL_SUPPLY)


def test_a_sol_curve_is_unchanged(curve_listed):
    """A SOL-quoted curve never parses its event on the lane for this: its price is its own."""
    buy = _slot_buy(TOKENS_OUT, virtual_sol_reserves=30 * 10**9, sol_amount=10**9,
                    virtual_token_reserves=10**15)
    reason, facts = L.curve_market_facts(buy, 150.0)
    assert reason is None and facts["price_usd"] == pytest.approx(30 / 1e9 * 150.0)
    assert "quote_mint" not in facts


@pytest.mark.parametrize("field", ["virtual_quote_reserves", "quote_amount"])
def test_a_zero_quote_field_refuses(curve_listed, field):
    ev = _event(TOKENS_OUT)
    reason, _facts = L.quote_curve_facts(dict(ev, **{field: 0}), quote=(8, BTC_USD))
    assert reason == "slot_lane_price_unreadable"


# --- the source's proof -------------------------------------------------------------------------

def test_the_proof_binds_the_signing_owner_and_the_quote_paid():
    ev = _event(TOKENS_OUT)
    proof = SI.solana_program_initiation(_slot_buy(TOKENS_OUT), MINT)
    assert proof is not None and proof["payer"] == OWNER
    assert proof["payment_atomic"] == str(ev["quote_amount"])
    assert proof["acquired_atomic"] == str(ev["token_amount"])


def test_an_off_curve_user_is_no_proof():
    """pump's `user` is a Signer: a wallet (on-curve) named there signed the transaction. A PDA user
    could only have signed by a program's invoke_signed, never the copied wallet."""
    from solders.pubkey import Pubkey
    pda = str(Pubkey.find_program_address([b"x"], Pubkey.from_string(P.PUMP_PROGRAM))[0])
    line = _event_line(TOKENS_OUT)
    raw = bytearray(base64.b64decode(line.split("Program data: ", 1)[1]))
    ev = _event(TOKENS_OUT)
    i = raw.find(bytes(Pubkey.from_string(ev["user"])))
    raw[i:i + 32] = bytes(Pubkey.from_string(pda))
    forged = "Program data: " + base64.b64encode(bytes(raw)).decode()
    assert SI.solana_program_initiation(_slot_buy(TOKENS_OUT, owner=pda, trade_event_log=forged), MINT) is None


# --- the builder --------------------------------------------------------------------------------

def _mint_rpc(calls):
    accounts = {WBTC: FX["quote_mint_account"], MINT: FX["base_mint_account"]}

    def rpc(method, params):
        calls.append(method)
        assert method == "getMultipleAccounts" and params[1]["encoding"] == "jsonParsed"
        return {"value": [accounts.get(a) for a in params[0]]}
    return rpc


def _builder(calls=None):
    b = P.SlotLaneCurveBuilder(user=USER, jup_call=lambda *a, **k: pytest.fail("no SOL leg here"),
                               rpc=_mint_rpc(calls if calls is not None else []), clock=lambda: 50.0)
    b._buyback = "11111111111111111111111111111112"
    return b


def _leg(spend_q=118_090, usdc=100_000_000):
    """An on-demand USDC -> WBTC leg as the worker hands it back (no instructions: the builder's
    own part is what is under test)."""
    return {"ixs": [], "tables": [], "usdc_atomic": usdc, "min_out_lamports": spend_q,
            "out_lamports": spend_q + 50, "fetched_at": 50.0, "quote": {}, "output_mint": WBTC}


def test_ondemand_quote_reads_both_mints_once_and_keeps_the_coins_program():
    calls = []
    b = _builder(calls)
    mint, quote, refusal = b.ondemand_quote(_slot_buy(TOKENS_OUT))
    assert (mint, quote, refusal) == (WBTC, (8, 0, 0), None)
    assert b.ondemand_quote(_slot_buy(EXACT_IN))[2] is None
    assert calls == ["getMultipleAccounts"]                          # cached
    assert b.coin_facts(MINT)["token_program"] == P.TOKEN_2022_PROGRAM
    snap = b.ondemand_facts(_slot_buy(TOKENS_OUT))
    assert snap["mint_values"][WBTC] == FX["quote_mint_account"] and snap["quote_read_at"] == 50.0


def test_a_fee_bearing_coin_refuses():
    bad = json.loads(json.dumps(FX["base_mint_account"]))
    bad["data"]["parsed"]["info"]["extensions"].append({"extension": "transferFeeConfig", "state": {
        "newerTransferFee": {"transferFeeBasisPoints": 100, "maximumFee": 1, "epoch": 0},
        "olderTransferFee": {"transferFeeBasisPoints": 100, "maximumFee": 1, "epoch": 0}}})
    b = _builder()
    b.rpc = lambda method, params: {"value": [FX["quote_mint_account"], bad]}
    assert b.ondemand_quote(_slot_buy(TOKENS_OUT))[2] == "slot_lane_curve_coin_unmovable"


def test_build_signs_leg_ata_and_buy_exact_quote_in_v2_under_the_caps(curve_listed, monkeypatch):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    usd = 100.0 / (118_090 / 1e8)                     # the leg's own rate: $100 for 118,090 atoms
    message, validity = b.build(buy, 100.0, SNAP, leg=_leg(), quote=(8, usd, 0, 0))
    assert message is not None, validity
    keys = [str(k) for k in message.account_keys]
    programs = [keys[ix.program_id_index] for ix in message.instructions]
    assert programs[-2:] == [P.ATA_PROGRAM, P.PUMP_PROGRAM]
    buy_ix = message.instructions[-1]
    data = bytes(buy_ix.data)
    assert data[:8] == P.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR
    spend, min_out = int.from_bytes(data[8:16], "little"), int.from_bytes(data[16:24], "little")
    assert spend == 118_090
    ev = _event(EXACT_IN)
    expected = P.quote_curve_tokens_out(spend, ev)
    assert 0 < min_out <= expected and min_out == validity["min_tokens_out"]
    limit = validity["pump_price_limit"]
    assert limit["source_average_price_usd"] == pytest.approx(
        (ev["quote_amount"] + ev["fee"] + ev["creator_fee"]) / 1e8 * usd / (ev["token_amount"] / 1e6))
    assert validity["target_mint"] == MINT and validity["quote_mint"] == WBTC


@pytest.mark.parametrize("quote_program", [P.TOKEN_PROGRAM, P.TOKEN_2022_PROGRAM])
def test_quote_curve_guard_witness_matches_the_signed_quote_units_and_floor(
        curve_listed, monkeypatch, quote_program):
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    observed_at, mint_account = b._mint_value(WBTC)
    mint_account = dict(mint_account, owner=quote_program)
    mint_value = b._mint_value
    monkeypatch.setattr(b, "_mint_value", lambda mint: (observed_at, mint_account)
                        if mint == WBTC else mint_value(mint))
    b.rpc = lambda *_: pytest.fail("guard metadata must use the existing mint cache")
    usd = 100.0 / (118_090 / 1e8)
    message, validity = b.build(buy, 100.0, SNAP, leg=_leg(), quote=(8, usd, 0, 0))
    assert message is not None, validity
    keys = [str(key) for key in message.account_keys]
    ix = message.instructions[-1]
    spend, floor = int.from_bytes(ix.data[8:16], "little"), int.from_bytes(ix.data[16:24], "little")
    signed = validity["entry_slippage"]
    assert signed == dict(venue="pump_curve", program=P.PUMP_PROGRAM, mint=MINT,
                          pool=P.curve_route_target(MINT), quote_mint=WBTC,
                          base_token_program=P.TOKEN_2022_PROGRAM, quote_token_program=quote_program,
                          instruction_discriminator=bytes(ix.data[:8]).hex(),
                          spend_atomic=spend, min_tokens_atomic=floor,
                          source_sig=buy.signature, source_slot=buy.slot)
    assert spend == _leg()["min_out_lamports"] != _leg()["usdc_atomic"]
    assert floor == validity["min_tokens_out"]
    assert [keys[index] for index in ix.accounts][1:5] == [
        MINT, WBTC, signed["base_token_program"], quote_program]


@pytest.mark.parametrize("over, why", [
    (dict(quote=(6, 1.0, 0, 0)), "quote_decimals_mismatch"),
    (dict(leg=None), "exotic_leg_missing"),
])
def test_build_refuses_a_wrong_or_missing_leg(curve_listed, over, why):
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    b.ondemand_quote(buy)
    kw = dict(leg=_leg(), quote=(8, 85_000.0, 0, 0))
    kw.update(over)
    assert b.build(buy, 100.0, SNAP, **kw) == (None, why)


def test_build_without_the_coins_program_refuses(curve_listed):
    b = _builder()
    assert b.build(_slot_buy(EXACT_IN), 100.0, SNAP, leg=_leg(), quote=(8, 85_000.0, 0, 0)) \
        == (None, "token_program_unknown")


def test_the_hard_average_cap_refuses_an_overpriced_leg(curve_listed, monkeypatch):
    """A leg that bought the quote 50% dear makes our unit price 50% over the source's average."""
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    b.ondemand_quote(buy)
    usd = 100.0 / (118_090 / 1e8)
    message, why = b.build(buy, 150.0, SNAP, leg=_leg(usdc=150_000_000), quote=(8, usd, 0, 0))
    assert message is None and "refuses" in why


# --- the worker ---------------------------------------------------------------------------------

def test_the_worker_serves_the_curve_builder_without_a_pool_cache(curve_listed):
    from sniper.execution.raydium_launchlab_buy import OnDemandLegWorker
    b = _builder()
    requeued, refused = [], []
    quote = {"outAmount": "118140", "otherAmountThreshold": "118090", "priceImpactPct": "0.001"}

    def jup(method, path, body=None, *, http=None):
        return (200, quote) if method == "GET" else (200, {"swapInstruction": {
            "programId": P.PUMP_PROGRAM, "accounts": [], "data": ""}, "addressLookupTableAddresses": []})
    import sniper.execution.pump_curve_buy as pcb
    w = OnDemandLegWorker(builders={"pump_curve": b}, admission_fn=lambda buy, q: None,
                          snapshot_fn=lambda: SNAP, clip_fn=lambda: 100.0, offer_fn=lambda b_, r: None,
                          start=False, clock=SimpleNamespace(monotonic=lambda: 60.0),   # = builder clock + 10 s
                          live_fn=lambda venue: True, requeue_fn=requeued.append,
                          refuse_fn=lambda b_, r: refused.append(r))
    orig = pcb._funded_jup_call
    pcb._funded_jup_call = jup
    try:
        result = w.process(_slot_buy(TOKENS_OUT))
    finally:
        pcb._funded_jup_call = orig
    assert result["status"] == "requeued", result
    assert refused == [] and requeued[0].ondemand["quote_mint"] == WBTC
    assert requeued[0].ondemand["quote"][0] == 8


# --- the lane, end to end on a requeued buy ------------------------------------------------------

def test_a_requeued_quote_curve_buy_fills_on_the_live_path_under_its_exotic_cap(
        curve_listed, monkeypatch, tmp_path):
    import contextlib
    import dataclasses
    import sqlite3
    from sniper.execution import exec_lock
    from sniper.persistence import db_memecoin as m
    monkeypatch.setattr(SI, "source_initiation_refusal", lambda *_: None)    # proof: tests above
    monkeypatch.setattr(exec_lock, "_lock_path", lambda key, lock_dir=None: str(tmp_path / ("%s.lock" % key)))
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(L.cfg, "MEMECOIN_FUNDED_REGIME_RISK_SCALE", 1.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_SLOT_LANE_VENUE_LIVE_MAX_PER_HOUR_BY_VENUE", {}, raising=False)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0.0)
    monkeypatch.setattr("sniper.services.memecoin_wallet_caps.entry_cap_usd", lambda *a, **k: None)
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None                    # the worker's read, same builder
    now = time.monotonic()
    usd = 10.0 / (11_809 / 1e8)
    buy = dataclasses.replace(buy, recv_monotonic=now, ondemand={
        "leg": _leg(spend_q=11_809, usdc=10_000_000), "quote": (8, usd, 0, 0), "quote_mint": WBTC})
    b.clock = lambda: 50.0
    sent = {}

    def execute(conn_, pending, message, *, validity, now_iso, trace, sol_usd=None, boundary_fn=None,
                entry_min_slot=None, entry_max_slot=None, entry_head_fn):
        # PR #486 (2026-10-20): automatic Solana entries carry no source-slot window; both bounds are None.
        assert (entry_min_slot, entry_max_slot) == (None, None)
        assert callable(entry_head_fn)
        sent.update(validity=validity, message=message)
        return {"status": "filled", "position_id": 1, "sig": "TXQ", "quote_residue_atomic": 5}
    c = sqlite3.connect(tmp_path / "ledger.db")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.commit()
    snap = L.PrefetchSnapshot(sol_usd=150.0, usdc_balance=400.0, sol_balance=0.5, blockhash=BLOCKHASH,
                              last_valid_height=1, observed_mono={"sol_usd": now - 1, "balances": now - 1,
                                                                  "blockhash": now - 1})
    legs = L.PendingLegs()
    legs.add(buy, deadline=now + L.MAX_SIGNAL_AGE_S)
    lane = L.SlotLane(conn=c, executor={"slot_execute_fn": execute},
                      build_fn=L.dispatch_build({"pump_curve": b}), snapshot_fn=lambda: snap,
                      armed_ids_fn=lambda: {"u1"}, kill_tripped_fn=lambda: False,
                      now_iso_fn=lambda: "2026-10-07T02:45:00+00:00", lock_key="sol-test", mode="live",
                      pending_legs=legs, shadow_clip_fn=lambda: 10.0,
                      clock=SimpleNamespace(monotonic=lambda: now, perf_counter=time.perf_counter))
    with contextlib.closing(c):
        result = lane.handle(buy)
        assert result["status"] == "filled", result
        assert lane._venue_live_sends["pump_curve:exotic"] and not lane._venue_live_sends.get("pump_curve")
        assert sent["validity"]["quote_mint"] == WBTC and sent["validity"]["residue_quote_mint"] == WBTC
        keys = [str(k) for k in sent["message"].account_keys]
        ix = sent["message"].instructions[-1]
        assert keys[ix.program_id_index] == P.PUMP_PROGRAM
        assert bytes(ix.data)[:8] == P.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR


def _usdc_frame():
    """EXACT_IN with its quote mint set to USDC (6 decimals), a curve the lane pays from the wallet's
    USDC account without a leg."""
    from solders.pubkey import Pubkey
    line = _event_line(EXACT_IN)
    raw = bytearray(base64.b64decode(line.split("Program data: ", 1)[1]))
    i = raw.find(bytes(Pubkey.from_string(WBTC)))
    raw[i:i + 32] = bytes(Pubkey.from_string(P.USDC_MINT))
    return dict(EXACT_IN, logs=["Program data: " + base64.b64encode(bytes(raw)).decode()])


def test_a_usdc_curve_refuses_by_name(curve_listed):
    """None of the 13 quote-token curves since 09-26 was USDC-quoted; until one is, it refuses."""
    reason, facts = L.curve_market_facts(_slot_buy(_usdc_frame()), 150.0)
    assert reason == "slot_lane_curve_usdc_quote_unsupported" and facts["quote_mint"] == P.USDC_MINT


def test_the_worker_refuses_a_leg_that_cannot_fit_one_packet_before_the_lane_reserves(curve_listed):
    """STC BiR7qY3f (2026-10-07): a 2-hop USDC -> WBTC leg + the 27-account buy built 1,446 bytes.
    The worker asks Jupiter for <= 20 leg accounts and assembles before handing the buy back."""
    from sniper.execution.raydium_launchlab_buy import OnDemandLegWorker
    from solders.pubkey import Pubkey
    b = _builder()
    paths, requeued, refused = [], [], []
    many = [{"pubkey": str(Pubkey.new_unique()), "isSigner": False, "isWritable": True} for _ in range(40)]
    quote = {"outAmount": "118140", "otherAmountThreshold": "118090", "priceImpactPct": "0"}

    def jup(method, path, body=None, *, http=None):
        paths.append(path)
        return (200, quote) if method == "GET" else (200, {"swapInstruction": {
            "programId": P.PUMP_PROGRAM, "accounts": many, "data": ""}, "addressLookupTableAddresses": []})
    w = OnDemandLegWorker(builders={"pump_curve": b}, admission_fn=lambda buy, q: None,
                          snapshot_fn=lambda: SNAP, clip_fn=lambda: 100.0, offer_fn=lambda b_, r: None,
                          start=False, clock=SimpleNamespace(monotonic=lambda: 60.0),
                          live_fn=lambda venue: True, requeue_fn=requeued.append,
                          refuse_fn=lambda b_, r: refused.append(r))
    orig = P._funded_jup_call
    P._funded_jup_call = jup
    try:
        result = w.process(_slot_buy(TOKENS_OUT))
    finally:
        P._funded_jup_call = orig
    assert "maxAccounts=20" in paths[0]
    assert result["status"] == "live_refused" and refused == ["slot_lane_curve_packet_oversize"]
    assert requeued == []
