"""Operator 2026-10-08: "Get rid of the equity panel, I don't want it." The equity change card is gone
from the page (section, rail entry, fetch, styles) and its recorder defaults off. The backend module
and its route stay, unused, so nothing that imports them breaks."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_equity_card_is_gone_from_the_page():
    app = (ROOT / "static" / "app.js").read_text()
    for gone in ("mc-equity-change", "/api/memecoin/real/equity-change", "_equityPanel", "_maybeLoadEquity",
                 "equity-win"):
        assert gone not in app, gone
    assert ".eq-" not in (ROOT / "static" / "styles.css").read_text()


def test_the_recorder_is_off_by_default():
    src = (ROOT / "sniper" / "settings" / "memecoin.py").read_text()
    assert 'os.getenv("MEMECOIN_EQUITY_RECORDER", "0")' in src
