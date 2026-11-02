"""Real lost fee-transfer receipts retain losses and prove only other accounts absent."""
import base64
import hashlib
import json
import time
from pathlib import Path

import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_universe as U

FIX = Path(__file__).parent / "fixtures/memecoin/desk_local_fee_envelopes_20261028"
SILENT = {bytes.fromhex("8b348655e4e56cf1"), bytes.fromhex("01214eb921432c5c")}


def receipt(name):
    saved = json.loads((FIX / name).read_text())
    raw = base64.b64decode(saved["raw_b64"], validate=True)
    assert hashlib.sha256(raw).hexdigest() == saved["frame_sha256"]
    info = pb.SubscribeUpdate.FromString(raw).transaction.transaction
    msg, meta = info.transaction.message, info.meta
    keys = [U.b58encode(k) for k in (*msg.account_keys, *meta.loaded_writable_addresses, *meta.loaded_readonly_addresses)]
    instructions = [dict(program_id_index=i.program_id_index, accounts=list(i.accounts), data=bytes(i.data))
                    for i in (*msg.instructions, *(i for g in meta.inner_instructions for i in g.instructions))]
    return raw, list(meta.log_messages), keys, instructions


def details(logs, keys, instructions, *, complete=True):
    return P.loss_details(logs, reason="pumpswap_virtual_quote_unproven", account_keys=keys,
                          instructions=instructions, inner_complete=complete)


@pytest.mark.parametrize("name", ["transfer_creator_fees_v1.json", "transfer_creator_fees_v2.json"])
def test_silent_fee_transfer_keeps_original_loss_and_proves_only_account_absence(name):
    _, logs, keys, instructions = receipt(name)
    with pytest.raises(P.DecodeLoss, match="pumpswap_virtual_quote_unproven"):
        P.events(logs)
    out = details(logs, keys, instructions)
    assert out["scope"] == "unknown" and out["pools"] == []
    assert out["swap_tx_account_keys"] == sorted(set(keys))
    bits = L.event_account_filter(out["swap_tx_account_keys"])
    assert all(not L.event_account_absent(bits, key) for key in keys)
    assert L.event_account_absent(bits, U.b58encode(bytes([199]) * 32))


@pytest.mark.parametrize("damage", ["data_length", "account_count", "incomplete", "extra_invoke", "cpi_bytes", "admin", "loader"])
def test_silent_fee_exception_never_exempts_incomplete_or_unknown_transactions(damage):
    _, logs, keys, instructions = receipt("transfer_creator_fees_v1.json")
    silent = next(i for i in instructions if i["data"][:8] in SILENT)
    complete = True
    if damage == "data_length":
        silent["data"] += b"\0"
    elif damage == "account_count":
        silent["accounts"].pop()
    elif damage == "incomplete":
        complete = False
    elif damage == "extra_invoke":
        logs += [f"Program {P.AMM_PROGRAM} invoke [1]", f"Program {P.AMM_PROGRAM} success"]
    elif damage == "cpi_bytes":
        cpi = next(i for i in instructions if i["data"][:8] == P._EVENT_CPI)
        cpi["data"] = cpi["data"][:-1] + bytes([cpi["data"][-1] ^ 1])
    elif damage == "admin":
        instructions.append(dict(program_id_index=keys.index(P.AMM_PROGRAM), accounts=[], data=bytes(8)))
    else:
        keys.append("BPFLoaderUpgradeab1e11111111111111111111111")
        instructions.append(dict(program_id_index=len(keys) - 1, accounts=[], data=b"\x03"))
    assert "swap_tx_account_keys" not in details(logs, keys, instructions, complete=complete)


def test_worker_keeps_original_receipt_and_target_loss_when_publishing_envelope(tmp_path, monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    raw, _, keys, instructions = receipt("transfer_creator_fees_v1.json")
    w = worker(tmp_path, monkeypatch)
    at = time.time() - .01
    try:
        w.process(raw, received_at=at)
        w.commit_files()
        db = w.files.conn("ps_decode_losses", at)
        assert db.execute("SELECT cause FROM ps_decode_losses").fetchall() == [("virtual_quote_unproven",)]
        assert db.execute("SELECT scope FROM ps_loss_attribution").fetchall() == [("unknown",)]
        assert db.execute("SELECT profile FROM ps_loss_event_envelopes").fetchall() == [(L.SWAP_TX_ENVELOPE_PROFILE,)]
        sweep = next(i for i in instructions if i["data"][:8].hex() == "20f6bf3408c949ba")
        target = keys[sweep["accounts"][2]]
        hi = time.time() + 1
        assert L.losses(tmp_path, target, at - 1, hi) == [at]
        assert L.losses(tmp_path, U.b58encode(bytes([199]) * 32), at - 1, hi) == []
    finally:
        for db in w.files._open.values():
            db.close()


def test_all_silent_calls_need_no_event_but_still_bind_every_invocation():
    _, _, keys, instructions = receipt("transfer_creator_fees_v1.json")
    silent = next(i for i in instructions if i["data"][:8] in SILENT)
    logs = [f"Program {P.AMM_PROGRAM} invoke [1]", "Program log: Instruction: TransferCreatorFeesToPump",
            f"Program {P.AMM_PROGRAM} success"]
    assert details(logs, keys, [silent])["swap_tx_account_keys"] == sorted(set(keys))
    logs += [f"Program {P.AMM_PROGRAM} invoke [1]", f"Program {P.AMM_PROGRAM} success"]
    assert "swap_tx_account_keys" not in details(logs, keys, [silent])


@pytest.mark.parametrize("copies", [0, 2])
def test_non_silent_calls_still_require_exactly_one_matching_event(copies):
    _, logs, keys, instructions = receipt("transfer_creator_fees_v1.json")
    silent = next(i for i in instructions if i["data"][:8] in SILENT)
    sweep = next(i for i in instructions if i["data"][:8].hex() == "20f6bf3408c949ba")
    cpi = next(i for i in instructions if i["data"][:8] == P._EVENT_CPI)
    line = next(line for _, line in P.program_data_logs(logs, P.AMM_PROGRAM))
    logs = [f"Program {P.AMM_PROGRAM} invoke [1]"]
    for _ in range(copies):
        logs += [line, f"Program {P.AMM_PROGRAM} invoke [2]", f"Program {P.AMM_PROGRAM} success"]
    logs += [f"Program {P.AMM_PROGRAM} success", f"Program {P.AMM_PROGRAM} invoke [1]",
             f"Program {P.AMM_PROGRAM} success"]
    assert "swap_tx_account_keys" not in details(logs, keys, [sweep, *([cpi] * copies), silent])
