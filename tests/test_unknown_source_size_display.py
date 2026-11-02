"""An unknown source size must not read as a small one.

Alert 753be289 (2026-09-26 22:43Z): the on-chain lane could not value a routed buy, and the
refusal 'source buy unknown under mcap floor 0.500% ($45)' reached the phone verbatim, which reads
as "they bought under $45", and the dashboard card said 'their buy too small'. The buy was 2 SOL.
The reason string itself stays byte-identical (it keys the source_buy_floor refusal family and
the wallet gate exemptions); only what the operator reads changes. The header falls back to the
short mint when no signal ever named the coin, instead of 'Unknown token'.
"""
import json
import shutil
import subprocess
from pathlib import Path

APP = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
UNKNOWN = "source buy unknown under mcap floor 0.500% ($45)"
UNKNOWN_NO_MCAP = "source buy unknown under unknown-mcap floor $500"
SMALL = "source buy $30 (0.172%) under mcap floor 0.500% ($86)"
MINT = "GFYQZ4BtpyewiL91j3LWwcxyvqEHG6Z9jswG3nug6fSx"


def _short_reasons(rows):
    node = shutil.which("node")
    assert node
    body = APP[APP.index("    _shortReason(row) {"):APP.index("    _entryLines(")]
    script = "const view = { _fmtMcapShort() { return ''; }, _reasonWords(r) { return r; }, " + body + "}; "
    script += "console.log(JSON.stringify(" + json.dumps(rows) + ".map(r => view._shortReason(r))));"
    return json.loads(subprocess.run([node, "-e", script], capture_output=True, text=True,
                                     check=True).stdout)


def test_the_card_says_size_unknown_and_keeps_a_real_small_buy():
    unknown, no_mcap, small = _short_reasons([{"reason": UNKNOWN}, {"reason": UNKNOWN_NO_MCAP},
                                              {"reason": SMALL}])
    for label in (unknown, no_mcap):
        assert "source size unknown" in label and "too small" not in label, label
    assert "their buy $30" in small and "unknown" not in small, small


def _render(**view):
    from sniper.services.memecoin_telegram import render_entry_notification
    base = {"status": "refused", "ticker": "X", "network_id": "solana", "source_handle": "w",
            "mint": MINT}
    out = render_entry_notification({**base, **view})
    return "\n".join(out) if isinstance(out, list) else str(out)


def test_the_phone_says_size_unknown_and_keeps_a_real_small_buy_verbatim():
    unknown = _render(reason=UNKNOWN)
    assert "Why: source size unknown (missing evidence); floor $45 not evaluated" in unknown, unknown
    assert "Why: source size unknown (missing evidence); floor $500 not evaluated" in _render(
        reason=UNKNOWN_NO_MCAP)
    assert f"Why: {SMALL}" in _render(reason=SMALL)


def test_the_header_shows_the_short_mint_when_no_ticker_is_known():
    header = _render(ticker=None, reason=UNKNOWN).splitlines()[0]
    assert "GFYQ…6fSx" in header and "Unknown token" not in header, header
    assert _render(ticker="PEPE", reason=UNKNOWN).splitlines()[0].count("PEPE") == 1
    assert "Unknown token" in _render(ticker=None, mint=None, reason=UNKNOWN).splitlines()[0]
