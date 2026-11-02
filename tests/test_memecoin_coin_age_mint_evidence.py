"""The coin-age floor reads the MINT's own evidence, not only the newest listed pool (2026-09-28).

2e32783b (Solana, 2026-09-23T16:07:36Z) was refused "coin_too_young: 54s old, floor 120s".
DexScreener listed one pair, the Raydium pool the coin graduated into 54 s earlier; the mint's
first transaction was 33,193 s old. The Helius answer below is the real one for that mint
(getTransactionsForAddress, ascending, signatures), read on the research key on 2026-09-28.
"""
import json
import threading
import time
import urllib.error
from datetime import datetime
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_coin_age as A
from sniper.services import memecoin_real_runner as R
from sniper.services.memecoin_risk import coin_age_refusal

MINT = "FjapQEucYhJj4DS91AW3nLTNgrEpwozLVdbcjxQ9rZCn"
DECISION = datetime.fromisoformat("2026-09-23T16:07:36.979261+00:00").timestamp()
REAL_ANSWER = {"jsonrpc": "2.0", "id": 1, "result": {"paginationToken": None, "data": [
    {"signature": "52QNCjbr8P7ZJQVLsKjyoRVjtdUikCV3fu3YwmUawWHJsF5PZuboEGEpatvqNUBPZvoD2chs7em6AMsAqTxDNGgq",
     "slot": 449627729, "transactionIndex": 136, "err": None, "memo": None,
     "blockTime": 1790146464, "confirmationStatus": "finalized"}]}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(A, "_FIRST_TS", {})
    monkeypatch.setattr(A, "_IN_FLIGHT", {})
    monkeypatch.setattr(A, "_STATS", dict.fromkeys(A._STATS, 0))
    monkeypatch.setenv("MEMECOIN_REAL_MIN_COIN_AGE_S", "120")
    monkeypatch.delenv("MEMECOIN_REAL_MIN_COIN_AGE_S_solana", raising=False)
    monkeypatch.setenv("MEMECOIN_COIN_AGE_MINT_EVIDENCE", "1")
    monkeypatch.setenv("ONCHAIN_WATCH_HELIUS_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=test")


def _classification(pair_age_s, mint=MINT):
    return SimpleNamespace(facts={"pair_age_s": pair_age_s, "mint": mint})


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_real_first_transaction_admits_the_graduated_coin(monkeypatch):
    monkeypatch.setattr(A.urllib.request, "urlopen", lambda req, timeout: _Resp(REAL_ANSWER))
    monkeypatch.setattr(A.time, "time", lambda: DECISION)
    assert coin_age_refusal(_classification(54.0), "solana") == "coin_too_young: 54s old, floor 120s"
    age = A.warm("solana", MINT)
    assert age == pytest.approx(33192.98, abs=0.01)
    assert coin_age_refusal(_classification(54.0), "solana") is None
    assert A.stats()["helius_calls"] == 1 and A.stats()["helius_ok"] == 1


def test_a_failed_first_transaction_is_skipped(monkeypatch):
    rows = [dict(REAL_ANSWER["result"]["data"][0], err={"InstructionError": [0, "x"]},
                 blockTime=1_000), REAL_ANSWER["result"]["data"][0]]
    monkeypatch.setattr(A.urllib.request, "urlopen",
                        lambda req, timeout: _Resp({"result": {"data": rows}}))
    assert A._helius_first_ts(MINT) == 1790146464.0


@pytest.mark.parametrize("failure", ["http_429", "transport", "garbage", "empty"])
def test_an_unknown_answer_leaves_the_gate_as_it_was(monkeypatch, failure):
    def urlopen(req, timeout):
        if failure == "http_429":
            raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        if failure == "transport":
            raise OSError("reset")
        return _Resp({"result": "nonsense"} if failure == "garbage" else {"result": {"data": []}})
    monkeypatch.setattr(A.urllib.request, "urlopen", urlopen)
    assert A.warm("solana", MINT) is None
    assert coin_age_refusal(_classification(54.0), "solana") == "coin_too_young: 54s old, floor 120s"
    assert A.stats()["helius_429" if failure == "http_429" else "helius_errors"] == 1
    assert A._FIRST_TS == {}, "a miss must not be cached"


def test_evidence_younger_than_the_floor_still_refuses():
    A.warm("solana", MINT, lookup=lambda chain, mint: time.time() - 100.0)
    assert coin_age_refusal(_classification(54.0), "solana") == "coin_too_young: 54s old, floor 120s"


def test_a_slow_lookup_is_bounded_and_fills_the_cache_for_the_next_attempt():
    release = threading.Event()

    def slow(chain, mint):
        release.wait(5)
        return time.time() - 1000.0
    began = time.monotonic()
    assert A.warm("solana", MINT, wait_s=0.05, lookup=slow) is None
    assert time.monotonic() - began < 0.5 and A.stats()["wait_expired"] == 1
    release.set()
    for t in threading.enumerate():
        if t.name == "coin-age-lookup":
            t.join(5)
    assert coin_age_refusal(_classification(54.0), "solana") is None


def test_the_gate_inside_the_reservation_never_does_io(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("I/O inside the reservation transaction")
    monkeypatch.setattr(A, "_helius_first_ts", boom)
    monkeypatch.setattr(A, "_tape_first_ts", boom)
    assert coin_age_refusal(_classification(54.0), "solana") == "coin_too_young: 54s old, floor 120s"


def test_the_hook_looks_up_only_on_what_would_be_a_refusal(monkeypatch):
    calls = []
    monkeypatch.setattr(A, "warm", lambda chain, mint, **k: calls.append((chain, mint)))
    R._warm_mint_age(None, _classification(500.0), "solana", MINT)     # old enough: no lookup
    R._warm_mint_age(None, _classification(None), "solana", MINT)      # unknown age admits: none
    R._warm_mint_age(None, None, "solana", MINT)
    assert calls == []
    R._warm_mint_age(None, _classification(54.0), "solana", MINT)
    assert calls == [("solana", MINT)]


def test_the_rollback_flag_turns_it_off(monkeypatch):
    A.warm("solana", MINT, lookup=lambda chain, mint: time.time() - 50_000.0)
    monkeypatch.setenv("MEMECOIN_COIN_AGE_MINT_EVIDENCE", "0")
    assert coin_age_refusal(_classification(54.0), "solana") == "coin_too_young: 54s old, floor 120s"


def test_evm_uses_our_tape_as_a_lower_bound(tmp_path):
    from sniper.services import memecoin_volume_writer as V
    path = str(tmp_path / "volume.db")
    v = V.connect(path)
    mint, pool = "0x" + "cc" * 20, "0x" + "aa" * 32
    v.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", ("4663", pool, "v4", mint))
    for at in ("2026-09-28T11:00:05+00:00", "2026-09-28T11:00:01+00:00", "2026-09-28T11:09:00+00:00"):
        v.execute("INSERT INTO pool_ticks(chain_id, pool, kind, block_no, tx_sig, log_index, observed_at)"
                  " VALUES(?,?,?,?,?,?,?)", ("4663", pool, "v3_swap", 1, at, 0, at))
    v.commit(); v.close()
    assert A._tape_first_ts("4663", mint, tape_path=path) == \
        datetime.fromisoformat("2026-09-28T11:00:01+00:00").timestamp()
    assert A._tape_first_ts("4663", "0x" + "dd" * 20, tape_path=path) is None
