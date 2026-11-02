"""Per seat, against every PASS ticket: what its own filter hid, what it saw, what it never listed. Read-only, exploratory. Never per book: filters belong to seats."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

HORIZON_S = 300


def _ro(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1.)
    return conn


def report(*, store_path, desk_path, outcomes_path, epoch_id, since_iso):
    """A hidden ticket stays a feed opportunity: the PASS list here is the full feed, not any seat's view. Unknown outcomes stay unknown."""
    with closing(_ro(desk_path)) as desk:
        passes = {}
        for ticket, raw in desk.execute("SELECT ticket_id,substr(inputs_json,1,65536) FROM desk_tickets WHERE stage='hard_filter' AND verdict='PASS' AND ts>=?", (since_iso,)):
            try: passes[ticket] = json.loads(raw)['evidence'].get('mint')
            except (ValueError, TypeError, KeyError): passes[ticket] = None
    with closing(_ro(store_path)) as store:
        seen, hidden, filters, ordered = {}, {}, {}, {}
        for actor, ticket in store.execute('SELECT actor_id,ticket_id FROM office_v2_feed_seen WHERE epoch_id=?', (epoch_id,)): seen.setdefault(actor, set()).add(ticket)
        for actor, ticket in store.execute('SELECT actor_id,ticket_id FROM office_v2_feed_hidden WHERE epoch_id=?', (epoch_id,)): hidden.setdefault(actor, set()).add(ticket)
        for actor, n, cases in store.execute('SELECT actor_id,count(*),sum(after_case IS NOT NULL) FROM office_v2_feed_filters WHERE epoch_id=? GROUP BY 1', (epoch_id,)): filters[actor] = dict(changes=n, after_a_case=cases)
        for actor, raw in store.execute('SELECT actor_id,labels_json FROM office_v2_order_labels WHERE epoch_id=?', (epoch_id,)):
            if json.loads(raw).get('hidden_from_seat') is True: ordered[actor] = ordered.get(actor, 0) + 1
    outcomes = {}
    try:
        with closing(_ro(outcomes_path)) as conn:
            for mint, collapsed, ret in conn.execute("SELECT mint,collapsed,ret FROM outcome_horizons WHERE horizon_s=? AND status='ok'", (HORIZON_S,)): outcomes[mint] = (collapsed, ret)
    except (OSError, sqlite3.Error): pass
    seats = {}
    for actor in set(seen) | set(hidden) | set(filters):
        hid = hidden.get(actor, set()) & set(passes)
        known = [outcomes[passes[t]] for t in hid if passes[t] in outcomes]
        seats[actor] = dict(pass_tickets=len(passes), hidden=len(hid), listed_visible=len((seen.get(actor, set()) & set(passes)) - hid), not_listed=len(set(passes) - seen.get(actor, set()) - hid),
                            orders_on_hidden_tickets=ordered.get(actor, 0), filters=filters.get(actor, dict(changes=0, after_a_case=0)),
                            hidden_outcomes=dict(horizon_s=HORIZON_S, known=len(known), unknown=len(hid) - len(known), collapsed=sum(c == 1 for c, _ in known)))
    return dict(state='known', labelled='exploratory, no inference', epoch_id=epoch_id, since=since_iso, seats=seats)


if __name__ == '__main__':
    import sys
    print(json.dumps(report(store_path=sys.argv[1], desk_path=sys.argv[2], outcomes_path=sys.argv[3], epoch_id=sys.argv[4], since_iso=sys.argv[5]), indent=1))
