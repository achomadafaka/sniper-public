"""The dashboard must say when it is not serving the runner's release.

2026-09-03: the web sat 127 commits behind and rendered 'funded contract · unreadable' over four
VALID manual-unmanaged-v1 contracts, using a validation rule the runner had already retired. The
operator closed a funded position two minutes after opening it. The web has its own release root
that memecoin deploys never touch, so it drifts with no signal that it has.
"""
from __future__ import annotations

from sniper.routes.api_memecoin import _release_drift

SHA_A = "a" * 40
SHA_B = "b" * 40


def test_matching_releases_are_not_drift(monkeypatch):
    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api, "_WEB_RELEASE_SHA", SHA_A)
    assert _release_drift({"lifi": {"release_sha": SHA_A}}) == {
        "web": SHA_A, "runner": SHA_A, "match": True}


def test_a_web_behind_the_runner_is_drift(monkeypatch):
    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api, "_WEB_RELEASE_SHA", SHA_A)
    got = _release_drift({"lifi": {"release_sha": SHA_B}})
    assert got["match"] is False and got["web"] == SHA_A and got["runner"] == SHA_B


def test_an_unknown_side_is_never_reported_as_a_match(monkeypatch):
    """A web that cannot name its own release is exactly the state this exists to surface, so it
    must not be silently treated as agreement."""
    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api, "_WEB_RELEASE_SHA", None)
    assert _release_drift({"lifi": {"release_sha": SHA_B}})["match"] is False
    monkeypatch.setattr(api, "_WEB_RELEASE_SHA", SHA_A)
    assert _release_drift({})["match"] is False
    assert _release_drift(None)["match"] is False


def test_a_plain_dev_checkout_stays_quiet(monkeypatch):
    """Neither side pinned -> None, so a local run does not nag at all."""
    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api, "_WEB_RELEASE_SHA", None)
    assert _release_drift({}) is None
    assert _release_drift(None) is None


def test_the_sha_is_resolved_at_import_not_per_request():
    """The web is launched through `.runtime/web/current`, a SYMLINK, so `abspath` never contains
    the SHA — that shipped and reported "unpinned checkout" on a properly pinned deploy.

    `realpath` fixes it, but ONLY at import time, when this process is genuinely executing the
    release `current` points at. Resolving per-request would follow the symlink to whatever a LATER
    deploy moved it to and report the new SHA while this process still served the old code — green
    in exactly the case the guard exists to catch."""
    import inspect, sniper.routes.api_memecoin as api

    body = inspect.getsource(api._release_drift)
    assert "realpath" not in body and "__file__" not in body, (
        "_release_drift must read the cached constant, never re-resolve per call")

    resolver = inspect.getsource(api._resolve_web_release_sha)
    assert "realpath" in resolver, "must follow the symlink, not abspath"

    src = inspect.getsource(api)
    assert src.index("_WEB_RELEASE_SHA = _resolve_web_release_sha()") < src.index(
        "def _release_drift"), "the constant must be resolved at module import"


def test_it_extracts_a_sha_from_a_real_release_path():
    import os, sniper.routes.api_memecoin as api
    ns = {"os": os,
          "__file__": f"/srv/.runtime/web/releases/{SHA_A}/sniper/routes/api_memecoin.py"}
    exec(compile(__import__("inspect").getsource(api._resolve_web_release_sha),
                 "<r>", "exec"), ns)
    assert ns["_resolve_web_release_sha"]() == SHA_A
    ns["__file__"] = "/home/dev/checkout/sniper/routes/api_memecoin.py"
    assert ns["_resolve_web_release_sha"]() is None


def test_a_missing_runner_is_not_asserted_as_drift():
    """Raised by sniper-12 on review. The runner SHA comes from its heartbeat; no heartbeat means we
    could not CHECK, not that the releases differ. `match=False` is still correct — unknown is never
    agreement — but the rendered text must not claim a drift that was never established. The UI
    branches on `!d.runner` before the drift wording."""
    import pathlib
    js = pathlib.Path("static/app.js").read_text()
    i = js.index("release_drift")
    block = js[i:i + 1800]
    assert "if (!d.runner)" in block, "the unknown-runner case needs its own branch"
    assert block.index("if (!d.runner)") < block.index("DASHBOARD IS ON A DIFFERENT RELEASE"), (
        "the unknown-runner branch must come BEFORE the drift assertion")
    assert "cannot confirm" in block


def test_relationship_metadata_is_bound_to_both_actual_release_shas(monkeypatch):
    import sniper.routes.api_memecoin as api
    monkeypatch.setattr(api,'_WEB_RELEASE_SHA',SHA_A)
    monkeypatch.setattr(api,'_WEB_RELEASE_RELATION',dict(state='known',web_sha=SHA_A,runner_sha=SHA_B,runner_is_ancestor=True,web_is_ancestor=False,funded_paths_changed=False))
    r=api._release_drift({'lifi':{'release_sha':SHA_B}})
    assert r['match'] is False and r['relationship']['runner_is_ancestor'] is True
    # Runner changed after this web was deployed: stale metadata must not silence the alarm.
    assert api._release_drift({'lifi':{'release_sha':'c'*40}})['relationship']['state']=='unknown'
