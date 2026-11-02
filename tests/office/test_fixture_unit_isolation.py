"""Offline command interception only; this gate never starts a systemd unit."""
from pathlib import Path
from types import SimpleNamespace
import re

import pytest
from office import test_child_launcher as child
from office import test_launcher_manager_unit as manager


def private_slice(argv):
    values=[a.split('=',1)[1] if a.startswith('--slice=') else a.split('=',2)[2] for a in argv if a.startswith(('--slice=','--property=Slice='))]
    assert len(values)==1
    name=values[0]
    assert re.fullmatch(r'office_test_[0-9a-f]{32}\.slice',name),name
    assert name!='office.slice' and '-' not in name
    return name


@pytest.mark.parametrize('adapter,expected',[('claude_sdk','denied'),('codex_exec','writable')])
def test_actual_auth_fixture_command_is_private_without_executing_it(tmp_path,monkeypatch,adapter,expected):
    fn=child.test_actual_sibling_auth_mount_permissions
    # Refuse before calling a fixture that imports its own unmockable subprocess runner.
    assert 'subprocess' not in fn.__code__.co_varnames
    class Captured(Exception):pass
    def no_spawn(argv,**kwargs):
        if argv[0].endswith('systemctl'):
            assert argv[1:3]==['--user','stop'] and re.fullmatch(r'office_test_[0-9a-f]{32}\.slice',argv[3])
            return SimpleNamespace(returncode=0)
        private_slice(argv)
        raise Captured()
    monkeypatch.setattr(child,'subprocess',SimpleNamespace(run=no_spawn))
    with pytest.raises(Captured):fn(tmp_path,adapter,expected)


def test_real_launcher_parent_command_is_not_in_the_live_office_slice():
    private_slice(manager.fixture_command(Path('/home/fixture'),'fixture-unit',[]))


def test_flat_fixture_slice_guard_rejects_live_and_hierarchical_names():
    from office.fixture_units import isolated_command
    for name in ('office.slice','office-test-123.slice','app.slice'):
        with pytest.raises(ValueError):isolated_command(['systemd-run','--slice=office.slice'],slice_name=name)
    private_slice(isolated_command(['systemd-run','--slice=office.slice']))


def test_embedded_child_probe_has_an_isolation_fence_before_spawn():
    assert 'isolated_command(child_command(' in manager.PROBE
    assert "assert '/office.slice/' not in proof['cgroup']" in manager.PROBE
