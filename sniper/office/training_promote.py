"""Explicit crew-only promotion; replay execution never imports this authority."""
import math

from . import memory as M, memory_store as MS, roster as R, store as S
from .training_store import ReplayShadow, ShadowRefused


def promote_lessons(shadow, office, *, entry_ids, clock, limits, by, launcher_check):
    if by != 'crew' or launcher_check(by) is not True:
        raise ShadowRefused('promotion_not_authorized')
    if (not isinstance(shadow, ReplayShadow) or not isinstance(entry_ids, list)
            or not 1 <= len(entry_ids) <= 64 or len(set(entry_ids)) != len(entry_ids)
            or office is shadow.conn or office.in_transaction):
        raise ShadowRefused('promotion_scope_invalid')
    purpose = office.execute('SELECT purpose FROM _store_info WHERE id=1').fetchone()
    marker = office.execute("SELECT 1 FROM sqlite_master WHERE name='_training_info'").fetchone()
    if purpose is None or purpose[0] != 'office' or marker is not None:
        raise ShadowRefused('promotion_target_invalid')
    entries = shadow.mem.get(entry_ids)
    if len(entries) != len(entry_ids):
        raise ShadowRefused('promotion_lesson_missing')
    for row in entries.values():
        if (row.get('epoch_kind') != 'discovery' or row.get('counts_as_evidence') is not False
                or row.get('replay_derived') is not True):
            raise ShadowRefused('promotion_source_invalid')
    with S.write_transaction(office):
        mem = MS.OfficeMemory(office)
        written = []
        for identity in entry_ids:
            source = entries[identity]
            row = {key:value for key,value in source.items() if key not in {'entry_id','known_at'}}
            row.update(section='office/playbook', kind='playbook', promoted_from=identity,
                       source_known_at=source['known_at'], replay_promoted=True)
            def actual_clock():
                value = clock()
                if type(value) not in (int,float) or not math.isfinite(value) or value < source['known_at']:
                    raise ShadowRefused('promotion_clock_unknown')
                return value
            new_id = M.write(mem, actual_clock, row)
            written.append(mem.get([new_id])[new_id]['known_at'])
        eid = R.change_org(office, limits, clock=clock,
                           change={'kind':'memory_promotion','entry_ids':entry_ids},
                           by=by, reason='explicit replay shadow promotion')
        epoch = office.execute('SELECT payload_json FROM epochs WHERE id=?', (eid,)).fetchone()
        import json
        if json.loads(epoch[0])['opened_at'] <= max(written):
            raise ShadowRefused('promotion_clock_not_advanced')
        return eid
