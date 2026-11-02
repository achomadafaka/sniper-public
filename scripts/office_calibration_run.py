"""Run the Office calibration for one k and write the content-addressed proof artifact. Simulation only, no I/O but the output file."""
import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.office.readiness import calibration as C  # noqa: E402
from sniper.office.readiness import verdict as V  # noqa: E402

PLAN = ROOT / "docs/audits/2026-10-28-office-calibration-plan.md"


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--k", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--block-days", type=int, default=5)
    p.add_argument("--alpha-scale", type=float, default=0.1)
    p.add_argument("--n-coins", type=int, default=240)
    p.add_argument("--delta0", type=float, default=1.0)
    p.add_argument("--delta1", type=float, default=2.5)
    p.add_argument("--sigma", type=float, default=3.0)
    a = p.parse_args(argv)
    results = []
    for g in C.GENERATORS:
        results.append(C.calibrate(k=a.k, generator=g, n_coins=a.n_coins, days=a.days, delta0=a.delta0, delta1=a.delta1,
                                   sigma=a.sigma, compute_budget_cells=5e13, seed=6000 + a.k, block_days=a.block_days,
                                   alpha_scale=a.alpha_scale))
        print(g, results[-1].supported, results[-1].type1_upper, results[-1].power_lower, flush=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=ROOT).stdout.strip()
    proof = C.make_proof(results, params={"n_coins": a.n_coins, "days": a.days, "block_days": a.block_days,
                                         "alpha_scale": a.alpha_scale, "day_method": "t_block", "delta0": a.delta0,
                                         "delta1": a.delta1, "sigma": a.sigma, "draws": V.draws_for(V.alpha_k(a.k)),
                                         "alpha_k": V.alpha_k(a.k)},
                         code_commit=commit, plan_sha256=hashlib.sha256(PLAN.read_bytes()).hexdigest(),
                         created_at=time.time())
    Path(a.out).write_text(json.dumps(proof, indent=1, sort_keys=True) + "\n")
    print("proof", proof["proof_id"], "supported_for_k", proof["supported_for_k"], "power_claim_for_k", proof["power_claim_for_k"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
