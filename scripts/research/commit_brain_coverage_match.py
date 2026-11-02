"""Match a commit sample to brain facts/episodes by the preregistered M1/M2 rules and score the outcomes. Read-only."""
import sys,os,json,re,subprocess,datetime,random,collections
sys.path.insert(0,'/home/bigpapi/Desktop/Sniper')
os.chdir('/home/bigpapi/Desktop/Sniper')
from sniper.util.envfiles import load_env_files
load_env_files('/home/bigpapi/Desktop/Sniper')
from bot.brain_graph.store import RestStore
from bot.brain_graph import READ_STATUSES
import argparse
ap=argparse.ArgumentParser(description='Match sampled commits to brain facts and episodes (read-only).')
ap.add_argument('sample');ap.add_argument('out');ap.add_argument('--repo',default=os.getcwd());ap.add_argument('--end',required=True,help='UTC ISO window end')
args=ap.parse_args()
R=args.repo
d=json.load(open(args.sample))
T0=datetime.datetime.fromisoformat(args.end.replace('Z','+00:00')).timestamp()
st=RestStore()
def ts(v):
    if not v:return None
    try:return datetime.datetime.fromisoformat(str(v).replace('Z','+00:00')).timestamp()
    except Exception:return None
facts=st._all('brain_facts')
print('facts',len(facts),file=sys.stderr)
since=datetime.datetime.fromtimestamp(T0-22*86400,datetime.timezone.utc).isoformat()
eps=st._all('brain_episodes',filters=(('recorded_at','gte',since),('author','neq','import')))
print('episodes',len(eps),file=sys.stderr)
HEX=re.compile(r'(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])')
def recs():
    for f in facts:
        text=' '.join(str(f.get(k) or '') for k in ('statement','detail'))+' '+json.dumps(f.get('evidence') or '')
        yield ('fact',f['id'],text,ts(f.get('recorded_at')),f)
    for e in eps:
        text=str(e.get('body') or '')+' '+str(e.get('source_ref') or '')
        yield ('episode',e['id'],text,ts(e.get('occurred_at')) or ts(e.get('recorded_at')),e)
R_=list(recs())
tok={}
for i,(k,rid,text,t,row) in enumerate(R_):
    for m in set(HEX.findall(text.lower())):tok.setdefault(m,set()).add(i)
def git(*a):return subprocess.run(['git','-C',R,*a],capture_output=True,text=True).stdout
def unique(prefix):
    return len(git('rev-parse','--disambiguate='+prefix).split())==1
STOP={'__init__.py','README.md','conftest.py'}
def keys(c):
    shas=[c['sha']]+([c['merge']] if c['merge'] else [])
    files=[f for f in git('show','--name-only','--format=',c['sha']).split('\n') if f]
    P=set(files)|{os.path.basename(f) for f in files if len(os.path.basename(f))>=12 and os.path.basename(f) not in STOP}
    return shas,P
def m1(shas):
    hit=set()
    for sha in shas:
        for L in range(7,41):
            t=sha[:L]
            if t in tok:
                if L<9 and not unique(t):continue
                hit|=tok[t]
    return hit
res=[];ask_cache={}
READ=set(READ_STATUSES)
def ask(q):
    if q in ask_cache:return ask_cache[q]
    try:o=subprocess.run(['python3','scripts/brain_cli.py','ask',q],capture_output=True,text=True,timeout=60).stdout
    except Exception:o=''
    ask_cache[q]=o;return o
for c in d['sample']:
    shas,P=keys(c)
    M1=m1(shas)
    M2=set()
    for i,(k,rid,text,t,row) in enumerate(R_):
        if i in M1 or t is None:continue
        if c['ct']-86400<=t<=c['ct']+14*86400 and any(p in text for p in P):M2.add(i)
    def split(ix):
        return [R_[i] for i in ix]
    out={}
    for variant,ix in (('strict',M1),('broad',M1|M2)):
        lf=[r for r in split(ix) if r[0]=='fact'];le=[r for r in split(ix) if r[0]=='episode']
        act=[r for r in lf if (r[4].get('status') in READ and r[4].get('valid_to') is None)]
        found=[]
        if act:
            Q=re.sub(r'^\w+(\([^)]*\))?!?:\s*','',c['subject'])
            txt=ask(c['sha'][:9])+'\n'+ask(Q)
            found=[r for r in act if r[1][:8] in txt]
        cat='FACT-RETRIEVABLE' if found else 'FACT-NOT-FOUND' if lf else 'EPISODE-ONLY' if le else 'NOTHING'
        out[variant]=dict(cat=cat,facts=[r[1][:8] for r in lf],episodes=[r[1][:8] for r in le])
    kind=re.match(r'^(feat|fix|test|docs|refactor|chore|research|perf|revert|build|ci|wip)\b',c['subject'])
    res.append(dict(sha=c['sha'],seat=c['seat'],week=c['week'],kind=kind.group(1) if kind else 'other',subject=c['subject'][:90],**out))
# negative control
rng=random.Random(20261031);neg=0
for _ in range(10):
    fake=''.join(rng.choice('0123456789abcdef') for _ in range(40))
    assert not git('cat-file','-t',fake).strip()
    if m1([fake]):neg+=1
json.dump(dict(main=d['main'],population=d['population'],N_h=d['N_h'],alloc=d['alloc'],n_facts=len(facts),n_episodes=len(eps),neg_control_matches=neg,results=res),open(args.out,'w'),indent=1)
print('done',len(res),'neg',neg)
