"""Coin Radar Stage 2: external discovery — parse, boost keys, upsert, discover orchestration, union."""
import sqlite3
from pathlib import Path

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_discovery_external as D
from sniper.services.memecoin_universe import universe_rows
from scripts import memecoin_discovery_worker as W


def _c():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_parse_gt_pools_extracts_mint_ticker_stats():
    payload = {"data": [
        {"attributes": {"name": "WOLF / WETH", "base_token_price_usd": "0.0004",
                        "market_cap_usd": "1250000", "reserve_in_usd": "88000",
                        "volume_usd": {"h24": "42000"}},
         "relationships": {"base_token": {"data": {"id": "robinhood_0xWOLF"}}}},
        {"attributes": {"name": "NoBase / WETH"}, "relationships": {}},   # no base token -> dropped
    ]}
    rows = D.parse_gt_pools(payload, "4663")
    assert len(rows) == 1
    r = rows[0]
    assert r["network_id"] == "4663" and r["mint"] == "0xwolf" and r["ticker"] == "WOLF"
    assert r["market_cap"] == 1_250_000.0 and r["liquidity"] == 88_000.0 and r["volume_24h"] == 42_000.0


def test_boost_keys_map_chains_and_drop_unmapped():
    payload = [
        {"chainId": "solana", "tokenAddress": "SoLmint"},
        {"chainId": "base", "tokenAddress": "0xBaSe"},
        {"chainId": "aptos", "tokenAddress": "0xnope"},   # unmapped -> dropped
        {"chainId": "solana"},                            # no address -> dropped
    ]
    http = lambda method, url, **kw: (200, payload, None)
    keys = D.dexscreener_boost_keys(http=http)
    nets = {k[0] for k in keys}
    assert ("solana", "SoLmint") in keys and ("8453", "0xbase") in keys
    assert "aptos" not in nets and len(keys) == 2       # deduped across the two boost URLs too


def test_upsert_and_rows_roundtrip():
    c = _c()
    n = m.upsert_external_discovery(c, [
        {"network_id": "solana", "mint": "M1", "ticker": "AAA", "source": "dex-boost",
         "market_cap": 5.0, "liquidity": 6.0, "volume_24h": 7.0},
    ], "2026-07-19T00:00:00+00:00")
    assert n == 1
    rows = m.external_discovery_rows(c, window_hours=None)
    assert rows[0]["mint"] == "M1" and rows[0]["armed_buyers"] == 0 and rows[0]["source"] == "dex-boost"
    # upsert again refreshes stats, keeps one row
    m.upsert_external_discovery(c, [{"network_id": "solana", "mint": "M1", "volume_24h": 99.0}],
                                "2026-07-19T00:05:00+00:00")
    rows = m.external_discovery_rows(c, window_hours=None)
    assert len(rows) == 1 and rows[0]["volume_24h"] == 99.0
    assert rows[0]["market_cap"] == 5.0 and rows[0]["liquidity"] == 6.0
    assert rows[0]["source"] == "dex-boost" and rows[0]["ticker"] == "AAA"


def test_external_rows_include_age_and_recency():
    c = _c()
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "M1"}],
        "2026-07-19T00:00:00+00:00",
    )
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "M1"}],
        "2026-07-19T00:05:00+00:00",
    )

    row = m.external_discovery_rows(
        c, window_hours=None, now_iso="2026-07-19T01:00:00+00:00",
    )[0]

    assert row["age_s"] == 3600.0
    assert row["recent_s"] == 3300.0


def test_prune_external_discovery_removes_only_stale_rows():
    c = _c()
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "OLD"}],
        "2026-07-10T00:00:00+00:00",
    )
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "FRESH"}],
        "2026-07-19T00:00:00+00:00",
    )

    assert m.prune_external_discovery(c, before_iso="2026-07-18T00:00:00+00:00") == 1
    assert {r["mint"] for r in m.external_discovery_rows(c, window_hours=None)} == {"FRESH"}


def test_discovery_worker_prunes_rows_outside_retention(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "OLD"}],
        "2026-07-10T00:00:00+00:00",
    )
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "FRESH"}],
        "2026-07-19T00:00:00+00:00",
    )
    c.close()
    monkeypatch.setattr(W.D, "discover", lambda conn, **kwargs: 0)
    monkeypatch.setattr(W.D, "refresh_universe_quotes", lambda conn, **kwargs: 0)

    W.run_once(
        str(path), retention_hours=24,
        now_iso="2026-07-19T12:00:00+00:00",
        host_now_iso="2026-07-19T12:00:00+00:00",
    )

    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    assert {r["mint"] for r in m.external_discovery_rows(c, window_hours=None)} == {"FRESH"}


def test_discover_orchestration_upserts_both_sources():
    c = _c()
    boost_keys = lambda: [("solana", "SBOOST")]
    dex_stats = lambda keys: {("solana", "SBOOST"): {"symbol": "BST", "price": 1.0,
                                                     "market_cap": 10.0, "liquidity": 2.0, "volume_24h": 3.0}}
    gt_rows = lambda net: [{"network_id": "4663", "mint": "0xGT", "ticker": "GTC", "source": "gt-pools",
                            "market_cap": 20.0, "liquidity": 4.0, "volume_24h": 5.0}]
    n = D.discover(c, boost_keys_fn=boost_keys, dex_stats_fn=dex_stats, gt_rows_fn=gt_rows,
                   quote_for_fn=lambda stats, mint, net: stats.get((net, mint)),
                   now_iso="2026-07-19T00:00:00+00:00")
    assert n == 2
    mints = {r["mint"] for r in m.external_discovery_rows(c, window_hours=None)}
    assert mints == {"SBOOST", "0xGT"}


def test_discover_defaults_to_corrected_real_utc(monkeypatch):
    c = _c()
    monkeypatch.setattr(D, "real_utc_now_iso", lambda: "2026-07-19T02:00:00+00:00")

    D.discover(
        c,
        boost_keys_fn=lambda: [],
        gt_rows_fn=lambda net: [{"network_id": net, "mint": "M1"}],
    )

    row = m.external_discovery_rows(c, window_hours=None)[0]
    assert row["first_seen"] == "2026-07-19T02:00:00+00:00"


def test_discover_include_gt_gate():
    c = _c()
    called = []
    gt = lambda net: (called.append(net), [{"network_id": "4663", "mint": "0xG", "ticker": "G",
                                            "source": "gt-pools"}])[1]
    # fast pass: GT leg skipped entirely (DexScreener-only, protects the runner's GT budget)
    D.discover(c, boost_keys_fn=lambda: [], gt_rows_fn=gt, include_gt=False,
               now_iso="2026-07-19T00:00:00+00:00")
    assert called == []
    # full pass: GT leg runs
    D.discover(c, boost_keys_fn=lambda: [], gt_rows_fn=gt, include_gt=True,
               now_iso="2026-07-19T00:05:00+00:00")
    assert called == ["4663"]


def test_refresh_universe_quotes_populates_cache():
    c = _c()
    m.add_wallet(c, "w1", "W1")
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": "e1", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": "w1", "source_handle": "w1", "network_id": "solana", "mint": "COINQ",
        "ticker": "Q", "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": "2026-07-19T00:00:00+00:00", "source_ts": "2026-07-19T00:00:00+00:00",
        "observed_at": "2026-07-19T00:00:00+00:00", "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=False)
    dex = lambda keys: {("solana", "COINQ"): {"market_cap": 100.0, "liquidity": 50.0, "volume_24h": 25.0}}
    n = D.refresh_universe_quotes(c, dex_batch_fn=dex, gt_batch_fn=lambda ks: {},
                                  quote_for_fn=lambda live, mint, net: live.get((net, mint)),
                                  now_iso="2026-07-19T01:00:00+00:00")
    assert n == 1
    qmap = m.radar_quote_map(c, now_iso="2026-07-19T01:05:00+00:00")
    assert qmap[("solana", "COINQ")]["volume_24h"] == 25.0 and qmap[("solana", "COINQ")]["market_cap"] == 100.0


def test_radar_quote_cache_excludes_stale_rows():
    c = _c()
    m.upsert_radar_quotes(
        c, [{"network_id": "solana", "mint": "OLD", "price": 1.0}],
        "2026-07-19T00:00:00+00:00",
    )

    assert ("solana", "OLD") in m.radar_quote_map(
        c, now_iso="2026-07-19T00:09:00+00:00", max_age_s=600,
    )
    assert ("solana", "OLD") not in m.radar_quote_map(
        c, now_iso="2026-07-19T00:11:00+00:00", max_age_s=600,
    )


def test_radar_watchdog_defaults_to_five_minute_discovery_cadence():
    script = (Path(__file__).parents[1] / "scripts/memecoin_radar_watchdog.sh").read_text()
    assert 'LOOP_S="${MEMECOIN_RADAR_DISCOVERY_LOOP_S:-300}"' in script
    assert '--loop "$LOOP_S"' in script


def test_radar_watchdog_separates_release_code_from_shared_state():
    script = (Path(__file__).parents[1] / "scripts/memecoin_radar_watchdog.sh").read_text()
    assert 'CODE_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd -P)"' in script
    assert 'STATE_ROOT="${SNIPER_STATE_ROOT:-/home/bigpapi/Desktop/Sniper}"' in script
    assert 'export SNIPER_STATE_ROOT="$STATE_ROOT"' in script
    assert 'export PYTHONPATH="$CODE_ROOT${PYTHONPATH:+:$PYTHONPATH}"' in script
    assert 'WORKER="$CODE_ROOT/scripts/memecoin_discovery_worker.py"' in script
    assert '"$PY" "$WORKER" --loop "$LOOP_S"' in script


def test_radar_watchdog_matches_only_the_active_release_worker():
    script = (Path(__file__).parents[1] / "scripts/memecoin_radar_watchdog.sh").read_text()
    assert 'MATCH="scripts/memecoin_discovery_worker.py"' in script
    # the release root must never return to the discovery pattern
    assert 'MATCH="$WORKER"' not in script
    assert 'grep -Fxq "$MATCH"' not in script
    # a substring test is not a suffix test
    assert 'grep -Fq "$MATCH"' not in script
    assert 'pgrep -f "memecoin_discovery_worker.py --loop"' not in script


def _watchdog_pid_matches(script_name, worker, cmdline_args, py="/usr/bin/python3"):
    """Run a watchdog's shipped `pid_matches` against one synthetic /proc cmdline.

    The function is executed, not read: a text assertion cannot show which cmdlines a
    matcher accepts.
    """
    import subprocess
    import tempfile
    from pathlib import Path as _Path

    script = _Path(__file__).parents[1] / "scripts" / script_name
    with tempfile.TemporaryDirectory() as tmp:
        proc = _Path(tmp) / "4242"
        proc.mkdir(parents=True)
        (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in cmdline_args) + b"\0")
        harness = (
            "set -u\n"
            f'PY="{py}"\n'
            f'MATCH="scripts/{worker}"\n'
            + r"""eval "$(sed -n '/^pid_matches() {/,/^}/p' """ + str(script)
            + r""" | sed 's#/proc/\$1/#""" + str(tmp) + r"""/$1/#')"
"""
            "pid_matches 4242 && echo YES || echo NO\n"
        )
        out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=30)
        assert out.stdout.strip() in {"YES", "NO"}, (out.stdout, out.stderr)
        return out.stdout.strip() == "YES"


def _radar_pid_matches(cmdline_args):
    return _watchdog_pid_matches(
        "memecoin_radar_watchdog.sh", "memecoin_discovery_worker.py", cmdline_args
    )


def test_radar_watchdog_requires_an_exact_path_suffix_not_a_substring():
    """A substring test adopts an unrelated process and then skips recovery.

    `/tmp/scripts/memecoin_discovery_worker.py.backup` contains the worker path, so a
    substring match reported the daemon as alive and the real worker never started.
    """
    real = "/home/bigpapi/Desktop/Sniper/.runtime/opportunity/releases/abc123/scripts/memecoin_discovery_worker.py"
    assert _radar_pid_matches(["/usr/bin/python3", real, "--loop", "60"])
    # a worker from ANY release still counts
    assert _radar_pid_matches(["/usr/bin/python3", real.replace("abc123", "def456"), "--loop", "60"])
    # but a lookalike path must not
    assert not _radar_pid_matches(
        ["/usr/bin/python3", "/tmp/scripts/memecoin_discovery_worker.py.backup", "--loop", "60"]
    )
    assert not _radar_pid_matches(
        ["/usr/bin/python3", "/x/scripts/memecoin_opportunity_worker.py", "--loop", "60"]
    )
    # POSITION matters, not mere presence: a viewer or a shell that merely NAMES the
    # worker file must not be adopted as the daemon.
    assert not _radar_pid_matches(
        ["/usr/bin/less", "/rel/abc/scripts/memecoin_discovery_worker.py"]
    )
    assert not _radar_pid_matches(
        ["/bin/bash", "-c", "cat /rel/abc/scripts/memecoin_discovery_worker.py"]
    )
    # and the interpreter must be argument 0, not merely present somewhere
    assert not _radar_pid_matches(
        ["/bin/sh", "/usr/bin/python3", "/rel/abc/scripts/memecoin_discovery_worker.py"]
    )
    # `-c` code that merely ENDS with the worker path is not the worker. Only the two
    # explicit launch forms are accepted, so every other interpreter flag is rejected.
    assert not _radar_pid_matches(
        ["/usr/bin/python3", "-c",
         "print(1) # /rel/abc/scripts/memecoin_discovery_worker.py"]
    )
    assert not _radar_pid_matches(
        ["/usr/bin/python3", "-m", "/rel/abc/scripts/memecoin_discovery_worker.py"]
    )
    # the `-u` launch form stays supported
    assert _radar_pid_matches(
        ["/usr/bin/python3", "-u", "/rel/abc/scripts/memecoin_discovery_worker.py"]
    )


def test_opportunity_watchdog_matcher_accepts_only_the_two_launch_forms():
    """The opportunity watchdog guards the lane that starts when capture returns to 1.

    It carried the same substring and position defects, so it needs the same behavioural
    coverage rather than inheriting confidence from the Radar test.
    """
    worker = "memecoin_opportunity_worker.py"
    real = f"/rel/abc/scripts/{worker}"

    def matches(args):
        return _watchdog_pid_matches("memecoin_opportunity_watchdog.sh", worker, args)

    # its launch is `"$PY" -u "$WORKER"`, so that form must match
    assert matches(["/usr/bin/python3", "-u", real])
    assert matches(["/usr/bin/python3", real])
    assert matches(["/usr/bin/python3", "-u", real.replace("abc", "def")])
    # and the same false positives must be rejected
    assert not matches(["/usr/bin/less", real])
    assert not matches(["/bin/bash", "-c", f"cat {real}"])
    assert not matches(["/usr/bin/python3", f"/tmp/scripts/{worker}.backup"])
    assert not matches(["/usr/bin/python3", "-c", f"print(1) # {real}"])
    assert not matches(["/bin/sh", "/usr/bin/python3", real])


def test_radar_watchdog_uses_a_single_flight_lock():
    script = (Path(__file__).parents[1] / "scripts/memecoin_radar_watchdog.sh").read_text()
    assert 'exec 9>"$STATE_ROOT/data/memecoin_radar.watchdog.lock"' in script
    assert "flock -n 9 || exit 0" in script
    assert '< /dev/null 9>&- &' in script


def test_radar_watchdog_recovers_a_missing_pid_file_without_a_duplicate():
    script = (Path(__file__).parents[1] / "scripts/memecoin_radar_watchdog.sh").read_text()
    assert "for proc_dir in /proc/[0-9]*; do" in script
    assert 'echo "$candidate" > "$PIDFILE"' in script


def test_repair_radar_future_timestamps_removes_known_host_clock_skew():
    c = _c()
    m.upsert_external_discovery(
        c, [{"network_id": "solana", "mint": "FUTURE"}],
        "2026-07-19T07:00:00+00:00",
    )
    m.upsert_radar_quotes(
        c, [{"network_id": "solana", "mint": "FUTURE", "price": 1.0}],
        "2026-07-19T07:00:00+00:00",
    )

    repaired = m.repair_radar_future_timestamps(
        c, now_iso="2026-07-19T00:00:00+00:00", host_skew_s=7 * 3600,
    )

    external = m.external_discovery_rows(c, window_hours=None)[0]
    quote = c.execute(
        "SELECT updated_at FROM memecoin_radar_quote WHERE chain_id='solana' AND mint='FUTURE'"
    ).fetchone()
    assert repaired == {"external": 1, "quotes": 1}
    assert external["first_seen"] == "2026-07-19T00:00:00+00:00"
    assert external["last_seen"] == "2026-07-19T00:00:00+00:00"
    assert quote["updated_at"] == "2026-07-19T00:00:00+00:00"


def test_universe_unions_external_but_dedups_feed_coins():
    c = _c()
    # a feed coin bought by a wallet
    m.add_wallet(c, "w1", "W1")
    m.insert_feed_observation_v2(c, {
        "upstream_event_id": "e1", "raw_type": "large_buy", "normalized_side": "buy",
        "source_user_id": "w1", "source_handle": "w1", "network_id": "solana", "mint": "FEEDCOIN",
        "ticker": "FEED", "their_price": 1.0, "their_market_cap": 1.0, "their_size_usd": 1.0,
        "source_ts_raw": "2026-07-19T00:00:00+00:00", "source_ts": "2026-07-19T00:00:00+00:00",
        "observed_at": "2026-07-19T00:00:00+00:00", "lag_s": 0.0, "raw_payload": {}},
        was_curated=True, was_real_approved=False)
    # external: one NEW coin + a duplicate of the feed coin (must be deduped)
    extra = m.external_discovery_rows(c, window_hours=None) + [
        {"network_id": "solana", "mint": "NEWCOIN", "ticker": "NEW", "buyers": 0, "armed_buyers": 0,
         "source": "dex-boost", "market_cap": None, "liquidity": None, "volume_24h": None},
        {"network_id": "solana", "mint": "FEEDCOIN", "ticker": "FEED", "buyers": 0, "armed_buyers": 0,
         "source": "dex-boost"},
    ]
    rows = universe_rows(c, window_hours=None, extra_sources=extra,
                         now_iso="2026-07-19T01:00:00+00:00", sort="buyers")
    mints = [r["mint"] for r in rows]
    assert "NEWCOIN" in mints and mints.count("FEEDCOIN") == 1   # external dup dropped
    feed = next(r for r in rows if r["mint"] == "FEEDCOIN")
    assert feed["buyers"] == 1 and feed["source"] == "wallet"   # feed row wins over external dup
