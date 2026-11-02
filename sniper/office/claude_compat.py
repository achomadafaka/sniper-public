"""Release-local Linux pilot SDK admission. No host SDK fallback, auth reads or pip installs."""
from contextlib import contextmanager
import hashlib
from importlib import metadata
import os
from pathlib import Path,PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

SDK_VERSION='0.2.174'
WHEEL_SHA256='728592effdfe63e96fbad7ba3b2ddf642e3aad344e73692b429898fbfcb33f12'
CLI_SHA256='b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56'
CLI_BYTES=291638640
MIN_CLI=(2,1,280)
WHEEL_FILE='claude-sdk.whl'
VENDOR_REL='vendor/claude_sdk'
VENDOR_ROOT=Path(__file__).resolve().parents[2]/VENDOR_REL
DIST_INFO='claude_agent_sdk-'+SDK_VERSION+'.dist-info'
CLI_REL='claude_agent_sdk/_bundled/claude'


def runtime_pins(release):
    return dict(root=str(Path(release)/VENDOR_REL),version=SDK_VERSION,wheel_sha256=WHEEL_SHA256,cli_sha256=CLI_SHA256)


def artifact_manifest():
    return dict(wheel_file=WHEEL_FILE,version=SDK_VERSION,wheel_sha256=WHEEL_SHA256,cli_sha256=CLI_SHA256)


@contextmanager
def _regular(path,maximum):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size>maximum:raise ValueError('claude_sdk_artifact_invalid')
        yield stream


@contextmanager
def _wheel(path):
    with _regular(path,400_000_000) as stream:
        if hashlib.file_digest(stream,'sha256').hexdigest()!=WHEEL_SHA256:raise ValueError('claude_sdk_wheel_hash_mismatch')
        stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            entries=archive.infolist();names=[i.filename for i in entries]
            if len(entries)>256 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>CLI_BYTES+16_777_216:
                raise ValueError('claude_sdk_wheel_invalid')
            for item in entries:
                rel=PurePosixPath(item.filename)
                if (rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0] not in ('claude_agent_sdk',DIST_INFO)
                        or stat.S_ISLNK(item.external_attr>>16)):raise ValueError('claude_sdk_wheel_path_invalid')
            if CLI_REL not in names or archive.getinfo(CLI_REL).file_size!=CLI_BYTES:raise ValueError('claude_cli_hash_mismatch')
            yield archive


def wheel_refusal(path):
    try:
        with _wheel(path):return None
    except (OSError,ValueError,zipfile.BadZipFile):return 'claude_sdk_wheel_invalid'


def verify_vendor(root=None):
    root=Path(root) if root is not None else VENDOR_ROOT
    try:
        if root.is_symlink() or not root.is_dir():return 'claude_sdk_vendor_missing'
        with _wheel(root/WHEEL_FILE) as archive:
            entries=[i for i in archive.infolist() if not i.is_dir()]
            expected={i.filename for i in entries}|{WHEEL_FILE}
            directories={str(p) for name in expected for p in PurePosixPath(name).parents if str(p)!='.'}
            found=set()
            for path in root.rglob('*'):
                rel=path.relative_to(root).as_posix()
                if path.is_symlink() or (path.is_dir() and rel not in directories):return 'claude_sdk_vendor_mismatch'
                if path.is_file():
                    if rel not in expected:return 'claude_sdk_vendor_mismatch'
                    found.add(rel)
            if found!=expected:return 'claude_sdk_vendor_mismatch'
            for item in entries:
                expected_hash=CLI_SHA256 if item.filename==CLI_REL else hashlib.sha256(archive.read(item)).hexdigest()
                with _regular(root/item.filename,item.file_size) as stream:
                    if hashlib.file_digest(stream,'sha256').hexdigest()!=expected_hash:return 'claude_cli_hash_mismatch' if item.filename==CLI_REL else 'claude_sdk_vendor_mismatch'
        dist=metadata.Distribution.at(root/DIST_INFO)
        if dist.metadata['Name']!='claude-agent-sdk' or dist.version!=SDK_VERSION:return 'claude_sdk_version_mismatch'
        return None
    except (OSError,ValueError,zipfile.BadZipFile):return 'claude_sdk_vendor_mismatch'


def install_wheel(wheel,root):
    """Installer only: stage and verify before rename; never overwrites an existing tree."""
    root=Path(root)
    if os.path.lexists(root):
        reason=verify_vendor(root)
        if reason:raise ValueError(reason)
        return
    root.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.parent.is_symlink():raise ValueError('claude_sdk_vendor_path_invalid')
    with tempfile.TemporaryDirectory(prefix='.claude_sdk-',dir=root.parent) as folder:
        stage=Path(folder)
        with _wheel(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir():continue
                target=stage/item.filename;target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                with archive.open(item) as src,target.open('xb') as dst:shutil.copyfileobj(src,dst,1_048_576)
                target.chmod(0o500 if item.filename==CLI_REL else 0o400)
        shutil.copyfile(wheel,stage/WHEEL_FILE);(stage/WHEEL_FILE).chmod(0o400)
        reason=verify_vendor(stage)
        if reason:raise ValueError(reason)
        if os.path.lexists(root):raise ValueError('claude_sdk_vendor_already_exists')
        os.rename(stage,root)


def remove_vendor(root):
    if not os.path.lexists(root):return
    reason=verify_vendor(root)
    if reason:raise ValueError(reason)
    shutil.rmtree(root)


def bundled_cli():return VENDOR_ROOT/CLI_REL


def activate_vendor():
    """Only the isolated model-child process calls this; never the threaded funded runner."""
    loaded=sys.modules.get('claude_agent_sdk')
    if loaded is not None and not Path(loaded.__file__).resolve().is_relative_to(VENDOR_ROOT):return 'claude_sdk_import_conflict'
    if str(VENDOR_ROOT) not in sys.path[:1]:sys.path.insert(0,str(VENDOR_ROOT))
    sys.dont_write_bytecode=True
    return None


def startup_refusal():
    reason=verify_vendor()
    if reason:return reason
    try:
        result=subprocess.run([str(bundled_cli()),'--version'],stdin=subprocess.DEVNULL,capture_output=True,text=True,
            timeout=3,cwd='/',env={'HOME':'/nonexistent','CLAUDE_CONFIG_DIR':'/nonexistent','PATH':'/usr/bin:/bin'})
        match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+) \(Claude Code\)\s*',result.stdout)
        if result.returncode or match is None:return 'claude_cli_version_unknown'
        if tuple(map(int,match.groups()))<MIN_CLI:return 'claude_cli_too_old'
        return None
    except (OSError,ValueError,subprocess.SubprocessError):return 'claude_cli_version_unknown'
