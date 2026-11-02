"""Render and verify the discovery pilot's base caps and separate per-child idle IO evidence."""
import json
import os
from pathlib import Path
import stat
from uuid import UUID
from scripts.office_watchdog import load_pilot,caps_applied
from sniper.office.child_launcher import extract_io_proof,read_regular


def render(manifest_path):
    manifest=load_pilot(manifest_path)
    return ('# Generated from SHA-pinned '+manifest['version']+'.\n'
            '# IOWeight is not enforceable in user scope; the child pre-exec idle IO check is mandatory.\n'
            '[Slice]\nCPUAccounting=yes\nMemoryAccounting=yes\nTasksAccounting=yes\nIOAccounting=yes\n'
            + '\n'.join(key+'='+str(manifest[key])+('%' if key=='CPUQuota' else '')
                        for key in ('CPUQuota','MemoryMax','TasksMax','IOWeight'))+'\n')


def read_child_receipt(path):
    path=Path(path)
    root=Path.home()/'Desktop/Sniper/data/office-control/launcher-state'
    if path.parent!=root or any(p.is_symlink() for p in (path,*path.parents)):
        raise ValueError('child_receipt_path')
    info=root.lstat();file_info=path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700
            or not stat.S_ISREG(file_info.st_mode) or file_info.st_uid!=os.getuid()
            or stat.S_IMODE(file_info.st_mode)!=0o600):
        raise ValueError('child_receipt_identity')
    rid=path.name.removesuffix('.result.json')
    if str(UUID(rid))!=rid or path.name!=rid+'.result.json':raise ValueError('child_receipt_id')
    value=json.loads(read_regular(path))
    if type(value)is not dict or value.get('request_id')!=rid:raise ValueError('child_receipt_id')
    return value


def verify(manifest_path,properties,*,kernel=caps_applied,child_receipts=()):
    manifest=load_pilot(manifest_path)
    expected=dict(CPUQuotaPerSecUSec=format(manifest['CPUQuota']/100,'g')+'s',
                  MemoryMax=str(manifest['MemoryMax']),TasksMax=str(manifest['TasksMax']))
    matches=all(properties.get(key)==value for key,value in expected.items())
    group=properties.get('ControlGroup');applied=False
    if matches and type(group)is str and group.startswith('/') and Path(group).name=='office.slice' and '..'not in Path(group).parts:
        try:applied=kernel(Path('/sys/fs/cgroup')/group.lstrip('/')) is True
        except Exception:applied=False
    evidence=[];invalid=False
    try:
        for receipt in child_receipts:
            rid=receipt.get('request_id')
            if type(rid)is not str or str(UUID(rid))!=rid:raise ValueError()
            proof=extract_io_proof('OFFICE_IO_PREEXEC '+json.dumps(receipt.get('io_preexec')),rid)
            if proof is None:raise ValueError()
            evidence.append(proof)
    except (ValueError,TypeError,AttributeError):invalid=True
    ready=matches and applied and not invalid
    state=('passed' if evidence else 'base_caps_ready') if ready else 'refused'
    return {'version':'office-pilot-caps-v2','state':state,'ready_for_child_preexec':ready,
            'expected':expected,'observed':{key:properties.get(key) for key in expected},
            'kernel_caps_applied':applied,'unit':'office.slice',
            'io':{'mechanism':'enforced_by_ioprio_idle','IOWeight':'not_enforceable_user_scope',
                  'observed_systemd_ioweight':properties.get('IOWeight'),
                  'evidence_state':'invalid' if invalid else 'verified' if evidence else 'pending_child_preexec',
                  'child_evidence':evidence}}
