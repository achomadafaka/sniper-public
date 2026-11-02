"""Native preparation fits the actual unbounded transaction in the Solana packet."""
from types import SimpleNamespace

import pytest
from solders.address_lookup_table_account import AddressLookupTableAccount
from solders.instruction import Instruction
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.execution import solana_slot_guard as G
from sniper.services import memecoin_slot_lane as L
from test_pumpswap_buy import SNAP, _Clock, _builder, _buy, pcb
from test_slot_lane_pump_alt import _table_account


SLOT = 458_900_000
TIP = (str(Pubkey.new_unique()), 200_000)


def _no_provider(*args, **kwargs):
    pytest.fail("packet preparation made a provider call")


def _case(monkeypatch, *, padding=137, held=True, already_in_leg=False, same_user=True):
    monkeypatch.setattr(L.cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 10.0)
    builder = _builder(_Clock())
    builder.rpc = builder.jup_call = _no_provider
    assert builder.pools.get(_buy().pool)["base_token_program"] == pcb.TOKEN_2022_PROGRAM
    # Opaque prefund instruction data reproduces the packet shape; these bytes are never executed.
    builder._leg["ixs"] = [Instruction(Pubkey.from_string(pcb.TOKEN_PROGRAM), bytes(padding), [])]
    user = builder.user if same_user else str(Pubkey.new_unique())
    address = str(Pubkey.new_unique())
    table, why = pcb.verified_pump_alt(address, _table_account(pcb.pump_alt_keys(user), authority=user), user)
    assert why is None
    if already_in_leg:
        builder._leg["tables"] = [table]
    curve = SimpleNamespace(user=user, pump_alt=table if held else None)
    buy = _buy(venue="pump_amm", slot=SLOT, signature="source-signature")
    return builder, buy, curve, table


def _size(message):
    return len(bytes(VersionedTransaction.populate(message, [Signature.default()])))


def _assert_unguarded(message):
    assert G.LIGHTHOUSE_PROGRAM not in [str(key) for key in message.account_keys]
    assert _size(message) <= 1232


@pytest.mark.parametrize("held,same_user", [(False, True), (True, False)])
def test_guard_only_overflow_does_not_refuse_a_fitting_unbounded_transaction(monkeypatch, held, same_user):
    builder, buy, curve, _ = _case(monkeypatch, held=held, same_user=same_user)
    base, _ = builder.build(buy, 5., SNAP)
    assert _size(base) == 1200
    tipped, reason = builder.build(buy, 5., SNAP, tip=TIP)
    assert tipped is None and reason == "transaction is 1249 bytes, over the 1232-byte packet"
    with pytest.raises(ValueError, match="entry_slot_guard_packet_oversized"):
        G.append_slot_guard(base, SLOT, SLOT + 1)

    message, reason = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP)

    assert message is not None, reason
    assert _size(message) == 1200
    _assert_unguarded(message)
    assert builder._leg["tables"] == []


def test_the_verified_held_table_compacts_the_real_token2022_buy(monkeypatch):
    builder, buy, curve, table = _case(monkeypatch)
    base, original = builder.build(buy, 5., SNAP)
    assert _size(base) == 1200
    leg_ixs = tuple(builder._leg["ixs"])

    message, validity = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP)

    assert message is not None, validity
    _assert_unguarded(message)
    assert _size(message) == 1141
    assert [lookup.account_key for lookup in message.address_table_lookups] == [table.key]
    for key in ("entry_slippage", "pump_price_limit", "blockhash", "last_valid_height"):
        assert validity[key] == original[key]
    assert tuple(builder._leg["ixs"]) == leg_ixs and builder._leg["tables"] == []


def test_a_held_table_already_in_the_leg_is_not_duplicated_or_mutated(monkeypatch):
    builder, buy, curve, table = _case(monkeypatch, already_in_leg=True)
    tables = builder._leg["tables"]
    message, validity = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP)
    assert message is not None, validity
    _assert_unguarded(message)
    assert [lookup.account_key for lookup in message.address_table_lookups] == [table.key]
    assert builder._leg["tables"] is tables and tables == [table]


@pytest.mark.parametrize("padding,tip,packet_size", [(0, None, 1062), (100, None, 1162), (59, TIP, 1170)])
def test_a_fitting_build_preserves_price_limits_without_a_slot_guard(monkeypatch, padding, tip, packet_size):
    builder, buy, curve, _ = _case(monkeypatch, padding=padding, held=False)
    base, original = builder.build(buy, 5., SNAP, tip=tip)
    message, validity = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP, tip=tip)
    assert message is not None, validity
    assert bytes(message) == bytes(base)
    assert validity["entry_slippage"] == original["entry_slippage"]
    assert validity["pump_price_limit"] == original["pump_price_limit"]
    assert validity["entry_slippage"]["source_slot"] == SLOT
    _assert_unguarded(message)
    assert _size(message) == packet_size


def test_real_tipped_packet_overflow_keeps_existing_untipped_retry(monkeypatch):
    builder, buy, curve, _ = _case(monkeypatch, padding=190)
    build = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})
    message, reason = build(buy, 5., SNAP, tip=TIP)
    assert message is None and "byte packet" in reason
    message, validity = build(buy, 5., SNAP)
    assert message is not None, validity
    _assert_unguarded(message)
    assert _size(message) == 1194


def test_an_extra_table_that_increases_wire_size_is_not_selected(monkeypatch):
    builder, buy, _, _ = _case(monkeypatch, padding=0)
    base, original = builder.build(buy, 5., SNAP)
    table = AddressLookupTableAccount(Pubkey.new_unique(), [Pubkey.from_string(pcb.TOKEN_2022_PROGRAM)])
    message, validity = builder.build(buy, 5., SNAP, lookup_tables=(table,))
    assert bytes(message) == bytes(base) and validity == original
    assert builder._leg["tables"] == []


def test_conflicting_contents_for_a_held_table_key_refuse_without_mutation(monkeypatch):
    builder, buy, curve, table = _case(monkeypatch, already_in_leg=True)
    conflicting = AddressLookupTableAccount(table.key, list(reversed(table.addresses)))
    curve.pump_alt = conflicting
    message, reason = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP)
    assert message is None and reason == "slot_lane_lookup_table_conflict"
    assert builder._leg["tables"] == [table]


def test_compaction_preserves_resolved_instruction_accounts_and_permissions(monkeypatch):
    builder, buy, curve, table = _case(monkeypatch)
    base, _ = builder.build(buy, 5., SNAP)
    compact, _ = L.dispatch_build({"pump_amm": builder, "pump_curve": curve})(buy, 5., SNAP)
    assert compact is not None

    def resolved(message):
        tables = {table.key: table}
        writable, readonly = [], []
        for lookup in message.address_table_lookups:
            addresses = tables[lookup.account_key].addresses
            writable.extend(addresses[i] for i in lookup.writable_indexes)
            readonly.extend(addresses[i] for i in lookup.readonly_indexes)
        keys = list(message.account_keys) + writable + readonly
        h = message.header
        def permissions(i):
            signed = i < h.num_required_signatures
            if i < len(message.account_keys):
                write = (i < h.num_required_signatures - h.num_readonly_signed_accounts if signed
                         else i < len(message.account_keys) - h.num_readonly_unsigned_accounts)
            else:
                write = i < len(message.account_keys) + len(writable)
            return signed, write
        return [(keys[ix.program_id_index], bytes(ix.data),
                 [(keys[i], *permissions(i)) for i in ix.accounts]) for ix in message.instructions]

    assert resolved(compact) == resolved(base)
