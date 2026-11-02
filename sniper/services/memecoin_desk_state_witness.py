"""Bounded public-frame diagnostics only; never state, price or loss-clearance proof."""
from __future__ import annotations

import base64
import collections
import hashlib
import json
import math
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KINDS = {
    '82a42461e48287a5': 'SweepPoolFeeEvent',
    '3f451c16305cc2b9': 'BoostBuyAndBurnEvent',
    '1609851aa02c47c0': 'WithdrawEvent',
    'ae7c4af90451f611': 'InitBoostEvent',
    '78f83d531f8e6b90': 'DepositEvent',
    '67f4521f2cf57777': 'BuyEvent',
}
# Only observed, exact event/instruction pairs get a subtype budget.
SCOPE_KEYS = {
    'scope_unknown_instruction_a039592ab58b2b42_a8_d8': ('82a42461e48287a5','a039592ab58b2b42'),
    'scope_unknown_instruction_5e06ca73ff60e8b7_a6_d8': ('67f4521f2cf57777','5e06ca73ff60e8b7'),
    'scope_layout_data_c62e1552b4d9e870_a26_d24': ('67f4521f2cf57777','c62e1552b4d9e870'),
}
KEYS = dict(KINDS)
KEYS.update({event+'_'+ix: KINDS[event]+':'+ix for event,ix in SCOPE_KEYS.values()})
TOTAL_DAY = 200
PER_KIND_DAY = 20
MAX_BYTES = 256 * 1024
KEEP_DAYS = 3


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _slot_name(name):
    """Only this writer's fixed names belong to its retention policy."""
    stem = name.removesuffix('.tmp')
    if not stem.endswith('.json'):
        return None
    key,sep,number = stem[:-5].rpartition('-')
    if not sep or key not in KEYS:
        return None
    try:
        slot = int(number)
    except ValueError:
        return None
    return (key,slot) if 1 <= slot <= PER_KIND_DAY and number == '%02d' % slot else None


class Writer:
    def __init__(self, base, *, enabled=True):
        self.root = Path(base) / 'pumpswap' / 'state_witnesses'
        self.enabled = enabled
        self.day = None
        self.blocked = False
        self.slots = {disc: set() for disc in KEYS}
        self.stats = collections.Counter()

    def _prepare(self, day):
        # Called once at startup/day roll, including failures: no repeated per-frame scans.
        self.day, self.blocked = day, True
        for path in (self.root.parent, self.root):
            if path.is_symlink():
                raise OSError('symlinked witness namespace')
        self.root.mkdir(parents=True, exist_ok=True)
        entries = list(self.root.iterdir())
        cutoff = date.fromisoformat(day) - timedelta(days=KEEP_DAYS-1)
        for directory in entries:
            try:
                when = date.fromisoformat(directory.name)
            except ValueError:
                continue
            if directory.name != when.isoformat() or directory.is_symlink() or not directory.is_dir():
                continue
            if directory.name > day:
                raise OSError('future witness bucket; clock rollback')
            for path in directory.iterdir():
                if _slot_name(path.name) is None or path.is_symlink() or not path.is_file():
                    continue
                if when < cutoff or path.name.endswith('.tmp'):
                    path.unlink(missing_ok=True)
                    self.stats['pruned'] += 1
            if when < cutoff:
                try:
                    directory.rmdir()
                except OSError:
                    self.stats['retained_nonempty_dirs'] += 1  # Foreign names are never deleted.
        current = self.root / day
        if current.is_symlink():
            raise OSError('symlinked witness bucket')
        current.mkdir(exist_ok=True)
        self.slots = {disc: set() for disc in KEYS}
        for path in current.iterdir():
            owned = _slot_name(path.name)
            if owned is not None:
                self.slots[owned[0]].add(owned[1])
        self.blocked = False

    def capture(self, raw, *, signature, slot, tx_index, received_at, known_at,
                discriminator, reason, idl_sha256, decoder_root):
        if not self.enabled:
            self.stats['disabled'] += 1
            return None
        if discriminator not in KINDS:
            return None
        if (not _finite(known_at) or not _finite(received_at) or not 0 <= received_at <= known_at
                or type(slot) is not int or not 0 < slot < 2**64
                or tx_index is not None and (type(tx_index) is not int or not 0 <= tx_index < 2**63)
                or not all(isinstance(v, str) and 0 < len(v) <= limit for v, limit in (
                    (signature,256),(reason,160),(idl_sha256,64),(decoder_root,4096)))
                or not isinstance(raw, (bytes, bytearray))):
            self.stats['invalid'] += 1
            return None
        pair = SCOPE_KEYS.get(reason.rpartition(':')[2])
        scope_instruction = pair[1] if pair is not None and pair[0] == discriminator else None
        key = discriminator+'_'+scope_instruction if scope_instruction else discriminator
        try:
            day = datetime.fromtimestamp(known_at, timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            self.stats['invalid'] += 1
            return None
        if self.day is not None and day < self.day:
            self.stats['clock_rollback'] += 1
            return None
        try:
            if day != self.day:
                self._prepare(day)
            if self.blocked:
                self.stats['storage_blocked'] += 1
                return None
            if sum(len(v) for v in self.slots.values()) >= TOTAL_DAY:
                self.stats['total_capped'] += 1
                return None
            available = next((n for n in range(1, PER_KIND_DAY+1) if n not in self.slots[key]), None)
            if available is None:
                self.stats['capped'] += 1
                return None
            self.slots[key].add(available)  # Failed/oversized attempts get no process-local retry.
            self.stats['attempted'] += 1
            if len(raw) > MAX_BYTES * 3 // 4:
                self.stats['oversize'] += 1
                return None
            raw = bytes(raw)
            body = dict(schema='desk-ps-state-witness/v1', diagnostic_only=True,
                        label_basis='first_blocking_scope_instruction' if scope_instruction else 'first_blocking_discriminator',
                        scope_instruction=scope_instruction, capture_key=key, kind=KINDS[discriminator],
                        discriminator=discriminator, signature=signature, slot=slot, tx_index=tx_index,
                        received_at=received_at, known_at=known_at, reason=reason, idl_sha256=idl_sha256,
                        decoder_root=decoder_root, frame_sha256=hashlib.sha256(raw).hexdigest(),
                        raw_b64=base64.b64encode(raw).decode())
            encoded = json.dumps(body, sort_keys=True, allow_nan=False).encode()
            if len(encoded) > MAX_BYTES:
                self.stats['oversize'] += 1
                return None
            current = self.root / day
            if any(p.is_symlink() for p in (self.root.parent, self.root, current)):
                raise OSError('symlinked witness path')
            target = current / ('%s-%02d.json' % (key, available))
            temp = target.with_name(target.name + '.tmp')
            with temp.open('xb') as stream:
                if stream.write(encoded) != len(encoded):
                    raise OSError('short witness write')
            # Same-directory atomic no-replace publication; never overwrite an earlier witness.
            os.link(temp, target)
            self.stats['stored'] += 1
            self.stats['bytes'] += len(encoded)
            try:
                temp.unlink()
            except OSError:
                self.stats['cleanup_error'] += 1
            return target
        except OSError:
            self.stats['storage_error'] += 1
            return None

    def status(self):
        return dict(enabled=self.enabled, day=self.day, blocked=self.blocked,
                    slots_used={KEYS[k]:len(v) for k,v in self.slots.items()}, **self.stats)
