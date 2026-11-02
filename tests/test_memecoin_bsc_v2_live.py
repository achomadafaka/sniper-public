"""BSC V2 observation must not turn a dark feed into volume or a fresh price."""
import pytest
import json

from sniper.services import memecoin_bsc_v2_live as live


MINT = "0x" + "aa" * 20
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
POOL = "0x" + "cc" * 20
SOURCE = "0x" + "11" * 32
OTHER = "0x" + "22" * 32
HASH = "0x" + "33" * 32


def test_sync_price_requires_verified_pair_and_fresh_quote(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_pool(c, POOL, MINT, WBNB, 9)
    live.record_quote(c, 600.0, 1000.0)
    live.record_head(c, 1, HASH, 1000.0, 1000.1)
    live.record_sync(c, POOL, 1, HASH, 10**12, 10**18, 1000.2)
    row = live.latest_mark(c, MINT, POOL, 1000.5)
    assert row["price"] == pytest.approx(0.6)
    assert row["observed_at"] == pytest.approx(1000.2)
    live.record_head(c, 2, HASH, 1004, 1004.1)
    assert live.latest_mark(c, MINT, POOL, 1005.0) is not None  # quiet reserves are unchanged
    assert live.latest_mark(c, "0x" + "dd" * 20, POOL, 1000.5) is None
    live.record_sync(c, POOL, 1, HASH, 10**12, 10**18, 1004.0, idx=1)
    assert live.latest_mark(c, MINT, POOL, 1004.1) is None  # late frame is not a fresh chain price
    live.record_head(c, 1, "0x" + "44" * 32, 1000.0, 1000.6)
    assert live.latest_mark(c, MINT, POOL, 1000.7) is None


def test_header_reordering_does_not_invent_continuous_coverage(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_head(c, 10, HASH, 700, 700.1)
    live.record_head(c, 12, HASH, 701, 701.1)
    assert c.execute("SELECT height FROM feed").fetchone()[0] == 10
    live.record_head(c, 11, HASH, 700.5, 701.2)
    assert c.execute("SELECT height FROM feed").fetchone()[0] == 12


def test_parent_hash_mismatch_resets_volume_window(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_head(c, 10, HASH, 700, 700.1)
    live.record_head(c, 11, "0x" + "44" * 32, 701, 701.1,
                     parent_hash="0x" + "55" * 32)
    row = c.execute("SELECT since,height FROM feed").fetchone()
    assert tuple(row) == (701, 11)


def test_v3_swap_price_uses_sqrt_ratio_and_token_decimals(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_v3_pool(c, POOL, MINT, WBNB, 9)
    live.record_quote(c, 600, 1000)
    live.record_head(c, 1, HASH, 1000, 1000.1)
    # One token (9 decimals) costs 0.001 WBNB (18 decimals): raw token1/token0 = 1e6.
    sqrt = int((10**6)**0.5 * 2**96)
    live.record_v3_swap(c, POOL, 1, HASH, sqrt, 1000.2)
    mark = live.latest_v3_mark(c, MINT, POOL, 1000.5)
    assert mark["price"] == pytest.approx(0.6)
    assert mark["source"] == "bsc_v3_swap"


def test_pool_binding_falls_back_to_funded_classification_when_receipt_has_no_pool():
    p = {"executed_pool": None,
         "evm_route_json": json.dumps({"protocol": "uniswap_v2", "path": [WBNB, MINT]}),
         "funded_contract_json": json.dumps({"classification": {"facts": {
             "liquidity_pair_address": POOL}}})}
    assert live._position_pool(p) == POOL


def test_m5_excludes_source_and_refuses_incomplete_coverage(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_quote(c, 600.0, 1000.0)
    live.record_head(c, 1, HASH, 700.0, 700.1)
    live.record_head(c, 2, HASH, 1000.0, 1000.1)
    live.record_swap(c, POOL, 2, HASH, SOURCE, 1, 0, 10**18)
    live.record_swap(c, POOL, 2, HASH, OTHER, 2, 0, 2 * 10**18)
    assert live.m5_bound(c, POOL, WBNB, MINT, SOURCE, 1000.5)["usd"] == pytest.approx(1200.0)
    assert live.m5_bound(c, POOL, WBNB, MINT, "0x" + "55" * 32, 1000.5)["usd"] is None
    live.reset_coverage(c)
    assert live.m5_bound(c, POOL, WBNB, MINT, SOURCE, 1000.5)["usd"] is None


def test_global_v3_swaps_feed_the_same_source_excluded_m5_bound(tmp_path):
    from _source_guard import code_of
    src = code_of(live.run)
    assert "(5, ['logs', {'topics': [[V3_SWAP, PANCAKE_V3_SWAP]]}])" in src   # both V3 Swap topics
    assert "kind == 5 and row['kind'] == 'v3_swap'" in src

    c = live.connect(tmp_path / "bsc.db")
    live.record_quote(c, 600, 1000)
    live.record_head(c, 1, HASH, 700, 700.1)
    live.record_head(c, 2, HASH, 1000, 1000.1)
    live.record_swap(c, POOL, 2, HASH, SOURCE, 1, 100, -10**18)
    live.record_swap(c, POOL, 2, HASH, OTHER, 2, 200, -2 * 10**18)
    assert live.m5_bound(c, POOL, WBNB, MINT, SOURCE, 1000.5)["usd"] == pytest.approx(1200)


def test_unpaired_swap_invalidates_last_pool_price(tmp_path):
    c = live.connect(tmp_path / "bsc.db")
    live.record_pool(c, POOL, MINT, WBNB, 9)
    live.record_quote(c, 600, 1000)
    live.record_head(c, 1, HASH, 1000, 1000.1)
    live.record_sync(c, POOL, 1, HASH, 10**12, 10**18, 1000.2, tx=SOURCE)
    live.record_head(c, 2, HASH, 1001, 1001.1)
    live.record_swap(c, POOL, 2, HASH, OTHER, 1, 0, 10**18)
    assert live.latest_mark(c, MINT, POOL, 1001.2) is None


def test_bsc_entry_requires_a_measured_five_minute_floor(monkeypatch):
    from sniper.services.memecoin_real_runner import volume_alive_decision, volume_gate_args
    monkeypatch.setenv("MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5_56", "0.02")
    _, _, _, args = volume_gate_args(
        {"volume_m5": None}, mcap=50_000, network_id="56", coin_band="nano",
        source_user_id="u", signal_usd=100)
    assert args["m5_floor"] == pytest.approx(0.02)
    assert args["m5_required"] is True
    assert volume_alive_decision(5000, 50_000, **args)[2] == "m5_volume_unreadable"


def _bsc_upkeep(tmp_path, rows):
    """Run the watchdog's BSC-observer block with ps, kill, setsid and sleep substituted. Returns the
    recorded calls. STATE_ROOT is tmp_path, whose .env names 56."""
    import shlex
    import subprocess
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "scripts/memecoin_watchdog.sh").read_text()
    tree = text[text.index("own_release_tree() {"):text.index("solana_watchers() {")]
    block = text[text.index("_bsc_chains=$(grep"):]
    block = block[:block.index("\nesac\n") + 6]
    (tmp_path / ".env").write_text("MEMECOIN_EVM_ALLOWLIST=4663,56\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    calls = tmp_path / "calls"
    harness = "\n".join([
        "set -u", f"STATE_ROOT={shlex.quote(str(tmp_path))}",
        f"ROOT={shlex.quote(str(tmp_path / '.runtime/memecoin/releases/new1'))}",
        "PY=/py; DB_PATH=/state/ledger.db", f"CALLS={shlex.quote(str(calls))}",
        "ps() { printf '%s\\n' " + " ".join(shlex.quote(r) for r in rows) + "; }",
        'kill() { [ "$1" = "-0" ] && return 1; echo "kill $*" >> "$CALLS"; }',
        'setsid() { echo "launch" >> "$CALLS"; }', "sleep() { :; }",
        'log() { echo "log $*" >> "$CALLS"; }', tree, block])
    out = subprocess.run(["bash", "-c", harness], capture_output=True, text=True,
                         env={"PATH": "/usr/bin:/bin"}, timeout=10)
    assert out.returncode == 0, out.stderr
    return calls.read_text().splitlines() if calls.exists() else []


def test_the_watchdog_retires_only_this_state_roots_old_bsc_observer(tmp_path):
    """2026-09-29: the retire used `pgrep -f` on ANY release path, so a whole-script test with a tmp
    STATE_ROOT whose .env named 56 would stop the live observer. Only this root's releases count."""
    worker = "sniper/services/memecoin_bsc_v2_live.py"
    rows = [f"22 /py -u {tmp_path}/.runtime/memecoin/releases/old2/{worker}",
            f"33 /py -u /home/other/.runtime/memecoin/releases/live3/{worker}",
            f"44 bash -c tail {tmp_path}/.runtime/memecoin/releases/old2/{worker}.log"]
    assert _bsc_upkeep(tmp_path, rows) == ["kill 22", "launch"]
