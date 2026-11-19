"""The goal's daily heartbeat: measure, record, flag, dispatch.

For every Linear project with a valid Metric block, one run:

1. measures the metric,
2. posts the reading to the goal's Slack channel,
3. posts a proposal when a flag needs a person's decision (react ✅ to plan more work),
4. records the reading as a Linear project status update, which is the goal's history,
5. hands the next ready ticket to the executor by commenting `/execute` on it.

Everything here is deterministic. The agents only ever act on what this script decides.
Runs as a Kubernetes CronJob; a rerun on the same day does nothing for a goal it already
recorded.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone

from clients import Issue, Linear, prometheus_value, slack_post
from metric import BlockError, Metric, NoBlock, parse_metric

STALL_RUNS = 5  # no reading better than the one STALL_RUNS-1 runs ago means stalled
NO_DISPATCH = "no-dispatch"  # a person's label to keep a ticket away from the executor
EXECUTE = "/execute"  # the comment the executor spawner listens for

READING = re.compile(r"^goal-reading: (?P<date>\d{4}-\d{2}-\d{2}) (?P<value>-?\d+(?:\.\d+)?)$", re.M)
PROPOSED = re.compile(r"^goal-proposed: (?P<key>\S+)$", re.M)

# Flags that a planner run can do something about, so their proposal carries the marker
# the planner spawner acts on.
PLANNER_FLAGS = ("worse_than_baseline", "stalled", "backlog_empty")
SENTENCES = {
    "target_reached": "it reached its target. Close the project when you agree",
    "worse_than_baseline": "it is worse than its baseline",
    "stalled": f"it has not improved in the last {STALL_RUNS} readings",
    "backlog_empty": "it has no ready tickets left",
}


@dataclass(frozen=True)
class Report:
    value: float
    flags: tuple[str, ...]
    health: str  # Linear's project health: onTrack, atRisk, offTrack
    progress: float  # 0.0 at the baseline, 1.0 at the target


def history(updates: list[dict]) -> list[tuple[date, float]]:
    """The readings recorded in the project's status updates, oldest first."""
    readings = []
    for update in updates:
        for match in READING.finditer(update["body"]):
            readings.append((date.fromisoformat(match["date"]), float(match["value"])))
    return sorted(readings)


def proposed_keys(updates: list[dict]) -> set[str]:
    return {m["key"] for u in updates for m in PROPOSED.finditer(u["body"])}


def ready(issues: list[Issue]) -> list[Issue]:
    """Todo tickets a person has not held back, in the order they should start."""
    candidates = [i for i in issues if i.state_type == "unstarted" and NO_DISPATCH not in i.labels]
    # Linear priority 0 means "none", so it sorts after low (4).
    return sorted(candidates, key=lambda i: (i.priority or 5, i.created_at))


def evaluate(metric: Metric, past: list[tuple[date, float]], value: float, ready_count: int) -> Report:
    span = metric.target - metric.baseline
    progress = max(0.0, min(1.0, (value - metric.baseline) / span))

    if metric.reached(value):
        return Report(value, ("target_reached",), "onTrack", progress)

    flags = []
    if metric.better(metric.baseline, value):
        flags.append("worse_than_baseline")
    window = [v for _, v in past[-(STALL_RUNS - 1) :]] + [value]
    if len(window) == STALL_RUNS and not any(metric.better(v, window[0]) for v in window[1:]):
        flags.append("stalled")
    if ready_count == 0:
        flags.append("backlog_empty")

    if "worse_than_baseline" in flags:
        health = "offTrack"
    elif flags:
        health = "atRisk"
    else:
        health = "onTrack"
    return Report(value, tuple(flags), health, progress)


def next_ticket(issues: list[Issue], parallel: int) -> tuple[Issue | None, str]:
    """Pick at most one ticket to start, keeping no more than `parallel` in progress."""
    in_flight = [i for i in issues if i.state_type == "started"]
    if len(in_flight) >= parallel:
        return None, f"{len(in_flight)} in progress, cap {parallel}"
    queue = ready(issues)
    if not queue:
        return None, "no ready ticket"
    return queue[0], "next by priority, then age"


def measure(metric: Metric, issues: list[Issue]) -> float:
    if metric.signal.kind == "tickets":
        return float(sum(1 for i in issues if i.state_type not in ("completed", "canceled")))
    return prometheus_value(metric.signal.query)


def render_reading(name: str, metric: Metric, report: Report) -> str:
    arrow = "📉" if metric.lower_is_better else "📈"
    flags = f" Flags: {', '.join(report.flags)}." if report.flags else ""
    return (
        f"{arrow} *{name}*: {report.value:g} "
        f"(baseline {metric.baseline:g} on {metric.baseline_date}, target {metric.target:g}). "
        f"{report.progress:.0%} of the way.{flags}"
    )


def render_proposal(name: str, project_id: str, flags: tuple[str, ...]) -> str:
    reasons = "; ".join(SENTENCES[f] for f in flags)
    text = f"*{name}* needs a decision: {reasons}."
    if any(f in PLANNER_FLAGS for f in flags):
        # The marker is how the planner knows which goal a ✅ is for. Keep it on its own
        # line and as a plain token: Slack can collapse the newlines when it is read back.
        text += f"\nReact ✅ to have the planner propose the next tickets.\ngoal-proposal {project_id}"
    return text


def run_goal(linear: Linear, project: dict, metric: Metric, today: date) -> None:
    pid, name = project["id"], project["name"]
    updates = linear.project_updates(pid)
    past = history(updates)
    if past and past[-1][0] == today:
        print(f"{name}: already recorded today")
        return

    issues = linear.project_issues(pid)
    value = measure(metric, issues)
    report = evaluate(metric, past, value, len(ready(issues)))

    slack_post(metric.channel, render_reading(name, metric, report))

    record = [render_reading(name, metric, report), "", f"goal-reading: {today} {value:g}"]
    if report.flags:
        week = today.strftime("%G-W%V")
        key = f"{'+'.join(report.flags)}:{week}"
        if key not in proposed_keys(updates):
            slack_post(metric.channel, render_proposal(name, pid, report.flags))
            record.append(f"goal-proposed: {key}")
    linear.create_project_update(pid, "\n".join(record), report.health)

    if "target_reached" in report.flags:
        return
    ticket, reason = next_ticket(issues, metric.parallel)
    if ticket is None:
        print(f"{name}: no dispatch ({reason})")
        return
    # Move first, then comment: a ticket in progress is never picked again, so a failed
    # comment leaves one stuck ticket for a person rather than two executors on it.
    full = linear.issue(ticket.identifier)
    linear.move(ticket.id, linear.state_id(full, "started"))
    linear.comment(ticket.id, EXECUTE)
    print(f"{name}: dispatched {ticket.identifier} ({reason})")


def main() -> int:
    linear = Linear()
    today = datetime.now(timezone.utc).date()
    failed = 0
    for project in linear.open_projects():
        try:
            metric = parse_metric(project["content"])
        except NoBlock:
            continue
        except BlockError as e:
            print(f"{project['name']}: invalid Metric block: {e}", file=sys.stderr)
            failed += 1
            continue
        try:
            run_goal(linear, project, metric, today)
        except Exception as e:  # one goal's failure must not stop the others
            print(f"{project['name']}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
