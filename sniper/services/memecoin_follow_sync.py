"""Keep the operator's Fomo following graph and curated wallet list reconciled.

Additive + never-delete: new follows added active (source='follow'); unfollowed
follow-sourced wallets are PAUSED (open positions still exit); manual wallets are
never touched; re-follow un-pauses. Fail-closed: only reconcile on a successful,
non-empty following fetch (a Fomo error must never pause the whole list).

The reverse invariant is one-way: every effective armed wallet is followed on Fomo.
Disarm, pause, exclude, and auto-demote never unfollow.
"""
from __future__ import annotations

from sniper.persistence import db_memecoin as m


def _default_user_fn(auth):
    from sniper.feeds.fomo_feed import fomo_get
    _s, j, _d = fomo_get(auth, "/v2/users/current")
    ro = j.get("responseObject") or j
    return ro.get("id") or ro.get("userId") or (ro.get("user") or {}).get("id")


def _default_following_fn(auth, you):
    """Return (follows, complete, reason). complete=True means the graph was read IN FULL.

    A VALID EMPTY graph — an account that genuinely follows nobody — is a complete read, and
    saying so is load-bearing: the caller's armed-follow repair loop runs only on a complete
    graph, so collapsing "follows nobody" into the same `([], False)` an HTTP failure produces
    is what let the funded roster sit with zero follows and no self-repair for ~42h
    (audit 2026-08-05). The old code both ignored the HTTP status and treated an empty page as
    ambiguous, so a 403 and an empty account were literally the same value.

    `reason` names WHICH of the three outcomes occurred, so the operator log can distinguish
    "the fetch broke" from "we follow nobody" — previously one string, `empty_or_error`, meant
    both. A mid-pagination error, a non-2xx status, a malformed payload, or hitting the 20-page
    cap all yield complete=False, so the caller still skips the PAUSE pass (a transient empty
    page-2 must never pause the whole follow list).
    """
    from sniper.feeds.fomo_feed import fomo_get
    out, page, complete, seen = [], 1, False, set()

    def fail(reason):
        try:
            ids = sorted(_default_following_ids_fn(auth))
        except Exception:
            return out, False, reason
        return ([{"id": uid, "userHandle": ""} for uid in ids],
                True, "following_ids_fallback")

    while page <= 20:  # hard cap; defensive pagination
        try:
            status, j, _d = fomo_get(auth, f"/v2/users/{you}/following?page={page}&limit=100")
        except Exception:
            return fail("following_fetch_failed")
        try:
            if not 200 <= int(status) < 300:
                return fail("following_fetch_failed")
        except (TypeError, ValueError):
            return fail("following_fetch_failed")
        if not isinstance(j, (dict, list)):
            return fail("following_malformed")
        ro = j.get("responseObject") if isinstance(j, dict) else j
        if ro is None:
            ro = j
        if isinstance(ro, list):
            items = ro
        elif isinstance(ro, dict):
            items = ro.get("following")
            if items is None:
                items = ro.get("users")
            if items is None:
                items = []
        else:
            return fail("following_malformed")
        # An authenticated 200 whose payload is not a list of members is a BROKEN read, not an
        # empty one. Without this it degraded to `items = []` and read as "follows nobody",
        # which now drives real follow POSTs.
        if not isinstance(items, list) or any(not isinstance(it, dict) for it in items):
            return fail("following_malformed")
        fresh = 0
        for it in items:
            uid = it.get("id") or it.get("userId") or (it.get("user") or {}).get("id")
            handle = (it.get("userHandle") or it.get("displayName")
                      or (it.get("user") or {}).get("userHandle") or "")
            if uid and uid not in seen:
                seen.add(uid)
                fresh += 1
                out.append({"id": uid, "userHandle": handle})
        if len(items) < 100:
            # A short page is the real end of the list. Now that the status and payload shape
            # are validated above, an EMPTY short page is a genuine empty graph, not ambiguity.
            complete = True
            break
        # The endpoint ignores `page` and `limit` — every page returns the same set, so the
        # loop used to fetch 20 identical pages, return 20x duplicates, and ALWAYS report
        # complete=False, which silently disabled the pause/reconcile pass. A page that adds
        # no new id means the server is not paginating: we already hold the full list.
        if not fresh:
            complete = True
            break
        page += 1
    if not complete:
        return fail("following_truncated")
    return out, True, (None if out else "following_empty_complete")


def _default_following_ids_fn(auth):
    from sniper.feeds.fomo_feed import fomo_get
    status, payload, _date = fomo_get(auth, "/v2/users/current/followingIds")
    if not 200 <= int(status) < 300:
        raise RuntimeError(f"following_ids_http_{status}")
    ro = payload.get("responseObject") if isinstance(payload, dict) else None
    ro = payload if ro is None else ro
    if isinstance(ro, list):
        raw_ids = ro
    elif isinstance(ro, dict):
        raw_ids = next((ro[k] for k in ("followingIds", "ids", "following")
                        if ro.get(k) is not None), None)
    else:
        raw_ids = None
    if not isinstance(raw_ids, list):
        raise RuntimeError("following_ids_malformed")
    out = set()
    for item in raw_ids:
        uid = (item.get("id") or item.get("userId")) if isinstance(item, dict) else item
        if uid:
            out.add(str(uid))
    return out


def _default_follow_fn(auth, you, target_id):
    from sniper.feeds.fomo_feed import fomo_request
    status, payload, _date = fomo_request(
        auth,
        "POST",
        "/follows",
        body={"user_id": str(you), "following_id": str(target_id)},
    )
    try:
        api_status = int(payload.get("statusCode"))
    except (AttributeError, TypeError, ValueError):
        api_status = None
    if not 200 <= int(status) < 300 or api_status != 200:
        raise RuntimeError(f"follow_rejected_http_{status}_api_{api_status}")
    return True


def ensure_target_followed(auth, target_id, *, user_fn=None, following_ids_fn=None,
                           follow_fn=None):
    """Establish one follow for the immediate arm path; raises on uncertain state."""
    user_fn = user_fn or _default_user_fn
    following_ids_fn = following_ids_fn or _default_following_ids_fn
    follow_fn = follow_fn or _default_follow_fn
    you = user_fn(auth)
    if not you:
        raise RuntimeError("no_user")
    followed_ids = following_ids_fn(auth)
    if followed_ids is None:
        raise RuntimeError("following_ids_missing")
    followed_ids = {str(uid) for uid in followed_ids if uid}
    target_id = str(target_id)
    if target_id in followed_ids:
        return {"followed": True, "created": False, "user_id": str(you)}
    if follow_fn(auth, you, target_id) is not True:
        raise RuntimeError("follow_rejected")
    return {"followed": True, "created": True, "user_id": str(you)}


def sync_followed(conn, auth, user_fn=None, following_fn=None, follow_fn=None):
    user_fn = user_fn or _default_user_fn
    following_fn = following_fn or _default_following_fn
    follow_fn = follow_fn or _default_follow_fn

    try:
        you = user_fn(auth)
    except Exception:
        you = None
    if not you:
        return {"skipped": True, "reason": "no_user"}

    try:
        result = following_fn(auth, you)
    except Exception:
        result = ([], False, "following_fetch_failed")
    # accept (follows, complete, reason) or (follows, complete) from the default fn, or a bare
    # list from an injected fn / operator-supplied full graph (treated as complete).
    reason = None
    if isinstance(result, tuple):
        if len(result) == 3:
            follows, complete, reason = result
        else:
            follows, complete = result
    else:
        follows, complete = (result or []), True
    # Fail closed ONLY on an unusable read. The old guard was `if not follows` — it fired on a
    # VALID EMPTY graph too, returning before the armed-repair loop below, which is why a roster
    # with every follow missing could never repair itself: the one state that most needs repair
    # was the one state that skipped it (audit 2026-08-05).
    if not follows and not complete:
        return {"skipped": True, "reason": reason or "following_fetch_failed"}
    # Whether the graph we READ was empty, captured before repair adds to `followed` below.
    graph_was_empty = not follows

    followed = {}
    for it in follows:
        uid = it.get("id")
        if uid:
            followed[uid] = it.get("userHandle") or ""

    cur = {w["source_user_id"]: w for w in m.list_wallets(conn)}
    # A wallet armed on ANY chain must keep its fomo follow (spec 2026-07-16).
    armed_ids = sorted(m.real_armed_any_chain_user_ids(conn))
    armed_set = set(armed_ids)
    armed_already_followed = len(armed_set.intersection(followed))
    armed_followed = 0
    armed_follow_failures = []

    # Reverse repair only from a complete graph. A partial read cannot prove a target is
    # missing and must not cause a duplicate external mutation.
    if complete:
        for uid in armed_ids:
            if uid in followed or ":" in uid:   # pump:/evm: ids are wallets, not Fomo users
                continue
            try:
                if follow_fn(auth, you, uid) is not True:
                    raise RuntimeError("follow_rejected")
            except Exception:
                armed_follow_failures.append(uid)
                continue
            followed[uid] = (cur.get(uid) or {}).get("handle") or ""
            armed_followed += 1

    added = unpaused = paused = 0

    for uid, handle in followed.items():
        w = cur.get(uid)
        if not w:
            m.add_wallet(conn, uid, handle, source="follow")
            added += 1
        elif (w.get("source") or "manual") == "follow" and w["paused"] and not w.get("excluded"):
            m.set_wallet_paused(conn, uid, False)   # re-follow un-pauses (unless operator-excluded)
            unpaused += 1
        # source == 'manual' -> sacrosanct, leave untouched

    # PAUSE pass only when the following graph was fetched COMPLETELY — a truncated
    # fetch must not pause wallets that are merely on an unfetched page.
    #
    # An EMPTY complete graph is excluded on purpose. "We follow nobody" is equally explained by
    # an account switch (which is exactly what produced the 2026-08-05 state) as by someone
    # unfollowing the entire roster one wallet at a time, and only the second reading justifies
    # pausing every follow-sourced wallet at once. Repair still runs above; the destructive
    # inference does not. A non-empty graph paginates and pauses exactly as before.
    if complete and not graph_was_empty and reason != "following_ids_fallback":
        for uid, w in cur.items():
            if ((w.get("source") or "manual") == "follow" and not w["paused"]
                    and uid not in followed and uid not in armed_set):
                m.set_wallet_paused(conn, uid, True)     # unfollow pauses (never delete)
                paused += 1

    return {"added": added, "unpaused": unpaused, "paused": paused,
            "followed": len(followed), "complete": complete,
            "reason": reason or (None if not graph_was_empty else "following_empty_complete"),
            "armed": len(armed_ids),
            "armed_already_followed": armed_already_followed,
            "armed_followed": armed_followed,
            "armed_follow_failed": len(armed_follow_failures),
            "armed_follow_failures": armed_follow_failures}
