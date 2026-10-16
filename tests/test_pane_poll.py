"""The pane scrape's period: often while a turn runs or something just happened, rarely for a quiet open session."""
from backend.session import ACTIVE_WINDOW_S, BUSY_POLL_S, IDLE_POLL_S, PUMP_TICK_S, pane_poll_ticks


def test_busy_pill_polls_often():
    assert pane_poll_ticks(True, 0.0, 10_000.0) == round(BUSY_POLL_S / PUMP_TICK_S)


def test_recent_activity_polls_often():
    now = 10_000.0
    assert pane_poll_ticks(False, now - 5, now) == round(BUSY_POLL_S / PUMP_TICK_S)


def test_quiet_session_polls_rarely():
    now = 10_000.0
    ticks = pane_poll_ticks(False, now - ACTIVE_WINDOW_S - 1, now)
    assert ticks == round(IDLE_POLL_S / PUMP_TICK_S)
    assert ticks > round(BUSY_POLL_S / PUMP_TICK_S)
