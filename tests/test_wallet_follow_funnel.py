"""The follow funnel (operator order 2026-10-01): rotation, candidate screens, commands, the add helper,
the read-only map route and its panel helpers."""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))

import wallet_follow_funnel as F  # noqa: E402


def test_selftest_rules_hold():
    assert F.selftest() == 0


def test_the_add_helper_refuses_a_typo_and_plans_each_state():
    from scripts.memecoin_follow_add import plan, valid_uid
    assert valid_uid("87f77c24-44e0-5696-baaf-dfe732083954")
    assert valid_uid("pump:SQHK48QT8SY1vYN44iXji7wQ6CJek8AjfX6mBp47TZq")
    assert not valid_uid("pump:not-an-address") and not valid_uid("kyle") and not valid_uid("")
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT PRIMARY KEY, paused INT, excluded INT)")
    c.executemany("INSERT INTO memecoin_wallets VALUES (?,?,?)", [("live", 0, 0), ("gone", 1, 1), ("held", 1, 0)])
    assert plan(c, "new") == ("add", "not on the roster")
    assert plan(c, "gone")[0] == "reobserve" and plan(c, "held")[0] == "add"
    assert plan(c, "live") == (None, "already curated: paper copies it")       # J7 is not asked (J7 out)


def _client(tmp_path, monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": sqlite3.connect(":memory:", check_same_thread=False)}))
    return app.test_client()


def test_the_opt_in_archive_serves_the_last_file_with_age_and_typed_missingness(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch)
    url = "/api/memecoin/wallet-map/funnel?diagnostic_only=1"
    absent = cl.get(url).get_json()
    assert absent["stamp"]["error"] == "not_generated"
    assert absent["publication"] == "diagnostic_only"
    assert absent["research_eligibility"]["eligible"] is False
    (tmp_path / "data" / "wallet_follow_funnel.json").write_text(json.dumps(
        {"stamp": {"generated_at": "2026-10-01T23:30:00"}, "funnel": [{"stage": "paper"}], "rotation": []}))
    body = cl.get(url).get_json()
    assert body["funnel"] == [{"stage": "paper"}] and body["stamp"]["age_s"] >= 0
    assert body["research_eligibility"]["eligible"] is False
    assert "authority_state" not in body["stamp"] and "clips" not in body
    (tmp_path / "data" / "wallet_follow_funnel.json").write_text("{not json")
    assert cl.get(url).get_json()["stamp"]["error"] == "unreadable"


def test_the_route_is_get_only(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch)
    assert cl.post("/api/memecoin/wallet-map/funnel").status_code in (403, 405)


def _core(expr):
    js = "const C = require('./static/wallet_map.js'); process.stdout.write(JSON.stringify(%s));" % expr
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def test_the_panel_never_shows_a_bare_percent():
    assert _core("C.pctText(0.0634, 119)") == "+6.3% on 119"
    assert _core("C.pctText(-0.052, 120)") == "-5.2% on 120"
    assert _core("C.pctText(null, 0)") == "none yet" and _core("C.pctText(0.5, 0)") == "none yet"


def test_the_stage_counts_and_the_slot_line():
    assert _core("C.stageCounts([{stage:'paper'},{stage:'armed'},{stage:'paper'}])") == {
        "curated": 0, "tracked": 0, "paper": 2, "trial": 0, "armed": 1}
    status = _core("C.funnelStampText({authority_state:'ok',live_at:'2026-10-05T21:00:00Z',error:'not_generated'})")
    assert "Live roster authority" in status and "2026-10-05T21:00:00Z" in status
    assert "independent of research results" in status and "disabled" not in status
    unavailable = _core("C.funnelStampText({authority_state:'unavailable'})")
    assert "Live authority unavailable" in unavailable and "money controls disabled" in unavailable


def test_the_add_helper_keeps_a_stored_name_and_a_new_row_needs_one():
    from scripts.memecoin_follow_add import stored_handle
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE memecoin_wallets (source_user_id TEXT PRIMARY KEY, handle TEXT, paused INT, excluded INT)")
    c.execute("INSERT INTO memecoin_wallets VALUES ('held', 'yaevwastaken', 1, 0)")
    assert stored_handle(c, "held") == "yaevwastaken" and stored_handle(c, "new") is None


def test_the_panel_shows_censored_shares_and_the_14_day_gated_counts():
    assert _core("C.censText(0.287)") == " · 29% cens" and _core("C.censText(null)") == ""
    assert _core("C.feasibleText({solana: 41, '4663': 3})") == "4663 3, solana 41"


def test_no_j7_slot_command_or_budget_reaches_the_map():
    # Operator 2026-10-02 (via Alpha): J7 is out of the wallet path; "Add it" is the roster row and
    # the trial start, and the caption names no J7 slot.
    producer = Path("scripts/research/wallet_follow_funnel.py").read_text()
    js = Path("static/wallet_map.js").read_text()
    for text in (producer, js):
        assert "j7_watchlist.py" not in text and "j7_budget" not in text
    assert "slotText" not in js and "memecoin_trial_arm.py start" in producer


def test_an_evm_candidate_without_a_proven_address_gets_no_commands():
    # Gamma LOW on 7f0ee065b: step 1 wrote a roster row, step 2 (the trial start) refused it
    # (OtherAboveFowl 56), because our on-chain detection has no address to watch.
    cand = {"uid": "87f77c24-44e0-5696-baaf-dfe732083954", "handle": "OtherAboveFowl"}
    assert F.evm_commands(cand, chain="56", has_address=False) == ([], "no proven 56 address")
    cmds, why = F.evm_commands(cand, chain="56", has_address=True)
    assert why is None and cmds[1] == "python3 scripts/memecoin_trial_arm.py start OtherAboveFowl 56 --confirm"
    row = dict(cand, chain="56", no_commands="no proven 56 address")
    buttons = _core("C.actionsFor('cand', %s, {'56':{trial:50,full_max:175}})" % json.dumps(row))
    assert buttons and all(b["disabled"] == "no proven 56 address" for b in buttons)
    row.pop("no_commands")
    buttons = _core("C.actionsFor('cand', %s, {'56':{trial:50,full_max:175}})" % json.dumps(row))
    assert buttons and all(b["disabled"] is None for b in buttons)
    producer = Path("scripts/research/wallet_follow_funnel.py").read_text()
    assert "from sniper.services.memecoin_trial_ops import evm_addresses" in producer   # the gate's own rule


# Explicit legacy arithmetic diagnostics. Default publication rejection is tested
# separately by test_wallet_execution_publication and its inventory check.
import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _legacy_arithmetic_only():
    with _diagnostic_mode():
        yield
