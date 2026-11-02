"""E1 on 56 and 8453: our own band mcap, no DexScreener (spec 2026-09-29-memecoin-e1-evm-own-mcap-design.md).

MEMECOIN_EVM_OWN_MCAP_CHAINS lists the EVM chains where DexScreener's market_cap never sets the band.
The source fill price x the coin's on-chain totalSupply does. The supply read is started beside the
market-data GET, not after it, and the stage waits at most MEMECOIN_EVM_OWN_MCAP_WAIT_MS past the
GET's return. A coin no own stage prices is refused mcap_unresolved, as EVM always has.
"""
import importlib.util
import sqlite3
import threading
import time
import types
from pathlib import Path

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_price as EP
from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

ROOT = Path(__file__).resolve().parents[1]
MINT = "0x" + "c3" * 20
SUPPLY = 1_000_000_000.0          # whole tokens
PRICE = 4.0e-05                   # USD per token: $40,000 at that supply


@pytest.fixture
def book(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_ALLOWLIST", {"56", "8453", "4663"})  # arming filters by it
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.add_wallet(c, "u", "tq")
    m.set_wallet_real_approved(c, "u", True, chains=["56", "8453", "4663"], force=True)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_OWN_MCAP_CHAINS", "56", raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_OWN_MCAP_WAIT_MS", 150.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 250_000.0)
    yield c
    c.close()


def _hook(monkeypatch, *, ds_mcap=90_000.0, get_s=0.0, supply=(SUPPLY, False, 12.0), read_s=0.0,
          marks=None):
    seen = {}

    def spy(conn, ev, contexts, **kw):
        seen.update(kw)
        return {"status": "filled"}

    monkeypatch.setattr(RR, "real_entry_hook", spy)
    monkeypatch.setattr(RR, "_alert_entry_result", lambda *a, **k: None)
    reads = []

    def total_supply(chain, mint):
        reads.append((chain, mint))
        if read_s:
            time.sleep(read_s)
        if isinstance(supply, Exception):
            raise supply
        return supply
    monkeypatch.setattr(EP, "evm_total_supply", total_supply)

    def stats(mint, network_id=None):
        if get_s:
            time.sleep(get_s)
        return {"market_cap": ds_mcap, "price": 3.0e-05, "liquidity": 9000.0}

    def mark(trace, name, **fields):
        (marks if marks is not None else []).append((name, fields))
    monkeypatch.setattr(RR, "_mark_evm_own_supply", mark)
    contexts = {"56": object(), "8453": object(), "4663": object()}
    from datetime import datetime, timezone
    hook = RR.make_real_hook(None, contexts, now_fn=lambda: datetime.now(timezone.utc).isoformat(),
                             trade_size=10.0, max_age_s=None, stats_fn=stats)
    return hook, seen, reads


def _ev(network="56", **kw):
    base = dict(side="buy", source_user_id="u", mint=MINT, network_id=network,
                their_market_cap=None, their_price=PRICE)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_a_listed_chain_bands_from_supply_times_the_source_fill_never_dexscreener(book, monkeypatch):
    hook, seen, reads = _hook(monkeypatch)
    assert hook(book, _ev())["status"] == "filled"
    ctx = seen["classification_context"]
    assert ctx["mcap_basis"] == "evm_source"
    assert ctx["effective_mcap"] == pytest.approx(PRICE * SUPPLY)
    assert ctx["market_stats"]["liquidity"] == 9000.0, "the GET still feeds the other gates"
    assert reads == [("56", MINT)]


def test_an_empty_knob_keeps_todays_dexscreener_band_and_reads_nothing(book, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_OWN_MCAP_CHAINS", "")
    hook, seen, reads = _hook(monkeypatch)
    hook(book, _ev())
    ctx = seen["classification_context"]
    assert ctx["mcap_basis"] == "dexscreener" and ctx["effective_mcap"] == 90_000.0
    assert reads == []


def test_an_unlisted_chain_is_unchanged(book, monkeypatch):
    hook, seen, reads = _hook(monkeypatch)
    hook(book, _ev(network="8453"))
    assert seen["classification_context"]["mcap_basis"] == "dexscreener" and reads == []


def test_an_event_mcap_needs_no_read(book, monkeypatch):
    hook, seen, reads = _hook(monkeypatch)
    hook(book, _ev(their_market_cap=120_000.0))
    assert seen["classification_context"]["mcap_basis"] == "event" and reads == []


def _readings(book, network, *pairs):
    """Older J7 readings (market cap, price) for MINT, one hour back."""
    from datetime import datetime, timedelta, timezone
    at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    for i, (mcap, price) in enumerate(pairs):
        m.insert_event(book, {'fomo_event_id': f'old-{i}', 'mint': MINT,
                             'network_id': network, 'side': 'buy', 'created_at': at,
                             'their_market_cap': mcap, 'their_price': price,
                             'source_user_id': 'other', 'source_handle': 'other',
                             'ticker': 'CZ', 'their_size_usd': 10.0,
                             'received_at': at, 'lag_s': 0})


@pytest.mark.parametrize('network', ['56', '8453', '4663'])
def test_sparse_history_uses_this_source_price_on_every_evm_chain(book, monkeypatch, network):
    """CZ: two old readings retained a $78.7k cap after its source price fell to $16.1k. Every EVM
    chain, not only the own-mcap ones (d31fed65, 4663, 2026-10-06: a $242,892 median of two readings
    4.8 h apart passed the $250k ceiling while this buy's price put the coin at ~$410k)."""
    _readings(book, network, (90_000.0, 9.0e-05), (90_000.0, 9.0e-05))
    hook, seen, reads = _hook(monkeypatch)
    hook(book, _ev(network=network))
    ctx = seen['classification_context']
    assert ctx['effective_mcap'] == pytest.approx(40_000.0)
    assert ctx['mcap_basis'] == 'history'
    assert reads == []           # reuse the same DB evidence, without a provider read


def test_the_d31fed65_median_now_refuses_over_the_ceiling(book, monkeypatch):
    _readings(book, '4663', (369_290.0, 3.6601733e-04), (116_494.0, 1.2128195e-04))
    hook, seen, _reads = _hook(monkeypatch)
    res = hook(book, _ev(network='4663', their_price=4.087598813539288e-04))
    assert res['status'] == 'skipped_mcap' and res['reason'].endswith('(over_ceiling)')
    assert 'classification_context' not in seen


def test_a_small_supply_coin_off_an_own_mcap_chain_is_priced_not_unknown(book, monkeypatch):
    """6,000 tokens at ~$1,000 (4663, 2026-10-06): outside the Solana-tuned supply band. Unknown there
    sends a blind-chain coin to assumed_nano, past the ceiling; the readings agree, so price them."""
    _readings(book, '4663', (6.0e6, 1000.0), (6.1e6, 1015.0))
    hook, seen, _reads = _hook(monkeypatch)
    res = hook(book, _ev(network='4663', their_price=1100.0))
    assert res['status'] == 'skipped_mcap' and res['reason'].endswith('(over_ceiling)')
    assert res['mcap'] == pytest.approx(6.0e3 * 1100.0, rel=0.01)


def test_an_own_mcap_chain_keeps_the_band_and_reads_supply_for_a_small_supply_coin(book, monkeypatch):
    _readings(book, '56', (6.0e6, 1000.0), (6.1e6, 1015.0))
    hook, seen, reads = _hook(monkeypatch)
    hook(book, _ev(network='56'))
    assert seen['classification_context']['mcap_basis'] == 'evm_source' and reads == [('56', MINT)]


def test_history_without_a_source_price_is_unknown_never_the_stale_median(book, monkeypatch):
    _readings(book, '8453', (90_000.0, 9.0e-05), (90_000.0, 9.0e-05))
    hook, seen, _reads = _hook(monkeypatch, ds_mcap=120_000.0)
    hook(book, _ev(network='8453', their_price=None))
    ctx = seen['classification_context']
    assert ctx['mcap_basis'] == 'dexscreener' and ctx['effective_mcap'] == 120_000.0


@pytest.mark.parametrize("why, supply, price", [
    ("a failed read", (None, False, 30.0), PRICE),
    ("a read that raised", RuntimeError("rpc down"), PRICE),
    ("no source price", (SUPPLY, True, 0.0), None),
    ("a zero source price", (SUPPLY, True, 0.0), 0.0),
])
def test_anything_unpriced_refuses_and_never_assumes(book, monkeypatch, why, supply, price):
    hook, seen, _reads = _hook(monkeypatch, supply=supply)
    res = hook(book, _ev(their_price=price))
    assert res["status"] == "skipped_mcap" and res["reason"] == "mcap_unresolved", why
    assert "classification_context" not in seen


def test_the_old_synchronous_last_resort_never_runs_on_a_listed_chain(book, monkeypatch):
    monkeypatch.setattr(RR, "_default_evm_source_mcap_fn",
                        lambda: (lambda *a: pytest.fail("a synchronous supply read on the fill path")))
    hook, _seen, _reads = _hook(monkeypatch, supply=(None, False, 5.0))
    assert hook(book, _ev())["reason"] == "mcap_unresolved"


def test_over_the_ceiling_on_our_own_mcap_refuses(book, monkeypatch):
    hook, _seen, _reads = _hook(monkeypatch, supply=(SUPPLY * 10, False, 5.0))      # $400,000
    res = hook(book, _ev())
    assert res["status"] == "skipped_mcap" and res["reason"].endswith("(over_ceiling)")


# ---------------------------------------------------------------------------- the wait (Alpha c)

def test_a_slow_read_never_holds_the_decision_past_the_get_by_more_than_the_knob(book, monkeypatch):
    marks = []
    hook, seen, _reads = _hook(monkeypatch, read_s=2.0, marks=marks)
    t0 = time.monotonic()
    res = hook(book, _ev())
    took = time.monotonic() - t0
    assert res["reason"] == "mcap_unresolved"
    assert took < 0.15 + 0.35, "the wait is bounded by the knob, not by the read (%.3fs)" % took
    [(_name, fields)] = marks
    assert fields["outcome"] == "wait_expired" and fields["waited_ms"] >= 100


def test_a_read_that_finishes_inside_the_get_adds_no_wait(book, monkeypatch):
    marks = []
    hook, seen, _reads = _hook(monkeypatch, get_s=0.3, read_s=0.05, marks=marks)
    hook(book, _ev())
    [(_name, fields)] = marks
    assert fields["outcome"] == "ok" and fields["waited_ms"] < 50, fields
    assert fields["read_ms"] == 12.0 and fields["cache_hit"] is False


# ---------------------------------------------------------------------------- the supply read

def test_the_supply_read_caches_only_success_and_a_failure_is_never_a_stale_supply(monkeypatch):
    EP._supply_cache.clear()
    calls = []
    answers = iter([None, "0x" + format(10 ** 27, "x")])          # decimals ok, then totalSupply
    monkeypatch.setattr("sniper.execution.evm_wallet.erc20_decimals", lambda mint, rpc, http=None: 18)

    def eth_call_raw(to, data, rpc, http=None):
        calls.append(to)
        return next(answers)
    monkeypatch.setattr("sniper.execution.evm_real_seams.eth_call_raw", eth_call_raw)
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "https://custody.invalid")
    EP._supply_cache[("56", "0x" + "dd" * 20)] = 5.0              # another coin's supply
    supply, hit, _ms = EP.evm_total_supply("56", MINT)
    assert (supply, hit) == (None, False), "a failed read is unresolved"
    assert ("56", MINT) not in EP._supply_cache, "and nothing is cached"
    supply, hit, _ms = EP.evm_total_supply("56", MINT)
    assert (supply, hit) == (1e9, False)
    supply, hit, ms = EP.evm_total_supply("56", MINT)
    assert (supply, hit, ms) == (1e9, True, 0.0) and len(calls) == 2, "a hit reads nothing"
    EP._supply_cache.clear()


def test_the_knob_parses_like_the_other_evm_chain_lists():
    assert RR._evm_chain_knob(" 56, base ,solana,nope", "MEMECOIN_EVM_OWN_MCAP_CHAINS") == frozenset({"56", "8453"})
    assert RR._evm_chain_knob("", "MEMECOIN_EVM_OWN_MCAP_CHAINS") == frozenset()


# ---------------------------------------------------------------------------- the STOP line (Alpha a)

def _read_mod():
    spec = importlib.util.spec_from_file_location("e1evm", ROOT / "scripts/research/e1_evm_own_mcap_read.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unresolved_over_five_percent_of_the_first_fifty_is_a_stop():
    """More than 5% of 50 is 3 or more: it can fire as soon as the third one lands."""
    r = _read_mod()
    msg = ("STOP(E1): %d mcap_unresolved in the first %d decisions on 56 (more than 5%% of 50): "
           "unset 56 from MEMECOIN_EVM_OWN_MCAP_CHAINS and restart")
    assert r.stop_flag(["filled"] * 47 + ["mcap_unresolved"] * 3, chain="56") == msg % (3, 50)
    assert r.stop_flag(["filled"] * 48 + ["mcap_unresolved"] * 2, chain="56") is None, "2 is 4%"
    assert r.stop_flag(["mcap_unresolved"] * 3, chain="56") == msg % (3, 3), "before 50"
    assert r.stop_flag(["filled"] * 50 + ["mcap_unresolved"] * 10, chain="56") is None, \
        "only the first 50 count"


@pytest.mark.parametrize("decimals, total", [(None, "0x10"), (99, "0x10"), (18, "0x"), (18, "0x0")])
def test_every_failed_supply_read_caches_nothing(monkeypatch, decimals, total):
    EP._supply_cache.clear()
    monkeypatch.setattr("sniper.execution.evm_wallet.erc20_decimals", lambda mint, rpc, http=None: decimals)
    monkeypatch.setattr("sniper.execution.evm_real_seams.eth_call_raw", lambda to, data, rpc, http=None: total)
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "https://custody.invalid")
    assert EP.evm_total_supply("56", MINT)[:2] == (None, False)
    assert EP._supply_cache == {}


def test_cold_supply_reads_overlap_without_extending_the_entry_deadline(monkeypatch):
    EP._supply_cache.clear()
    both_reads = threading.Barrier(2)

    def decimals(mint, rpc, http=None):
        both_reads.wait(timeout=0.5)
        return 18

    def total(to, data, rpc, http=None):
        both_reads.wait(timeout=0.5)
        return hex(10 ** 27)

    monkeypatch.setattr('sniper.execution.evm_wallet.erc20_decimals', decimals)
    monkeypatch.setattr('sniper.execution.evm_real_seams.eth_call_raw', total)
    monkeypatch.setattr(cfg, 'evm_rpc_for_chain', lambda chain: 'https://custody.invalid')
    try:
        assert EP.evm_total_supply('1', MINT)[:2] == (1e9, False)
    finally:
        EP._supply_cache.clear()
