"""The processed Shyft routed path on the real PVPLAND receipt (meFDigZV, bigwarzeth, 2026-10-14
15:43Z): a USDC route through the Token-2022 TransferFee mint Xsc9qvGR into a pump curve coin.

(h1) transaction_metadata now reads TransferCheckedWithFee; before, it refused the whole update and the
routed pairer never ran. (g) every refusal names its reason, in a skip note and the worker heartbeat.
(e) a pair that moved and never paired writes its pair_expired note, as the legacy socket's expire()."""
import copy
import json
import struct
from pathlib import Path

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_shyft_confirmed as conf
from sniper.services import memecoin_shyft_signal as g
from sniper.services import memecoin_slot_skips as skips

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/pvpland_meFDigZV_route.json").read_text())
SIG = FIX["transaction"]["signatures"][0]
OWNER = "FRT8YuLJ9q8PPX2o4RUiJ9hWL427hNmMJ2M6JdgGgzwK"
OWNERS = {OWNER: ("uid", "bigwarzeth")}
COIN = "yR1un9YuM4HesmuJxnBYMWpkK8jrvAYdngsLxU9pump"
FEE_MINT = "Xsc9qvGR1efVDFGLrVsmkzv3qi45LTBjeUKSPmx9qEh"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@pytest.fixture(autouse=True)
def empty_skips():
    skips.drain()
    with skips._LAST_LOCK:
        skips._LAST.clear()
    yield
    skips.drain()


def wire(fix=FIX):
    """The receipt as the Yellowstone wire update: compiled instructions with their raw data."""
    u = pb.SubscribeUpdate()
    u.transaction.slot = fix["slot"]
    i = u.transaction.transaction
    i.signature = base58.b58decode(fix["transaction"]["signatures"][0])
    i.transaction.signatures.extend(base58.b58decode(s) for s in fix["transaction"]["signatures"])
    msg, meta = fix["transaction"]["message"], fix["meta"]
    m = i.transaction.message
    m.header.num_required_signatures = msg["header"]["numRequiredSignatures"]
    m.header.num_readonly_signed_accounts = msg["header"]["numReadonlySignedAccounts"]
    m.header.num_readonly_unsigned_accounts = msg["header"]["numReadonlyUnsignedAccounts"]
    m.account_keys.extend(base58.b58decode(k) for k in msg["accountKeys"])
    m.recent_blockhash = base58.b58decode(msg["recentBlockhash"])
    m.versioned = True
    for ix in msg["instructions"]:
        c = m.instructions.add(program_id_index=ix["programIdIndex"])
        c.accounts, c.data = bytes(ix["accounts"]), base58.b58decode(ix["data"])
    i.meta.fee = meta["fee"]
    i.meta.pre_balances.extend(meta["preBalances"])
    i.meta.post_balances.extend(meta["postBalances"])
    i.meta.log_messages.extend(meta["logMessages"])
    for group in meta["innerInstructions"]:
        inner = i.meta.inner_instructions.add(index=group["index"])
        for ix in group["instructions"]:
            c = inner.instructions.add(program_id_index=ix["programIdIndex"], stack_height=ix["stackHeight"])
            c.accounts, c.data = bytes(ix["accounts"]), base58.b58decode(ix["data"])
    for name, rows in (("pre_token_balances", meta["preTokenBalances"]),
                       ("post_token_balances", meta["postTokenBalances"])):
        for b in rows:
            r = getattr(i.meta, name).add(account_index=b["accountIndex"], owner=b["owner"], mint=b["mint"],
                                          program_id=b["programId"])
            r.ui_token_amount.amount = b["uiTokenAmount"]["amount"]
            r.ui_token_amount.decimals = b["uiTokenAmount"]["decimals"]
    return u


def metadata(fix=FIX, why=None):
    return conf.transaction_metadata(wire(fix), OWNERS, head_slot=fix["slot"], recv_monotonic=0,
                                     head_monotonic=0, why=why)


def routed(fix=FIX):
    return g.routed_buys(wire(fix), OWNERS, recv_monotonic=0, recv_utc="u", head_slot=fix["slot"],
                         head_mono=0, native_buys=())


def _fee_instruction(fix):
    keys = fix["transaction"]["message"]["accountKeys"]
    for group in fix["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            data = base58.b58decode(ix["data"])
            if keys[ix["programIdIndex"]] == TOKEN_2022 and data[:2] == bytes([26, 1]):
                return ix
    raise AssertionError("no TransferCheckedWithFee in the fixture")


def _with_fee_data(**changes):
    fix = copy.deepcopy(FIX)
    ix = _fee_instruction(fix)
    data = bytearray(base58.b58decode(ix["data"]))
    if "fee" in changes:
        struct.pack_into("<Q", data, 11, changes["fee"])
    if "subtag" in changes:
        data[1] = changes["subtag"]
    ix["data"] = base58.b58encode(bytes(data)).decode()
    return fix


def _fee_transfers(tx):
    return [ix for ix in tx["transaction"]["message"]["instructions"]
            + [x for group in tx["meta"]["innerInstructions"] for x in group["instructions"]]
            if (ix.get("parsed") or {}).get("type") == "transferCheckedWithFee"]


def test_pvpland_now_yields_metadata_and_the_routed_pairer_emits_its_buy():
    why = []
    tx = metadata(why=why)
    assert tx is not None and why == []
    fee, = _fee_transfers(tx)
    # As jsonParsed names it (getTransaction jsonParsed of the same receipt): gross amount, fee from data.
    assert fee["parsed"]["info"]["mint"] == FEE_MINT
    assert fee["parsed"]["info"]["tokenAmount"] == dict(amount="468413511", decimals=8)
    assert fee["parsed"]["info"]["feeAmount"] == dict(amount="0", decimals=8)
    buy, = routed()
    assert (buy.mint, buy.usdc_spent_atomic, buy.token_delta_atomic) == (COIN, 1_500_000_000, 67_582_841_530_300)
    assert buy.signature == SIG


def test_a_nonzero_fee_is_read_from_the_data_and_never_exceeds_the_amount():
    tx = metadata(_with_fee_data(fee=1_000))
    fee, = _fee_transfers(tx)
    assert fee["parsed"]["info"]["feeAmount"]["amount"] == "1000"            # never assumed 0
    assert fee["parsed"]["info"]["tokenAmount"]["amount"] == "468413511"      # the source's debit
    why = []
    assert metadata(_with_fee_data(fee=468_413_512), why=why) is None
    assert why == ["token_transfer_fee_invalid"]


def test_an_unknown_transfer_fee_sub_tag_is_refused_with_its_reason_and_noted():
    """(g): the refusal reason reaches the caller and a metadata_invalid skip note for the signature."""
    fix = _with_fee_data(subtag=2)                                            # not TransferCheckedWithFee
    why = []
    assert metadata(fix, why=why) is None and why == ["token_instruction_unsupported:26"]
    why = []
    assert g.routed_buys(wire(fix), OWNERS, recv_monotonic=0, recv_utc="u", head_slot=fix["slot"],
                         head_mono=0, native_buys=(), why=why) == ()
    assert why == ["token_instruction_unsupported:26"]
    assert skips.decision_for(SIG) == ("metadata_invalid", "token_instruction_unsupported:26")


def test_tag_26_on_the_legacy_token_program_stays_unsupported():
    keys = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"] + [OWNER] * 4
    ix = pb.CompiledInstruction(program_id_index=0, accounts=bytes([1, 2, 3, 4]),
                                data=bytes([26, 1]) + struct.pack("<QBQ", 5, 6, 0))
    with pytest.raises(ValueError, match="token_instruction_unsupported:26"):
        conf._instruction(ix, keys)


def test_a_refused_update_with_no_tracked_signer_writes_no_note():
    fix = _with_fee_data(subtag=2)
    assert g.routed_buys(wire(fix), {"11111111111111111111111111111111": ("u", "h")}, recv_monotonic=0,
                         recv_utc="u", head_slot=fix["slot"], head_mono=0, native_buys=()) == ()
    assert skips.decision_for(SIG) is None


def test_a_pair_that_moved_and_never_paired_writes_its_cause_once_per_signature():
    """(e): the owner's coin rose but its USDC did not fall: no buy, one pair_expired note, even when
    both Shyft feeds deliver the same transaction."""
    fix = copy.deepcopy(FIX)
    pre = {b["accountIndex"]: b for b in fix["meta"]["preTokenBalances"] if b["owner"] == OWNER}
    for b in fix["meta"]["postTokenBalances"]:
        if b["owner"] == OWNER and b["accountIndex"] in pre:
            b["uiTokenAmount"]["amount"] = pre[b["accountIndex"]]["uiTokenAmount"]["amount"]
    skips.drain()
    assert routed(fix) == () and routed(fix) == ()
    assert skips.decision_for(SIG) == ("pair_expired", "usdc_not_spent")
    assert [row[:3] for row in skips.drain()] == [(SIG, "pair_expired", "usdc_not_spent")]


def test_a_decoder_hint_never_replaces_the_lane_s_decision():
    skips.note(SIG, "rejected", "mcap $280178 over ceiling $250000")
    routed(_with_fee_data(subtag=2))
    assert skips.decision_for(SIG) == ("rejected", "mcap $280178 over ceiling $250000")


def test_a_late_hint_cannot_overwrite_a_flushed_lane_decision(tmp_path):
    """Across a restart the memory is empty; the timing file's upsert keeps the lane's word."""
    from sniper.services import memecoin_fill_timing as ft
    conn = ft.connect(tmp_path / "timing.db")
    skips.note(SIG, "rejected", "kill_switch")
    skips.flush(conn, skips.drain())
    with skips._LAST_LOCK:
        skips._LAST.clear()
    skips.note(SIG, "metadata_invalid", "token_instruction_unsupported:26")
    skips.flush(conn, skips.drain())
    assert ft.skip_of(conn, "solana", SIG) == dict(outcome="rejected", reason="kill_switch")
