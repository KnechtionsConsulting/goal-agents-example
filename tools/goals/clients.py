"""Thin clients for Linear, Slack and Prometheus. Standard library only."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime


def _post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


# --- Linear ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    title: str
    state_type: str  # triage, backlog, unstarted, started, completed, canceled
    priority: int  # 0 none, 1 urgent ... 4 low
    created_at: str
    labels: frozenset[str] = field(default_factory=frozenset)
    url: str = ""
    description: str = ""
    completed_at: str | None = None
    project_id: str | None = None


ISSUE_FIELDS = """
  id identifier title priority createdAt completedAt url description
  state { type } labels { nodes { name } } project { id }
"""


def _issue(node: dict) -> Issue:
    return Issue(
        id=node["id"],
        identifier=node["identifier"],
        title=node["title"],
        state_type=node["state"]["type"],
        priority=node["priority"],
        created_at=node["createdAt"],
        labels=frozenset(label["name"] for label in node["labels"]["nodes"]),
        url=node.get("url", ""),
        description=node.get("description") or "",
        completed_at=node.get("completedAt"),
        project_id=(node.get("project") or {}).get("id"),
    )


class Linear:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or os.environ["LINEAR_API_KEY"]

    def query(self, query: str, **variables: object) -> dict:
        body = _post_json(
            "https://api.linear.app/graphql",
            {"query": query, "variables": variables},
            {"Authorization": self.api_key},
        )
        if body.get("errors"):
            raise RuntimeError(f"Linear: {body['errors']}")
        return body["data"]

    def open_projects(self) -> list[dict]:
        data = self.query("{ projects(first: 100) { nodes { id name url content completedAt canceledAt } } }")
        return [p for p in data["projects"]["nodes"] if not p["completedAt"] and not p["canceledAt"]]

    def project(self, project_id: str) -> dict:
        return self.query("query($id: String!) { project(id: $id) { id name url content } }", id=project_id)[
            "project"
        ]

    def project_issues(self, project_id: str) -> list[Issue]:
        data = self.query(
            f"query($id: String!) {{ project(id: $id) {{ issues(first: 250) {{ nodes {{ {ISSUE_FIELDS} }} }} }} }}",
            id=project_id,
        )
        return [_issue(n) for n in data["project"]["issues"]["nodes"]]

    def project_updates(self, project_id: str) -> list[dict]:
        """The project's status updates, oldest first."""
        data = self.query(
            "query($id: String!) { project(id: $id) { projectUpdates(first: 50) { nodes { body createdAt } } } }",
            id=project_id,
        )
        return sorted(data["project"]["projectUpdates"]["nodes"], key=lambda u: u["createdAt"])

    def create_project_update(self, project_id: str, body: str, health: str) -> None:
        self.query(
            """mutation($input: ProjectUpdateCreateInput!) {
                 projectUpdateCreate(input: $input) { success } }""",
            input={"projectId": project_id, "body": body, "health": health},
        )

    def issue(self, identifier: str) -> dict:
        """The issue with its team's workflow states, for moving it."""
        return self.query(
            f"""query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS}
                  team {{ id states {{ nodes {{ id type position }} }} }} }} }}""",
            id=identifier,
        )["issue"]

    def state_id(self, issue: dict, state_type: str) -> str:
        states = [s for s in issue["team"]["states"]["nodes"] if s["type"] == state_type]
        return min(states, key=lambda s: s["position"])["id"]

    def move(self, issue_id: str, state_id: str) -> None:
        self.query(
            """mutation($id: String!, $state: String!) {
                 issueUpdate(id: $id, input: {stateId: $state}) { success } }""",
            id=issue_id,
            state=state_id,
        )

    def comment(self, issue_id: str, body: str) -> None:
        self.query(
            """mutation($input: CommentCreateInput!) { commentCreate(input: $input) { success } }""",
            input={"issueId": issue_id, "body": body},
        )

    def label_id(self, name: str) -> str:
        data = self.query(
            "query($name: String!) { issueLabels(filter: {name: {eq: $name}}) { nodes { id } } }",
            name=name,
        )
        nodes = data["issueLabels"]["nodes"]
        if not nodes:
            raise RuntimeError(f"Linear has no label named {name!r}; create it first")
        return nodes[0]["id"]

    def swap_label(self, issue_id: str, remove: str, add: str) -> None:
        self.query(
            """mutation($id: String!, $add: String!, $remove: String!) {
                 a: issueAddLabel(id: $id, labelId: $add) { success }
                 r: issueRemoveLabel(id: $id, labelId: $remove) { success } }""",
            id=issue_id,
            add=self.label_id(add),
            remove=self.label_id(remove),
        )

    def create_issue(
        self, team_id: str, project_id: str, title: str, description: str, state_id: str, label_ids: list[str]
    ) -> dict:
        data = self.query(
            """mutation($input: IssueCreateInput!) {
                 issueCreate(input: $input) { issue { identifier url } } }""",
            input={
                "teamId": team_id,
                "projectId": project_id,
                "title": title,
                "description": description,
                "stateId": state_id,
                "labelIds": label_ids,
            },
        )
        return data["issueCreate"]["issue"]

    def issues_with_label(self, label: str, state_type: str) -> list[Issue]:
        data = self.query(
            f"""query($label: String!, $state: String!) {{
                  issues(first: 100, filter: {{labels: {{name: {{eq: $label}}}},
                                               state: {{type: {{eq: $state}}}}}}) {{
                    nodes {{ {ISSUE_FIELDS} }} }} }}""",
            label=label,
            state=state_type,
        )
        return [_issue(n) for n in data["issues"]["nodes"]]


# --- Slack ----------------------------------------------------------------------------


def slack_post(channel: str, text: str, thread_ts: str | None = None) -> str:
    payload = {"channel": channel, "text": text, "unfurl_links": False}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    body = _post_json(
        "https://slack.com/api/chat.postMessage",
        payload,
        {"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
    )
    if not body.get("ok"):
        raise RuntimeError(f"Slack: {body.get('error')}")
    return body["ts"]


# --- Prometheus -----------------------------------------------------------------------


def prometheus_value(query: str, at: datetime | None = None) -> float:
    """Run an instant query that must return exactly one number."""
    params = {"query": query}
    if at:
        params["time"] = str(at.timestamp())
    url = f"{os.environ['PROMETHEUS_URL'].rstrip('/')}/api/v1/query?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        body = json.load(response)
    if body.get("status") != "success":
        raise RuntimeError(f"Prometheus: {body.get('error')}")
    result = body["data"]
    if result["resultType"] == "scalar":
        return float(result["result"][1])
    series = result["result"]
    if len(series) != 1:
        raise RuntimeError(f"query returned {len(series)} series; it must return exactly one")
    return float(series[0]["value"][1])


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
