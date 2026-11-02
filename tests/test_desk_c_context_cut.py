"""C context receives the same validated cut facts serialized for the model."""
import copy

import pytest

import test_desk_v28_judge_inputs as V
from sniper.services import memecoin_desk_context_facts as C
from test_memecoin_desk_judge import databases  # noqa: F401


@pytest.mark.parametrize("gated", [False, True])
def test_context_cut_forwarding_is_policy_bound(databases, tmp_path, monkeypatch, gated):
    policy = V.v28_policy()
    if gated:
        policy["context"]["ps_trending"] = {
            "version": "desk-ps-trending-v1", "definition_sha256": "f" * 64}
    monkeypatch.setattr(V, "v28_policy", lambda: copy.deepcopy(policy))
    tape = tmp_path / "tape"
    V.ready(databases, tape)
    seen, calls = [], []
    context = C.unknown_block("no_source")
    if gated:
        def context_fn(ticket, candidate, ts, *, ps_facts=None):
            seen.append((candidate, ts, ps_facts))
            return context
    else:
        def context_fn(ticket, candidate, ts):
            seen.append((candidate, ts, None))
            return context

    V.run(databases, tape, calls=calls, context_fn=context_fn)
    assert len(calls) == len(seen) == 1
    assert calls[0]["ctx"] == context
    candidate, ts, facts = seen[0]
    if gated:
        assert isinstance(facts, dict)
        assert facts == calls[0]["ps_facts"]
        assert facts["snapshot_hash"] == candidate["snapshot"]["hash"]
        assert facts["as_of"] == ts
        assert facts["receipt_cut"] == V.NOW < ts
        assert facts["cut_slot"] == candidate["snapshot"]["fields"]["pool_state"]["value"]["cut_slot"]
    else:
        assert facts is None
