# Anti-Patterns — Long-Form

Each rule in `SKILL.md` exists because of a specific failure mode we
have seen or want to prevent. This file expands the rationale.

## "One profile per task" / per-task profiles

**Symptom**: the team-builder creates `task-42-helper`,
`research-task-99`, `qa-for-the-checkout-bug`, etc.

**Why it is bad**:

1. Profiles are persistent. A per-task profile becomes a per-task
   skeleton on disk forever, polluting the roster.
2. The next team-builder run sees N similar profiles and either
   reuses one at random (wrong) or proliferates more (worse).
3. Dashboard cards become unreadable — every task carries a unique
   profile, every profile has exactly one card.

**Right answer**: name the profile for the role, not the task.
`qa-engineer` is correct; `qa-engineer-for-task-42` is wrong.

## "Bypassing propose"

**Symptom**: the team-builder calls `kanban_provision_profile`
without a prior `kanban_propose_profile`. Allowed only via
`inline=` in `auto` mode for the explicit shortcut path.

**Why it is bad**:

1. Conflict checks (alias collision, name validity, duplicate
   profile) live inside propose. Skipping them produces
   `FileExistsError` deep in `create_profile` instead of a clean
   `{ok: false, conflicts: [...]}`.
2. The dashboard cannot show "what was proposed" if no proposal
   event exists.
3. Manual-mode operators have nothing to approve or reject.

**Right answer**: propose first, then provision the
`proposal_event_id`. The two-step is cheap.

## "Spawning when reuse fits"

**Symptom**: every requirement results in N freshly-cloned profiles,
each with a slightly different name.

**Why it is bad**:

1. Two profiles with the same role + skills + toolsets is dead
   weight. The dispatcher will balance load across them
   accidentally and badly.
2. Future team-builder runs see overlapping options and pick
   non-deterministically — same input, different team.

**Right answer**: reuse is the default. Create only when an
existing profile cannot do the role even after a clone.

## "Re-proposing under conflicts"

**Symptom**: `kanban_propose_profile("qa-engineer")` returns
`profile_exists`. The team-builder picks `qa-engineer-2` and
proposes again, succeeding.

**Why it is bad**:

1. You have just shadow-cloned the role under a different name.
   The old profile and the new profile do the same thing.
2. The conflict was a signal to reuse, not to rename.

**Right answer**: switch the decision to `reuse:qa-engineer`. If
the existing profile genuinely lacks a skill, propose a clone
(`clone_from:qa-engineer`) under a specialization name like
`qa-engineer-mobile` — but only when the specialization is real.

## "Shell-installing skills"

**Symptom**: the team-builder's reasoning includes
`"I'll run hermes skills install …"`.

**Why it is bad**:

1. The dispatcher runs you in a profile that already has the
   skills it needs. If a tool says "no", that's a constraint,
   not a missing dependency you should fix.
2. Shell installation is non-transactional and runs outside the
   kanban audit trail. Profile lifecycle should always go through
   the propose / provision tools so the parent task records what
   was created.

**Right answer**: when a tool refuses, post a `kanban_comment`
explaining what you wanted to do, then `kanban_block` (if the
operator must intervene) or pivot the design.

## "Creating without recording"

**Symptom**: the team-builder calls some shell command or API to
create a profile and forgets to call `kanban_provision_profile`.
The on-disk profile exists but no `created_profiles` entry on the
parent task.

**Why it is bad**:

1. Dashboard "what did this task produce?" view is wrong.
2. Reverse-lookup (`find_tasks_by_created_profile(name)`) returns
   empty for an obviously created profile, breaking orphan
   detection.

**Right answer**: always go through the tool. The tool records
the audit trail as part of its work.
