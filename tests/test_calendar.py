from datetime import datetime, timezone
from lazarr.calendar import released, next_search_start
from zoneinfo import ZoneInfo


def test_release_date_precision_and_unknown():
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    assert released(None, "Europe/Saratov", now)
    assert released("2026-09-11", "Europe/Saratov", now)
    assert not released("2026-09-12", "Europe/Saratov", now)
    assert not released("2026-09-13", "Europe/Saratov", now)
    assert released("2026-09-12T11:00:00Z", "Europe/Saratov", now)
    assert not released("2026-09-12T13:00:00Z", "Europe/Saratov", now)


def test_next_daily_start():
    now = datetime(2026, 9, 12, 20, tzinfo=timezone.utc)
    assert (
        next_search_start("01:30", ZoneInfo("Europe/Saratov"), now)
        == datetime(2026, 9, 12, 21, 30, tzinfo=timezone.utc).timestamp()
    )
    assert (
        next_search_start("00:00", ZoneInfo("Europe/Saratov"), now)
        == datetime(2026, 9, 13, 20, tzinfo=timezone.utc).timestamp()
    )
