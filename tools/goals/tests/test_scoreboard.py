from datetime import date

from agent_tools import find_marker
from clients import Issue
from metric import Metric, Signal
from scoreboard import (
    evaluate,
    history,
    next_ticket,
    proposed_keys,
    ready,
    render_proposal,
)

METRIC = Metric(
    Signal("tickets", "open"),
    baseline=40,
    baseline_date=date(2026, 11, 2),
    target=10,
    channel="C0123456789",
    parallel=2,
)
PROJECT = "0b6c1f0e-2a51-4f43-9a0a-6a1c2b3d4e5f"


def issue(
    n: int,
    state: str = "unstarted",
    priority: int = 3,
    created: str = "2026-11-01",
    labels: tuple[str, ...] = (),
) -> Issue:
    return Issue(
        id=f"id-{n}",
        identifier=f"ENG-{n}",
        title=f"Ticket {n}",
        state_type=state,
        priority=priority,
        created_at=created,
        labels=frozenset(labels),
    )


def readings(*values: float) -> list[tuple[date, float]]:
    return [(date(2026, 11, d + 1), v) for d, v in enumerate(values)]


def test_history_and_proposals_come_from_status_updates() -> None:
    updates = [
        {"body": "text\n\ngoal-reading: 2026-11-03 31\ngoal-proposed: stalled:2026-W45", "createdAt": "b"},
        {"body": "goal-reading: 2026-11-02 35", "createdAt": "a"},
        {"body": "a person's update", "createdAt": "c"},
    ]
    assert history(updates) == [(date(2026, 11, 2), 35.0), (date(2026, 11, 3), 31.0)]
    assert proposed_keys(updates) == {"stalled:2026-W45"}


def test_on_track_when_improving() -> None:
    report = evaluate(METRIC, readings(38, 35, 33, 31), 30, ready_count=3)
    assert report.flags == () and report.health == "onTrack"
    assert round(report.progress, 2) == 0.33


def test_target_reached_suppresses_other_flags() -> None:
    report = evaluate(METRIC, readings(12, 12, 12, 12), 9, ready_count=0)
    assert report.flags == ("target_reached",)


def test_stalled_needs_a_full_window_without_improvement() -> None:
    assert "stalled" in evaluate(METRIC, readings(30, 31, 30, 32), 30, 3).flags
    assert "stalled" not in evaluate(METRIC, readings(30, 31, 29, 32), 30, 3).flags
    assert "stalled" not in evaluate(METRIC, readings(30, 31, 30), 30, 3).flags


def test_worse_than_baseline_is_off_track() -> None:
    report = evaluate(METRIC, [], 45, ready_count=0)
    assert report.flags == ("worse_than_baseline", "backlog_empty")
    assert report.health == "offTrack"


def test_ready_skips_held_and_orders_by_priority_then_age() -> None:
    tickets = [
        issue(1, priority=0, created="2026-10-01"),  # no priority sorts last
        issue(2, priority=2, created="2026-11-02"),
        issue(3, priority=2, created="2026-11-01"),
        issue(4, priority=1, labels=("no-dispatch",)),
        issue(5, state="backlog", priority=1),
    ]
    assert [i.identifier for i in ready(tickets)] == ["ENG-3", "ENG-2", "ENG-1"]


def test_next_ticket_respects_parallel_cap() -> None:
    tickets = [issue(1, state="started"), issue(2, state="started"), issue(3)]
    assert next_ticket(tickets, parallel=2)[0] is None
    assert next_ticket(tickets, parallel=3)[0].identifier == "ENG-3"
    assert next_ticket([issue(1, state="completed")], parallel=2) == (None, "no ready ticket")


def test_proposal_carries_marker_only_when_the_planner_can_help() -> None:
    with_marker = render_proposal("Fewer job failures", PROJECT, ("stalled",))
    assert find_marker(with_marker) == {"phase": "plan", "project_id": PROJECT}
    assert find_marker(render_proposal("Fewer job failures", PROJECT, ("target_reached",))) is None


def test_marker_survives_collapsed_newlines_and_ignores_other_messages() -> None:
    assert find_marker(f"Plan: 3 tickets. React ✅ to create them. goal-plan {PROJECT}") == {
        "phase": "create",
        "project_id": PROJECT,
    }
    assert find_marker("Can someone review https://github.com/acme/app/pull/12?") is None
