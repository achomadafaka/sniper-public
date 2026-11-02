"""DexScreener's 30-pair cap in the shared batch readers.

/latest/dex/tokens returns at most 30 PAIRS per response, even for one token (WETH on Base and wSOL
returned exactly 30 each, 2026-09-17). dexscreener_stats_batch_keyed asks 30 MINTS per chunk, so a
heavy mint can push listed mints out of the answer, and they came back with no stats and no error:
in capture-v2 sweep 699a2d4c, 26 Solana mints read missing_pair and 12 of 12 re-asked alone priced.
A capped chunk's missing mints are re-asked in ONE more GET. A mint still missing stays unknown.
"""
import sqlite3

import pytest

from sniper.execution import solana_quote as Q


@pytest.fixture(autouse=True)
def _reset_cells():
    Q._ds_cooldown_until[0] = Q._ds_funded_cooldown_until[0] = 0.0
    yield
    Q._ds_cooldown_until[0] = Q._ds_funded_cooldown_until[0] = 0.0


def _pair(mint, px=1.1, liq=5000):
    return {"chainId": "solana", "pairAddress": "%s-%s" % (mint, liq), "priceUsd": str(px),
            "baseToken": {"address": mint}, "liquidity": {"usd": liq},
            "info": {"socials": [{"type": "twitter", "url": "https://x.com/" + mint}]}}


def _heavy(mint="So1111"):
    return [_pair(mint, liq=1000 + i) for i in range(30)]


def _http(responses):
    gets = []

    def http(method, url, headers=None, body=None):
        gets.append(url.split("/tokens/", 1)[1].split("?", 1)[0].split(","))
        answer = responses[len(gets) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer
    return http, gets


KEYS = [("solana", "So1111"), ("solana", "So2222"), ("solana", "So3333")]


def test_a_capped_chunk_recovers_a_dropped_mint_in_one_more_get():
    http, gets = _http([(200, {"pairs": _heavy()}, None),
                        (200, {"pairs": [_pair("So2222", px=2.0)]}, None)])
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=http, funded=True)
    assert len(gets) == 2 and sorted(gets[1]) == ["So2222", "So3333"]   # only the missing mints
    assert out[("solana", "So2222")]["price"] == 2.0
    assert ("solana", "So3333") not in out                              # an uncapped re-ask miss
    assert Q.dexscreener_cooldown_active(funded=True) is False


def test_an_uncapped_miss_stays_a_miss_with_one_get():
    http, gets = _http([(200, {"pairs": [_pair("So1111")]}, None)])
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=http)
    assert len(gets) == 1 and set(out) == {("solana", "So1111")}


def test_a_capped_re_ask_never_loops_and_keeps_what_it_returned():
    http, gets = _http([(200, {"pairs": _heavy()}, None),
                        (200, {"pairs": _heavy("So2222")}, None)])
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=http)
    assert len(gets) == 2
    assert set(out) == {("solana", "So1111"), ("solana", "So2222")}     # So3333 unknown


def test_a_429_on_the_re_ask_keeps_first_read_stats_and_trips_the_breaker():
    http, gets = _http([(200, {"pairs": _heavy()}, None), (429, {}, None)])
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=http, funded=True)
    assert len(gets) == 2 and set(out) == {("solana", "So1111")}
    assert Q.dexscreener_cooldown_active(funded=True) is True


def test_an_exception_on_the_re_ask_keeps_first_read_stats_and_trips_the_breaker():
    http, gets = _http([(200, {"pairs": _heavy()}, None), TimeoutError("timed out")])
    out = Q.dexscreener_stats_batch_keyed(KEYS, http=http, funded=True)
    assert len(gets) == 2 and set(out) == {("solana", "So1111")}
    assert Q.dexscreener_cooldown_active(funded=True) is True


def test_a_capped_chunk_after_a_failed_re_ask_stops_the_remaining_chunks():
    keys = [("solana", "So1111")] + [("solana", "Sx%02d" % i) for i in range(35)]   # 2 chunks
    http, gets = _http([(200, {"pairs": _heavy()}, None), (503, {}, None)])
    Q.dexscreener_stats_batch_keyed(keys, http=http)
    assert len(gets) == 2                        # no third GET for the second chunk


def test_no_re_ask_on_a_non_200_first_read():
    http, gets = _http([(429, {}, None)])
    assert Q.dexscreener_stats_batch_keyed(KEYS, http=http) == {} and len(gets) == 1


def test_socials_batch_recovers_a_dropped_mint_and_never_loops():
    http, gets = _http([(200, {"pairs": _heavy()}, None),
                        (200, {"pairs": [_pair("So2222")]}, None)])
    out = Q.dexscreener_socials_batch(KEYS, http=http)
    assert len(gets) == 2 and sorted(gets[1]) == ["So2222", "So3333"]
    assert out[("solana", "So2222")]["twitter"] == "https://x.com/So2222"


def test_socials_batch_uncapped_miss_is_one_get():
    http, gets = _http([(200, {"pairs": [_pair("So1111")]}, None)])
    Q.dexscreener_socials_batch(KEYS, http=http)
    assert len(gets) == 1


def test_the_funded_exit_sweep_sees_the_recovered_mid(monkeypatch):
    """memecoin_real_runner.real_exit_sweep reads the batch with funded=True and no price_fn.
    Before: a capped answer left So2222/So3333 with no mid (peak stayed at entry). After: 2 GETs."""
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0)
    now = "2026-07-15T00:00:00+00:00"
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    ids = [m.open_position(c, source_user_id="u", source_handle="h", mint=mint, ticker="T",
                           entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                           entry_lag_s=-1.0, their_size_usd=0.0, opened_at=now,
                           strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real",
                           network_id="solana") for _n, mint in KEYS]
    gets = []

    def fake(method, url, headers=None, body=None, timeout=None):
        assert "dexscreener" in url, url
        asked = url.split("/tokens/", 1)[1].split("?", 1)[0].split(",")
        gets.append(asked)
        if len(gets) == 1:
            return 200, {"pairs": [_pair("So1111", px=1.1, liq=1000 + i) for i in range(30)]}, None
        return 200, {"pairs": [_pair(mint, px=1.1) for mint in asked]}, None
    monkeypatch.setattr(Q, "_http", fake)
    ctx = {"solana": {"exit_fn": lambda *a, **k: pytest.fail("no exit on a modest gain"),
                      "preview_exit_fn": lambda pos: None}}
    rr.real_exit_sweep(c, ctx, now_fn=lambda: now)
    assert len(gets) == 2
    # One sweep is one tick: the display peak needs a second to confirm it (2026-10-17), so the
    # recovered mid shows as the unconfirmed high.
    assert [m.get_position(c, pid)["peak_unconfirmed_price"] for pid in ids] == [pytest.approx(1.1)] * 3
