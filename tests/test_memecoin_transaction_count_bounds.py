"""Transaction floors count identities; side/volume evidence still counts swap logs."""
import pytest

from sniper.services import memecoin_bsc_v2_live as bsc
from sniper.services import memecoin_onchain_volume as volume
from sniper.services import memecoin_real_runner as runner
from test_memecoin_bsc_own_pool_e7 import _store, MINT, WBNB, SOURCE
from test_memecoin_onchain_volume import _conn, _price, CHAIN, POOL, AT


@pytest.mark.parametrize("distinct", [False, True])
def test_bsc_same_pool_logs_do_not_inflate_the_transaction_floor(tmp_path, distinct):
    other = "0x" + "22" * 32
    third = "0x" + "23" * 32 if distinct else other
    c = _store(tmp_path, "0x" + "cc" * 20,
               [(SOURCE, 10**18), (other, 10**18), (third, -10**18)])
    evidence = bsc.m5_bound(c, "0x" + "cc" * 20, WBNB, MINT, SOURCE, 1000.5)
    c.close()
    assert evidence["swaps"] == 2 and evidence["buys"] == evidence["sells"] == 1
    bound = runner.onchain_m5_txns_lower_bound(evidence)
    assert bound == (2 if distinct else 1)
    allowed, _, reason = runner.volume_alive_decision(
        None, 50_000, abs_floor=0, turnover_floor=0,
        m5_txns_floor=2, m5_txns_required=True, m5_txns_lower_bound=bound)
    assert (allowed, reason) == ((True, "own_tape_proven") if distinct
                                  else (False, "m5_txns_unreadable"))


@pytest.mark.parametrize("other", ["0xother", "0xOTHER"])
def test_evm_same_pool_logs_count_one_transaction_and_exclude_source(other):
    from test_memecoin_onchain_volume import MINT as evm_mint
    c = _conn([(100, "0xSOURCE", 500), (100, "0xother", 100), (100, other, -100)])
    evidence = volume.onchain_volume_m5(c, CHAIN, evm_mint, AT,
                                        exclude_tx="0xsource", price_fn=_price)
    c.close()
    assert evidence["swaps"] == 2 and evidence["usd"] == 200
    assert evidence["pools"][0]["buys"] == evidence["pools"][0]["sells"] == 1
    assert runner.onchain_m5_txns_lower_bound(evidence) == 1


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_transaction_identity_cannot_prove_a_count(missing):
    from test_memecoin_onchain_volume import MINT as evm_mint
    c = _conn([(100, missing, 100), (100, missing, 100)])
    evidence = volume.onchain_volume_m5(c, CHAIN, evm_mint, AT, price_fn=_price)
    c.close()
    assert evidence["swaps"] == 2 and evidence["usd"] == 200
    assert runner.onchain_m5_txns_lower_bound(evidence) is None


def test_count_is_one_pools_distinct_transactions_and_legacy_rows_stay_unknown():
    evidence = {"reason": "ok", "usd": 100, "swaps": 9,
                "pools": [{"pool": "a", "swaps": 5, "txns": 2},
                          {"pool": "b", "swaps": 4, "txns": 3}]}
    assert runner.onchain_m5_txns_lower_bound(evidence) == 3
    assert runner.onchain_m5_txns_lower_bound({"reason": "ok", "usd": 100, "swaps": 9}) is None
    assert runner.onchain_m5_txns_lower_bound({**evidence, "unverified_swaps": 1}) is None
    assert runner.onchain_m5_txns_lower_bound({"reason": "ok", "usd": 100, "txns": 9,
                                              "pools": [{"pool": "a", "swaps": 9}]}) is None


def test_bsc_source_identity_is_case_insensitive(tmp_path):
    pool = "0x" + "cc" * 20
    source = "0x" + "ab" * 32
    c = _store(tmp_path, pool, [(source, 10**18), ("0x" + "22" * 32, 10**18)])
    c.execute("UPDATE swaps SET tx=? WHERE tx=?", (source.upper(), source))
    evidence = bsc.m5_bound(c, pool, WBNB, MINT, source, 1000.5)
    c.close()
    assert evidence["reason"] == "ok" and evidence["swaps"] == 1
    assert runner.onchain_m5_txns_lower_bound(evidence) == 1


def test_bsc_missing_identity_is_unknown_while_swap_evidence_remains(tmp_path):
    pool = "0x" + "cc" * 20
    c = _store(tmp_path, pool, [(SOURCE, 10**18), ("0x" + "22" * 32, 10**18)])
    c.execute("UPDATE swaps SET tx=NULL WHERE tx!=?", (SOURCE,))
    evidence = bsc.m5_bound(c, pool, WBNB, MINT, SOURCE, 1000.5)
    c.close()
    assert evidence["swaps"] == 1 and evidence["usd"] == 600
    assert runner.onchain_m5_txns_lower_bound(evidence) is None


def test_foreign_chain_pool_and_orphan_do_not_add_transactions():
    from test_memecoin_onchain_volume import MINT as evm_mint
    c = _conn([(100, "0xsource", 100), (100, "0xother", 100)],
              orphans=[(100, "0xorphan", 100)])
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,"
              "amount0,amount1,observed_at,block_hash) "
              "SELECT ?,?,kind,block_no,?,100,amount0,amount1,observed_at,block_hash "
              "FROM pool_ticks WHERE tx_sig='0xother'", ("8453", POOL, "0xforeign"))
    c.execute("INSERT INTO pool_ticks (chain_id,pool,kind,block_no,tx_sig,log_index,"
              "amount0,amount1,observed_at,block_hash) "
              "SELECT chain_id,?,kind,block_no,?,101,amount0,amount1,observed_at,block_hash "
              "FROM pool_ticks WHERE tx_sig='0xother'", ("0xunbound", "0xforeignpool"))
    evidence = volume.onchain_volume_m5(c, CHAIN, evm_mint, AT,
                                        exclude_tx="0xsource", price_fn=_price)
    c.close()
    assert evidence["orphan_swaps"] == 1
    assert runner.onchain_m5_txns_lower_bound(evidence) == 1


def test_measured_empty_window_retains_zero_transaction_count():
    from test_memecoin_onchain_volume import MINT as evm_mint
    c = _conn()
    evidence = volume.onchain_volume_m5(c, CHAIN, evm_mint, AT, price_fn=_price)
    c.close()
    assert runner.onchain_m5_txns_lower_bound(evidence) == 0
