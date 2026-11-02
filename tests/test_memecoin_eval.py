from sniper.replay.memecoin_eval import (recency_weight, effective_sample_size, paired_delta,
    sign_flip_p, leave_two_out_positive, graduation_ok)

def test_recency_weight_halves_at_half_life():
    assert abs(recency_weight(48, 48) - 0.5) < 1e-9

def test_ess_matches_formula_order():
    # 60 episodes over 10 days, half-life 2d -> ESS ~ 2.9*6*2 ~ 35 (ballpark; monotone check)
    ws = [recency_weight(h, 48) for h in range(0, 240, 4)]
    ess = effective_sample_size(ws)
    assert 10 < ess < len(ws)

def test_paired_delta_recency_weighted():
    pairs = [{"d": 1.0, "age_hours": 0, "block_id": 0}, {"d": -1.0, "age_hours": 240, "block_id": 1}]
    assert paired_delta(pairs, 48) > 0     # recent +1 outweighs old -1

def test_leave_two_out_drops_best_two():
    pairs = [{"d": 100.0, "age_hours": 0, "block_id": 0}, {"d": 100.0, "age_hours": 0, "block_id": 1},
             {"d": -1.0, "age_hours": 0, "block_id": 2}, {"d": -1.0, "age_hours": 0, "block_id": 3}]
    assert leave_two_out_positive(pairs, 48) is False   # edge collapses without the two big winners

def test_graduation_reports_all_gates():
    pairs = [{"d": 1.0, "age_hours": i, "block_id": i // 6} for i in range(40)]
    res = graduation_ok(pairs, half_life_hours=96, ess_floor=5)
    assert set(res) >= {"ess", "ess_ok", "delta", "recent_block_ok", "leave_two_out_ok", "pass"}

def test_sign_flip_p_is_bounded_below_never_zero():
    from sniper.replay.memecoin_eval import sign_flip_p
    # all-positive differences: observed stat is the max possible -> p must be the floor 1/(n+1), not 0
    allpos = [{"d": 1.0 + i * 0.1, "age_hours": 0, "block_id": 0} for i in range(40)]
    p = sign_flip_p(allpos, 96, n=500)
    assert p > 0.0 and p <= 1.0 / (500 + 1) + 1e-9   # exactly the floor, never zero
    # a non-positive observed delta -> no significance
    assert sign_flip_p([{"d": -1.0, "age_hours": 0, "block_id": 0}], 96, n=500) == 1.0
