"""Compute required sample size per stratum.

Detect effect size = 0.02 EV (2pp) on attempted markets.
Assumed SD of per-market net PnL = sigma (see sensitivity table).
Significance: alpha = 0.0125 (Bonferroni-4 for 4 strata at overall 0.05).
Power: 0.80.
Test: one-sample t-test against H0: EV=0 (one-sided).

Reports required N for a range of sigma values.
"""
import math


def required_n(effect, sigma, alpha, power):
    """One-sample t-test sample size approximation via normal.

    n = ((Z_{1-alpha} + Z_{power}) * sigma / effect)^2
    """
    from statistics import NormalDist
    z_alpha = NormalDist().inv_cdf(1 - alpha)
    z_beta = NormalDist().inv_cdf(power)
    return math.ceil(((z_alpha + z_beta) * sigma / effect) ** 2)


def required_n_two_sided(effect, sigma, alpha, power):
    from statistics import NormalDist
    z_alpha = NormalDist().inv_cdf(1 - alpha / 2)
    z_beta = NormalDist().inv_cdf(power)
    return math.ceil(((z_alpha + z_beta) * sigma / effect) ** 2)


def main():
    effect = 0.02
    alpha = 0.0125
    power = 0.80
    print(f"Detection target: {effect} EV per attempted market")
    print(f"alpha={alpha} (Bonferroni-4, one-sided), power={power}")
    print(f"One-sample test against H0: EV=0")
    print()
    print(f"{'sigma':>8} | {'N one-sided':>12} | {'N two-sided':>12} | {'interpretation':40}")
    print("-" * 90)
    for sigma in [0.05, 0.08, 0.10, 0.12, 0.15, 0.20, 0.30, 0.50]:
        n1 = required_n(effect, sigma, alpha, power)
        n2 = required_n_two_sided(effect, sigma, alpha, power)
        interp = (
            "trivially feasible" if n1 < 100 else
            "modest sample" if n1 < 500 else
            "substantial sample" if n1 < 2000 else
            "large sample, tight" if n1 < 10000 else
            "infeasible"
        )
        print(f"{sigma:>8.2f} | {n1:>12} | {n2:>12} | {interp}")

    print()
    print("--- Effect-size sensitivity (sigma=0.10, one-sided, Bonferroni-4) ---")
    print(f"{'effect':>8} | {'required_N':>12} | {'interpretation':40}")
    print("-" * 70)
    for eff in [0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.075]:
        n = required_n(eff, 0.10, alpha, power)
        interp = (
            "trivially feasible" if n < 100 else
            "modest sample" if n < 500 else
            "substantial sample" if n < 2000 else
            "large sample, tight" if n < 10000 else
            "infeasible"
        )
        print(f"{eff:>8.3f} | {n:>12} | {interp}")


if __name__ == "__main__":
    main()
