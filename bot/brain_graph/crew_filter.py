"""Exclude Office provenance from crew reads, regardless of the web-taint label.

Office facts must link only Office-section entities; episodes must carry an
``office:<actor>:<event-id>`` source_ref. Legacy unmarked crew episodes still read.
"""
from bot.brain_graph.store import BrainUnavailable, _pg_in

SNAPSHOT_POLICY = 'crew-office-exclusion-v1'


def office_namespace(value):
    value = str(value or '').strip().lower()
    return value == 'office' or value.startswith(('office:', 'office/'))


class CrewFilter:
    def __init__(self, store):
        self.store = store
        self.trusted_sections = {s['id'] for s in store.sections() if not office_namespace(s['slug'])}

    def facts(self, rows):
        ids = sorted({e for r in rows for e in r.get('entity_ids') or []})
        trusted = set()
        # Resolve returned rows, not an Office index taken before concurrent writes.
        for start in range(0, len(ids), 100):
            chunk, after = ids[start:start + 100], None
            for _ in range(101):
                entities = self.store.rows_page('brain_entities', select='id,section_id',
                    filters=(('id', 'in', _pg_in(chunk)),), after_id=after, limit=100)
                if not entities:
                    break
                if after is not None and entities[-1]['id'] <= after:
                    raise BrainUnavailable('crew provenance paging did not advance')
                trusted.update(e['id'] for e in entities if e.get('section_id') in self.trusted_sections)
                after = entities[-1]['id']
            else:
                raise BrainUnavailable('crew provenance exceeds bounded read')
        return [r for r in rows if r.get('entity_ids') and set(r['entity_ids']) <= trusted]

    @staticmethod
    def episodes(rows):
        return [r for r in rows if not office_namespace(r.get('source_ref'))
                and not office_namespace(r.get('author'))]
