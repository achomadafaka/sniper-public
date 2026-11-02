from pathlib import Path
import subprocess

def test_all_six_seats_reach_pixel_floor():
    code=r'''
const assert=require('node:assert/strict'),F=require('./static/office-floor.js');
const roles=['head','judge','scout','researcher','forensics','narrative'];
const feed={as_of:100,roster:{state:'known',items:roles.map(role=>({actor_id:role,role}))},seats:{state:'known',items:roles.map(role=>({actor_id:role,activity:{kind:'sleeping',at:99}}))}};
assert.deepEqual(F.v2Scene(feed).map(s=>s.role),roles);
'''
    r=subprocess.run(['node','-e',code],cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True)
    assert r.returncode==0,r.stderr
