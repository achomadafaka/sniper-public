"""Relay option A section 5: the label-only pre-gate (spec 2026-10-09, Alpha option ii). It names a
refusal only when the hook's own mcap ceiling or source-buy floor would make it, from the hook's own
pure pieces, and is silent whenever it is not certain. The agreement check replays recorded attempts."""
import json

import pytest

from sniper.services import memecoin_relay_pregate as pg

UID = "u-1"


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    from sniper.settings import memecoin as cfg
    for name, value in dict(MEMECOIN_REAL_MAX_MCAP_USD=250_000.0, MEMECOIN_REAL_MIN_MCAP_USD=0.0,
                            MEMECOIN_REAL_MAX_MCAP_USD_BY_WALLET={}, MEMECOIN_REAL_MIN_MCAP_USD_BY_WALLET={},
                            MEMECOIN_AUTO_TEMPLATES="off", MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO=0.008,
                            MEMECOIN_REAL_MIN_SOURCE_BUY_UNKNOWN_MCAP_USD=0.0,
                            MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND={},
                            MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT=0.0,
                            MEMECOIN_REAL_MAX_SOURCE_BUY_USD=0.0).items():
        monkeypatch.setattr(cfg, name, value, raising=False)
    monkeypatch.setattr(cfg, "min_source_buy_usd_for", lambda chain, uid=None, env=None: 0.0)


def _decide(**over):
    kw = dict(chain="4663", source_user_id=UID, mcap=4_300_000.0, mcap_basis="history", their_usd=900.0,
              prior_net_usd=0.0, armed=True)
    kw.update(over)
    return pg.decision(**kw)


def test_abyss_over_the_ceiling_is_named_in_the_hooks_own_words():
    assert _decide() == ("over_ceiling", "mcap $4,300,000 over ceiling $250,000 (over_ceiling)")


@pytest.mark.parametrize("over, why", [
    (dict(armed=False), "not_armed"),
    (dict(mcap=None), "mcap_uncertain"),
    (dict(mcap_basis="dexscreener"), "mcap_uncertain"),
    (dict(mcap_basis="event"), "mcap_uncertain"),
    (dict(mcap_basis=None), "mcap_uncertain"),
])
def test_it_is_silent_when_it_cannot_be_certain(over, why):
    assert _decide(**over) == (None, why)


def test_auto_templates_or_a_lift_that_admits_keep_it_silent(monkeypatch):
    from sniper.settings import memecoin as cfg
    # The lift admits it past the ceiling, so the hook goes on to the floor, and so does the label.
    assert _decide(lift_fn=lambda mc, ceiling: 10_000_000.0, their_usd=40_000.0) == (None, "passes")
    assert _decide(lift_fn=lambda mc, ceiling: 10_000_000.0)[0] == "source_buy_floor"
    assert _decide(lift_fn=lambda mc, ceiling: None)[0] == "over_ceiling"     # absent never lifts
    monkeypatch.setattr(cfg, "MEMECOIN_AUTO_TEMPLATES", "on")
    monkeypatch.setattr(cfg, "MEMECOIN_SCALP_MCAP_USD", 1_000_000.0, raising=False)
    assert _decide() == (None, "auto_templates")


def test_an_under_floor_mcap_is_the_hooks_call_not_a_label(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_MCAP_USD", 50_000.0)
    assert _decide(mcap=20_000.0) == (None, "mcap_under_floor")


def test_the_source_buy_floor_label_is_the_hooks_family_reason():
    cls, text = _decide(mcap=150_000.0, their_usd=900.0)        # 0.6% under the 0.8% ratio floor
    assert cls == "source_buy_floor" and text.startswith("source buy $900") and "under mcap floor 0.800%" in text
    assert _decide(mcap=150_000.0, their_usd=1_300.0) == (None, "passes")
    # The trader's earlier net counts, exactly as the hook judges it.
    assert _decide(mcap=150_000.0, their_usd=900.0, prior_net_usd=400.0) == (None, "passes")


@pytest.mark.parametrize("over, why", [(dict(exempt=frozenset({"source_buy_floor"})), "floor_exempt"),
                                       (dict(their_usd=None), "size_unknown")])
def test_an_exempt_wallet_or_an_unknown_size_gets_no_floor_label(over, why):
    assert _decide(mcap=150_000.0, **over) == (None, why)


@pytest.mark.parametrize("reason, cls", [
    ("mcap $4,300,000 over ceiling $250,000 (over_ceiling)", "over_ceiling"),
    ("source buy $373 (0.474%) under mcap floor 0.800% ($630)", "source_buy_floor"),
    ("source buy $100 under absolute floor $200", "source_buy_floor"),
    ("coin ran 9.8x above first-seen price (cap 3.0x)", None), (None, None)])
def test_the_hooks_reason_is_classed(reason, cls):
    assert pg.hook_class(reason) == cls


# --- the agreement check (condition 1) -------------------------------------------------------------

def _record(status, reason, *, mcap=4_300_000.0, basis="history", size=900.0, aid="a1"):
    trace = json.dumps({"substage_events": [{"name": "source_buy_floor", "source_size_usd": size,
                                             "prior_net_usd": 0.0, "configured_mcap_ratio": 0.008}]})
    return (aid, "4663", UID, status, reason, mcap, basis, None, trace)


def test_the_agreement_check_passes_when_every_label_matches_the_hook():
    from scripts.research.relay_pregate_agreement import agreement
    report = agreement([_record("refused", "mcap $4,300,000 over ceiling $250,000 (over_ceiling)"),
                        _record("filled", "filled", mcap=100_000.0, size=1_000.0, aid="a2"),
                        _record("refused", "x", basis="dexscreener", aid="a3")], pg.decision)
    assert report["failures"] == []
    assert report["counts"]["agreed:over_ceiling"] == 1 and report["counts"]["silent:mcap_uncertain"] == 1


def test_the_agreement_check_fails_on_a_labelled_admitted_attempt_and_on_a_wrong_class():
    from scripts.research.relay_pregate_agreement import agreement
    report = agreement([_record("filled", "filled"),
                        _record("refused", "coin ran 9.8x above first-seen price (cap 3.0x)", aid="a2")],
                       pg.decision)
    assert [(f["attempt_id"], f["hook"]) for f in report["failures"]] == [("a1", "admitted"), ("a2", None)]


def test_a_pregate_that_resolves_the_ratio_wrong_fails_the_check_never_hides_as_drift(monkeypatch):
    """The drift bar is resolved by the check with the hook's functions, not read from the pre-gate."""
    from scripts.research.relay_pregate_agreement import agreement
    from sniper.settings import memecoin as cfg

    def wrong(**kw):                       # a mutant that skips the band ratio (5 real failures)
        monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {})
        try:
            return pg.decision(**kw)
        finally:
            monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {"micro": 0.005})
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO_BY_BAND", {"micro": 0.005})
    rec = _record("filled", "filled", mcap=150_000.0, size=900.0)
    trace = json.loads(rec[-1])
    trace["substage_events"][0]["configured_mcap_ratio"] = 0.005        # what the hook judged then
    rec = rec[:-1] + (json.dumps(trace),)
    assert agreement([rec], pg.decision)["failures"] == []               # the real one passes (0.6% > 0.5%)
    report = agreement([rec], wrong)
    assert [f["hook"] for f in report["failures"]] == ["admitted"] and report["config_drift"] == []


# --- the watcher: a label publishes at once; the proofs still record (condition 2) ------------------

class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=()):
        self.log.append((sql, params))
        return self

    def commit(self):
        pass

    def close(self):
        pass


def _watch(monkeypatch, *, label, verdict, mode="on"):
    import threading
    from scripts.research import onchain_evm_wallet_watch as watch
    from sniper.services import memecoin_relay_initiation as relay
    from sniper.settings import memecoin as cfg
    import test_memecoin_relay_initiation as R
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_PREGATE", mode)
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_DEPOSIT_PROOF_CHAINS", frozenset())
    commits, sql, done = [], [], threading.Event()
    monkeypatch.setattr(watch, "get_connection", lambda *_a: _Conn(sql))
    monkeypatch.setattr(watch, "get_readonly_connection", lambda *_a: _Conn([]))
    monkeypatch.setattr(watch, "_commit_detections", lambda conn, rows: commits.extend(rows))
    monkeypatch.setattr(watch, "_say", lambda line: (line.find("late_after_pregate") >= 0 or
                                                    "pregate" not in line) and done.set())
    monkeypatch.setattr(pg, "for_row", lambda conn, row, chain, now_iso, **kw: label)

    def verifier(item):
        if isinstance(verdict, Exception):
            raise verdict
        return dict(item, row=dict(item["row"], buy=dict(item["row"]["buy"], source_initiation=dict(
            item["row"]["buy"]["source_initiation"], proof_version=verdict))))
    monkeypatch.setattr(relay, "WatcherVerifier", lambda *a, **k: verifier)
    worker = watch._relay_origin_worker(None)
    _req, _origin, proof, transaction = R.inputs()
    row = dict(tx_hash=proof["tx_hash"], source_user_id=R.UID, buy={"source_initiation": proof, "mint": proof["mint"]},
               valuation={})
    return watch, worker, row, transaction, commits, sql, done


@pytest.mark.parametrize("verdict, late", [("relay-origin-bound-v1", None),
                                           ("x", "relay_request_not_indexed")])
def test_a_label_publishes_the_row_at_once_and_the_proof_still_records(monkeypatch, verdict, late):
    from sniper.services import memecoin_relay_initiation as relay
    label = ("over_ceiling", "mcap $4,300,000 over ceiling $250,000 (over_ceiling)",
             {"mcap": 4_300_000.0, "mcap_basis": "history"})
    watch, worker, row, transaction, commits, sql, done = _watch(
        monkeypatch, label=label, verdict=relay.ProofError(late) if late else verdict)
    try:
        assert watch._defer_relay_origin(worker, row, {"_transaction": transaction}, 600.0, 5.0)
        assert done.wait(2)
        assert len(commits) == 1                                        # one row, published once
        val = commits[0]["valuation"]
        assert val["relay_origin_error"] == "pregate:" + label[1] and val["relay_pregate"]["mcap"] == 4_300_000.0
        updates = [(q, p) for q, p in sql if q.startswith("UPDATE onchain_wallet_detections")]
        assert len(updates) == 1                                        # the late proof, recorded
        recorded = json.loads(updates[0][1][0])
        assert recorded["error"] == late
        assert recorded["proof_version"] == (None if late else verdict)
    finally:
        worker.close()


def test_with_the_knob_off_no_label_is_asked(monkeypatch):
    from sniper.settings import memecoin as cfg
    asked = []
    watch, worker, row, transaction, commits, sql, done = _watch(
        monkeypatch, label=("over_ceiling", "x", {}), verdict="relay-origin-bound-v1")
    monkeypatch.setattr(cfg, "MEMECOIN_RELAY_PREGATE", "off")
    monkeypatch.setattr(pg, "for_row", lambda *a, **k: asked.append(1))
    try:
        assert watch._defer_relay_origin(worker, row, {"_transaction": transaction}, 600.0, 5.0)
        assert done.wait(2)
        assert asked == [] and len(commits) == 1 and commits[0]["valuation"].get("relay_origin_error") is None
    finally:
        worker.close()


# --- Gamma M2: judge with the RUNNER's config ---------------------------------------------------------

def _ledger(tmp_path, *, override=None):
    import sqlite3
    from sniper.persistence import db_memecoin as db
    conn = sqlite3.connect(str(tmp_path / "l.db"))
    conn.row_factory = sqlite3.Row
    db.ensure_memecoin_tables(conn)
    db.add_wallet(conn, UID, UID)
    db.set_wallet_real_approved(conn, UID, True, chains=["4663"], force=True)
    if override is not None:
        conn.execute("INSERT INTO memecoin_entry_knob_overrides (name, value, active) VALUES (?,?,1)",
                     ("MEMECOIN_REAL_MIN_SOURCE_BUY_USD", json.dumps(override)))
    conn.commit()
    return conn


def _floor_row(size):
    return {"tx_hash": "0x" + "ab" * 32, "source_user_id": UID,
            "buy": {"source_initiation": {"mint": "0x" + "3" * 40, "tx_hash": "0x" + "ab" * 32},
                    "source_price": 0.00002, "source_size_usd": size}}


@pytest.fixture
def _floor_config(monkeypatch):
    """Gamma's live case: .env floor 200 with the 2% tolerance, an override of 197 in the runner."""
    from sniper.services import memecoin_entry_knobs as ek
    from sniper.services import memecoin_real_runner as rr
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 200.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_BUY_FLOOR_TOLERANCE_PCT", 2.0)
    monkeypatch.setattr(cfg, "min_source_buy_usd_for", lambda chain, uid=None, env=None: 200.0)
    monkeypatch.setattr(cfg, "source_buy_floor_override", lambda chain, uid, env=None: None)
    monkeypatch.setattr(ek, "_DEFAULTS", {})
    monkeypatch.setattr(rr, "_cached_evm_source_mcap", lambda chain, mint, price, trace=None: 20_000.0)


@pytest.mark.parametrize("size", [193.5, 194.5, 195.5])
def test_an_active_override_is_the_bar_not_this_processes_env(tmp_path, _floor_config, size):
    assert pg.for_row(_ledger(tmp_path), _floor_row(size), chain="4663", now_iso="2026-10-09T00:00:00+00:00")[0] \
        == "source_buy_floor"                                                 # 200 x 0.98 = 196
    (tmp_path / "o").mkdir()
    cls, why, inputs = pg.for_row(_ledger(tmp_path / "o", override=197), _floor_row(size), chain="4663",
                                  now_iso="2026-10-09T00:00:00+00:00")
    assert (cls, why) == (None, "passes") and inputs["overrides"] == ["MEMECOIN_REAL_MIN_SOURCE_BUY_USD"]


def test_the_override_is_read_never_applied(tmp_path, _floor_config):
    from sniper.settings import memecoin as cfg
    pg.for_row(_ledger(tmp_path, override=197), _floor_row(194.5), chain="4663",
               now_iso="2026-10-09T00:00:00+00:00")
    assert cfg.MEMECOIN_REAL_MIN_SOURCE_BUY_USD == 200.0          # the settings module is untouched


def test_an_env_newer_than_the_process_is_silent(tmp_path, _floor_config):
    import os
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    started = os.path.getmtime(env) - 5
    got = pg.for_row(_ledger(tmp_path), _floor_row(100.0), chain="4663", now_iso="2026-10-09T00:00:00+00:00",
                     env_paths=(env, tmp_path / "missing.env"), started_at=started)
    assert got[:2] == (None, "config_stale")
    fresh = pg.for_row(_ledger(tmp_path), _floor_row(100.0), chain="4663", now_iso="2026-10-09T00:00:00+00:00",
                       env_paths=(env,), started_at=os.path.getmtime(env) + 5)
    assert fresh[0] == "source_buy_floor"


def test_in_shadow_the_label_rides_on_the_row_and_nothing_is_published_early(monkeypatch):
    label = ("over_ceiling", "mcap $4,300,000 over ceiling $250,000 (over_ceiling)", {"mcap": 4_300_000.0})
    watch, worker, row, transaction, commits, sql, done = _watch(
        monkeypatch, label=label, verdict="relay-origin-bound-v1", mode="shadow")
    try:
        assert watch._defer_relay_origin(worker, row, {"_transaction": transaction}, 600.0, 5.0)
        assert done.wait(2)
        assert len(commits) == 1 and not [q for q, _p in sql if q.startswith("UPDATE")]
        val = commits[0]["valuation"]
        assert val.get("relay_origin_error") is None                         # the row waited as today
        assert val["relay_pregate"]["mode"] == "shadow" and val["relay_pregate"]["cls"] == "over_ceiling"
        assert commits[0]["buy"]["source_initiation"]["proof_version"] == "relay-origin-bound-v1"
    finally:
        worker.close()


@pytest.mark.parametrize("raw, mode", [("", "off"), ("0", "off"), ("shadow", "shadow"), ("1", "on"), ("on", "on"),
                                       ("nonsense", "off")])
def test_the_mode_knob(raw, mode):
    from sniper.settings.memecoin import _relay_pregate_mode
    assert _relay_pregate_mode(raw) == mode


def test_the_live_grade_counts_only_proved_rows_and_fails_on_a_label_the_runner_admitted():
    from scripts.research.relay_pregate_agreement import grade_live
    rows = [  # (pregate json, proof_version on the row, runner terminal status, runner terminal reason)
        ({"mode": "shadow", "cls": "over_ceiling"}, "relay-origin-bound-v1", "refused",
         "mcap $4,300,000 over ceiling $250,000 (over_ceiling)"),
        ({"mode": "shadow", "cls": "source_buy_floor"}, "relay-deposit-processed-v1", "filled", "filled"),
        ({"mode": "shadow", "cls": "over_ceiling"}, "relay-envelope-other-target-v1", "refused",
         "relay_origin_not_indexed"),
        ({"mode": "shadow", "cls": None, "text": "mcap_uncertain"}, "relay-origin-bound-v1", "filled", "filled")]
    report = grade_live(rows)
    assert report["counts"]["graded"] == 2 and report["counts"]["unproved"] == 1
    assert report["counts"]["agreed:over_ceiling"] == 1
    assert [f["runner"] for f in report["label_refuses_admitted"]] == ["admitted"]
    assert report["ready_for_on"] is False


def test_a_quiet_shadow_reports_its_stale_share_and_is_never_ready():
    """Alpha: operator .env edits silence the pre-gate (config_stale) until the next watcher restart."""
    from scripts.research.relay_pregate_agreement import grade_live
    stale = [({"mode": "shadow", "cls": None, "text": "config_stale"}, "relay-origin-bound-v1", "filled", "filled")] * 40
    one = [({"mode": "shadow", "cls": "over_ceiling"}, "relay-origin-bound-v1", "refused",
            "mcap $4,300,000 over ceiling $250,000 (over_ceiling)")]
    report = grade_live(stale + one)
    assert (report["rows"], report["graded"], report["config_stale"]) == (41, 1, 40)
    assert report["config_stale_share"] == pytest.approx(40 / 41)
    assert report["label_refuses_admitted"] == [] and report["ready_for_on"] is False


# --- Gamma L4: the env must be no newer than the RUNNER's start either -------------------------------

def _heartbeat(tmp_path, started_iso):
    path = tmp_path / "runner.heartbeat"
    path.write_text(json.dumps({"ts": 1.0} if started_iso is None else {"ts": 1.0, "started_at": started_iso}))
    return path


def test_an_env_edit_the_runner_has_not_restarted_on_is_silent(tmp_path, _floor_config):
    import os
    from datetime import datetime, timezone
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    mtime = os.path.getmtime(env)
    old_runner = datetime.fromtimestamp(mtime - 60, timezone.utc).isoformat()
    new_runner = datetime.fromtimestamp(mtime + 60, timezone.utc).isoformat()
    kw = dict(chain="4663", now_iso="2026-10-09T00:00:00+00:00", env_paths=(env,), started_at=mtime + 5)
    row = _floor_row(100.0)
    # The watcher restarted after the edit; the runner did not: silent, never the new env's label.
    assert pg.for_row(_ledger(tmp_path), row, runner_heartbeat=_heartbeat(tmp_path, old_runner), **kw)[:2] \
        == (None, "config_stale_runner")
    assert pg.for_row(_ledger(tmp_path), row, runner_heartbeat=_heartbeat(tmp_path, new_runner), **kw)[0] \
        == "source_buy_floor"


@pytest.mark.parametrize("beat", ["missing", "no_started_at", "garbage"])
def test_an_unreadable_runner_start_is_silent(tmp_path, _floor_config, beat):
    import os
    env = tmp_path / ".env"
    env.write_text("X=1\n")
    path = tmp_path / "runner.heartbeat"
    if beat == "no_started_at":
        path = _heartbeat(tmp_path, None)
    elif beat == "garbage":
        path.write_text("{not json")
    got = pg.for_row(_ledger(tmp_path), _floor_row(100.0), chain="4663", now_iso="2026-10-09T00:00:00+00:00",
                     env_paths=(env,), started_at=os.path.getmtime(env) + 5, runner_heartbeat=path)
    assert got[:2] == (None, "runner_start_unknown")


def test_the_live_grade_reports_silences_by_reason():
    from scripts.research.relay_pregate_agreement import grade_live
    rows = ([({"cls": None, "text": "config_stale_runner"}, "relay-origin-bound-v1", "filled", "filled")] * 3
            + [({"cls": None, "text": "mcap_uncertain"}, "relay-origin-bound-v1", "filled", "filled")] * 2)
    report = grade_live(rows)
    assert report["silences"] == {"config_stale_runner": 3, "mcap_uncertain": 2} and report["config_stale"] == 3
