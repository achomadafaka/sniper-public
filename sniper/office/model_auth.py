"""Host-side subscription snapshots. Claude credentials are never written back."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path

from sniper.office.launcher_client import read_regular


class AuthRefused(ValueError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _discard_child(child_root, rid, original):
    descriptors = []
    try:
        fd = os.open(child_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(fd)
        for name in (rid, 'home', '.claude'):
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        file_fd = os.open('.credentials.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            import stat
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ValueError('auth_snapshot_not_regular')
            raw = os.read(file_fd, 1_048_577)
        finally:
            os.close(file_fd)
        os.unlink('.credentials.json', dir_fd=fd)
        return raw != original
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _snapshot_directory(parent, namespace='.claude'):
    """Walk without symlinks; retain the final directory FD through the entire call."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for name in (Path(parent).absolute() / 'home' / namespace).parts[1:]:
            try:
                os.mkdir(name, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise AuthRefused('auth_snapshot_path') from None


@contextmanager
def claude_snapshot(source, parent, *, now, deadline_at, child_root, alert):
    if not callable(alert):
        raise AuthRefused('auth_alert_required')
    try:
        raw = read_regular(source)
        document = json.loads(raw)
        oauth = document['claudeAiOauth']
        expiry = oauth['expiresAt']
        if (not _number(expiry) or not _number(now) or not _number(deadline_at)
                or not 0 < deadline_at - now <= 120
                or type(oauth['accessToken']) is not str or not oauth['accessToken']):
            raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise AuthRefused('claude_auth_unknown') from None
    # Claude's credential expiresAt is milliseconds since Unix epoch; our deadlines are corrected UTC seconds.
    if expiry / 1000. <= deadline_at + 60.:
        raise AuthRefused('claude_auth_near_expiry')
    # RO mounts are defense in depth: a writable parent can be renamed. Never expose a refresh credential.
    allowed = ('accessToken', 'expiresAt', 'scopes', 'subscriptionType', 'rateLimitTier')
    try:
        raw = json.dumps({'claudeAiOauth': {k: oauth[k] for k in allowed if k in oauth}}, allow_nan=False).encode()
    except (ValueError, TypeError):
        raise AuthRefused('claude_auth_unknown') from None
    parent = Path(parent)
    directory = _snapshot_directory(parent)
    try:
        fd = os.open('.credentials.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o400, dir_fd=directory)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
    except OSError:
        os.close(directory)
        raise AuthRefused('auth_snapshot_path') from None
    try:
        yield
    finally:
        # The launcher additionally mounts this file read-only. Detect unexpected changes without accepting them.
        changed = _discard_child(child_root, parent.name, raw)
        try:
            os.unlink('.credentials.json', dir_fd=directory)
        except FileNotFoundError:
            pass
        finally:
            os.close(directory)
        if changed:
            alert('claude_auth_child_rotated', parent.name)
