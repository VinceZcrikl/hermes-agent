---
name: kanban-team-builder
description: Decompose a high-level requirement into roles, decide reuse-vs-create against the live profile roster, and provision missing profiles through kanban_propose_profile + kanban_provision_profile. Loaded automatically into the bundled `team-builder` profile when the kanban dashboard is in `auto` mode; opt-in for other profiles.
version: 0.1.0
metadata:
  hermes:
    tags: [kanban, multi-agent, profile-provisioning, orchestration]
    related_skills: [kanban-orchestrator, kanban-worker]
---

# Kanban Team Builder — Auto-Provision Roles for a Requirement

> You are reading this skill because the dispatcher routed a task to the
> `team-builder` profile (auto mode) or because the task was tagged with
> `metadata.needs_team_builder=true`. Your job is **roster design and
> profile provisioning** — not the substantive work itself.

## When you are activated

The kanban dispatcher rewrites a task's assignee to `team-builder` when:

1. The dashboard `profile_provisioning` setting is `auto`.
2. The task's original assignee does not resolve to a real Hermes profile.
3. The `team-builder` profile + `kanban-team-builder` skill are installed
   (the dashboard PATCH endpoint guarantees this when an operator toggles
   to auto mode; if you see this skill loaded, both prerequisites hold).
   The skill loads via the dispatcher's `--skills` flag — the assignee
   rewrite also merges `kanban-team-builder` into the task's `skills`
   column so the spawned worker receives it.

The original assignee is preserved on the task as a `profile.required`
event with `payload.original_assignee` — read it via
`kanban_show()` (the event tail is in `worker_context`) or by querying
`task_events` directly for `kind='profile.required'`.

## Mandatory workflow

Run these steps in order. Do not skip the roster check; the whole
point of this workflow is to avoid creating duplicate profiles.

1. **Read the requirement.** `kanban_show()` on your current task.
   Extract the original assignee from the latest `profile.required`
   event's payload and treat it as the role hint (e.g.
   `original_assignee="qa-engineer"` means a QA-shaped role is wanted).

2. **Pull the roster.** Call `kanban_list_profiles()` once. The response
   includes for each existing profile: name, role, description,
   loaded skills, toolsets, model, and per-profile open/running/done
   task counts. Treat this as your ground truth — a profile that
   isn't here doesn't exist.

3. **Decompose the requirement into candidate roles.** For most tasks
   one or two roles is the right answer. Resist scaffolding a
   ten-person org chart for what's actually a single backend change.

4. **Decide reuse vs. clone vs. create per role.** Apply these
   heuristics in order:

   - **Reuse (`reuse:<existing-name>`)** when an existing profile's
     role + skills + toolsets cover the candidate. Even an
     imperfect match should usually be a reuse: profiles get
     better with use, not with proliferation.
   - **Clone (`clone_from:<existing-name>`)** when an existing
     profile is the right starting point but needs a different
     model, SOUL, or one or two extra skills. Naming convention:
     `<base>-<specialization>` (e.g. `backend-dev-react`).
   - **Create (`create_new`)** only when no existing profile is
     within editing distance. New profiles are persistent — they
     will be reused by future runs, so name them for the role
     (`qa-engineer`), not the task (`task-123-helper`).

5. **Propose every create / clone with `kanban_propose_profile`.**
   The tool runs hard checks (name validity, alias collision,
   duplicate profile) and writes a `profile.proposed` event. Inspect
   the response:

   - `ok=true`: clean proposal.
   - `ok=false`: address each `conflicts[]` entry. A `profile_exists`
     conflict means you should reuse; rewrite your decision.
   - `requires_human_approval=true`: dashboard is in `manual` mode.
     Stop here, post a `kanban_comment` summarizing the design, and
     let the human approve in the UI. Do NOT call provision yet.

6. **Provision approved proposals (`auto` mode only).** Call
   `kanban_provision_profile(proposal_event_id=<id>)` once per
   approved proposal. The tool fuses on `off` mode and on missing
   approval in `manual` mode, so this is safe to call back-to-back
   with a successful propose under `auto`.

7. **Fan out child tasks.** For every candidate role (reuse or
   newly-provisioned), call `kanban_create(assignee=<name>,
   parents=[<your_task_id>], ...)` with the substantive work.
   The dispatcher takes over from there.

8. **Complete with the manifest.** `kanban_complete(summary=...,
   metadata={created_profiles: [...], reused_profiles: [...],
   child_tasks: [...]}, created_cards=[...])`. Hand the human a
   crisp record of the team you assembled.

## Decision rules (cheat sheet)

| Situation | Decision |
|---|---|
| Role is generic engineering work + `default` profile fits | reuse `default` |
| Existing profile matches role but is busy (high open count) | reuse anyway — clone only changes the SOUL, not throughput |
| Existing profile + new domain skill (translation, x-ray reading) | clone with `skills: [the_new_skill]` |
| New role with no analog in roster, broad scope | create_new |
| Two candidate roles overlap ≥70% (skills + toolsets) | merge into one role |
| Original_assignee is a known control-plane lane (`orion-cc`, etc.) | post a comment explaining you can't team-build for control-plane lanes; complete with `block` |

## Anti-patterns (do NOT do these)

- **Per-task profiles.** `task-42-helper`, `qa-for-this-bug`. Profiles
  are persistent — name for the role.
- **Bypassing propose.** Calling `kanban_provision_profile` without a
  prior `kanban_propose_profile` skips the conflict checks. Only
  acceptable in `auto` mode with `inline=` for explicit shortcut.
- **Spawning when reuse fits.** Two profiles with the same role +
  toolsets = noise. Reuse is the default; create is the exception.
- **Re-proposing under conflicts.** If a `profile_exists` conflict
  fires, switch to reuse. Don't pick a new name and re-propose —
  you'll just clone the role under a different label.
- **Shell-installing skills or profiles.** Use the tools. The
  dispatcher already installed everything you need; if a tool says
  no, post a comment and block, don't shell out.

## When to block instead of provision

- Original assignee is empty / NULL → block with a comment asking the
  task author to set an assignee or describe the role.
- `kanban_propose_profile` returns conflicts you can't resolve (e.g.
  every plausible name collides) → block with the conflict list.
- The substantive task is already inside team-builder's competence
  and creating a child task would loop forever → execute it inline,
  complete cleanly, do not provision.

## Outputs

When you complete, your `metadata` should look like:

```json
{
  "created_profiles": [
    {"name": "qa-engineer", "role": "QA Engineer",
     "base": "default", "skills": ["test-runner"]}
  ],
  "reused_profiles": [
    {"name": "backend-dev", "for_role": "Backend changes"}
  ],
  "child_tasks": ["t_abc123", "t_def456"],
  "rejected_designs": []
}
```

This shape is what the dashboard renders in the team-builder run
history; structured fields beat prose for downstream automation.

See `references/walkthrough.md` for an end-to-end example, and
`references/anti-patterns.md` for the long-form rationale behind the
"reuse-first" rule.
