#!/usr/bin/env python3
"""Install crew code only; preserve mailbox/routes and never start a service."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile

MODULES = ('crew_session_bridge.py', 'crew_session_start.py')


def checked(path, kind, *, private=False):
    info = path.lstat()
    predicate = stat.S_ISDIR if kind == 'dir' else stat.S_ISREG
    if (not predicate(info.st_mode) or info.st_uid != os.getuid()
            or private and info.st_mode & 0o077):
        raise ValueError('unsafe install path')


def directory(path, *, private=False):
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    checked(path, 'dir', private=private)
    return path


def atomic_write(path, data, mode=0o600):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def atomic_link(path, target):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    os.close(fd)
    temp = Path(name)
    temp.unlink()
    try:
        temp.symlink_to(target)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def release(base, contents):
    digest = hashlib.sha256()
    for name in MODULES:
        digest.update(name.encode() + b'\0' + contents[name] + b'\0')
    ident = digest.hexdigest()
    root = directory(base / 'releases', private=True)
    target = directory(root / ident, private=True)
    for name in MODULES:
        path = target / name
        if path.exists() or path.is_symlink():
            checked(path, 'file', private=True)
            if path.read_bytes() != contents[name]:
                raise ValueError('release content mismatch')
        else:
            atomic_write(path, contents[name])
    return ident


def validate_release(base, ident):
    if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{64}', ident):
        raise ValueError('invalid release identity')
    target = base / 'releases' / ident
    checked(target, 'dir', private=True)
    contents = {}
    for name in MODULES:
        checked(target / name, 'file', private=True)
        contents[name] = (target / name).read_bytes()
    if release(base, contents) != ident:
        raise ValueError('release content mismatch')


def manifest(base):
    path = base / 'installed.json'
    if not path.exists() and not path.is_symlink():
        return None
    checked(path, 'file', private=True)
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or set(data) != {'active', 'previous'}:
        raise ValueError('invalid installed manifest')
    validate_release(base, data['active'])
    if data['previous'] is not None:
        validate_release(base, data['previous'])
    return data


def switch(base, wrapper, active, previous):
    validate_release(base, active)
    old_wrapper = wrapper.read_bytes() if wrapper.exists() else None
    old_target = os.readlink(base / 'current') if (base / 'current').is_symlink() else None
    data = {'active': active, 'previous': previous}
    try:
        atomic_link(base / 'current', 'releases/' + active)
        for name in MODULES:
            atomic_link(base / name, 'current/' + name)
        script = base / 'releases' / active / MODULES[0]
        body = '#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(script)) + ' "$@"\n'
        atomic_write(wrapper, body.encode(), 0o700)
        atomic_write(base / 'installed.json', (json.dumps(data) + '\n').encode())
    except BaseException:
        if old_target is not None:
            atomic_link(base / 'current', old_target)
        else:
            (base / 'current').unlink(missing_ok=True)
            for name in MODULES:
                if (base / name).is_symlink():
                    (base / name).unlink()
        if old_wrapper is not None:
            atomic_write(wrapper, old_wrapper, 0o700)
        else:
            wrapper.unlink(missing_ok=True)
        raise
    return data


def install(source, home=None, *, rollback_only=False):
    home = Path.home() if home is None else Path(home)
    source = Path(source)
    contents = {}
    if not rollback_only:
        for name in MODULES:
            checked(source / name, 'file')
            contents[name] = (source / name).read_bytes()
    directory(home)
    local = directory(home / '.local')
    lib = directory(local / 'lib')
    bindir = directory(local / 'bin')
    base = directory(lib / 'sniper-crew', private=True)
    wrapper = bindir / 'sniper-crew'
    if wrapper.exists() or wrapper.is_symlink():
        checked(wrapper, 'file', private=True)
    if (base / 'current').exists() and not (base / 'current').is_symlink():
        raise ValueError('unsafe current release path')
    lock_path = base / 'install.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        checked(lock_path, 'file', private=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
        old = manifest(base)
        if rollback_only:
            if old is None or old['previous'] is None:
                raise ValueError('no previous crew release')
            return switch(base, wrapper, old['previous'], old['active'])
        if old is None:
            legacy = {}
            for name in MODULES:
                path = base / name
                if path.exists() or path.is_symlink():
                    checked(path, 'file', private=True)
                    legacy[name] = path.read_bytes()
            if legacy and len(legacy) != len(MODULES):
                raise ValueError('incomplete legacy crew installation')
            prior = release(base, legacy) if legacy else None
            # Establish a recoverable legacy target before switching code.
            if prior is not None:
                atomic_link(base / 'current', 'releases/' + prior)
                atomic_write(base / 'installed.json',
                             (json.dumps({'active': prior, 'previous': None}) + '\n').encode())
        else:
            prior = old['active']
        active = release(base, contents)
        if old is not None and old['active'] == active:
            return switch(base, wrapper, active, old['previous'])
        return switch(base, wrapper, active, prior)


def rollback(home=None):
    return install(Path(__file__).parent, home, rollback_only=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    try:
        result = rollback() if args.rollback else install(Path(__file__).parent)
        print(json.dumps(result))
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Crew code installation refused: {exc}\n')
