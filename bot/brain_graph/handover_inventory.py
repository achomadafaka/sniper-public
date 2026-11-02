"""Read-only, bounded crew brain inventory. No credentials, writes or installation.

Two complete stable scans are an optimistic read, NOT a database snapshot.
The host still compares before/after note writing and must combine this brain
content digest with its complete mail/action/terminal ledger content. Returned
facts are data; membership never authenticates operator prose. The host must
derive role from its authenticated binding, never model note/tool arguments. No embeddings
or fact prose are returned, and cut_id is provenance only.
"""
import hashlib
import json
import uuid

from . import AUTHORS, READ_STATUSES, STATUSES, VERIFIERS
from .crew_filter import office_namespace
from .store import BrainRefused, ENTITY_COLUMNS, FACT_COLUMNS, _pg_in, pg_array


# Fixed legacy migration references only. Prose never chooses or expands targets.
LEGACY_ORDER_REFERENCES=(
    ('fb8b9d24-9fbe-4eed-be12-8ade792a976b','1caba31f-ccc4-4df0-99cb-bcae1cac831e'),
    ('6e6e4761-412c-4bfc-8caa-2570bcd6e525','205ef146-5915-424d-9732-b589c3578493'),
    ('7745bb91-c1ff-4165-bfb7-ca01f8d2ee07','4243b987-ed86-4b5d-ba46-4107f4d33944'),
    ('982b056e-aff2-4eb3-971b-983d881f1d03','96fc14a9-2f60-48c3-87b6-f04b0314b3e9'),
    ('61671fa2-9a62-4492-9ff9-21593d6e22b4','9d3331cd-e425-4e6a-8673-5544aa17358f'),
    ('7ff5bfda-6c31-4f77-be9c-03ac57b7f472','b97052f5-5579-440a-8772-c99f3a8d371a'),
)


class InventoryRefused(BrainRefused):
    pass


def is_open(fact):
    """Inventory membership is broader than eligibility to complete a task."""
    return (isinstance(fact, dict) and fact.get('status') in READ_STATUSES
            and 'valid_to' in fact and fact['valid_to'] is None)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise InventoryRefused('inventory_identity_invalid')
    return value


class _Reader:
    def __init__(self, store, page_size, max_pages, max_bytes):
        if (type(page_size) is not int or not 1 <= page_size <= 500
                or type(max_pages) is not int or not 1 <= max_pages <= 4
                or type(max_bytes) is not int or not 1 <= max_bytes <= 4*1024**2):
            raise InventoryRefused('inventory_budget_invalid')
        self.store, self.page_size, self.max_pages = store, page_size, max_pages
        self.remaining, self.calls = max_bytes, 0

    def fetch(self, table, select, filters):
        rows, after = [], None
        columns = set(select.split(','))
        for _ in range(self.max_pages):
            self.calls += 1
            if self.calls > 32:
                raise InventoryRefused('inventory_request_cap')
            page = self.store.rows_page(table, select=select, filters=filters, after_id=after, limit=self.page_size)
            if not isinstance(page, list) or len(page) > self.page_size:
                raise InventoryRefused('inventory_page_invalid')
            self.remaining -= len(_json(page))
            if self.remaining < 0:
                raise InventoryRefused('inventory_byte_cap')
            if not page:
                return rows
            for row in page:
                if not isinstance(row, dict) or set(row) != columns:
                    raise InventoryRefused('inventory_projection_invalid')
                current = _id(row.get('id'))
                if after is not None and current <= after:
                    raise InventoryRefused('inventory_page_not_advancing')
                after = current
                rows.append(row)
            # A short server page is not proof of exhaustion: only EMPTY ends a scan.
        raise InventoryRefused('inventory_pagination_capped')


def _referenced_orders(reader,orders):
    pairs=LEGACY_ORDER_REFERENCES
    if not isinstance(pairs,(tuple,list)) or len(pairs)>64:
        raise InventoryRefused('inventory_order_reference_cap')
    if any(not isinstance(pair,(tuple,list)) or len(pair)!=2 for pair in pairs):
        raise InventoryRefused('inventory_order_reference_invalid')
    pointers=[_id(p) for p,t in pairs];targets=[_id(t) for p,t in pairs]
    if len(set(pointers))!=len(pointers) or set(pointers)&set(targets):
        raise InventoryRefused('inventory_order_reference_cycle_or_duplicate')
    active={f['id']:f for f in orders}
    ids=set(pointers)|set(targets)
    rows=reader.fetch('brain_facts',FACT_COLUMNS,(('id','in',_pg_in(sorted(ids))),)) if ids else []
    found={f['id']:f for f in rows}
    if not set(found)<=ids:raise InventoryRefused('inventory_order_reference_unexpected')
    bindings=[]
    for pointer,target in pairs:
        # An entirely absent migration pair does not apply to another graph.
        if pointer not in found and target not in found and pointer not in active:continue
        if target not in found:raise InventoryRefused('inventory_order_target_missing')
        if pointer in active and pointer not in found:raise InventoryRefused('inventory_order_pointer_missing')
        if (pointer not in active or pointer not in found) and is_open(found[target]):
            raise InventoryRefused('inventory_order_pointer_missing')
        bindings.append((pointer,target))
    for f in rows:
        if (f.get('kind') not in ('decision','settled') or f.get('author') not in AUTHORS
                or f.get('status') not in STATUSES or 'valid_to' not in f
                or type(f.get('version')) is not int or f['version']<0
                or not isinstance(f.get('entity_ids'),list) or not f['entity_ids']
                or len(set(f['entity_ids']))!=len(f['entity_ids'])):
            raise InventoryRefused('inventory_order_target_invalid')
        if f['id'] in active and active[f['id']]!=f:
            raise InventoryRefused('inventory_order_target_changed')
    return bindings,[found[t] for t in sorted({t for p,t in bindings})],[f for f in rows if f['id'] in pointers]


def _snapshot(reader, role):
    actors = reader.fetch('brain_entities', ENTITY_COLUMNS,
        (('type', 'eq', 'actor'), ('name_lc', 'in', _pg_in((role, 'operator')))))
    by_name = {}
    for entity in actors:
        if (entity.get('type') != 'actor' or entity.get('name_lc') not in (role, 'operator')
                or entity['name_lc'] in by_name):
            raise InventoryRefused('inventory_actor_invalid')
        by_name[entity['name_lc']] = entity
    if set(by_name) != {role, 'operator'}:
        raise InventoryRefused('inventory_actor_missing')
    active = (('status', 'in', _pg_in(READ_STATUSES)), ('valid_to', 'is', 'null'))
    tasks = reader.fetch('brain_facts', FACT_COLUMNS,
        (*active, ('kind', 'eq', 'task'), ('entity_ids', 'cs', pg_array([by_name[role]['id']]))))
    orders = reader.fetch('brain_facts', FACT_COLUMNS,
        (*active, ('kind', 'in', _pg_in(('decision', 'settled'))),
         ('entity_ids', 'cs', pg_array([by_name['operator']['id']]))))
    bindings,referenced,pointer_facts=_referenced_orders(reader,orders)
    refs = {e['id'] for e in actors}
    for fact in referenced+pointer_facts:refs.update(_id(value) for value in fact['entity_ids'])
    for rows, kinds, actor in ((tasks, ('task',), role), (orders, ('decision', 'settled'), 'operator')):
        for fact in rows:
            ids = fact.get('entity_ids')
            if (not is_open(fact) or fact.get('kind') not in kinds or fact.get('author') not in AUTHORS
                    or type(fact.get('version')) is not int or fact['version'] < 0
                    or not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                    or by_name[actor]['id'] not in ids):
                raise InventoryRefused('inventory_fact_invalid')
            refs.update(_id(value) for value in ids)
    if len(refs) > 500:
        raise InventoryRefused('inventory_reference_cap')
    entities = reader.fetch('brain_entities', ENTITY_COLUMNS, (('id', 'in', _pg_in(sorted(refs))),))
    if {e['id'] for e in entities} != refs:
        raise InventoryRefused('inventory_reference_missing')
    for actor in actors:
        if actor != next(e for e in entities if e['id'] == actor['id']):
            raise InventoryRefused('inventory_actor_changed')
    section_ids = {_id(e.get('section_id')) for e in entities}
    sections = reader.fetch('brain_sections', 'id,slug', (('id', 'in', _pg_in(sorted(section_ids))),))
    if {s['id'] for s in sections} != section_ids:
        raise InventoryRefused('inventory_section_missing')
    if any(not isinstance(s.get('slug'), str) or not s['slug'] or office_namespace(s['slug']) for s in sections):
        raise InventoryRefused('inventory_office_or_unknown_provenance')
    return dict(role=role, tasks=tasks, orders=orders, order_references=bindings, referenced_orders=referenced, reference_pointer_facts=pointer_facts, entities=entities, sections=sections)


def build_inventory(store, role, *, page_size=500, max_pages=4, max_bytes=4*1024**2):
    """Return brain-only proof or typed refusal, never a partial/empty fallback.

    Tasks are owned by the linked actor, not necessarily their writing author.
    All READ_STATUSES, judge-pending work and expired verification dates survive.
    Orders include decision AND settled records on actor:operator. Status and
    provenance remain inside the digest; neither is promoted to authority.
    """
    try:
        if not isinstance(role, str) or role.lower() not in set(VERIFIERS)-{'operator'}:
            raise InventoryRefused('inventory_crew_role_invalid')
        role = role.lower()
        reader = _Reader(store, page_size, max_pages, max_bytes)
        before = _json(_snapshot(reader, role))
        snapshot = _snapshot(reader, role)
        after = _json(snapshot)
        if before != after:
            raise InventoryRefused('inventory_content_changed')
        orders={f['id']:f for f in snapshot['orders']+snapshot['referenced_orders'] if is_open(f)}
        orders=[orders[key] for key in sorted(orders)]
        targets={f['id']:f for f in snapshot['referenced_orders']}
        references=[dict(pointer_id=p,id=t,version=targets[t]['version'],status=targets[t]['status'],
                         state='live' if is_open(targets[t]) else 'retired') for p,t in snapshot['order_references']]
        return dict(scope='brain_only', role=role, complete=True, pagination_complete=True,
            cut_id='brain:'+str(uuid.uuid4()), brain_content_digest=hashlib.sha256(after).hexdigest(),
            tasks=[dict(id=f['id'], version=f['version'], owner=role) for f in snapshot['tasks']],
            operator_order_ids=[f['id'] for f in orders],referenced_order_targets=references,
            operator_orders=[dict(id=f['id'], version=f['version'], owner='operator', author=f['author'],
                status=f['status'], judge_pending=f['judge_pending']) for f in orders])
    except InventoryRefused:
        raise
    except Exception as exc:
        # Backend text can contain secrets; unknown/read/shape errors never become empty work.
        raise InventoryRefused('inventory_read_or_shape_failed:'+type(exc).__name__) from None
