"""The funded wallet's pump address lookup table for quote-curve buys (2026-10-07, Alpha).

A quote-curve buy (leg + 27 accounts) built 1,111-1,446 bytes against the 1,232-byte packet; 12
keys every such buy names and no leg brings save 152 bytes in a table. The operator creates it
with scripts/memecoin_create_pump_alt.py --confirm; the curve builder reads it once on the
prefetch thread and uses it only when it holds exactly those 12 keys and is active.
"""
import importlib.util
import json
import struct
from pathlib import Path

import pytest
from solders.pubkey import Pubkey

from sniper.execution import pump_curve_buy as P
from test_slot_lane_quote_curve import (EXACT_IN, SNAP, USER, _builder, _leg, _slot_buy,
                                        curve_listed)  # noqa: F401 (fixture)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memecoin_create_pump_alt.py"
TABLE = str(Pubkey.new_unique())


def _table_account(keys, *, deactivation=2 ** 64 - 1, owner=P.ALT_PROGRAM, authority=USER):
    """A lookup table account as getAccountInfo returns it: the 56-byte meta, then the keys."""
    auth = (b"\x01" + bytes(Pubkey.from_string(authority))) if authority else b"\x00" + bytes(32)
    meta = struct.pack("<IQQB", 1, deactivation, 10, 0) + auth + b"\x00\x00"
    raw = meta + b"".join(bytes(Pubkey.from_string(k)) for k in keys)
    import base64
    return {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]}


def test_the_twelve_keys_are_distinct_and_name_the_wallets_volume_account():
    keys = P.pump_alt_keys(USER)
    assert len(keys) == len(set(keys)) == 12
    assert str(P._pda([b"user_volume_accumulator", bytes(P._pk(USER))], P.PUMP_PROGRAM)) in keys


@pytest.mark.parametrize("case, why", [
    ("exact", None), ("one_key_off", "table_keys_mismatch"), ("short", "table_keys_mismatch"),
    ("deactivated", "table_deactivated"), ("foreign_owner", "table_owner_X"), ("absent", "table_absent"),
    ("foreign_authority", "table_authority_foreign"), ("frozen", None),
])
def test_a_table_is_used_only_when_it_holds_exactly_the_keys(case, why):
    keys = P.pump_alt_keys(USER)
    account = {
        "exact": _table_account(keys[::-1]),                       # order does not matter
        "one_key_off": _table_account(keys[:-1] + [str(Pubkey.new_unique())]),
        "short": _table_account(keys[:-1]),
        "deactivated": _table_account(keys, deactivation=12345),
        "foreign_owner": _table_account(keys, owner="X"),
        "absent": None,
        "foreign_authority": _table_account(keys, authority=str(Pubkey.new_unique())),
        "frozen": _table_account(keys, authority=None),             # nobody can deactivate it
    }[case]
    table, got = P.verified_pump_alt(TABLE, account, USER)
    assert got == why
    assert (table is not None) == (why is None)
    if table is not None:
        assert str(table.key) == TABLE and len(table.addresses) == 12


def _with_table(b, tmp_path, account, calls, status="active"):
    state = tmp_path / "slot_lane_pump_alt.json"
    state.write_text(json.dumps({"address": TABLE, "status": status}))
    b.pump_alt_path = str(state)
    mint_rpc = b.rpc

    def rpc(method, params):
        if method == "getAccountInfo":
            calls.append(params[0])
            return {"value": account}
        return mint_rpc(method, params)
    b.rpc = rpc
    return state


def test_the_builder_reads_the_table_once_and_builds_through_it(curve_listed, tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35.0)
    calls = []
    b = _builder()
    buy = _slot_buy(EXACT_IN)
    assert b.ondemand_quote(buy)[2] is None
    usd = 100.0 / (118_090 / 1e8)
    without, _ = b.build(buy, 100.0, SNAP, leg=_leg(), quote=(8, usd, 0, 0))
    _with_table(b, tmp_path, _table_account(P.pump_alt_keys(USER)), calls)
    b._refresh_pump_alt()
    b._refresh_pump_alt()
    assert calls == [TABLE] and b.pump_alt is not None              # once
    message, validity = b.build(buy, 100.0, SNAP, leg=_leg(), quote=(8, usd, 0, 0))
    assert message is not None, validity
    assert [str(l.account_key) for l in message.address_table_lookups] == [TABLE]
    assert without is not None and len(message.account_keys) < len(without.account_keys)


def test_a_mismatched_table_is_logged_once_and_never_used(tmp_path, caplog):
    calls = []
    b = _builder()
    keys = P.pump_alt_keys(USER)
    _with_table(b, tmp_path, _table_account(keys[:-1] + [str(Pubkey.new_unique())]), calls)
    b._refresh_pump_alt()
    b.clock = lambda: 10_000.0
    b._refresh_pump_alt()
    assert b.pump_alt is None and calls == [TABLE]
    assert sum("not used: table_keys_mismatch" in r.getMessage() for r in caplog.records) == 1


def test_a_pending_table_is_not_read_until_the_script_marks_it_active(tmp_path):
    calls = []
    b = _builder()
    now = [50.0]
    b.clock = lambda: now[0]
    state = _with_table(b, tmp_path, _table_account(P.pump_alt_keys(USER)), calls, status="pending")
    b._refresh_pump_alt()
    assert calls == [] and b.pump_alt is None and not b._pump_alt_done
    state.write_text(json.dumps({"address": TABLE, "status": "active"}))
    now[0] += P.PUMP_ALT_RETRY_S
    b._refresh_pump_alt()
    assert calls == [TABLE] and b.pump_alt is not None


def test_no_state_file_yet_retries_after_a_minute(tmp_path):
    b = _builder()
    b.pump_alt_path = str(tmp_path / "absent.json")
    now = [50.0]
    b.clock = lambda: now[0]
    b._refresh_pump_alt()
    assert b._pump_alt_next == 50.0 + P.PUMP_ALT_RETRY_S and not b._pump_alt_done
    calls = []
    _with_table(b, tmp_path, _table_account(P.pump_alt_keys(USER)), calls)
    b._refresh_pump_alt()
    assert calls == []                                         # not before the minute
    now[0] += P.PUMP_ALT_RETRY_S
    b._refresh_pump_alt()
    assert calls == [TABLE] and b.pump_alt is not None


# --- the operator's script ---------------------------------------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location("create_pump_alt", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_create_and_extend_name_the_derived_table_and_the_twelve_keys():
    mod = _script()
    keys = P.pump_alt_keys(USER)
    (create, extend), table = mod.create_and_extend_instructions(USER, 454_100_000, keys)
    expect, bump = Pubkey.find_program_address(
        [bytes(Pubkey.from_string(USER)), struct.pack("<Q", 454_100_000)], Pubkey.from_string(P.ALT_PROGRAM))
    assert table == str(expect)
    assert bytes(create.data) == struct.pack("<IQB", 0, 454_100_000, bump)
    data = bytes(extend.data)
    assert data[:12] == struct.pack("<IQ", 2, 12)
    assert [str(Pubkey.from_bytes(data[12 + 32 * i:44 + 32 * i])) for i in range(12)] == keys
    for ix in (create, extend):
        assert str(ix.program_id) == P.ALT_PROGRAM
        assert [(str(a.pubkey), a.is_signer, a.is_writable) for a in ix.accounts] == [
            (table, False, True), (USER, True, False), (USER, True, True), (P.SYSTEM_PROGRAM, False, False)]


def test_the_dry_run_prints_and_sends_nothing_and_an_existing_state_refuses(tmp_path, monkeypatch, capsys):
    from sniper.execution import solana_wallet
    from sniper.settings import memecoin as cfg
    mod = _script()
    key = tmp_path / "kp.json"
    kp = solana_wallet.generate_keypair_file(str(key))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "http://unused")
    seen = []
    monkeypatch.setattr(mod, "_rpc", lambda url, method, params: seen.append(method) or 3_953_280)
    state = tmp_path / "alt.json"
    assert mod.main(["--keypair", str(key), "--state", str(state)]) == 0
    out = capsys.readouterr().out
    assert "Dry run. Nothing signed or sent." in out and str(kp.pubkey()) in out
    assert seen == ["getMinimumBalanceForRentExemption"] and not state.exists()
    state.write_text(json.dumps({"address": TABLE, "status": "active"}))
    assert mod.main(["--keypair", str(key), "--state", str(state), "--confirm"]) == 2
    assert "REFUSED" in capsys.readouterr().out and seen == ["getMinimumBalanceForRentExemption"]


@pytest.mark.parametrize("verdict, height, code, new_attempt", [
    ("confirmed", 100, 0, False),          # it landed: verify and mark active, no new table
    ("pending", 100, 1, False),            # may yet land: refuse
    ("unavailable", 100, 1, False),
    ("not_found", 150, 1, False),          # unseen, blockhash still valid: refuse
    ("not_found", 201, 0, True),           # unseen and expired: a new attempt is safe (dry run here)
    ("failed", 100, 0, True),              # reverted: no table exists
])
def test_a_rerun_resolves_the_pending_signature_before_any_new_table(
        tmp_path, monkeypatch, capsys, verdict, height, code, new_attempt):
    from sniper.execution import solana_real_seams as seams
    from sniper.execution import solana_wallet
    from sniper.execution.solana_real_seams import ConfirmState
    from sniper.settings import memecoin as cfg
    mod = _script()
    key = tmp_path / "kp.json"
    kp = solana_wallet.generate_keypair_file(str(key))
    authority = str(kp.pubkey())
    keys = P.pump_alt_keys(authority)
    state = tmp_path / "alt.json"
    pending = {"status": "pending", "address": TABLE, "authority": authority, "keys": keys,
               "signature": "SIG", "recent_slot": 90, "last_valid_block_height": 200}
    state.write_text(json.dumps(pending))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "http://unused")
    monkeypatch.setattr(seams, "rpc_confirm", lambda sig, url, **k: ConfirmState(verdict, landing_slot=95)
                        if verdict == "confirmed" else ConfirmState(verdict))
    account = _table_account(keys, authority=authority)

    def rpc(url, method, params):
        return {"getBlockHeight": height, "getSlot": 96, "getMinimumBalanceForRentExemption": 3_953_280,
                "getAccountInfo": {"value": account}}[method]
    monkeypatch.setattr(mod, "_rpc", rpc)
    monkeypatch.setattr(mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    argv = ["--keypair", str(key), "--state", str(state)] + (["--confirm"] if verdict == "confirmed" else [])
    assert mod.main(argv) == code
    out = capsys.readouterr().out
    after = json.loads(state.read_text())
    if verdict == "confirmed":
        assert after["status"] == "active" and after["address"] == TABLE and "DONE" in out
    else:
        assert after == pending                                      # untouched
    assert ("Dry run. Nothing signed or sent." in out) == new_attempt


# --- N12 (Codex follow-up 2026-10-07, F-SOL-2): the decision and the creation share one lock -------

@pytest.mark.parametrize("start", ["absent", "expired_pending"])
def test_two_confirm_runs_that_both_start_before_either_locks_create_one_table(tmp_path, monkeypatch,
                                                                                capsys, start):
    """Codex's interleaving: run B goes to completion right as run A takes its first lock. Before
    the fix A had already read the state (absent, or an expired pending one) outside the lock and
    sent a second creation. Now A reads the state inside the lock and refuses B's active table."""
    import contextlib
    from sniper.execution import exec_lock
    from sniper.execution import solana_real_seams as seams
    from sniper.execution import solana_wallet
    from sniper.execution.solana_real_seams import ConfirmState
    from sniper.settings import memecoin as cfg
    mod = _script()
    key = tmp_path / "kp.json"
    kp = solana_wallet.generate_keypair_file(str(key))
    authority = str(kp.pubkey())
    keys = P.pump_alt_keys(authority)
    state = tmp_path / "alt.json"
    if start == "expired_pending":
        state.write_text(json.dumps({"status": "pending", "address": TABLE, "authority": authority,
                                     "keys": keys, "signature": "OLD", "recent_slot": 90,
                                     "last_valid_block_height": 100}))
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_RPC_URL", "http://unused")
    sends, slot = [], [100]
    monkeypatch.setattr(seams, "rpc_send", lambda raw, url: sends.append(raw) or True)
    monkeypatch.setattr(seams, "rpc_confirm", lambda sig, url, **k: (
        ConfirmState("not_found") if sig == "OLD" else ConfirmState("confirmed", landing_slot=slot[0])))
    monkeypatch.setattr(P, "verified_pump_alt", lambda address, account, user: (object(), None))

    def rpc(url, method, params):
        if method == "getSlot":
            slot[0] += 1
            return slot[0]
        return {"getBlockHeight": 500, "getMinimumBalanceForRentExemption": 3_953_280,
                "getAccountInfo": {"value": {"data": ["", "base64"]}},
                "getLatestBlockhash": {"value": {"blockhash": "11111111111111111111111111111111",
                                                 "lastValidBlockHeight": 600}}}[method]
    monkeypatch.setattr(mod, "_rpc", rpc)
    monkeypatch.setattr(mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    argv = ["--keypair", str(key), "--state", str(state), "--confirm"]
    first = [True]

    @contextlib.contextmanager
    def lock(key_, **kw):
        if first[0]:                       # run B, start to finish, as run A takes its first lock
            first[0] = False
            assert mod.main(argv) == 0
        yield True
    monkeypatch.setattr(exec_lock, "wallet_exec_lock", lock)
    code_a = mod.main(argv)
    assert len(sends) == 1, "two creations sent"
    assert code_a == 2 and "REFUSED" in capsys.readouterr().out
    assert json.loads(state.read_text())["status"] == "active"
