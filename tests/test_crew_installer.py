"""Crew code installation never replaces the private mailbox or starts services."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


def installer():
    path = Path(__file__).resolve().parents[1] / 'tools/crew/install.py'
    spec = importlib.util.spec_from_file_location('crew_installer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(tmp_path, version):
    root = tmp_path / ('source-' + version)
    root.mkdir()
    (root / 'crew_session_bridge.py').write_text(
        'from crew_session_start import VERSION\nprint(VERSION)\n')
    (root / 'crew_session_start.py').write_text(f'VERSION = {version!r}\n')
    return root


def test_install_preserves_state_and_runs_exact_release(tmp_path):
    home = tmp_path / 'home'
    state = home / '.cache/sniper-crew'
    state.mkdir(parents=True)
    (state / 'mailbox.db').write_bytes(b'unchanged mailbox')
    (state / 'context.json').write_text('{"private": "unchanged"}')
    result = installer().install(source(tmp_path, 'one'), home)
    wrapper = home / '.local/bin/sniper-crew'
    assert subprocess.check_output([str(wrapper)], text=True).strip() == 'one'
    base = home / '.local/lib/sniper-crew'
    assert (base / 'current').resolve().name == result['active']
    assert (base / 'crew_session_bridge.py').resolve().parent.name == result['active']
    assert wrapper.stat().st_mode & 0o777 == 0o700
    assert (state / 'mailbox.db').read_bytes() == b'unchanged mailbox'
    assert (state / 'context.json').read_text() == '{"private": "unchanged"}'


def test_repeat_install_retains_rollback_and_rollback_restores_code(tmp_path):
    module = installer()
    home = tmp_path / 'home'
    one = module.install(source(tmp_path, 'one'), home)
    two_source = source(tmp_path, 'two')
    two = module.install(two_source, home)
    assert two['previous'] == one['active']
    assert module.install(two_source, home) == two
    result = module.rollback(home)
    assert result['active'] == one['active']
    assert subprocess.check_output([str(home / '.local/bin/sniper-crew')], text=True).strip() == 'one'


def test_repeat_install_repairs_modified_wrapper_without_losing_previous(tmp_path):
    module = installer()
    home = tmp_path / 'home'
    module.install(source(tmp_path, 'one'), home)
    src = source(tmp_path, 'two')
    expected = module.install(src, home)
    wrapper = home / '.local/bin/sniper-crew'
    wrapper.write_text('#!/bin/sh\necho WRONG\n')
    assert module.install(src, home) == expected
    assert subprocess.check_output([str(wrapper)], text=True).strip() == 'two'


def test_existing_legacy_modules_are_preserved_as_rollback(tmp_path):
    home = tmp_path / 'home'
    base = home / '.local/lib/sniper-crew'
    base.mkdir(parents=True, mode=0o700)
    old = source(tmp_path, 'legacy')
    for path in old.iterdir():
        (base / path.name).write_bytes(path.read_bytes())
        (base / path.name).chmod(0o600)
    result = installer().install(source(tmp_path, 'new'), home)
    assert result['previous']
    installer().rollback(home)
    assert subprocess.check_output([str(home / '.local/bin/sniper-crew')], text=True).strip() == 'legacy'


def test_symlinked_install_root_refuses_without_outside_writes(tmp_path):
    home = tmp_path / 'home'
    parent = home / '.local/lib'
    parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (parent / 'sniper-crew').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='unsafe'):
        installer().install(source(tmp_path, 'one'), home)
    assert not list(outside.iterdir())


def test_source_symlink_and_unsafe_install_directory_refuse(tmp_path):
    src = source(tmp_path, 'one')
    (src / 'crew_session_start.py').unlink()
    (src / 'crew_session_start.py').symlink_to(src / 'crew_session_bridge.py')
    with pytest.raises(ValueError, match='unsafe'):
        installer().install(src, tmp_path / 'home')
    src = source(tmp_path, 'two')
    base = tmp_path / 'home/.local/lib/sniper-crew'
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o777)
    with pytest.raises(ValueError, match='unsafe'):
        installer().install(src, tmp_path / 'home')


def test_rollback_refuses_corrupt_release_identity(tmp_path):
    module = installer()
    home = tmp_path / 'home'
    module.install(source(tmp_path, 'one'), home)
    manifest = home / '.local/lib/sniper-crew/installed.json'
    manifest.write_text(json.dumps({'active': 'x', 'previous': '../../outside'}))
    with pytest.raises(ValueError):
        module.rollback(home)


def test_wrapper_switch_failure_restores_prior_release(tmp_path, monkeypatch):
    module = installer()
    home = tmp_path / 'home'
    first = module.install(source(tmp_path, 'one'), home)
    wrapper = home / '.local/bin/sniper-crew'
    real_write = module.atomic_write
    fired = False

    def fail_once(path, data, mode=0o600):
        nonlocal fired
        if Path(path) == wrapper and not fired:
            fired = True
            raise OSError('simulated wrapper switch failure')
        return real_write(path, data, mode)

    monkeypatch.setattr(module, 'atomic_write', fail_once)
    with pytest.raises(OSError, match='simulated'):
        module.install(source(tmp_path, 'two'), home)
    assert subprocess.check_output([str(wrapper)], text=True).strip() == 'one'
    base = home / '.local/lib/sniper-crew'
    assert (base / 'current').resolve().name == first['active']
    assert json.loads((base / 'installed.json').read_text())['active'] == first['active']


@pytest.mark.parametrize('legacy', [False, True])
def test_failed_first_install_can_be_retried(tmp_path, monkeypatch, legacy):
    module = installer()
    home = tmp_path / 'home'
    if legacy:
        base = home / '.local/lib/sniper-crew'
        base.mkdir(parents=True, mode=0o700)
        for path in source(tmp_path, 'legacy').iterdir():
            (base / path.name).write_bytes(path.read_bytes())
            (base / path.name).chmod(0o600)
    real_write = module.atomic_write

    def fail_wrapper(path, data, mode=0o600):
        if Path(path) == home / '.local/bin/sniper-crew':
            raise OSError('first wrapper failure')
        return real_write(path, data, mode)

    src = source(tmp_path, 'new')
    monkeypatch.setattr(module, 'atomic_write', fail_wrapper)
    with pytest.raises(OSError, match='first wrapper failure'):
        module.install(src, home)
    monkeypatch.setattr(module, 'atomic_write', real_write)
    module.install(src, home)
    assert subprocess.check_output([str(home / '.local/bin/sniper-crew')], text=True).strip() == 'new'
    if legacy:
        module.rollback(home)
        assert subprocess.check_output([str(home / '.local/bin/sniper-crew')], text=True).strip() == 'legacy'
