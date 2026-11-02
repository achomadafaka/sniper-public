"""Per-leg fee provenance (Gamma/Codex/Alpha 2026-10-22): the fee footprint recorded at the write-ahead is derived
from the EXACT bound bytes (priority + known tips), never carried from an earlier sign; unproven bytes record NULL.
And the entry clamp refuses unsigned a known Sender tip above its cap (Codex's 50M reproducer)."""
import base64
import sqlite3

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
from sniper.persistence import db_memecoin as m


def payload(ixs, kp=None):
    kp = kp or Keypair()
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [], Hash.default())
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode(), kp


def swap():
    return Instruction(Pubkey.new_unique(), bytes(8), [])


def test_the_footprint_is_priority_plus_known_tips_from_the_bytes():
    kp = Keypair()
    nozomi = A.tip_instructions(str(kp.pubkey()), (A.NOZOMI_TIP_ACCOUNTS[0], 1_000_000))
    p, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(), *nozomi], kp)
    assert A.signed_fee_footprint_lamports(p) == 400_000 + 1_000_000
    sender = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], 5_000))
    p, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(), *sender], kp)
    assert A.signed_fee_footprint_lamports(p) == 400_000 + 5_000


@pytest.mark.parametrize("shape", ["no_limit", "dup_price", "dup_limit", "garbage"])
def test_unproven_bytes_record_none(shape):
    if shape == "garbage":
        assert A.signed_fee_footprint_lamports("0xdeadbeef") is None
        return
    ixs = {"no_limit": [set_compute_unit_price(1), swap()],
           "dup_price": [set_compute_unit_limit(1000), set_compute_unit_price(1), set_compute_unit_price(2), swap()],
           "dup_limit": [set_compute_unit_limit(1000), set_compute_unit_limit(2000), swap()]}[shape]
    assert A.signed_fee_footprint_lamports(payload(ixs)[0]) is None


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _pid(c, mint="M"):
    return m.reserve_pending(c, mint=mint, source_user_id="u", strategy="s", notional=10,
                             reserved_at="2026-10-22T00:00:00+00:00", network_id="solana")


def test_set_pending_sig_records_the_bound_bytes_footprint_in_the_same_update_and_never_carries():
    c = _ledger()
    a, b, n = _pid(c, "A"), _pid(c, "B"), _pid(c, "N")
    pa, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap()])
    pb, _ = payload([set_compute_unit_limit(200_000), set_compute_unit_price(2_000_000), swap()])
    statements = []
    c.set_trace_callback(statements.append)
    m.set_pending_sig(c, a, "sa", "t", signed_payload_b64=pa)
    c.set_trace_callback(None)
    assert [x for x in statements if x.startswith("UPDATE memecoin_pending_entries")][0].count("entry_fee_ceiling") == 1
    m.set_pending_sig(c, b, "sb", "t", signed_payload_b64=pb)
    m.set_pending_sig(c, n, "sn", "t")                                     # legacy caller without bytes
    got = {r[0]: r[1] for r in c.execute("SELECT id, entry_fee_ceiling_lamports FROM memecoin_pending_entries")}
    assert got == {a: 400_000, b: 400_000, n: None}
    m.set_pending_sig(c, a, "sa2", "t", signed_payload_b64=pb)             # a re-sign records ITS bytes
    assert c.execute("SELECT entry_fee_ceiling_lamports FROM memecoin_pending_entries WHERE id=?", (a,)).fetchone()[0] \
        == 400_000


def test_bind_entry_submission_records_the_footprint_and_a_non_solana_payload_is_null():
    c = _ledger()
    s, e = _pid(c, "S"), _pid(c, "E")
    p, _ = payload([set_compute_unit_limit(300_000), set_compute_unit_price(10_000_000), swap()])
    kw = dict(signed_payload_sha256="h", blockhash="bh", last_valid_height=1, nonce=None, backend="t", bound_at="t")
    assert m.bind_entry_submission(c, s, tx_identity="sig-s", signed_payload_b64=p, **kw)
    assert m.bind_entry_submission(c, e, tx_identity="0xabc", signed_payload_b64="0x02f8...", **kw)
    got = {r[0]: r[1] for r in c.execute("SELECT id, entry_fee_ceiling_lamports FROM memecoin_pending_entries")}
    assert got == {s: 3_000_000, e: None}


def test_the_clamp_refuses_a_known_sender_tip_above_its_cap():
    kp = Keypair()
    sender = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], 50_000_000))
    p, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(), *sender], kp)
    with pytest.raises(ValueError, match="sender_ab_tip_guard: entry_sender_tip_over_cap"):
        A.cap_unsigned_entry_priority(p, tip_lamports=0)
    legal = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], 5_000))
    p, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(), *legal], kp)
    _out, info = A.cap_unsigned_entry_priority(p, tip_lamports=0)
    assert info["sender_tip_lamports"] == 5_000 and info["fee_ceiling_lamports"] <= 6_520_000 + 5_000


def test_an_old_shape_pending_table_gains_the_nullable_column_and_old_sql_still_works():
    c = _ledger()
    cols = [r[1] for r in c.execute("PRAGMA table_info(memecoin_pending_entries)") if r[1] != "entry_fee_ceiling_lamports"]
    c.execute("ALTER TABLE memecoin_pending_entries RENAME TO pe_new")
    c.execute("CREATE TABLE memecoin_pending_entries AS SELECT %s FROM pe_new WHERE 0" % ",".join(cols))
    c.execute("DROP TABLE pe_new")
    assert "entry_fee_ceiling_lamports" not in {r[1] for r in c.execute("PRAGMA table_info(memecoin_pending_entries)")}
    c.execute("INSERT INTO memecoin_pending_entries(id, mint, status) VALUES (7, 'OLD', 'reserved')")
    c.commit()
    m.ensure_memecoin_tables(c)                                             # bootstrap migration
    row = c.execute("SELECT entry_fee_ceiling_lamports FROM memecoin_pending_entries WHERE id=7").fetchone()
    assert row[0] is None                                                   # nullable, no default
    c.execute("UPDATE memecoin_pending_entries SET tx_sig=?, broadcast_at=? WHERE id=?", ("s", "t", 7))   # old release
    assert c.execute("SELECT tx_sig FROM memecoin_pending_entries WHERE id=7").fetchone()[0] == "s"


def test_the_stored_footprint_always_describes_the_stored_bytes():
    """Gamma LOW 1: a legacy set_pending_sig signature replaced by the first live bind records the NEW bytes'
    footprint; a second bind with different bytes is refused by the WHERE, so footprint and payload never diverge."""
    c = _ledger()
    pid = _pid(c, "R")
    old, _ = payload([set_compute_unit_limit(200_000), set_compute_unit_price(1_000_000), swap()])
    new, _ = payload([set_compute_unit_limit(300_000), set_compute_unit_price(2_000_000), swap()])
    other, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(9_000_000), swap()])
    m.set_pending_sig(c, pid, "sig-old", "t", signed_payload_b64=old)
    kw = dict(blockhash="bh", last_valid_height=1, nonce=None, backend="t", bound_at="t")
    assert m.bind_entry_submission(c, pid, tx_identity="sig-new", signed_payload_b64=new, signed_payload_sha256="h-new",
                                   replace_unbound="sig-old", **kw)
    row = c.execute("SELECT tx_sig, entry_fee_ceiling_lamports FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()
    assert tuple(row) == ("sig-new", 600_000)
    assert not m.bind_entry_submission(c, pid, tx_identity="sig-new", signed_payload_b64=other,
                                       signed_payload_sha256="h-other", **kw)
    assert c.execute("SELECT entry_fee_ceiling_lamports FROM memecoin_pending_entries WHERE id=?", (pid,)).fetchone()[0] \
        == 600_000


def _alt_payload(dest_kind, lamports=50_000_000):
    """Codex's 20cff3792 case: a static SystemProgram transfer whose DESTINATION is loaded from a lookup table."""
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.system_program import TransferParams, transfer
    kp = Keypair()
    dest = {"sender": Pubkey.from_string(A.TIP_ACCOUNTS[0]), "nozomi": Pubkey.from_string(A.NOZOMI_TIP_ACCOUNTS[0]),
            "unknown": Pubkey.new_unique()}[dest_kind]
    alt = AddressLookupTableAccount(Pubkey.new_unique(), [dest])
    ixs = [set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(),
           transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=dest, lamports=lamports))]
    msg = MessageV0.try_compile(kp.pubkey(), ixs, [alt], Hash.default())
    assert str(dest) not in [str(k) for k in msg.account_keys]                       # really loaded
    return base64.b64encode(bytes(VersionedTransaction.populate(msg, [Signature.default()]))).decode()


@pytest.mark.parametrize("dest_kind", ["sender", "nozomi", "unknown"])
def test_a_transfer_to_a_lookup_table_destination_is_unproven_and_refused(dest_kind):
    p = _alt_payload(dest_kind)
    assert A.signed_fee_footprint_lamports(p) is None
    with pytest.raises(ValueError, match="sender_ab_tip_guard: entry_loaded_transfer_destination"):
        A.cap_unsigned_entry_priority(p, tip_lamports=0)


def test_a_static_legal_sender_tip_still_signs_with_its_footprint():
    kp = Keypair()
    legal = A.tip_instructions(str(kp.pubkey()), (A.TIP_ACCOUNTS[0], 5_000))
    p, _ = payload([set_compute_unit_limit(400_000), set_compute_unit_price(1_000_000), swap(), *legal], kp)
    assert A.signed_fee_footprint_lamports(p) == 405_000
    A.cap_unsigned_entry_priority(p, tip_lamports=0)


def test_the_shared_tip_readers_used_by_posting_and_exits_are_unchanged_for_a_loaded_destination():
    """Codex/Alpha scope: only the ENTRY clamp and the footprint fail closed on a loaded transfer destination; the
    shared readers (tip_transfers, payload_tip, guard_refusal) behave exactly as before, so exits deliver unchanged."""
    p = _alt_payload("sender")
    msg = VersionedTransaction.from_bytes(base64.b64decode(p)).message
    assert A.tip_transfers(msg) == [] and A.tip_transfers(msg, A._NOZOMI_SET) == []   # loaded: not counted, as before
    assert A.payload_tip(p) == 0
    assert A.guard_refusal(msg, 0) is None
