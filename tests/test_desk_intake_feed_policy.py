"""Frozen feed enrollment, including direct callers that bypass the emitter."""
import copy

import pytest

from scripts import memecoin_desk_provision_cohort as P
from sniper.persistence import db_memecoin_desk as D
from sniper.services import memecoin_desk as desk
from sniper.services import memecoin_desk_contract as K
from test_memecoin_desk_contract import AT, BOUNDS
from test_memecoin_desk_intake_filter import RULE, decide, intake_candidate, pol
from test_memecoin_desk_intake_assembler_runner import MintRpc, run
from test_memecoin_desk_pipeline import POLICY, databases


FEEDS = ["dexscreener", "fomo", "pumpfun"]


def frozen(feeds=None):
    rule = copy.deepcopy(RULE)
    if feeds is not None:
        rule["feeds"] = feeds
    return P.frozen_policy(judge_p_admit=1.0, round_trip_loss_pct_max=8, hook_allowlist=[],
                           rail_bounds=BOUNDS, venue="pumpswap", intake=rule)


@pytest.mark.parametrize("feed", ["fomo", "dexscreener"])
def test_legacy_cohort_cannot_admit_an_unenrolled_feed(feed):
    assert decide(intake_candidate(feed=feed))["reason"] == "kill:intake_feed_not_enrolled"


@pytest.mark.parametrize("feed", FEEDS)
def test_new_three_feed_policy_passes_each_feed_without_changing_safety(feed):
    assert decide(intake_candidate(feed=feed), p=pol(feeds=FEEDS))["reason"] == "pass"
    assert decide(intake_candidate(feed=feed, quote_sol=39), p=pol(feeds=FEEDS))["reason"] == "kill:liq_min"


@pytest.mark.parametrize("feeds", [[], ["fomo", "fomo"], ["gmgn"], "fomo", None, [1],
                                   ["pumpfun", "fomo"]])
def test_malformed_frozen_enrollment_refuses_before_either_registry_write(databases, feeds):
    policy = {**POLICY, "venue": "pumpswap", "hard_filter": pol(feeds=feeds)}
    with pytest.raises(ValueError, match="intake"):
        desk.provision_cohort(*databases, producer="U", policy=policy,
                              prompt_version="desk-judge-v26", created_at=AT)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_cohorts").fetchone()[0] == 0
    assert databases[1].execute("SELECT COUNT(*) FROM memecoin_desk_cohorts").fetchone()[0] == 0


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("feed", ["fomo", "dexscreener"])
def test_unenrolled_feed_cannot_write_an_emission_or_ticket(databases, direct, feed):
    cid = desk.provision_cohort(*databases, producer="U", policy={**POLICY, "venue": "pumpswap",
                               "hard_filter": pol()}, prompt_version="desk-judge-v26", created_at=AT)
    c = K.validate_candidate(intake_candidate(feed=feed))
    ingest = D.record_candidate if direct else desk.ingest_candidate
    with pytest.raises(ValueError, match="intake_feed_not_enrolled"):
        ingest(databases[0], c, cohort_id=cid, now_iso=AT, cooldown_s=900)
    assert databases[0].execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 0
    assert databases[0].execute("SELECT COUNT(*) FROM desk_ticket_heads").fetchone()[0] == 0


def test_new_policy_canonicalizes_feed_order_and_changes_identity():
    old = frozen()
    first, second = frozen(["pumpfun", "fomo", "dexscreener"]), frozen(FEEDS)
    assert first["hard_filter"]["intake"]["feeds"] == FEEDS
    assert first == second and P.cohort_id(first) == P.cohort_id(second)
    assert "feeds" not in old["hard_filter"]["intake"]
    assert P.cohort_id(old) != P.cohort_id(first)
    assert K.intake_feeds(old["hard_filter"]) == ("pumpfun",)
    assert K.intake_feeds(first["hard_filter"]) == tuple(FEEDS)


def test_feed_cli_requires_intake_and_does_not_write(tmp_path):
    with pytest.raises(SystemExit) as exc:
        P.main(["--judge-p-admit", "1", "--round-trip-loss-pct-max", "8", "--intake-feeds", "fomo",
                "--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db")])
    assert exc.value.code == 2 and not list(tmp_path.iterdir())


def test_three_feed_cli_preview_freezes_sources_and_writes_no_databases(tmp_path, monkeypatch, capsys):
    import json
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_DESK_RAIL_BOUNDS", "tp_pct:15-150,sl_pct:10-40,trail_pct:8-30")
    args = ["--judge-p-admit", "1", "--round-trip-loss-pct-max", "8", "--intake", "--venue", "pumpswap",
            "--intake-feeds", "pumpfun", "fomo", "dexscreener", "--playbook",
            str(P.ROOT / "docs/superpowers/specs/2026-10-27-desk-judge-playbook-v26.md"), "--intake-prereg",
            "docs/audits/2026-10-27-intake-desk-cohort-preregistration.md@84f35a5a496009116484937579f3dbce706bf126",
            "--desk-db", str(tmp_path / "desk.db"), "--ledger-db", str(tmp_path / "ledger.db")]
    assert P.main(args) == 0
    text = capsys.readouterr().out
    output = json.loads(text.split("\nMEMECOIN_DESK_U_COHORT_ID=", 1)[0])
    assert output["policy"]["hard_filter"]["intake"]["feeds"] == FEEDS
    assert output["policy"]["judge_calls_per_hour"] == 40
    assert not (tmp_path / "desk.db").exists() and not (tmp_path / "ledger.db").exists()


def test_assembler_rejects_unenrolled_feed_before_rpc_or_ticket(run):
    import json
    c = run["cand"]
    c["intake"]["feed"] = "fomo"
    run["path"].write_text(json.dumps(c) + "\n")
    rpc = MintRpc()
    assert run["invoke"](rpc=rpc) == 0
    assert rpc.calls == 0
    with D.connect(run["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM desk_candidate_emissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM desk_ticket_heads").fetchone()[0] == 0
    log = (run["desk"] / "assembler_intake" / (run["path"].stem + ".jsonl")).read_text()
    assert "intake_feed_not_enrolled" in log
