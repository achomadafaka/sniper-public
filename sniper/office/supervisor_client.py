"""Credential-free supervisor transport over the launcher's protected read-only socket directory."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct

from sniper.office.launcher_client import Client, Transport, read_regular


class Endpoint:
    def __init__(self, path, *, expected_uid):
        if type(expected_uid) is not int or expected_uid < 0:
            raise ValueError('launcher_uid')
        self.path, self.expected_uid = Path(path), expected_uid
        if not self.path.is_absolute() or '..' in self.path.parts:
            raise ValueError('launcher_socket_path')

    def __call__(self, conn):
        try:
            parent = self.path.parent
            if any(p.is_symlink() for p in (self.path, parent, *parent.parents)):
                return False
            directory, endpoint = parent.stat(), self.path.lstat()
            if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
                    or directory.st_uid != self.expected_uid or endpoint.st_uid != self.expected_uid
                    or not stat.S_ISSOCK(endpoint.st_mode)
                    or not os.statvfs(parent).f_flag & os.ST_RDONLY
                    or conn.getpeername() != str(self.path)):
                return False
            _, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return uid == self.expected_uid
        except (OSError, ValueError, TypeError, struct.error):
            return False


@contextmanager
def no_credentials(adapter, parent):
    yield


def build(config_path, *, expected_sha256, clock):
    """The external pin and rendered RO socket mount are operator-controlled, never model inputs."""
    raw = read_regular(config_path)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('supervisor_transport_config_unapproved')
    config = json.loads(raw)
    peer = Endpoint(config['socket_path'], expected_uid=config['crew_uid'])
    client = Client({'socket_path': config['socket_path'], 'request_root': config['request_root']},
                    clock=clock, auth=no_credentials, verify_server=peer)
    return {provider: Transport(provider, client, clock=clock) for provider in ('claude', 'codex')}
