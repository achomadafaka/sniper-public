"""The Wallet Map's action buttons (operator 2026-10-02: "arm it directly and see it in Armed"): the
trial route, the live overlay that moves a row between tabs, and the action specs the page sends."""
import json
import sqlite3
import subprocess

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_trial_ops as ops

ADDR = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
UID = "pump:" + ADDR


def _conn():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _detect(c, uid=UID, addr=ADDR):
    from sniper.util.clock import real_utc_now_iso
    now = real_utc_now_iso()
    c.execute("INSERT INTO onchain_wallet_detections (signature, source_user_id, address, network_id, mint, "
              "detected_at, created_at) VALUES ('s1', ?, ?, 'solana', 'M', ?, ?)", (uid, addr, now, now))
    c.commit()


@pytest.fixture
def client(tmp_path, monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from sniper.routes import api_memecoin
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_REAL_TRIAL_CLIP_USD_SOLANA", "20")
    monkeypatch.setattr(ops, "GRAPH_PATH", str(tmp_path / "no-graph.db"))
    monkeypatch.setattr(api_memecoin, "_notify_operator", lambda *a, **k: None)
    (tmp_path / "data").mkdir()
    conn = _conn()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn, "csrf_enforce_all": True}))
    cl = app.test_client()
    token = cl.get("/api/memecoin/csrf-token").get_json()["csrf_token"]
    return cl, conn, token, tmp_path


def _post(cl, token, url, body):
    return cl.post(url, json=body, headers={"X-CSRF-Token": token})


def test_the_trial_route_needs_the_csrf_token_and_a_json_body(client):
    cl, conn, token, _ = client
    url = "/api/memecoin/wallets/%s/trial" % UID
    assert cl.post(url, json={"action": "start", "chain": "solana"}).status_code == 403
    r = cl.post(url, data='{"action": "start", "chain": "solana"}',      # valid JSON, as a form can send
                headers={"X-CSRF-Token": token, "Content-Type": "text/plain"})
    assert r.status_code == 400 and r.get_json()["error"] == "json_body_required"


def test_a_candidate_trial_dry_runs_then_adds_starts_and_arms_as_the_dashboard(client):
    cl, conn, token, _ = client
    _detect(conn)
    url = "/api/memecoin/wallets/%s/trial" % UID
    dry = _post(cl, token, url, {"action": "start", "chain": "solana", "add": True, "handle": "9xQeWvG8"}).get_json()
    assert dry["ok"] and dry["would_add"] and dry["clip_usd"] == 20.0
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 0
    got = _post(cl, token, url, {"action": "start", "chain": "solana", "add": True, "handle": "9xQeWvG8",
                                 "confirm": True}).get_json()
    assert got["ok"] and got["added"] and got["written"], got
    assert m.wallet_armed_chains(conn, UID) == ["solana"]
    actor = conn.execute("SELECT actor FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert actor == "operator:dashboard"


def test_a_refused_trial_shows_the_gates_own_reason_and_writes_nothing(client):
    cl, conn, token, _ = client
    got = _post(cl, token, "/api/memecoin/wallets/%s/trial" % UID,
                {"action": "start", "chain": "solana", "add": True, "handle": "x", "confirm": True})
    assert got.status_code == 200 and not got.get_json()["ok"]
    assert got.get_json()["reason"].startswith("no on-chain buy on solana in 7 days")
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallets").fetchone()[0] == 0


@pytest.mark.parametrize("keep, armed", [(True, ["solana"]), (False, [])])
def test_ending_a_trial_keeps_or_disarms(client, keep, armed):
    cl, conn, token, _ = client
    _detect(conn)
    url = "/api/memecoin/wallets/%s/trial" % UID
    _post(cl, token, url, {"action": "start", "chain": "solana", "add": True, "handle": "h", "confirm": True})
    got = _post(cl, token, url, {"action": "end", "chain": "solana", "keep_armed": keep, "disarm": not keep,
                                 "confirm": True}).get_json()
    assert got["ok"] and m.open_trial(conn, UID, "solana") is None
    assert m.wallet_armed_chains(conn, UID) == armed


def _write_funnel(tmp_path, **over):
    body = {"stamp": {"generated_at": "2026-10-02T10:00:00"},
            "funnel": [{"uid": "u-paper", "handle": "paperguy", "stage": "paper", "armed": [], "trial": False,
                        "paper_n": 30, "paper_ret": 0.05}],
            "solana_candidates": [{"address": ADDR, "coins": 5, "won": 3, "best_sources": 2,
                                   "found_by": [], "unchecked": [], "commands": []}],
            "evm_candidates": {}, "rotation": [{"uid": "u-rot", "handle": "rot", "why": ["silent 9 d"]}]}
    body.update(over)
    (tmp_path / "data" / "wallet_follow_funnel.json").write_text(json.dumps(body))


def test_the_overlay_moves_a_trialled_candidate_into_the_pipeline_and_drops_the_retired(client):
    cl, conn, token, tmp_path = client
    _write_funnel(tmp_path)
    _detect(conn)
    for uid, h in (("u-paper", "paperguy"), ("u-rot", "rot")):
        m.add_wallet(conn, uid, h, "")
    m.set_wallet_chain_armed(conn, "u-paper", "4663", True, force=True)
    _post(cl, token, "/api/memecoin/wallets/%s/trial" % UID,
          {"action": "start", "chain": "solana", "add": True, "handle": "9xQeWvG8", "confirm": True})
    assert cl.delete("/api/memecoin/wallets/u-rot", headers={"X-CSRF-Token": token}).status_code == 200
    body = cl.get("/api/memecoin/wallet-map/funnel").get_json()
    stages = {r["uid"]: (r["stage"], r["armed"], r.get("trial_chains")) for r in body["funnel"]}
    assert stages[UID] == ("trial", ["solana"], ["solana"])           # the candidate moved tab
    assert stages["u-paper"] == ("armed", ["4663"], [])                # armed live, not the file's paper
    assert body["solana_candidates"] == [] and body["rotation"] == []  # moved / retired
    assert body["clips"]["solana"]["trial"] == 20.0 and body["stamp"]["live_at"]


def _actions(expr):
    js = "const C = require('./static/wallet_map.js'); process.stdout.write(JSON.stringify(%s));" % expr
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


CLIPS = {"solana": {"trial": 20, "full_max": 175}, "56": {"trial": None, "full_max": 150}}


def test_each_candidate_action_names_its_endpoint():
    acts = _actions("C.actionsFor('cand', {uid: 'pump:%s', handle: '9xQeWvG8', chain: 'solana'}, %s)"
                    % (ADDR, json.dumps(CLIPS)))
    by = {a["key"]: a for a in acts}
    assert by["trial"]["steps"] == [{"method": "POST", "url": "/api/memecoin/wallets/pump%3A" + ADDR + "/trial",
                                     "body": {"action": "start", "chain": "solana", "add": True,
                                              "handle": "9xQeWvG8"}, "dry_run": True}]
    assert by["trial"]["money"] and "$20 trial" in by["trial"]["confirm"]
    assert [s["url"] for s in by["arm"]["steps"]] == ["/api/memecoin/wallets", "/api/memecoin/wallets/pump%3A" + ADDR + "/real"]
    assert by["arm"]["steps"][1]["body"] == {"chain": "solana", "armed": True, "reason": "Wallet Map"}
    assert by["arm"]["money"] and "at most $175" in by["arm"]["confirm"]
    assert by["watch"]["steps"] == [{"method": "POST", "url": "/api/memecoin/wallets",
                                     "body": {"source_user_id": "pump:" + ADDR, "handle": "9xQeWvG8",
                                              "label": "follow funnel"}}]
    assert not by["watch"]["money"]


def test_a_candidate_without_a_proven_address_gets_disabled_buttons_with_the_reason():
    acts = _actions("C.actionsFor('cand', {uid: 'u1', handle: 'h', chain: '56', no_commands: 'no proven 56 address'}, %s)"
                    % json.dumps(CLIPS))
    assert acts and all(a["disabled"] == "no proven 56 address" for a in acts if a["key"] in ("trial", "arm"))


def test_stage_actions_name_their_endpoints():
    paper = {a["key"]: a for a in _actions("C.actionsFor('paper', {uid: 'u1', handle: 'h', chain: 'solana'}, %s)" % json.dumps(CLIPS))}
    assert set(paper) == {"trial", "arm", "retire"}
    assert paper["retire"]["steps"] == [{"method": "DELETE", "url": "/api/memecoin/wallets/u1", "body": None}]
    trial = {a["key"]: a for a in _actions("C.actionsFor('trial', {uid: 'u1', handle: 'h', trial_chains: ['solana']}, %s)" % json.dumps(CLIPS))}
    assert trial["keep:solana"]["steps"][0]["body"] == {"action": "end", "chain": "solana", "keep_armed": True, "confirm": True}
    assert trial["keep:solana"]["money"] and not trial["end:solana"]["money"]
    assert trial["end:solana"]["steps"][0]["body"] == {"action": "end", "chain": "solana", "disarm": True, "confirm": True}
    armed = {a["key"]: a for a in _actions("C.actionsFor('armed', {uid: 'u1', handle: 'h', armed: ['solana', '4663']}, %s)" % json.dumps(CLIPS))}
    assert armed["disarm:4663"]["steps"][0] == {"method": "POST", "url": "/api/memecoin/wallets/u1/real",
                                                "body": {"chain": "4663", "armed": False, "reason": "Wallet Map"}}
    assert armed["pause"]["steps"][0]["body"] == {"paused": True}
    rot = _actions("C.actionsFor('rotation', {uid: 'u1', handle: 'h'}, %s)" % json.dumps(CLIPS))
    assert [a["key"] for a in rot] == ["retire"]


def test_cached_evm_candidates_cannot_enter_live_roster_controls(client):
    cl, conn, token, tmp_path = client
    _write_funnel(tmp_path, evm_candidates={"56": {"base_rate": 0.1, "candidates": [
        {"uid": "87f77c24-44e0-5696-baaf-dfe732083954", "handle": "old", "coins": 10, "ran": 4,
         "commands": ["python3 scripts/memecoin_follow_add.py x --handle old --confirm"]}]}})
    body = cl.get("/api/memecoin/wallet-map/funnel").get_json()
    assert body["evm_candidates"] == {} and body['funnel'] == []


def test_the_trial_route_keeps_its_own_csrf_check_with_the_blanket_guard_off(tmp_path, monkeypatch):
    """MEMECOIN_WEB_CSRF_ENFORCE_ALL=0 drops the blueprint guard; a money route keeps its own."""
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": _conn(), "csrf_enforce_all": False}))
    r = app.test_client().post("/api/memecoin/wallets/%s/trial" % UID, json={"action": "start", "chain": "solana"})
    assert r.status_code == 403


def test_a_trial_write_that_raises_never_leaves_the_ledger_write_locked(client, monkeypatch):
    """Whatever raises after a write's implicit BEGIN, the route ends the transaction (Gamma, 2026-10-02):
    the web thread's connection is on the funded ledger."""
    cl, conn, token, _ = client

    def half_written(c, *a, **k):
        c.execute("INSERT INTO memecoin_wallets(source_user_id, handle, label, paused, source, added_at) "
                  "VALUES ('u-x', 'x', '', 0, 'manual', '2026-10-02')")
        raise sqlite3.IntegrityError("boom")
    monkeypatch.setattr(ops, "start", half_written)
    r = _post(cl, token, "/api/memecoin/wallets/%s/trial" % UID, {"action": "start", "chain": "solana", "confirm": True})
    assert r.status_code == 500 and not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM memecoin_wallets WHERE source_user_id='u-x'").fetchone()[0] == 0


def test_the_overlay_names_the_graders_stop_on_the_trial_row(client):
    """Gamma L1: the trial row carries the stop, so the page can show it and Keep armed can name it."""
    cl, conn, token, tmp_path = client
    _write_funnel(tmp_path)
    _detect(conn)
    _post(cl, token, "/api/memecoin/wallets/%s/trial" % UID,
          {"action": "start", "chain": "solana", "add": True, "handle": "9xQeWvG8", "confirm": True})
    t = m.open_trial(conn, UID, "solana")
    conn.execute("INSERT INTO memecoin_trial_verdicts (trial_id, checkpoint, checked_at, closes, mean_r, verdict, "
                 "action, detail_json) VALUES (?, '5', '2026-10-02T11:00:00+00:00', 5, -0.32, 'DEMOTE', 'stopped', '{}')",
                 (t["id"],))
    m.stop_trial(conn, t["id"], now_iso="2026-10-02T11:00:00+00:00")
    rows = {r["uid"]: r for r in cl.get("/api/memecoin/wallet-map/funnel").get_json()["funnel"]}
    assert rows[UID]["trial_stops"] == {"solana": {"stopped_at": "2026-10-02T11:00:00+00:00", "verdict": "DEMOTE",
                                                   "mean_r": -0.32, "closes": 5}}
    assert 'u-paper' not in rows  # cache-only identity is not a live roster wallet
    url = "/api/memecoin/wallets/%s/trial" % UID
    got = _post(cl, token, url, {"action": "end", "chain": "solana", "keep_armed": True, "confirm": True}).get_json()
    assert not got["ok"] and "DEMOTE" in got["reason"] and m.open_trial(conn, UID, "solana")
    got = _post(cl, token, url, {"action": "end", "chain": "solana", "keep_armed": True, "confirm": True,
                                 "ack_stop": "DEMOTE"}).get_json()
    assert got["ok"] and m.wallet_armed_chains(conn, UID) == ["solana"]


def test_the_overlay_drops_an_excluded_wallet_from_every_candidate_list(client):
    """Gamma L3: promote() kept a retired roster wallet in solana_/evm_candidates."""
    cl, conn, token, tmp_path = client
    evm_uid = "87f77c24-44e0-5696-baaf-dfe732083954"
    _write_funnel(tmp_path, evm_candidates={"56": {"base_rate": 0.1, "candidates": [
        {"uid": evm_uid, "handle": "old", "coins": 10, "ran": 4, "commands": []}]}})
    for uid, h in ((UID, "sol"), (evm_uid, "old")):
        m.add_wallet(conn, uid, h, "")
        m.set_wallet_excluded(conn, uid, True)
    body = cl.get("/api/memecoin/wallet-map/funnel").get_json()
    assert body["solana_candidates"] == [] and body["evm_candidates"] == {}
    assert UID not in {r["uid"] for r in body["funnel"]}


STOPS = {"solana": {"verdict": "DEMOTE", "mean_r": -0.32, "closes": 5, "stopped_at": "2026-10-02T11:00:00+00:00"},
         "56": {"verdict": "PROMOTE", "mean_r": 0.12, "closes": 10, "stopped_at": "2026-10-02T11:05:00+00:00"}}


def test_keep_armed_on_a_stopped_trial_names_the_stop_and_sends_it():
    """Gamma L1: the confirm names the grader's stop, and only that confirm sends ack_stop."""
    trial = {a["key"]: a for a in _actions(
        "C.actionsFor('trial', {uid: 'u1', handle: 'h', trial_chains: ['solana', '56', '4663'], trial_stops: %s}, %s)"
        % (json.dumps(STOPS), json.dumps(CLIPS)))}
    keep = trial["keep:solana"]
    assert "STOPPED" in keep["confirm"] and "DEMOTE" in keep["confirm"] and "-32.0% on 5" in keep["confirm"]
    assert keep["steps"][0]["body"]["ack_stop"] == "DEMOTE" and keep["label"].endswith("override DEMOTE")
    assert "PROMOTE" in trial["keep:56"]["confirm"] and "ack_stop" not in trial["keep:56"]["steps"][0]["body"]
    assert "STOPPED" not in trial["keep:4663"]["confirm"] and "ack_stop" not in trial["keep:4663"]["steps"][0]["body"]


def test_the_chain_picker_re_enables_trial_where_the_picked_chain_has_a_clip():
    """Gamma L4: Trial's disabled state came from the default chain only."""
    clips = {"solana": {"trial": None, "full_max": 175}, "4663": {"trial": 20, "full_max": 100}}
    out = _actions("""(() => {
      const b = [{dataset: {key: 'trial', blocked: '1'}, disabled: true, title: 'no trial clip on Solana', textContent: 'Trial'},
                 {dataset: {key: 'arm'}, disabled: false, title: '', textContent: 'Arm full'}];
      const at = ch => C.actionsFor('paper', {uid: 'u1', handle: 'h', chain: ch}, %s);
      C.syncButtons(b, at('4663'));
      const on = JSON.parse(JSON.stringify(b));
      C.syncButtons(b, at('solana'));
      return [on, b]; })()""" % json.dumps(clips))
    on, off = out
    assert on[0] == {"dataset": {"key": "trial"}, "disabled": False, "title": "", "textContent": "Trial $20"}
    assert off[0]["disabled"] and off[0]["dataset"]["blocked"] == "1" and off[0]["title"].startswith("no trial clip")
    assert not on[1]["disabled"] and not off[1]["disabled"]


def test_trial_is_the_primary_action_on_candidate_and_paper_rows():
    """Operator 2026-10-02 (via Alpha): Arm full on a candidate met the promote gate (0 closes, $0 copy
    PnL) and could never work; Trial is the way in, so it is the primary button and Arm full says why."""
    for kind, row in (("cand", "{uid: 'pump:%s', handle: 'h', chain: 'solana'}" % ADDR),
                      ("paper", "{uid: 'u1', handle: 'h', chain: 'solana'}")):
        by = {a["key"]: a for a in _actions("C.actionsFor('%s', %s, %s)" % (kind, row, json.dumps(CLIPS)))}
        assert by["trial"].get("primary") and not by["arm"].get("primary")
        assert "promote gate" in by["arm"]["confirm"] and "Trial" in by["arm"]["confirm"]


# --- the operator's override of the promote gate (operator yes 2026-10-02, via Alpha) ----------------

def _run(kind, answers, replies):
    """Drive runSteps with scripted confirm answers and /real replies; return (message, sent, asked)."""
    js = """
      (async () => {
        const answers = %s, replies = %s, sent = [], asked = [];
        const send = async (method, url, body) => {
          sent.push({method, url, body});
          if (url.endsWith('/real')) return replies.shift();
          return {ok: true};
        };
        const ask = msg => { asked.push(msg); return answers.shift() || false; };
        const row = %s;
        const act = C.actionsFor('%s', row, %s).find(a => a.key === 'arm');
        const msg = await C.runSteps(act, send, ask);
        process.stdout.write(JSON.stringify([msg, sent, asked]));
      })();""" % (json.dumps(answers), json.dumps(replies),
                  "{uid: 'pump:%s', handle: 'h', chain: 'solana'}" % ADDR if kind == "cand"
                  else "{uid: 'u1', handle: 'h', chain: 'solana'}", kind, json.dumps(CLIPS))
    out = subprocess.run(["node", "-e", "const C = require('./static/wallet_map.js');" + js],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


REFUSED = {"ok": False, "action": "approve_refused",
           "reason": "gate: 0 alpha_b closes on solana (need >=3) and $0.00 copy PnL (need >=$0.00)"}


@pytest.mark.parametrize("kind", ["cand", "paper"])
def test_the_override_needs_the_arm_confirm_and_a_second_one_naming_the_refusal(kind):
    forced = lambda sent: [s for s in sent if s["url"].endswith("/real") and s["body"].get("force")]
    msg, sent, asked = _run(kind, [False], [])                          # first confirm declined
    assert msg is None and sent == [] and len(asked) == 1
    msg, sent, asked = _run(kind, [True, False], [REFUSED])              # override declined
    assert not forced(sent) and "refused: gate" in msg and len(asked) == 2
    assert "0 alpha_b closes" in asked[1] and "h" in asked[1] and "Solana" in asked[1] and "$175" in asked[1]
    assert "operator override" in asked[1]
    msg, sent, asked = _run(kind, [True, True], [REFUSED, {"ok": True}])  # both confirms
    (f,) = forced(sent)
    assert f["body"] == {"chain": "solana", "armed": True, "reason": "Wallet Map", "force": True,
                         "actor": "operator:dashboard"}
    assert msg.startswith("done:") and "operator override" in msg


@pytest.mark.parametrize("reply", [{"ok": False, "action": "identity_required", "reason": "funded authority requires an identified source"},
                                   {"ok": False, "action": "excluded_requires_reobserve", "reason": "excluded"}])
def test_no_override_is_offered_for_anything_but_the_promote_gate(reply):
    msg, sent, asked = _run("paper", [True, True], [reply])
    assert len(asked) == 1 and not any(s["body"].get("force") for s in sent if s["body"]) and "refused" in msg


@pytest.mark.parametrize("actor, recorded", [("operator:dashboard", "operator:dashboard"), (None, "ui"), ("root", "ui")])
def test_the_real_route_records_the_dashboard_actor_only_when_the_page_sends_it(client, actor, recorded):
    cl, conn, token, _ = client
    m.add_wallet(conn, "u-o", "o", "")
    body = {"chain": "solana", "armed": True, "reason": "Wallet Map", "force": True}
    if actor:
        body["actor"] = actor
    r = _post(cl, token, "/api/memecoin/wallets/u-o/real", body)
    assert r.status_code == 200, r.get_json()
    assert conn.execute("SELECT actor FROM memecoin_wallet_approvals ORDER BY id DESC LIMIT 1").fetchone()[0] == recorded


def test_missing_file_still_shows_live_arm(client):
    cl, conn, token, _ = client
    m.add_wallet(conn, 'new-live', 'new', '')
    m.set_wallet_chain_armed(conn, 'new-live', 'robinhood', True, force=True)
    body = cl.get('/api/memecoin/wallet-map/funnel').get_json()
    row = next(r for r in body['funnel'] if r['uid'] == 'new-live')
    assert row['armed'] == ['4663']
    assert row['effective_chains'] == ['4663']
    assert 'paper_ret' not in row and 'measurement_state' not in row
    assert body['stamp']['authority_state'] == 'ok'


def test_overlay_paused_leftover_arm_is_not_effective(client):
    from sniper.services.memecoin_wallet_map import funnel_live_overlay
    from sniper.settings import memecoin as cfg
    _, conn, _, _ = client
    m.add_wallet(conn, 'paused-live', 'paused', '')
    m.set_wallet_chain_armed(conn, 'paused-live', 'solana', True, force=True)
    conn.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='paused-live'")
    body = funnel_live_overlay({'funnel': []}, conn, cfg, '2026-10-02T00:00:00+00:00')
    row = next(r for r in body['funnel'] if r['uid'] == 'paused-live')
    assert row['armed'] == ['solana'] and row['effective_chains'] == []
    assert row['stage'] not in ('armed', 'trial')


def test_overlay_failure_does_not_assert_cached_authority(client, monkeypatch):
    from sniper.services import memecoin_wallet_map
    cl, conn, token, tmp_path = client
    _write_funnel(tmp_path)
    def fail(*a, **k):
        raise RuntimeError('read unavailable')
    monkeypatch.setattr(memecoin_wallet_map, 'funnel_live_overlay', fail)
    body = cl.get('/api/memecoin/wallet-map/funnel').get_json()
    assert body['stamp']['authority_state'] == 'unavailable'


def test_armed_link_keeps_the_selected_chain():
    assert _actions("C.armedUrl('4663')") == '/?tab=memecoin&mc_view=wallets&mc_chain=4663'


def test_failed_authority_read_is_not_watch_only():
    assert _actions("C.authorityAfterReadback('A','4663',{stamp:{authority_state:'unavailable'}})") == 'unavailable'
    assert _actions("C.authorityAfterReadback('A','4663',{stamp:{authority_state:'ok'},funnel:[{uid:'A',effective_chains:['4663']}]})") == 'armed'


def test_shared_coin_rows_count_distinct_coins_not_events():
    assert _actions("C.sharedCoinRows([{src_uid:'A',dst_uid:'B',coins:['M','M']},{src_uid:'B',dst_uid:'A',coins:['M']}],['A','B'])") == [{'a':'A','b':'B','coins':['M'],'n':1}]


def test_universe_removes_excluded_cached_authority_and_marks_new_live_roster(client):
    cl,conn,token,tmp_path=client
    m.add_wallet(conn,'excluded','old','');m.add_wallet(conn,'new','new','')
    conn.execute("UPDATE memecoin_wallets SET excluded=1 WHERE source_user_id='excluded'");conn.commit()
    (tmp_path/'data'/'wallet_universe_4663.json').write_text(json.dumps(dict(
        version=1,chain='4663',wallets=[dict(uid='excluded',armed=True,effective=True)],
        edges=[dict(src_uid='excluded',dst_uid='new')],graph_state='ok')))
    body=cl.get('/api/memecoin/wallet-map/universe?chain=4663').get_json()
    assert [w['uid'] for w in body['wallets']]==['new']
    assert body['wallets'][0]['from_roster'] is True and body['edges']==[]
