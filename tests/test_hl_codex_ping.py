import os

from sniper.algos.hl_microstructure import persistence as P
import scripts.hl_market_radar_codex_ping as ping


def test_codex_ping_writes_readonly_review_prompt(tmp_path):
    db = tmp_path / "hlm.sqlite"
    out = tmp_path / "ping.md"
    codex_out = tmp_path / "codex.md"
    conn = P.connect(str(db))
    P.ensure_schema(conn)
    conn.close()

    rc = ping.main([
        "--db-path", str(db),
        "--output", str(out),
        "--codex-output", str(codex_out),
        "--skip-codex",
    ])

    assert rc == 0
    text = out.read_text()
    assert "ROLE: Codex scheduled reviewer" in text
    assert "Hyperliquid Market Radar" in text
    assert "read-only" in text.lower()
