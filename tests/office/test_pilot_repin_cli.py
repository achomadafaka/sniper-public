"""Full CLI through real sealed-bundle validators; all Git, SQLite, and systemd state is synthetic."""
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess

from .test_pilot_activate import env,sha,SCRIPT
from .test_pilot_repin import helper,state_store


def test_complete_cli_repin_dry_run_apply_verify_and_rollback(env,state_store):
    e=env;repo=e['repo'];git=e['git'];m,runtime,tool,store,ledger=state_store
    source=SCRIPT.parents[1]/'config/office/prompts/discovery2'
    shutil.copytree(source,repo/'config/office/prompts/discovery2')
    (repo/'sniper/office/runtime_steps.py').write_text("_REFS={}\n_REQUEST_ARGS={}\nSCHEMA={'type':'object'}\n")
    git('add','--','config/office/prompts/discovery2','sniper/office/runtime_steps.py');git('commit','-m','fixture discovery2')
    old=git('rev-parse','HEAD');old_release=repo/'.runtime/office/releases'/old
    bundle=e['bundle'];manifest=e['manifest'];first=e['release']

    def rewrite_bundle(root,old_root,new_root,new_sha):
        doc=json.loads((root/'manifest.json').read_text())
        for name in doc['files']:
            if not name.endswith('.json'):continue
            raw=(root/name).read_text().replace(str(old_root),str(new_root))
            (root/name).write_text(raw)
        for name in ('runtime','launcher'):
            (root/(name+'.sha256')).write_text(sha((root/(name+'.json')).read_bytes())+'\n')
        doc.update(main_sha=new_sha,release_root=str(new_root),files={n:sha((root/n).read_bytes()) for n in doc['files']})
        (root/'manifest.json').write_text(json.dumps(doc));return sha((root/'manifest.json').read_bytes())

    config=json.loads((bundle/'runtime.json').read_text())
    approval=json.loads((source/'sha256.json').read_text())
    config['pilot_seed_launcher_config']['prompts']={s:dict(text=(source/(s+'.md')).read_text(),sha256=approval['files'][s+'.md'])
                       for s in ('head','scout','judge_claude','judge_gpt')}
    (bundle/'runtime.json').write_text(json.dumps(config))
    old_pin=rewrite_bundle(bundle,first,old_release,old)
    def run(target,target_bundle,pin,*flags):
        return subprocess.run(['bash',str(SCRIPT),'--repo',str(repo),'--sha',target,'--bundle',str(target_bundle),
                               '--bundle-sha256',pin,*flags],env=e['child_env'],capture_output=True,text=True,timeout=20)
    initial=run(old,bundle,old_pin,'--apply');assert initial.returncode==3,initial.stdout+initial.stderr

    # Match the synthetic sealed epoch's prompts to the actual approved fixture profile.
    c=sqlite3.connect(store)
    frozen=json.loads(c.execute('SELECT payload_json FROM org_changes').fetchone()[0])
    for slot,actor in frozen['new_actors'].items():
        actor['prompt_hash']=approval['files'][slot+'.md']
        c.execute('UPDATE agents SET payload_json=? WHERE id=?',(json.dumps(actor),slot))
    raw=json.dumps(frozen,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode();receipt=sha(raw)
    c.execute('UPDATE org_changes SET row_hash=?,payload_json=?',(receipt,raw.decode()))
    c.execute('UPDATE office_epoch_control SET payload_json=?',(json.dumps(dict(active_epoch='office-pilot-discovery-2',receipt_id=receipt)),))
    c.commit();c.close()
    shutil.copyfile(store,repo/'data/office/office.db');shutil.copyfile(ledger,repo/'data/office/ledger/ledger.db')
    shutil.copyfile(runtime['clock_file'],repo/'data/clock_offset.json')

    (repo/'reporting-only').write_text('no tool or prompt change')
    git('add','--','reporting-only');git('commit','-m','fixture reporting upgrade');new=git('rev-parse','HEAD')
    new_release=repo/'.runtime/office/releases'/new;new_bundle=bundle.parent/'next-bundle';shutil.copytree(bundle,new_bundle)
    new_pin=rewrite_bundle(new_bundle,old_release,new_release,new)
    flags=['--repin-from-bundle',str(bundle),'--repin-from-bundle-sha256',old_pin]
    before=(repo/'data/office/office.db').read_bytes()
    dry=run(new,new_bundle,new_pin,*flags);assert dry.returncode==0,dry.stdout+dry.stderr
    assert json.loads(dry.stdout)['state']=='dry_run' and not new_release.exists()
    applied=run(new,new_bundle,new_pin,*flags,'--apply');assert applied.returncode==0,applied.stdout+applied.stderr
    assert json.loads(applied.stdout)['state']=='repinned'
    assert (repo/'.runtime/office/current').resolve()==new_release
    assert (repo/'data/office/office.db').read_bytes()==before
    verified=run(new,new_bundle,new_pin,*flags,'--verify');assert verified.returncode==0,verified.stdout+verified.stderr
    assert json.loads(verified.stdout)['state']=='repin_verified'
    rolled=run(new,new_bundle,new_pin,*flags,'--rollback','--apply');assert rolled.returncode==0,rolled.stdout+rolled.stderr
    assert (repo/'.runtime/office/current').resolve()==old_release
