"""Pre-existing provider terms at deploy (Alpha GO 2026-10-16).

3a2b415a8 was refused after the switch on shyft_runtime_prepare_unready, a RabbitStream outage the OLD
runner had too (vendor down since ~18:20Z, before the cutover); the rollback then failed the same gate
on the restored release and kept the entry hold until a manual CLEAR (18:29:49-18:37:54Z). The old
runner's --shyft-runtime-only terms, read before the trip, are now the baseline: a term already failing
then pages once and does not refuse; a fresh term still refuses; no baseline is the old strict gate.

Every deploy function runs here alone under `bash -c` with a fake PY (the extraction pattern of
test_memecoin_provider_rh_cutover): no deploy runs and nothing touches the live checkout.
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts/deploy_memecoin_runtime.sh"
PREPARE = "shyft_runtime_prepare_unready"
OBSERVER = "shyft_runtime_observer_unready"
RH_PROCESS = "shyft_runtime_rh_process_invalid"


def function(name):
    text = DEPLOY.read_text()
    body = text[text.index("\n" + name + "() {") + 1:]
    return body[:body.index("\n}\n") + 3]


# ---- the verifier: baseline subtraction -----------------------------------------------------------

def _verify_module():
    spec = importlib.util.spec_from_file_location("runtime_verify_under_test", ROOT / "scripts/memecoin_runtime_verify.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module          # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


def test_a_baseline_term_is_preexisting_and_any_other_is_fresh():
    v = _verify_module()
    assert v.split_preexisting([PREPARE, OBSERVER], frozenset({PREPARE})) == ([OBSERVER], [PREPARE])
    assert v.split_preexisting([PREPARE], frozenset({PREPARE})) == ([], [PREPARE])
    assert v.split_preexisting([OBSERVER], frozenset()) == ([OBSERVER], [])


@pytest.mark.parametrize("content", [None, "", "{not json", '{"errors": []}', '[1, 2]', '"x"'])
def test_an_unreadable_or_malformed_baseline_excuses_nothing(tmp_path, content):
    v = _verify_module()
    path = tmp_path / "baseline.json"
    if content is not None:
        path.write_text(content)
    assert v.baseline_terms(path) == frozenset()


def _main(monkeypatch, capsys, errors, argv):
    v = _verify_module()
    monkeypatch.setattr(v, "verify_shyft_runtime_graph", lambda code_root, state_root: list(errors))
    monkeypatch.setattr(sys, "argv", ["verify"] + argv)
    with pytest.raises(SystemExit) as exit_:
        v.main()
    return exit_.value.code, json.loads(capsys.readouterr().out)


def test_only_vendor_terms_are_excusable_after_the_switch_and_all_on_rollback():
    """Gamma B1 on 00459ca48: a process/graph/config/TPU term is ours; only the rollback excuses it."""
    v = _verify_module()
    baseline = frozenset({PREPARE, RH_PROCESS, "shyft_runtime_tpu_unready"})
    assert v.excusable_terms(baseline, "vendor") == frozenset({PREPARE})
    assert v.excusable_terms(baseline, "all") == baseline
    assert v.VENDOR_TERMS == frozenset({PREPARE})


def test_the_cli_reports_preexisting_and_refuses_only_on_fresh(tmp_path, monkeypatch, capsys):
    base = tmp_path / "b.json"
    base.write_text(json.dumps([PREPARE]))
    args = ["--code-root", str(tmp_path), "--state-root", str(tmp_path), "--shyft-runtime-only"]
    assert _main(monkeypatch, capsys, [PREPARE], args + ["--baseline-errors", str(base)]) == (
        0, {"ok": True, "errors": [], "preexisting": [PREPARE]})
    assert _main(monkeypatch, capsys, [PREPARE, OBSERVER], args + ["--baseline-errors", str(base)]) == (
        1, {"ok": False, "errors": [OBSERVER], "preexisting": [PREPARE]})
    assert _main(monkeypatch, capsys, [PREPARE], args) == (1, {"ok": False, "errors": [PREPARE]})   # no baseline
    base.write_text(json.dumps([PREPARE, RH_PROCESS]))
    assert _main(monkeypatch, capsys, [RH_PROCESS], args + ["--baseline-errors", str(base)]) == (
        1, {"ok": False, "errors": [RH_PROCESS], "preexisting": []})                          # vendor scope
    assert _main(monkeypatch, capsys, [RH_PROCESS], args + ["--baseline-errors", str(base),
                                                            "--baseline-scope", "all"]) == (
        0, {"ok": True, "errors": [], "preexisting": [RH_PROCESS]})


# ---- the deploy functions -------------------------------------------------------------------------

FAKE_PY = r'''#!/usr/bin/env python3
"""Fake PY: the verifier answers TEST_TERMS (a comma list, or TEST_GARBAGE); every -c snippet runs on
the real interpreter, the page's against the fake sniper package the fixture writes into DEFAULT_SOURCE."""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["TEST_CALLS"], "a") as f:
    f.write(" ".join(a for a in args if not a.startswith("import")) + "\n")
if args and args[0] == "-c":
    os.execv(os.environ["REAL_PY"], [os.environ["REAL_PY"]] + args)
if args and args[0].endswith("memecoin_runtime_verify.py"):
    if "--shyft-runtime-only" not in args:            # --rollback-only, --rh-process-only: ready
        print(json.dumps({"ok": True, "errors": []}))
        sys.exit(0)
    if os.environ.get("TEST_GARBAGE") == "1":
        print("not json")
        sys.exit(1)
    errors = [t for t in os.environ.get("TEST_TERMS", "").split(",") if t]
    if "--baseline-errors" in args:
        try:
            baseline = set(json.load(open(args[args.index("--baseline-errors") + 1])))
        except Exception:
            baseline = set()
        scope = args[args.index("--baseline-scope") + 1] if "--baseline-scope" in args else "vendor"
        if scope != "all":
            baseline &= {"shyft_runtime_prepare_unready"}
        fresh = [e for e in errors if e not in baseline]
        print(json.dumps({"ok": not fresh, "errors": fresh, "preexisting": [e for e in errors if e in baseline]}))
        sys.exit(1 if fresh else 0)
    print(json.dumps({"ok": not errors, "errors": errors}))
    sys.exit(1 if errors else 0)
sys.exit(0)
'''


FAKE_TELEGRAM = """import os
def send_telegram(text):
    with open(os.environ["TEST_PAGES"], "a") as f:
        f.write(text.replace("\\n", " ") + "\\n")
    # Delivered only when the token came from the state root's .env (the one-shot must load it).
    ok = os.environ.get("MEMECOIN_TG_BOT_TOKEN") == "from-state-env" and os.environ.get("TEST_TG_FAIL") != "1"
    return (ok, 0.0)


def dispatch_off():
    return os.environ.get("MEMECOIN_TG_DISPATCH_ENABLED", "1").strip().lower() in ("0", "false", "no")


def send_telegram_audited(text, *, kind="generic", source_id=None):
    ok, ms = send_telegram(text)
    with open(os.environ["TEST_PAGES"] + ".audit", "a") as f:
        f.write("%s %d\\n" % (kind, int(ok)))
    return ok, ms
"""


def _fake_sniper(root):
    """DEFAULT_SOURCE's sniper package for the page one-shot: the real envfiles, a fake send_telegram."""
    for pkg in ("sniper", "sniper/util", "sniper/services"):
        (root / pkg).mkdir(parents=True, exist_ok=True)
        (root / pkg / "__init__.py").write_text("")
    (root / "sniper/util/envfiles.py").write_text((ROOT / "sniper/util/envfiles.py").read_text())
    (root / "sniper/services/memecoin_telegram.py").write_text(FAKE_TELEGRAM)


TG_KEYS = ("MEMECOIN_TG_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "MEMECOIN_TG_CHAT_ID", "TELEGRAM_CHAT_ID",
           "MEMECOIN_TG_DISPATCH_ENABLED")


@pytest.fixture
def shell(tmp_path):
    fake = tmp_path / "python"
    fake.write_text(FAKE_PY)
    fake.chmod(0o755)
    (tmp_path / "data").mkdir()
    (tmp_path / "old").mkdir()
    _fake_sniper(tmp_path)
    (tmp_path / ".env").write_text("MEMECOIN_TG_BOT_TOKEN=from-state-env\n")

    def run(body, *, terms="", garbage=False, extra=(), tg_fail=False):
        harness = "\n".join(["set -euo pipefail", f"PY={fake}", f"STATE_ROOT={tmp_path}",
                             f"RELEASE={tmp_path / 'new'}", f"OLD_RELEASE={tmp_path / 'old'}",
                             f"DEFAULT_SOURCE={tmp_path}", "SHA=abc123", "SHYFT_RUNTIME=1",
                             'SHYFT_BASELINE=""; SHYFT_PREEXISTING=""; SHYFT_PAGED=0',
                             function("step"), function("snapshot_shyft_baseline"),
                             function("shyft_runtime_gate"), function("page_shyft_preexisting"),
                             *extra, body])
        env = {k: v for k, v in os.environ.items() if k not in TG_KEYS} | dict(
            TEST_CALLS=str(tmp_path / "calls"), TEST_PAGES=str(tmp_path / "pages"), REAL_PY=sys.executable,
            TEST_TERMS=terms, TEST_GARBAGE=str(int(garbage)), TEST_TG_FAIL=str(int(tg_fail)))
        return subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=20, env=env)

    def pages():
        path = tmp_path / "pages"
        return path.read_text().splitlines() if path.exists() else []
    return run, pages, tmp_path


def test_the_snapshot_records_the_old_runners_failing_terms_and_never_aborts(shell):
    run, _pages, tmp = shell
    r = run('snapshot_shyft_baseline; echo "BASELINE=$SHYFT_BASELINE"; cat "$SHYFT_BASELINE"', terms=PREPARE)
    assert r.returncode == 0, r.stderr                     # the verifier's exit 1 is not a set -e abort
    assert "shyft_baseline terms=" + PREPARE in r.stdout
    assert json.loads(r.stdout.strip().splitlines()[-1]) == [PREPARE]
    assert "--code-root %s" % (tmp / "old") in (tmp / "calls").read_text()


def test_an_unreadable_snapshot_is_strict(shell):
    run, _pages, _tmp = shell
    r = run('snapshot_shyft_baseline; echo "BASELINE=[$SHYFT_BASELINE]"', terms=PREPARE, garbage=True)
    assert r.returncode == 0, r.stderr
    assert "terms=unknown (strict)" in r.stdout and "BASELINE=[]" in r.stdout


@pytest.mark.parametrize("runtime, old", [("0", "old"), ("1", "missing")])
def test_no_shyft_runtime_or_no_old_release_takes_no_snapshot(shell, runtime, old):
    run, _pages, tmp = shell
    r = run('SHYFT_RUNTIME=%s; OLD_RELEASE=%s; snapshot_shyft_baseline; echo "BASELINE=[$SHYFT_BASELINE]"'
            % (runtime, shlex.quote(str(tmp / old))), terms=PREPARE)
    assert r.returncode == 0 and "BASELINE=[]" in r.stdout
    assert not (tmp / "calls").exists()


def test_a_preexisting_term_passes_the_gate_and_pages_once(shell):
    run, pages, tmp = shell
    r = run('snapshot_shyft_baseline; shyft_runtime_gate "$RELEASE"; echo "PRE=$SHYFT_PREEXISTING"; '
            'page_shyft_preexisting landed; page_shyft_preexisting landed', terms=PREPARE)
    assert r.returncode == 0, r.stderr
    assert "PRE=" + PREPARE in r.stdout and "shyft_preexisting terms=%s context=landed" % PREPARE in r.stdout
    assert len(pages()) == 1 and PREPARE in pages()[0] and "abc123" in pages()[0]


def test_a_fresh_term_still_refuses(shell):
    run, pages, tmp = shell
    (tmp / "data" / "b.json").write_text(json.dumps([PREPARE]))
    r = run('SHYFT_BASELINE=%s; shyft_runtime_gate "$RELEASE"' % shlex.quote(str(tmp / "data" / "b.json")),
            terms=PREPARE + "," + OBSERVER)
    assert r.returncode == 1 and pages() == []


def test_a_preexisting_rh_process_term_refuses_after_the_switch_but_not_on_rollback(shell):
    """Gamma B1: the snapshot caught the RH feed mid-restart; a release whose own RH feed never starts
    must not land on that. The restored old release against the same baseline is the status quo."""
    run, pages, tmp = shell
    base = shlex.quote(str(tmp / "data" / "b.json"))
    (tmp / "data" / "b.json").write_text(json.dumps([RH_PROCESS, PREPARE]))
    assert run('SHYFT_BASELINE=%s; shyft_runtime_gate "$RELEASE" vendor' % base, terms=RH_PROCESS).returncode == 1
    r = run('SHYFT_BASELINE=%s; shyft_runtime_gate "$RELEASE" vendor; echo "PRE=$SHYFT_PREEXISTING"' % base,
            terms=PREPARE)
    assert r.returncode == 0 and "PRE=" + PREPARE in r.stdout
    r = run('SHYFT_BASELINE=%s; shyft_runtime_gate "$OLD_RELEASE" all; echo "PRE=$SHYFT_PREEXISTING"' % base,
            terms=RH_PROCESS + "," + PREPARE)
    assert r.returncode == 0 and "PRE=%s,%s" % (RH_PROCESS, PREPARE) in r.stdout
    assert pages() == []


def test_a_prepare_term_that_was_clean_before_the_trip_refuses(shell):
    """The 3a2b415a8 term, but fresh: the old runner passed before the trip."""
    run, pages, tmp = shell
    (tmp / "data" / "b.json").write_text("[]")
    r = run('SHYFT_BASELINE=%s; shyft_runtime_gate "$RELEASE"' % shlex.quote(str(tmp / "data" / "b.json")),
            terms=PREPARE)
    assert r.returncode == 1 and pages() == []


def test_without_a_baseline_the_gate_is_todays_strict_gate(shell):
    run, pages, tmp = shell
    assert run('shyft_runtime_gate "$RELEASE"', terms=PREPARE).returncode == 1
    assert run('shyft_runtime_gate "$RELEASE"', terms="").returncode == 0
    calls = (tmp / "calls").read_text()
    assert "--baseline-errors" not in calls


def test_a_failed_page_never_changes_the_verdict(shell):
    run, _pages, tmp = shell
    r = run('SHYFT_PREEXISTING=%s; PY=/bin/false; page_shyft_preexisting landed; echo survived' % PREPARE)
    assert r.returncode == 0 and "survived" in r.stdout and "page failed" in r.stderr


def _one_pass_clock(tmp, name):
    """`date +%s` answers 100 (the deadline is set), 100 (one loop pass), then 200 (expired); every
    other date call (step's timestamp) is the real one."""
    a, b = tmp / (name + ".a"), tmp / (name + ".b")
    return ('date() { if [ "${1:-}" != "+%%s" ]; then command date "$@"; return; fi; '
            'if [ -e %s ]; then if [ -e %s ]; then echo 200; else touch %s; echo 100; fi; '
            'else touch %s; echo 100; fi; }' % (a, b, b, a))


ROLLBACK = ('PROVIDER_RH_STOPPED=0; PROVIDER_ROLLBACK_STARTED=100; '
            'verify_provider_rollback && page_shyft_preexisting "rolled back" && echo ROLLED_BACK_OK')


def test_the_rollback_gate_passes_on_preexisting_terms_so_the_hold_clears(shell):
    run, pages, tmp = shell
    with open(tmp / ".env", "a") as f:
        f.write("MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\n")
    r = run("snapshot_shyft_baseline; " + ROLLBACK, terms=PREPARE,
            extra=(_one_pass_clock(tmp, "c1"), function("verify_provider_rollback")))
    assert r.returncode == 0 and "ROLLED_BACK_OK" in r.stdout, r.stderr
    assert len(pages()) == 1 and "rolled back" in pages()[0]


def test_without_a_baseline_the_rollback_gate_stays_strict_and_the_hold_stays(shell):
    run, pages, tmp = shell
    with open(tmp / ".env", "a") as f:
        f.write("MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\n")
    r = run(ROLLBACK, terms=PREPARE, extra=(_one_pass_clock(tmp, "c2"), function("verify_provider_rollback")))
    assert r.returncode == 1 and "ROLLED_BACK_OK" not in r.stdout and pages() == []
    assert r.stdout.count("shyft_runtime_gate_waiting") == 1
    assert r.stdout.count("shyft_runtime_gate_failed") == 1


def test_the_wiring_snapshots_before_the_trip_and_pages_before_the_clear():
    text = DEPLOY.read_text()
    assert text.index("  snapshot_shyft_baseline\n") < text.index('TRIP_SECOND="$(date +%S)"') \
        < text.index('step "entry_gate_trip"')
    rollback = function("rollback")
    assert rollback.index("clear_deploy_entry_gate") < rollback.index('page_shyft_preexisting "rolled back"')
    assert 'shyft_runtime_gate "$RELEASE" vendor' in function("provider_runtime_ready")
    assert 'shyft_runtime_gate "$OLD_RELEASE" all' in function("verify_provider_rollback")
    landed = text.index('page_shyft_preexisting "landed"')
    success = text.index("SUCCESS=1\n")
    assert text.index('clear_deploy_entry_gate || fail') < text.index('step "entry_gate_clear"') \
        < success < landed                               # after SUCCESS: a TERM mid-send never rolls back
    assert text.index('rm -f "$DEPLOY_INTENT" 2>/dev/null || true   # the deploy landed') < landed
    for reset in ("ROLLOVER_ENV_CHANGED=0", "LIFI_ENV_CHANGED=0", "PROVIDER_CHANGED=0"):
        assert text.index(reset, success) < landed       # nor restores the pre-deploy .env (Gamma MEDIUM)
    assert text.index("trap - EXIT", success) < landed
    assert "timeout 20" in function("page_shyft_preexisting")


# ---- a verified restored runner clears the intent even when the hold stays (Alpha 2026-10-16) ------

def _rollback_harness(tmp, *, runner_up, graph_ok):
    """rollback() alone, Shyft runtime, switched, through verify_provider_rollback with a fake verifier:
    the restored runner up or not (--rollback-only), the graph gate fresh-failing or not."""
    state = tmp / "state"; (state / "data").mkdir(parents=True)
    current, old = tmp / "current", tmp / "old"
    old.mkdir(); (old / "scripts").mkdir(); current.symlink_to(old)
    (old / "scripts/memecoin_watchdog.sh").write_text("exit 0\n")
    (state / ".env").write_text("MEMECOIN_SOLANA_INFRA_PROVIDER=shyft\n")
    intent, original, rh = state / "intent", state / "data/.provider-original.x", state / "data/.provider-rh.x"
    for path in (intent, original, rh):
        path.write_text("x")
    fake = tmp / "python"
    fake.write_text("#!/bin/bash\n"
                    "case \" $* \" in *' --rollback-only '*) exit %d;; *' --shyft-runtime-only'*) exit %d;; esac\n"
                    "exit 0\n" % (0 if runner_up else 1, 0 if graph_ok else 1))
    fake.chmod(0o755)
    text = DEPLOY.read_text()
    body = text[text.index("rollback() {", text.index("SWITCHED=0")):]
    body = body[:body.index("\n}\n") + 3]
    # rollback() reads `date +%s` once (PROVIDER_ROLLBACK_STARTED), then the deadline, then one loop pass.
    counter = tmp / "rb.count"
    clock = ('date() { if [ "${1:-}" != "+%%s" ]; then command date "$@"; return; fi; local n; '
             'n=$(( $(cat %s 2>/dev/null || echo 0) + 1 )); echo "$n" > %s; '
             'if [ "$n" -le 3 ]; then echo 100; else echo 200; fi; }' % (counter, counter))
    harness = f"""set -euo pipefail
SUCCESS=0; SWITCHED=1; PROVIDER_ENV=''; SHYFT_RUNTIME=1; PROVIDER_RECOVERY_REQUIRED=0; PROVIDER_CANDIDATE_LAUNCHED=0
SNIPER_DEPLOY_NO_PROCESS=0; OLD_RELEASE={old}; RELEASE={tmp / 'candidate'}; DEFAULT_SOURCE={tmp}; PY={fake}
CURRENT={current}; MANIFEST={state / 'manifest'}; OLD_MANIFEST={state / 'old-manifest'}; SHA=abc123
STATE_ROOT={state}; LAUNCH_VIA_CRON=0; ROLLOVER_OLD_RUNNER_STOPPED=1; PROVIDER_RH_STOPPED=0
CANDIDATE_MANIFEST={state / 'candidate-manifest'}; ROLLOVER_ENV_BACKUP=''; LIFI_ENV_ROLLBACK=''
DEPLOY_INTENT={intent}; PROVIDER_ORIGINAL_IDENTITIES={original}; PROVIDER_RH_RECORD={rh}; PROVIDER_BACKUP=''
SHYFT_BASELINE=''; SHYFT_PREEXISTING=''; SHYFT_PAGED=0; ROLLBACK_RUNNER_VERIFIED=0
restore_lifi_env() {{ :; }}
restore_rollover_env() {{ :; }}
restore_provider_env() {{ :; }}
source_mirror_rollback_safe() {{ :; }}
clear_deploy_entry_gate() {{ echo HOLD_CLEARED; }}
cleanup_provider_stage() {{ :; }}
pkill() {{ :; }}
{clock}
{function("step")}
{function("shyft_runtime_gate")}
{function("page_shyft_preexisting")}
{function("verify_provider_rollback")}
{body}
false || rollback
"""
    r = subprocess.run(["bash", "-c", harness], capture_output=True, text=True, timeout=20)
    return r, intent, original, rh


def test_a_verified_runner_removes_the_intent_and_records_even_when_the_hold_stays(tmp_path):
    r, intent, original, rh = _rollback_harness(tmp_path, runner_up=True, graph_ok=False)
    assert "HOLD_CLEARED" not in r.stdout and "entry hold" in r.stderr, r.stderr
    assert not intent.exists() and not original.exists() and not rh.exists()


def test_a_runner_that_never_verified_keeps_the_intent_and_records(tmp_path):
    r, intent, original, rh = _rollback_harness(tmp_path, runner_up=False, graph_ok=True)
    assert "HOLD_CLEARED" not in r.stdout
    assert intent.exists() and original.exists() and rh.exists()


def test_a_fully_verified_rollback_clears_the_hold_and_the_intent(tmp_path):
    r, intent, _original, _rh = _rollback_harness(tmp_path, runner_up=True, graph_ok=True)
    assert "HOLD_CLEARED" in r.stdout and not intent.exists(), r.stderr


# ---- the page is SENT, not queued, with the state root's env (Alpha 2026-10-16: 18:58Z never arrived) ----

def test_the_page_is_sent_synchronously_with_the_state_roots_env(shell):
    run, pages, _tmp = shell
    r = run('SHYFT_PREEXISTING=%s; page_shyft_preexisting landed; echo survived' % PREPARE)
    assert r.returncode == 0 and "survived" in r.stdout
    assert "page failed" not in r.stderr, r.stderr                 # delivered: the env file's token was used
    assert len(pages()) == 1 and PREPARE in pages()[0] and "memecoin deploy" in pages()[0]
    assert "send_telegram" in function("page_shyft_preexisting") and "send_alert" not in function("page_shyft_preexisting")
    assert (_tmp / "pages.audit").read_text().splitlines() == ["operational:deploy 1"]     # its audit row


def test_an_undelivered_page_prints_the_failure_line_and_never_changes_the_verdict(shell):
    run, pages, _tmp = shell
    r = run('SHYFT_PREEXISTING=%s; page_shyft_preexisting landed; echo survived' % PREPARE, tg_fail=True)
    assert r.returncode == 0 and "survived" in r.stdout
    assert "shyft preexisting page failed (verdict unchanged)" in r.stderr
    assert len(pages()) == 1                                       # called once, not retried
    assert (_tmp / "pages.audit").read_text().splitlines() == ["operational:deploy 0"]     # send_ok=0


def test_without_the_state_env_the_page_is_not_delivered(shell):
    run, pages, tmp = shell
    (tmp / ".env").write_text("")
    r = run('SHYFT_PREEXISTING=%s; page_shyft_preexisting landed' % PREPARE)
    assert r.returncode == 0 and "page failed" in r.stderr and len(pages()) == 1


def test_a_send_that_hangs_is_cut_at_the_timeout_and_fails_loudly(shell):
    """Gamma LOW-1: the page's one-shot is bounded; a hung send ends as an undelivered page."""
    import time as _time
    run, pages, tmp = shell
    (tmp / "sniper/services/memecoin_telegram.py").write_text(
        "import time\ndef send_telegram(text):\n    time.sleep(60)\n    return (True, 0.0)\n")
    body = ('SHYFT_PREEXISTING=%s; timeout() { command timeout 1 "${@:2}"; }; '
            'page_shyft_preexisting landed; echo survived' % PREPARE)
    started = _time.monotonic()
    r = run(body)
    assert r.returncode == 0 and "survived" in r.stdout and "page failed" in r.stderr
    assert _time.monotonic() - started < 15


def test_the_hard_off_switch_sends_nothing_writes_nothing_and_says_skipped(shell):
    """Alpha 2026-10-16: MEMECOIN_TG_DISPATCH_ENABLED=0 in the state env silences the deploy page."""
    run, pages, tmp = shell
    with open(tmp / ".env", "a") as f:
        f.write("MEMECOIN_TG_DISPATCH_ENABLED=0\n")
    r = run('SHYFT_PREEXISTING=%s; page_shyft_preexisting landed; echo survived' % PREPARE)
    assert r.returncode == 0 and "survived" in r.stdout
    assert "shyft preexisting page skipped: dispatch off (verdict unchanged)" in r.stderr
    assert "page failed" not in r.stderr
    assert pages() == [] and not (tmp / "pages.audit").exists()


@pytest.mark.parametrize("baseline", [False, True])
def test_provider_failure_names_terms_without_weakening_the_gate(shell, baseline):
    run, pages, tmp = shell
    setup = ""
    if baseline:
        (tmp / "data" / "b.json").write_text("[]")
        setup = 'SHYFT_BASELINE=%s; ' % shlex.quote(str(tmp / "data" / "b.json"))
    r = run(setup + 'shyft_runtime_gate "$RELEASE" vendor', terms=OBSERVER)
    assert r.returncode == 1 and pages() == []
    assert "shyft_runtime_gate_waiting" in r.stdout and OBSERVER in r.stdout
    assert "shyft_runtime_gate_failed" not in r.stdout


def test_repeated_identical_provider_failures_log_once(shell):
    run, _pages, _tmp = shell
    r = run('shyft_runtime_gate "$RELEASE" vendor || :; '
            'shyft_runtime_gate "$RELEASE" vendor || :', terms=OBSERVER)
    assert r.returncode == 0
    assert r.stdout.count("shyft_runtime_gate_waiting") == 1
    assert "shyft_runtime_gate_failed" not in r.stdout


@pytest.mark.parametrize("recovered", [False, True])
def test_only_exhausted_forward_gate_reports_failure(shell, recovered):
    run, _pages, _tmp = shell
    text = DEPLOY.read_text()
    terminal = text[text.index('  [ "$VERIFIED" -eq 1 ] || {'):text.index('  step "new_runner_verified"')]
    recovery = 'export TEST_TERMS=""; shyft_runtime_gate "$RELEASE" vendor; VERIFIED=1; ' if recovered else 'VERIFIED=0; '
    r = run('shyft_runtime_gate "$RELEASE" vendor || :; '
            'shyft_runtime_gate "$RELEASE" vendor || :; ' + recovery + terminal, terms=OBSERVER)
    assert r.returncode == (0 if recovered else 1)
    assert r.stdout.count("shyft_runtime_gate_waiting") == 1
    assert r.stdout.count("shyft_runtime_gate_failed") == (0 if recovered else 1)
    if not recovered:
        assert "scope=vendor terms=" + OBSERVER in r.stdout
