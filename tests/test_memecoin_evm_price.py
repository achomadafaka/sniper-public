"""GeckoTerminal pricing for DexScreener-blind EVM chains (2026-07-14).

DexScreener has NO pair for Robinhood-chain (4663) tokens — 323,801 of 323,801 capture
observations came back `missing_pair`, i.e. 100%. Consequences, all measured:
  * every 4663 paper position closes `unquotable` at exactly $0.00 (fictional flat), so the
    EVM lane's PnL is unmeasurable and every arm's PnL is biased optimistic;
  * we have ZERO price history for 4663, so no exit policy can be fitted for that chain;
  * the price rails (TP/stop/trail) can never nominate on 4663 — a position can only leave via
    mirror-on-sell or the blind 24h unquotable timer.

This module makes GeckoTerminal's indexed price available to the paper and real sweeps so marks
accumulate and the chain becomes measurable without an execution-provider dependency.

Liquidity is unknown from this price seam -> None, which `slippage_frac` already treats as the
conservative cap. Pessimistic, not fabricated.
"""
import pytest

from sniper.services.memecoin_evm_price import evm_price_batch


def test_prices_evm_mints_dexscreener_could_not_price():
    keys = [("4663", "0xaaa"), ("4663", "0xbbb")]
    px = evm_price_batch(keys, price_for=lambda chain, mint: 0.5 if mint == "0xaaa" else 2.0)
    assert px[("4663", "0xaaa")]["price"] == 0.5
    assert px[("4663", "0xbbb")]["price"] == 2.0
    # liquidity unknown from a bare probe -> None (slippage_frac applies the conservative cap;
    # never invent a liquidity number, that would fabricate an optimistic paper fill).
    assert px[("4663", "0xaaa")]["liquidity"] is None
    assert px[("4663", "0xaaa")]["source"] == "probe"


def test_skips_solana_mints():
    """Solana is priced fine by DexScreener — never probe it through the blind-chain seam."""
    keys = [("solana", "CizcDa22HJjXybKLriibqP167GcJjqiDHDPtuiMmpump")]
    assert evm_price_batch(keys, price_for=lambda c, m: pytest.fail("must not quote solana")) == {}


def test_unpriceable_mint_is_omitted_not_zeroed():
    """A mint we cannot price must be ABSENT from the map, never present with price 0. A 0 would
    read as a -100% tick and fire every stop in the book."""
    px = evm_price_batch([("4663", "0xdead")], price_for=lambda c, m: None)
    assert px == {}
    px = evm_price_batch([("4663", "0xdead")], price_for=lambda c, m: 0.0)
    assert px == {}


def test_quote_failure_does_not_break_the_sweep():
    """One bad mint must not take down pricing for the rest — the exit sweep depends on this map."""
    def price_for(chain, mint):
        if mint == "0xboom":
            raise RuntimeError("price provider 500")
        return 1.25
    px = evm_price_batch([("4663", "0xboom"), ("4663", "0xgood")], price_for=price_for)
    assert ("4663", "0xboom") not in px
    assert px[("4663", "0xgood")]["price"] == 1.25


def test_respects_the_call_cap():
    """Bounded provider work per sweep — a large EVM book must not scale unbounded."""
    calls = []

    def price_for(chain, mint):
        calls.append(mint)
        return 1.0
    keys = [("4663", "0x%02x" % i) for i in range(50)]
    px = evm_price_batch(keys, price_for=price_for, cap=10)
    assert len(calls) == 10 and len(px) == 10


# --- wiring into the ~15s exit sweep -------------------------------------------------------------

def test_bracket_pass_fills_only_the_gaps_dexscreener_left(tmp_path, monkeypatch):
    """The 0x fallback must ONLY price mints DexScreener could not. A DexScreener price is richer
    (it carries liquidity + mcap, which the fill model and the mcap exit-bands need), so a 0x mid
    must never overwrite one."""
    import sniper.services.memecoin_runner as R

    calls = []

    def fake_dex(keys):
        # solana prices fine; the 4663 mint comes back empty, exactly as in production
        return {("solana", "SoLmint"): {"price": 1.0, "liquidity": 50_000.0, "mcap": 1e6}}

    def fake_evm_price_for(chain, mint):
        calls.append((chain, mint))
        return 0.25

    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats_batch_keyed", fake_dex)
    captured = {}
    monkeypatch.setattr(R, "sweep_exits_for_test_capture", None, raising=False)

    import sniper.services.memecoin_exits as E
    monkeypatch.setattr(E, "sweep_exits", lambda conn, price_map, now_iso, **kw: captured.update(price_map) or [])

    conn = _mem_db_with_open_positions([("solana", "SoLmint"), ("4663", "0xEvmMint")])
    R.bracket_pass(conn, evm_price_for=fake_evm_price_for, gt_batch_fn=lambda keys: {},
                   now_fn=lambda: "2026-07-15T00:00:00+00:00")

    assert calls == [("4663", "0xEvmMint")], "should quote ONLY the unpriced EVM mint"
    assert captured[("solana", "SoLmint")]["liquidity"] == 50_000.0    # dexscreener row untouched
    assert captured[("4663", "0xEvmMint")]["price"] == 0.25            # gap filled
    assert captured[("4663", "0xEvmMint")]["source"] == "probe"


def test_bracket_pass_batch_gt_prices_every_open_blind_coin_not_just_the_cap(monkeypatch):
    """A large open 4663 book must ALL get fresh marks each pass. The per-mint rotation alone
    (cap 8) refreshed only ~8/pass, so a 364-position book left bags (incl. the real ones) ~10min
    stale. A GeckoTerminal batch prices every open blind-chain coin in ~one HTTP per 30, so every
    position gets a live mark + price rail every sweep."""
    import sniper.services.memecoin_runner as R
    import sniper.services.memecoin_exits as E
    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats_batch_keyed",
                        lambda keys: {})
    captured = {}
    monkeypatch.setattr(E, "sweep_exits",
                        lambda conn, price_map, now_iso, **kw: captured.update(price_map) or [])
    mints = [f"0x{i:02x}" for i in range(20)]        # 20 open coins, well over the per-mint cap of 8
    conn = _mem_db_with_open_positions([("4663", m) for m in mints])

    def gt_batch(keys):
        return {tuple(k): {"price": 0.5, "liquidity": None, "mcap": None, "source": "geckoterminal"}
                for k in keys}
    # 0x is dead (price_for -> None), so the GT batch is the ONLY price source this pass
    R.bracket_pass(conn, evm_price_for=lambda c, m: None, gt_batch_fn=gt_batch,
                   now_fn=lambda: "2026-07-15T02:00:00+00:00")

    priced = [k for k in captured if k[0] == "4663" and (captured[k] or {}).get("price")]
    assert len(priced) == 20, "every open 4663 coin must be marked, got %d" % len(priced)
    assert all(captured[k]["source"] == "geckoterminal" for k in priced)


def _mem_db_with_open_positions(pairs):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE memecoin_positions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_user_id TEXT, source_handle TEXT, mint TEXT,
        ticker TEXT, entry_price REAL, entry_notional REAL, is_open INTEGER DEFAULT 1,
        opened_at TEXT, closed_at TEXT, realized_pnl REAL, exit_reason TEXT,
        strategy TEXT DEFAULT 'control', execution_mode TEXT DEFAULT 'paper', peak_price REAL,
        network_id TEXT, hold_clock_normalized_at TEXT, last_quote_price REAL, last_quote_ts TEXT,
        stuck_bag INTEGER DEFAULT 0)""")
    for net, mint in pairs:
        conn.execute("INSERT INTO memecoin_positions(mint, network_id, entry_price, entry_notional,"
                     " opened_at, strategy) VALUES(?,?,?,?,?,'control')",
                     (mint, net, 1.0, 100.0, "2026-07-14T23:00:00+00:00"))
    conn.commit()
    return conn


def test_capped_passes_still_cover_the_whole_book(monkeypatch):
    """The pass is capped and bracket_pass runs on the poll loop, so a coin must
    not be permanently starved just because it sits past the cap. Successive passes rotate on last
    ATTEMPT, so every open EVM coin gets priced within a few passes."""
    from sniper.services import memecoin_evm_price as P
    import sniper.services.memecoin_runner as R
    import sniper.services.memecoin_exits as E
    P.reset_rotation_state()

    monkeypatch.setattr("sniper.execution.solana_quote.dexscreener_stats_batch_keyed",
                        lambda keys: {})           # dexscreener prices nothing (the 4663 reality)
    monkeypatch.setattr(E, "sweep_exits", lambda conn, price_map, now_iso, **kw: [])
    mints = [f"0x{i:02x}" for i in range(P.DEFAULT_CAP * 2)]
    conn = _mem_db_with_open_positions([("4663", m) for m in mints])
    seen = set()

    def price_for(chain, mint):
        seen.add(mint)
        return 1.0
    for _ in range(2):                             # two capped passes -> full coverage of 16 mints
        R.bracket_pass(conn, evm_price_for=price_for, gt_batch_fn=lambda keys: {},
                       now_fn=lambda: "2026-07-15T01:00:00+00:00")

    assert seen == set(mints), "capped passes must rotate until every coin is covered, got %s" % seen


# --- starvation: unpriceable mints must not eat the cap forever ----------------------------------

def test_unpriceable_mints_do_not_starve_the_priceable_ones():
    """THE BUG (seen live 2026-07-15): 20 `evm-unknown` mints are not on the allowlist and never
    resolve, so they never price. Rotating on last SUCCESSFUL quote left their timestamp NULL
    forever, so they sorted first on every pass and consumed the whole cap — the 13 real 4663 coins
    got starved and only 1 mark landed in 90s. Rotate on last ATTEMPT, and back off a mint that
    keeps failing, or the blind chain stays blind."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    clock = [1000.0]

    dead = [("evm-unknown", "0xdead%d" % i) for i in range(8)]
    good = [("4663", "0xgood%d" % i) for i in range(3)]
    asked = []

    def price_for(chain, mint):
        asked.append(mint)
        return None if chain == "evm-unknown" else 1.0

    # pass 1: the cap is spent, mostly on the dead mints (they are first in the list)
    P.evm_price_batch(dead + good, price_for, cap=8, clock=lambda: clock[0])
    # pass 2, a minute later: the dead mints were just ATTEMPTED, so the good ones must go first
    clock[0] += 60.0
    asked.clear()
    px = P.evm_price_batch(dead + good, price_for, cap=8, clock=lambda: clock[0])

    assert all(m.startswith("0xgood") for m in asked[:3]), (
        "priceable mints must be quoted before recently-attempted dead ones, got %s" % asked[:3])
    assert len(px) == 3, "all three 4663 coins should be priced on this pass"


# --- market cap for DexScreener-blind chains ------------------------------------------------------
# DexScreener carries no pair for 4663, so mcap/liquidity/volume come back empty and the operator's
# real-positions table shows blank columns for every non-Solana row. Liquidity and 24h volume are
# genuinely unknowable from a swap quote and must stay None (never fabricate them) — but MARKET CAP
# is derivable honestly: price x on-chain totalSupply. It is also what the mcap exit-bands need.

def test_market_cap_derived_from_price_times_total_supply():
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    px = P.evm_price_batch(
        [("4663", "0xtok")],
        price_for=lambda c, m: 0.002,                     # $0.002 / token
        mcap_for=lambda c, m, price: price * 1_000_000_000,   # 1e9 supply -> $2,000,000
    )
    row = px[("4663", "0xtok")]
    assert row["price"] == 0.002
    assert row["mcap"] == 2_000_000
    # never fabricated — a swap quote cannot know these
    assert row["liquidity"] is None


def test_mcap_failure_leaves_it_unknown_and_never_blocks_the_price():
    """An unreadable totalSupply must not cost us the PRICE — the exit rails depend on the price."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()

    def boom(c, m, price):
        raise RuntimeError("rpc down")
    px = P.evm_price_batch([("4663", "0xtok")], price_for=lambda c, m: 0.002, mcap_for=boom)
    assert px[("4663", "0xtok")]["price"] == 0.002
    assert px[("4663", "0xtok")]["mcap"] is None


# --- the 0x fallback must NEVER price a chain DexScreener already covers -------------------------
#
# THE BUG (2026-07-14, cost a +$785.66 phantom TP): the fallback ran on ANY unpriced EVM mint. Base
# (8453) IS covered by DexScreener, but on a single sweep DexScreener transiently missed a mint, the
# 0x fallback kicked in, and its quote was garbage — RUSSELL's true consensus was 0.00206 (318
# DexScreener marks, range 0.00202-0.00217) while 0x returned 0.030549, a 14.8x error. That tick
# fired `take_profit` on the control arm for +$785.66 of fabricated PnL, and it polluted the mark
# tape (58 zerox marks on 4 Base coins) that our arming decisions are scored from.
#
# A swap quote for ONE whole token of an illiquid memecoin is not a reliable mid. It is only worth
# trusting where there is NO alternative — i.e. chain 4663, which DexScreener does not index at all
# (missing_pair on 100% of its tokens). Everywhere else, a transient DexScreener miss must simply be
# SKIPPED: no mark is strictly better than a wrong mark.

def test_fallback_refuses_chains_dexscreener_already_covers():
    """Base/BSC/Ethereum have DexScreener pairs; a transient miss stays missing."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    price_for = P.build_evm_price_for(blind_chains={"4663"})
    assert price_for("8453", "0xtok") is None, "must not price Base (DexScreener covers it)"
    assert price_for("56", "0xtok") is None
    assert price_for("1", "0xtok") is None


def test_build_price_for_returns_none_after_gt_miss_without_http_fallback():
    from sniper.services import memecoin_evm_price as P
    calls = []
    price_for = P.build_evm_price_for(
        blind_chains={"4663"}, _gt_fn=lambda *_a: None,
        http=lambda *args, **kwargs: calls.append((args, kwargs)) or (500, {}, None),
    )

    assert price_for("4663", "0xtok") is None
    assert calls == []


def test_gt_miss_on_the_blind_chain_remains_unpriced():
    """A GeckoTerminal miss stays missing; no execution-provider fallback is allowed."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    price_for = P.build_evm_price_for(
        blind_chains={"4663"}, _gt_fn=lambda *_a: None)
    assert price_for("4663", "0xtok") is None


# --- honest source: the source string reaching the mark tape is the true provider.

def test_evm_price_batch_stamps_source_from_a_price_for_tuple():
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    px = P.evm_price_batch([("4663", "0xg")],
                           price_for=lambda c, m: (0.5, "geckoterminal"))
    assert px[("4663", "0xg")]["price"] == 0.5
    assert px[("4663", "0xg")]["source"] == "geckoterminal"


def test_evm_price_batch_scalar_price_for_is_neutral_probe():
    """A bare injected price has generic provenance; it must not impersonate a provider."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    px = P.evm_price_batch([("4663", "0xz")], price_for=lambda c, m: 0.25)
    assert px[("4663", "0xz")]["source"] == "probe"


def test_build_price_for_uses_gt_and_leaves_misses_unpriced():
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()
    gt_seen = []

    def gt(chain, mint):
        gt_seen.append((chain, mint))
        return 0.9 if mint == "0xhasgt" else None      # GT prices only one of them

    price_for = P.build_evm_price_for(blind_chains={"4663"}, _gt_fn=gt)
    assert price_for("4663", "0xhasgt") == (0.9, "geckoterminal")
    assert price_for("4663", "0xnogt") is None
    assert gt_seen == [("4663", "0xhasgt"), ("4663", "0xnogt")]


def test_build_price_for_never_consults_gt_on_dexscreener_covered_chains():
    """GT inherits the RUSSELL rule: a secondary source must NEVER price a chain DexScreener covers.
    The blind-chain gate runs before GT, so GT is not even called for Base/BSC/Ethereum."""
    from sniper.services import memecoin_evm_price as P
    P.reset_rotation_state()

    def gt(chain, mint):
        raise AssertionError("GT must not be consulted on a DexScreener-covered chain")
    price_for = P.build_evm_price_for(blind_chains={"4663"}, _gt_fn=gt)
    assert price_for("8453", "0xtok") is None
