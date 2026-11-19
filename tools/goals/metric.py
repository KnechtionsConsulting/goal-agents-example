"""Parse the two blocks a goal runs on.

A goal is a Linear project whose description starts with a Metric block:

    ## Metric
    - Signal: promql: histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[1d])) by (le))
    - Baseline: 1.8 on 2026-11-02
    - Target: 1.2
    - Channel: C0123456789
    - Parallel: 2

Each ticket the planner files carries a Success signal, which the verifier checks
after the change ships:

    ## Success signal
    - Signal: promql: histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[1d])) by (le))
    - Expect: < 1.6
    - Check: 7d

A ticket with nothing to measure says so instead:

    ## Success signal
    None: refactor with no runtime effect

Scripts read these blocks, so this module is the one definition of what is valid.
"""

from __future__ import annotations

import operator
import re
from dataclasses import dataclass
from datetime import date, timedelta

FIELD = re.compile(r"^[-*]\s*(?P<name>[A-Za-z]+)\s*:\s*(?P<value>.*?)\s*$")
CHANNEL = re.compile(r"^[CG][A-Z0-9]{8,}$")
BASELINE = re.compile(r"^(?P<value>-?\d+(?:\.\d+)?)\s+on\s+(?P<date>\d{4}-\d{2}-\d{2})$")
EXPECT = re.compile(r"^(?P<op><=|>=|<|>)\s*(?P<value>-?\d+(?:\.\d+)?)$")
CHECK = re.compile(r"^(?P<days>\d+)d$")
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class NoBlock(LookupError):
    """The text has no block with this heading."""


class BlockError(ValueError):
    """The block is there but a field is missing or malformed."""


@dataclass(frozen=True)
class Signal:
    kind: str  # "promql" or "tickets"
    query: str  # the PromQL, or "open" for tickets


@dataclass(frozen=True)
class Metric:
    signal: Signal
    baseline: float
    baseline_date: date
    target: float
    channel: str
    parallel: int = 2

    @property
    def lower_is_better(self) -> bool:
        return self.target < self.baseline

    def better(self, a: float, b: float) -> bool:
        """Whether reading `a` is strictly better than reading `b`."""
        return a < b if self.lower_is_better else a > b

    def reached(self, value: float) -> bool:
        return value <= self.target if self.lower_is_better else value >= self.target


@dataclass(frozen=True)
class SuccessSignal:
    signal: Signal
    op: str
    threshold: float
    check: timedelta

    def passes(self, value: float) -> bool:
        return OPS[self.op](value, self.threshold)


def _section(text: str, heading: str) -> list[str]:
    """Return the lines under `## <heading>` up to the next heading."""
    lines = (text or "").splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.fullmatch(rf"##\s+{re.escape(heading)}\s*", line.strip(), re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        raise NoBlock(f"no '## {heading}' section")
    body = []
    for line in lines[start:]:
        if line.startswith("#"):
            break
        body.append(line.strip())
    return body


def _fields(lines: list[str], allowed: set[str]) -> dict[str, str]:
    """Read the leading bullet lines. The first line that is not a bullet ends the block."""
    fields: dict[str, str] = {}
    started = False
    for line in lines:
        if not line:
            if started:
                break
            continue
        if not line.startswith(("-", "*")):
            break
        started = True
        match = FIELD.match(line)
        if not match:
            raise BlockError(f"not a 'Name: value' field: {line!r}")
        name = match["name"].lower()
        if name not in allowed:
            raise BlockError(f"unknown field {match['name']!r}")
        if name in fields:
            raise BlockError(f"field {match['name']!r} appears twice")
        fields[name] = match["value"]
    return fields


def _require(fields: dict[str, str], *names: str) -> None:
    missing = [n for n in names if not fields.get(n)]
    if missing:
        raise BlockError(f"missing field(s): {', '.join(missing)}")


def parse_signal(value: str, kinds: tuple[str, ...]) -> Signal:
    kind, _, query = value.partition(":")
    kind, query = kind.strip().lower(), query.strip()
    if kind not in kinds:
        raise BlockError(f"Signal kind must be one of {', '.join(kinds)}, got {kind!r}")
    if not query:
        raise BlockError("Signal has no query")
    if kind == "tickets" and query != "open":
        raise BlockError("the only tickets signal is 'tickets: open'")
    return Signal(kind, query)


def parse_metric(text: str) -> Metric:
    fields = _fields(_section(text, "Metric"), {"signal", "baseline", "target", "channel", "parallel"})
    _require(fields, "signal", "baseline", "target", "channel")

    baseline = BASELINE.match(fields["baseline"])
    if not baseline:
        raise BlockError("Baseline must read '<number> on <YYYY-MM-DD>'")
    try:
        target = float(fields["target"])
    except ValueError:
        raise BlockError("Target must be a number") from None
    if target == float(baseline["value"]):
        raise BlockError("Target equals Baseline, so the direction is unknown")
    if not CHANNEL.match(fields["channel"]):
        raise BlockError("Channel must be a Slack channel ID such as C0123456789")
    parallel = fields.get("parallel", "2")
    if not parallel.isdigit() or int(parallel) < 1:
        raise BlockError("Parallel must be a whole number of 1 or more")

    return Metric(
        signal=parse_signal(fields["signal"], ("promql", "tickets")),
        baseline=float(baseline["value"]),
        baseline_date=date.fromisoformat(baseline["date"]),
        target=target,
        channel=fields["channel"],
        parallel=int(parallel),
    )


def parse_success_signal(text: str) -> SuccessSignal | None:
    """Return the ticket's Success signal, or None when it says `None: <reason>`."""
    lines = _section(text, "Success signal")
    first = next((line for line in lines if line), "")
    if first.lower().startswith("none:"):
        if not first[5:].strip():
            raise BlockError("'None:' needs a reason")
        return None

    fields = _fields(lines, {"signal", "expect", "check"})
    _require(fields, "signal", "expect", "check")
    expect = EXPECT.match(fields["expect"])
    if not expect:
        raise BlockError("Expect must read '<op> <number>' with op one of <, <=, >, >=")
    check = CHECK.match(fields["check"])
    if not check or int(check["days"]) < 1:
        raise BlockError("Check must read '<days>d', such as 7d")
    return SuccessSignal(
        signal=parse_signal(fields["signal"], ("promql",)),
        op=expect["op"],
        threshold=float(expect["value"]),
        check=timedelta(days=int(check["days"])),
    )


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Check a Metric block or Success signal")
    parser.add_argument("kind", choices=["metric", "signal"])
    parser.add_argument("file")
    args = parser.parse_args()
    with open(args.file) as f:
        text = f.read()
    try:
        result = parse_metric(text) if args.kind == "metric" else parse_success_signal(text)
    except (NoBlock, BlockError) as e:
        print(f"invalid: {e}", file=sys.stderr)
        sys.exit(1)
    print(result)
