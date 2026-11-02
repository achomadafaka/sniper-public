"""The PumpSwap native exit transaction, built against a RECORDED pool state (Alpha GO 2026-10-17).

The pool, its vaults, its base mint and GlobalConfig are the accounts read on chain for the fixture
(tests/fixtures/memecoin/pumpswap_sell_instructions.json, the 24-account creator-layout pool). The RPC
and Jupiter are fakes; simulateTransaction returns explicitly mocked success/error responses.
These tests prove assembly and refusal behavior, never live simulation or execution."""
import base64
import json
from pathlib import Path

import pytest

from sniper.execution import pumpswap_sell as PS

FIXTURE = json.loads((Path(__file__).parent / "fixtures/memecoin/pumpswap_sell_instructions.json").read_text())
SELL24 = [s for s in FIXTURE["sells"] if len(s["accounts"]) == 24][0]["accounts"]
POOL, BASE_VAULT, QUOTE_VAULT, MINT = SELL24[0], SELL24[7], SELL24[8], SELL24[3]
USER = "5ChwZsPCRXW3bSuWQbjGZfYTnnALg5iaKqCMLzxMRUhz"
STATE = FIXTURE["state"]
WSOL_MINT_ACCOUNT = {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                     "data": [base64.b64encode(bytes(44) + bytes([9, 1]) + bytes(36)).decode(), "base64"]}
SIM_OK = {"value": {"err": None, "logs": ["Program log: Instruction: Sell"]}}
SIM_6004 = {"value": {"err": {"InstructionError": [3, {"Custom": 6004}]}, "logs": ["Program log: failed"]}}


class Rpc:
    def __init__(self, sim=SIM_OK, pool_owner=None):
        self.sim, self.pool_owner, self.calls = sim, pool_owner, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "getMultipleAccounts" and params[0][0] == POOL:
            pool = dict(STATE["pool24"]["value"][0])
            if self.pool_owner:
                pool["owner"] = self.pool_owner
            return {"result": {"context": {"slot": 1}, "value": [pool, STATE["global"]["value"][0]]}}
        if method == "getMultipleAccounts":
            v = STATE["pool24"]["value"]
            return {"result": {"context": {"slot": STATE["pool24"]["context"]["slot"]},
                               "value": [v[1], v[2], v[3], WSOL_MINT_ACCOUNT]}}
        if method == "getLatestBlockhash":
            return {"result": {"value": {"blockhash": "11111111111111111111111111111111",
                                         "lastValidBlockHeight": 7}}}
        if method == "simulateTransaction":
            return {"result": self.sim}
        raise AssertionError(method)


def jup(method, path, body=None):
    if method == "GET":
        from urllib.parse import parse_qs, urlsplit
        amount = parse_qs(urlsplit(path).query)["amount"][0]
        return 200, {"inputMint": PS.WSOL_MINT, "outputMint": PS.USDC_MINT, "inAmount": amount,
                     "outAmount": "4200000", "otherAmountThreshold": "4100000", "swapMode": "ExactIn",
                     "slippageBps": 300, "routePlan": [{"swapInfo": {"label": "x", "ammKey": "y"}}]}
    return 200, {"swapInstruction": {"programId": "11111111111111111111111111111111", "data": "",
                                     "accounts": [{"pubkey": USER, "isSigner": True, "isWritable": True}]}}


def _rpc_fn(rpc):
    return lambda method, params: (rpc(method, params) or {}).get("result")


def _build(rpc, *, mint=MINT, amount=5_794_837_521):
    return PS.build_pumpswap_native_exit(mint, amount, 3.0, user=USER, pool=POOL, rpc=rpc,
                                         jup_call=jup, fee_bps=150)


def _instructions(quote):
    from solders.transaction import VersionedTransaction
    tx = VersionedTransaction.from_bytes(base64.b64decode(quote["_native_swap"]["swapTransaction"]))
    keys = [str(k) for k in tx.message.account_keys]
    return [(keys[ix.program_id_index], [keys[i] for i in ix.accounts], bytes(ix.data))
            for ix in tx.message.instructions]


def test_a_recorded_pool_builds_a_simulated_sell_of_the_exact_balance():
    rpc = Rpc()
    quote = _build(rpc)
    assert quote is not None and quote["_venue"] == "pumpswap_native"
    assert rpc.calls[-1] == "simulateTransaction", "simulated before it is handed back"
    sells = [ix for ix in _instructions(quote) if ix[0] == PS.AMM_PROGRAM]
    assert len(sells) == 1
    program, accounts, data = sells[0]
    assert data[:8] == PS.SELL_DISCRIMINATOR and int.from_bytes(data[8:16], "little") == 5_794_837_521
    floor = int.from_bytes(data[16:24], "little")
    assert floor == quote["_pool_leg"]["min_out"] > 0
    assert len(accounts) == 24 and accounts[0] == POOL and accounts[3] == MINT
    # the floor sits under the chain's own answer for this very sell (9,503,441 lamports, SellEvent)
    assert 0.9 * 9_503_441 < floor < 9_503_441
    ixs = _instructions(quote)
    assert ixs[-1][0] == "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA" and ixs[-1][2] == bytes([9]), \
        "a SOL pool closes the wSOL ATA last, unwrapping the surplus"


def test_a_simulation_error_is_no_exit():
    assert _build(Rpc(sim=SIM_6004)) is None


@pytest.mark.parametrize("why", ["not_owned", "wrong_mint", "zero"])
def test_no_exit_for_a_pool_that_is_not_the_coins_pumpswap_pool_or_no_balance(why):
    if why == "not_owned":
        assert _build(Rpc(pool_owner="LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo")) is None
    elif why == "wrong_mint":
        assert _build(Rpc(), mint="So11111111111111111111111111111111111111112") is None
    else:
        assert _build(Rpc(), amount=0) is None


def test_missing_simulation_success_is_unknown():
    assert _build(Rpc(sim={"value": {"logs": []}})) is None


def test_tail_quote_must_bind_the_requested_input_and_amount():
    def wrong_tail(method, path, body=None):
        status, data = jup(method, path, body)
        if method == "GET":
            data["inAmount"] = "1"
        return status, data
    assert PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3, user=USER, pool=POOL,
                                         rpc=Rpc(), jup_call=wrong_tail, fee_bps=150) is None


@pytest.mark.parametrize("broken", ["global_owner", "vault_mint", "vault_authority", "mint_owner", "old_slot"])
def test_pool_state_refuses_inconsistent_account_identity(broken):
    import copy
    from solders.pubkey import Pubkey
    rpc = Rpc()
    def read(method, params):
        result = copy.deepcopy(rpc(method, params))
        if method != "getMultipleAccounts":
            return result
        values = result["result"]["value"]
        if params[0][0] == POOL:
            if broken == "global_owner":
                values[1]["owner"] = PS.SYSTEM_PROGRAM
        elif broken in ("vault_mint", "vault_authority"):
            raw = bytearray(base64.b64decode(values[0]["data"][0]))
            offset = 0 if broken == "vault_mint" else 32
            raw[offset:offset + 32] = bytes(Pubkey.from_string(USER))
            values[0]["data"][0] = base64.b64encode(raw).decode()
        elif broken == "mint_owner":
            values[2]["owner"] = PS.SYSTEM_PROGRAM
        elif broken == "old_slot":
            result["result"]["context"]["slot"] = 0
        return result
    assert PS.read_pool_state(POOL, rpc=lambda m, p: read(m, p)["result"]) is None


def test_entry_bound_coin_vault_resolves_its_actual_pumpswap_pool():
    rpc = Rpc()
    def read(method, params):
        if method == "getMultipleAccounts" and len(params[0]) == 2 and params[0][0] == BASE_VAULT:
            return {"result": {"context": {"slot": 1}, "value": [STATE["pool24"]["value"][1],
                                                                     STATE["global"]["value"][0]]}}
        return rpc(method, params)
    quote = PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3, user=USER, pool=BASE_VAULT,
                                          rpc=read, jup_call=jup, fee_bps=150)
    assert quote is not None
    assert quote["_pool_leg"]["pool"] == POOL
    assert [ix for ix in _instructions(quote) if ix[0] == PS.AMM_PROGRAM][0][1][0] == POOL


def _native_quote():
    return {"_venue": PS.VENUE, "inputMint": MINT, "inAmount": "5", "outputMint": PS.USDC_MINT,
            "outAmount": "10000000", "otherAmountThreshold": "9000000", "routePlan": [],
            "_pool_leg": {"pool": POOL, "min_out": 9, "expected_out": 10},
            "_native_swap": {"swapTransaction": "AAAA", "lastValidBlockHeight": 7}}


def test_shared_swap_builder_uses_prebuilt_native_exit_without_jupiter(monkeypatch):
    from sniper.execution import solana_real_seams as S
    def refuse(*args, **kwargs):
        raise AssertionError("prebuilt native exit must not be sent to Jupiter")
    monkeypatch.setattr(S, "_jup_build_once", refuse)
    quote = _native_quote()
    assert S.jupiter_swap_tx(quote, USER, return_metadata=True) == quote["_native_swap"]


@pytest.mark.parametrize("jupiter_usdc", [None, 1_000_000, 9_800_000])
def test_exact_preview_and_execution_choose_native_for_blind_or_severely_divergent_route(monkeypatch, jupiter_usdc):
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", True)
    monkeypatch.setattr(S, "bound_pumpswap_pool", lambda *a, **k: POOL)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 5)
    native = _native_quote()
    monkeypatch.setattr(PS, "build_pumpswap_native_exit", lambda *a, **k: native)
    quote = None if jupiter_usdc is None else dict(native, _venue="jupiter", outAmount=str(jupiter_usdc))
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: quote)
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    monkeypatch.setattr(S, "_typed_jupiter_exit_quote", lambda *a, **k: (quote, None if quote else QuoteFailure.NO_ROUTE))
    ex = S.build_real_solana_executor(keypair=Keypair(), rpc_url="https://custody.invalid",
                                      http=lambda *a, **k: (200, {}, None),
                                      usdc_fn=lambda: 0, token_fn=lambda m: 5,
                                      native_exit_hint_fn=lambda *a: True)
    chosen = ex["exit_quote_fn"](MINT, 5, 3)
    preview = ex["preview_exit_fn"]({"id": 42, "mint": MINT})
    wants_native = jupiter_usdc is None or jupiter_usdc == 1_000_000
    assert chosen is (native if wants_native else quote)
    assert preview.expected_usdc == (10 if wants_native else 9.8)
    assert preview.route_id == (PS.VENUE if wants_native else "jupiter")


def test_a_direct_usdc_pool_needs_no_jupiter_tail():
    import copy
    from solders.pubkey import Pubkey
    rpc = Rpc()
    def read(method, params):
        response = copy.deepcopy(rpc(method, params))
        if method == "getMultipleAccounts":
            values = response["result"]["value"]
            i, offset = (0, 75) if params[0][0] == POOL else (1, 0)
            raw = bytearray(base64.b64decode(values[i]["data"][0]))
            raw[offset:offset + 32] = bytes(Pubkey.from_string(PS.USDC_MINT))
            values[i]["data"][0] = base64.b64encode(raw).decode()
            if params[0][0] != POOL:
                mint_raw = bytearray(base64.b64decode(values[3]["data"][0]))
                mint_raw[44] = 6
                values[3]["data"][0] = base64.b64encode(mint_raw).decode()
        return response
    def no_tail(*args, **kwargs):
        raise AssertionError("USDC is already the settlement asset")
    quote = PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3, user=USER, pool=POOL,
                                          rpc=read, jup_call=no_tail, fee_bps=150)
    assert quote is not None
    assert int(quote["otherAmountThreshold"]) == quote["_pool_leg"]["min_out"]
    assert int(quote["outAmount"]) == quote["_pool_leg"]["expected_out"]
    assert len(quote["routePlan"]) == 1
    assert _instructions(quote)[-1][0] == PS.AMM_PROGRAM


def test_pool_binding_is_pinned_to_the_position_and_ambiguous_mint_is_unavailable(tmp_path, monkeypatch):
    import sqlite3
    from sniper.execution import solana_real_seams as S
    db = tmp_path / "tape.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sol_position_pools(position_id INTEGER PRIMARY KEY,mint TEXT,pool TEXT,venue_kind TEXT)")
    conn.executemany("INSERT INTO sol_position_pools VALUES(?,?,?,?)", [(1, MINT, BASE_VAULT, "amm"),
                                                                     (2, MINT, USER, "amm")])
    conn.commit(); conn.close()
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_DB", str(db))
    assert S.bound_pumpswap_pool(MINT, position_id=1) == BASE_VAULT
    assert S.bound_pumpswap_pool(MINT, position_id=2) == USER
    assert S.bound_pumpswap_pool(MINT, position_id=3) is None
    assert S.bound_pumpswap_pool(MINT) is None


def test_tail_atomic_amounts_cannot_be_fractional_numbers():
    def fractional_tail(method, path, body=None):
        status, data = jup(method, path, body)
        if method == "GET":
            data["inAmount"] = float(data["inAmount"]) + 0.5
        return status, data
    assert PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3, user=USER, pool=POOL,
                                         rpc=Rpc(), jup_call=fractional_tail, fee_bps=150) is None


def test_native_exit_uses_existing_durable_send_fence_and_unknown_never_resends(monkeypatch):
    import sqlite3
    from solders.keypair import Keypair
    from sniper.execution import solana_real_seams as S
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    keypair = Keypair()
    def tail(method, path, body=None):
        status, payload = jup(method, path, body)
        if method == "POST":
            payload["swapInstruction"]["accounts"][0]["pubkey"] = str(keypair.pubkey())
        return status, payload
    native = PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3, user=str(keypair.pubkey()), pool=POOL,
                                           rpc=Rpc(), jup_call=tail, fee_bps=150)
    assert native is not None
    conn = sqlite3.connect(":memory:"); conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = m.open_position(conn, source_user_id="u", source_handle="unit", mint=MINT, ticker="coin",
                          entry_price=0.001, entry_notional=5.79, entry_fill_source="chain", entry_lag_s=0,
                          their_size_usd=1, opened_at="2026-10-17T00:00:00Z", strategy="alpha_b",
                          execution_mode="real", network_id="solana")
    m.insert_fill(conn, position_id=pid, kind="entry", price=0.001, qty=5794.837521, notional=5.79,
                  fill_source="chain", fraction=1, created_at="2026-10-17T00:00:00Z")
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", True)
    monkeypatch.setattr(S, "bound_pumpswap_pool", lambda *a, **k: POOL)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 5_794_837_521)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: None)
    monkeypatch.setattr(PS, "build_pumpswap_native_exit", lambda *a, **k: native)
    sends = []
    def send(*args, **kwargs):
        current = m.get_position(conn, pid)
        assert current["is_open"] == 1 and current["close_claim_id"] and current["close_sig"]
        intent = json.loads(current["exit_intent_json"])
        assert intent["signed_payload_digest"] and intent["tx_identity"] == current["close_sig"]
        sends.append(current["close_sig"])
    monkeypatch.setattr(S, "rpc_send", send)
    monkeypatch.setattr(S, "rpc_confirm", lambda *a, **k: "timeout")
    def no_fast_build(*args, **kwargs):
        raise AssertionError("native bytes must bypass the Jupiter fast builder")
    executor = S.build_real_solana_executor(keypair=keypair, rpc_url="https://custody.invalid",
                                            usdc_fn=lambda: 0, token_fn=lambda m: 5794.837521,
                                            fast_tx_builder=no_fast_build)
    result = executor["exit_fn"](conn, m.get_position(conn, pid), "mirror", "2026-10-17T01:00:00Z")
    assert result["status"] == "unknown" and len(sends) == 1
    result = executor["exit_fn"](conn, m.get_position(conn, pid), "mirror", "2026-10-17T01:00:01Z")
    assert result["status"] == "already_closing" and len(sends) == 1
    assert m.get_position(conn, pid)["is_open"] == 1


# A successful simulation can spend old quote inventory if a Token-2022 transfer fee
# makes the pool's pre-transfer minimum larger than this sell's actual quote credit.
QUOTE_2022 = '11111111111111111111111111111112'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'


def _mint_extension(kind, payload):
    import struct
    return struct.pack('<HH', kind, len(payload)) + payload


def _quote_2022_mint(*extensions):
    return bytes(44) + bytes([6, 1]) + bytes(36 + 83) + bytes([1]) + b''.join(extensions)


def _zero_fee_config(*, older_bps=0, newer_bps=0, maximum=100_000):
    import struct
    # TransferFeeConfig: authorities32+32, withheld8, then two packed (epoch8,max8,bps2).
    return bytes(72) + struct.pack('<QQHQQH', 0, maximum, older_bps, 100, maximum, newer_bps)


def _build_with_quote_mint(raw_mint, program=TOKEN_2022):
    import copy
    from solders.pubkey import Pubkey
    rpc, tail_calls = Rpc(), []
    def read(method, params):
        reply = copy.deepcopy(rpc(method, params))
        if method == 'getMultipleAccounts':
            values = reply['result']['value']
            pool_read = params[0][0] == POOL
            index, offset = (0, 75) if pool_read else (1, 0)
            raw = bytearray(base64.b64decode(values[index]['data'][0]))
            raw[offset:offset + 32] = bytes(Pubkey.from_string(QUOTE_2022))
            values[index]['data'][0] = base64.b64encode(raw).decode()
            if not pool_read:
                values[1]['owner'] = program
                values[3] = {'owner': program, 'data': [base64.b64encode(raw_mint).decode(), 'base64']}
        return reply
    def tail(method, path, body=None):
        tail_calls.append(method)
        status, payload = jup(method, path, body)
        if method == 'GET':
            payload['inputMint'] = QUOTE_2022
        return status, payload
    quote = PS.build_pumpswap_native_exit(MINT, 5_794_837_521, 3., user=USER, pool=POOL,
                                          rpc=read, jup_call=tail, fee_bps=150)
    return quote, rpc.calls, tail_calls


@pytest.mark.parametrize('older,newer', [(300, 300), (0, 300), (300, 0)])
def test_native_quote_transfer_fee_refuses_before_tail_even_when_simulation_would_succeed(older, newer):
    raw = _quote_2022_mint(_mint_extension(1, _zero_fee_config(older_bps=older, newer_bps=newer)))
    quote, rpc_calls, tail_calls = _build_with_quote_mint(raw)
    assert quote is None
    assert not tail_calls and 'simulateTransaction' not in rpc_calls


@pytest.mark.parametrize('raw', [
    _quote_2022_mint(),
    _quote_2022_mint(_mint_extension(1, _zero_fee_config())),
    _quote_2022_mint(_mint_extension(1, _zero_fee_config(older_bps=300, newer_bps=300, maximum=0))),
    _quote_2022_mint(_mint_extension(18, bytes(64))),
    _quote_2022_mint(_mint_extension(19, bytes(80))),
    _quote_2022_mint(_mint_extension(3, bytes(32))),
    _quote_2022_mint()[:82],  # plain Token-2022 Mint has no padding/type/TLV
], ids=['extended_empty', 'zero_bps', 'zero_max', 'metadata_pointer', 'metadata', 'mint_close', 'plain'])
def test_zero_fee_and_harmless_quote_mints_still_build_without_more_reads(raw):
    quote, rpc_calls, tail_calls = _build_with_quote_mint(raw)
    assert quote is not None
    assert rpc_calls == ['getMultipleAccounts', 'getMultipleAccounts', 'getLatestBlockhash', 'simulateTransaction']
    assert tail_calls == ['GET', 'POST']


@pytest.mark.parametrize('raw', [
    _quote_2022_mint(_mint_extension(14, bytes(64))),  # TransferHook
    _quote_2022_mint(_mint_extension(12, bytes(32))),  # PermanentDelegate
    _quote_2022_mint(_mint_extension(6, bytes([2]))),  # frozen default account state
    _quote_2022_mint(_mint_extension(65535, b'')),
    _quote_2022_mint(_mint_extension(1, bytes(107))),  # truncated fee state
    _quote_2022_mint(_mint_extension(1, bytes(109))),  # oversized fee state
    _quote_2022_mint(_mint_extension(18, bytes(63))),
    _quote_2022_mint(_mint_extension(18, bytes(65))),
    _quote_2022_mint(_mint_extension(3, bytes(31))),
    _quote_2022_mint(_mint_extension(3, bytes(33))),
    _quote_2022_mint(_mint_extension(19, bytes(79))),
    _quote_2022_mint(_mint_extension(18, bytes(64)), _mint_extension(18, bytes(64))),
    _quote_2022_mint() + bytes([1, 0, 108]),  # truncated TLV length header
    _quote_2022_mint() + bytes([1, 0, 108, 0]),  # truncated TLV value
    _quote_2022_mint()[:165],  # missing Mint account type
    _quote_2022_mint()[:165] + bytes([2]),  # token ACCOUNT, not a Mint
    _quote_2022_mint()[:82] + bytes([1]) + bytes(82) + bytes([1]),  # nonzero padding
    _quote_2022_mint() + bytes([0, 0, 0, 0, 1]),  # hidden data after terminator
], ids=['hook', 'delegate', 'frozen', 'unknown', 'fee_short', 'fee_long', 'pointer_short',
        'pointer_long', 'close_short', 'close_long', 'metadata_short', 'duplicate', 'header_short', 'value_short', 'type_missing', 'account_type',
        'padding', 'hidden_after_terminator'])
def test_unknown_or_malformed_quote_extensions_refuse_before_tail(raw):
    quote, rpc_calls, tail_calls = _build_with_quote_mint(raw)
    assert quote is None
    assert not tail_calls and 'simulateTransaction' not in rpc_calls


def test_plain_legacy_quote_mint_preserves_native_exit():
    quote, rpc_calls, tail_calls = _build_with_quote_mint(_quote_2022_mint()[:82],
        program='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
    assert quote is not None and tail_calls == ['GET', 'POST']
    assert rpc_calls[-1] == 'simulateTransaction'


def test_legacy_quote_with_extension_bytes_is_not_a_legacy_mint():
    quote, rpc_calls, tail_calls = _build_with_quote_mint(_quote_2022_mint(),
        program='TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA')
    assert quote is None and not tail_calls and 'simulateTransaction' not in rpc_calls
