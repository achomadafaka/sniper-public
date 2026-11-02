"""Bounded registered-stream tail. Host adapters still verify producer semantics.

First attachment starts at EOF so inherited history never triggers a new swap.
Rotation/truncation requires explicit host reconciliation; it never auto-rebases.
Only offsets/identity are persisted, never transcript text or partial lines.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .crew_session_bridge import BridgeError, private, private_dir, read_json, write_json


class RegisteredTail:
    def __init__(self,path,cursor,*,allowlist,version,pinned_version):
        self.path=Path(path).absolute();self.cursor=Path(cursor).absolute()
        if self.path not in {Path(p).absolute() for p in allowlist}:raise ValueError('transcript_not_registered')
        if self.path==self.cursor:raise ValueError('cursor_is_transcript')
        private_dir(self.cursor.parent)
        self.version=version;self.pinned_version=pinned_version

    def read(self):
        unknown=lambda reason:dict(state='unknown',reason=reason,records=[],needs_page=True)
        if not isinstance(self.version,str) or not self.version or self.version!=self.pinned_version:
            return unknown('producer_version_unverified')
        lock=None;fd=None
        try:
            lock=os.open(self.cursor.with_suffix('.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return unknown('tail_busy')
            before=private(self.path)
            fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
            stat=os.fstat(fd)
            binding=dict(path=str(self.path),device=stat.st_dev,inode=stat.st_ino,version=self.version)
            if (before.st_dev,before.st_ino)!=(stat.st_dev,stat.st_ino):return unknown('transcript_changed')
            if not self.cursor.exists():
                write_json(self.cursor,dict(binding,offset=stat.st_size,edge_hash=hashlib.sha256(os.pread(fd,min(512,stat.st_size),max(0,stat.st_size-512))).hexdigest()))
                return dict(state='initializing',records=[])
            if private(self.cursor).st_size>4096:return unknown('cursor_oversized')
            saved=read_json(self.cursor)
            offset=saved.get('offset')
            if any(saved.get(k)!=v for k,v in binding.items()):return unknown('transcript_identity_changed')
            if type(offset) is not int or offset<0 or stat.st_size<offset:return unknown('transcript_truncated')
            edge=hashlib.sha256(os.pread(fd,min(512,offset),max(0,offset-512))).hexdigest()
            if saved.get('edge_hash')!=edge:return unknown('transcript_prefix_changed')
            os.lseek(fd,offset,os.SEEK_SET);raw=os.read(fd,65536)
            last=raw.rfind(b'\n')
            if last<0:
                if len(raw)==65536:return unknown('transcript_line_oversized')
                return dict(state='pending' if raw else 'known',records=[])
            records=[]
            for line in raw[:last].split(b'\n'):
                if not line:continue
                value=json.loads(line)
                if not isinstance(value,dict):return unknown('transcript_record_invalid')
                records.append(value)
            after=private(self.path)
            if (after.st_dev,after.st_ino)!=(stat.st_dev,stat.st_ino) or after.st_size<offset+len(raw):
                return unknown('transcript_changed')
            next_offset=offset+last+1
            write_json(self.cursor,dict(binding,offset=next_offset,edge_hash=hashlib.sha256(os.pread(fd,min(512,next_offset),max(0,next_offset-512))).hexdigest()))
            return dict(state='known',records=records)
        except (BridgeError,OSError,ValueError,TypeError,KeyError):return unknown('transcript_read_failed')
        finally:
            if fd is not None:os.close(fd)
            if lock is not None:os.close(lock)
