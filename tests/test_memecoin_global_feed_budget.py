"""The global feed is a discovery telescope, not a trading signal.

Measured over the 14 days to 2026-09-06: `global_feed` produced 16 real fills for -$79.9 and 6,383
paper positions for -$8,772.6, while `follow_activity` produced 394 real fills for +$942.8. It also
spent 14x the fomo request budget of the armed lane (7,609 events per 10 min against 558), and
because both share one rate guard, every armed-wallet poll queued behind it — which is what made
the armed REST lane's lag 21 s mean and 191 s worst.

It stays running: `memecoin_live_wallet_ranking` restricts to `signal_source='global_feed'`
precisely because follow_activity rows exist only for wallets we already follow, so it is the only
way we see a wallet before we follow it, and 24 armed wallets were found through it.
"""
import ast
import pathlib


REPO = pathlib.Path(__file__).resolve().parents[1]


def test_throttle_backlog_returns_a_skipped_poll(monkeypatch):
    from sniper.feeds.fomo_feed import FomoThrottleBacklog
    from sniper.services import memecoin_runner as runner

    def refused(*a, **kw):
        raise FomoThrottleBacklog("local wait cap")

    monkeypatch.setattr(runner, "fomo_get", refused)
    result = runner.run_once(None, None, 10)
    assert result["skipped_feed_reason"] == "throttle_backlog"
    assert result["events"] == result["opened"] == result["exits"] == 0


def test_other_feed_failures_still_reach_the_runner(monkeypatch):
    import pytest
    from sniper.services import memecoin_runner as runner

    def failed(*args, **kwargs):
        raise RuntimeError("feed failure")

    monkeypatch.setattr(runner, "fomo_get", failed)
    with pytest.raises(RuntimeError, match="feed failure"):
        runner.run_once(None, None, 10)


def test_the_feed_cadence_gate_does_not_touch_the_loop_period():
    """Slowing the LOOP would slow the ~15 s stop-loss bracket sweep that shares it.

    That coupling is what let a feed backoff hold a stop-loss on 2026-09-02: the loop went 143 s
    without a cycle and a confirmed stop at -41.5% did not sell until -81.3%. The gate must sit on
    the feed FETCH only.
    """
    src = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    assert "_GLOBAL_FEED_PERIOD_S" in src
    # the loop's own sleep still uses the unmodified poll interval
    assert "POLL_MS / 1000.0" in src
    # and the bracket sweep keeps its own cadence
    assert "last_bracket > 15" in src


def test_the_gate_defaults_preserve_previous_behaviour():
    """Both knobs default to "as before": fetch every cycle, and allow global-feed entries.

    Read the DEFAULT out of the `os.getenv` call rather than matching source text — the first cut
    of this test matched a literal that the formatter had split across two lines, so it failed on
    formatting rather than on behaviour.
    """
    src = (REPO / "sniper" / "settings" / "memecoin.py").read_text()
    tree = ast.parse(src)
    defaults = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv" and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[1], ast.Constant)):
            defaults[node.args[0].value] = node.args[1].value
    assert defaults.get("MEMECOIN_GLOBAL_FEED_PERIOD_S") == "0", "feed cadence must default to off"
    assert defaults.get("MEMECOIN_REAL_GLOBAL_FEED_ENTRIES") == "1", (
        "global-feed entries must default to allowed, so the code change alone is inert")


def test_a_manual_entry_is_never_gated_by_the_global_feed_switch():
    """The operator asking for a coin is not the firehose."""
    src = (REPO / "sniper" / "services" / "memecoin_real_runner.py").read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        seg = ast.get_source_segment(src, node) or ""
        if "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES" in seg:
            found = True
            assert "not _manual_entry" in seg, "manual entries must bypass this gate"
            assert "global_feed" in seg
    assert found, "the global-feed entry gate is missing"


def test_the_refusal_names_itself_rather_than_reusing_another_reason():
    src = (REPO / "sniper" / "services" / "memecoin_real_runner.py").read_text()
    assert '"reason": "global_feed entries disabled"' in src


def test_the_loop_never_hard_subscripts_the_cycle_result():
    """A skipped-feed cycle returns a PARTIAL result, so every read of it must tolerate a miss.

    The first cut of the cadence gate returned `{"events", "opened", "skipped_feed_not_due"}` while
    the caller did `exits += res["exits"]`. That raised on every skipped cycle: the heartbeat was
    not written, the EVM pool watch did not run, and `finish_db_cycle(failed=True)` booked a healthy
    cycle as a failure. Reading with `.get` is what makes the partial shape safe, so pin it.
    """
    src = (REPO / "scripts" / "memecoin_runner_loop.py").read_text()
    tree = ast.parse(src)
    bad = [ast.unparse(n) for n in ast.walk(tree)
           if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
           and n.value.id == "res"]
    assert not bad, "hard subscript of the cycle result: " + ", ".join(bad)
