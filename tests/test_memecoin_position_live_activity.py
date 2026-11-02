"""Live volume on the funded position card (2026-09-13, operator)."""
import json
import pathlib
import sqlite3
import subprocess

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_exits import write_marks

NOW = "2026-09-13T07:00:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def test_a_sweep_mark_stores_token_volume_and_trade_count():
    c = _conn()
    write_marks(c, {("solana", "CAT"): {"price": 2.6e-05, "liquidity": 9000.0, "market_cap": 25501.0,
                                        "volume_m5": 6139.4, "volume_h1": 131471.1,
                                        "txns_m5_buys": 138, "txns_m5_sells": 47,
                                        "source": "dexscreener"}}, NOW)
    row = c.execute("SELECT volume_m5, volume_h1, txns_m5 FROM memecoin_position_marks").fetchone()
    assert tuple(row) == (6139.4, 131471.1, 185)


def test_a_price_only_mark_leaves_the_activity_unknown():
    c = _conn()
    write_marks(c, {("solana", "CAT"): {"price": 2.6e-05, "source": "fastlane_mid"}}, NOW)
    row = c.execute("SELECT volume_m5, volume_h1, txns_m5 FROM memecoin_position_marks").fetchone()
    assert tuple(row) == (None, None, None)


def test_the_card_reads_the_newest_activity_and_the_last_swap():
    c = _conn()
    ins = ("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, source, volume_m5, "
           "volume_h1, txns_m5) VALUES(?,?,?,?,?,?,?,?)")
    c.execute(ins, ("solana", "CAT", "2026-09-13T06:58:00+00:00", 1.0, "dexscreener", 100.0, 900.0, 5))
    c.execute(ins, ("solana", "CAT", "2026-09-13T06:59:00+00:00", 1.0, "dexscreener", 6139.0, 131471.0, 185))
    c.execute(ins, ("solana", "CAT", "2026-09-13T06:59:40+00:00", 1.0, "fastlane_sol_swap", None, None, None))
    c.execute(ins, ("solana", "CAT", "2026-09-13T06:30:00+00:00", 1.0, "dexscreener", 1.0, 1.0, 1))
    a = m.position_live_activity(c, "solana", "CAT", now_iso=NOW)
    assert (a["volume_m5"], a["txns_m5"], a["volume_h1"]) == (6139.0, 185, 131471.0)
    assert a["last_trade_at"] == "2026-09-13T06:59:40+00:00"
    old = m.position_live_activity(c, "solana", "CAT", now_iso="2026-09-13T08:00:00+00:00")
    assert old["volume_m5"] is None, "a 61-minute-old read is not live"
    assert m.position_live_activity(c, "4663", "CAT", now_iso=NOW)["last_trade_at"] is None


def test_the_card_line_renders_readable_numbers():
    app = (pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    pick = lambda name: app[app.index("    %s(" % name):app.index("\n    },\n", app.index("    %s(" % name)) + 7]
    script = ("const esc=v=>String(v??''); const mc={_now(){return Date.parse('2026-09-13T07:00:00Z');},"
              + pick("_liveActivityLine") + pick("_fmtMcapShort") + pick("_quoteAge") + "};"
              "console.log(JSON.stringify(mc._liveActivityLine({live_activity:{volume_m5:6139.4,txns_m5:185,"
              "volume_h1:131471,last_trade_at:'2026-09-13T06:59:57Z',volume_at:'2026-09-13T06:59:50Z'}})));"
              "console.log(JSON.stringify(mc._liveActivityLine({})));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout.splitlines()
    line = json.loads(out[0])
    import re
    assert "5m $6K · 185 trades · 1h $131K · last trade 3s ago" in re.sub(r"<[^>]+>", "", line)
    # The age is a data-observation-at node, so _ageRealMarks() ticks it (Codex audit #8).
    assert 'data-observation-at="2026-09-13T06:59:57Z">3s</time>' in line
    assert json.loads(out[1]) == ""
