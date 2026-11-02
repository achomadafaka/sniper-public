"""Exit fee clamp (operator yes 2026-10-23): every Solana EXIT build is clamped to
MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS before signing. An exit is never refused, delayed or dropped:
unreadable bytes are signed unchanged and the leg is recorded unbounded. Codex's reproducer: Jupiter 'auto'
returning 1.4M CU x 100M uL (140M lamports) used to be signed unchanged."""
import base64

import pytest
from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.execution import solana_sender_ab as A
from sniper.execution import solana_real_seams as seams
from sniper.execution import solana_exit_executor as exits
from sniper.execution.solana_swap_executor import signed_priority_fee_lamports
from sniper.settings import memecoin as cfg

CAP = 5_000_000


def _payload(kp, *budget, tips=()):
    msg = MessageV0.try_compile(kp.pubkey(), [*budget, Instruction(Pubkey.new_unique(), bytes(40), []), *tips],
                                [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


def _exit_sign(monkeypatch, swap, kp):
    """The REAL exit sign seam: build_real_solana_executor's exit_fn hands execute_exit its sign_fn."""
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", CAP)
    monkeypatch.setattr(seams, "jupiter_swap_tx", lambda *a, **k: swap)
    seen = {}

    def fake_execute_exit(conn, position, exit_reason, now_iso, **kw):
        seen["sign_fn"] = kw["sign_fn"]
        return {"status": "stuck"}
    monkeypatch.setattr(exits, "execute_exit", fake_execute_exit)
    ex = seams.build_real_solana_executor(keypair=kp, rpc_url="http://offline.invalid",
                                          token_fn=lambda *a, **k: 0, usdc_fn=lambda *a, **k: 0)
    monkeypatch.setattr(seams, "_rpc_call", lambda *a, **k: pytest.fail("offline test reached RPC"))
    import sniper.persistence.db_memecoin as m
    monkeypatch.setattr(m, "record_exit_execution", lambda *a, **k: None)
    ex["exit_fn"](None, {"id": 1, "mint": "M", "network_id": "solana"}, "b_stop", "2026-10-23T00:00:00Z")
    sign_fn = seen["sign_fn"]
    out = sign_fn({"outAmount": "1"})
    return out, sign_fn.sign_timing()


def test_codex_140m_exit_is_clamped_at_the_real_seam(monkeypatch):
    kp = Keypair()
    raw = _payload(kp, set_compute_unit_limit(1_400_000), set_compute_unit_price(100_000_000))
    assert signed_priority_fee_lamports(raw) == 140_000_000
    out, split = _exit_sign(monkeypatch, raw, kp)
    assert out is not None
    assert signed_priority_fee_lamports(out[0]) == CAP * 1_400_000 // 1_000_000 == 7_000_000
    assert split["exit_fee"]["fee_ceiling_lamports"] == 7_000_000
    assert split["exit_fee"]["clamped"] is True


def test_a_price_under_the_cap_is_signed_unchanged(monkeypatch):
    kp = Keypair()
    raw = _payload(kp, set_compute_unit_limit(200_000), set_compute_unit_price(1_000))
    out, split = _exit_sign(monkeypatch, raw, kp)
    assert signed_priority_fee_lamports(out[0]) == 200
    assert split["exit_fee"]["clamped"] is False
    assert split["exit_fee"]["fee_ceiling_lamports"] == CAP * 200_000 // 1_000_000


def test_unreadable_exit_bytes_sign_unchanged_and_record_unbounded(monkeypatch):
    kp = Keypair()
    raw = _payload(kp, set_compute_unit_limit(200_000), set_compute_unit_limit(300_000),
                   set_compute_unit_price(100_000_000))                       # duplicate limit: unreadable
    out, split = _exit_sign(monkeypatch, raw, kp)
    assert out is not None                                                     # never refused
    assert split["exit_fee"]["fee_ceiling_lamports"] is None
    assert split["exit_fee"]["bounded"] is False


def test_missing_limit_bounds_at_the_runtime_maximum():
    kp = Keypair()
    out, info = A.clamp_exit_priority(_payload(kp, set_compute_unit_price(100_000_000)), cap=CAP)
    assert info["fee_ceiling_lamports"] == CAP * 1_400_000 // 1_000_000
    assert info["clamped"] is True


def test_known_tips_in_the_bytes_count_in_the_ceiling():
    kp = Keypair()
    tips = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], 10_000))
    _out, info = A.clamp_exit_priority(
        _payload(kp, set_compute_unit_limit(200_000), set_compute_unit_price(1), tips=tips), cap=CAP)
    assert info["fee_ceiling_lamports"] == 1_000_000 + 10_000


def test_metadata_dict_keeps_its_fields():
    kp = Keypair()
    meta = {"swapTransaction": _payload(kp, set_compute_unit_limit(1_400_000), set_compute_unit_price(10**8)),
            "lastValidBlockHeight": 7}
    out, info = A.clamp_exit_priority(meta, cap=CAP)
    assert out["lastValidBlockHeight"] == 7 and info["clamped"] is True
    assert signed_priority_fee_lamports(out["swapTransaction"]) == 7_000_000


@pytest.mark.parametrize("cap", [0, -1])
def test_a_non_positive_cap_never_zeroes_an_exit_fee(cap):
    kp = Keypair()
    raw = _payload(kp, set_compute_unit_limit(200_000), set_compute_unit_price(1_000))
    out, info = A.clamp_exit_priority(raw, cap=cap)
    assert out == raw and info["bounded"] is False and info["fee_ceiling_lamports"] is None
