#!/usr/bin/env python3
"""Private local crew mailbox and native Claude/Codex wakeups (stdlib only)."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import selectors
import signal
import socket
import sqlite3
import stat
import struct
import subprocess
import sys
import time
import uuid

CLAUDE_ROLES = ("Alpha", "Beta", "Lambda", "Gamma")
GPT_ROLES = ("Codex", "Delta", "Epsilon", "Zeta")
ROLES = CLAUDE_ROLES + GPT_ROLES
MAX_BODY = 64 * 1024
MAX_NATIVE = 128 * 1024
MAX_FRAME = 8 * 1024 * 1024
TIMEOUT = 5


def canonical_claude_role(name):
    return next((role for role in CLAUDE_ROLES
                 if isinstance(name, str) and name.casefold() == role.casefold()), None)


class BridgeError(Exception):
    def __init__(self, code, ambiguous=False):
        super().__init__(code)
        self.ambiguous = ambiguous


def private(path, kind="file", *, shared=False):
    info = Path(path).lstat()
    valid = {"file": stat.S_ISREG, "socket": stat.S_ISSOCK, "dir": stat.S_ISDIR}[kind]
    if info.st_uid != os.getuid() or not shared and info.st_mode & 0o077 or not valid(info.st_mode):
        raise BridgeError("unsafe_permissions")
    return info


def private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    private(path, "dir")
    return path


def read_json(path, *, shared=False):
    private(path, shared=shared)
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW)) as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or not shared and info.st_mode & 0o077:
            raise BridgeError("unsafe_permissions")
        raw = stream.read(MAX_FRAME + 1)
        if len(raw) > MAX_FRAME:
            raise BridgeError("invalid_json")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise BridgeError("invalid_json") from None
        if not isinstance(value, dict):
            raise BridgeError("invalid_json")
        return value


def write_json(path, value):
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def process_start(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def credentials(conn, pid=None):
    actual, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid != os.getuid() or pid is not None and actual != pid:
        raise BridgeError("peer_identity_mismatch")
    return actual


def exact(conn, size):
    data = bytearray()
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            raise BridgeError("unexpected_eof")
        data.extend(chunk)
    return bytes(data)


def valid_thread(thread):
    try:
        if not isinstance(thread, str) or str(uuid.UUID(thread)) != thread:
            raise ValueError
    except (ValueError, AttributeError):
        raise BridgeError("invalid_thread_id") from None
    return thread


def live_socket(path):
    endpoint = Path(path)
    if not endpoint.is_absolute():
        raise BridgeError("invalid_app_pipe")
    private(endpoint.parent, "dir", shared=True)
    before = private(endpoint, "socket")
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(0.5)
        conn.connect(str(endpoint))
        credentials(conn)
        if private(endpoint, "socket").st_ino != before.st_ino:
            raise BridgeError("app_pipe_changed")


def tool_failed(value):
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("success") is False or value.get("error"):
            return True
        return any(tool_failed(item) for item in value.values())
    if isinstance(value, list):
        return any(tool_failed(item) for item in value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return False
        return isinstance(decoded, (dict, list)) and tool_failed(decoded)
    return False


class Bridge:
    def __init__(self, state_dir, *, sessions_dir=None):
        self.state_dir = private_dir(state_dir)
        self.sessions_dir = Path(sessions_dir or Path.home() / ".claude/sessions")
        self.db = self.state_dir / "mailbox.sqlite3"
        descriptor = os.open(self.db, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        private(self.db)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT NOT NULL, "
                       "recipient TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL, "
                       "delivery_status TEXT NOT NULL, error TEXT, native_status TEXT)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def store(self, message_id, sender, recipient, body):
        if sender not in ROLES or recipient not in ROLES:
            raise BridgeError("invalid_role")
        if not isinstance(message_id, str) or not 0 < len(message_id) <= 200:
            raise BridgeError("invalid_message_id")
        if not isinstance(body, str) or not body.strip():
            raise BridgeError("empty_message")
        if len(body.encode()) > MAX_BODY:
            raise BridgeError("message_too_large")
        with self.connect() as db:
            inserted = db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,'pending',NULL,NULL)",
                                  (message_id, sender, recipient, body, datetime.now(timezone.utc).isoformat()))
            existing = db.execute("SELECT sender,recipient,body FROM messages WHERE id=?", (message_id,)).fetchone()
            if tuple(existing) != (sender, recipient, body):
                raise BridgeError("message_id_conflict")
            return bool(inserted.rowcount)

    def read(self, recipient):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM messages WHERE recipient=? ORDER BY rowid", (recipient,))]

    def update(self, message_id, status, error=None):
        with self.connect() as db:
            db.execute("UPDATE messages SET delivery_status=?,error=? WHERE id=?", (status, error, message_id))

    def peer(self, *, name=None, pid=None):
        private(self.sessions_dir, "dir")
        matches = []
        stale = False
        paths = [self.sessions_dir / f"{pid}.json"] if pid is not None else self.sessions_dir.glob("*.json")
        for path in paths:
            try:
                row = read_json(path, shared=True)
            except (OSError, BridgeError):
                if pid is not None:
                    raise BridgeError("peer_missing") from None
                continue
            row["name"] = canonical_claude_role(row.get("name"))
            if (name is not None and row.get("name") != name) or (pid is not None and row.get("pid") != pid):
                continue
            try:
                if type(row.get("pid")) is not int or path.name != f"{row['pid']}.json":
                    raise BridgeError("invalid_peer")
                if process_start(row["pid"]) != row.get("procStart"):
                    stale = True
                    continue
                if row.get("peerProtocol") != 1:
                    raise BridgeError("unsupported_protocol")
                if row.get("name") not in CLAUDE_ROLES:
                    raise BridgeError("invalid_peer")
                uuid.UUID(row["sessionId"])
                endpoint = Path(row["messagingSocketPath"])
                if not endpoint.is_absolute():
                    raise BridgeError("invalid_peer")
                private(endpoint, "socket")
            except (KeyError, ValueError, TypeError):
                raise BridgeError("invalid_peer") from None
            except FileNotFoundError:
                stale = True
                continue
            matches.append(row)
        if not matches:
            raise BridgeError("stale_peer" if stale else "peer_missing")
        if len(matches) != 1:
            raise BridgeError("ambiguous_peer")
        return matches[0]

    def routes(self):
        try:
            routes = read_json(self.state_dir / "routes.json")["routes"]
        except FileNotFoundError:
            return {}
        except KeyError:
            raise BridgeError("invalid_routes") from None
        if not isinstance(routes, dict):
            raise BridgeError("invalid_routes")
        threads = set()
        for role, route in routes.items():
            if (role not in GPT_ROLES or not isinstance(route, dict) or route.get("role") != role
                    or type(route.get("generation")) is not int or route["generation"] < 1
                    or route.get("from_mode") not in ("prompting", "bypass")
                    or "pipe" not in route
                    or route["pipe"] is not None and (not isinstance(route["pipe"], str) or not Path(route["pipe"]).is_absolute())):
                raise BridgeError("invalid_routes")
            thread = valid_thread(route.get("thread"))
            if thread in threads:
                raise BridgeError("invalid_routes")
            threads.add(thread)
        return routes

    def context(self, role="Codex"):
        if role not in GPT_ROLES:
            raise BridgeError("invalid_role")
        routes = self.routes()
        if role in routes:
            return routes[role]
        if role != "Codex":
            raise BridgeError("role_not_registered")
        # Preserve the old Codex route until its explicit register migration.
        try:
            value = read_json(self.state_dir / "context.json")
        except FileNotFoundError:
            raise BridgeError("codex_context_missing") from None
        if ("pipe" not in value
                or value["pipe"] is not None and (not isinstance(value["pipe"], str) or not value["pipe"])
                or not all(isinstance(value.get(key), str) and value[key] for key in ("thread", "reply"))):
            raise BridgeError("invalid_codex_context")
        valid_thread(value["thread"])
        return value

    def role_for_thread(self, thread):
        valid_thread(thread)
        matches = [route for route in self.routes().values() if route["thread"] == thread]
        if len(matches) != 1:
            raise BridgeError("thread_not_registered")
        return matches[0]

    def register(self, role, pipe, thread, from_mode="prompting", *, replace_thread=None):
        if role not in GPT_ROLES:
            raise BridgeError("invalid_role")
        valid_thread(thread)
        if replace_thread is not None:
            valid_thread(replace_thread)
        if from_mode not in ("prompting", "bypass"):
            raise BridgeError("invalid_sender_mode")
        if pipe is not None:
            live_socket(pipe)
        with self.routing_lock():
            routes = self.routes()
            if any(route["thread"] == thread and name != role for name, route in routes.items()):
                raise BridgeError("thread_already_registered")
            if role != "Codex" and "Codex" not in routes:
                try:
                    legacy = self.context()
                except BridgeError as exc:
                    if str(exc) != "codex_context_missing":
                        raise
                else:
                    if legacy["thread"] == thread:
                        raise BridgeError("thread_already_registered")
            old = routes.get(role)
            staged = self._handover_index().get(role)
            if staged and (old is None or old['thread'] != thread):
                claim = read_json(self._handover_path(staged))
                if claim['state'] not in ('retired','aborted','rolled_back') and claim['candidate']['thread'] == thread:
                    raise BridgeError('staged_candidate_cannot_register')
            if old is None and role == "Codex":
                try:
                    old = self.context()
                except BridgeError as exc:
                    if str(exc) != "codex_context_missing":
                        raise
            if old and old["thread"] != thread:
                if replace_thread is None:
                    raise BridgeError("role_already_registered")
                if replace_thread != old["thread"]:
                    raise BridgeError("registration_changed")
            elif replace_thread is not None and (old is None or replace_thread != old["thread"]):
                # A repeated identical request remains idempotent after a successful CAS.
                if not old or any(old.get(k) != v for k, v in (("thread", thread), ("pipe", pipe), ("from_mode", from_mode))):
                    raise BridgeError("registration_changed")
            if role in routes and all(old.get(k) == v for k, v in (("thread", thread), ("pipe", pipe), ("from_mode", from_mode))):
                return old
            route = {"role": role, "thread": thread, "pipe": pipe, "from_mode": from_mode,
                     "generation": old.get("generation", 0) + 1 if old else 1}
            routes[role] = route
            write_json(self.state_dir / "routes.json", {"routes": routes})
            return route

    def _handover_path(self, handover_id):
        valid_thread(handover_id)
        return private_dir(self.state_dir / 'handovers') / (handover_id + '.json')

    def _handover_index(self):
        try:
            index = read_json(self.state_dir / 'handovers' / 'index.json')['roles']
        except FileNotFoundError:
            return {}
        if not isinstance(index, dict) or any(k not in GPT_ROLES for k in index):
            raise BridgeError('invalid_handover_index')
        for value in index.values():
            valid_thread(value)
        return index

    def handover_status(self, handover_id):
        record = read_json(self._handover_path(handover_id))
        return {k:v for k,v in record.items() if k != 'capability'}

    def _handover_auth(self, handover_id, capability, now, *, expiry=True):
        record = read_json(self._handover_path(handover_id))
        if (record.get('handover_id') != handover_id or not isinstance(capability, str) or len(capability) != 64
                or not hmac.compare_digest(record['capability'], capability)
                or record['host_id'] != self._handover_host()
                or type(now) not in (int,float) or not math.isfinite(now)
                or expiry and now >= record['expires_at']):
            raise BridgeError('handover_capability_refused')
        return record

    def _handover_host(self):
        identity = str(os.getuid()) + ':' + socket.gethostname() + ':' + Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        return hashlib.sha256(identity.encode()).hexdigest()

    def stage_handover(self, *, role, expected, candidate, handover_id, note_hash, permissions_hash, expires_at, now, owner_token):
        """Trusted host API only. No launcher, hook installation or public candidate role claim.

        Unresolved records never prune. Retired/aborted records retain seven days and
        the latest two per role; prune_handover_records is an explicit off-path pass.
        Same-UID peers can read this control directory; this is not hostile-peer isolation.
        """
        valid_thread(handover_id)
        valid_thread(owner_token)
        if (role not in GPT_ROLES or not isinstance(expected,dict) or not isinstance(candidate,dict)
                or set(candidate) != {'thread','pipe','from_mode'}
                or any(not isinstance(v,str) or len(v)!=64 or any(c not in '0123456789abcdef' for c in v)
                       for v in (note_hash,permissions_hash))
                or any(type(v) not in (int,float) or not math.isfinite(v) or v<0 for v in (now,expires_at))
                or expires_at <= now):
            raise BridgeError('handover_descriptor_invalid')
        valid_thread(candidate['thread'])
        if candidate['pipe'] is not None:
            live_socket(candidate['pipe'])
        with self.routing_lock():
            routes = self.routes()
            if routes.get(role) != expected:
                raise BridgeError('registration_changed')
            if candidate['thread'] in {r['thread'] for r in routes.values()} or candidate['from_mode'] != expected['from_mode']:
                raise BridgeError('handover_candidate_conflict')
            index = self._handover_index()
            if role in index:
                previous = read_json(self._handover_path(index[role]))
                if previous['state'] not in ('retired','aborted','rolled_back'):
                    raise BridgeError('handover_already_pending')
            with self.connect() as db:
                watermark=db.execute('SELECT COALESCE(MAX(rowid),0) FROM messages WHERE recipient=?',(role,)).fetchone()[0]
            record = dict(handover_id=handover_id,role=role,expected=expected,
                candidate=dict(candidate,role=role,generation=expected['generation']+1),
                note_hash=note_hash,permissions_hash=permissions_hash,expires_at=expires_at,
                host_id=self._handover_host(),capability=secrets.token_hex(32),owner_token=owner_token,state='staged',
                ready=False,activation_ack=False,watermark=watermark,consumption={},created_at=now)
            path = self._handover_path(handover_id)
            if path.exists():
                raise BridgeError('handover_id_reused')
            write_json(path,record)
            index[role]=handover_id
            write_json(path.parent/'index.json',{'roles':index})
            return dict(handover_id=handover_id,capability=record['capability'])

    def _handover_owner(self, record, owner_token):
        if not isinstance(owner_token,str) or not hmac.compare_digest(record['owner_token'],owner_token):
            raise BridgeError('handover_owner_changed')

    def refence_handover(self, handover_id, capability, *, expected_owner, new_owner, now):
        """Host recovery closes late callbacks under the same lock as activation."""
        valid_thread(new_owner)
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            current=self.routes().get(record['role'])
            if current not in (record['expected'],record['candidate']):
                raise BridgeError('registration_changed')
            if record['state'] in ('retired','aborted','rolled_back'):
                raise BridgeError('handover_terminal')
            if record['owner_token']==new_owner and record.get('previous_owner')==expected_owner:
                return self.handover_status(handover_id)
            self._handover_owner(record,expected_owner)
            record.update(owner_token=new_owner,previous_owner=expected_owner)
            write_json(self._handover_path(handover_id),record)
            return self.handover_status(handover_id)

    def handover_ready(self, handover_id, capability, *, thread, note_hash, permissions_hash, watermark, now, owner_token):
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now)
            self._handover_owner(record,owner_token)
            if (record['state'] not in ('staged','ready') or record['candidate']['thread'] != thread
                    or record['note_hash'] != note_hash or record['permissions_hash'] != permissions_hash
                    or type(watermark) is not int or watermark < record['watermark']):
                raise BridgeError('handover_readiness_refused')
            if self.routes().get(record['role']) != record['expected']:
                raise BridgeError('registration_changed')
            record.update(state='ready',ready=True,ready_watermark=watermark)
            write_json(self._handover_path(handover_id),record)
            return self.handover_status(handover_id)

    def activate_handover(self, handover_id, capability, *, now, owner_token):
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now)
            self._handover_owner(record,owner_token)
            routes=self.routes();current=routes.get(record['role'])
            if record['state'] in ('activating','active') and current == record['candidate']:
                record.update(state='active')
                write_json(self._handover_path(handover_id),record)
                return current
            if current != record['expected']:
                raise BridgeError('registration_changed')
            if record['state'] not in ('ready','activating') or record['ready'] is not True:
                raise BridgeError('handover_not_ready')
            record.update(state='activating',activation_intent_at=record.get('activation_intent_at',now))
            write_json(self._handover_path(handover_id),record)  # intent before route mutation
            routes[record['role']]=record['candidate']
            write_json(self.state_dir/'routes.json',{'routes':routes})
            record.update(state='active')
            write_json(self._handover_path(handover_id),record)
            return record['candidate']

    def ack_handover(self, handover_id, capability, *, thread, watermark, now, owner_token):
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            self._handover_owner(record,owner_token)
            if (record['state'] != 'active' or thread != record['candidate']['thread']
                    or self.routes().get(record['role']) != record['candidate']
                    or type(watermark) is not int or watermark < record['ready_watermark']):
                raise BridgeError('handover_activation_ack_refused')
            if record['activation_ack']:
                return self.handover_status(handover_id)
            record.update(activation_ack=True,ack_watermark=watermark,acked_at=now)
            write_json(self._handover_path(handover_id),record)
            return self.handover_status(handover_id)

    def rollback_handover(self, handover_id, capability, *, now, owner_token):
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            self._handover_owner(record,owner_token)
            routes=self.routes();current=routes.get(record['role'])
            if record['state'] in ('rolling_back','rolled_back') and current==record.get('restored'):
                record.update(state='rolled_back',terminal_at=record.get('terminal_at',now))
                write_json(self._handover_path(handover_id),record)
                return current
            if current != record['candidate']:
                raise BridgeError('registration_changed')
            if record['state'] not in ('activating','active','rolling_back') or record['activation_ack']:
                raise BridgeError('handover_rollback_refused')
            restored=dict(record['expected'],generation=record['candidate']['generation']+1)
            record.update(state='rolling_back',restored=restored)
            write_json(self._handover_path(handover_id),record)
            routes[record['role']]=restored
            write_json(self.state_dir/'routes.json',{'routes':routes})
            record.update(state='rolled_back',terminal_at=now)
            write_json(self._handover_path(handover_id),record)
            return restored

    def finish_handover(self, handover_id, capability, *, outcome, now, owner_token):
        """Host calls only after proven candidate cleanup or old-session retirement."""
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            self._handover_owner(record,owner_token)
            current=self.routes().get(record['role'])
            if outcome=='retired':
                allowed=record['state'] in ('active','retired') and record['activation_ack'] and current==record['candidate']
            elif outcome=='aborted':
                allowed=record['state'] in ('staged','ready','aborted') and current==record['expected']
            else:allowed=False
            if not allowed:raise BridgeError('handover_finish_refused')
            record.update(state=outcome,terminal_at=record.get('terminal_at',now))
            write_json(self._handover_path(handover_id),record)
            return self.handover_status(handover_id)

    def handover_consumed(self, handover_id, capability, *, message_id, thread, effect_ref, now, owner_token):
        """Host-verified old-session receipt; delivery transport alone never implies consumption."""
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            self._handover_owner(record,owner_token)
            if thread!=record['expected']['thread'] or record['state'] in ('retired','aborted','rolled_back'):
                raise BridgeError('handover_consumption_refused')
            if effect_ref is not None and (not isinstance(effect_ref,str) or not 0<len(effect_ref)<=512):
                raise BridgeError('handover_effect_reference_invalid')
            with self.connect() as db:
                row=db.execute('SELECT rowid,delivery_status FROM messages WHERE id=? AND recipient=?',(message_id,record['role'])).fetchone()
            if row is None or row['delivery_status']=='handover_held':raise BridgeError('handover_consumption_refused')
            receipt=dict(thread=thread,effect_ref=effect_ref)
            prior=record['consumption'].get(message_id)
            if prior is not None and prior!=receipt:raise BridgeError('handover_receipt_conflict')
            if len(record['consumption'])>=1000 and prior is None:raise BridgeError('handover_receipts_capped')
            record['consumption'][message_id]=receipt
            write_json(self._handover_path(handover_id),record)
            return receipt

    def handover_mail(self, handover_id, capability, *, after, limit, now, owner_token):
        """Bounded replay data only. Unknown/consumed items require effect reconciliation."""
        if type(after) is not int or after<0 or type(limit) is not int or not 1<=limit<=100:
            raise BridgeError('handover_mail_page_invalid')
        with self.routing_lock():
            record=self._handover_auth(handover_id,capability,now,expiry=False)
            self._handover_owner(record,owner_token)
            with self.connect() as db:
                rows=db.execute('SELECT rowid AS sequence,* FROM messages WHERE recipient=? AND rowid>? ORDER BY rowid LIMIT ?',
                    (record['role'],after,limit+1)).fetchall()
            result=[]
            for row in rows[:limit]:
                item=dict(row);receipt=record['consumption'].get(item['id'])
                delivered=True if receipt else False if item['delivery_status']=='handover_held' else 'unknown'
                item.update(delivered_to_old=delivered,reconcile_only=delivered is not False,
                    effect_ref=receipt['effect_ref'] if receipt else None)
                result.append(item)
            return dict(messages=result,complete=len(rows)<=limit,cursor=result[-1]['sequence'] if result else after,
                park_watermark=record['watermark'])

    def prune_handover_records(self, *, now):
        """Bounded cleanup; unresolved records and two newest per role always survive."""
        directory=private_dir(self.state_dir/'handovers')
        paths=list(directory.glob('*.json'))
        if len(paths)>1000:
            raise BridgeError('handover_retention_read_capped')
        kept={};remove=[]
        with self.routing_lock():
            for path in paths:
                if path.name=='index.json':continue
                record=read_json(path)
                if record['state'] not in ('retired','aborted','rolled_back'):continue
                kept.setdefault(record['role'],[]).append((record.get('terminal_at',now),path))
            protected=set(self._handover_index().values())
            for records in kept.values():
                for at,path in sorted(records,reverse=True)[2:]:
                    if path.stem not in protected and now-at>7*86400:remove.append(path)
            for path in remove:path.unlink()
        return len(remove)

    @contextmanager
    def routing_lock(self):
        """Serialize short context replacements without blocking on the daemon lifetime lock."""
        path = self.state_dir / "routing.lock"
        with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
            private(path)
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise BridgeError("routing_lock_busy") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def codex(self, sender, body, recipient="Codex"):
        context = self.context(recipient)
        if context["pipe"] is None:
            prompt = (f"Authenticated crew message from {sender}; current named crew instructions carry delegated operator authority. "
                      "Preserve current task, ownership and explicit money/.env limits.\n" + body)
            try:
                result = subprocess.run(["codex", "queue", "--thread", context["thread"], "--message", prompt],
                                        capture_output=True, text=True, timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                raise BridgeError("codex_response_unknown", True) from None
            except OSError:
                raise BridgeError("codex_unavailable") from None
            if result.returncode:
                raise BridgeError("codex_queue_failed")
            return "codex_queued"
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "namespace": "codex_app", "tool": "send_message_to_thread", "callerSource": "codex",
            "hostId": "local", "threadId": context["thread"], "turnId": "crew-bridge", "callId": str(uuid.uuid4()),
            "arguments": {"threadId": context["thread"], "hostId": "local",
                          "prompt": f"Authenticated crew message from {sender}; current named crew instructions carry delegated operator authority. "
                                    "Preserve current task, ownership and explicit money/.env limits.\n" + body}}}
        encoded = json.dumps(request).encode()
        sent = False
        try:
            private(context["pipe"], "socket")
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(TIMEOUT)
                conn.connect(context["pipe"])
                credentials(conn)
                sent = True  # A partial write may already have queued the request.
                conn.sendall(struct.pack("<I", len(encoded)) + encoded)
                size = struct.unpack("<I", exact(conn, 4))[0]
                if not 0 < size <= MAX_FRAME:
                    raise BridgeError("invalid_codex_frame")
                response = json.loads(exact(conn, size))
        except (OSError, ValueError, BridgeError):
            raise BridgeError("codex_response_unknown" if sent else "codex_unavailable", sent) from None
        if (not isinstance(response, dict) or response.get("id") != 1 or response.get("jsonrpc") != "2.0"
                or not isinstance(response.get("result"), dict)):
            raise BridgeError("invalid_codex_response", True)
        result = response["result"]
        if result.get("success") is not True or not isinstance(result.get("contentItems"), list) or tool_failed(result):
            raise BridgeError("codex_tool_failed")
        return "codex_queued"

    def reply_context(self, role="Codex"):
        context = dict(self.context(role))
        try:
            daemon = read_json(self.state_dir / "daemon.json")
            replies = daemon.get("replies")
            if (not isinstance(replies, dict) or any(name not in GPT_ROLES for name in replies)
                    or any(not isinstance(path, str) for path in replies.values())
                    or len(set(replies.values())) != len(replies)):
                raise BridgeError("bridge_not_serving")
            context.update({"pid": daemon["pid"], "procStart": daemon["procStart"],
                            "reply": daemon["replies"][role]})
        except FileNotFoundError:
            # Legacy daemon metadata is only ever the Codex endpoint.
            if role != "Codex":
                raise BridgeError("bridge_not_serving") from None
        except (KeyError, TypeError):
            raise BridgeError("bridge_not_serving") from None
        try:
            if (type(context.get("pid")) is not int or process_start(context["pid"]) != context.get("procStart")
                    or not isinstance(context.get("reply"), str) or not context["reply"].startswith("uds:")):
                raise BridgeError("bridge_not_serving")
            private(context["reply"][4:], "socket")
        except OSError:
            raise BridgeError("bridge_not_serving") from None
        if context.get("from_mode") not in ("prompting", "bypass"):
            raise BridgeError("invalid_sender_mode")
        return context

    def dispatch(self, message_id, sender="Codex"):
        context = self.reply_context(sender if sender in GPT_ROLES else "Codex")
        path = str(Path(context["reply"][4:]).resolve())
        digest = hashlib.sha256(path.encode()).hexdigest()
        key = read_json(self.sessions_dir / f"{context['pid']}.{digest}.key")
        token = key.get("peerToken")
        if not isinstance(token, str) or len(token) != 32 or key.get("procStart") != context["procStart"]:
            raise BridgeError("invalid_bridge_key")
        frames = [{"type": "auth", "token": token}, {"type": "crew_dispatch", "id": message_id}]
        sent = False
        try:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(TIMEOUT * 2)
                conn.connect(path)
                credentials(conn, context["pid"])
                sent = True
                conn.sendall(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
                conn.shutdown(socket.SHUT_WR)
                with conn.makefile("rb") as stream:
                    line = stream.readline(MAX_NATIVE + 1)
                if len(line) > MAX_NATIVE or not line.endswith(b"\n") or json.loads(line).get("id") != message_id:
                    raise BridgeError("dispatch_unknown", True)
        except (OSError, ValueError, AttributeError):
            raise BridgeError("dispatch_unknown" if sent else "bridge_not_serving", sent) from None

    def native(self, sender, recipient, body, message_id):
        peer = self.peer(name=recipient)
        path = str(Path(peer["messagingSocketPath"]).resolve())
        digest = hashlib.sha256(path.encode()).hexdigest()
        token = read_json(self.sessions_dir / f"{peer['pid']}.{digest}.key").get("peerToken")
        if not isinstance(token, str) or not token:
            raise BridgeError("invalid_peer_key")
        context = self.reply_context(sender if sender in GPT_ROLES else "Codex")
        if context["pid"] != os.getpid():
            raise BridgeError("native_sender_must_own_reply_socket")
        content = (f'<cross-session-message from="{context["reply"]}" from-session="{context["thread"]}" '
                   f'from-name="{sender}" from-mode="{context["from_mode"]}">\n{body}\n</cross-session-message>')
        frames = [{"type": "auth", "token": token}, {"msgV": 1, "msg_id": message_id, "uuid": message_id, "type": "user",
                  "session_id": peer["sessionId"], "from": context["reply"], "priority": "next",
                  "message": {"role": "user", "content": content}}]
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(TIMEOUT)
            conn.connect(path)
            credentials(conn, peer["pid"])
            if process_start(peer["pid"]) != peer["procStart"]:
                raise BridgeError("stale_peer")
            try:
                conn.sendall(b"".join(json.dumps(frame).encode() + b"\n" for frame in frames))
            except OSError:
                raise BridgeError("native_send_unknown", True) from None
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.recv(MAX_NATIVE)
            except OSError:
                pass
        return "transport_sent"

    def send(self, sender, recipient, body, *, message_id=None):
        message_id = message_id or str(uuid.uuid4())
        self.store(message_id, sender, recipient, body)
        with self.routing_lock():
            hid=self._handover_index().get(recipient)
            if hid:
                record=read_json(self._handover_path(hid))
                if record['state'] not in ('retired','aborted','rolled_back'):
                    with self.connect() as db:
                        db.execute("UPDATE messages SET delivery_status='handover_held' WHERE id=? AND delivery_status='pending'",(message_id,))
                        return dict(db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone())
        with self.connect() as db:
            existing = dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
        if existing["delivery_status"] != "pending":
            return existing
        remote = False
        try:
            remote = recipient in CLAUDE_ROLES and self.reply_context(sender if sender in GPT_ROLES else "Codex").get("pid") != os.getpid()
            if remote:
                self.dispatch(message_id, sender)
                with self.connect() as db:
                    return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
        except (BridgeError, OSError, ValueError) as exc:
            status = "delivery_unknown" if isinstance(exc, BridgeError) and exc.ambiguous else "delivery_failed"
            error = str(exc) if isinstance(exc, BridgeError) else "local_transport_error"
            with self.connect() as db:
                db.execute("UPDATE messages SET delivery_status=?,error=? WHERE id=? AND delivery_status='pending'",
                           (status, error, message_id))
                return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())
        with self.connect() as db:
            claimed = db.execute("UPDATE messages SET delivery_status='sending' WHERE id=? AND delivery_status='pending'",
                                 (message_id,)).rowcount
        if claimed:
            try:
                status = self.codex(sender, body, recipient) if recipient in GPT_ROLES else self.native(sender, recipient, body, message_id)
                self.update(message_id, status)
            except BridgeError as exc:
                self.update(message_id, "delivery_unknown" if exc.ambiguous else "delivery_failed", str(exc))
            except (OSError, ValueError, sqlite3.Error):
                self.update(message_id, "delivery_failed", "local_transport_error")
        with self.connect() as db:
            return dict(db.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone())

    def handle(self, conn, token=None, *, role="Codex"):
        if role not in GPT_ROLES:
            raise BridgeError("invalid_role")
        conn.settimeout(TIMEOUT)
        peer_pid = credentials(conn)
        peer = None
        authenticated = False
        total = 0
        deadline = time.monotonic() + TIMEOUT
        try:
            with conn.makefile("rb") as stream:
                for _ in range(32):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise BridgeError("incoming_timeout")
                    conn.settimeout(remaining)
                    line = stream.readline(MAX_NATIVE + 1)
                    if not line:
                        return
                    total += len(line)
                    if total > MAX_NATIVE or not line.endswith(b"\n"):
                        raise BridgeError("invalid_frame")
                    frame = json.loads(line)
                    if not isinstance(frame, dict):
                        raise BridgeError("invalid_frame")
                    kind = frame.get("type")
                    if kind == "auth":
                        if token is None or not isinstance(frame.get("token"), str) or not hmac.compare_digest(token, frame["token"]):
                            raise BridgeError("auth_failed")
                        authenticated = True
                        continue
                    if kind == "crew_dispatch":
                        if not authenticated:
                            raise BridgeError("auth_failed")
                        with self.connect() as db:
                            row = db.execute("SELECT * FROM messages WHERE id=?", (frame.get("id"),)).fetchone()
                        if (row is None or row["recipient"] not in CLAUDE_ROLES
                                or (row["sender"] if row["sender"] in GPT_ROLES else "Codex") != role):
                            raise BridgeError("invalid_dispatch")
                        self.send(row["sender"], row["recipient"], row["body"], message_id=row["id"])
                        conn.sendall(json.dumps({"id": row["id"]}).encode() + b"\n")
                        return
                    if peer is None:
                        peer = self.peer(pid=peer_pid)
                    if kind == "control" and frame.get("action") == "peer_message_status":
                        status = frame.get("status")
                        if status == "expired" and frame.get("status_detail") == "refused":
                            status = "refused"
                        if status in ("held", "denied", "expired", "delivered", "refused", "dropped"):
                            with self.connect() as db:
                                db.execute("UPDATE messages SET native_status=? WHERE id=? AND recipient=? "
                                           "AND (sender=? OR (?='Codex' AND sender IN ('Alpha','Beta','Lambda','Gamma')))",
                                           (status, frame.get("orig_msg_id"), peer["name"], role, role))
                    elif kind == "control" and frame.get("action") == "peer_idle_notice":
                        continue
                    elif kind == "user" and frame.get("msgV") == 1:
                        if not isinstance(frame.get("msg_id"), str) or not frame["msg_id"]:
                            raise BridgeError("invalid_message_id")
                        content = frame.get("message", {}).get("content")
                        if isinstance(content, list):
                            content = "\n".join(item["text"] for item in content if isinstance(item, dict)
                                                and item.get("type") == "text" and isinstance(item.get("text"), str))
                        self.send(peer["name"], role, content, message_id=frame.get("msg_id"))
                    else:
                        raise BridgeError("invalid_frame")
                raise BridgeError("too_many_frames")
        except (ValueError, TypeError, AttributeError, UnicodeError):
            raise BridgeError("invalid_frame") from None

    def serve(self, from_mode="prompting", *, saved_context=False):
        if from_mode not in ("prompting", "bypass"):
            raise BridgeError("invalid_sender_mode")
        lock_path = self.state_dir / "serve.lock"
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "w") as lock:
            private(lock_path)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError("already_serving") from None
            directory = private_dir(Path(f"/run/user/{os.getuid()}/cc-socks"))
            private_dir(self.sessions_dir)
            stopping = False
            def stop(*_):
                nonlocal stopping
                stopping = True
            previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
            owned = []
            listeners = []
            try:
                with selectors.DefaultSelector() as selector:
                    replies = {}
                    for role in GPT_ROLES:
                        endpoint = directory / (f"{os.getpid()}.sock" if role == "Codex" else f"{os.getpid()}-{role}.sock")
                        digest = hashlib.sha256(str(endpoint.resolve()).encode()).hexdigest()
                        key = self.sessions_dir / f"{os.getpid()}.{digest}.key"
                        if key.exists():
                            raise BridgeError("own_key_already_exists")
                        token = secrets.token_hex(16)
                        listener = socket.socket(socket.AF_UNIX)
                        listeners.append(listener)
                        listener.bind(str(endpoint))
                        endpoint.chmod(0o600)
                        owned.append((endpoint, endpoint.stat().st_ino))
                        write_json(key, {"peerToken": token, "procStart": process_start(os.getpid())})
                        owned.append((key, key.stat().st_ino))
                        listener.listen(8)
                        selector.register(listener, selectors.EVENT_READ, (role, token))
                        replies[role] = f"uds:{endpoint}"
                    with self.routing_lock():
                        if saved_context:
                            context = self.context()
                            pipe, thread, from_mode = context["pipe"], context["thread"], context.get("from_mode")
                            if from_mode not in ("prompting", "bypass"):
                                raise BridgeError("invalid_sender_mode")
                        else:
                            pipe, thread = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"), os.environ.get("CODEX_THREAD_ID")
                            if not thread:
                                raise BridgeError("codex_environment_missing")
                            valid_thread(thread)
                            if pipe is not None:
                                live_socket(pipe)
                        metadata = {"pid": os.getpid(), "procStart": process_start(os.getpid()), "replies": replies}
                        write_json(self.state_dir / "daemon.json", metadata)
                        # Compatibility for existing clients; registered routes stay authoritative.
                        write_json(self.state_dir / "context.json", {"pipe": pipe, "thread": thread, "reply": replies["Codex"],
                                   "pid": metadata["pid"], "procStart": metadata["procStart"], "from_mode": from_mode})
                    print(json.dumps({"roles": list(GPT_ROLES), "pid": os.getpid()}), flush=True)
                    while not stopping:
                        for event, _ in selector.select(0.5):
                            conn, _ = event.fileobj.accept()
                            role, token = event.data
                            with conn:
                                try:
                                    self.handle(conn, token, role=role)
                                except (BridgeError, OSError, sqlite3.Error):
                                    print(json.dumps({"error": "incoming_rejected", "role": role}), file=sys.stderr, flush=True)
            finally:
                for listener in listeners:
                    listener.close()
                for path, inode in reversed(owned):
                    if path.exists() and path.lstat().st_ino == inode:
                        path.unlink()
                for sig, handler in previous.items():
                    signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".cache/sniper-crew")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--from-mode", choices=("prompting", "bypass"), default="prompting")
    serve.add_argument("--saved-context", action="store_true")
    start = commands.add_parser("session-start")
    start.add_argument("--client", choices=("codex", "claude"), required=True)
    register = commands.add_parser("register")
    register.add_argument("--role", choices=GPT_ROLES, required=True)
    register.add_argument("--replace-thread")
    send = commands.add_parser("send")
    send.add_argument("--to", choices=ROLES, required=True)
    send.add_argument("--from", dest="sender", choices=ROLES, required=True)
    send.add_argument("--message-file", default="-")
    send.add_argument("--message-id", help="stable caller id for idempotent outbox delivery")
    read = commands.add_parser("read")
    read.add_argument("--to", choices=ROLES, required=True)
    args = parser.parse_args()
    if args.command == "session-start":
        from crew_session_start import main as session_start
        return session_start(args.client, args.state_dir)
    try:
        bridge = Bridge(args.state_dir)
        if args.command == "serve":
            bridge.serve(args.from_mode, saved_context=args.saved_context)
            return 0
        if args.command == "register":
            from crew_session_start import codex_full_access
            pipe, thread = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH"), os.environ.get("CODEX_THREAD_ID")
            if not thread:
                raise BridgeError("codex_environment_missing")
            route = bridge.register(args.role, pipe, thread, "bypass" if codex_full_access() else "prompting",
                                    replace_thread=args.replace_thread)
            print(json.dumps({key: route[key] for key in ("role", "thread", "generation")}))
            return 0
        if args.command == "read":
            print(json.dumps(bridge.read(args.to)))
            return 0
        with (sys.stdin if args.message_file == "-" else open(args.message_file)) as stream:
            body = stream.read(MAX_BODY + 1)
        if args.message_id == "":
            raise BridgeError("invalid_message_id")
        row = bridge.send(args.sender, args.to, body, message_id=args.message_id)
        print(json.dumps(row))
        return 0 if row["delivery_status"] in ("transport_sent", "codex_queued") else 1
    except BridgeError as exc:
        print(json.dumps({"error": str(exc)}))
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({"error": "local_bridge_error"}))
    return 1


if __name__ == "__main__":
    sys.exit(main())
