"""Phase 3 of spec 2026-07-30: funded arming is per chain and fail-closed.

Reverses the 2026-07-15 global-arming decision. `memecoin_wallets.real_chains`
is the authority list; NULL means armed nowhere and is never read as "all".
"""
import sqlite3

import sniper.persistence.db_memecoin as m
from _web_csrf import csrf_client


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _armed(c, uid="u1", chains=None):
    m.add_wallet(c, uid, "h-" + uid)
    m.set_wallet_real_approved(c, uid, True, force=True)
    if chains is not None:
        m.set_wallet_real_chains(c, uid, chains)
    return c


def test_null_real_chains_is_armed_nowhere():
    """NULL must never be read as 'all chains' - that is the pooled behaviour being removed."""
    c = _armed(_conn())
    for nid in ("solana", "4663", "8453", "56", "1", "evm-unknown"):
        assert m.real_approved_user_ids(c, network_id=nid) == set(), f"NULL armed on {nid}"


def test_wallet_is_armed_only_on_its_listed_chains():
    c = _armed(_conn(), chains=["solana"])
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    assert m.real_approved_user_ids(c, network_id="4663") == set()
    assert m.real_approved_user_ids(c, network_id="8453") == set()


def test_missing_chain_fails_closed():
    """An authority caller that cannot name a chain gets nobody, not everybody."""
    c = _armed(_conn(), chains=["solana", "4663"])
    assert m.real_approved_user_ids(c) == set()
    assert m.real_approved_user_ids(c, network_id=None) == set()
    assert m.real_approved_user_ids(c, network_id="") == set()


def test_paused_and_excluded_still_override_chain_arming():
    c = _armed(_conn(), chains=["solana"])
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    c.execute("UPDATE memecoin_wallets SET paused=0, excluded=1 WHERE source_user_id='u1'")
    c.commit()
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_combined_roster_hides_legacy_configured_chain_for_a_paused_wallet():
    c = _armed(_conn(), chains=["robin"])
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()

    row = next(r for r in m.list_curated_with_stats(c)
               if r["source_user_id"] == "u1")

    assert row["configured_chains"] == []
    assert row["armed_chains"] == []


def test_arming_a_paused_configured_wallet_resumes_it():
    c = _armed(_conn(), chains=["robin"])
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()

    result = m.set_wallet_chain_armed(c, "u1", "robin", True, force=True)
    row = c.execute(
        "SELECT paused FROM memecoin_wallets WHERE source_user_id='u1'"
    ).fetchone()

    assert result["ok"] is True
    assert row["paused"] == 0
    assert m.real_approved_user_ids(c, network_id="4663") == {"u1"}


def test_identity_free_wallet_cannot_gain_funded_authority_even_when_forced():
    c = _conn()
    m.add_wallet(c, "unknown", "")

    result = m.set_wallet_chain_armed(c, "unknown", "solana", True, force=True)

    assert result["ok"] is False
    assert "identified source" in result["reason"]
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_pause_clears_all_funded_authority():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)

    m.set_wallet_paused(c, "u1", True)
    row = c.execute(
        "SELECT paused,real_approved,real_chains FROM memecoin_wallets "
        "WHERE source_user_id='u1'"
    ).fetchone()

    assert dict(row) == {"paused": 1, "real_approved": 0, "real_chains": None}


def test_unpause_restores_watch_only_not_funded_authority():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
    m.set_wallet_paused(c, "u1", True)

    m.set_wallet_paused(c, "u1", False)
    row = c.execute(
        "SELECT paused,real_approved,real_chains FROM memecoin_wallets "
        "WHERE source_user_id='u1'"
    ).fetchone()

    assert dict(row) == {"paused": 0, "real_approved": 0, "real_chains": None}


def test_excluded_wallet_cannot_be_armed_before_reobserve():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_excluded(c, "u1", True)

    result = m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)

    assert result["ok"] is False
    assert result["action"] == "excluded_requires_reobserve"
    assert m.wallet_armed_chains(c, "u1") == []


def test_final_disarm_returns_a_legacy_paused_row_to_watch_only():
    c = _armed(_conn(), chains=["solana"])
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()

    m.set_wallet_chain_armed(c, "u1", "solana", False)
    row = c.execute(
        "SELECT paused,real_approved,real_chains FROM memecoin_wallets "
        "WHERE source_user_id='u1'"
    ).fetchone()

    assert dict(row) == {"paused": 0, "real_approved": 0, "real_chains": None}


def test_revoking_real_approved_disarms_every_chain():
    c = _armed(_conn(), chains=["solana", "4663"])
    m.set_wallet_real_approved(c, "u1", False)
    assert m.real_approved_user_ids(c, network_id="solana") == set()
    assert m.real_approved_user_ids(c, network_id="4663") == set()


def test_wallet_armed_chains_roundtrip():
    c = _armed(_conn(), chains=["solana", "4663"])
    assert m.wallet_armed_chains(c, "u1") == ["4663", "solana"]
    m.set_wallet_real_chains(c, "u1", [])
    assert m.wallet_armed_chains(c, "u1") == []
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_direct_chain_list_requires_the_approval_flag():
    c = _conn()
    m.add_wallet(c, "u1", "h1")

    result = m.set_wallet_real_chains(c, "u1", ["solana"])

    assert result["ok"] is False
    assert result["action"] == "approval_required"
    assert m.wallet_armed_chains(c, "u1") == []


def test_direct_empty_chain_list_returns_watch_only():
    c = _armed(_conn(), chains=["solana"])

    result = m.set_wallet_real_chains(c, "u1", [])
    row = c.execute(
        "SELECT paused,real_approved,real_chains FROM memecoin_wallets "
        "WHERE source_user_id='u1'"
    ).fetchone()

    assert result["ok"] is True
    assert dict(row) == {"paused": 0, "real_approved": 0, "real_chains": None}


def test_chain_matching_is_case_and_whitespace_insensitive():
    c = _armed(_conn(), chains=[" Solana ", "4663"])
    assert m.wallet_armed_chains(c, "u1") == ["4663", "solana"]
    assert m.real_approved_user_ids(c, network_id="SOLANA") == {"u1"}


def test_entry_gate_asks_authority_for_the_event_chain():
    """The funded entry gate must pass the event's chain, not ask the global set."""
    import inspect

    import sniper.services.memecoin_real_runner as rr

    src = inspect.getsource(rr)
    assert "real_approved_user_ids(conn, network_id=chain)" in src, \
        "funded entry gate must scope authority to the event's chain"
    assert "real_approved_user_ids(conn)\n" not in src, \
        "no chainless authority read may remain on the funded path"


def test_a_wallet_armed_elsewhere_is_refused_on_this_chain():
    c = _armed(_conn(), chains=["solana"])
    assert "u1" not in m.real_approved_user_ids(c, network_id="4663")
    assert "u1" in m.real_approved_user_ids(c, network_id="solana")


# ── Audit remediation 2026-07-30: canonical identity, atomicity, per-chain cohorts ──

def test_ui_chain_keys_canonicalize_to_funded_network_ids():
    """Storing 'robin' must arm the chain the funded runner calls '4663'.

    The UI speaks robin/base; funded events carry 4663/8453. Storing the UI spelling left
    real_approved_user_ids('4663') empty, so a wallet that rendered as armed could never
    execute.
    """
    c = _armed(_conn(), chains=["robin", "base"])
    assert m.wallet_armed_chains(c, "u1") == ["4663", "8453"]
    assert m.real_approved_user_ids(c, network_id="4663") == {"u1"}
    assert m.real_approved_user_ids(c, network_id="8453") == {"u1"}
    # …and the UI spelling still resolves on read.
    assert m.real_approved_user_ids(c, network_id="robin") == {"u1"}


def test_arming_one_chain_does_not_clear_the_others():
    """Reproduces the audit finding: arm robin, then solana, and robin survived."""
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "robin", True, force=True)
    assert m.wallet_armed_chains(c, "u1") == ["4663"]
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
    assert m.wallet_armed_chains(c, "u1") == ["4663", "solana"]
    m.set_wallet_chain_armed(c, "u1", "4663", False)
    assert m.wallet_armed_chains(c, "u1") == ["solana"]
    assert m.real_approved_user_ids(c, network_id="4663") == set()
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}


def test_disarming_the_last_chain_clears_the_approval_flag():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
    m.set_wallet_chain_armed(c, "u1", "solana", False)
    assert m.wallet_armed_chains(c, "u1") == []
    assert c.execute("SELECT real_approved FROM memecoin_wallets "
                     "WHERE source_user_id='u1'").fetchone()[0] == 0


def test_chain_arm_is_one_transaction():
    """A failure mid-write must leave NO half-armed state.

    real_approved=1 with real_chains=NULL is read as 'armed nowhere' by current code but as
    'armed everywhere' by the pinned funded release, so the half-state is dangerous.
    """
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()
    import sniper.persistence.db_memecoin as mod
    original = mod._audit_approval

    def boom(*a, **k):
        raise RuntimeError("audit write failed")

    mod._audit_approval = boom
    try:
        try:
            m.set_wallet_chain_armed(c, "u1", "solana", True, force=True)
        except RuntimeError:
            pass
    finally:
        mod._audit_approval = original
    row = c.execute("SELECT real_approved, real_chains, paused FROM memecoin_wallets "
                    "WHERE source_user_id='u1'").fetchone()
    assert row["real_approved"] == 0 and not row["real_chains"], \
        "a failed arm left a half-armed row"
    assert row["paused"] == 1, "a failed arm cleared pause"


def test_audit_row_records_which_chain():
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "robin", True, force=True)
    row = c.execute("SELECT action, chain FROM memecoin_wallet_approvals "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["action"] == "chain_arm" and row["chain"] == "4663"


def test_cohort_start_is_per_chain():
    """Re-arming 4663 after a 4663 demotion must start a NEW 4663 cohort.

    With a global cohort the old 4663 losses kept counting and the guardrail re-fired on the
    very next close.
    """
    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "solana", True, force=True,
                             now_iso="2026-07-01T00:00:00+00:00")
    m.set_wallet_chain_armed(c, "u1", "robin", True, force=True,
                             now_iso="2026-07-02T00:00:00+00:00")
    m.set_wallet_chain_armed(c, "u1", "robin", False, now_iso="2026-07-03T00:00:00+00:00")
    m.set_wallet_chain_armed(c, "u1", "robin", True, force=True,
                             now_iso="2026-07-04T00:00:00+00:00")
    assert m.wallet_real_cohort_start(c, "u1", network_id="4663") == "2026-07-04T00:00:00+00:00"
    # solana was never revoked -> lifetime
    assert m.wallet_real_cohort_start(c, "u1", network_id="solana") is None


def test_demotion_losses_do_not_follow_a_rearmed_chain():
    from sniper.services import memecoin_real_runner as rr

    c = _conn()
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "4663", True, force=True,
                             now_iso="2026-07-01T00:00:00+00:00")
    for i in range(3):
        c.execute(
            "INSERT INTO memecoin_positions(source_user_id,mint,network_id,execution_mode,"
            "is_open,strategy,realized_pnl,closed_at) VALUES('u1',?, '4663','real',0,'alpha_b',"
            "-500.0,?)", (f"M{i}", f"2026-07-02T0{i}:00:00+00:00"))
    c.commit()
    assert rr.check_wallet_demotion(c, "u1", network_id="4663",
                                    alert_fn=lambda *a: None) is True
    assert m.wallet_armed_chains(c, "u1") == []
    # Operator re-arms; the old losses are pre-cohort and must not re-fire.
    m.set_wallet_chain_armed(c, "u1", "4663", True, force=True,
                             now_iso="2026-07-05T00:00:00+00:00")
    rec = m.wallet_real_record(c, "u1", network_id="4663")
    assert rec["closed_count"] == 0 and rec["realized_total"] == 0.0


def _client(ctx_extra=None):
    from flask import Flask

    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    # Arming establishes the Fomo follow BEFORE writing authority (spec 2026-07-16, wired into
    # the per-chain path 2026-08-05), so these stubs are what keep the arming tests offline.
    # Without them the route correctly 503s rather than arming a wallet it cannot follow.
    ctx = {"conn": c, "follow_user_fn": lambda a: "me",
           "follow_following_ids_fn": lambda a: set(),
           "follow_target_fn": lambda a, you, target: True}
    if ctx_extra:
        ctx.update(ctx_extra)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def test_route_single_chain_op_is_additive():
    """POST {chain, armed} adds/removes ONE chain and never replaces the list."""
    client, c = _client()
    m.add_wallet(c, "u1", "h1")
    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"chain": "robin", "armed": True, "force": True})
    assert r.status_code == 200 and r.get_json()["armed_chains"] == ["4663"]
    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"chain": "solana", "armed": True, "force": True})
    assert r.get_json()["armed_chains"] == ["4663", "solana"]
    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"chain": "4663", "armed": False})
    assert r.get_json()["armed_chains"] == ["solana"]


def test_chain_arm_tries_the_fomo_follow_but_a_refusal_does_not_block():
    """AUDIT 2026-08-05 put the follow on the per-chain path; REVERSED 2026-09-12 (operator: "fix")
    to best-effort, because the Fomo account is banned and signals arrive over J7. The follow is
    still ATTEMPTED; its failure lands in the chain-scoped approval reason."""
    tried = []

    def refuse(*a):
        tried.append(a)
        raise RuntimeError("follow_rejected")
    client, c = _client({"follow_target_fn": refuse})
    m.add_wallet(c, "u1", "h1")

    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"chain": "solana", "armed": True, "force": True})

    assert r.status_code == 200 and tried, "the follow must still be attempted"
    assert m.wallet_armed_chains(c, "u1") == ["solana"]
    row = c.execute("SELECT chain, reason FROM memecoin_wallet_approvals "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["chain"] == "solana" and "fomo follow unavailable" in row["reason"]


def test_excluded_chain_arm_makes_no_fomo_call():
    calls = []
    client, c = _client({"follow_target_fn": lambda *args: calls.append(args) or True})
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_excluded(c, "u1", True)

    response = client.post(
        "/api/memecoin/wallets/u1/real",
        json={"chain": "solana", "armed": True, "force": True},
    )

    assert response.status_code == 400
    assert response.get_json()["action"] == "excluded_requires_reobserve"
    assert calls == []


def test_resume_still_tries_fomo_but_a_refusal_does_not_keep_it_paused():
    """REVERSED 2026-09-12 (operator: "fix"): the follow is best-effort; see
    test_chain_arm_tries_the_fomo_follow_but_a_refusal_does_not_block."""
    client, c = _client({
        "follow_target_fn": lambda *_: (
            _ for _ in ()
        ).throw(RuntimeError("follow_rejected")),
    })
    m.add_wallet(c, "u1", "h1")
    m.set_wallet_chain_armed(c, "u1", "robin", True, force=True)
    c.execute("UPDATE memecoin_wallets SET paused=1 WHERE source_user_id='u1'")
    c.commit()

    response = client.post(
        "/api/memecoin/wallets/u1/real",
        json={"chain": "robin", "armed": True, "force": True},
    )
    paused = c.execute(
        "SELECT paused FROM memecoin_wallets WHERE source_user_id='u1'"
    ).fetchone()[0]

    assert response.status_code == 200 and "follow_rejected" in response.get_json()["fomo_follow_failed"]
    assert paused == 0
    assert m.real_approved_user_ids(c, network_id="4663") == {"u1"}


def test_armed_board_separates_approved_nowhere_from_real_authority():
    """AUDIT 2026-08-05: `approved_count` counts the FLAG, and a flag with no chain list
    authorises nothing — it read 32 while only 20 wallets could open a position anywhere."""
    client, c = _client()
    m.add_wallet(c, "armed", "h-armed")
    m.set_wallet_chain_armed(c, "armed", "solana", True, force=True)
    m.add_wallet(c, "nowhere", "h-nowhere")
    m.set_wallet_real_approved(c, "nowhere", True, force=True)      # flag, no chains

    body = client.get("/api/memecoin/armed?chain=solana").get_json()
    rows = {r["source_user_id"]: r for r in body["rows"]}

    assert body["approved_count"] == 2, "both carry the flag"
    assert body["armed_anywhere_count"] == 1, "only one has authority anywhere"
    assert body["approved_nowhere_count"] == 1
    assert rows["nowhere"]["approved_nowhere"] is True
    assert rows["nowhere"]["effective"] is False
    assert rows["armed"]["approved_nowhere"] is False
    assert rows["armed"]["effective"] is True


def test_rearming_an_already_armed_chain_makes_no_fomo_call():
    """An idempotent re-arm grants no new authority, so a Fomo outage must not 503 it."""
    calls = []
    client, c = _client({"follow_target_fn": lambda a, you, t: calls.append(t) or True})
    m.add_wallet(c, "u1", "h1")

    first = client.post("/api/memecoin/wallets/u1/real",
                        json={"chain": "solana", "armed": True, "force": True})
    assert first.status_code == 200 and calls == ["u1"]

    client.get("/api/memecoin/armed?chain=solana")   # no state change
    again = client.post("/api/memecoin/wallets/u1/real",
                        json={"chain": "solana", "armed": True, "force": True})

    assert again.status_code == 200
    assert calls == ["u1"], "re-arm must not re-post a follow"
    assert m.wallet_armed_chains(c, "u1") == ["solana"]


def test_under_j7_the_arm_never_calls_fomo(monkeypatch):
    """Operator 2026-09-12: "you can turn this off". Every follow 403s on the banned account and
    pages FOMO-BAN-GUARD; signals arrive over J7, so the follow is not attempted at all."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SIGNAL_SOURCE", "j7")

    def forbidden(*_a):
        raise AssertionError("no fomo call under J7")
    client, c = _client({"follow_target_fn": forbidden, "follow_user_fn": forbidden,
                         "follow_following_ids_fn": forbidden})
    m.add_wallet(c, "u1", "h1")
    r = client.post("/api/memecoin/wallets/u1/real", json={"chain": "solana", "armed": True, "force": True})
    assert r.status_code == 200 and m.wallet_armed_chains(c, "u1") == ["solana"]
    assert "fomo_follow_failed" not in r.get_json()
    r = client.post("/api/memecoin/wallets/u1/real",
                    json={"approved": True, "chains": ["solana", "56"], "force": True})
    assert r.status_code == 200
