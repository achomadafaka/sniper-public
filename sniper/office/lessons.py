"""Office lessons loop (operator GO f7a006dd step 4): the Postmortem seat pages Gamma's call grader through grade.read,
writes lessons into office memory through lesson.propose, and the next prompt of the target role carries them.
Read-only over the ledger; the only writer is office memory (append-only, retention follows memory_entries)."""
from sniper.office import memory as M
from sniper.office.call_grader import grade_closed

SECTION = "office/lessons"
TITLES = {"head": "Head", "scout": "Scout", "judge": "Judge", "postmortem": "Postmortem"}
TARGETS = frozenset({"head", "scout", "judge"})
PAGE_MAX = 50


def _cursor(text):
    if text == "":
        return None
    at, sep, order_id = text.partition("|")
    if not sep or not order_id:
        raise ValueError("grade_cursor_invalid")
    return (float(at), order_id)


def grade_page(ledger_conn, *, epoch_id, evaluated, viewer_role, cursor, limit=PAGE_MAX):
    """One bounded page. Grades of an evaluated (or unknown) epoch stay hidden from quarantined roles before the look."""
    if evaluated is not False and TITLES.get(viewer_role) in M.QUARANTINED_ROLES | {None}:
        return {"state": "refused", "reason": "grades_quarantined", "rows": [], "next": None}
    limit = max(1, min(int(limit), PAGE_MAX))
    rows = grade_closed(ledger_conn, epoch_id=epoch_id, after=_cursor(cursor), limit=limit)
    last = rows[-1]["cursor"] if len(rows) == limit else None
    return {"state": "known", "rows": rows, "next": None if last is None else "%r|%s" % (float(last[0]), last[1])}


def propose(mem, clock, *, author, for_role, text, order_ids, evaluated):
    if for_role not in TARGETS:
        raise ValueError("lesson_role_invalid")
    return M.write(mem, clock, {"section": SECTION, "kind": "lesson", "author": author, "for_role": for_role, "text": text,
                                "source_trade_ids": list(order_ids), "evaluation_derived": evaluated is not False})


_NOBODY = {"coin": object(), "creator": object(), "minute": object()}   # no same-source exclusion for role-wide lessons


def for_role(mem, *, role, cut, live=False, k=5):
    """Newest lessons for this role known by the cut. Quarantined roles read sealed snapshots only, so none here.
    live=True (epoch flag lessons_live, office-paper-eval-1 only) also injects grade-derived lessons: the loop is the policy under test."""
    title = TITLES.get(role)
    if title is None or title in M.QUARANTINED_ROLES:
        return {"state": "quarantined", "items": []}
    # ponytail: newest 200 visible entries, then filter by role; switch to a namespace+role index if memory grows past that
    pool = M.retrieve(mem, "", decision_cut=cut, candidate=_NOBODY, viewer_role=title, k=200)
    items = [{"entry_id": e["entry_id"], "known_at": e["known_at"], "text": e["text"]}
             for e in pool if e.get("section") == SECTION and e.get("kind") == "lesson" and e.get("for_role") == role
             and (live is True or e.get("evaluation_derived") is False)][:k]   # default: grade-derived lessons stay shadow until promoted
    return {"state": "known", "items": items}
