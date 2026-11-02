"""Blind-chain probe capture + candidate cohorts (2026-07-16 capture-coverage spec).

Chain 4663 is DexScreener-blind (545k/545k missing_pair), so its forward tape must come
from bounded GeckoTerminal batches running off the money path; and every concrete-chain coin the feed
shows us gets a low-priority `candidate` capture cohort so lifecycle datasets stop being
survivorship-only. Spec: docs/superpowers/specs/2026-07-16-memecoin-capture-coverage-design.md
"""
import sqlite3
import importlib

from sniper.persistence import db_memecoin as m


class _CaptureProxy:
    def __getattr__(self, name):
        return getattr(importlib.import_module("sniper.services.memecoin_capture_v2"), name)


capture = _CaptureProxy()

NOW = "2026-07-16T12:00:00+00:00"
LATER = "2026-07-16T12:05:00+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _register(conn, network, mint, *, context="signal", priority=100,
              observed=NOW, expires="2026-07-17T12:00:00+00:00"):
    capture.register_capture_cohort(conn, network, mint, context, observed, expires, priority)


def _obs(conn, mint):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_quote_observations_v2 WHERE mint=? ORDER BY id", (mint,))]


def _cohort(conn, network, mint):
    return dict(conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 WHERE network_id=? AND mint=?",
        (network, mint)).fetchone())


# ---------------------------------------------------------------- blind pass


def test_blind_pass_writes_probe_ok_and_advances_cohort():
    conn = _conn()
    _register(conn, "4663", "0xaaa")
    result = capture.capture_blind_pass(
        conn, NOW,
        price_batch_fn=lambda keys: {("4663", "0xaaa"): {
            "price": 0.5, "liquidity": None, "mcap": 1_000_000.0, "source": "probe"}},
        max_mints=8, blind_chains={"4663"}, period_s=60.0,
    )
    rows = _obs(conn, "0xaaa")
    assert len(rows) == 1
    row = rows[0]
    assert row["quality"] == "probe_ok"
    assert row["dex_id"] == "probe"
    assert row["pair_address"] == "probe:4663"
    assert row["price"] == 0.5
    assert row["liquidity"] is None          # never fabricated from a swap quote
    assert row["market_cap"] == 1_000_000.0
    assert row["capture_version"] == 2
    cohort = _cohort(conn, "4663", "0xaaa")
    assert cohort["last_good_price"] == 0.5
    assert cohort["next_due_at"] > NOW       # advanced by period_s
    assert result.captured == 1
    sweep = conn.execute(
        "SELECT * FROM memecoin_quote_sweeps_v2 WHERE sweep_id=?", (result.sweep_id,)).fetchone()
    assert sweep is not None and sweep["captured_count"] == 1


def test_blind_pass_derives_dex_id_and_pair_from_stats_source():
    """A GeckoTerminal mark keeps exact provider provenance in the capture row."""
    conn = _conn()
    _register(conn, "4663", "0xgt")
    capture.capture_blind_pass(
        conn, NOW,
        price_batch_fn=lambda keys: {("4663", "0xgt"): {
            "price": 0.5, "liquidity": None, "mcap": 42.0, "source": "geckoterminal"}},
        max_mints=8, blind_chains={"4663"}, period_s=60.0)
    row = _obs(conn, "0xgt")[0]
    assert row["quality"] == "probe_ok"
    assert row["dex_id"] == "geckoterminal"
    assert row["pair_address"] == "geckoterminal:4663"
    assert row["market_cap"] == 42.0


def test_default_blind_batch_returns_gt_rows_and_leaves_misses_absent():
    """The blind worker returns GeckoTerminal rows only; missing keys stay missing."""
    gt_keys = []

    def gt_batch(keys):
        gt_keys.extend([tuple(k) for k in keys])
        return {("4663", "0xa"): {"price": 1.0, "liquidity": None, "mcap": None,
                                  "source": "geckoterminal"}}

    fn = capture._default_blind_price_batch_fn({"4663"}, gt_batch_fn=gt_batch)
    out = fn([("4663", "0xa"), ("4663", "0xb")])
    assert out[("4663", "0xa")]["source"] == "geckoterminal"
    assert ("4663", "0xb") not in out
    assert gt_keys == [("4663", "0xa"), ("4663", "0xb")]


def test_default_blind_batch_does_not_bind_a_second_provider(monkeypatch):
    from sniper.services import memecoin_evm_price as price

    monkeypatch.setattr(
        price, "build_evm_price_for",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("second provider bound")),
    )
    fn = capture._default_blind_price_batch_fn(
        {"4663"}, gt_batch_fn=lambda keys: {
            ("4663", "0xa"): {"price": 1.0, "source": "geckoterminal"}
        },
    )

    assert fn([("4663", "0xa"), ("4663", "0xb")]) == {
        ("4663", "0xa"): {"price": 1.0, "source": "geckoterminal"}
    }


def test_blind_pass_selects_only_blind_chains_and_respects_budget():
    conn = _conn()
    _register(conn, "solana", "SOLMINT", priority=400)
    _register(conn, "4663", "0xhigh", priority=400)
    _register(conn, "4663", "0xmid", priority=100)
    _register(conn, "4663", "0xlow", priority=50)
    seen = []

    def price_batch_fn(keys):
        seen.extend(keys)
        return {tuple(k): {"price": 1.0, "liquidity": None, "mcap": None, "source": "geckoterminal"}
                for k in keys}

    result = capture.capture_blind_pass(
        conn, NOW, price_batch_fn=price_batch_fn, max_mints=2,
        blind_chains={"4663"}, period_s=60.0,
    )
    assert ("solana", "SOLMINT") not in [tuple(k) for k in seen]
    assert [tuple(k) for k in seen] == [("4663", "0xhigh"), ("4663", "0xmid")]
    assert result.budget_skipped == 1                      # 0xlow over budget
    assert _cohort(conn, "solana", "SOLMINT")["next_due_at"] == NOW   # untouched
    assert _cohort(conn, "4663", "0xlow")["next_due_at"] == NOW       # not advanced
    assert _obs(conn, "SOLMINT") == [] and _obs(conn, "0xlow") == []


def test_blind_pass_default_uses_thirty_addresses_in_one_batch(monkeypatch):
    """GT supports 30 addresses per request; use the full existing request without another call."""
    from sniper.settings import memecoin as cfg

    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_BLIND_PERIOD_S", 60.0)
    conn = _conn()
    for index in range(31):
        _register(conn, "4663", f"0x{index:040x}")
    calls = []

    def price_batch_fn(keys):
        calls.append(list(keys))
        return {tuple(key): {"price": 1.0, "source": "geckoterminal"} for key in keys}

    result = capture.capture_blind_pass(
        conn, NOW, price_batch_fn=price_batch_fn, blind_chains={"4663"},
    )

    assert cfg.MEMECOIN_CAPTURE_BLIND_MAX_MINTS == 30
    assert len(calls) == 1 and len(calls[0]) == 30
    assert result.requested == 30 and result.budget_skipped == 1


def test_blind_pass_prioritizes_approved_pending_horizon_before_other_signals():
    conn = _conn()
    approved, ordinary, candidate = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40
    for event_id, mint, is_approved in (
        ("approved", approved, 1),
        ("ordinary", ordinary, 0),
    ):
        conn.execute(
            "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, normalized_side, "
            "network_id, mint, their_price, observed_at, was_real_approved, capture_version, "
            "clock_domain, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "large_buy", "buy", "4663", mint, 1.0, NOW, is_approved, 2,
             "observed_real_utc", "{}"),
        )
        capture.create_outcome_v2(
            conn, upstream_event_id=event_id, source_user_id="u", handle="h",
            network_id="4663", mint=mint, ticker="T", signal_observed_at=NOW,
            ref_price=1.0, horizons=[60], created_at=NOW,
        )
    # Deliberately give the unapproved signal a higher stored priority. Eligibility truth wins.
    _register(conn, "4663", approved, priority=50)
    _register(conn, "4663", ordinary, priority=100)
    _register(conn, "4663", candidate, context="candidate", priority=50)
    seen = []

    capture.capture_blind_pass(
        conn, NOW, price_batch_fn=lambda keys: seen.extend(keys) or {}, max_mints=1,
        blind_chains={"4663"}, period_s=60.0,
    )

    assert seen == [("4663", approved)]


def test_blind_pass_reuses_fresh_position_marks_before_network_budget():
    conn = _conn()
    _register(conn, "4663", "0xfresh", priority=50)
    _register(conn, "4663", "0xstale", priority=400)
    _register(conn, "4663", "0xmissing", priority=100)
    conn.executemany(
        "INSERT INTO memecoin_position_marks(chain_id,mint,sweep_ts,mid,liquidity,mcap,source,quality) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [
            ("4663", "0xfresh", "2026-07-16T11:59:30+00:00", 0.5, 1200.0,
             900_000.0, "geckoterminal", "live"),
            ("4663", "0xstale", "2026-07-16T11:57:00+00:00", 0.4, 800.0,
             700_000.0, "geckoterminal", "live"),
        ],
    )
    conn.commit()
    seen = []

    def price_batch_fn(keys):
        seen.extend(keys)
        return {("4663", "0xstale"): {
            "price": 0.45, "liquidity": 850.0, "mcap": 720_000.0,
            "source": "geckoterminal",
        }}

    result = capture.capture_blind_pass(
        conn, NOW, price_batch_fn=price_batch_fn, max_mints=1,
        blind_chains={"4663"}, period_s=60.0,
    )

    assert seen == [("4663", "0xstale")]
    fresh = _obs(conn, "0xfresh")[0]
    assert fresh["quality"] == "probe_ok"
    assert fresh["pair_address"] == "position_mark:4663"
    assert fresh["dex_id"] == "geckoterminal"
    assert fresh["observed_at"] == "2026-07-16T11:59:30+00:00"
    assert fresh["source_ts"] == "2026-07-16T11:59:30+00:00"
    assert fresh["price"] == 0.5
    assert fresh["liquidity"] == 1200.0
    assert fresh["market_cap"] == 900_000.0
    assert _obs(conn, "0xmissing") == []
    assert result.eligible == 3
    assert result.requested == 2
    assert result.captured == 2
    assert result.budget_skipped == 1


def test_blind_pass_unpriced_writes_probe_unpriced_gap():
    conn = _conn()
    _register(conn, "4663", "0xdead")
    capture.capture_blind_pass(
        conn, NOW, price_batch_fn=lambda keys: {}, max_mints=8,
        blind_chains={"4663"}, period_s=60.0,
    )
    rows = _obs(conn, "0xdead")
    assert len(rows) == 1
    assert rows[0]["quality"] == "probe_unpriced"
    assert rows[0]["price"] is None
    cohort = _cohort(conn, "4663", "0xdead")
    assert cohort["last_good_price"] is None               # no fake anchor
    assert cohort["next_due_at"] > NOW                     # still rotates


def test_blind_pass_ratio_guard_labels_outlier_and_keeps_last_good():
    conn = _conn()
    _register(conn, "4663", "0xbbb")
    ok = {("4663", "0xbbb"): {"price": 0.003, "liquidity": None, "mcap": None,
                                  "source": "geckoterminal"}}
    capture.capture_blind_pass(conn, NOW, price_batch_fn=lambda k: ok, max_mints=8,
                               blind_chains={"4663"}, period_s=60.0)
    spike = {("4663", "0xbbb"): {"price": 17.099, "liquidity": None, "mcap": None,
                                 "source": "geckoterminal"}}
    capture.capture_blind_pass(conn, LATER, price_batch_fn=lambda k: spike, max_mints=8,
                               blind_chains={"4663"}, period_s=60.0)
    rows = _obs(conn, "0xbbb")
    assert [r["quality"] for r in rows] == ["probe_ok", "probe_ratio_outlier"]
    assert _cohort(conn, "4663", "0xbbb")["last_good_price"] == 0.003   # spike never anchors
    assert all(r["quality"] != "ok" for r in rows)         # probe rows are opt-in, never 'ok'


# ------------------------------------------------------- main sweep exclusion


def test_main_sweep_excludes_blind_cohorts_entirely():
    conn = _conn()
    _register(conn, "4663", "0xaaa", priority=400)
    _register(conn, "solana", "SOLMINT", priority=100)
    seen = []

    def stats_batch_fn(keys):
        seen.extend(keys)
        return {("solana", "SOLMINT"): {
            "price": 1.0, "liquidity": 10.0, "market_cap": 1.0, "volume_24h": 1.0,
            "chain": "solana", "base_address": "SOLMINT", "pair_address": "p", "dex_id": "d"}}

    result = capture.capture_v2_sweep(conn, NOW, stats_batch_fn, 120,
                                      exclude_networks={"4663"})
    assert ("4663", "0xaaa") not in [tuple(k) for k in seen]
    assert result.eligible == 1                            # blind cohort not even eligible
    assert _obs(conn, "0xaaa") == []                       # no missing_pair spam
    assert _obs(conn, "SOLMINT")[0]["quality"] == "ok"


# ------------------------------------------------------------------ outcomes


def test_create_outcome_v2_accepts_blind_chain_and_rejects_unknown():
    import pytest
    conn = _conn()
    outcome_id = capture.create_outcome_v2(
        conn, upstream_event_id="ev1", source_user_id="u", handle="h", network_id="4663",
        mint="0xaaa", ticker="T", signal_observed_at=NOW, ref_price=0.5,
        horizons=[60], created_at=NOW,
    )
    row = conn.execute("SELECT network_id FROM memecoin_signal_outcomes_v2 "
                       "WHERE outcome_id=?", (outcome_id,)).fetchone()
    assert row["network_id"] == "4663"
    with pytest.raises(ValueError):
        capture.create_outcome_v2(
            conn, upstream_event_id="ev2", source_user_id="u", handle="h", network_id="9999",
            mint="0xbbb", ticker="T", signal_observed_at=NOW, ref_price=0.5,
            horizons=[60], created_at=NOW,
        )


def test_assign_due_horizons_accepts_probe_ok_but_not_outlier():
    conn = _conn()
    capture.create_outcome_v2(
        conn, upstream_event_id="ev1", source_user_id="u", handle="h", network_id="4663",
        mint="0xaaa", ticker="T", signal_observed_at=NOW, ref_price=0.5,
        horizons=[60], created_at=NOW,
    )
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, pair_address, "
        "dex_id, observed_at, price, quality, contexts, capture_version) "
        "VALUES ('s1','4663','0xaaa','zerox:4663','zerox','2026-07-16T12:01:05+00:00',"
        "0.6,'probe_ok','signal',2)")
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, pair_address, "
        "dex_id, observed_at, price, quality, contexts, capture_version) "
        "VALUES ('s2','4663','0xaaa','zerox:4663','zerox','2026-07-16T12:01:00+00:00',"
        "99.0,'probe_ratio_outlier','signal',2)")
    conn.commit()
    capture.assign_due_horizons(conn, "2026-07-16T12:02:00+00:00", tolerance_s=90.0)
    mark = conn.execute("SELECT * FROM memecoin_horizon_marks_v2").fetchone()
    assert mark["status"] == "observed"
    assert mark["price"] == 0.6                            # probe_ok used, outlier ignored


# --------------------------------------------------------- candidate cohorts


def _ev_dict(network, mint, side="buy"):
    return {"side": side, "network_id": network, "mint": mint}


def test_register_candidate_cohorts_concrete_chains_only():
    conn = _conn()
    n = capture.register_candidate_cohorts(conn, [
        _ev_dict("solana", "SOLMINT"),
        _ev_dict("4663", "0xaaa"),
        _ev_dict("evm-unknown", "0xbbb"),        # ambiguous identity -> skipped
        _ev_dict(None, "0xccc"),                 # no chain -> skipped
        _ev_dict("solana", "SELLMINT", side="sell"),   # sells don't open coverage
    ], NOW, ttl_s=3600.0)
    assert n == 2
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 ORDER BY network_id")]
    assert {(r["network_id"], r["mint"]) for r in rows} == {("solana", "SOLMINT"),
                                                            ("4663", "0xaaa")}
    for r in rows:
        assert r["contexts"] == "candidate"
        assert r["priority"] == 50               # below signal(100) and position(400)
        assert r["expires_at"] == "2026-07-16T13:00:00+00:00"


def test_register_candidate_cohorts_merges_with_existing_contexts():
    conn = _conn()
    _register(conn, "solana", "SOLMINT", context="position", priority=400)
    capture.register_candidate_cohorts(conn, [_ev_dict("solana", "SOLMINT")], NOW, ttl_s=3600.0)
    cohort = _cohort(conn, "solana", "SOLMINT")
    assert cohort["contexts"] == "candidate,position"
    assert cohort["priority"] == 400             # candidate never downgrades a position cohort


def test_register_candidate_cohorts_disabled_via_setting(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_CAPTURE_CANDIDATES_ENABLED", False)
    conn = _conn()
    n = capture.register_candidate_cohorts(conn, [_ev_dict("solana", "SOLMINT")], NOW)
    assert n == 0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_capture_cohorts_v2").fetchone()[0] == 0


def test_run_once_registers_candidate_cohorts_for_all_feed_buys():
    """Integration: the firehose choke point registers coverage for EVERY concrete-chain
    buy — including sub-discovery-floor buys from non-curated wallets."""
    from sniper.services import memecoin_runner as run
    conn = _conn()
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "stranger", "tokenAddress": "M2",
         "createdAt": "2026-07-16T10:00:00Z",
         "body": {"userHandle": "whale", "ticker": "B", "price": 0.02,
                  "currentSizeUsd": 50, "isFirstBuy": True}},     # sub-floor: still captured
    ]}}

    def http(method, url, headers=None, body=None):
        return 200, feed, "Thu, 16 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    run.run_once(conn, _Auth(), 100.0, http=http, fill_fn=lambda *a, **k: (0.02, "j"),
                 min_discovery_usd=1000)
    cohort = conn.execute(
        "SELECT * FROM memecoin_capture_cohorts_v2 WHERE mint='M2'").fetchone()
    assert cohort is not None
    assert "candidate" in cohort["contexts"]


def test_run_once_registers_candidates_after_funded_hook(monkeypatch):
    """Coverage writes are local research work and must not precede funded submission."""
    from sniper.services import memecoin_capture_v2 as capture_module
    from sniper.services import memecoin_holder_evidence as holder_module
    from sniper.services import memecoin_runner as run
    conn = _conn()
    m.add_wallet(conn, "u1", "alice")
    feed = {"responseObject": {"feed": [
        {"id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "M2",
         "createdAt": "2026-07-16T10:00:00Z",
         "body": {"userHandle": "alice", "ticker": "B", "price": 0.02,
                  "currentSizeUsd": 2_000, "isFirstBuy": True}},
    ]}}
    order = []

    def http(method, url, headers=None, body=None):
        return 200, feed, "Thu, 16 Jul 2026 10:00:01 GMT"

    class _Auth:
        access = "t"; refresh = "r"; ca_id = "c"
        def refresh_token(self, http=None): return "t"

    monkeypatch.setattr(
        capture_module, "register_candidate_cohorts",
        lambda *_args, **_kwargs: order.append("candidate") or 1,
    )
    monkeypatch.setattr(
        holder_module, "reconcile_feed_observations",
        lambda *_args, **_kwargs: order.append("evidence"),
    )
    run.run_once(
        conn, _Auth(), 100.0, http=http,
        fill_fn=lambda *a, **k: (0.02, "j"),
        real_hook=lambda *_args: order.append("funded") or {"status": "filled"},
    )

    assert order == ["funded", "evidence", "candidate"]
