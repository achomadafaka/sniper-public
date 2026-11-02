"""Codex requested_pinned identity: accepted in discovery and in the eval-1 runtime, never elsewhere; basis preserved."""
from sniper.office import models, pilot_models as PM

MANIFEST = {'seats': {'judge_gpt': {'provider': 'codex', 'model': 'gpt-6-luna'}, 'scout': {'provider': 'codex', 'model': 'gpt-6-luna'}, 'judge_claude': {'provider': 'claude', 'model': 'claude-opus-5-5'}}}


def run(monkeypatch, seat='judge_gpt', basis='requested_pinned', **kw):
    env = models.Envelope(requested_model='m', requested_provider='p', outcome='success', served_model=None,
                          served_model_basis=basis, text='{}')
    monkeypatch.setattr(PM.models, 'call', lambda *a, **k: env)
    return PM.call(MANIFEST, seat, 's', 'u', deadline_s=1, schema_obj={}, clock=lambda: 1., transports={'codex': None, 'claude': None}, **kw)


def test_eval1_runtime_accepts_pinned_codex_and_keeps_the_basis(monkeypatch):
    out = run(monkeypatch, evaluated_pinned=True)
    assert out.outcome == 'success' and out.served_model == 'gpt-6-luna' and out.served_model_basis == 'requested_pinned'


def test_discovery_still_accepts_and_neither_flag_refuses(monkeypatch):
    assert run(monkeypatch, discovery_only=True).served_model == 'gpt-6-luna'
    out = run(monkeypatch)
    assert out.outcome == 'invalid' and out.error_reason == 'served_model_unknown'


def test_never_for_claude_or_an_unknown_basis(monkeypatch):
    assert run(monkeypatch, seat='judge_claude', evaluated_pinned=True).outcome == 'invalid'
    assert run(monkeypatch, basis='unknown', evaluated_pinned=True).outcome == 'invalid'


def test_eval1_pinned_identity_is_for_the_gpt_judge_seat_only(monkeypatch):
    assert run(monkeypatch, seat='scout', evaluated_pinned=True).outcome == 'invalid'
