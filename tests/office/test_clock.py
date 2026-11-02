import json
from sniper.office.clock import corrected_now


def test_clock_requires_fresh_numeric_single_sample(tmp_path):
    path = tmp_path / 'clock.json'
    assert corrected_now(path, host_now=120.) is None
    for doc in ({'offset_s': 17., 'at': 100.}, {'offset_s': 0., 'at': 100.}):
        path.write_text(json.dumps(doc))
        assert corrected_now(path, host_now=120.) == 120. - doc['offset_s']
    for doc in ({'offset_s': True, 'at': 100.}, {'offset_s': 0.}, {'offset_s': 0., 'at': -4000.}, {'offset_s': 0., 'at': 121.}):
        path.write_text(json.dumps(doc))
        assert corrected_now(path, host_now=120.) is None
