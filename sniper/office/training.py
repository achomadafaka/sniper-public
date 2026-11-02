"""Bounded accelerated discovery practice. Host dispatch owns quota/cgroup/cancellation.

No live Office, evaluation, or trading-store handles enter this driver. Archive
frames use the live cut validator; lessons get actual trusted write time, never
the historical replay time. Model text cannot select authority or output labels.
Frames are archive-only practice inputs, not verdict outcomes. The host supplies
the oldest open/planned epoch start; every frame must precede that frozen boundary.
"""
import hashlib
import json
import math

from . import capabilities as C
from .broker import quote_external
from .models import Envelope, _object
from .scheduler import Scheduler
from .training_store import ReplayShadow, ShadowRefused

COUNTER = 'replay_while_live_backlogged'
SCHEMA = dict(type='object', properties={'lesson': {'type':'string'}}, required=['lesson'], additionalProperties=False)


def _finite(v):
    return type(v) in (int,float) and math.isfinite(v) and v >= 0


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(',',':'), ensure_ascii=False, allow_nan=False).encode()


class ReplayFloor:
    def __init__(self, shadow, *, clock, dispatch, backlog, capacity, record_counter, scheduler):
        if not isinstance(shadow, ReplayShadow) or not isinstance(scheduler, Scheduler):
            raise ValueError('training_shadow_and_scheduler_required')
        self.shadow, self.clock, self.dispatch = shadow, clock, dispatch
        self.backlog, self.capacity, self.record_counter = backlog, capacity, record_counter
        self.scheduler = scheduler

    def _backlogged(self):
        try:
            state = self.backlog()
            if not isinstance(state, dict) or set(state) != {'live','d_judge'} or any(type(v) is not bool for v in state.values()):
                return None
            queued = self.scheduler.live_backlogged()
            return (queued or any(state.values())) if type(queued) is bool else None
        except Exception:
            return None

    def run(self, frames, *, actors, run_id, archive_before):
        outputs = []
        def result(state, reason=None):
            return dict(state=state, reason=reason, epoch_kind='discovery', counts_as_evidence=False, outputs=outputs)
        try:
            if (not _finite(archive_before) or not isinstance(frames, list) or len(frames) > 64 or not isinstance(actors, list)
                    or not 1 <= len(actors) <= 24 or not isinstance(run_id, str) or not 1 <= len(run_id) <= 128):
                return result('refused','replay_bounds')
            raw = _canon(dict(frames=frames, actors=actors))
            if len(raw) > 1048576:
                return result('refused','replay_bounds')
            frozen = json.loads(raw)
            frames, actors = frozen['frames'], frozen['actors']
            previous = -1.
            for frame in frames:
                at, cut, rows = frame['at'], frame['cut'], frame['rows']
                if _finite(at) and at >= archive_before:
                    return result('refused','archive_crosses_epoch_start')
                if (not _finite(at) or at <= previous or C.cut_refusal(cut)
                        or cut['receipt_cut'] != at or cut['clock_sample']['corrected_utc'] != at
                        or cut['knowledge_cut'] > at or not isinstance(rows, list) or len(rows) > 512):
                    return result('refused','replay_cut_invalid')
                previous = at
                for row in rows:
                    if not _finite(row.get('known_at')) or row['known_at'] > cut['knowledge_cut']:
                        return result('refused','future_knowledge')
                    quote_external(row['text'], row['source'], row['known_at'])
            for actor in actors:
                if (not isinstance(actor.get('agent_id'), str) or not actor['agent_id']
                        or actor.get('role') not in C.CAPS or actor.get('provider') not in {'claude','codex'}
                        or not isinstance(actor.get('model'), str) or not actor['model']
                        or not isinstance(actor.get('system'), str) or not 1 <= len(actor['system']) <= 32768
                        or actor.get('prompt_hash') != hashlib.sha256(actor['system'].encode()).hexdigest()
                        or type(actor.get('generation')) is not int or actor['generation'] < 1):
                    return result('refused','replay_actor_invalid')
        except (KeyError, TypeError, ValueError, RecursionError):
            return result('refused','replay_input_invalid')
        for index, frame in enumerate(frames):
            for actor in actors:
                backlog = self._backlogged()
                if backlog is not False:
                    return result('paused','live_backlog' if backlog else 'backlog_unknown')
                try:
                    if self.capacity() is not True or not _finite(self.clock()):
                        return result('paused','capacity_or_clock_unknown')
                    key = hashlib.sha256(_canon([run_id,index,actor['agent_id']])).hexdigest()
                    input_hash = hashlib.sha256(_canon([frame,actor,archive_before])).hexdigest()
                    entry = self.shadow.get(key, input_hash)
                    if entry is None:
                        ctx = C.Context(actor['agent_id'], actor['role'], 'training-shadow', actor['generation'], None, frame['cut'])
                        request = dict(context=ctx.__dict__, inputs=[quote_external(row['text'],row['source'],row['known_at']) for row in frame['rows']],
                                       epoch_kind='discovery', counts_as_evidence=False, replay_at=frame['at'])
                        failed = False
                        entered = False
                        def dispatch_replay():
                            nonlocal entered
                            entered = True
                            return self.dispatch(actor['provider'], model=actor['model'],
                                system=actor['system']+'\nPractise on this archived paper desk. Return one lesson; this is discovery, never trading or promotion evidence.',
                                user=_canon(request).decode(), schema=json.loads(_canon(SCHEMA)), deadline_s=30., actor_id=actor['agent_id'], priority='replay')
                        try:
                            answer = self.scheduler.submit(actor['provider'], dispatch_replay, priority='replay', timeout_s=0.)
                        except Exception:
                            failed = True
                        after = self._backlogged()
                        if after is not False:
                            if after is True:
                                if not entered:
                                    return result('paused','live_backlog')
                                recorded = self.record_counter(COUNTER) is True
                                return result('paused', 'replay_ran_while_live_backlogged' if recorded else 'violation_counter_unknown')
                            return result('paused','backlog_unknown')
                        if failed:
                            return result('paused','replay_dependency_unknown')
                        if (not isinstance(answer, Envelope) or answer.outcome != 'success'
                                or answer.requested_provider != actor['provider'] or answer.requested_model != actor['model']
                                or not isinstance(answer.text, str) or len(answer.text) > 16000):
                            return result('paused','replay_model_unknown')
                        lesson = json.loads(answer.text, object_pairs_hook=_object)
                        if not isinstance(lesson, dict) or set(lesson) != {'lesson'}:
                            return result('refused','replay_lesson_invalid')
                        entry = self.shadow.write_lesson(key=key, input_hash=input_hash, text=lesson['lesson'],
                            actor_id=actor['agent_id'], run_id=run_id, replay_at=frame['at'], clock=self.clock)
                    outputs.append(dict(lesson_id=entry['entry_id'], actor_id=actor['agent_id'], replay_at=frame['at'],
                                        epoch_kind='discovery', counts_as_evidence=False))
                except (ShadowRefused, ValueError, TypeError, KeyError):
                    return result('refused','replay_output_invalid')
                except Exception:
                    return result('paused','replay_dependency_unknown')
        return result('completed')
