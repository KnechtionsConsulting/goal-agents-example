# Goal-driven agents on Kelos

A minimal version of agents that work toward a measured goal. You write down a metric, a baseline and a target. From then on, agents plan tickets that should move the metric, implement them one pull request at a time, and check each change against its own success signal after it ships. A person steers the loop with ✅ reactions in Slack.

It runs on [Kelos](https://github.com/kelos-dev/kelos), which turns Slack reactions and webhooks into coding-agent Tasks on Kubernetes. Linear holds the goals and tickets, Slack is where people follow and steer them, and Prometheus supplies the numbers.

## The loop

```
                 ┌──────────────────────── daily ────────────────────────┐
                 ▼                                                       │
   Linear project with a Metric block                                    │
                 │                                                       │
        goal-scoreboard (CronJob)                                        │
        measure → post the reading → record it → flag?                   │
                 │                         │                             │
                 │ no flag                 │ stalled / worse / no tickets │
                 ▼                         ▼                             │
     dispatch the next Todo ticket    proposal in Slack                  │
     (move it to In Progress,          "React ✅ to plan"                │
      comment /execute)                    │ ✅                           │
                 │                         ▼                             │
                 │              goal-planner (Kelos, Slack ✅)            │
                 │              plan reply "React ✅ to create"           │
                 │                         │ ✅                           │
                 │                         ▼                             │
                 │              tickets in Todo, each with a             │
                 │              Success signal and ship:pending ─────────┤
                 ▼                                                       │
     goal-executor (Kelos, Linear /execute comment)                      │
     one ticket → one pull request → a person reviews and merges         │
                 │                                                       │
                 ▼                                                       │
     goal-verifier (CronJob), Check days after the ticket is Done        │
     measure the Success signal → ship:passed or ship:failed ────────────┘
```

## What is where

| Path | What it does |
|---|---|
| `tools/goals/metric.py` | Parses a project's Metric block and a ticket's Success signal. The one definition of both. |
| `tools/goals/scoreboard.py` | The daily run: measures each goal, posts and records the reading, raises flags, dispatches one ticket. |
| `tools/goals/verifier.py` | Checks shipped tickets against their Success signal and labels the verdict. |
| `tools/goals/agent_tools.py` | The commands the planner and executor run to read goals and write tickets and comments. |
| `tools/goals/clients.py` | Small Linear, Slack and Prometheus clients, standard library only. |
| `deploy/goals/cronjobs.yaml` | The scoreboard and verifier as plain Kubernetes CronJobs. |
| `deploy/goals/planner-spawner.yaml` | Kelos TaskSpawner that plans on a ✅. |
| `deploy/goals/executor-spawner.yaml` | Kelos TaskSpawner that implements a ticket on a `/execute` comment. |
| `deploy/goals/base.yaml` | The Workspace (the repo agents clone), the shared Secret and a daily cost cap per agent. |
| `deploy/goals/agent.Dockerfile` | The Kelos Claude Code image plus python3. |
| `examples/` | A goal's project description and a ticket the planner would file. |

## How it is built

**A goal is data in the tracker.** A goal is any Linear project whose description starts with a Metric block (`examples/goal-project.md`). Nothing is registered anywhere else, so starting a goal is writing that block and creating a channel. The direction comes from the numbers: a target below the baseline means lower is better.

**Scripts decide and agents act.** Every number, flag and dispatch decision comes from deterministic code that runs as a CronJob with no model in it. Models do the two jobs that need judgment: choosing what to change, and changing it. When a reading or a dispatch looks wrong, it can be traced to a script and a test.

**People steer with two reactions.** The scoreboard asks for help only when a flag says the goal needs a decision: it is worse than its baseline, it has not improved in five readings, or it has run out of tickets. One ✅ asks the planner for a plan, and a second ✅ on the plan files the tickets. No ticket is created without both.

**Markers route the reactions.** A proposal carries `goal-proposal <project-id>` and a plan carries `goal-plan <project-id>`. The planner reads the marker on the message that got the ✅ to know which goal it is for and which phase to run. The markers are matched as tokens rather than at a line start because Slack can return a bot message with its newlines collapsed.

**One ticket at a time per slot.** Each run starts at most one ticket, and only while fewer than `Parallel` tickets are In Progress. The scoreboard moves the ticket to In Progress before it comments `/execute`, so a failed comment leaves one stuck ticket for a person rather than two executors on one ticket.

**Every change is judged after it ships.** The planner writes a Success signal into each ticket before any code exists: a query, the value it should reach and how many days after the ticket is Done to check it. The verifier measures it then and labels the ticket `ship:passed` or `ship:failed`. A failed verdict goes to the goal's channel for a person to act on.

**Reruns do nothing twice.** Each run records its reading in a Linear project status update with a `goal-reading: <date> <value>` line, and the status updates are the goal's history. A second run on the same day finds today's reading and skips the goal. A proposal is recorded the same way and posted at most once a week for the same flags. Each ticket the planner files carries a `goal-ticket: <key>` line, so rerunning the create phase finds it instead of filing it again.

**Spend is capped per agent.** Each spawner has a Kelos `TaskBudget`, and Kelos stops creating Tasks for that spawner once the day's cap is spent.

## Setting it up

1. Copy `tools/goals/` into the repository the agents will work in, and point the Workspace and the CronJobs at that repository.
2. Build `deploy/goals/agent.Dockerfile`, push it, and set the `image:` on both spawners.
3. In Linear, create the labels `ship:pending`, `ship:passed`, `ship:failed`, `ship:invalid` and `no-dispatch`. Add a webhook for Comment events that posts to `https://<kelos-webhook-host>/webhook/linear-comment`.
4. Install Kelos with its Slack server and webhook server. The Slack app needs the `reaction_added` event and the `reactions:read` scope, and the bot token the scripts use needs `chat:write`.
5. Fill in `deploy/goals/base.yaml` and apply: `kubectl apply -k deploy/goals`.
6. Start a goal: create a Linear project with a Metric block, then invite the bot to the channel named in it. Check the block first with `python3 tools/goals/metric.py metric <file>`.

Run the tests with `pytest tools/goals/tests`.

## Known gap

A Kelos reaction trigger matches on the emoji only. It can exclude messages by pattern but cannot require one, so any ✅ in a channel the bot is in starts a planner Task. Step 0 of the planner ends those runs without doing anything, but Kelos has already posted its "Working on your request..." reply in the thread. Inviting the bot only to goal channels keeps this rare. A trigger that requires the reacted-to message to match a pattern would close the gap.

## What a production version adds

This example leaves out work that a larger deployment needs:

- **Dispatch on merge.** A webhook on merged executor pull requests starts the next ticket at once, and the daily run stays as the fallback.
- **Recovering lost executors.** A ticket with no progress for a few hours is retried, and handed back to a person after two retries.
- **Pacing.** Wait for the last change's verdict, or a fixed delay after a merge, before starting the next ticket.
- **One reviewer per slot.** Give each project member one open pull request at a time and assign them the ticket.
- **One Slack thread per ticket**, where every agent posts its events and people leave notes the executor reads before it starts.
- **Automatic follow-ups** for a failed verdict, capped so a chain of failures comes back to a person.
- **Feeders.** Spawners that file tickets from a specific source, such as error tracking, instead of the planner.
- **More kinds of metric**, such as a number computed by a skill or a script, and Success signals checked in a browser.
