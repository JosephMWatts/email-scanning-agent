# tests/test_window.py — Deliverable 1.5 (Option C dynamic fetch window).
#
# Covers the ten scoped cases:
#   compute_fetch_window (pure, no I/O):
#     a. First-run fallback when there is no watermark.
#     b. Normal sub-day window rounds up to 1 day, no gap.
#     c. A >25h recovered span flags gap=True.
#     d. A long absence clamps to the 7-day cap.
#     e. Boundary: exactly 25.0h is not a gap; 25.01h is (strict greater-than).
#     f. --since-days override takes precedence over the dynamic logic.
#   read_last_success (tmp_path Agent Runs/ dir):
#     g. Empty/absent Agent Runs/ → None.
#     h. Picks the latest success, skipping a more-recent failed run.
#     i. Filters by agent_id; another agent's newer success is ignored.
#     j. A malformed log is skipped; the next valid success is returned.

import os
from datetime import datetime, timedelta, timezone

import harness

UTC = timezone.utc
NOW = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)


# --- compute_fetch_window (pure) -----------------------------------------


def test_a_first_run_fallback():
    w = harness.compute_fetch_window(None, NOW)
    assert w.since_days == harness.FIRST_RUN_DAYS == 1
    assert w.first_run is True
    assert w.override is False
    assert w.capped is False
    assert w.gap is False
    assert w.recovered_hours is None


def test_b_normal_sub_day_no_gap():
    w = harness.compute_fetch_window(NOW - timedelta(hours=3), NOW)
    assert w.since_days == 1          # ceil(3h) → 1 day
    assert w.recovered_hours == 3.0
    assert w.gap is False
    assert w.first_run is False
    assert w.override is False
    assert w.capped is False


def test_c_gap_flagged_over_threshold():
    w = harness.compute_fetch_window(NOW - timedelta(hours=31), NOW)
    assert w.since_days == 2          # ceil(31h / 24) → 2 days
    assert w.recovered_hours == 31.0
    assert w.gap is True
    assert w.capped is False


def test_d_long_absence_clamps_to_cap():
    w = harness.compute_fetch_window(NOW - timedelta(days=14), NOW)
    assert w.since_days == harness.MAX_WINDOW_DAYS == 7
    assert w.capped is True
    assert w.recovered_hours == 14 * 24  # 336.0
    assert w.gap is True                  # 336h well past 25h


def test_e_gap_boundary_strict_greater_than():
    exact = harness.compute_fetch_window(NOW - timedelta(hours=25), NOW)
    assert exact.recovered_hours == 25.0
    assert exact.gap is False            # exactly 25h is not a gap

    over = harness.compute_fetch_window(NOW - timedelta(hours=25, seconds=36), NOW)
    assert over.recovered_hours > 25.0   # 25.01h
    assert over.gap is True


def test_f_override_takes_precedence():
    # last_success that would otherwise compute since_days=2 (31h → ceil 2).
    w = harness.compute_fetch_window(
        NOW - timedelta(hours=31), NOW, override_days=5
    )
    assert w.since_days == 5
    assert w.override is True
    assert w.gap is False
    assert w.first_run is False
    assert w.capped is False
    assert w.recovered_hours is None     # dynamic logic ignored


# --- read_last_success (I/O) ---------------------------------------------


def _write_log(vault, agent_id, started_at, status):
    """Write one run-log via the production writer, so the reader is tested
    against exactly what write_run_log emits. completed_at is stamped a minute
    after started_at."""
    return harness.write_run_log(
        vault_path=vault,
        agent_id=agent_id,
        agent_version="1.0.0",
        started_at=started_at,
        completed_at=started_at + timedelta(minutes=1),
        status=status,
        trigger="manual",
        input_summary="INBOX",
        output_summary="",
        output_paths=[],
    )


def test_g_absent_runs_dir_yields_none(tmp_path):
    assert harness.read_last_success(str(tmp_path), "calendar-agent") is None


def test_h_skips_more_recent_failed(tmp_path):
    vault = str(tmp_path)
    success_start = datetime(2026, 5, 30, 7, 0, tzinfo=UTC)
    failed_start = datetime(2026, 5, 31, 7, 0, tzinfo=UTC)  # newer filename
    _write_log(vault, "calendar-agent", success_start, "success")
    _write_log(vault, "calendar-agent", failed_start, "failed")

    got = harness.read_last_success(vault, "calendar-agent")
    assert got == success_start + timedelta(minutes=1)


def test_i_filters_by_agent_id(tmp_path):
    vault = str(tmp_path)
    cal_start = datetime(2026, 5, 30, 7, 0, tzinfo=UTC)
    flow_a_start = datetime(2026, 5, 31, 7, 0, tzinfo=UTC)  # newer, other agent
    _write_log(vault, "calendar-agent", cal_start, "success")
    _write_log(vault, "email-scanner-flow-a", flow_a_start, "success")

    got = harness.read_last_success(vault, "calendar-agent")
    assert got == cal_start + timedelta(minutes=1)


def test_j_skips_malformed_returns_next_valid(tmp_path):
    vault = str(tmp_path)
    good_start = datetime(2026, 5, 29, 7, 0, tzinfo=UTC)
    _write_log(vault, "calendar-agent", good_start, "success")

    # A newer, malformed calendar-agent log: garbled, no usable frontmatter.
    runs_dir = os.path.join(vault, "Agent Runs")
    bad_name = "2026-05-31 0700 calendar-agent.md"
    with open(os.path.join(runs_dir, bad_name), "w", encoding="utf-8") as fh:
        fh.write("not even frontmatter\nstatus: success\ncompleted_at: nonsense\n")

    got = harness.read_last_success(vault, "calendar-agent")
    assert got == good_start + timedelta(minutes=1)
