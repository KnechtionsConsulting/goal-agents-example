"""Check each shipped ticket against its Success signal.

A ticket opts in with the `ship:pending` label (the planner adds it to every ticket it
files with a measurable signal). Once the ticket is Done and its Check period has passed
since it was completed, this script measures the signal once, comments the verdict on
the ticket and swaps the label for `ship:passed` or `ship:failed`. A failed verdict is
also posted to the goal's channel, where a person decides what to do next.

Runs as a Kubernetes CronJob.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from clients import Issue, Linear, parse_time, prometheus_value, slack_post
from metric import (
    BlockError,
    NoBlock,
    SuccessSignal,
    parse_metric,
    parse_success_signal,
)

PENDING, PASSED, FAILED, INVALID = "ship:pending", "ship:passed", "ship:failed", "ship:invalid"


@dataclass(frozen=True)
class Verdict:
    passed: bool
    value: float

    def render(self, signal: SuccessSignal) -> str:
        outcome = "passed" if self.passed else "failed"
        return (
            f"Ship check {outcome}: measured {self.value:g}, expected {signal.op} "
            f"{signal.threshold:g}.\n\n`{signal.signal.query}`"
        )


def is_due(issue: Issue, signal: SuccessSignal, now: datetime) -> bool:
    return issue.completed_at is not None and now >= parse_time(issue.completed_at) + signal.check


def judge(signal: SuccessSignal, value: float) -> Verdict:
    return Verdict(signal.passes(value), value)


def goal_channel(linear: Linear, project_id: str | None) -> str | None:
    if not project_id:
        return None
    try:
        return parse_metric(linear.project(project_id)["content"]).channel
    except (NoBlock, BlockError):
        return None


def check(linear: Linear, issue: Issue, now: datetime) -> None:
    try:
        signal = parse_success_signal(issue.description)
    except (NoBlock, BlockError) as e:
        linear.comment(issue.id, f"Ship check skipped: the Success signal is invalid ({e}).")
        linear.swap_label(issue.id, remove=PENDING, add=INVALID)
        return
    if signal is None:
        linear.swap_label(issue.id, remove=PENDING, add=PASSED)
        return
    if not is_due(issue, signal, now):
        return

    verdict = judge(signal, prometheus_value(signal.signal.query, at=now))
    # The comment is the record; the label is what keeps the next run from checking again.
    linear.comment(issue.id, verdict.render(signal))
    linear.swap_label(issue.id, remove=PENDING, add=PASSED if verdict.passed else FAILED)

    channel = goal_channel(linear, issue.project_id)
    if channel and not verdict.passed:
        slack_post(
            channel,
            f"{issue.identifier} shipped but failed its Success signal: measured "
            f"{verdict.value:g}, expected {signal.op} {signal.threshold:g}. {issue.url}",
        )
    print(f"{issue.identifier}: {'passed' if verdict.passed else 'failed'}")


def main() -> int:
    linear = Linear()
    now = datetime.now(timezone.utc)
    failed = 0
    for issue in linear.issues_with_label(PENDING, "completed"):
        try:
            check(linear, issue, now)
        except Exception as e:
            print(f"{issue.identifier}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
