from types import SimpleNamespace
from tools.free_live_mlb_scope import retain_scoped_events


def test_uncaptured_scoped_events_retain_empty_history_defaults():
    events = {1: SimpleNamespace(Place='Nationals Park'), 2: SimpleNamespace(Place='Petco Park')}
    selected, latest, captures, counts = retain_scoped_events('mlb', (events, {}, {}, {'games': 2}))
    assert set(selected) == {1}
    assert latest[1] is None
    assert captures[1] == 0
    assert set(events) == {1, 2}
