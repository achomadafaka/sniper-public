"""The measured PS pass must not omit point gaps at its closed window boundaries."""
from sniper.services import memecoin_desk_dump_group_source as S


def test_unknown_scope_point_at_creation_window_start_blocks():
    got = S.ps_window_holes([(940., 940., 'gap:worker_start', None, 'ps')], 'P', 1000., 1100.)
    assert got['holes_blocking'] == 1


def test_own_pool_point_at_cutoff_blocks():
    got = S.ps_window_holes([(1100., 1100., 'gap:worker_start', 'P', 'ps')], 'P', 1000., 1100.)
    assert got['holes_blocking'] == 1


def test_future_creation_does_not_prove_empty_window():
    assert S.ps_window_holes([], 'P', 1200., 1100.)['created_known'] is False
