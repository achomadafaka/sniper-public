"""The wrap script on BSC (--chain 56) and its reserve (2026-09-28, operator via Alpha).

The funded runner sizes BSC entries from WBNB (CHAIN_QUOTE["56"]); the custody wallet held 0.1912
BNB native and 0.0000045 WBNB, so every BSC clip was $0. The script wraps native above a reserve
taken from the runner's OWN BSC gas need. Every chain/key/sign/send seam is faked; no network.
"""
import contextlib
import io
import runpy
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
WALLET = "0x" + "12" * 20
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"


def _gas_cfg(monkeypatch):
    """Tonight's BSC gas settings (.env): entry 0.00005, approval 0.00002, exit 0.0006 BNB,
    retry factor 2, 2 concurrent on 56, top-up target 0.003, no per-chain floor."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "gas_entry_native_for", lambda chain: 0.00005)
    monkeypatch.setattr(cfg, "gas_approval_native_for", lambda chain: 0.00002)
    monkeypatch.setattr(cfg, "gas_exit_native_for", lambda chain: 0.0006)
    monkeypatch.setattr(cfg, "gas_reserve_min_native_for", lambda chain: 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_GAS_RETRY_FACTOR", 2.0)
    monkeypatch.setattr(cfg, "max_concurrent_for", lambda chain, **kw: 2 if str(chain) == "56" else 3)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", 3 * 10 ** 15)
    return cfg


@pytest.fixture
def wrap(tmp_path, monkeypatch):
    from sniper.execution import evm_wallet, evm_real_seams
    from sniper.persistence import db_memecoin as m
    from sniper.util import envfiles
    module = runpy.run_path(str(ROOT / "scripts/memecoin_robinhood_wrap_excess_gas.py"))
    st = {"sent": [], "signed": [], "chain_id": 56, "native": 191_200_000_000_000_000,
          "rpc_urls": []}
    (tmp_path / "data").mkdir()
    ledger = sqlite3.connect(tmp_path / "ledger.db"); ledger.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(ledger); ledger.close()
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(envfiles, "load_env_files", lambda *a: None)
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_56", "offline://bsc")
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "offline://robinhood")
    _gas_cfg(monkeypatch)
    monkeypatch.setattr(evm_wallet, "load_keypair", lambda *a: SimpleNamespace(address=WALLET))

    def rpc(url, method, *a, **k):
        st["rpc_urls"].append(url)
        if method == "eth_chainId":
            return {"result": hex(st["chain_id"])}
        return {"result": hex(30000)}
    monkeypatch.setattr(evm_wallet, "_rpc", rpc)
    monkeypatch.setattr(evm_wallet, "native_balance_raw_or_none", lambda *a: st["native"])
    monkeypatch.setattr(evm_wallet, "erc20_balance_raw", lambda *a: 4_500_000_000_000)
    monkeypatch.setattr(evm_real_seams, "eth_nonce", lambda *a: 7)
    monkeypatch.setattr(evm_real_seams, "eth_gas_price", lambda *a: 100_000_000)       # 0.1 gwei
    monkeypatch.setattr(evm_real_seams, "sign_tx",
                        lambda tx, acct: st["signed"].append(tx) or ("RAW", "0xsig"))
    monkeypatch.setattr(evm_real_seams, "eth_send_raw", lambda *a: st["sent"].append(a) or "0xsig")
    monkeypatch.setattr(evm_real_seams, "eth_confirm", lambda *a, **kw: "confirmed")
    return module, st, tmp_path


def _run(module, monkeypatch, root, *extra):
    monkeypatch.setattr(sys, "argv", ["wrap", "--chain", "56", "--state-root", str(root),
                                      "--lock-wait-s", "0", *extra])
    with contextlib.redirect_stdout(io.StringIO()) as out:
        module["main"]()
    return out.getvalue()


# --- the reserve arithmetic ----------------------------------------------------------------------

def test_the_bsc_reserve_is_the_runners_own_gas_need_with_headroom(monkeypatch):
    cfg = _gas_cfg(monkeypatch)
    module = runpy.run_path(str(ROOT / "scripts/memecoin_robinhood_wrap_excess_gas.py"))
    reserve, terms = module["gas_need_reserve"](56, cfg)
    # book: entry+approval + (1 open + 1) x (exit+approval) x 2 = 0.00007 + 2 x 0.00062 x 2
    assert terms["book_need"] == pytest.approx(0.00255)
    # headroom: 3 round trips x (entry+approval+exit+approval) x 2 = 3 x 0.00069 x 2
    assert terms["headroom"] == pytest.approx(0.00414)
    assert reserve == pytest.approx(0.00669)
    from sniper.services.memecoin_evm_risk import required_native_gas
    one_more = required_native_gas(SimpleNamespace(
        gas_reserve_min_native=0.0, gas_entry_native=0.00005, gas_approval_native=0.00002,
        gas_exit_native=0.0006, gas_retry_factor=2.0, chain_concurrent=1))
    assert terms["book_need"] == pytest.approx(one_more), "the gate's own number for the last slot"


# The top-up target binds only on 4663, where the gas top-up pass runs: a raised target (the 4663
# native float, 0.145 ETH on 2026-09-28) must not become a BSC reserve. It refused a 0.02 BNB
# reserve as "under the runner's own gas need 0.1450" before this.
@pytest.mark.parametrize("target_wei, floor, expected", [
    (3 * 10 ** 15, 0.0, 0.00669), (10 ** 16, 0.0, 0.00669), (145 * 10 ** 15, 0.0, 0.00669),
    (3 * 10 ** 15, 0.02, 0.02)])
def test_the_56_reserve_ignores_the_4663_topup_target_and_keeps_the_chain_floor(
        monkeypatch, target_wei, floor, expected):
    cfg = _gas_cfg(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_GAS_TOPUP_TARGET_WEI", target_wei)
    monkeypatch.setattr(cfg, "gas_reserve_min_native_for", lambda chain: floor)
    module = runpy.run_path(str(ROOT / "scripts/memecoin_robinhood_wrap_excess_gas.py"))
    assert module["gas_need_reserve"](56, cfg)[0] == pytest.approx(expected)


def test_4663_keeps_the_topup_target_reserve(monkeypatch):
    cfg = _gas_cfg(monkeypatch)
    module = runpy.run_path(str(ROOT / "scripts/memecoin_robinhood_wrap_excess_gas.py"))
    assert module["default_reserve"](4663, cfg) == (0.003, {"topup_target": 0.003})


# --- the chain wiring ------------------------------------------------------------------------------

def test_the_bsc_dry_run_prints_the_terms_and_signs_nothing(wrap, monkeypatch):
    module, st, root = wrap
    out = _run(module, monkeypatch, root)
    assert "on BSC" in out and "native BNB 0.191200" in out and "WBNB 0.000005" in out
    assert "book_need=0.002550" in out and "headroom=0.004140" in out
    assert "Would wrap" in out and "BNB -> WBNB" in out
    assert st["signed"] == [] and st["sent"] == []
    assert set(st["rpc_urls"]) == {"offline://bsc"}, "only the chain-56 RPC is read"


def test_the_bsc_wrap_signs_the_excess_to_wbnb_on_chain_56(wrap, monkeypatch):
    from sniper.execution import exec_lock
    module, st, root = wrap
    taken = []
    real = exec_lock.wallet_exec_lock
    monkeypatch.setattr(exec_lock, "wallet_exec_lock",
                        lambda key, **kw: taken.append(key) or real(key, **kw))
    _run(module, monkeypatch, root, "--confirm")
    (tx,) = st["signed"]
    gas_wei = 60_000 * 100_000_000
    assert tx["to"] == WBNB and tx["data"] == "0xd0e30db0" and tx["chainId"] == 56
    assert tx["value"] == 191_200_000_000_000_000 - int(0.00669 * 1e18) - gas_wei
    assert taken == ["evm-56-" + WALLET], "the BSC executor's own lock key"
    receipts = list((root / "data").glob("bsc-wrap-*.json"))
    assert len(receipts) == 1 and '"chain": 56' in receipts[0].read_text()
    assert list((root / "data").glob("robinhood-wrap-*.json")) == []


def test_an_rpc_that_is_not_bsc_refuses(wrap, monkeypatch):
    module, st, root = wrap
    st["chain_id"] = 4663
    with pytest.raises(ValueError, match="does not serve chain 56"):
        _run(module, monkeypatch, root, "--confirm")
    assert st["signed"] == []


def test_a_reserve_under_the_gas_need_refuses(wrap, monkeypatch):
    module, st, root = wrap
    with pytest.raises(ValueError, match="gas need"):
        _run(module, monkeypatch, root, "--reserve-native", "0.004", "--confirm")
    assert st["signed"] == []


def test_a_bsc_reservation_in_flight_refuses_and_a_4663_one_does_not(wrap, monkeypatch):
    from sniper.persistence import db_memecoin as m
    module, st, root = wrap
    ledger = sqlite3.connect(root / "ledger.db"); ledger.row_factory = sqlite3.Row
    pid = m.reserve_pending(ledger, mint="0xrobin", source_user_id="u", strategy="s", notional=10.0,
                            reserved_at="2026-09-28T05:00:00Z", network_id="4663")
    _run(module, monkeypatch, root, "--confirm")
    assert len(st["sent"]) == 1, "another chain's reservation does not block BSC"
    m.release_pending(ledger, pid, "test")
    m.reserve_pending(ledger, mint="0xbsc", source_user_id="u", strategy="s", notional=10.0,
                      reserved_at="2026-09-28T05:00:00Z", network_id="56")
    ledger.close()
    (root / "data" / list((root / "data").glob("bsc-wrap-*.json"))[0].name).unlink()
    with pytest.raises(ValueError, match="in flight"):
        _run(module, monkeypatch, root, "--confirm")
    assert len(st["sent"]) == 1


def test_an_unreadable_ledger_blocks_a_signed_wrap(wrap, monkeypatch):
    module, st, root = wrap
    monkeypatch.setenv("DB_PATH", str(root / "missing.db"))
    with pytest.raises(ValueError, match="ledger could not be read"):
        _run(module, monkeypatch, root, "--confirm")
    out = _run(module, monkeypatch, root)
    assert "BLOCKING" in out and st["signed"] == []


def test_an_unresolved_robinhood_receipt_does_not_block_bsc(wrap, monkeypatch):
    module, st, root = wrap
    (root / "data" / "robinhood-wrap-x.json").write_text(
        '{"wallet": "%s", "chain": 4663, "state": "wrap_unknown", "wrap_sig": "0xr"}' % WALLET)
    _run(module, monkeypatch, root, "--confirm")
    assert len(st["sent"]) == 1


def test_an_unresolved_bsc_receipt_blocks_the_next_bsc_wrap(wrap, monkeypatch):
    from sniper.execution import evm_real_seams
    module, st, root = wrap
    monkeypatch.setattr(evm_real_seams, "eth_confirm", lambda *a, **kw: "unknown")
    _run(module, monkeypatch, root, "--confirm")
    with pytest.raises(ValueError, match="unresolved"):
        _run(module, monkeypatch, root, "--confirm")
    assert len(st["sent"]) == 1


def test_base_uses_its_own_gas_need_and_receipt_prefix(monkeypatch):
    module = runpy.run_path(str(ROOT / 'scripts/memecoin_robinhood_wrap_excess_gas.py'))
    assert module['CHAINS'][8453]['reserve'] == 'gas_need'
    assert module['CHAINS'][8453]['receipt_prefix'] == 'base-wrap-'
    cfg = _gas_cfg(monkeypatch)
    reserve, terms = module['default_reserve'](8453, cfg)
    assert reserve == module['gas_need_reserve'](8453, cfg)[0]
    assert terms['topup_target'] == 0.0


def test_wrap_cli_binds_generation_to_explicit_state_root(wrap,monkeypatch):
    from sniper.util.state_paths import state_root
    module,st,root=wrap
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(ROOT))
    _run(module,monkeypatch,root)
    assert state_root()==root
