"""The operator's real `.env` must not be present in a test process's environment.

NON-ENUMERATED ON PURPOSE. A guard that pins three named constants to their defaults is a list
someone has to maintain, and it reopens the moment a knob is added — the same objection that ruled
out fixing this by pinning constants. This reads the KEY NAMES out of the real env files at test
time and asserts none of them is in `os.environ`, so it covers the next knob nobody has hit yet and
names the specific offender when it fires.

NAMES ONLY. A value is never read, compared or printed here: the file holds live credentials.

THE BUG IT GUARDS. `.env` sets `MEMECOIN_REAL_GLOBAL_FEED_ENTRIES=0`. Around ten test files call
`load_dotenv`/`load_env_files`, which puts it into `os.environ`; settings reads it AT IMPORT into a
module constant; the runner then refuses every `global_feed` entry. On one 101-file chunk that was
16 of 18 failures, in files that never mention the flag. Worse under `-n0` than under xdist, because
one load poisons every module imported after it in the same process.
"""
import os
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ENV_FILES = [_REPO_ROOT / ".env", _REPO_ROOT / ".fomo.env"]


def _fingerprints(path):
    """{key: sha256(value)[:16]} for one env file.

    A HASH, NOT A VALUE. The guard has to answer "did the OPERATOR'S value arrive", because
    `tests/conftest.py` deliberately re-sets several of these names to SAFE values — blanking
    `SUPABASE_*`, pinning `MEMECOIN_REAL_ENABLED=0`, redirecting the kill switch — and a
    name-only check calls each of those a leak. Comparing fingerprints separates "conftest chose
    this" from "the operator's file arrived", and no value is ever read into an assertion, a
    message or a log. The file holds live credentials.
    """
    import hashlib
    out = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = hashlib.sha256(v.strip().strip('"').strip("'").encode()).hexdigest()[:16]
    return out


def _key_names(path):
    try:
        text = path.read_text()
    except OSError:
        return set()
    return {line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#") and "=" in line}


def _pinned_names():
    """PINNED_MEMECOIN_ENV's keys, parsed from tests/conftest.py rather than imported.

    `from conftest import ...` resolves to whichever conftest pytest registered LAST under that
    name. In a session that also collects tests/ink (the deploy gate selects 9 of its files when
    settings/memecoin.py changes), that is tests/ink/conftest.py, the import failed, the old
    fallback treated it as "nothing pinned", and MEMECOIN_REAL_TP_PCT read as a leak (2026-10-07).
    A missing dict now fails loudly instead of silently emptying the exclusions."""
    import ast
    tree = ast.parse(pathlib.Path(__file__).with_name("conftest.py").read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(getattr(t, "id", None) == "PINNED_MEMECOIN_ENV" for t in node.targets)):
            return {k.value for k in node.value.keys if isinstance(k, ast.Constant)}
    raise AssertionError("tests/conftest.py has no literal PINNED_MEMECOIN_ENV dict")


def _leaked():
    """Key names whose LIVE value fingerprint matches the operator file's. Names only come back."""
    import hashlib
    fps = {}
    for f in _ENV_FILES:
        fps.update(_fingerprints(f))
    if not fps:
        pytest.skip("no operator env file in this checkout — nothing to leak")
    # Names conftest pins ON PURPOSE are not leaks even when the operator file happens to agree.
    # `MEMECOIN_REAL_TP_PCT` is 0 in `.env`, 0 in conftest and 0 in the code default, so a
    # fingerprint match there says nothing. Read from conftest rather than restating the list, so
    # a new pin excludes itself and this guard stays non-enumerated.
    _pinned = _pinned_names()
    out = []
    for key, want in fps.items():
        if key in _pinned:
            continue
        live = os.environ.get(key)
        if live is None:
            continue
        if hashlib.sha256(live.encode()).hexdigest()[:16] == want:
            out.append(key)
    return sorted(out)


def test_no_operator_env_value_reaches_the_test_process():
    leaked = _leaked()
    assert not leaked, (
        "the operator's real env values reached this test process: " + ", ".join(leaked) +
        ". A value read at IMPORT time into a settings constant then changes behaviour in files "
        "that never mention it. See the neutralisation in tests/conftest.py.")


def test_the_specific_value_that_caused_this_is_covered():
    """A named regression case, kept because the general assertion above cannot say WHY it exists.

    Not a substitute for it — if this name is ever removed from `.env`, the general guard still
    holds the line for every other key.
    """
    if "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES" not in set().union(*(set(_fingerprints(f)) for f in _ENV_FILES)):
        pytest.skip("the original carrier is no longer in the operator env")
    assert "MEMECOIN_REAL_GLOBAL_FEED_ENTRIES" not in os.environ


def test_the_settings_constant_reads_its_code_default():
    """The consequence, asserted where a reader will understand it.

    `sniper/settings/memecoin.py` defaults this to true; the operator's `.env` sets it to 0. If the
    suite ever sees false here, the environment leaked before settings was imported.
    """
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_REAL_GLOBAL_FEED_ENTRIES is True
