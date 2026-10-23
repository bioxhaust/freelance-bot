from datetime import datetime, timezone

from scheduler import is_fresh_enough

NOW = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)  # 09:00 MSK
CUTOFF = datetime(2026, 10, 23, 5, 47, tzinfo=timezone.utc)  # 08:47 MSK


def test_missing_publication_time_is_not_fresh() -> None:
    assert is_fresh_enough(None, not_before=CUTOFF, now=NOW, max_age_minutes=60) is False


def test_order_from_before_cutoff_is_not_fresh() -> None:
    published = datetime(2026, 10, 23, 5, 24, tzinfo=timezone.utc)  # 08:24 MSK
    assert is_fresh_enough(published, not_before=CUTOFF, now=NOW, max_age_minutes=180) is False


def test_order_after_cutoff_inside_window_is_fresh() -> None:
    published = datetime(2026, 10, 23, 5, 50, tzinfo=timezone.utc)
    assert is_fresh_enough(published, not_before=CUTOFF, now=NOW, max_age_minutes=60) is True


def test_order_after_cutoff_but_too_old_when_seen_is_not_fresh() -> None:
    published = datetime(2026, 10, 23, 5, 50, tzinfo=timezone.utc)
    later = datetime(2026, 10, 23, 7, 0, tzinfo=timezone.utc)
    assert is_fresh_enough(published, not_before=CUTOFF, now=later, max_age_minutes=60) is False
