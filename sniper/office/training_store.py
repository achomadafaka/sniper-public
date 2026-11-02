"""Private discovery-shadow memory. An existing Office/evaluation database is never adopted."""
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3

from . import memory as M, memory_store as MS, store as S


class ShadowRefused(ValueError):
    pass


class ReplayShadow:
    def __init__(self, path):
        path = Path(path).absolute()
        if path.name != 'training_shadow.db' or path.is_symlink():
            raise ShadowRefused('shadow_path_denied')
        fresh = False
        try:
            fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            fresh = True
        except FileExistsError:
            if path.stat().st_nlink != 1:
                raise ShadowRefused('shadow_alias_denied')
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as check:
                    if check.execute('SELECT purpose FROM _training_info WHERE id=1').fetchone() != ('discovery_shadow_v1',):
                        raise ShadowRefused('not_a_training_shadow')
            except sqlite3.Error:
                raise ShadowRefused('not_a_training_shadow') from None
        self.conn = S.open_store(path)
        self.mem = MS.OfficeMemory(self.conn)
        if fresh:
            with S.write_transaction(self.conn):
                self.conn.execute('CREATE TABLE _training_info(id INTEGER PRIMARY KEY,purpose TEXT NOT NULL)')
                self.conn.execute("INSERT INTO _training_info VALUES(1,'discovery_shadow_v1')")
                self.conn.execute('CREATE TABLE training_writes(key TEXT PRIMARY KEY,input_hash TEXT NOT NULL,entry_hash TEXT NOT NULL)')

    def get(self, key, input_hash):
        row = self.conn.execute('SELECT input_hash,entry_hash FROM training_writes WHERE key=?', (key,)).fetchone()
        if row is None:
            return None
        if row[0] != input_hash:
            raise ShadowRefused('frozen_replay_inputs_changed')
        entry = self.mem.get([row[1]]).get(row[1])
        if entry is None:
            raise ShadowRefused('shadow_lesson_missing')
        return entry

    def write_lesson(self, *, key, input_hash, text, actor_id, run_id, replay_at, clock):
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            raise ShadowRefused('lesson_invalid')
        with S.write_transaction(self.conn):
            existing = self.get(key, input_hash)
            if existing is not None:
                return existing
            def actual_clock():
                value = clock()
                if type(value) not in (int,float) or not math.isfinite(value) or value < replay_at:
                    raise ShadowRefused('training_clock_unknown_or_backdated')
                return value
            identity = M.write(self.mem, actual_clock, dict(section='office/practice', kind='practice',
                text=text, author=actor_id, epoch_kind='discovery', counts_as_evidence=False,
                replay_derived=True, run_id=run_id, replay_at=replay_at, training_key=key))
            self.conn.execute('INSERT INTO training_writes VALUES(?,?,?)', (key, input_hash, identity))
            return self.mem.get([identity])[identity]

    def entries(self):
        return self.mem.entries()

    def close(self):
        self.conn.close()
