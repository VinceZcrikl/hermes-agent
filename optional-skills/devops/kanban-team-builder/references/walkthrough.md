# Team Builder — End-to-End Walkthrough

This is what a clean team-builder run looks like, from dispatcher
hand-off to fan-out.

## Setup

- Dashboard `profile_provisioning=auto`.
- Roster: `default`, `backend-dev`, `frontend-dev`, `team-builder`.
- A user creates a task: `assignee=qa-engineer`, title="Add E2E tests for checkout flow".
- Profile `qa-engineer` does not exist.

## Dispatcher (no human action)

1. `dispatch_once()` finds the task in `ready`.
2. `profile_exists("qa-engineer")` returns False.
3. Mode is `auto` and `team-builder` profile exists, so the dispatcher
   rewrites `assignee` to `team-builder`, merges `kanban-team-builder`
   into the task's `skills` JSON column (so the spawn command picks
   up `--skills kanban-team-builder`), and emits a `profile.required`
   event with payload
   `{"original_assignee": "qa-engineer", "rewritten_to": "team-builder"}`.
4. Next tick: dispatcher spawns a worker for the task under
   `team-builder`. The worker has `kanban-team-builder` skill loaded
   via the `--skills` flag (the standard kanban per-task skill
   mechanism — Hermes has no profile-level skill auto-load).

## Worker run (LLM, with kanban-team-builder skill)

```text
1. kanban_show()
   → task.title = "Add E2E tests for checkout flow"
   → events tail includes profile.required {original_assignee: qa-engineer}
   → conclusion: a QA-shaped role is needed.

2. kanban_list_profiles()
   → returns [default, backend-dev, frontend-dev, team-builder]
   → none of them have testing skills loaded.
   → decision: create_new "qa-engineer" cloned from "default", with
     skills=[playwright, jest], role="QA Engineer".

3. kanban_propose_profile(
     name="qa-engineer",
     role="QA Engineer",
     description="Owns automated end-to-end tests; uses Playwright "
                 "and Jest; reports flakes back to the implementing "
                 "profile via comments.",
     base="default",
     skills=["playwright", "jest"],
     toolsets=["terminal", "http"],
   )
   → ok=true, proposal_event_id=4711, requires_human_approval=false

4. kanban_provision_profile(proposal_event_id=4711)
   → ok=true, profile_path=~/.hermes/profiles/qa-engineer/
   → records the new profile on the parent task's created_profiles.

5. kanban_create(
     title="Add E2E tests for checkout flow",
     assignee="qa-engineer",
     body="<full original spec>",
     parents=[<this_task_id>],
   )
   → returns t_d2f9...

6. kanban_complete(
     summary="Provisioned qa-engineer profile and dispatched the "
             "checkout E2E task to it.",
     metadata={
       "created_profiles": [{"name": "qa-engineer", "role": "QA Engineer",
                              "base": "default"}],
       "reused_profiles": [],
       "child_tasks": ["t_d2f9..."],
     },
     created_cards=["t_d2f9..."],
   )
```

## What happens next (no team-builder action)

Dispatcher next tick → spawns a worker under `qa-engineer` for
`t_d2f9...`. That worker runs the substantive E2E test work; it
never sees team-builder again unless a future run hits the same path.

## Edge cases this walkthrough deliberately avoids

- **Manual mode**: step 4 would not run; the team-builder would
  `kanban_comment` and stop after step 3, waiting for the dashboard's
  `/profiles/proposals/{id}/approve` to materialize the profile and
  re-promote the parent task.
- **Reuse instead of create**: if `backend-dev-qa` already existed
  with playwright skills, step 2 would produce `decision = reuse:
  backend-dev-qa` and the workflow would skip directly to step 5,
  passing `assignee="backend-dev-qa"` to `kanban_create`.
- **Cluster of roles**: a multi-role requirement runs steps 3-5 once
  per role, accumulating proposals before fan-out.
