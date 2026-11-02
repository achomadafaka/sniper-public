"""E14 step 1: funded momentum on our own tape, as a zero-wait SHADOW on 4663 and 8453.

Spec: docs/superpowers/specs/2026-10-08-memecoin-funded-momentum-own-data-design.md (Gamma PASS
7a4dcbfed).

The funded classifier's momentum reads DexScreener rows (capture_v2), and its phase reaches money in
two ways:
- `post_peak` nominates an exact-quote refusal against `peak_price`;
- a runup of at least 20% makes the regime `hot_narrow`, which scales the clip to 0.75.

This step records our own series beside it and changes nothing else.

The own series is one spot pool's post-swap sqrtPrice on a 30 s grid, carried forward. Its own price
is never mixed with the provider's.
"""
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_volume as ov
from sniper.services.memecoin_market_state import classify_funded_entry, own_momentum_facts
from sniper.settings import memecoin as cfg

# --- units: real stored swaps (data/memecoin_onchain_volume.db, 2026-10-08 ~23:50Z) ----------------
# The post-swap spot of a small swap sits within a fraction of a percent of that swap's own average
# price |quote| / |coin|. A decimals or orientation error is off by 10^12 or inverted, not by 2%.

REAL_SWAPS = [
    # 4663 Uniswap V4 (66-char pool id, native ETH quote, coin 18 decimals)
    dict(pool="0x509c6c6826ef06e0e29f24f18d9389533c52358a3b385d99a67a2afbb07f23c6",
         amount0="19564366073759560", amount1="-62329871571844270",
         sqrt="141497299954751313151484925832", quote_side=0, d0=18, d1=18),
    # 8453 V3 (WETH quote, coin 18 decimals)
    dict(pool="0x20cb8f872ae894f7c9e32e621c186e5afce82fd0",
         amount0="-41292379008793321", amount1="122175858032637231104",
         sqrt="4308663127950403890455454294665", quote_side=0, d0=18, d1=18),
    # 4663 V4 with mixed decimals (quote 18, other side 6)
    dict(pool="0xbac3aa3b91584a53a579b3c999a56756e954e59247e497bad1d25a4334bde551",
         amount0="16255717943125892", amount1="-40157012",
         sqrt="3939210963853384293815586", quote_side=0, d0=18, d1=6),
]


def _spot(swap):
    """USD per coin at quote_usd 1, i.e. quote units per coin, through the shared paper-tick helper."""
    from sniper.services.memecoin_paper_ticks import spot_usd
    coin = 1 - swap["quote_side"]
    return spot_usd(swap["sqrt"], mint_is_c1=coin == 1, coin_decimals=(swap["d0"], swap["d1"])[coin],
                    quote_decimals=(swap["d0"], swap["d1"])[swap["quote_side"]], quote_usd=1.0)


@pytest.mark.parametrize("swap", REAL_SWAPS, ids=lambda s: s["pool"][:10])
def test_the_spot_of_a_real_stored_swap_matches_its_own_average_price(swap):
    amounts = ((swap["amount0"], swap["d0"]), (swap["amount1"], swap["d1"]))
    (qa, qd), (ca, cd) = amounts[swap["quote_side"]], amounts[1 - swap["quote_side"]]
    average = (abs(int(qa)) / 10 ** qd) / (abs(int(ca)) / 10 ** cd)
    assert _spot(swap) == pytest.approx(average, rel=0.02)


# --- the series on a tape fixture --------------------------------------------------------------------

CHAIN = "4663"
MINT = "0x70d3e2fc00000000000000000000000000000000"
QUOTE = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
POOL = "0x" + "aa" * 32                  # V4 id
POOL2 = "0x" + "bb" * 20                 # V3
AT = 1789560000.0
SINCE = AT - 3600.0


def _sqrt_for(quote_per_coin, quote_side=0):
    """sqrtPriceX96 for a coin at `quote_per_coin`, both tokens 18 decimals. token1/token0 is coins
    per quote when the quote is token0, quote per coin when it is token1."""
    ratio = 1.0 / quote_per_coin if quote_side == 0 else quote_per_coin
    return str(int(ratio ** 0.5 * 2 ** 96))


def _tape(swaps=(), *, protocols=((POOL, "v4"),), meta=None, headers=True, hashes=True):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE pool_ticks (id INTEGER PRIMARY KEY AUTOINCREMENT, chain_id TEXT, pool TEXT,
            kind TEXT, block_no INTEGER, tx_sig TEXT, log_index INTEGER, maker TEXT,
            amount0 TEXT, amount1 TEXT, observed_at TEXT, sqrt_price_x96 TEXT, block_hash TEXT);
        CREATE TABLE pool_meta (chain_id TEXT, pool TEXT, token0 TEXT, token1 TEXT,
            quote_side INTEGER, resolved_at TEXT, token0_decimals INTEGER, token1_decimals INTEGER);
        CREATE TABLE block_times (chain_id TEXT, block_no INTEGER, block_ts TEXT,
            fetched_at TEXT, block_hash TEXT, PRIMARY KEY (chain_id, block_no));
        CREATE TABLE onchain_pool_protocols (chain_id TEXT, pool TEXT, protocol TEXT, mint TEXT);
        CREATE TABLE tick_gaps (id INTEGER PRIMARY KEY,chain_id TEXT,from_block INTEGER,
            to_block INTEGER,reason TEXT,detected_at TEXT,resolved_at TEXT);
        CREATE INDEX idx_tick_gaps_open ON tick_gaps(chain_id,resolved_at,from_block);
    """)
    for pool, protocol in protocols:
        c.execute("INSERT INTO onchain_pool_protocols VALUES (?,?,?,?)", (CHAIN, pool, protocol, MINT))
        row = (meta or {}).get(pool, (QUOTE, MINT, 0, 18, 18))
        c.execute("INSERT INTO pool_meta VALUES (?,?,?,?,?,?,?,?)",
                  (CHAIN, pool, row[0], row[1], row[2], "", row[3], row[4]))
    from datetime import datetime, timezone
    # the newest header is fresh, as on a live tape
    c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)",
              (CHAIN, 10_000, datetime.fromtimestamp(AT - 1, timezone.utc).isoformat(), "", "0xh10000"))
    for i, (ts, price, pool, kind) in enumerate(
            (s + (POOL, "v3_swap"))[:4] if len(s) == 2 else s for s in swaps):
        block = 1000 + i
        if headers:
            c.execute("INSERT INTO block_times VALUES (?,?,?,?,?)",
                      (CHAIN, block, datetime.fromtimestamp(ts, timezone.utc).isoformat(), "",
                       "0xh%d" % block if hashes else None))
        c.execute("INSERT INTO pool_ticks (chain_id, pool, kind, block_no, tx_sig, log_index, "
                  "amount0, amount1, observed_at, sqrt_price_x96, block_hash) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (CHAIN, pool, kind, block, "0xtx%d" % i, 0, "1", "-1",
                   datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                   None if kind != "v3_swap" else _sqrt_for(
                       price, ((meta or {}).get(pool) or (0, 0, 0))[2] or 0), "0xh%d" % block))
    c.commit()
    return c


def _usd(_chain, _token):
    return 2000.0


def _series(conn, **kw):
    args = dict(since=SINCE, price_fn=_usd)
    args.update(kw)
    return ov.own_spot_series(conn, CHAIN, MINT, AT, **args)


@pytest.mark.parametrize("bounds,chain,resolved,censored", [
    ((2000, 2001), CHAIN, None, True),       # inside the requested window
    ((1001, 1002), CHAIN, None, True),       # after the carried anchor, before the window
    ((900, 999), CHAIN, None, False),         # entirely before the anchor
    ((10001, 11000), CHAIN, None, False),     # after the newest required header
    ((2000, 2001), "8453", None, False),      # another chain
    ((2000, 2001), CHAIN, "recovered", False),
])
def test_public_momentum_reader_censors_only_open_gaps_needed_by_its_series(
        monkeypatch, bounds, chain, resolved, censored):
    from datetime import datetime, timezone
    from sniper.services import memecoin_volume_writer as writer
    c = _tape([(AT - 1200, 5e-6)])
    c.executescript("""
        CREATE TABLE volume_feed(chain_id TEXT,since REAL,through REAL,logs_seen INTEGER,received REAL);
        CREATE TABLE volume_feed_lag(chain_id TEXT,v4_lag_s REAL,v3_lag_s REAL,pons_lag_s REAL,observed REAL);
        CREATE TABLE volume_quote_prices(chain_id TEXT,token TEXT,usd REAL,observed REAL);
    """)
    c.execute("INSERT INTO volume_feed VALUES (?,?,?,?,?)", (CHAIN, SINCE, AT - 1, 1, AT - 1))
    c.execute("INSERT INTO volume_feed_lag VALUES (?,?,?,?,?)", (CHAIN, 0, 0, 0, AT - 1))
    c.execute("INSERT INTO volume_quote_prices VALUES (?,?,?,?)", (CHAIN, QUOTE, 2000, AT - 1))
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_M5_PROTOCOL_LAG_S", 30)
    assert writer.momentum_series(c, CHAIN, MINT, AT)["reason"] == "ok"
    # Missing endpoint headers still have a known surrounding block range.
    c.execute("INSERT INTO tick_gaps VALUES (1,?,?,?,?,?,?)",
              (chain, *bounds, "queue_overflow",
               datetime.fromtimestamp(AT - 450, timezone.utc).isoformat(), resolved))
    out = writer.momentum_series(c, CHAIN, MINT, AT)
    c.close()
    assert out["reason"] == ("tape_gap" if censored else "ok")
    assert (out["points"] == []) == censored
    assert own_momentum_facts(out)["phase"] == ("unknown" if censored else "flat")


def test_momentum_without_gap_evidence_stays_unknown():
    c = _tape([(AT - 1200, 5e-6)])
    c.execute("DROP TABLE tick_gaps")
    out = _series(c)
    c.close()
    assert out["reason"] == "gap_evidence_unavailable" and out["points"] == []


def test_the_grid_carries_the_last_swap_forward_and_skips_points_before_the_first_swap():
    out = _series(_tape([(AT - 600, 1e-6), (AT - 300, 2e-6)]))
    assert out["reason"] == "ok"
    points = out["points"]
    assert len(points) == 21                       # grid points at AT-600 .. AT, every 30 s
    assert points[0][0] == AT - 600 and points[-1][0] == AT
    assert points[0][1] == pytest.approx(1e-6 * 2000.0, rel=1e-6)
    assert points[-1][1] == pytest.approx(2e-6 * 2000.0, rel=1e-6)
    assert [p[1] for p in points[:10]] == [points[0][1]] * 10        # carried forward
    assert out["pool"] == POOL and out["quote_usd"] == 2000.0


def test_an_intra_grid_spike_is_not_a_point():
    """A buy undone two seconds later (a sandwich) never sits on a grid point, so it cannot become
    the peak that a false post_peak_confirmed would be measured against."""
    out = _series(_tape([(AT - 600, 1e-6), (AT - 445, 9e-6), (AT - 443, 1e-6)]))
    assert max(p[1] for p in out["points"]) == pytest.approx(1e-6 * 2000.0, rel=1e-6)


def test_the_spot_standing_at_the_window_start_seeds_the_grid():
    """Gamma on 345f327f7: 100 before the window, 70 then 90 inside. Without the anchor the series
    reads first 70 and runup +28.6% (hot_narrow, clip x0.75); the truth is first 100, runup 0."""
    out = _series(_tape([(AT - 1000, 100e-6), (AT - 600, 70e-6), (AT - 300, 90e-6)]))
    assert out["reason"] == "ok"
    facts = own_momentum_facts({**out, "read_at": "x", "read_ms": 1.0, "wait_would_ms": 0.0})
    assert facts["own_first_price"] == pytest.approx(100e-6 * 2000.0, rel=1e-6)
    assert facts["own_peak_price"] == pytest.approx(100e-6 * 2000.0, rel=1e-6)
    assert facts["own_runup_pct"] == pytest.approx(0.0, abs=1e-6)
    assert facts["own_giveback_pct"] == pytest.approx(10.0, rel=1e-6)
    assert out["points"][0][0] == AT - 900 and len(out["points"]) == 31


def test_an_anchor_before_the_sockets_first_header_is_not_used():
    out = _series(_tape([(AT - 1000, 100e-6), (AT - 600, 70e-6), (AT - 300, 90e-6)]),
                  since=AT - 950)
    assert out["points"][0][1] == pytest.approx(70e-6 * 2000.0, rel=1e-6)
    assert out["points"][0][0] == AT - 600


def test_a_quiet_coin_on_a_connected_socket_is_a_measured_flat():
    out = _series(_tape([(AT - 1200, 5e-6)]))
    assert out["reason"] == "ok" and len(out["points"]) == 31
    facts = own_momentum_facts({**out, "read_at": "x", "read_ms": 1.0, "wait_would_ms": 0.0})
    assert facts["phase"] == "flat"


def test_an_unverified_last_swap_before_the_window_is_no_anchor():
    c = _tape([(AT - 1000, 100e-6), (AT - 300, 90e-6)])
    c.execute("UPDATE block_times SET block_hash=NULL WHERE block_no=1000")
    out = _series(c)
    assert out["points"][0][0] == AT - 300


def test_one_pool_only_the_busiest_spot_pool():
    swaps = [(AT - 500, 1e-6, POOL, "v3_swap"), (AT - 400, 1e-6, POOL, "v3_swap"),
             (AT - 300, 1e-6, POOL, "v3_swap"), (AT - 200, 5e-6, POOL2, "v3_swap")]
    out = _series(_tape(swaps, protocols=((POOL, "v4"), (POOL2, "v3"))))
    assert out["pool"] == POOL
    assert {round(p[1], 9) for p in out["points"]} == {round(1e-6 * 2000.0, 9)}


def test_points_never_precede_the_sockets_first_header():
    out = _series(_tape([(AT - 800, 1e-6), (AT - 100, 2e-6)]), since=AT - 200)
    assert out["reason"] == "ok"
    assert min(p[0] for p in out["points"]) >= AT - 100       # the AT-800 swap cannot anchor
    assert out["covered_s"] == 200.0


@pytest.mark.parametrize("make,kw,reason", [
    (lambda: _tape([], protocols=()), {}, "no_known_pool"),
    (lambda: _tape([(AT - 100, 1e-6, POOL, "pons_v2_swap")], protocols=((POOL, "pons_v2"),)), {},
     "no_spot_pool"),
    (lambda: _tape([]), {}, "no_swaps_in_window"),
    (lambda: _tape([(AT - 1200, 1e-6)]), {"since": AT - 1000}, "no_swaps_in_window"),
    (lambda: _tape([(AT - 100, 1e-6)], hashes=False), {}, "hash_unverified"),
    (lambda: _tape([(AT - 100, 1e-6)], headers=False), {}, "hash_unverified"),
    (lambda: _tape([(AT - 100, 1e-6)]), {"price_fn": lambda *_: None}, "quote_not_priceable"),
    (lambda: _tape([(AT - 100, 1e-6)], meta={POOL: (QUOTE, MINT, None, 18, 18)}), {},
     "pool_meta_incomplete"),
    (lambda: _tape([(AT - 100, 1e-6)]), {"protocol_lag_s": {"v4": 400.0, "v3": 1.0},
                                         "max_protocol_lag_s": 30.0}, "protocol_lagging"),
    (lambda: _tape([(AT - 100, 1e-6)]), {"protocol_lag_s": {"v4": None, "v3": 1.0},
                                         "max_protocol_lag_s": 30.0}, "protocol_silent"),
    (lambda: _tape([(AT - 100, 1e-6)]), {"since": AT + 1}, "window_incomplete"),
])
def test_every_blind_case_is_an_explicit_unknown_never_flat(make, kw, reason):
    out = _series(make(), **kw)
    assert out["reason"] == reason
    assert out["points"] == []


def test_the_coin_decimals_come_from_the_token_table_when_pool_meta_lacks_them():
    """pool_meta keeps decimals on few rows (88% of 4663 and 99% of 8453 carry none, 2026-10-08);
    the paper tick engine's resolver reads the tape's token table."""
    c = _tape([(AT - 100, 1e-6), (AT - 50, 1e-6)], meta={POOL: (QUOTE, MINT, 0, 18, None)})
    assert _series(c)["reason"] == "decimals_unknown"
    c.execute("CREATE TABLE volume_token_decimals (chain_id TEXT, token TEXT, decimals INTEGER, "
              "observed REAL)")
    c.execute("INSERT INTO volume_token_decimals VALUES (?,?,?,?)", (CHAIN, MINT, 18, 0.0))
    out = _series(c)
    assert out["reason"] == "ok"
    assert out["points"][-1][1] == pytest.approx(1e-6 * 2000.0, rel=1e-6)


def test_a_coin_on_token0_is_priced_the_same_way():
    c = _tape([(AT - 100, 3e-6), (AT - 50, 3e-6)], meta={POOL: (MINT, QUOTE, 1, 18, 18)})
    out = _series(c)
    assert out["reason"] == "ok" and out["quote_token"] == QUOTE
    assert out["points"][-1][1] == pytest.approx(3e-6 * 2000.0, rel=1e-6)


def test_a_stale_feed_is_unknown():
    c = _tape([(AT - 100, 1e-6)])
    c.execute("DELETE FROM block_times WHERE block_no=10000")
    c.execute("UPDATE block_times SET block_ts='2026-01-01T00:00:00+00:00'")
    assert _series(c)["reason"] == "feed_stale"


# --- the phase, on the same thresholds as the provider path ------------------------------------------

def _read(points, **extra):
    return {"reason": "ok", "points": points, "pool": POOL, "protocol": "v4", "quote_token": QUOTE,
            "quote_usd": 2000.0, "covered_s": 900.0, "read_at": "x", "read_ms": 1.0,
            "wait_would_ms": 0.0, **extra}


def test_post_peak_on_the_own_series_with_the_peak_in_usd_per_token():
    pts = [(AT - 600 + 30 * i, p) for i, p in enumerate([1.0, 1.2, 1.5, 1.4, 1.0, 0.9])]
    facts = own_momentum_facts(_read(pts))
    assert facts["phase"] == "post_peak"
    assert facts["own_peak_price"] == 1.5 and facts["own_first_price"] == 1.0
    assert facts["own_display_price"] == 0.9
    assert facts["own_runup_pct"] == pytest.approx(50.0)
    assert facts["own_giveback_pct"] == pytest.approx(40.0)
    assert facts["points"] == 6 and facts["span_s"] == 150.0


@pytest.mark.parametrize("points,phase", [
    ([(AT - 60, 1.0), (AT - 30, 1.0)], "unknown"),                       # 2 points
    ([(AT - 20, 1.0), (AT - 10, 1.0), (AT, 1.0)], "unknown"),            # span 20 s < 30 s
    ([(AT - 60, 1.0), (AT - 30, 1.0), (AT, 1.0)], "flat"),
    ([(AT - 60, 1.0), (AT - 30, 1.05), (AT, 1.15)], "climbing"),
    ([(AT - 60, 1.0), (AT - 30, 1.8), (AT, 2.05)], "extended"),
])
def test_the_own_phase_uses_the_provider_thresholds(points, phase):
    assert own_momentum_facts(_read(points))["phase"] == phase


def test_an_unknown_read_is_an_unknown_phase_with_its_reason():
    facts = own_momentum_facts({"reason": "read_not_ready"})
    assert facts["phase"] == "unknown" and facts["reason"] == "read_not_ready"
    assert facts["own_peak_price"] is None


# --- the shadow changes nothing the provider decides ---------------------------------------------

def _book():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


KW = dict(network_id="4663", mint=MINT, signal_source="follow_activity",
          decision_at="2026-10-08T23:00:00+00:00", effective_mcap=50_000.0, assumed_micro=False,
          base_clip_usd=None, consensus_n_60m=None)


def test_the_shadow_rides_beside_the_provider_phase_and_decides_nothing():
    pts = [(AT - 600 + 30 * i, p) for i, p in enumerate([1.0, 1.2, 1.5, 1.4, 1.0, 0.9])]
    base = classify_funded_entry(_book(), market_stats={"price": 1.0}, **KW)
    shadow = classify_funded_entry(_book(), market_stats={"price": 1.0},
                                   momentum_own=_read(pts), **KW)
    assert shadow.facts["momentum_own"]["phase"] == "post_peak"
    assert shadow.momentum_phase == base.momentum_phase == "unknown"
    assert shadow.market_regime == base.market_regime
    assert shadow.risk_scale == base.risk_scale
    assert shadow.facts["momentum_basis"] == base.facts["momentum_basis"] == "provider_mid"
    assert {k: v for k, v in shadow.facts.items() if k != "momentum_own"} == base.facts


def test_the_provider_price_never_enters_the_own_series():
    pts = [(AT - 60, 1.0), (AT - 30, 1.0), (AT, 1.0)]
    a = classify_funded_entry(_book(), market_stats={"price": 1.0}, momentum_own=_read(pts), **KW)
    b = classify_funded_entry(_book(), market_stats={"price": 99.0}, momentum_own=_read(pts), **KW)
    assert a.facts["momentum_own"] == b.facts["momentum_own"]
    assert a.facts["momentum_own"]["phase"] == "flat"


def test_no_shadow_read_means_no_shadow_key():
    assert "momentum_own" not in classify_funded_entry(_book(), market_stats={}, **KW).facts


# --- zero wait (Gamma, required) ---------------------------------------------------------------

class _BlockedFuture:
    def done(self):
        return False

    def result(self, timeout=None):
        raise AssertionError("the shadow must never wait on the read")


def test_a_blocked_read_is_peeked_not_awaited():
    from sniper.services.memecoin_real_runner import _momentum_own_peek
    holder = {"future": _BlockedFuture()}
    started = time.perf_counter()
    got = _momentum_own_peek(holder)
    assert (time.perf_counter() - started) * 1000.0 < 1.0
    assert got == {"reason": "read_not_ready"}
    assert holder["peeked"] == "not_ready" and holder["classified_mono"] is not None


def test_a_blocked_read_costs_the_classification_nothing():
    from sniper.services import memecoin_real_runner as rr

    class _Ev:
        mint = MINT
        signal_source = "follow_activity"
        priced_mcap_shadow = None

    class _Id:
        network_id = "4663"

    ctx = {"effective_mcap": 50_000.0, "assumed_micro": False, "consensus_n_60m": None,
           "market_stats": {}, "mcap_basis": None, "momentum_own": {"future": _BlockedFuture()}}
    cls = rr._classify_funded_draft(_book(), _Ev(), _Id(), KW["decision_at"], ctx)
    assert cls.facts["momentum_own"]["reason"] == "read_not_ready"


def test_a_ready_read_lands_with_zero_would_be_wait():
    from concurrent.futures import Future
    from sniper.services.memecoin_real_runner import _momentum_own_peek
    fut = Future()
    fut.set_result({**_read([(AT, 1.0)]), "ready_mono": time.monotonic()})
    got = _momentum_own_peek({"future": fut})
    assert got["wait_would_ms"] == 0.0 and "ready_mono" not in got


def test_a_late_read_lands_on_the_trace_with_its_would_be_wait():
    from concurrent.futures import Future
    from sniper.services.memecoin_real_runner import _momentum_own_late_best_effort, _momentum_own_peek

    class _Trace:
        def __init__(self):
            self.marks = []

        def mark_substage(self, name, **kw):
            self.marks.append((name, kw))
            return True

    fut = Future()
    holder = {"future": fut}
    _momentum_own_peek(holder)                                   # classified while not ready
    fut.set_result({**_read([(AT - 60, 1.0), (AT - 30, 1.0), (AT, 1.0)]),
                    "ready_mono": holder["classified_mono"] + 0.012})
    trace = _Trace()
    trace.momentum_own = holder
    _momentum_own_late_best_effort(trace)
    (name, kw), = trace.marks
    assert name == "momentum_own_late"
    assert kw["wait_would_ms"] == pytest.approx(12.0, abs=0.01)
    assert kw["phase"] == "flat" and kw["reason"] == "ok"


def test_a_read_that_was_ready_at_classification_leaves_no_late_mark():
    from sniper.services.memecoin_real_runner import _momentum_own_late_best_effort

    class _Trace:
        marks = []

        def mark_substage(self, name, **kw):
            self.marks.append(name)

    trace = _Trace()
    trace.momentum_own = {"peeked": "ready"}
    _momentum_own_late_best_effort(trace)
    assert trace.marks == []


def test_the_read_pool_is_created_once_under_threads():
    from sniper.services import memecoin_real_runner as rr
    rr._MOMENTUM_POOL = None
    seen = []
    barrier = threading.Barrier(8)

    def grab():
        barrier.wait()
        seen.append(rr._momentum_read_pool())

    threads = [threading.Thread(target=grab) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(p) for p in seen}) == 1


def test_only_chains_with_an_own_source_start_a_read():
    """56, 1 and 5042 have no pre-decision own source (E14 step 2 deferred). Solana reads its own
    marks since step 0 of the Solana pre-signal spec (tests/test_memecoin_e14_solana_own_marks.py)."""
    from sniper.services import memecoin_real_runner as rr
    assert rr._start_momentum_read("56", "0xabc") is None
    assert rr._start_momentum_read("1", "0xabc") is None
    assert rr._start_momentum_read("5042", "0xabc") is None


def test_a_missing_store_is_read_failed_not_an_exception(tmp_path, monkeypatch):
    from sniper.services import memecoin_volume_writer as vw
    monkeypatch.setattr(vw, "db_path", lambda: str(tmp_path / "absent.db"))
    out = vw.read_momentum("4663", MINT)
    assert out["reason"] in ("volume_store_unavailable", "window_incomplete")
    assert out["points"] == []
