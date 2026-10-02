import json

import pytest

from nexus_dl.budget import (
    DAY,
    HOUR,
    OFFICIAL_DAILY,
    OFFICIAL_HOURLY,
    RequestBudget,
    RequestLimitReached,
    format_duration,
)


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def budget(clock=None, **options):
    return RequestBudget(clock=clock or Clock(), **options)


def test_usage_follows_the_rolling_windows():
    clock = Clock()
    b = budget(clock)
    b.record(100)
    clock.advance(3500)
    assert (b.usage().hour_used, b.usage().day_used) == (100, 100)
    clock.advance(200)  # 3700 s later: out of the hour, still in the day
    assert (b.usage().hour_used, b.usage().day_used) == (0, 100)
    clock.advance(DAY)
    assert b.usage().day_used == 0


def test_defaults_are_ninety_percent_of_what_nexus_allows():
    b = budget()
    assert (b.hourly, b.daily) == (450, 18_000)
    assert (OFFICIAL_HOURLY, OFFICIAL_DAILY) == (500, 20_000)


def test_limits_can_be_lowered_but_never_raised_above_nexus_limits():
    assert (budget(hourly=100, daily=1000).hourly, budget(hourly=100, daily=1000).daily) == (100, 1000)
    b = budget(hourly=99_999, daily=999_999)
    assert (b.hourly, b.daily) == (500, 20_000)
    assert budget(hourly=0, daily=-5).hourly == 1


def test_no_wait_while_there_is_room():
    b = budget(hourly=10)
    b.record(9)
    assert b.wait_time(1) == (0.0, "")
    assert b.wait_time(2)[0] > 0


def test_wait_until_enough_old_requests_have_aged_out_of_the_hour():
    clock = Clock()
    b = budget(clock, hourly=10)
    b.record(6)  # t = 0
    clock.advance(100)
    b.record(4)  # t = 100: the hour is now full
    clock.advance(100)  # t = 200
    delay, limiting = b.wait_time(5)  # needs the 6 old ones to age out: at t = 3600
    assert limiting == "hourly" and delay == pytest.approx(3400)
    delay, _ = b.wait_time(1)  # one slot frees up as soon as the first batch is gone as well
    assert delay == pytest.approx(3400)


def test_the_daily_limit_is_enforced_at_the_same_time():
    clock = Clock()
    b = budget(clock, hourly=500, daily=20)
    b.record(20)
    clock.advance(2 * HOUR)  # the hourly window is empty, the daily one is not
    delay, limiting = b.wait_time(1)
    assert limiting == "daily" and delay == pytest.approx(DAY - 2 * HOUR)


def test_a_request_bigger_than_the_whole_limit_waits_out_the_window():
    assert budget(hourly=5).wait_time(6) == (HOUR, "hourly")


def test_wait_for_sleeps_in_short_slices_and_reports_progress():
    clock = Clock()
    b = budget(clock, hourly=10)
    b.record(10)
    sleeps, reports = [], []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(seconds)

    b.wait_for(1, sleep, lambda delay, limiting: reports.append((round(delay), limiting)))
    assert max(sleeps) <= 5.0 and sum(sleeps) >= HOUR - 1  # waited out the hour in short, interruptible steps
    assert reports[0] == (3600, "hourly") and reports[-1][0] <= 6
    assert b.wait_time(1) == (0.0, "")


def test_wait_for_returns_immediately_when_there_is_room():
    b = budget()
    b.wait_for(5, lambda s: pytest.fail("must not sleep"))


def test_giving_up_when_the_wait_would_be_too_long():
    clock = Clock()
    b = budget(clock, hourly=500, daily=20)
    b.record(20)
    with pytest.raises(RequestLimitReached, match=r"daily request budget \(20 per 24 hours\)") as caught:
        b.wait_for(1, lambda s: pytest.fail("must not wait"))
    assert "Run the tool again later" in str(caught.value)


def test_stop_during_a_wait_propagates():
    from nexus_dl.errors import Cancelled

    b = budget(hourly=1)
    b.record(1)

    def stop(_seconds):
        raise Cancelled()

    with pytest.raises(Cancelled):
        b.wait_for(1, stop)


def test_usage_survives_a_restart(tmp_path):
    path = tmp_path / "usage.json"
    clock = Clock()
    first = RequestBudget(path, clock=clock)
    first.record(30)
    clock.advance(10)
    first.record(12)
    first.flush()
    second = RequestBudget(path, clock=clock)
    assert second.usage().hour_used == 42
    clock.advance(DAY + 1)
    assert RequestBudget(path, clock=clock).usage().day_used == 0  # old entries are not carried over


def test_a_damaged_usage_file_is_ignored(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{oops", encoding="utf-8")
    assert RequestBudget(path).usage().day_used == 0
    path.write_text(json.dumps({"entries": [["x", 1], [1], None, [9e9, -4]]}), encoding="utf-8")
    assert RequestBudget(path).usage().day_used == 0


def test_flush_writes_only_when_something_changed(tmp_path):
    path = tmp_path / "usage.json"
    b = RequestBudget(path)
    b.flush()
    assert not path.exists()
    b.record(3)
    b.flush()
    assert json.loads(path.read_text(encoding="utf-8"))["entries"][0][1] == 3


@pytest.mark.parametrize(
    "seconds, text",
    [(5, "5 s"), (89, "89 s"), (120, "2 min"), (3000, "50 min"), (5400, "1 h 30 min"), (8 * 3600, "8 h 00 min")],
)
def test_format_duration(seconds, text):
    assert format_duration(seconds) == text
