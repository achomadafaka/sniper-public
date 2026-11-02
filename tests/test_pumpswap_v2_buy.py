"""PumpSwap buy_exact_quote_in_v2 sources (2026-10-08, TARDIGRADE: OmakaseOnly's $729 buy refused
'source average price unavailable', attempts b024eba6 and 5c71ad09).

The v2 BuyEvent keeps the v1 exact-quote meaning (quote_amount_in is the gross the buyer paid), but
names no protocol fee recipient: v2 leaves its fees in the pool, so both recipient fields are the
default pubkey. Our slot lane signs a v1 buy_exact_quote_in, which must name a GlobalConfig member
(v1 2Auf5yTD succeeded on the same pool 11 minutes later naming one). Every frame here is real:
tests/fixtures/memecoin/pumpswap_v2_buy_20261008.json."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.execution import pump_curve_buy as pcb
from sniper.execution import pumpswap_buy as ps

FIX = json.loads((Path(__file__).parent / "fixtures" / "memecoin" / "pumpswap_v2_buy_20261008.json").read_text())
TARD = FIX["tardigrade"]
V2_LOGS = [TARD["buy_event_log"]] + [s["buy_event_log"] for s in FIX["v2_samples"]]
BLOCKHASH = "11111111111111111111111111111111"
SOL_USD = 110.377                       # the source receipt's cached SOL/USD
LEG_LAMPORTS = int(5.0 / SOL_USD * 1e9 * 0.995)   # a $5 USDC -> SOL leg at that price


@pytest.mark.parametrize("line", V2_LOGS)
def test_every_live_v2_event_reads_its_gross_quote(line):
    ev = ps.parse_buy_event(line)
    assert ev["ix_name"] == "buy_exact_quote_in_v2"
    parts = ev["quote_amount_in_with_lp_fee"] + ev["protocol_fee"] + ev["coin_creator_fee"]
    assert ev["quote_amount_in"] == parts != ev["user_quote_amount_in"]
    assert ps.buy_event_gross_quote(ev) == parts


def test_a_v2_event_whose_fees_contradict_it_still_refuses():
    ev = ps.parse_buy_event(TARD["buy_event_log"])
    assert ps.buy_event_gross_quote(dict(ev, quote_amount_in=ev["quote_amount_in"] + 1)) is None


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _builder(*, recipient=True):
    event = ps.parse_buy_event(TARD["buy_event_log"])
    facts = ps.parse_pool_account(TARD["pool_account_b64"])
    facts.update(base_token_program=TARD["base_token_program"], quote_token_program=TARD["quote_token_program"],
                 base_decimals=TARD["base_decimals"])
    b = ps.SlotLanePumpSwapBuilder(user=event["user"], jup_call=lambda *a, **k: None,
                                   rpc=lambda *a: None, clock=_Clock())
    b._buyback = ps.buyback_fee_recipient_from_global_config(FIX["global_config_b64"])
    if recipient:
        b._protocol_recipient = ps.protocol_fee_recipient_from_global_config(FIX["global_config_b64"])
    b.pools._pools[event["pool"]] = facts
    b._leg = {"ixs": [], "tables": [], "usdc_atomic": pcb.clip_atomic(5.0),
              "min_out_lamports": LEG_LAMPORTS, "out_lamports": LEG_LAMPORTS + 300_000, "fetched_at": _Clock.t}
    buy = SimpleNamespace(pool=event["pool"], trade_event_log=TARD["buy_event_log"], mint=None)
    return b, buy, event


@pytest.fixture
def _cap_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)


def test_tardigrade_builds_and_names_a_global_config_protocol_recipient(_cap_on):
    b, buy, event = _builder()
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=SOL_USD)
    message, validity = b.build(buy, 5.0, snap)
    assert message is not None, validity
    keys = [str(k) for k in message.account_keys]
    buy_ix = [ix for ix in message.instructions if keys[ix.program_id_index] == ps.AMM_PROGRAM]
    assert len(buy_ix) == 1
    accounts = [keys[i] for i in bytes(buy_ix[0].accounts)]
    recipient = ps.protocol_fee_recipient_from_global_config(FIX["global_config_b64"])
    assert event["protocol_fee_recipient"] == ps.SYSTEM_PROGRAM
    assert accounts[9] == recipient != ps.SYSTEM_PROGRAM
    assert accounts[10] == str(ps.ata(ps._pk(recipient), ps._pk(ps.WSOL_MINT), ps._pk(ps.TOKEN_PROGRAM)))
    # The copy-premium cap kept its event average: the v2 gross, not a refusal.
    base_res, quote_res = ps.post_trade_reserves(event)
    price = validity["pump_price_limit"]["source_price_usd"]
    expected = (price * event["quote_amount_in"] * base_res
                / (event["base_amount_out"] * (quote_res + b.pools._pools[event["pool"]]["virtual_quote"])))
    assert b.last_build["source_average_price_usd"] == pytest.approx(expected, rel=1e-12)


def test_a_v2_source_without_a_cached_recipient_refuses(_cap_on):
    b, buy, _event = _builder(recipient=False)
    snap = SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=SOL_USD)
    assert b.build(buy, 5.0, snap) == (None, "no_protocol_fee_recipient")


def _signed_recipient(message):
    keys = [str(k) for k in message.account_keys]
    buy_ix = [ix for ix in message.instructions if keys[ix.program_id_index] == ps.AMM_PROGRAM]
    return [keys[i] for i in bytes(buy_ix[0].accounts)][9]


def test_a_v1_source_keeps_its_own_recipient(_cap_on):
    b, _buy, _event = _builder()
    line = FIX["v1_same_pool"]["buy_event_log"]
    ev = ps.parse_buy_event(line)
    assert ev["ix_name"] == "buy_exact_quote_in"
    assert ev["protocol_fee_recipient"] not in (ps.SYSTEM_PROGRAM, b._protocol_recipient)
    buy = SimpleNamespace(pool=ev["pool"], trade_event_log=line, mint=None)
    message, validity = b.build(buy, 5.0, SimpleNamespace(blockhash=BLOCKHASH, last_valid_height=1, sol_usd=SOL_USD))
    assert message is not None, validity
    assert _signed_recipient(message) == ev["protocol_fee_recipient"]
