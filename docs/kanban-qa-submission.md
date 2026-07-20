# Kanban QA submission and Motoko sign-off

## Purpose

`kanban_submit_qa` is the board-backed handoff from an active worker to Motoko.
It is intentionally not a side-channel approval: it ends the active worker run,
sets the task status to `qa_review`, assigns the task to `motoko`, records a
`qa_submitted` event, and adds a `QA evidence:` task comment.

## Agent-tool invocation

From the active task's worker session, call:

```text
kanban_submit_qa(evidence="tests: 12/12 pass; source SHA: abc123; reviewed required gate")
```

`task_id` is optional for a dispatched worker; it defaults to
`HERMES_KANBAN_TASK`. The task must be `running` with a live run id, and
`evidence` must be non-empty.

A successful response reports `status: "qa_review"`. The dispatcher then
routes the board item to Motoko. Motoko records a pass by completing the
claimed QA task with normal completion proof metadata. A rejection is recorded
with `kanban_reject_qa(reason="...")`, which writes `qa_rejected` and a
capability block.

## CLI invocation

For operator or scripted use:

```bash
hermes kanban submit-qa <task-id> "tests pass; evidence summary"
# Alias: hermes kanban qa-submit <task-id> "tests pass; evidence summary"
```

## Valid board evidence

A valid submission has all of the following durable records for the same task:

1. `task_events.kind = "qa_submitted"`, including `evidence` and
   `submitted_by`.
2. `tasks.status = "qa_review"` and `tasks.assignee = "motoko"` before QA
   claims the item.
3. A `QA evidence: ...` row in `task_comments`.
4. After Motoko passes, a completed Motoko run with required completion proof
   metadata and a `completed` event.

The non-Motoko worker completion gate checks for `qa_submitted` in
`task_events` and fails closed if that durable record is absent. A
`qa_submitted` record only establishes that the work reached QA; it does not
convert a failing review into a pass. Release gates must additionally verify
Motoko's completed QA result and the substantive acceptance criteria.
