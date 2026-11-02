"""Synthetic launcher-policy authority, not runtime/approval evidence."""
import copy
import hashlib
import os
import uuid


class LauncherFixture:
    def __init__(self, root, clock):
        self.root, self.clock = root, clock
        self.fds, self.probes = [], []
        self.pinned = None

    def fresh(self, by='crew'):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.fds.append(fd)
        info = os.fstat(fd)
        now = self.clock()
        return dict(schema='office-launcher-policy-v1', principal=by,
            office_uid=os.geteuid()+1, office_gids=[],
            approved_full_guard_sha256=hashlib.sha256((self.root/'host_guard.json').read_bytes()).hexdigest(),
            approval_origin='launcher_config', approval_ref=dict(commit='a'*40,path='config/office/host_guard.json',approver='gamma',second_approver='alpha'),
            config_root_fd=fd, config_root_dev_ino=[info.st_dev, info.st_ino],
            config_root_readonly_proof=dict(mount_ro=True,source='mountinfo',checked_at=now),
            issued_at=now, expires_at=now+60., nonce=uuid.uuid4().hex)

    def policy(self, by):
        return copy.deepcopy(self.pinned) if self.pinned is not None else self.fresh(by)

    def probe(self, relative):
        self.probes.append(relative)
        return False

    def close(self):
        for fd in self.fds:
            os.close(fd)
        self.fds.clear()
