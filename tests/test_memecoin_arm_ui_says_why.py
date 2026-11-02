"""The Funded authority board covers every funded chain and a refused arm says why (2026-09-12).

BSC (56) is funded (MEMECOIN_EVM_ALLOWLIST=8453,4663,56) but had no arming table, and a refused arm
showed a bare "fomo_follow_failed" toast that the operator read as a dead button.
"""
import pathlib
import re
import json
import shutil
import subprocess

APP = (pathlib.Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()


def test_bsc_is_a_funded_chain_on_the_board():
    line = next(l for l in APP.splitlines() if "const FUNDED_CHAINS" in l)
    assert set(re.findall(r"'(\w+)'", line)) >= {"solana", "base", "robin", "bsc"}


def test_arc_is_a_funded_chain_on_the_board():
    line = next(l for l in APP.splitlines() if "const FUNDED_CHAINS" in l)
    assert "arc" in set(re.findall(r"'(\w+)'", line))


def test_a_refused_arm_names_the_reason_instead_of_the_raw_error():
    body = APP[APP.index("async _chainArm("):APP.index("async _realToggle(")]
    assert body.count("suppressErrorToast: true") == 2, "both arm requests must own their error text"
    assert "Fomo would not follow this wallet" in body and "Nothing changed." in body
    assert "catch (e) { return; }" not in body, "a silent catch is the dead-button bug"


def test_build_failures_read_in_plain_words():
    body = APP[APP.index("    _shortReason(row) {"):APP.index("    _entryLines(")]
    assert "route build refused|simulation refused|sign failed" in body
    assert "'buy would have failed on-chain'" in body


def test_an_unverified_exit_is_not_displayed_as_a_proved_unsellable_coin():
    body = APP[APP.index("    _shortReason(row) {"):APP.index("    _entryLines(")]
    prefix = "exit route pays TTWofwAg at a Pump.fun hop; Jupiter cannot build that sell "
    rows = [{"reason": prefix + text} for text in (
        "(unsellable trap, fries 2026-09-09) [native exit check_failed:RuntimeError]",
        "(exit not verified) [native exit check_failed:RuntimeError]",
        "(exit not verified) [native exit too_big:1504>1232]",
    )]
    script = "const view = { " + body + "}; "
    script += "console.log(JSON.stringify(" + json.dumps(rows) + ".map(r => view._shortReason(r))));"
    out = subprocess.run([shutil.which("node"), "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["exit not verified"] * len(rows)


def test_source_buy_card_uses_effective_floor_when_recorded():
    node = shutil.which("node")
    assert node
    body = APP[APP.index("    _shortReason(row) {"):APP.index("    _entryLines(")]
    reason = "source buy $995 (0.587%) under mcap floor 0.800% ($1600)"
    rows = [{"reason": reason, "source_buy_effective_floor_usd": 1568},
            {"reason": reason, "gate_evidence": [{"name": "source_buy_floor",
                                               "effective_floor_usd": 1568}]},
            {"reason": reason},
            {"reason": reason, "source_buy_effective_floor_usd": "bad"}]
    script = "const view = { _fmtMcapShort() { return ''; }, " + body + "}; "
    script += "console.log(JSON.stringify(" + json.dumps(rows) + ".map(r => view._shortReason(r))));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    labels = json.loads(out.stdout)
    assert all("needs $1,568" in label for label in labels[:2])
    assert all("needs" not in label and "nominal floor $1600" in label for label in labels[2:])


def test_an_entry_without_a_ticker_borrows_the_newest_known_one():
    """PENIS (JE3HT7) came from the on-chain watcher with no ticker and showed as "JE3HT7Sb"."""
    import sqlite3
    from sniper.services.memecoin_entry_view import _known_ticker
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_events (id INTEGER PRIMARY KEY, mint TEXT, network_id TEXT, ticker TEXT)")
    c.executemany("INSERT INTO memecoin_events (mint, network_id, ticker) VALUES (?,?,?)",
                  [("JE3", "solana", "OLD"), ("JE3", "solana", "PENIS"), ("JE3", "solana", None),
                   ("JE3", "4663", "OTHERCHAIN")])
    assert _known_ticker(c, "solana", "JE3") == "PENIS"
    assert _known_ticker(c, "8453", "JE3") is None
    assert _known_ticker(c, "solana", None) is None
