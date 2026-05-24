"""Unit tests for gateway.SpliceStats."""
import pytest
from gateway import SpliceStats


def test_initial_state():
    s = SpliceStats()
    assert s.up_total == 0
    assert s.down_total == 0
    assert s.up_window == 0
    assert s.down_window == 0


def test_note_up_accumulates():
    s = SpliceStats()
    s.note_up(100)
    s.note_up(200)
    assert s.up_total == 300
    assert s.up_window == 300
    assert s.down_total == 0


def test_note_down_accumulates():
    s = SpliceStats()
    s.note_down(512)
    assert s.down_total == 512
    assert s.down_window == 512
    assert s.up_total == 0


def test_snapshot_returns_correct_kibps():
    s = SpliceStats()
    s.note_up(1024)
    s.note_down(2048)
    snap = s.snapshot(1.0)
    assert snap["type"] == "splice_stats"
    assert snap["up_kibps"] == pytest.approx(1.0)
    assert snap["down_kibps"] == pytest.approx(2.0)
    assert snap["up_total"] == 1024
    assert snap["down_total"] == 2048


def test_snapshot_resets_window_but_keeps_totals():
    s = SpliceStats()
    s.note_up(1024)
    s.note_down(1024)
    s.snapshot(1.0)

    # New traffic after the snapshot.
    s.note_up(512)
    snap2 = s.snapshot(1.0)

    assert snap2["up_kibps"] == pytest.approx(0.5)
    assert snap2["down_kibps"] == pytest.approx(0.0)
    assert snap2["up_total"] == 1536   # cumulative
    assert snap2["down_total"] == 1024


def test_snapshot_zero_interval_does_not_divide_by_zero():
    s = SpliceStats()
    s.note_up(1024)
    snap = s.snapshot(0.0)
    assert snap["up_kibps"] > 0   # clamped to 1e-6, so a huge but finite number


def test_snapshot_idle_window_is_zero_kibps():
    s = SpliceStats()
    snap = s.snapshot(1.0)
    assert snap["up_kibps"] == 0.0
    assert snap["down_kibps"] == 0.0


def test_multiple_snapshots_independent_windows():
    s = SpliceStats()
    s.note_up(1024)
    s.snapshot(1.0)

    snap2 = s.snapshot(1.0)
    assert snap2["up_kibps"] == 0.0   # window was cleared
