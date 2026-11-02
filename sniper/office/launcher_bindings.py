"""Host-only evidence that the UUID-owned launcher child has stopped."""
import json
from pathlib import Path
import subprocess
from uuid import UUID

from sniper.office.child_launcher import read_regular


def unit_properties(unit):
    result = subprocess.run(['systemctl', '--user', 'show', unit,
                             '--property=Id,ActiveState,SubState,MainPID,ControlPID,ControlGroup'],
                            capture_output=True, text=True, timeout=2, check=True)
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


class Completion:
    """Use only launcher-owned state and host cgroups, never a child-written receipt.

    Missing state or failed reads remain unknown. A client timeout is not completion.
    """
    def __init__(self, state_root, *, cgroup_root='/sys/fs/cgroup', probe=unit_properties):
        self.state_root, self.cgroup_root = Path(state_root), Path(cgroup_root)
        self.probe = probe

    def __call__(self, rid):
        try:
            if type(rid) is not str or str(UUID(rid)) != rid:
                return False
            receipt = json.loads(read_regular(self.state_root / (rid + '.result.json')))
            if receipt.get('request_id') != rid or receipt.get('outcome') not in (
                    'success', 'timeout', 'invalid', 'child_error', 'output_limit'):
                return False
            unit = 'office-model-' + rid + '.service'
            properties = self.probe(unit)
            if (properties.get('Id') != unit or properties.get('ActiveState') not in ('inactive', 'failed')
                    or properties.get('SubState') not in ('dead', 'failed')
                    or properties.get('MainPID') != '0' or properties.get('ControlPID') != '0'):
                return False
            group = properties.get('ControlGroup')
            if group == '':
                return True
            path = Path(group)
            if (not path.is_absolute() or '..' in path.parts or path.name != unit
                    or path.parent.name != 'office.slice'):
                return False
            events = read_regular(self.cgroup_root / group.lstrip('/') / 'cgroup.events').decode()
            fields = dict(line.split() for line in events.splitlines())
            return fields.get('populated') == '0'
        except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
            return False
