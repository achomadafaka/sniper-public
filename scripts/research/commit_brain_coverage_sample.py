"""Draw the stratified commit sample (seat x window-third). Preregistered in docs/audits/*commit-brain-coverage*; read-only."""
import subprocess,re,json,random,datetime,collections,sys
import argparse,os
ap=argparse.ArgumentParser(description='Stratified commit sample for the commit-to-brain coverage study (read-only).')
ap.add_argument('out');ap.add_argument('--repo',default=os.getcwd());ap.add_argument('--start',required=True,help='UTC ISO window start (exclusive)');ap.add_argument('--end',required=True,help='UTC ISO window end (inclusive)')
ap.add_argument('--ref',default='main',help='commit-ish to sample from; pin the SHA for a reproducible population')
ap.add_argument('--seed',type=int,required=True);ap.add_argument('--n',type=int,default=100)
args=ap.parse_args()
R=args.repo
def git(*a):return subprocess.run(['git','-C',R,*a],capture_output=True,text=True,check=True).stdout
T0=datetime.datetime.fromisoformat(args.end.replace('Z','+00:00')).timestamp()
lo=datetime.datetime.fromisoformat(args.start.replace('Z','+00:00')).timestamp()
main=git('rev-parse',args.ref).strip()
rows=[l.split('\x1f') for l in git('log','--no-merges',main,'--format=%H%x1f%ct%x1f%s').splitlines()]
pop=[(h,int(ct),s) for h,ct,s in rows if lo<int(ct)<=T0]
# first-parent merge map: for each merge on main first-parent, the commits it brought in
fp=git('log','--merges','--first-parent',main,'--format=%H%x1f%s%x1f%b%x1e').split('\x1e')
merge_of={}
merges=[]
for rec in fp:
    rec=rec.strip('\n')
    if not rec:continue
    p=rec.split('\x1f');merges.append((p[0],p[1],p[2] if len(p)>2 else ''))
for h,subj,body in merges:
    try:
        ids=git('rev-list',h+'^1..'+h+'^2').split()
    except subprocess.CalledProcessError:continue
    for c in ids:merge_of.setdefault(c,(h,subj,body))
SEATS=['alpha','beta','gamma','delta','epsilon','zeta','lambda','codex']
def seat_in(text):
    t=text.lower()
    best=None
    for s in SEATS:
        m=re.search(r'\b'+s+r'\b',t)
        if m and (best is None or m.start()<best[0]):best=(m.start(),s)
    return best[1] if best else None
def seat_of(h,subj):
    m=merge_of.get(h)
    if m:
        mh,ms,mb=m
        s=seat_in(ms)
        if s:return s,mh
        b=re.search(r"Merge branch '([^']+)'",ms+' '+mb)
        if b:
            s=seat_in(b.group(1))
            if s:return s,mh
    s=seat_in(subj)
    return (s or 'unassigned'),(m[0] if m else None)
def week(ct):
    third=(T0-lo)/3
    d=T0-ct
    return 'W1' if d<third else 'W2' if d<2*third else 'W3'   # thirds of the window, newest first
cells=collections.defaultdict(list)
info={}
for h,ct,s in pop:
    seat,mh=seat_of(h,s)
    w=week(ct);cells[(seat,w)].append(h);info[h]=dict(sha=h,ct=ct,subject=s,seat=seat,week=w,merge=mh)
n=args.n
out={'main':main,'population':len(pop),'cells':{f'{k[0]}|{k[1]}':len(v) for k,v in cells.items()}}
if len(pop)<=n:
    sample=[h for h,_,_ in pop];alloc={k:len(v) for k,v in cells.items()}
else:
    keys=sorted(cells)
    assert len(keys)<=n,('too many cells',len(keys))
    alloc={k:1 for k in keys}
    rest=n-len(keys)
    extra={k:len(cells[k])-1 for k in keys}
    tot=sum(extra.values())
    quota={k:rest*extra[k]/tot for k in keys}
    base={k:min(int(quota[k]),extra[k]) for k in keys}
    left=rest-sum(base.values())
    while left>0:
        cand=sorted([k for k in keys if base[k]<extra[k]],key=lambda k:(-(quota[k]-base[k]),k))
        if not cand:break
        base[cand[0]]+=1;left-=1
    alloc={k:1+base[k] for k in keys}
    rng=random.Random(args.seed);sample=[]
    for k in keys:
        L=sorted(cells[k]);rng.shuffle(L);sample+=L[:alloc[k]]
out['alloc']={f'{k[0]}|{k[1]}':v for k,v in alloc.items()}
out['sample']=[info[h] for h in sample]
out['N_h']={f'{k[0]}|{k[1]}':len(v) for k,v in cells.items()}
json.dump(out,open(args.out,'w'),indent=1)
print('main',main,'pop',len(pop),'cells',len(cells),'sample',len(sample))
print(sorted(collections.Counter(i['seat'] for i in out['sample']).items()))
print(sorted(collections.Counter(i['week'] for i in out['sample']).items()))
print('unassigned pop',sum(len(v) for k,v in cells.items() if k[0]=='unassigned'))
