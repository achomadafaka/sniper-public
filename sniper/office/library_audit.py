"""Bounded preflight for the approved read-only user library mount; never executes .pth files."""
import os
from pathlib import Path
import stat
import time

MAX_ENTRIES=20_000
MAX_PTH_BYTES=65_536


def audit(path):
    root=Path(path)
    if not root.is_absolute() or any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('library_root')
    deadline=time.monotonic()+5
    stack=[root];count=pth_count=0
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                count+=1
                if count>MAX_ENTRIES or time.monotonic()>deadline:
                    raise ValueError('library_audit_budget')
                if entry.is_symlink():
                    # Conservative: even internal links refuse, so no unwalked alias can hide an executable .pth.
                    raise ValueError('library_symlink')
                info=entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(Path(entry.path))
                elif not stat.S_ISREG(info.st_mode):
                    raise ValueError('library_special_file')
                elif entry.name.endswith('.pth'):
                    pth_count+=1
                    fd=os.open(entry.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
                    try:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):raise ValueError('library_special_file')
                        with os.fdopen(fd,'rb',closefd=False) as source:raw=source.read(MAX_PTH_BYTES+1)
                    finally:os.close(fd)
                    if len(raw)>MAX_PTH_BYTES:raise ValueError('library_pth_budget')
                    if any(line.lstrip().startswith(('import ','import\t')) for line in raw.decode('utf-8-sig').splitlines()):
                        raise ValueError('library_executable_pth')
    return {'state':'passed','root':str(root),'entries':count,'pth_files':pth_count,'symlinks':0}
