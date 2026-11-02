"""A curve-bound position whose coin GRADUATES mid-hold gets the native PumpSwap exit on the canonical pool
(Alpha/operator 2026-10-23). Before: bound_pumpswap_pool found no 'amm' row and Jupiter (2-9 s late on a fresh
pool) was the only exit. The fixture's recorded pool IS the canonical PDA of its mint."""
import sqlite3

import pytest

from sniper.execution import pumpswap_sell as PS
from sniper.execution import solana_real_seams as S
from sniper.execution.solana_native_entry_quote import accounts_for
from test_pumpswap_native_exit import MINT, POOL, BASE_VAULT, USER, Rpc, _native_quote, jup  # noqa: F401


@pytest.fixture
def tape(tmp_path, monkeypatch):
    db = tmp_path / "tape.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sol_position_pools(position_id INTEGER PRIMARY KEY,mint TEXT,pool TEXT,venue_kind TEXT)")
    conn.executemany("INSERT INTO sol_position_pools VALUES(?,?,?,?)", [
        (1, MINT, "CurveVault111111111111111111111111111111111", "pump_curve"),
        (2, MINT, BASE_VAULT, "amm"),
        (3, "OtherMint1111111111111111111111111111111111", "x", "pump_curve")])
    conn.commit(); conn.close()
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_DB", str(db))


def test_the_fixture_pool_is_the_canonical_pda():
    assert accounts_for(MINT)["pool"] == POOL


def test_only_a_curve_bound_position_of_this_mint_gets_the_canonical_pool(tape):
    assert S.graduated_curve_pool(MINT, position_id=1) == POOL
    assert S.graduated_curve_pool(MINT, position_id=2) is None          # amm-bound: bound_pumpswap_pool owns it
    assert S.graduated_curve_pool(MINT, position_id=3) is None          # binding names another mint
    assert S.graduated_curve_pool(MINT, position_id=9) is None          # unbound
    assert S.graduated_curve_pool(MINT, position_id=None) is None


def test_unreadable_binding_is_no_pool(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMECOIN_SOL_TAPE_DB", str(tmp_path / "missing" / "tape.db"))
    assert S.graduated_curve_pool(MINT, position_id=1) is None


def _executor(monkeypatch, rpc):
    from solders.keypair import Keypair
    from sniper.settings import memecoin as cfg
    from sniper.analysis.memecoin_quote_failures import QuoteFailure
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_NATIVE_EXIT", True)
    monkeypatch.setattr(S, "token_balance_raw", lambda *a, **k: 5_794_837_521)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *a, **k: None)                  # Jupiter not indexed yet
    monkeypatch.setattr(S, "_typed_jupiter_exit_quote", lambda *a, **k: (None, QuoteFailure.NO_ROUTE))
    monkeypatch.setattr(S, "unbuildable_curve_quote_asset", lambda *a, **k: None, raising=False)
    seen = []
    real = PS.build_pumpswap_native_exit

    def spy(mint, amount, slip, **kw):
        seen.append(kw["pool"])
        kw["rpc"] = lambda method, params: (rpc(method, params) or {}).get("result")
        return real(mint, amount, slip, **kw)
    monkeypatch.setattr(PS, "build_pumpswap_native_exit", spy)
    kp = Keypair()

    def echo_jup(method, path, body=None, http=None):           # the fixture fake pins 300 bps and its own user
        status, out = jup(method, path, body)
        if method != "GET":
            out = {"swapInstruction": dict(out["swapInstruction"], accounts=[
                {"pubkey": str(kp.pubkey()), "isSigner": True, "isWritable": True}])}
        if method == "GET":
            from urllib.parse import parse_qs, urlsplit
            out = dict(out, slippageBps=int(parse_qs(urlsplit(path).query)["slippageBps"][0]))
        return status, out
    monkeypatch.setattr(S, "_jup_call", echo_jup)
    ex = S.build_real_solana_executor(keypair=kp, rpc_url="https://custody.invalid",
                                      http=lambda *a, **k: (200, {}, None), usdc_fn=lambda: 0,
                                      token_fn=lambda m: 5_794_837_521)
    return ex, seen


def test_preview_of_a_graduated_curve_position_builds_the_native_exit_on_the_canonical_pool(tape, monkeypatch):
    S._GRADUATED_MISS.clear()
    ex, seen = _executor(monkeypatch, Rpc())
    preview = ex["preview_exit_fn"]({"id": 1, "mint": MINT})
    assert seen == [POOL]
    assert preview.route_id == PS.VENUE and preview.expected_usdc > 0


def test_before_graduation_the_pool_does_not_exist_and_the_exit_is_todays(tape, monkeypatch):
    S._GRADUATED_MISS.clear()
    class NoPool(Rpc):
        def __call__(self, method, params):
            if method == "getMultipleAccounts" and params[0][0] == POOL:
                return {"result": {"context": {"slot": 1}, "value": [None, None]}}
            return super().__call__(method, params)
    ex, seen = _executor(monkeypatch, NoPool())
    preview = ex["preview_exit_fn"]({"id": 1, "mint": MINT})
    assert seen == [POOL] and preview.route_id != PS.VENUE                  # tried, refused, Jupiter's failure stands


def test_a_not_graduated_miss_is_not_read_again_for_ten_seconds(tape, monkeypatch):
    class NoPool(Rpc):
        def __call__(self, method, params):
            if method == "getMultipleAccounts" and params[0][0] == POOL:
                return {"result": {"context": {"slot": 1}, "value": [None, None]}}
            return super().__call__(method, params)
    S._GRADUATED_MISS.clear()
    ex, seen = _executor(monkeypatch, NoPool())
    ex["preview_exit_fn"]({"id": 1, "mint": MINT})
    ex["preview_exit_fn"]({"id": 1, "mint": MINT})
    assert seen == [POOL]                                                    # second attempt: no pool read
    S._GRADUATED_MISS[MINT] -= 11
    ex["preview_exit_fn"]({"id": 1, "mint": MINT})
    assert seen == [POOL, POOL]                                              # after 10 s: tried again
