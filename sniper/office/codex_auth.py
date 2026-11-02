"""Shared Codex slot and existing identity/CAS writeback around one isolated child."""
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import tempfile

from scripts import memecoin_desk_checker as checker
from sniper.office.model_auth import AuthRefused, _snapshot_directory


def _take_child(child_root, rid):
    fds = []
    try:
        fd = os.open(child_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW);fds.append(fd)
        for name in (rid, 'home', '.codex'):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd);fds.append(fd)
        auth = os.open('auth.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            if not stat.S_ISREG(os.fstat(auth).st_mode):
                raise ValueError()
            with os.fdopen(auth, 'rb', closefd=False) as source:
                raw = source.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError()
        finally:
            os.close(auth)
        os.unlink('auth.json', dir_fd=fd)
        return raw
    except (OSError, ValueError):
        raise AuthRefused('codex_auth_snapshot_missing') from None
    finally:
        for fd in reversed(fds):os.close(fd)


@contextmanager
def snapshot(*, real_home, parent, child_root, private_state, lock_path, completed, alert):
    """`completed` must prove the owned sibling stopped, using the protected launcher receipt/cgroup.

    Unknown cleanup keeps the private recovery copy and child credentials, alerts, and refuses writeback.
    The host must pause this provider on that typed failure; never retry while cleanup is unknown.
    """
    if not callable(completed) or not callable(alert):
        raise AuthRefused('trusted_auth_bindings_required')
    parent, private_state = Path(parent), Path(private_state)
    private_state.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = private_state.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise AuthRefused('auth_private_state')
    try:
        with checker.codex_slot(timeout_s=0, path=Path(lock_path)):
            prepared = Path(tempfile.mkdtemp(prefix='office-auth-', dir=private_state))
            directory = None
            clean = False
            try:
                home, copy = checker.prepare_codex_home(prepared, real_home=real_home)
                if home is None:
                    raise AuthRefused('no_codex_login')
                directory = _snapshot_directory(parent, namespace='.codex')
                fd = os.open('auth.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
                with os.fdopen(fd, 'wb') as target:target.write(copy.read_bytes())
                try:
                    yield
                finally:
                    if completed(parent.name) is not True:
                        alert('codex_cleanup_unproven', parent.name)
                        raise AuthRefused('codex_cleanup_unproven')
                    copy.write_bytes(_take_child(child_root, parent.name))
                    status = checker.sync_auth_back(copy, real_home=real_home, page=lambda _:None)
                    if status.startswith('rejected:'):
                        alert('codex_auth_writeback_refused', parent.name)
                        raise AuthRefused('codex_auth_writeback_refused')
                    alert('codex_auth_' + status.split(':')[0], parent.name)
                    clean = True
            finally:
                if directory is not None:
                    if clean:
                        try:os.unlink('auth.json', dir_fd=directory)
                        except FileNotFoundError:pass
                    os.close(directory)
                if clean:shutil.rmtree(prepared)
    except checker.CodexBusy:
        raise AuthRefused('codex_busy') from None
