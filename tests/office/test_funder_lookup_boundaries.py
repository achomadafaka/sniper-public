from office.test_funder_lookup import F, W, A, Provider, dbs

def test_missing_provider_funder_field_is_unknown_not_absent():
    got = F.funder_of(W, fetch=Provider(200, {}))
    assert got['state'] == 'unknown', got

def test_non_object_local_metadata_is_unreadable_not_a_crash(tmp_path):
    got = F.funder_of(W, **dbs(tmp_path, cluster=[
        (W, A, 'resolved', 1., 'funded_by', '[]')]))
    assert got['state'] == 'unknown'
    assert got['unreadable_sources'] == ['cluster_funder_cache']
