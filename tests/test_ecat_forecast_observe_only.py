from test_desk_v23_contract import fj, F, PLAN, verdict


def test_off_grid_forecast_does_not_veto_valid_entry():
    raw = {**verdict(), 'p': PLAN, 'f': F}
    assert fj(raw)['action'] == 'ENTER'
    assert fj({**raw, 'f': {**F, 'u': 15}})['action'] == 'ENTER'
