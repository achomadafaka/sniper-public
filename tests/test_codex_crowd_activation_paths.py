"""Source unit contract only; no environment files or running services are read."""
import configparser
from pathlib import Path
import shlex


def test_pilot_unit_binds_host_state_after_environment_file():
    p=configparser.ConfigParser(interpolation=None)
    p.read(Path(__file__).resolve().parents[1]/'deploy/office/memecoin-large-buyer-pilot.service')
    service=p['Service'];argv=shlex.split(service['ExecStart'])
    assert argv[:2]==['/usr/bin/env','SNIPER_STATE_ROOT=%h/Desktop/Sniper']
    assert argv[2]=='%h/.pyenv/versions/3.13.0/bin/python3'
    assert argv[3]=='scripts/memecoin_large_buyer_pilot.py'
    assert service['WorkingDirectory']=='%h/Desktop/Sniper/.runtime/memecoin/current'
    assert argv[argv.index('--ledger')+1]=='%h/Desktop/Sniper/sniper_tracker.db'
    assert argv[argv.index('--arm')+1]=='%h/Desktop/Sniper/data/office/v2/large-buyer-arm/arm.db'
    assert service['EnvironmentFile']=='%h/Desktop/Sniper/.env'
    assert not any(x.startswith(('MEMECOIN_KILL_SWITCH_FILE=','SNIPER_CLOCK_OFFSET_FILE=')) for x in argv)
