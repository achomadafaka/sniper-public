"""Crew-pinned four-seat routing; unknown served identity grants no pilot action."""
from dataclasses import replace
import hashlib
import json

from sniper.office.launcher_client import read_regular
from sniper.office import models


PROVIDERS={'head':'claude','scout':'claude','judge_claude':'claude','judge_gpt':'codex'}
EVAL1_PROVIDERS={**PROVIDERS,'postmortem':'claude'}
EVAL1_PINNED_SEATS=frozenset({'judge_gpt'})   # prereg Am13: labelled requested_pinned identity, GPT Judge only, frozen   # office-paper-eval-1 adds the Postmortem seat


def load(path, expected_sha256):
    raw=read_regular(path)
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('pilot_models_unapproved')
    doc=json.loads(raw)
    if (type(doc)is not dict or set(doc)!={'version','seats'} or doc['version']!='office-pilot-models-v1'
            or type(doc['seats'])is not dict or set(doc['seats']) not in (set(PROVIDERS),set(EVAL1_PROVIDERS))):
        raise ValueError('pilot_model_seats')
    for seat in doc['seats']:
        provider=EVAL1_PROVIDERS[seat];entry=doc['seats'][seat]
        prefix='claude-' if provider=='claude' else 'gpt-'
        if (type(entry)is not dict or set(entry)!={'provider','model'} or entry['provider']!=provider
                or type(entry['model'])is not str or not entry['model'].startswith(prefix)):
            raise ValueError('pilot_model_family')
    return doc


def call(manifest, seat, system, user, *, deadline_s, schema_obj, clock, transports, discovery_only=False, evaluated_pinned=False):
    """evaluated_pinned: the eval-1 runtime accepts codex's requested_pinned identity exactly as discovery does
    (served_model = the pinned manifest model, basis stays requested_pinned so the record never claims an echo)."""
    entry=manifest['seats'][seat]
    envelope=models.call(entry['provider'],entry['model'],system,user,deadline_s=deadline_s,
                         schema_obj=schema_obj,clock=clock,transport=transports[entry['provider']])
    if envelope.outcome=='success' and envelope.served_model is None:
        if ((discovery_only is True or (evaluated_pinned is True and seat in EVAL1_PINNED_SEATS))
                and entry['provider']=='codex' and envelope.served_model_basis=='requested_pinned'):
            return replace(envelope,served_model=entry['model'])
        return replace(envelope,outcome='invalid',error_reason='served_model_unknown',text=None)
    return envelope
