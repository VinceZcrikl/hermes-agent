# Kanban Auto-Team — Implementation Plan

## Goal

Make "build a multi-agent team for a requirement" a first-class kanban capability. Today the dispatcher silently skips any task whose `assignee` does not resolve to an existing profile (`skipped_nonspawnable`); there is no runtime path that takes a high-level requirement, designs a roster, deduplicates against the existing profile inventory, and provisions the missing profiles linked back to the originating task. The closest prior art — `optional-skills/creative/kanban-video-orchestrator/` — is video-domain-specific, depends on a generated `setup.sh`, and requires a human to run it.

This plan adds a hybrid core + plugin + skill feature so an orchestrator profile can: (1) read the live profile roster through a tool, (2) let the LLM decide reuse vs. clone vs. create per role, (3) propose new profiles with hard collision checks, (4) provision approved proposals and persistently link them to the parent task. Behavior is opt-in via a three-state dashboard switch (`off` / `manual` / `auto`).

## Architecture Decisions

- **Three layers, one PR.** Core owns primitives (DB schema + agent tools + dispatcher hook), the dashboard plugin owns the human-facing approval surface, the optional skill owns the LLM-driven role-design workflow. Each layer has a job no other layer can do, and shipping them together avoids partial-feature confusion.
- **Default mode is `manual`, not `off`.** Existing deployments after upgrade see one visible-but-non-destructive change: tasks that used to be silently `skipped_nonspawnable` now surface as `profile.required` cards in the dashboard. That is strictly an observability improvement. Operators who want byte-identical legacy behavior can explicitly set `off`.
- **`auto` is transactional and self-bootstrapping.** When the operator toggles the dashboard switch to `auto`, the `PATCH` endpoint atomically installs the `kanban-team-builder` skill from the bundled `optional-skills/` source, auto-creates a `team-builder` orchestrator profile (cloned from `default`, with the skill in `always_load`), then persists the setting. Any failure aborts the toggle — no half-state. This eliminates the "set auto, but skill is not installed → tasks stuck forever" footgun.
- **Persistent profiles with reverse linkage.** Created profiles stay on disk under `~/.hermes/profiles/<name>/` and tasks record what they spawned. No automatic GC — orphan cleanup is a doctor concern, not a dispatcher concern.
- **LLM judges similarity, with the roster as context.** The `kanban_list_profiles` tool returns full role metadata; the orchestrator decides reuse / clone_from / create_new in-prompt. No vector store, no embeddings, no extra dependency. Hard-fail conditions (name collision, alias collision, illegal name) are still enforced inside `kanban_propose_profile` regardless of LLM judgment.
- **Dispatcher stays dumb.** Provisioning runs in an orchestrator profile, not in the dispatcher loop. The dispatcher only routes tasks based on a board-level `profile_provisioning` setting — it never invokes an LLM itself.
- **Reuse, do not duplicate.** All profile lifecycle calls funnel through the existing functions in `hermes_cli/profiles.py` (`create_profile`, `list_profiles`, `normalize_profile_name`, `validate_profile_name`, `profile_exists`, `check_alias_collision`, `seed_profile_skills`) and the existing skill installer at `hermes_cli/skills_hub.do_install`. No parallel implementation.

## Behavior Model

```
new task on board with assignee=X
  └─→ does profile X exist?
        ├─ yes → existing dispatcher path (unchanged)
        └─ no  → branch on board.kanban.profile_provisioning:
              ├─ off            → skipped_nonspawnable (legacy behavior, opt-in)
              ├─ manual (NEW    → emit profile.required event, hold task in
              │   DEFAULT)        ready; dashboard surfaces a card; human
              │                   picks reuse / create / cancel; approval
              │                   calls kanban_provision_profile
              └─ auto           → rewrite task.assignee to "team-builder",
                                  stash original under metadata.original_assignee,
                                  set metadata.needs_team_builder=true; task
                                  stays ready and the dispatcher spawns a
                                  normal team-builder worker on the same
                                  tick — no triage limbo, no separate
                                  orchestrator polling
```

The LLM workflow inside the skill:

1. `kanban_list_profiles()` — pull current roster and per-profile task counts into worker context.
2. Decompose the requirement into N candidate roles (`{role, responsibilities, skills, toolsets, model}`).
3. For each candidate role, decide `reuse:<name>` / `clone_from:<name>` / `create_new`. Heuristics in the skill prompt: synonymous role name, ≥70% skill overlap, and identical toolsets push toward reuse; only-model-or-SOUL difference pushes toward clone; orthogonal responsibilities push toward create.
4. Submit `create_new` and `clone_from` decisions through `kanban_propose_profile`. The tool runs hard checks (validate name, alias collision, profile_exists, similarity hint against the LLM-supplied description vs. existing roster) and writes a `profile.proposed` event.
5. In `auto` mode the orchestrator immediately calls `kanban_provision_profile`. In `manual` mode it stops and waits for the dashboard approval event.
6. After provisioning, the orchestrator emits `kanban_create` calls for each child task with the new assignees. The dispatcher takes over from there.

## Data Model Changes

### `tasks` table — new column

```sql
ALTER TABLE tasks ADD COLUMN created_profiles TEXT;
-- JSON array of {name, role, base, created_at, created_by_run, status}
-- NULL on legacy rows; readers normalize to []
```

Backfill is intentionally absent: NULL means "this task created nothing", which is correct for every existing row.

New helpers in `hermes_cli/kanban_db.py`:
- `record_created_profile(conn, task_id, profile_name, role, base, run_id)` — append-only mutation under the standard `BEGIN IMMEDIATE` transaction.
- `find_tasks_by_profile(conn, profile_name) -> list[TaskRow]` — used by doctor and dashboard for orphan / "where did this profile come from" queries.

### `task_events` table — new event kinds

No schema change — the existing `kind TEXT` column already accepts arbitrary strings. New kinds:
- `profile.required` — emitted by dispatcher in manual mode.
- `profile.proposed` — emitted by `kanban_propose_profile`. Payload: `{name, role, description, base, skills, toolsets, model, conflicts, similar_to}`.
- `profile.created` — emitted by `kanban_provision_profile`. Payload: `{name, role, base, profile_path}`.
- `profile.rejected` — emitted by dashboard reject endpoint. Payload: `{proposal_event_id, reason}`.

WebSocket subscribers receive these alongside existing task events without protocol changes.

## Core Changes

### `hermes_cli/kanban_db.py`
- Add the migration in `_apply_migrations()` (idempotent — SQLite raises if column exists, catch-and-skip).
- Add `record_created_profile`, `find_tasks_by_profile`.
- In `dispatch_once()` (around line 3124-3244), branch on board config when `profile_exists(assignee)` is false:
  - `off` → existing `skipped_nonspawnable` path.
  - `manual` → write `task_events(kind='profile.required')`, leave task in `ready`, log and continue.
  - `auto` → rewrite the row's `assignee` column to `team-builder`, merge `{original_assignee: <prior>, needs_team_builder: true}` into `metadata`, leave `status='ready'`. The same dispatcher tick will then pick the task up as a normal `team-builder` worker spawn.
- Configuration: read `kanban.profile_provisioning` from board config (`<board>/config.yaml`), with fallback to global `~/.hermes/config.yaml` `dashboard.kanban.profile_provisioning`. **Default is `manual`** when the key is absent, both for fresh installs and for upgrades. Operators who want strict legacy behavior set the value to `off` explicitly.

### `tools/kanban_tools.py`
Three new tools registered next to the current seven (around line 794-855), each gated by `_check_kanban_mode()` so they only appear inside dispatcher-spawned worker subprocesses.

- `kanban_list_profiles()` → `{profiles: [{name, description, role, skills, toolsets, model, task_counts: {open, running, done}, created_at}]}`. Backed by `list_profiles()` plus a per-profile `config.yaml` read for role/description/toolsets/skills.
- `kanban_propose_profile(name, role, description, base?, skills?, toolsets?, model?)` → `{ok, proposal_event_id, conflicts: [{kind, detail}], existing_match?, requires_human_approval}`. Does NOT create the profile. Runs `validate_profile_name`, `check_alias_collision`, `profile_exists`, then writes `profile.proposed`. `requires_human_approval=true` when the board is in `manual` mode.
- `kanban_provision_profile(proposal_event_id?, inline_args?, link_to_task=true)` → `{ok, profile_name, profile_path}`. Calls `create_profile(name, clone_from=base, clone_config=True)`, then `seed_profile_skills`, then `record_created_profile` on the worker's task, then writes `profile.created`. **Hard-fuses on `off` mode**: returns `{ok: false, reason: 'provisioning_disabled'}` immediately so a hallucinating LLM can never spawn a profile. In `manual` mode requires a `proposal_event_id` whose latest state is `approved` (joined against `profile.approved` events written by the dashboard).

Tool count assertion in `tests/tools/test_kanban_tools.py:43-52` updates from 7 to 10.

## Plugin Changes (dashboard backend)

`plugins/kanban/dashboard/plugin_api.py` — backend only. The dashboard frontend bundle lives in an external repo and is delivered separately; this PR ships the API surface so the frontend PR can land on top.

New endpoints (all under `/api/plugins/kanban/`):

- `GET /profiles` — full roster + per-profile aggregate (`open`, `running`, `done` task counts) merged from `list_profiles()` and a `find_tasks_by_profile`-backed aggregate query.
- `GET /profiles/proposals?status=pending|approved|rejected|all` — query `task_events.kind='profile.proposed'` filtered by latest follow-up event per proposal.
- `POST /profiles/proposals/{event_id}/approve` — calls `kanban_provision_profile` with `proposal_event_id=event_id`. Writes `profile.approved` event before provisioning so the tool's check passes.
- `POST /profiles/proposals/{event_id}/reject` body `{reason}` — writes `profile.rejected` event and posts a kanban comment on the parent task.
- `GET /settings/profile-provisioning` → `{value: 'off'|'manual'|'auto', source: 'board'|'global'|'default'}`. When the key is absent, returns `value='manual'` with `source='default'`.
- `PATCH /settings/profile-provisioning` body `{value, scope: 'board'|'global'}` — writes the appropriate config file. **When `value='auto'` the endpoint runs a transactional bootstrap before persisting**:
  1. Probe `~/.hermes/skills/kanban-team-builder/SKILL.md` to see whether the skill is already installed.
  2. If absent, call `skills_hub.do_install("official/devops/kanban-team-builder", skip_confirm=True, invalidate_cache=True)` which copies the skill from the bundled `optional-skills/` source into `~/.hermes/skills/`.
  3. Probe `profile_exists("team-builder")`.
  4. If absent, call `create_profile("team-builder", clone_from="default", clone_config=True)`, then merge `always_load: [kanban-team-builder]` into the new profile's `config.yaml`.
  5. Persist the setting into `~/.hermes/config.yaml` under `dashboard.kanban.profile_provisioning`.
  6. On any step failure: do NOT persist the setting, return `409` with the error chain. On success: return `{value: 'auto', provisioning_setup: {skill_installed_now, profile_created_now, team_builder_profile}}` so the UI can display "we did X, Y, Z for you".
  Reuses the same low-level config read/write helpers as `GET /config` (line 1059-1080).

WebSocket `/events` (line 1462-1530) needs no code change — it tails `task_events`, so the new event kinds flow automatically. Frontend filters them.

Auth model is unchanged: HTTP routes remain localhost-only; WebSocket still requires `?token=`.

**New plugin imports**: `hermes_cli/skills_hub.do_install` ([skills_hub.py:408](hermes_cli/skills_hub.py#L408)) and `hermes_cli/profiles.create_profile` ([profiles.py:424](hermes_cli/profiles.py#L424)). The `OptionalSkillSource` adapter ([tools/skills_hub.py:2324-2399](tools/skills_hub.py#L2324-L2399)) already exposes the bundled `optional-skills/` tree as the `official/...` source — no install fetch goes over the network.

## Skill Changes

New skill at `optional-skills/devops/kanban-team-builder/`:

```
optional-skills/devops/kanban-team-builder/
├── SKILL.md                         # decision framework + workflow
├── manifest.json                    # version, dependencies, activation rules
└── references/
    ├── role-archetype-matrix.md     # generic role library (researcher, backend, qa, ...)
    ├── walkthrough.md               # end-to-end example
    └── anti-patterns.md             # "do not create per-task profiles", etc.
```

`SKILL.md` content outline:

1. **Activation** — task carries `metadata.needs_team_builder=true` or `assignee=team-builder`.
2. **Mandatory workflow** — the six-step list above, written as instructions to the agent.
3. **Decision rules** — reuse / clone / create heuristics.
4. **Failure handling** — if `kanban_provision_profile` fails after a successful propose, emit a `kanban_comment`, do not retry blindly, leave the proposal in `proposed` state for human review.
5. **Anti-patterns** — never create one-shot profiles; never propose a profile whose role is already covered by two or more existing profiles; never bypass `kanban_propose_profile` and call CLI shell commands directly.

The skill ships in `optional-skills/` (not `skills/`) so it is opt-in via `hermes skills install official/devops/kanban-team-builder`. Default installs see no change.

## Files Touched

To modify:
- `tools/kanban_tools.py` — three new tools and their schemas.
- `hermes_cli/kanban_db.py` — migration, helpers, dispatcher hook.
- `plugins/kanban/dashboard/plugin_api.py` — six new endpoints.
- `website/docs/user-guide/features/kanban.md` — tool reference, auto-team section.
- `tests/tools/test_kanban_tools.py` — tool count + per-tool assertions.
- `tests/plugins/test_kanban_dashboard_plugin.py` — endpoint coverage.

To create:
- `optional-skills/devops/kanban-team-builder/{SKILL.md,manifest.json,references/*}`.
- `tests/tools/test_kanban_team_builder.py` — happy path, conflicts, idempotent re-propose.
- `tests/hermes_cli/test_kanban_provisioning.py` — dispatcher branch on each of `off` / `manual` / `auto`.

To read for design reference (no edit):
- `hermes_cli/profiles.py:182-566` — full profile lifecycle API.
- `hermes_cli/skills_hub.py:408` — `do_install(identifier, skip_confirm=True, invalidate_cache=True)` programmatic entry.
- `tools/skills_hub.py:2324-2399` — `OptionalSkillSource` registers `optional-skills/` as the `official/...` source with `builtin` trust.
- `hermes_cli/web_server.py:2466` — `POST /api/profiles` reference implementation.
- `optional-skills/creative/kanban-video-orchestrator/scripts/bootstrap_pipeline.py` — `validate_plan` / `render_team_md` patterns to mirror.
- `skills/devops/kanban-orchestrator/SKILL.md` — voice and structure for the new skill.

## Verification

1. **Unit tests.**
   - Tool schemas, happy paths, validation errors, idempotent re-propose under the same name.
   - Migration: applying twice is a no-op; legacy row reads return `[]`.
   - Dispatcher: each of `off` / `manual` / `auto` produces the right side effect on a synthetic task with a missing assignee. Specifically, `auto` mode rewrites `assignee` to `team-builder` and stashes `metadata.original_assignee`.
   - `kanban_provision_profile` hard-fuses with `{ok: false}` when the setting is `off`, even with valid arguments.
2. **Transactional toggle test (new).** Empty HOME with no `kanban-team-builder` skill and no `team-builder` profile.
   - `PATCH /settings/profile-provisioning {value: 'auto'}` — assert: skill appears at `~/.hermes/skills/kanban-team-builder/`, profile appears at `~/.hermes/profiles/team-builder/` with `always_load` containing the skill, and the setting is persisted.
   - Repeat the same `PATCH` — assert idempotent (no duplicate work, response indicates `skill_installed_now=false, profile_created_now=false`).
   - Inject a failure into `do_install` — assert the setting is NOT persisted, the profile is NOT created, and the response is `409` with the error chain.
3. **End-to-end smoke.** Spin up a temp HOME via `tests/hermes_cli/conftest.py`, set `profile_provisioning=auto` (which boots the skill + team-builder profile), seed one task with `assignee=non-existent`, run a dispatcher tick, and assert: assignee is rewritten to `team-builder`; `metadata.original_assignee='non-existent'`; a simulated team-builder worker that calls `kanban_list_profiles` → `kanban_propose_profile` → `kanban_provision_profile` → `kanban_create` produces a child task with the original `non-existent` assignee, the parent task's `created_profiles` JSON contains the new entry, and a `profile.created` event is in `task_events`. The next dispatcher tick spawns a worker for the new child task.
4. **Manual demo.** Start gateway + dashboard with an empty environment.
   - In the dashboard switch the setting from default `manual` to `auto`. Confirm the UI surfaces "we installed the skill and created the team-builder profile for you".
   - Create a task with `assignee=non-existent`. Confirm the dispatcher routes it to `team-builder`, the worker provisions a profile, and the child task carrying the original assignee shows up in `ready`.
   - Switch back to `manual`. Create another task with `assignee=does-not-exist`. Confirm the dashboard surfaces a `profile.required` card; approve it; confirm the profile lands on disk and a `profile.created` event is delivered over WebSocket.
5. **Regression.** The existing seven kanban tools and dispatcher behavior are untouched when `profile_provisioning=off`. With `manual` (the new default) on a deployment that previously had no setting, the only observable difference is that `profile.required` cards appear for tasks that used to be silently skipped — strictly an observability gain, no destructive change. Run the full kanban test suite to confirm zero regressions.
6. **Docs.** `website/docs/user-guide/features/kanban.md` builds clean and includes a new "Auto team building" section with the decision diagram from this plan, plus a changelog note flagging the default-mode change.

## Compatibility Envelope

Behavior under each setting when the operator does NOT install the skill manually:

| Setting | Behavior | Impact on existing deployments |
|---|---|---|
| `manual` (new default) | Tasks with missing assignees emit `profile.required` events; the dashboard surfaces them as cards; humans drive approval. The skill is irrelevant — the human is the orchestrator. | Non-destructive observability change after upgrade: tasks that used to disappear silently now appear as cards. |
| `off` (opt-in legacy) | Identical to pre-PR behavior — `skipped_nonspawnable`. | Zero behavior change for operators who explicitly select it. |
| `auto` + skill not installed | Cannot happen: the `PATCH` endpoint installs the skill and creates the `team-builder` profile transactionally before persisting. Failed bootstrap → 409 → setting is not stored. | No "stuck task" footgun. |

**Migration safety:**
- The `tasks.created_profiles` `ALTER TABLE` is wrapped in `try/except OperationalError: duplicate column name` for idempotency.
- Existing readers must use `sqlite3.Row` factory access (column-name lookup) to remain forward-compatible. PR sweeps `hermes_cli/kanban_db.py` for any positional `row[i]` access on `tasks` and converts to `row['col']`.
- `kanban_provision_profile` hard-fuses on `off` so a hallucinating worker LLM cannot create profiles even if it manages to call the tool.

## Rollout

- Single PR carries core + plugin + skill + tests + docs.
- Default mode changes to `manual`. Release notes call out the change explicitly: "Tasks with missing assignees previously disappeared silently; they now surface as `profile.required` cards. To restore strict legacy behavior, set `dashboard.kanban.profile_provisioning: off` in `~/.hermes/config.yaml`."
- `auto` mode self-bootstraps the skill and the orchestrator profile transactionally, so operators can flip the dashboard switch without separately running `hermes skills install`.
- Frontend bundle for the dashboard is delivered through the existing external frontend release pipeline; this PR's backend exposes a stable contract the frontend can land against without blocking the merge.

## Non-Goals

- No vector store, no embedding-based similarity. The LLM is the judge.
- No automatic profile destruction or archiving. Profiles persist until a human runs `hermes profile delete`.
- No dispatcher-side LLM execution. The dispatcher continues to be a pure scheduler.
- No changes to `skills/devops/kanban-orchestrator/SKILL.md`. The team-builder workflow is a separate, opt-in skill.
- No new CLI subcommands in this PR. `hermes kanban team-status` and friends are deferred to a follow-up if usage demands it.
