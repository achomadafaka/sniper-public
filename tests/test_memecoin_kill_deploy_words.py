"""A refusal during a deploy's own kill-switch trip says so (Alpha, 2026-10-07; operator ref 1bf3a775).

Every deploy trips the switch for ~20-25 s while the runner restarts. A buy refused in that window
paged "Why: kill switch is on" (HOTDOG, refused 17:16:11Z inside the runtime-deploy hold
17:15:48.67Z..17:16:13.86Z), and the operator read it as an emergency stop. The refusal reason
stays `kill_switch_tripped`; only the card's words change. The hold is read from the switch's own
audit log, not the live file: that hold cleared 2.7 s after the refusal, before the card rendered.
"""
import sqlite3

from sniper.services import memecoin_arming as A
from sniper.services.memecoin_telegram import render_entry_notification
from test_memecoin_notification_lifecycle import view

DEPLOY = "runtime-deploy"
LOG = """\
2026-10-07T17:05:52.642131+00:00 TRIP source=runtime-deploy token=t1
2026-10-07T17:06:14.267133+00:00 CLEAR source=runtime-deploy token=t1
2026-10-07T17:10:00.000000+00:00 TRIP source=web:127.0.0.1 token=t2
2026-10-07T17:12:00.000000+00:00 CLEAR source=web:127.0.0.1 token=t2
2026-10-07T17:15:48.671565+00:00 TRIP source=runtime-deploy token=t3
2026-10-07T17:16:13.860379+00:00 CLEAR source=runtime-deploy token=t3
2026-10-07T17:30:00.000000+00:00 TRIP source=runtime-deploy token=t4
2026-10-07T17:30:05.000000+00:00 CLEAR_REJECTED source=web:127.0.0.1 token=-
2026-10-07T17:40:00.000000+00:00 CLEAR source=runtime-deploy token=t4
2026-10-07T18:00:00.000000+00:00 TRIP source=runtime-deploy token=t5
"""


def _log(tmp_path, text=LOG):
    path = tmp_path / "kill_switch_audit.log"
    path.write_text(text)
    return path


def _restart(tmp_path, at, now="2026-10-07T18:00:30+00:00", text=LOG):
    return A.kill_deploy_restart_at(at, now_iso=now, audit_path=_log(tmp_path, text))


def test_a_refusal_inside_a_short_deploy_hold_is_a_deploy_restart(tmp_path):
    assert _restart(tmp_path, "2026-10-07T17:16:11.400000+00:00") is True        # HOTDOG
    assert _restart(tmp_path, "2026-10-07T17:06:00+00:00") is True


def test_an_operator_hold_or_a_deploy_hold_past_the_grace_is_not(tmp_path):
    assert _restart(tmp_path, "2026-10-07T17:11:00+00:00") is False              # web hold
    assert _restart(tmp_path, "2026-10-07T17:35:00+00:00") is False              # 10 min deploy hold


def test_an_open_deploy_hold_counts_only_while_inside_the_grace(tmp_path):
    at = "2026-10-07T18:00:10+00:00"
    assert _restart(tmp_path, at, now="2026-10-07T18:00:30+00:00") is True
    assert _restart(tmp_path, at, now="2026-10-07T18:05:00+00:00") is False      # the deploy left it on


def test_no_covering_hold_or_no_log_is_unknown(tmp_path):
    assert _restart(tmp_path, "2026-10-07T17:20:00+00:00") is None               # after t3 cleared
    assert _restart(tmp_path, "2026-10-07T17:00:00+00:00") is None               # before any trip
    assert A.kill_deploy_restart_at("2026-10-07T17:16:11+00:00", now_iso="2026-10-07T18:00:00+00:00",
                                    audit_path=tmp_path / "missing.log") is None
    assert _restart(tmp_path, None) is None
    assert _restart(tmp_path, "2026-10-07T17:16:11+00:00", text="garbage line\n\n") is None


def _card(**fields):
    v = view("refused", reason="kill_switch_tripped", **fields)
    return render_entry_notification(v)


def test_the_card_names_a_deploy_restart_and_keeps_the_switch_wording_otherwise():
    assert "Why: deploy restart (kill switch auto-trip)" in _card(kill_deploy_restart=True)
    assert "Why: kill switch is on" in _card(kill_deploy_restart=False)
    assert "Why: kill switch is on" in _card()


def test_the_worker_reads_the_hold_at_the_decision(tmp_path, monkeypatch):
    from sniper.persistence import db_memecoin as m
    from sniper.services import memecoin_telegram as tg
    path = tmp_path / "ledger.db"
    conn = sqlite3.connect(path)
    m.ensure_memecoin_tables(conn)
    conn.close()
    monkeypatch.setenv("DB_PATH", str(path))
    seen = []
    monkeypatch.setattr(A, "kill_deploy_restart_at", lambda at, **k: seen.append(at) or True)
    worker = tg._NotificationWorker()
    text = worker._render(view("refused", reason="kill_switch_tripped",
                               decision_started_at="2026-10-07T17:16:11.400000+00:00"))
    assert seen == ["2026-10-07T17:16:11.400000+00:00"]
    assert "Why: deploy restart (kill switch auto-trip)" in text
    seen.clear()
    worker._render(view("refused", reason="not_approved", decision_started_at="2026-10-07T17:16:11+00:00"))
    assert seen == []                                                            # other refusals: no read


def test_the_grace_is_read_when_asked_not_at_import(tmp_path, monkeypatch):
    """Gamma INFO (a) on dec777806: an import-time constant freezes the default in any process that
    imports memecoin_arming before its .env is loaded."""
    at = "2026-10-07T17:16:11.400000+00:00"
    monkeypatch.setenv("MEMECOIN_KILL_DEPLOY_GRACE_S", "10")
    assert _restart(tmp_path, at) is False                                       # the 25 s hold > 10 s
    monkeypatch.setenv("MEMECOIN_KILL_DEPLOY_GRACE_S", "180")
    assert _restart(tmp_path, at) is True


# --- the dashboard (Gamma scope note, Alpha 2026-10-07: static/app.js _reasonWords) -------------

def test_the_entries_route_marks_deploy_restarts_with_one_log_read(monkeypatch):
    from flask import Flask
    from sniper.persistence import db_memecoin as m
    from sniper.routes import api_memecoin as api
    from test_memecoin_entry_view import attempt
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    for aid, origin, reason in (("a1", "e1", "kill_switch_tripped"), ("a2", "e2", "kill_switch_tripped"),
                                ("a3", "e3", "not_approved")):
        attempt(c, aid=aid, origin=origin, status="refused", terminal_reason=reason)
    reads, seen = [], []
    monkeypatch.setattr(A, "read_kill_audit_tail", lambda *a, **k: reads.append(1) or "TAIL")
    monkeypatch.setattr(A, "kill_deploy_restart_at",
                        lambda at, **k: seen.append(k.get("tail")) or True)
    app = Flask(__name__)
    app.register_blueprint(api.create_api_memecoin_blueprint({"conn": c}))
    rows = app.test_client().get("/api/memecoin/entries").get_json()["entries"]
    marks = {r["attempt_id"]: r.get("kill_deploy_restart") for r in rows}
    assert marks == {"a1": True, "a2": True, "a3": None}
    assert reads == [1] and seen == ["TAIL", "TAIL"]


def _js(method_names, calls):
    import json
    import shutil
    import subprocess
    from pathlib import Path
    app = (Path(__file__).resolve().parents[1] / "static" / "app.js").read_text()
    starts = {"_reasonWords": "    _reasonWords(", "_shortReason": "    _shortReason(row) {"}
    ends = {"_reasonWords": "    _entryDelivery(", "_shortReason": "    _entryLines("}
    body = "".join(app[app.index(starts[n]):app.index(ends[n])] for n in method_names)
    script = "const view = { _fmtMcapShort() { return ''; }, " + body + "}; "
    script += "console.log(JSON.stringify([" + ",".join(calls) + "]));"
    node = shutil.which("node")
    assert node
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_the_dashboard_words_match_the_card():
    deploy = '{reason: "kill_switch_tripped", status: "refused", kill_deploy_restart: true}'
    plain = '{reason: "kill_switch_tripped", status: "refused"}'
    got = _js(["_reasonWords", "_shortReason"], [
        'view._reasonWords("kill_switch_tripped", %s)' % deploy,
        'view._reasonWords("kill_switch_tripped", %s)' % plain,
        'view._reasonWords("kill_switch_tripped")',
        'view._shortReason(%s)' % deploy])
    assert got == ["deploy restart (kill switch auto-trip)", "kill switch is on", "kill switch is on",
                   "deploy restart (kill switch auto-trip)"]
