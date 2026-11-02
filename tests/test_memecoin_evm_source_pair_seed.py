"""The source's own pair token seeds a 56 entry's V2 path search (2026-10-15, operator "fill
fastest"; Alpha go on (4a)).

On 56 the winning v2 rung spent most of its ~300 ms on pair_quote_tokens, a DexScreener GET per
uncached coin (54 / 233 / 296 ms live on the last three submitted mints). The watcher's receipt
decode already holds the leg the source bought through; it now also names the token the source paid
INTO that pool (`counter_token`), and an ENTRY's search tries it before DexScreener.

Pins (Alpha): a seed whose paths quote nothing falls through to exactly one DexScreener lookup; the
counter is bound to the leg's pool and its exact input amount; the field is additive (an old reader
ignores it); the knob off is today's search."""
from types import SimpleNamespace

import pytest
from eth_utils import keccak

from sniper.execution import evm_source_route as src
from sniper.execution import evm_v2_exit_quote as V
from sniper.services import memecoin_evm_post_trade as pt

CHAIN = "56"
MINT = "0x" + "22" * 20
X = "0x" + "33" * 20                      # the coin's pair token
Y = "0x" + "44" * 20
PAIR = "0x" + "55" * 20
OTHER_POOL = "0x" + "66" * 20
OWNER = "0x" + "77" * 20
ROUTER = "0x" + "88" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"


def _w(n):
    return "%064x" % n


def _t(addr):
    return "0x" + "0" * 24 + addr[2:]


def _transfer(token, frm, to, amount):
    return {"address": token, "topics": [pt.TRANSFER, _t(frm), _t(to)], "data": "0x" + _w(amount)}


def _v2_buy(extra=()):
    """The source buys 500 MINT (token0 of PAIR) for 1000 X through PAIR."""
    return [
        _transfer(X, ROUTER, PAIR, 1000),
        _transfer(MINT, PAIR, OWNER, 500),
        {"address": PAIR, "topics": [pt.V2_SYNC], "data": "0x" + _w(10_000) + _w(21_000)},
        {"address": PAIR, "topics": [pt.V2_SWAP, _t(ROUTER), _t(OWNER)],
         "data": "0x" + _w(0) + _w(1000) + _w(500) + _w(0)},
        *extra]


def _post(logs):
    return pt.source_post_trade(CHAIN, {"status": "0x1", "logs": logs}, MINT, owner=OWNER,
                                pool_key_fn=lambda *a: None)


# --- the watcher's decode -------------------------------------------------------------------

def test_the_decode_names_the_token_paid_into_the_legs_pool():
    post = _post(_v2_buy())
    assert post["venue"] == "v2" and post["pool"] == PAIR and post["counter_token"] == X


@pytest.mark.parametrize("extra, want", [
    ([_transfer(Y, ROUTER, OTHER_POOL, 1000)], X),     # the same amount into ANOTHER pool: ignored
    ([_transfer(Y, ROUTER, PAIR, 7)], X),              # another amount into this pool: ignored
    ([_transfer(Y, ROUTER, PAIR, 1000)], None),        # two tokens fit the leg exactly: no seed
])
def test_the_counter_is_bound_to_the_legs_pool_and_exact_input(extra, want):
    post = _post(_v2_buy(extra))
    assert post["venue"] == "v2" and post.get("counter_token") == want


def test_the_field_is_additive_an_old_reader_resolves_identically():
    post = _post(_v2_buy())
    old = {k: v for k, v in post.items() if k != "counter_token"}
    a = pt.EvmChaseReference(CHAIN, post=post).resolve(2.0, PAIR)
    b = pt.EvmChaseReference(CHAIN, post=old).resolve(2.0, PAIR)
    assert a == b


# --- the seed --------------------------------------------------------------------------------

class _Ref:
    def __init__(self, post):
        self.post = post

    def payload(self):
        return self.post


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(V, "_PAIR_SEED", {})
    monkeypatch.setattr(V, "_PAIR_CACHE", {})
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_PAIR_SEED_CHAINS", CHAIN)


@pytest.mark.parametrize("post, seeded", [
    ({"venue": "v2", "pool": PAIR, "counter_token": X}, (X,)),
    ({"venue": "v3", "pool": PAIR, "counter_token": X}, (X,)),
    ({"venue": "v4", "pool": "0x" + "ab" * 32}, None),
    ({"venue": "v2", "pool": PAIR}, None),                       # an older watcher: no field
    ({"venue": "v2", "pool": PAIR, "counter_token": MINT}, None),
    ({"reason": "split_route"}, None),
    (None, None),
])
def test_only_a_v2_or_v3_counter_token_seeds(post, seeded):
    src.seed_pairs(CHAIN, _Ref(post), MINT)
    assert V.seeded_pair_tokens(CHAIN, MINT) == seeded


def test_the_knob_off_seeds_nothing(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_SOURCE_PAIR_SEED_CHAINS", "")
    assert src.seed_pairs(CHAIN, _Ref({"venue": "v2", "counter_token": X}), MINT) is False
    assert V.seeded_pair_tokens(CHAIN, MINT) is None


# --- the entry's path search -------------------------------------------------------------------

@pytest.fixture
def search(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS", frozenset({CHAIN}),
                        raising=False)
    lookups, outs = [], {}

    def ds(chain, coin, **kw):
        lookups.append(coin)
        return (Y,)
    monkeypatch.setattr(V, "pair_quote_tokens", ds)

    def quote_path(call_fn, router, amount_in, path):
        out = outs.get(tuple(path))
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(V, "_quote_path", quote_path)

    def run(sell, buy):
        return V.dex_v2_exit_quote(CHAIN, sell, buy, 10 ** 18, OWNER, cfg_router=ROUTER,
                                   connector_tokens=(WBNB, USDT), slippage_bps=100,
                                   call_fn=lambda *a: None)
    return run, lookups, outs


def test_a_seeded_entry_routes_through_the_source_pair_without_dexscreener(search):
    run, lookups, outs = search
    V.seed_pair_tokens(CHAIN, MINT, (X,))
    outs[(WBNB, X, MINT)] = 900
    q = run(WBNB, MINT)
    assert q["route"]["path"] == [WBNB, X, MINT] and lookups == []


@pytest.mark.parametrize("seed_answer", [None, 0, V.V2Reverted("no pair"),
                                         V.V2ReadUnavailable("timeout")])
def test_a_seed_that_routes_nothing_asks_dexscreener_exactly_once(search, seed_answer):
    run, lookups, outs = search
    V.seed_pair_tokens(CHAIN, MINT, (X,))
    outs[(WBNB, X, MINT)] = seed_answer
    outs[(WBNB, Y, MINT)] = 700
    q = run(WBNB, MINT)
    assert q["route"]["path"] == [WBNB, Y, MINT] and lookups == [MINT]


def test_without_a_seed_the_search_is_todays(search):
    run, lookups, outs = search
    outs[(WBNB, Y, MINT)] = 700
    assert run(WBNB, MINT)["route"]["path"] == [WBNB, Y, MINT] and lookups == [MINT]


def test_an_exit_never_takes_the_seed(search):
    run, lookups, outs = search
    V.seed_pair_tokens(CHAIN, MINT, (X,))
    outs[(MINT, X, WBNB)] = 800              # quotable, so a seeded exit would take it
    outs[(MINT, Y, WBNB)] = 700
    assert run(MINT, WBNB)["route"]["path"] == [MINT, Y, WBNB] and lookups == [MINT]


def test_a_connector_seed_skips_dexscreener_when_the_direct_pair_routes(search):
    run, lookups, outs = search
    V.seed_pair_tokens(CHAIN, MINT, (WBNB,))            # the source paid WBNB into the coin's pair
    outs[(WBNB, MINT)] = 950
    assert run(WBNB, MINT)["route"]["path"] == [WBNB, MINT] and lookups == []


def test_the_early_ladder_seeds_before_it_starts(tmp_path, monkeypatch):
    import sqlite3
    import threading
    from sniper.services import memecoin_real_runner as R
    monkeypatch.setattr(R, "_early_evm_quote_clip", lambda *a, **k: (175.0, None, {}))
    monkeypatch.setattr(R, "_early_evm_safety_fn", lambda *a, **k: None)
    seen, done = [], threading.Event()

    def prepare(mint, usd, *, on_started=None, **kw):
        seen.append(V.seeded_pair_tokens(CHAIN, mint))
        done.set()
        return None
    prepare.early_deadline = True
    db = tmp_path / "l.db"
    sqlite3.connect(db).close()
    monkeypatch.setattr(R, "context_for", lambda contexts, chain: {"prepare_entry_quote_fn": prepare})
    monkeypatch.setattr("sniper.persistence.db_core._connection_db_path", lambda c: str(db))
    ev = SimpleNamespace(mint=MINT, signal_source="onchain_watch", delivery_lane=None,
                         evm_chase_reference=_Ref({"venue": "v2", "pool": PAIR, "counter_token": X}))
    trace = SimpleNamespace(mark_substage=lambda *a, **k: True)
    assert R._start_early_evm_quote(sqlite3.connect(db), ev, {}, trace, object(), CHAIN) is not None
    assert done.wait(2.0) and seen == [(X,)]


def test_the_executor_quote_seeds_before_its_ladder():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "sniper/execution/evm_real_seams.py").read_text()
    i = text.index("def quote_at_snapshot(")
    body = text[i:text.index("def quote_miss_at_snapshot(", i)]
    assert body.index("_src.seed_pairs(chain, chase_reference, token_mint)") < body.index(
        "result = _venue_quote_combined(")
