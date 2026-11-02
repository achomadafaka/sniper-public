"""Follow-sync: fomo following graph -> curated list. Additive, manual-sacrosanct,
fail-closed. No network — fomo fetches injected."""
import sqlite3

from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


# ── Task A: schema + add_wallet source/upsert + open_position_user_ids ───────
def test_source_column_default_manual():
    c = _conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(memecoin_wallets)")}
    assert "source" in cols
    m.add_wallet(c, "u1", "alice")
    assert m.list_wallets(c)[0]["source"] == "manual"


def test_follow_add_is_insert_or_ignore():
    """A follow re-add must not disturb the row's STATE. Its display name is not state.

    This asserted `handle == "alice"` as its proxy for "the row was ignored". That froze the
    roster name at first-add while fomo went on renaming accounts (uid bfc5cd45… delivered events
    as both `groguguy67` and `ZonalItchyDeer`, and a lookup by the current name missed the row).
    Since 2026-08-19 the handle refreshes and everything else is still ignored — which is the
    invariant this test is actually for.
    """
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    w = m.list_wallets(c)[0]
    assert w["source"] == "follow" and w["paused"] == 0
    m.set_wallet_paused(c, "u1", True)
    m.add_wallet(c, "u1", "alice2", source="follow")     # re-add: state ignored, name refreshed
    w = m.list_wallets(c)[0]
    assert w["paused"] == 1 and w["source"] == "follow"  # state untouched — the point of the test
    assert w["handle"] == "alice2"                       # display name follows the account


def test_manual_add_upserts_over_paused_follow():
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    m.set_wallet_paused(c, "u1", True)                   # unfollowed
    m.add_wallet(c, "u1", "alice", source="manual")      # manual add-by-handle wins
    w = m.list_wallets(c)[0]
    assert w["source"] == "manual" and w["paused"] == 0


def test_open_position_user_ids():
    c = _conn()
    m.open_position(c, source_user_id="u1", source_handle="a", mint="M", ticker="T",
                    entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                    entry_lag_s=0.3, their_size_usd=1.0, opened_at="t")
    assert m.open_position_user_ids(c) == {"u1"}


# ── Task B: sync_followed reconcile ─────────────────────────────────────────
from sniper.services.memecoin_follow_sync import sync_followed
from sniper.services import memecoin_follow_sync as follow_sync
from _web_csrf import csrf_client


def _sync(c, follows, you="me"):
    return sync_followed(c, auth=None, user_fn=lambda a: you,
                         following_fn=lambda a, u: follows)


def _byid(c):
    return {w["source_user_id"]: w for w in m.list_wallets(c)}


def test_sync_adds_new_follows():
    c = _conn()
    r = _sync(c, [{"id": "u1", "userHandle": "alice"}, {"id": "u2", "userHandle": "bob"}])
    assert r["added"] == 2 and r["followed"] == 2
    assert _byid(c)["u1"]["source"] == "follow" and _byid(c)["u1"]["paused"] == 0


def test_sync_pauses_unfollowed():
    c = _conn()
    _sync(c, [{"id": "u1", "userHandle": "a"}, {"id": "u2", "userHandle": "b"}])
    r = _sync(c, [{"id": "u2", "userHandle": "b"}])          # unfollowed u1
    assert r["paused"] == 1
    assert _byid(c)["u1"]["paused"] == 1 and _byid(c)["u2"]["paused"] == 0


def test_sync_refollow_unpauses():
    c = _conn()
    _sync(c, [{"id": "u1", "userHandle": "a"}, {"id": "u2", "userHandle": "b"}])
    _sync(c, [{"id": "u2", "userHandle": "b"}])              # unfollow u1
    r = _sync(c, [{"id": "u1", "userHandle": "a"}, {"id": "u2", "userHandle": "b"}])
    assert r["unpaused"] == 1 and _byid(c)["u1"]["paused"] == 0


def test_sync_never_touches_manual():
    c = _conn()
    m.add_wallet(c, "m1", "manualguy", source="manual")
    _sync(c, [{"id": "u1", "userHandle": "a"}])              # m1 not in follows
    assert _byid(c)["m1"]["source"] == "manual" and _byid(c)["m1"]["paused"] == 0
    _sync(c, [{"id": "u2", "userHandle": "b"}])              # still not followed
    assert _byid(c)["m1"]["paused"] == 0                     # manual stays active


def test_sync_on_a_complete_empty_graph_pauses_nothing():
    """CHANGED 2026-08-05 (was `test_sync_fail_closed_on_empty`, which asserted `skipped`).

    That assertion encoded the defect: an empty graph returned BEFORE the armed-follow repair
    loop, so the one state that most needs repair was the one state that skipped it. A bare list
    from an injected fn is by convention a COMPLETE operator-supplied graph, so an empty one now
    proceeds. The protection this test actually exists for is unchanged and still asserted — an
    empty graph must not pause the roster. Failure/malformed reads still skip; see
    `test_failed_following_fetch_makes_no_follow_or_pause_mutations`.
    """
    c = _conn()
    _sync(c, [{"id": "u1", "userHandle": "a"}])
    r = _sync(c, [])
    assert not r.get("skipped")
    assert r["reason"] == "following_empty_complete"
    assert r["paused"] == 0
    assert _byid(c)["u1"]["paused"] == 0                     # NOT paused


def test_sync_fail_closed_on_no_user():
    c = _conn()
    r = sync_followed(c, auth=None, user_fn=lambda a: None,
                      following_fn=lambda a, u: [{"id": "u1"}])
    assert r.get("skipped") and r["reason"] == "no_user"


def _arm(c, uid, handle=None, source="manual", chains=("solana",)):
    """Arming is per chain (2026-07-30): approval alone leaves a wallet armed nowhere."""
    m.add_wallet(c, uid, handle or uid, source=source)
    m.set_wallet_real_approved(c, uid, True, chains=list(chains), force=True)


def test_sync_repairs_every_missing_effective_armed_follow():
    c = _conn()
    for uid in ("u1", "u2", "u3"):
        _arm(c, uid)
    calls = []

    r = sync_followed(
        c,
        auth=None,
        user_fn=lambda a: "me",
        following_fn=lambda a, u: [{"id": "u1", "userHandle": "one"}],
        follow_fn=lambda a, you, target: calls.append((you, target)) or True,
    )

    assert calls == [("me", "u2"), ("me", "u3")]
    assert r["armed"] == 3
    assert r["armed_already_followed"] == 1
    assert r["armed_followed"] == 2
    assert r["armed_follow_failed"] == 0
    assert r["armed_follow_failures"] == []
    assert r["followed"] == 3


def test_sync_armed_follow_failure_is_isolated_and_never_revokes():
    c = _conn()
    for uid in ("u1", "u2", "u3"):
        _arm(c, uid)
    calls = []

    def follow(_auth, _you, target):
        calls.append(target)
        if target == "u2":
            raise RuntimeError("rejected")
        return True

    r = sync_followed(
        c,
        auth=None,
        user_fn=lambda a: "me",
        following_fn=lambda a, u: [{"id": "u1"}],
        follow_fn=follow,
    )

    assert calls == ["u2", "u3"]
    assert r["armed_followed"] == 1
    assert r["armed_follow_failed"] == 1
    assert r["armed_follow_failures"] == ["u2"]
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1", "u2", "u3"}


def test_sync_repairs_armed_follow_source_before_pause_reconciliation():
    c = _conn()
    _arm(c, "u1", source="follow")
    calls = []

    r = sync_followed(
        c,
        auth=None,
        user_fn=lambda a: "me",
        following_fn=lambda a, u: [{"id": "external"}],
        follow_fn=lambda a, you, target: calls.append(target) or True,
    )

    assert calls == ["u1"]
    assert r["armed_followed"] == 1 and r["paused"] == 0
    assert _byid(c)["u1"]["paused"] == 0


def test_sync_does_not_reverse_follow_ineligible_wallets():
    c = _conn()
    _arm(c, "paused")
    m.set_wallet_paused(c, "paused", True)
    _arm(c, "excluded")
    m.set_wallet_excluded(c, "excluded", True)
    m.add_wallet(c, "paper", "paper")
    calls = []

    r = sync_followed(
        c,
        auth=None,
        user_fn=lambda a: "me",
        following_fn=lambda a, u: [{"id": "external"}],
        follow_fn=lambda a, you, target: calls.append(target) or True,
    )

    assert calls == []
    assert r["armed"] == 0 and r["armed_followed"] == 0


def test_sync_incomplete_graph_skips_reverse_follow_mutations():
    c = _conn()
    _arm(c, "u1")
    calls = []

    r = sync_followed(
        c,
        auth=None,
        user_fn=lambda a: "me",
        following_fn=lambda a, u: ([{"id": "external"}], False),
        follow_fn=lambda a, you, target: calls.append(target) or True,
    )

    assert calls == []
    assert r["complete"] is False
    assert r["armed"] == 1 and r["armed_followed"] == 0


def test_default_follow_requires_fomo_status_code_200(monkeypatch):
    from sniper.feeds import fomo_feed

    monkeypatch.setattr(
        fomo_feed,
        "fomo_request",
        lambda *a, **k: (200, {"statusCode": 500}, None),
    )

    try:
        follow_sync._default_follow_fn(None, "me", "u1")
    except RuntimeError as exc:
        assert "follow_rejected" in str(exc)
    else:
        raise AssertionError("malformed Fomo success was accepted")


def test_default_following_ids_parses_complete_id_payload(monkeypatch):
    from sniper.feeds import fomo_feed

    monkeypatch.setattr(
        fomo_feed,
        "fomo_get",
        lambda *a, **k: (200, {"responseObject": {"followingIds": ["u1", "u2"]}}, None),
    )

    assert follow_sync._default_following_ids_fn(None) == {"u1", "u2"}


def test_empty_following_ids_allows_first_follow_and_graph_repair(monkeypatch):
    from sniper.feeds import fomo_feed

    def get(auth, path):
        if path == "/v2/users/current/followingIds":
            return 200, {"responseObject": {"followingIds": []}}, None
        return 503, {}, None

    monkeypatch.setattr(fomo_feed, "fomo_get", get)
    follows = []
    result = follow_sync.ensure_target_followed(
        None, "u1", user_fn=lambda auth: "me",
        follow_fn=lambda auth, me, uid: follows.append(uid) or True)
    assert result["created"] is True
    assert follows == ["u1"]
    assert follow_sync._default_following_fn(None, "me") == ([], True, "following_ids_fallback")


# ── Task C: paused wallet's open position still mirror-exits (decision 1b) ───
from sniper.services import memecoin_runner as run


class _Auth:
    access = "t"; refresh = "r"; ca_id = "c"
    def refresh_token(self, http=None): return "t"


def _raw(type_, uid, mint, eid, price=0.005):
    return {"id": eid, "type": type_, "userId": uid, "tokenAddress": mint,
            "createdAt": "2026-07-05T10:00:00Z",
            "body": {"userHandle": "x", "ticker": "T", "price": price,
                     "marketCap": "1000000", "currentSizeUsd": 3000.0, "isFirstBuy": True}}


def _run(c, raw_items, fill_price=0.010):
    feed = {"responseObject": {"feed": raw_items}}
    def http(method, url, headers=None, body=None):
        return 200, feed, "Sun, 05 Jul 2026 10:00:01 GMT"
    return run.run_once(c, _Auth(), size_usd=100.0, http=http,
                        fill_fn=lambda *a, **k: (fill_price, "jupiter"))


def test_paused_wallet_open_position_still_mirror_exits():
    c = _conn()
    m.add_wallet(c, "u1", "a", source="follow")
    m.set_wallet_paused(c, "u1", True)                 # unfollowed
    pid = m.open_position(c, source_user_id="u1", source_handle="a", mint="M", ticker="T",
                          entry_price=0.005, entry_notional=100.0, entry_fill_source="jupiter",
                          entry_lag_s=0.3, their_size_usd=1.0, opened_at="t")
    m.insert_fill(c, position_id=pid, kind="entry", price=0.005, qty=20000.0, notional=100.0,
                  fill_source="jupiter", fraction=1.0, created_at="t")
    _run(c, [_raw("large_sell", "u1", "M", "s1")], fill_price=0.010)   # trader sells
    pos = m.get_position(c, pid)
    # offline the executable exit quote is unavailable, so the close books censored
    # ('mirror_noquote'); the property under test is that the PAUSED wallet still mirror-exits.
    assert pos["is_open"] == 0 and pos["exit_reason"].startswith("mirror")


def test_paused_wallet_does_not_open_new_position():
    c = _conn()
    m.add_wallet(c, "u1", "a", source="follow")
    m.set_wallet_paused(c, "u1", True)
    _run(c, [_raw("large_buy", "u1", "NEWMINT", "b1")])
    assert m.get_open_position(c, "u1", "NEWMINT") is None   # paused -> no new entry


# ── Task D: sync-follows route ──────────────────────────────────────────────
from flask import Flask
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client(ctx_extra=None):
    c = _conn()
    ctx = {"conn": c}
    if ctx_extra:
        ctx.update(ctx_extra)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint(ctx))
    return csrf_client(app), c


def test_sync_follows_route():
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_fn": lambda a, u: [{"id": "u1", "userHandle": "alice"}],
    })
    r = cl.post("/api/memecoin/sync-follows").get_json()
    assert r["added"] == 1 and r["followed"] == 1
    assert c.execute("SELECT source FROM memecoin_wallets WHERE source_user_id='u1'").fetchone()[0] == "follow"


def test_sync_follows_route_fail_closed():
    cl, c = _client({"follow_user_fn": lambda a: None,
                     "follow_following_fn": lambda a, u: []})
    r = cl.post("/api/memecoin/sync-follows").get_json()
    assert r.get("skipped")


def test_real_arm_follows_before_persisting_approval():
    calls = []
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: set(),
        "follow_target_fn": lambda a, you, target: (
            calls.append((you, target, target in m.real_approved_user_ids(c, network_id="solana"))) or True
        ),
    })
    m.add_wallet(c, "u1", "alice")

    response = cl.post(
        "/api/memecoin/wallets/u1/real",
        json={"approved": True, "chains": ["solana"], "force": True, "reason": "operator"},
    )
    body = response.get_json()

    assert response.status_code == 200 and body["ok"] is True
    assert calls == [("me", "u1", False)]
    assert "u1" in m.real_approved_user_ids(c, network_id="solana")
    assert body["fomo_follow_created"] is True


def test_real_arm_already_followed_does_not_post_again():
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: {"u1"},
        "follow_target_fn": lambda *args: (_ for _ in ()).throw(AssertionError("duplicate POST")),
    })
    m.add_wallet(c, "u1", "alice")

    body = cl.post(
        "/api/memecoin/wallets/u1/real", json={"approved": True, "chains": ["solana"], "force": True},
    ).get_json()

    assert body["ok"] is True and body["fomo_follow_created"] is False
    assert "u1" in m.real_approved_user_ids(c, network_id="solana")


def test_real_arm_gate_refusal_makes_no_fomo_request():
    def forbidden(*_args):
        raise AssertionError("Fomo called before gate passed")

    cl, c = _client({
        "follow_user_fn": forbidden,
        "follow_following_ids_fn": forbidden,
        "follow_target_fn": forbidden,
    })
    m.add_wallet(c, "u1", "alice")

    body = cl.post(
        "/api/memecoin/wallets/u1/real", json={"approved": True, "chains": ["solana"]},
    ).get_json()

    assert body["ok"] is False and body["action"] == "approve_refused"
    assert "u1" not in m.real_approved_user_ids(c, network_id="solana")
    row = c.execute("SELECT action FROM memecoin_wallet_approvals").fetchone()
    assert row["action"] == "approve_refused"


def test_real_arm_fomo_failure_arms_anyway_and_is_audited():
    """REVERSED 2026-09-12 (operator: "fix"). Fomo banned the account on 2026-08-03 and signals
    arrive over the J7 relay, so a refused follow no longer blocks authority. It is recorded."""
    def rejected(_auth, _you, _target):
        raise RuntimeError("rejected")

    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: set(),
        "follow_target_fn": rejected,
    })
    m.add_wallet(c, "u1", "alice")

    response = cl.post(
        "/api/memecoin/wallets/u1/real",
        json={"approved": True, "chains": ["solana"], "force": True, "reason": "operator"},
    )
    body = response.get_json()

    assert response.status_code == 200
    assert body["ok"] is True and "rejected" in body["fomo_follow_failed"]
    assert "u1" in m.real_approved_user_ids(c, network_id="solana")
    row = c.execute("SELECT * FROM memecoin_wallet_approvals ORDER BY id DESC").fetchone()
    assert "fomo follow unavailable" in row["reason"]


def test_real_disarm_never_calls_fomo():
    def forbidden(*_args):
        raise AssertionError("disarm must not mutate Fomo")

    cl, c = _client({
        "follow_user_fn": forbidden,
        "follow_following_ids_fn": forbidden,
        "follow_target_fn": forbidden,
    })
    m.add_wallet(c, "u1", "alice")
    m.set_wallet_real_approved(c, "u1", True, chains=["solana"], force=True)

    body = cl.post(
        "/api/memecoin/wallets/u1/real", json={"approved": False},
    ).get_json()

    assert body["ok"] is True and body["action"] == "revoke"
    assert "u1" not in m.real_approved_user_ids(c, network_id="solana")


def test_following_pagination_terminates_when_the_server_ignores_page():
    """The fomo endpoint ignores page/limit and returns the same set every time.

    The loop used to fetch 20 identical pages, return 20x duplicates (2240 objects for 112
    real follows) and ALWAYS report complete=False — which silently disabled the reconcile
    pass on every sync.
    """
    from sniper.services import memecoin_follow_sync as fs

    same_page = [{"id": f"u{i}", "userHandle": f"h{i}"} for i in range(112)]
    calls = []

    def fake_get(auth, path):
        calls.append(path)
        return 200, {"responseObject": {"following": same_page}}, None

    fs_get = "sniper.feeds.fomo_feed.fomo_get"
    import unittest.mock as mock
    with mock.patch(fs_get, fake_get):
        follows, complete, reason = fs._default_following_fn(None, "me")
    assert len(follows) == 112, "duplicates must not accumulate"
    assert complete is True, "a non-paginating server still yields a COMPLETE list"
    assert reason is None, "a healthy non-empty read has no diagnostic reason"
    assert len(calls) == 2, f"must stop after the first repeat, not 20 pages (got {len(calls)})"


# ── AUDIT 2026-08-05: the armed-follow invariant (spec 2026-07-16) was unenforced ──
# Two independent defects let a funded roster run with zero Fomo follows and no self-repair:
#   1. the PER-CHAIN arm route (the form the UI actually sends) wrote authority without ever
#      establishing the follow — the guard existed only on the legacy whole-list path;
#   2. `sync_followed` fail-closed on an empty follow list BEFORE the armed-repair loop, so the
#      repair was dead exactly when every follow was missing.
# Route tests live here rather than in test_memecoin_per_chain_arming.py because the Flask
# harness (`_client`) is defined in this module; duplicating it would fork the fixture.
import sniper.services.memecoin_follow_sync as fs


def _armed_wallet(c, uid, chain="solana"):
    m.add_wallet(c, uid, "h-" + uid)
    m.set_wallet_real_approved(c, uid, True, chains=[chain], force=True)


def test_per_chain_arm_follows_before_writing_authority():
    """The follow must be established BEFORE the chain is armed, not after and not never."""
    calls = []
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: set(),
        "follow_target_fn": lambda a, you, target: (
            calls.append((target, m.wallet_armed_chains(c, target))) or True
        ),
    })
    m.add_wallet(c, "u1", "alice")

    body = cl.post("/api/memecoin/wallets/u1/real",
                   json={"chain": "solana", "armed": True, "force": True}).get_json()

    assert body["ok"] is True
    # authority must still have been EMPTY at the moment we posted the follow
    assert calls == [("u1", [])], f"follow ordering wrong: {calls}"
    assert "u1" in m.real_approved_user_ids(c, network_id="solana")


def test_per_chain_arm_follow_failure_still_arms_and_says_so():
    cl, c = _client({
        "follow_user_fn": lambda a: "me",
        "follow_following_ids_fn": lambda a: set(),
        "follow_target_fn": lambda *_: (_ for _ in ()).throw(RuntimeError("follow_rejected")),
    })
    m.add_wallet(c, "u1", "alice")

    r = cl.post("/api/memecoin/wallets/u1/real",
                json={"chain": "solana", "armed": True, "force": True})

    assert r.status_code == 200 and "follow_rejected" in r.get_json()["fomo_follow_failed"]
    assert m.real_approved_user_ids(c, network_id="solana") == {"u1"}
    row = c.execute("SELECT action, chain, reason FROM memecoin_wallet_approvals "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    assert row["chain"] == "solana" and "fomo follow unavailable" in row["reason"]


def test_per_chain_arm_gate_refusal_makes_no_fomo_request():
    def forbidden(*_a):
        raise AssertionError("Fomo called before the chain gate passed")

    cl, c = _client({"follow_user_fn": forbidden, "follow_following_ids_fn": forbidden,
                     "follow_target_fn": forbidden})
    m.add_wallet(c, "u1", "alice")

    body = cl.post("/api/memecoin/wallets/u1/real",
                   json={"chain": "solana", "armed": True}).get_json()

    assert body["ok"] is False and body["action"] == "approve_refused"
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_per_chain_disarm_makes_no_external_call():
    """Disarm is local and must never touch Fomo — this service never unfollows."""
    def forbidden(*_a):
        raise AssertionError("Fomo called on disarm")

    cl, c = _client({"follow_user_fn": forbidden, "follow_following_ids_fn": forbidden,
                     "follow_target_fn": forbidden})
    _armed_wallet(c, "u1")

    body = cl.post("/api/memecoin/wallets/u1/real",
                   json={"chain": "solana", "armed": False}).get_json()

    assert body["ok"] is True
    assert m.real_approved_user_ids(c, network_id="solana") == set()


def test_valid_empty_following_graph_repairs_every_armed_wallet():
    """An account that follows NOBODY is a complete read, not an error — repair must run."""
    c = _conn()
    _armed_wallet(c, "u1")
    _armed_wallet(c, "u2", chain="4663")
    posted = []

    res = fs.sync_followed(
        c, None,
        user_fn=lambda a: "me",
        following_fn=lambda a, you: ([], True),
        follow_fn=lambda a, you, target: (posted.append(target) or True),
    )

    assert sorted(posted) == ["u1", "u2"], f"armed repair did not run: {res}"
    assert res["armed_followed"] == 2 and not res.get("skipped")


def test_failed_following_fetch_makes_no_follow_or_pause_mutations():
    c = _conn()
    m.add_wallet(c, "u1", "alice", source="follow")
    _armed_wallet(c, "u2")

    res = fs.sync_followed(
        c, None,
        user_fn=lambda a: "me",
        following_fn=lambda a, you: ([], False),
        follow_fn=lambda *_: (_ for _ in ()).throw(AssertionError("followed on a failed read")),
    )

    assert res.get("skipped") and res["reason"] == "following_fetch_failed"
    assert dict(c.execute("SELECT paused FROM memecoin_wallets "
                          "WHERE source_user_id='u1'").fetchone())["paused"] == 0


def test_valid_empty_graph_does_not_pause_the_whole_roster():
    """Repair yes, mass-pause no: 'we follow nobody' is equally explained by an account switch."""
    c = _conn()
    m.add_wallet(c, "bystander", "bob", source="follow")
    _armed_wallet(c, "u1")

    res = fs.sync_followed(c, None, user_fn=lambda a: "me",
                           following_fn=lambda a, you: ([], True),
                           follow_fn=lambda a, you, t: True)

    assert res["armed_followed"] == 1
    assert res["paused"] == 0, "an empty graph must not pause the roster"
    assert dict(c.execute("SELECT paused FROM memecoin_wallets "
                          "WHERE source_user_id='bystander'").fetchone())["paused"] == 0


def test_one_repair_failure_does_not_stop_later_repairs():
    c = _conn()
    for uid in ("u1", "u2", "u3"):
        _armed_wallet(c, uid)

    def flaky(a, you, target):
        if target == "u1":
            raise RuntimeError("follow_rejected")
        return True

    res = fs.sync_followed(c, None, user_fn=lambda a: "me",
                           following_fn=lambda a, you: ([], True), follow_fn=flaky)

    assert res["armed_follow_failed"] == 1 and res["armed_followed"] == 2
    assert res["armed_follow_failures"] == ["u1"]


def test_default_following_fn_separates_failure_empty_and_malformed(monkeypatch):
    import sniper.feeds.fomo_feed as ff

    def _reply(status, payload):
        monkeypatch.setattr(ff, "fomo_get", lambda a, p: (status, payload, None))

    _reply(200, {"responseObject": {"following": []}})
    assert fs._default_following_fn(None, "me") == ([], True, "following_empty_complete")

    _reply(403, {"responseObject": {"following": []}})
    assert fs._default_following_fn(None, "me") == ([], False, "following_fetch_failed")

    _reply(200, {"responseObject": {"following": "not-a-list"}})
    assert fs._default_following_fn(None, "me") == ([], False, "following_malformed")

    _reply(200, {"responseObject": {"following": [{"id": "u1", "userHandle": "alice"}]}})
    follows, complete, reason = fs._default_following_fn(None, "me")
    assert follows == [{"id": "u1", "userHandle": "alice"}] and complete and reason is None


def test_default_following_fn_uses_complete_id_fallback_without_pause(monkeypatch):
    import sniper.feeds.fomo_feed as ff

    def reply(_auth, path):
        if path.startswith("/v2/users/me/following?"):
            return 403, {}, None
        assert path == "/v2/users/current/followingIds"
        return 200, {"responseObject": {"followingIds": ["armed", "kept"]}}, None

    monkeypatch.setattr(ff, "fomo_get", reply)
    follows, complete, reason = fs._default_following_fn(None, "me")
    assert follows == [
        {"id": "armed", "userHandle": ""},
        {"id": "kept", "userHandle": ""},
    ]
    assert complete is True and reason == "following_ids_fallback"

    c = _conn()
    _armed_wallet(c, "armed")
    m.add_wallet(c, "absent", "absent", source="follow")
    result = fs.sync_followed(
        c, None, user_fn=lambda _auth: "me",
        following_fn=lambda _auth, _you: (follows, complete, reason),
        follow_fn=lambda *_args: True,
    )
    assert result["armed_already_followed"] == 1
    assert c.execute(
        "SELECT paused FROM memecoin_wallets WHERE source_user_id='absent'"
    ).fetchone()["paused"] == 0
