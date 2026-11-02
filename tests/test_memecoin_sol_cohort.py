"""Step 1 of spec 2026-10-09 Solana pre-signal feed: the cohort stream and its 24 h canary.

The operator approved it via Alpha, and Gamma's five points are built in.

What the stream is:
- Its own process with its own key (MEMECOIN_SOL_COHORT_HELIUS_URL) and its own DB. It is never
  inside the funded runner and never touches a funded key.
- A coin is subscribed from its first sighting:
  - own: an armed wallet's on-chain buy, from onchain_wallet_detections, with the transaction;
  - j7: a J7 follow_activity buy. Measurement only. Every coins row and every mark carries
    trigger_first.
- Each sighting extends the coin's lifetime. An explicit cap evicts the oldest last sighting, and
  counts it.
- Marks are the paper vault stream's ticks in QUOTE units, because SOL/USD lives only in the runner.
  Only fresh ticks are written.
- The stream health timeline separates a blind stream from a quiet pool.
- The canary measures bytes and credits, the binding rate, the live share, and coverage at
  decision_started_at, per trigger.
"""
import asyncio
import contextlib
import sqlite3

import pytest

from sniper.services import memecoin_sol_cohort as co

NOW = 1_791_600_000.0


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, fomo_event_id TEXT, mint TEXT, side TEXT,
            network_id TEXT, received_at TEXT, delivery_lane TEXT);
        CREATE TABLE onchain_wallet_detections (id INTEGER PRIMARY KEY, signature TEXT, mint TEXT,
            network_id TEXT, detected_at TEXT);
        CREATE TABLE memecoin_positions (id INTEGER PRIMARY KEY, mint TEXT, network_id TEXT,
            execution_mode TEXT, paper_tick_json TEXT, opened_at TEXT);
    """)
    return c


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _store():
    return co.connect_db(":memory:")


# --- key guard -----------------------------------------------------------------------------------

def test_unset_or_keyless_knob_starts_nothing():
    assert co.key_refusal("", {}) == "unset"
    assert co.key_refusal("https://mainnet.helius-rpc.com/", {}) == "keyless"


def test_a_key_shared_with_any_other_configured_url_is_refused():
    url = "https://mainnet.helius-rpc.com/?api-key=cafe0001-aaaa-bbbb-cccc-000000000000"
    env = {co.KNOB: url, "MEMECOIN_SOLANA_RPC_URL": url}
    assert co.key_refusal(url, env).startswith("shared_with:")
    assert co.key_refusal(url, {co.KNOB: url}) is None


# --- triggers --------------------------------------------------------------------------------------

def test_the_first_poll_starts_from_now_and_later_rows_become_sightings():
    led, st = _ledger(), _store()
    led.execute("INSERT INTO memecoin_events VALUES (1,'swap:old','OLD','buy','solana',?, 'j7_wallets')",
                (_iso(NOW - 100),))
    trig = co.Triggers(st, lambda: led)
    assert trig.poll() == 0                                    # no backfill
    led.execute("INSERT INTO memecoin_events VALUES (2,'swap:u1','M1','buy','solana',?, 'j7_wallets')",
                (_iso(NOW),))
    led.execute("INSERT INTO memecoin_events VALUES (3,'swap:u2','M2','sell','solana',?, 'j7_wallets')",
                (_iso(NOW),))
    led.execute("INSERT INTO memecoin_events VALUES (4,'swap:u3','0xE','buy','8453',?, 'j7_wallets')",
                (_iso(NOW),))
    led.execute("INSERT INTO onchain_wallet_detections VALUES (1,'SigA','M3','solana',?)", (_iso(NOW + 1),))
    led.execute("INSERT INTO onchain_wallet_detections VALUES (2,'SigB','M1','solana',?)", (_iso(NOW + 2),))
    assert trig.poll() == 3
    rows = {r["mint"]: dict(r) for r in st.execute("SELECT * FROM coins")}
    assert set(rows) == {"M1", "M3"}
    assert rows["M1"]["trigger_first"] == "j7" and rows["M1"]["onchain_sig"] == "SigB"
    assert rows["M1"]["j7_first_at"] == NOW and rows["M1"]["onchain_first_at"] == NOW + 2
    assert rows["M1"]["last_seen_at"] == NOW + 2
    assert rows["M3"]["trigger_first"] == "own" and rows["M3"]["j7_first_at"] is None


def test_each_sighting_extends_the_lifetime_and_the_cap_evicts_the_oldest_last_sighting():
    st = _store()
    for i, seen in enumerate((NOW - 90_000, NOW - 300, NOW - 200, NOW - 100)):
        co.record_sighting(st, "M%d" % i, "j7", seen)
    live = co.wanted(st, NOW, lifetime_s=86_400, cap=2)
    assert [r["mint"] for r in live] == ["M3", "M2"]           # M0 expired, M1 evicted by the cap
    metrics = dict(st.execute("SELECT name, value FROM counters").fetchall())
    assert metrics["cap_hits"] == 1 and metrics["evictions"] == 1
    co.record_sighting(st, "M0", "own", NOW, sig="SigZ")       # a new sighting revives it
    assert "M0" in [r["mint"] for r in co.wanted(st, NOW, lifetime_s=86_400, cap=10)]


# --- binding candidates --------------------------------------------------------------------------

def test_an_own_coin_binds_from_its_source_transaction():
    st = _store()
    co.record_sighting(st, "M3", "own", NOW, sig="SigA")
    (row,) = co.wanted(st, NOW, lifetime_s=86_400, cap=10)
    assert co.origin_of(row) == "swap:raybot:SigA:"


def test_a_j7_coin_tries_the_paper_binding_then_the_pump_curve_then_the_canonical_pool():
    led = _ledger()
    led.execute("INSERT INTO memecoin_positions VALUES (1,'4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R','solana','paper',"
                "'{\"binding\": \"entry_route\", \"pool\": \"PaperPool1\"}', ?)", (_iso(NOW - 50),))
    from sniper.execution.solana_native_entry_quote import accounts_for
    derived = accounts_for("4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R")
    assert co.candidates(led, "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R") == ["PaperPool1", derived["curve"], derived["pool"]]


def test_the_resolver_takes_the_first_candidate_that_binds(monkeypatch):
    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger)
    tried = []

    def base_resolve(self, mint, pool, origin):
        tried.append(pool)
        return ("entry_route", ("Owner", "CoinV", "So11111111111111111111111111111111111111112",
                                "QuoteV")) if pool == "Good" else (None, None)
    monkeypatch.setattr(co.PaperSolTicks, "_resolve", base_resolve)
    monkeypatch.setattr(co, "candidates", lambda _led, _mint: ["Bad1", "Good", "Never"])
    kind, got = src._resolve("MintY", None, None)
    assert kind == "entry_route" and tried == ["Bad1", "Good"]


# --- the sampler -----------------------------------------------------------------------------------

class _Tick:
    def __init__(self, price, kind="sol_swap"):
        self.price, self.tick_kind = price, kind


class _Src:
    def __init__(self, ticks):
        self.ticks = ticks

    def tick(self, pos, binding, now):
        got = self.ticks.get(pos["mint"])
        return (got, None) if got else (None, "no_mark")

    def binding(self, cid):
        return {"binding": "entry_route", "pool": "P-%s" % cid, "quote_mint": "So11111111111111111111111111111111111111112"}


def test_the_sampler_writes_fresh_ticks_in_quote_units_with_the_trigger():
    st = _store()
    co.record_sighting(st, "M1", "own", NOW, sig="S1")
    co.record_sighting(st, "M2", "j7", NOW)
    coins = co.wanted(st, NOW, lifetime_s=86_400, cap=10)
    n = co.sample_once(st, _Src({"M1": _Tick(2.5e-07)}), coins, NOW + 15)
    assert n == 1
    (mark,) = [dict(r) for r in st.execute("SELECT * FROM marks")]
    assert mark["mint"] == "M1" and mark["price_quote"] == 2.5e-07 and mark["kind"] == "sol_swap"
    assert mark["trigger_first"] == "own" and mark["ts"] == NOW + 15


def test_old_marks_are_pruned():
    st = _store()
    st.execute("INSERT INTO marks(mint, pool, quote_mint, ts, price_quote, kind, trigger_first) "
               "VALUES ('M','P','Q',?,1.0,'sol_swap','own')", (NOW - co.RETAIN_S - 1,))
    st.execute("INSERT INTO marks(mint, pool, quote_mint, ts, price_quote, kind, trigger_first) "
               "VALUES ('M','P','Q',?,1.0,'sol_swap','own')", (NOW - 10,))
    co.prune(st, NOW)
    assert st.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 1


# --- canary metering --------------------------------------------------------------------------------

def test_every_received_message_is_metered_at_its_uncompressed_size():
    meter = co.ByteMeter()

    class _Ws:
        def __init__(self):
            self.sent = []

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            async def gen():
                for msg in ('{"a":1}', "é" * 10):
                    yield msg
            return gen()

    @contextlib.asynccontextmanager
    async def connect(url):
        yield _Ws()

    async def run():
        got = []
        async with co.counted_connect(connect, meter)("wss://x") as ws:
            await ws.send("sub")
            async for raw in ws:
                got.append(raw)
        return got
    assert asyncio.run(run()) == ['{"a":1}', "é" * 10]
    assert meter.take() == (7 + 20, 2)


def test_a_minute_of_bytes_becomes_credits_at_two_per_tenth_of_a_megabyte():
    st = _store()
    co.record_minute(st, minute=100, bytes_=250_000, messages=40, rpc_calls=3, subscribed=12)
    row = dict(st.execute("SELECT * FROM meter").fetchone())
    assert row["credits"] == pytest.approx(250_000 / 100_000 * 2 + 3)


def test_the_stream_health_timeline_records_each_epoch():
    st = _store()
    co.record_epoch(st, started=NOW, ended=NOW + 300, vaults=40, error=None)
    co.record_epoch(st, started=NOW + 301, ended=NOW + 302, vaults=40, error="ConnectionClosed")
    rows = [dict(r) for r in st.execute("SELECT * FROM epochs ORDER BY started")]
    assert [r["error"] for r in rows] == [None, "ConnectionClosed"]


# --- coverage, measured at the decision ------------------------------------------------------------

def test_coverage_counts_only_marks_before_the_decision_and_splits_first_sightings():
    st = _store()
    led = sqlite3.connect(":memory:")
    led.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, network_id TEXT, mint TEXT, "
                "decision_started_at TEXT, delivery_lane TEXT)")
    co.record_sighting(st, "COV", "own", NOW - 1000, sig="S")
    co.record_sighting(st, "FIRST", "j7", NOW - 5)
    for k in range(4):
        st.execute("INSERT INTO marks(mint, pool, quote_mint, ts, price_quote, kind, trigger_first) "
                   "VALUES ('COV','P','Q',?,1.0,'sol_swap','own')", (NOW - 600 + 100 * k,))
    st.execute("INSERT INTO marks(mint, pool, quote_mint, ts, price_quote, kind, trigger_first) "
               "VALUES ('FIRST','P','Q',?,1.0,'sol_swap','j7')", (NOW + 30,))    # after its decision
    for aid, mint in (("a1", "COV"), ("a2", "FIRST"), ("a3", "NEVER")):
        led.execute("INSERT INTO memecoin_entry_attempts VALUES (?, 'solana', ?, ?, 'j7_wallets')",
                    (aid, mint, _iso(NOW)))
    out = co.coverage(led, st, since=_iso(NOW - 10), until=_iso(NOW + 10))
    assert out["attempts"] == 3
    assert out["covered"] == {"own": 1, "j7": 0}
    assert out["first_sighting"] == 1 and out["never_sighted"] == 1


def test_each_coins_binding_state_is_kept_so_an_unresolved_graduation_is_never_read_as_quiet():
    """Gamma on the plan: a curve that completes and never rebinds ends graduation_unresolved, and
    its silence is a lost binding, not a quiet pool."""
    st = _store()
    co.record_sighting(st, "A", "own", NOW, sig="S")
    co.record_sighting(st, "B", "j7", NOW)
    coins = co.wanted(st, NOW, lifetime_s=86_400, cap=10)

    class _Bindings:
        def binding(self, cid):
            return {coins[0]["cid"]: {"binding": "graduation_unresolved", "pool": "Curve"},
                    coins[1]["cid"]: {"binding": "entry_route", "pool": "P"}}.get(cid)
    co.sync_bindings(st, _Bindings(), coins, NOW)
    got = dict(st.execute("SELECT mint, binding FROM coins").fetchall())
    assert got == {"A": "graduation_unresolved", "B": "entry_route"}
    assert co.binding_summary(st) == {"graduation_unresolved": 1, "entry_route": 1}


def test_stream_rows_are_counted_by_the_last_sighting_so_one_run_prices_every_lifetime():
    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger, clock=lambda: NOW)
    with src._lock:
        src._bindings[1] = {"binding": "entry_route", "mint": "A", "pool": "PoolA", "coin_vault": "c",
                            "quote_vault": "q", "quote_mint": "Q", "venue_kind": "amm"}
        src._bindings[2] = {"binding": "entry_route", "mint": "B", "pool": "CurveB", "coin_vault": "c2",
                            "quote_vault": "CurveB", "quote_mint": "So1", "venue_kind": "pump_curve"}
    src.last_seen = {1: NOW - 600, 2: NOW - 7 * 3600}
    src._on_row({"pool": "PoolA", "kind": "sol_swap"})
    src._on_row({"pool": "PoolA", "kind": "sol_swap"})
    src._on_row({"pool": "CurveB", "kind": "sol_curve_state"})
    src._on_row({"pool": "Unknown", "kind": "sol_swap"})
    assert src.counters["rows_age:lt1h"] == 2 and src.counters["rows_age:6to24h"] == 1
    assert src.counters["rows_age:unknown"] == 1


def test_the_streams_counters_are_snapshotted_into_the_store():
    st = _store()
    co.snapshot_counters(st, {"graduations": 3, "rebinds": 2, "grad_unresolved": 1, "rpc:getTransaction": 9})
    got = dict(st.execute("SELECT name, value FROM counters").fetchall())
    assert got["src:graduations"] == 3 and got["src:grad_unresolved"] == 1
    co.snapshot_counters(st, {"graduations": 4})
    assert dict(st.execute("SELECT name, value FROM counters").fetchall())["src:graduations"] == 4


def test_the_lifetime_projection_scales_stream_credits_by_the_rows_each_lifetime_keeps():
    counters = {"src:rows_age:lt1h": 600, "src:rows_age:1to6h": 300, "src:rows_age:6to24h": 100}
    got = co.lifetime_projection(counters, stream_credits_month=1_000_000.0, rpc_credits_month=50_000.0)
    assert got == {"1h": pytest.approx(650_000.0), "6h": pytest.approx(950_000.0),
                   "24h": pytest.approx(1_050_000.0)}


def test_the_plan_is_the_cheapest_that_holds_the_month():
    assert co.plan_for(900_000) == ("Free", 0)
    assert co.plan_for(5_300_000) == ("Developer", 49)
    assert co.plan_for(47_000_000) == ("Business", 499)
    assert co.plan_for(250_000_000) == (None, None)


def test_the_process_is_default_off_and_refuses_a_shared_key():
    assert co.run({}) == 0
    url = "https://mainnet.helius-rpc.com/?api-key=cafe0001-aaaa-bbbb-cccc-000000000000"
    assert co.run({co.KNOB: url, "MEMECOIN_PAPER_TICKS_HELIUS_URL": url}) == 0


def test_the_cap_evicts_j7_only_coins_before_own_sighted_ones_and_counts_by_trigger():
    """Gamma LOW 1: step 1b reads the own population, so the cap must spend J7-only coins first."""
    st = _store()
    co.record_sighting(st, "OWN_OLD", "own", NOW - 900, sig="S1")
    co.record_sighting(st, "J7_NEW", "j7", NOW - 10)
    co.record_sighting(st, "J7_MID", "j7", NOW - 100)
    live = [r["mint"] for r in co.wanted(st, NOW, lifetime_s=86_400, cap=2)]
    assert live == ["OWN_OLD", "J7_NEW"]
    metrics = dict(st.execute("SELECT name, value FROM counters").fetchall())
    assert metrics["evictions:j7_only"] == 1 and metrics.get("evictions:own", 0) == 0


def test_a_refused_key_leaves_a_marker_the_watchdog_honours_and_a_start_clears_it(tmp_path):
    """Gamma LOW 2: a refused key used to exit 0 and be relaunched every cron tick."""
    marker = tmp_path / "cohort.refused"
    url = "https://mainnet.helius-rpc.com/?api-key=cafe0001-aaaa-bbbb-cccc-000000000000"
    assert co.run({co.KNOB: url, "MEMECOIN_PAPER_TICKS_HELIUS_URL": url}, marker_path=str(marker)) == 0
    text = marker.read_text()
    assert text.startswith("shared_with:MEMECOIN_PAPER_TICKS_HELIUS_URL") and "cafe0001" not in text
    assert co.run({}, marker_path=str(marker)) == 0              # unset: off, not a refusal
    assert not marker.exists()


def test_an_own_sighting_after_a_failed_j7_binding_tries_again_from_its_transaction(monkeypatch):
    """Canary hour 1 (2026-10-09 09:48Z): a coin J7 sighted first (no transaction) spent its three
    candidate tries and stayed unbound, and the armed wallet's later on-chain buy, which carries the
    transaction, never got a try. That is the own population step 1b reads."""
    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger)
    calls = []

    def resolve(self, mint, pool, origin):
        calls.append(origin)
        if origin:
            return "source_swap", ("Owner", "CoinV", "So11111111111111111111111111111111111111112", "QuoteV")
        return None, None
    monkeypatch.setattr(co.PaperSolTicks, "_resolve", resolve)
    monkeypatch.setattr(co, "candidates", lambda _led, _mint: ["NoSuchPool"])
    src.wanted = [(7, "MintZ", None, None)]
    for _ in range(4):
        src.resolve_pass()
    assert calls == [None, None, None] and src.binding(7) == {"binding": "unbound"}
    src.wanted = [(7, "MintZ", None, "swap:raybot:SigOwn:")]
    src.resolve_pass()
    assert calls[-1] == "swap:raybot:SigOwn:"
    assert src.binding(7)["binding"] == "source_swap"


@pytest.fixture
def graduated_cohort():
    """Both the historical curve and canonical AMM have resolvable real-shaped receipts."""
    from sniper.execution.solana_native_entry_quote import accounts_for
    from sniper.services.memecoin_solana_amounts import PUMP_PROGRAM_ID, WSOL
    from sniper.services.memecoin_solana_tape import NATIVE_SOL

    mint = "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R"
    addresses = accounts_for(mint)
    curve, pool = addresses["curve"], addresses["pool"]
    payer = "4E1f85HhyYQ8LwWjxsGd7JuemNkBC8hjkDN3VHo98Q3o"

    def token(owner, mint_, index, amount):
        return {"mint": mint_, "owner": owner, "accountIndex": index,
                "uiTokenAmount": {"amount": str(amount), "decimals": 6}}

    def receipt(owner, coin_vault, quote_vault=None):
        keys = [payer, owner, coin_vault] + ([quote_vault] if quote_vault else [])
        pre = [token(owner, mint, 2, 1000)]
        post = [token(owner, mint, 2, 900)]
        if quote_vault:
            pre.append(token(owner, WSOL, 3, 500))
            post.append(token(owner, WSOL, 3, 550))
        return {"transaction": {"message": {"accountKeys": keys,
                "instructions": [] if quote_vault else [{"programId": PUMP_PROGRAM_ID}]}},
                "meta": {"preTokenBalances": pre, "postTokenBalances": post,
                         "preBalances": [1000000, 10000, 2039280],
                         "postBalances": [999995, 10500, 2039280], "err": None}}

    transactions = {"curve-tx": receipt(curve, "CurveCoinVault"),
                    "amm-tx": receipt(pool, "AmmCoinVault", "AmmQuoteVault")}
    reads = []

    def rpc(method, params):
        reads.append((method, params[0]))
        if method == "getSignaturesForAddress":
            return [{"signature": "curve-tx" if params[0] == curve else "amm-tx"}]
        if method == "getTransaction":
            return transactions[params[0]]
        raise AssertionError((method, params))

    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger,
                            rpc_call=rpc, clock=lambda: NOW, mono=lambda: 100.0)
    src.wanted = [(1, mint, None, None)]
    src._bindings[1] = src._entry("entry_route", mint, (curve, "CurveCoinVault", NATIVE_SOL, curve))
    return src, mint, curve, pool, reads


def test_graduation_binds_canonical_amm_without_rediscovering_completed_curve(graduated_cohort):
    src, _mint, curve, pool, reads = graduated_cohort
    src._graduated(curve)
    src.resolve_pass()
    assert src._bindings[1]["pool"] == pool
    assert src._bindings[1]["venue_kind"] == "amm"
    assert src.binding(1)["binding"] == "graduated"
    assert reads == [("getSignaturesForAddress", pool), ("getTransaction", "amm-tx")]
    assert src.counters["rebinds"] == 1


def test_canonical_amm_fixture_also_resolves_with_base_resolver(graduated_cohort):
    src, mint, _curve, pool, _reads = graduated_cohort
    kind, got = co.PaperSolTicks._resolve(src, mint, pool, None)
    assert kind == "entry_route" and got[0] == pool
    assert got[1:] == ("AmmCoinVault", "So11111111111111111111111111111111111111112", "AmmQuoteVault")


@pytest.mark.parametrize("failure", ["not_found", "unreadable"])
def test_unavailable_canonical_amm_keeps_graduation_retry_without_curve_fallback(graduated_cohort, failure):
    src, _mint, curve, pool, reads = graduated_cohort
    original_rpc = src._rpc_call

    def rpc(method, params):
        if method == "getSignaturesForAddress" and params[0] == pool:
            reads.append((method, pool))
            if failure == "unreadable":
                raise TimeoutError()
            return []
        return original_rpc(method, params)

    src._rpc_call = rpc
    src._graduated(curve)
    src.resolve_pass()
    assert src.binding(1)["binding"] == "graduating"
    assert src._bindings[1]["grad_last"] == failure
    assert reads == [("getSignaturesForAddress", pool)]
    assert src.counters["rebinds"] == 0


def _stream_attempts_report(monkeypatch, attempts):
    """Run the real stream and its report across fake handshake/live/close intervals."""
    import websockets
    from scripts.research import sol_cohort_canary_report as canary

    clock = [NOW]
    pending = iter(attempts)

    @contextlib.asynccontextmanager
    async def connect(*_args, **_kwargs):
        handshake, live, failure, closing = next(pending)
        clock[0] += handshake
        if failure == "handshake":
            raise TimeoutError("handshake failed")

        class Socket:
            async def send(self, _data):
                pass

            def __aiter__(self):
                async def frames():
                    clock[0] += live
                    if failure == "connected":
                        raise TimeoutError("connected stream failed")
                    if False:
                        yield ""
                return frames()

        try:
            yield Socket()
        finally:
            clock[0] += closing

    monkeypatch.setattr(websockets, "connect", connect)
    epochs = []
    stream = co._metered_stream(co.ByteMeter(), epochs, lambda: clock[0])
    for _attempt in attempts:
        try:
            asyncio.run(stream({"v1": object()}, lambda row: None, ws_url="wss://offline"))
        except TimeoutError:
            pass
    st = _store()
    for epoch in epochs:
        co.record_epoch(st, **epoch)
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_entry_attempts (network_id TEXT, mint TEXT, "
                   "decision_started_at TEXT, delivery_lane TEXT)")
    return canary.report(st, ledger, _iso(NOW), _iso(clock[0]))["stream"], epochs


@pytest.mark.parametrize("attempt,expected_share,errors", [
    ((60, 0, "handshake", 0), 0.0, 1),
    ((0, 60, None, 0), 1.0, 0),
    ((20, 40, None, 0), 0.6667, 0),
    ((20, 40, "connected", 0), 0.6667, 1),
    ((0, 40, None, 20), 0.6667, 0),
])
def test_uptime_counts_connected_interval_and_retains_attempt_errors(monkeypatch, attempt, expected_share, errors):
    stream, _epochs = _stream_attempts_report(monkeypatch, [attempt])
    assert stream["live_share"] == expected_share
    assert stream["epochs"] == 1 and stream["epoch_errors"] == errors


def test_reconnects_keep_separate_live_intervals_after_failed_handshake(monkeypatch):
    attempts = [(10, 0, "handshake", 0), (10, 20, "connected", 0), (0, 30, None, 0)]
    stream, epochs = _stream_attempts_report(monkeypatch, attempts)
    assert stream["live_share"] == 0.7143
    assert stream["epochs"] == 3 and stream["epoch_errors"] == 2
    assert len(epochs) == 3


def test_instant_failed_handshake_at_window_start_still_counts_attempt_and_error(monkeypatch):
    stream, _epochs = _stream_attempts_report(monkeypatch, [(0, 0, "handshake", 0), (0, 60, None, 0)])
    assert stream["epochs"] == 2 and stream["epoch_errors"] == 1
    assert stream["live_share"] == 1.0


def test_legacy_epoch_uptime_is_unknown_and_local_schema_migrates_idempotently(tmp_path):
    from scripts.research import sol_cohort_canary_report as canary

    path = tmp_path / "cohort.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE epochs (id INTEGER PRIMARY KEY, started REAL, ended REAL, vaults INTEGER, error TEXT)")
    old.execute("INSERT INTO epochs VALUES (1, ?, ?, 2, NULL)", (NOW, NOW + 60))
    old.commit()
    old.close()
    st = co.connect_db(str(path))
    st.close()
    st = co.connect_db(str(path))
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_entry_attempts (network_id TEXT, mint TEXT, "
                   "decision_started_at TEXT, delivery_lane TEXT)")
    stream = canary.report(st, ledger, _iso(NOW), _iso(NOW + 60))["stream"]
    assert stream["live_share"] is None
    assert stream["epochs"] == 1 and stream["unknown_epochs"] == 1


def test_read_only_report_accepts_unmigrated_legacy_epochs_as_unknown():
    from scripts.research import sol_cohort_canary_report as canary

    st = _store()
    st.execute("DROP TABLE epochs")
    st.execute("CREATE TABLE epochs (id INTEGER PRIMARY KEY, started REAL, ended REAL, vaults INTEGER, error TEXT)")
    st.execute("INSERT INTO epochs VALUES (1, ?, ?, 2, 'TimeoutError')", (NOW, NOW + 60))
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_entry_attempts (network_id TEXT, mint TEXT, "
                   "decision_started_at TEXT, delivery_lane TEXT)")
    stream = canary.report(st, ledger, _iso(NOW), _iso(NOW + 60))["stream"]
    assert stream["live_share"] is None and stream["unknown_epochs"] == 1
    assert stream["epoch_errors"] == 1 and stream["observed_live_seconds"] == 0
    assert {r[1] for r in st.execute("PRAGMA table_info(epochs)")} == {"id", "started", "ended", "vaults", "error"}


@pytest.mark.parametrize("since,until,share", [(0, 100, None), (30, 60, 1.0), (0, 10, 0.0)])
def test_report_clips_connected_time_to_requested_window(since, until, share):
    from scripts.research import sol_cohort_canary_report as canary

    st = _store()
    co.record_epoch(st, started=NOW, ended=NOW + 80, vaults=2, error=None,
                    connected_at=NOW + 20, connection_observed=True)
    co.record_epoch(st, started=NOW - 100, ended=NOW - 50, vaults=2, error=None)
    ledger = sqlite3.connect(":memory:")
    ledger.execute("CREATE TABLE memecoin_entry_attempts (network_id TEXT, mint TEXT, "
                   "decision_started_at TEXT, delivery_lane TEXT)")
    stream = canary.report(st, ledger, _iso(NOW + since), _iso(NOW + until))["stream"]
    assert stream["live_share"] == share and stream["unknown_epochs"] == 0
    if share is None:
        assert stream['unknown_seconds'] == 20 and stream['observation_coverage'] == .8
        assert stream['observed_live_seconds'] == 60


@pytest.mark.parametrize("trigger", ["own", "j7"])
def test_recurring_mint_retains_current_stream_cost_in_renewed_one_hour_lifetime(trigger):
    st = _store()
    co.record_sighting(st, "M", trigger, NOW - 7200, sig="first")
    co.record_sighting(st, "M", trigger, NOW, sig="renewed")
    (coin,) = co.wanted(st, NOW, lifetime_s=3600)
    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger, clock=lambda: NOW)
    src._bindings[coin["cid"]] = {"pool": "P", "venue_kind": "amm"}
    src.last_seen = {coin["cid"]: coin["last_seen_at"]}
    src._on_row({"pool": "P", "kind": "sol_swap"})
    counters = {"src:" + key: value for key, value in src.counters.items()}
    projected = co.lifetime_projection(counters, stream_credits_month=1_000_000, rpc_credits_month=50_000)
    assert projected == {"1h": 1_050_000, "6h": 1_050_000, "24h": 1_050_000}


@pytest.mark.parametrize("age,bucket", [(0, "lt1h"), (3600, "lt1h"), (3600.001, "1to6h"),
                                       (21600, "1to6h"), (21600.001, "6to24h"), (86400, "6to24h")])
def test_cost_bucket_boundaries_match_inclusive_last_sighting_retention(age, bucket):
    src = co.CohortSolTicks("https://x/?api-key=k", ledger_factory=_ledger, clock=lambda: NOW)
    src._bindings[1] = {"pool": "P", "venue_kind": "amm"}
    src.last_seen = {1: NOW - age}
    src._on_row({"pool": "P", "kind": "sol_swap"})
    assert src.counters["rows_age:" + bucket] == 1
