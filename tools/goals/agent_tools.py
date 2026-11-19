"""Commands the planner and executor agents run.

The agents decide what to plan and how to change the code. Everything they read from or
write to Linear goes through these commands, so a ticket always comes out in the shape the
scoreboard and verifier read, and a retried run never files a ticket twice.

    agent_tools.py marker <file>              which goal and phase a ✅'d message is for
    agent_tools.py goal <project-id>          the goal, its recent readings and open tickets
    agent_tools.py issue <identifier>         one ticket
    agent_tools.py create-ticket --project <id> --title <t> --body-file <f> --key <k>
    agent_tools.py comment <identifier> --body-file <f>
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from clients import Linear
from metric import BlockError, NoBlock, parse_metric, parse_success_signal
from scoreboard import history
from verifier import PENDING

# Matched as a token, not at a line start: Slack can return a bot message with its
# newlines collapsed.
MARKER = re.compile(r"(?:^|\s)goal-(?P<phase>proposal|plan) (?P<project>[0-9a-f-]{36})(?:\s|$)")
PHASES = {"proposal": "plan", "plan": "create"}


def find_marker(text: str) -> dict | None:
    match = MARKER.search(text)
    if not match:
        return None
    return {"phase": PHASES[match["phase"]], "project_id": match["project"]}


def ticket_key_line(key: str) -> str:
    return f"goal-ticket: {key}"


def cmd_marker(args: argparse.Namespace) -> int:
    with open(args.file) as f:
        found = find_marker(f.read())
    if not found:
        return 1  # not a message this agent acts on
    print(json.dumps(found))
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    linear = Linear()
    project = linear.project(args.project_id)
    metric = parse_metric(project["content"])
    readings = history(linear.project_updates(args.project_id))[-14:]
    issues = linear.project_issues(args.project_id)
    print(
        json.dumps(
            {
                "name": project["name"],
                "url": project["url"],
                "description": project["content"],
                "lower_is_better": metric.lower_is_better,
                "baseline": metric.baseline,
                "target": metric.target,
                "readings": [{"date": str(d), "value": v} for d, v in readings],
                "tickets": [
                    {
                        "identifier": i.identifier,
                        "title": i.title,
                        "state": i.state_type,
                        "labels": sorted(i.labels),
                    }
                    for i in issues
                ],
            },
            indent=2,
        )
    )
    return 0


def cmd_issue(args: argparse.Namespace) -> int:
    issue = Linear().issue(args.identifier)
    print(json.dumps({k: issue[k] for k in ("identifier", "title", "url", "description")}, indent=2))
    return 0


def cmd_create_ticket(args: argparse.Namespace) -> int:
    with open(args.body_file) as f:
        body = f.read()
    try:
        signal = parse_success_signal(body)
    except (NoBlock, BlockError) as e:
        print(f"invalid Success signal: {e}", file=sys.stderr)
        return 1

    linear = Linear()
    key_line = ticket_key_line(args.key)
    for issue in linear.project_issues(args.project):
        if key_line in issue.description:
            print(json.dumps({"identifier": issue.identifier, "url": issue.url, "existing": True}))
            return 0

    team = linear.query(
        "query($id: String!) { project(id: $id) { teams(first: 1) { nodes { id states { nodes { id type position } } } } } }",
        id=args.project,
    )["project"]["teams"]["nodes"][0]
    todo = min((s for s in team["states"]["nodes"] if s["type"] == "unstarted"), key=lambda s: s["position"])
    labels = [linear.label_id(PENDING)] if signal is not None else []
    created = linear.create_issue(
        team_id=team["id"],
        project_id=args.project,
        title=args.title,
        description=f"{body.rstrip()}\n\n{key_line}\n",
        state_id=todo["id"],
        label_ids=labels,
    )
    print(json.dumps({**created, "existing": False}))
    return 0


def cmd_comment(args: argparse.Namespace) -> int:
    linear = Linear()
    with open(args.body_file) as f:
        linear.comment(linear.issue(args.identifier)["id"], f.read())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    for name, positional, fn in (
        ("marker", "file", cmd_marker),
        ("goal", "project_id", cmd_goal),
        ("issue", "identifier", cmd_issue),
    ):
        p = sub.add_parser(name)
        p.add_argument(positional)
        p.set_defaults(fn=fn)
    p = sub.add_parser("create-ticket")
    p.add_argument("--project", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", required=True)
    p.add_argument("--key", required=True, help="unique per plan item, so a rerun files nothing twice")
    p.set_defaults(fn=cmd_create_ticket)
    p = sub.add_parser("comment")
    p.add_argument("identifier")
    p.add_argument("--body-file", required=True)
    p.set_defaults(fn=cmd_comment)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
