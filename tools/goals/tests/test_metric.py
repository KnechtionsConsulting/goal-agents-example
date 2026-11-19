from datetime import date, timedelta

import pytest
from metric import BlockError, NoBlock, parse_metric, parse_success_signal

METRIC = """\
## Metric
- Signal: promql: sum(rate(job_failures_total[1d]))
- Baseline: 40 on 2026-11-02
- Target: 10
- Channel: C0123456789

## Context
Why this goal exists.
- not a field, the block already ended
"""


def test_metric_reads_fields_and_direction() -> None:
    metric = parse_metric(METRIC)
    assert metric.signal.kind == "promql"
    assert metric.baseline == 40 and metric.baseline_date == date(2026, 11, 2)
    assert metric.target == 10 and metric.parallel == 2
    assert metric.lower_is_better
    assert metric.better(12, 20) and not metric.better(20, 12)
    assert metric.reached(10) and not metric.reached(10.5)


def test_metric_higher_is_better_when_target_above_baseline() -> None:
    metric = parse_metric(METRIC.replace("Target: 10", "Target: 90"))
    assert not metric.lower_is_better
    assert metric.reached(91)


def test_metric_tickets_signal() -> None:
    metric = parse_metric(METRIC.replace("promql: sum(rate(job_failures_total[1d]))", "tickets: open"))
    assert metric.signal.kind == "tickets"


@pytest.mark.parametrize(
    "old, new, message",
    [
        ("- Target: 10", "- Taget: 10", "unknown field"),
        ("- Channel: C0123456789", "- Channel: #goals", "Channel"),
        ("40 on 2026-11-02", "40", "Baseline"),
        ("- Target: 10", "- Target: 40", "direction"),
        ("- Channel: C0123456789", "- Channel: C0123456789\n- Parallel: 0", "Parallel"),
    ],
)
def test_metric_rejects_bad_fields(old: str, new: str, message: str) -> None:
    with pytest.raises(BlockError, match=message):
        parse_metric(METRIC.replace(old, new))


def test_metric_missing_block() -> None:
    with pytest.raises(NoBlock):
        parse_metric("## Context\nnothing here")


SIGNAL = """\
Make the nightly job retry on timeouts.

## Success signal
- Signal: promql: sum(increase(job_failures_total[7d]))
- Expect: < 15
- Check: 7d
"""


def test_success_signal() -> None:
    signal = parse_success_signal(SIGNAL)
    assert signal.op == "<" and signal.threshold == 15
    assert signal.check == timedelta(days=7)
    assert signal.passes(14) and not signal.passes(15)


def test_success_signal_none_needs_reason() -> None:
    assert parse_success_signal("## Success signal\nNone: docs only") is None
    with pytest.raises(BlockError):
        parse_success_signal("## Success signal\nNone:")


def test_success_signal_rejects_tickets_kind_and_bad_expect() -> None:
    with pytest.raises(BlockError, match="kind"):
        parse_success_signal(SIGNAL.replace("promql: sum(increase(job_failures_total[7d]))", "tickets: open"))
    with pytest.raises(BlockError, match="Expect"):
        parse_success_signal(SIGNAL.replace("< 15", "about 15"))
