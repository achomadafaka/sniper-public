#!/usr/bin/env python3
"""Pilot telemetry: bounded procfs/cgroup/heartbeat reads, no funded ledger handle."""
import argparse
import json
import math
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from office_measure_headroom import pressure, read_json, _available


PILOT_ROSTER_SHA256 = 'a1edd52ee9357fcbb6e28d3fdb78f0c375f9b5fd2f2146f33c97fd96e468ab22'


def pinned_roster(config):
    import hashlib
    try:
        raw = Path(config['roster_file']).read_bytes()
        return hashlib.sha256(raw).hexdigest() == PILOT_ROSTER_SHA256
    except (OSError, KeyError, TypeError):
        return False


def health_sample(config, *, now_host):
    from office_watchdog import caps_applied, finite
    output = dict(ts=now_host, funded_heartbeat_age_s=None, cpu_psi_some_avg10=None,
                  io_psi_some_avg10=None, mem_available_bytes=None, base_caps_applied=False,
                  guard_policy_version="office-host-guard-pilot-v3",
                  roster_valid=False, resources_verified=False)
    try:
        output['cpu_psi_some_avg10'] = pressure('cpu')
        output['io_psi_some_avg10'] = pressure('io')
        output['mem_available_bytes'] = _available()
        beat = read_json(config['funded_heartbeat'])
        if beat.get('auth_ok') is not True or not finite(beat.get('ts')) or beat['ts'] > now_host:
            raise ValueError('funded_heartbeat_unknown')
        output['funded_heartbeat_age_s'] = now_host - beat['ts']
        output['base_caps_applied'] = caps_applied(config['office_cgroup'])
        output['roster_valid'] = pinned_roster(config)
        # Pilot has no research RPC/VM resource. T12 must prove separate resources before enabling one.
        output['resources_verified'] = config.get('research_enabled') is False
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        output['telemetry_error'] = True
    return output


def monitor(config_path):
    from office_watchdog import write_atomic
    config = read_json(config_path)
    while True:
        write_atomic(config['sample_out'], health_sample(config, now_host=time.time()))
        time.sleep(1.)  # guard debounces heartbeat at 2 s and pressure/memory at 5 s


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    monitor(parser.parse_args().config)
