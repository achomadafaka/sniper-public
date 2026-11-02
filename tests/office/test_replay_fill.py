"""Task 8: counterfactual fill = the Office ledger engine itself (same ps_paper quotes, same rails, same costs) run on a SCRATCH in-memory book. Quotes stubbed as in test_ledger."""
import pytest

from sniper.office import ledger as L
from sniper.office import replay_fill as R

MINT = "Mint1111111111111111111111111111111111111pump"
T = 1_800_000_000.0
FX = 150.0


def policy(**over):
    base = dict(enabled=True, chains=("solana",), clip_usd=30.0, rail_bounds={}, max_concurrent=3, daily_loss_usd=100.0, fill_model_version="office-fill-v1", sl_pct=30.0, tp_pct=100.0,
                trail_pct=0.0, catastrophe_pct=60.0, max_hold_s=3600.0, cap_min_headroom_pct=0.0, fx_max_age_s=120.0, entry_timeout_s=60.0)
    base.update(over)
    return L.OfficeBookPolicy(**base)


class Tape:
    """value(now) -> lamports for the full position (None = unquotable with reason)."""
    def __init__(self, f, buy_why=None):
        self.f, self.buy_why = f, buy_why

    def buy(self, desk_dir, mint, spend, at, *, evaluated_at, integrity=False, **kw):
        if self.buy_why:
            return None, self.buy_why
        return {"tokens_atomic": str(spend * 1000), "spend_lamports": str(spend), "cash_left_atomic": "0", "pool": "P", "proof": {"slot": 1}}, None

    def sell(self, desk_dir, mint, qty, at, *, evaluated_at, integrity=False, **kw):
        v = self.f(at)
        if isinstance(v, str):
            return None, v
        return {"lamports": str(int(v * int(qty) // 1000)), "pool": "P", "proof": {"slot": 2}}, None


@pytest.fixture
def stub(monkeypatch):
    def install(tape):
        monkeypatch.setattr(L, "_ps_buy", tape.buy)
        monkeypatch.setattr(L, "_ps_sell", tape.sell)
        monkeypatch.setattr(L, "_canonical_pool", lambda mint: "P")
    return install


def run(pol, t_decision=T, horizon=900, tick=2.0, **kw):
    kw.setdefault("bound_usd_per_fill", 0.)
    kw.setdefault("clock_sample_fn", lambda at: {"clock_sample_id": "sample-1", "offset_s": 0., "sampled_at": T - 10})
    return R.counterfactual(pol, "/nonexistent", network_id="solana", mint=MINT, pool="P", t_decision=t_decision, horizon_s=horizon, sol_usd_fn=lambda at: (FX, at), tick_s=tick, **kw)


def test_take_profit_rail_closes_with_positive_pnl_net_of_the_modeled_costs(stub):
    stub(Tape(lambda at: 1.0 if at < T + 100 else 2.5))                 # value 2.5x spend after t+100: tp (100%) fires
    r = run(policy())
    assert r["exit_reason"] == "tp" and r["pnl_usd"] > 30.0 * 1.4 and r["exit_t"] > T + 100


def test_no_rail_trips_so_the_horizon_exit_closes_the_position(stub):
    stub(Tape(lambda at: 1.05))
    r = run(policy(), horizon=120)
    assert r["exit_reason"] == "horizon" and abs(r["pnl_usd"] - 30.0 * 0.05) < 1.0 and r["exit_t"] >= T + 120


def test_stop_rail_loses_and_pnl_is_negative(stub):
    stub(Tape(lambda at: 1.0 if at < T + 60 else 0.5))
    r = run(policy())
    assert r["exit_reason"] in ("stop", "catastrophe") and r["pnl_usd"] < -10.0


def test_a_missing_pool_or_unquotable_exit_is_censored_with_the_reason_never_zero(stub):
    stub(Tape(lambda at: 1.0, buy_why="no_swaps_before_cut"))
    assert run(policy()) == {"censored": "no_swaps_before_cut"}
    stub(Tape(lambda at: 1.0 if at < T + 10 else "capture_gap"))
    r = run(policy(), horizon=60)
    assert r == {"censored": "exit_unquotable"}


def test_the_tick_must_stay_inside_the_ledger_grace_or_the_replay_would_record_outages(stub):
    stub(Tape(lambda at: 1.0))
    with pytest.raises(ValueError):
        run(policy(), tick=L.TICK_GRACE_S + 1.0)
    with pytest.raises(ValueError):
        run(policy(), tick=None)                                         # the live watcher tick is a frozen input, never a default


def test_deterministic_and_leaves_no_state_behind(stub, tmp_path):
    stub(Tape(lambda at: 1.0 if at < T + 100 else 2.5))
    assert run(policy()) == run(policy())
    assert not list(tmp_path.iterdir())                                  # scratch book is in memory


def test_nonzero_offset_applied_only_at_real_ledger_quote_boundary(stub):
    class Recorded(Tape):
        def buy(self, *args, **kwargs):
            cuts.append(args[3])
            return super().buy(*args, **kwargs)
    cuts = []
    stub(Recorded(lambda at: 1.05))
    r = run(policy(), horizon=30, clock_sample_fn=lambda at: {'clock_sample_id': 'frozen', 'offset_s': 17., 'sampled_at': T + 7})
    assert cuts == [T + L.ENTRY_DELAY_S + 17.]
    assert r['clock_samples'][0]['clock_sample_id'] == 'frozen'
    assert r['clock_samples'][0]['host_cut'] - r['clock_samples'][0]['corrected_cut'] == 17.


def test_unknown_or_stale_clock_censors_without_tape_read(stub):
    stub(Tape(lambda at: pytest.fail('unknown clock read tape')))
    assert run(policy(), clock_sample_fn=lambda at: None) == {'censored': 'clock_unknown'}
    assert run(policy(), clock_sample_fn=lambda at: {'clock_sample_id': 'old', 'offset_s': 0., 'sampled_at': T - 4000}) == {'censored': 'clock_unknown'}
