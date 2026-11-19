from datetime import datetime, timedelta, timezone

from clients import Issue
from metric import Signal, SuccessSignal
from verifier import is_due, judge

SIGNAL = SuccessSignal(Signal("promql", "sum(x)"), op="<=", threshold=15, check=timedelta(days=7))


def shipped(completed_at: str | None) -> Issue:
    return Issue(
        id="id-1",
        identifier="ENG-1",
        title="t",
        state_type="completed",
        priority=0,
        created_at="2026-11-01T00:00:00Z",
        completed_at=completed_at,
    )


def test_due_only_after_the_check_period() -> None:
    issue = shipped("2026-11-02T10:00:00.000Z")
    assert not is_due(issue, SIGNAL, datetime(2026, 11, 9, 9, tzinfo=timezone.utc))
    assert is_due(issue, SIGNAL, datetime(2026, 11, 9, 10, tzinfo=timezone.utc))
    assert not is_due(shipped(None), SIGNAL, datetime(2027, 1, 1, tzinfo=timezone.utc))


def test_judge_and_render() -> None:
    verdict = judge(SIGNAL, 15)
    assert verdict.passed
    assert verdict.render(SIGNAL).startswith("Ship check passed: measured 15, expected <= 15.")
    assert not judge(SIGNAL, 15.5).passed
