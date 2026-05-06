"""Kanban tools — structured tool-call surface for worker + orchestrator agents.

These tools are only registered into the model's schema when the agent is
running under the dispatcher (env var ``HERMES_KANBAN_TASK`` set). A
normal ``hermes chat`` session sees **zero** kanban tools in its schema.

Why tools instead of just shelling out to ``hermes kanban``?

1. **Backend portability.** A worker whose terminal tool points at Docker
   / Modal / Singularity / SSH would run ``hermes kanban complete …``
   inside the container, where ``hermes`` isn't installed and the DB
   isn't mounted. Tools run in the agent's Python process, so they
   always reach ``~/.hermes/kanban.db`` regardless of terminal backend.

2. **No shell-quoting footguns.** Passing ``--metadata '{"x": [...]}'``
   through shlex+argparse is fragile. Structured tool args skip it.

3. **Better errors.** Tool-call failures return structured JSON the
   model can reason about, not stderr strings it has to parse.

Humans continue to use the CLI (``hermes kanban …``), the dashboard
(``hermes dashboard``), and the slash command (``/kanban …``) — all
three bypass the agent entirely. The tools are ONLY for the worker
agent's handoff back to the kernel.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def _check_kanban_mode() -> bool:
    """Tools are available when:

    1. ``HERMES_KANBAN_TASK`` is set (dispatcher-spawned worker), OR
    2. The current profile has ``kanban`` in its toolsets config
       (orchestrator profiles like techlead that route work via Kanban).

    Humans running ``hermes chat`` without the kanban toolset see zero
    kanban tools. Workers spawned by the kanban dispatcher (gateway-
    embedded by default) and orchestrator profiles with the kanban
    toolset enabled see all seven.
    """
    if os.environ.get("HERMES_KANBAN_TASK"):
        return True

    # Check if the current profile has the kanban toolset enabled.
    # Uses load_config() which has mtime-based caching, so this adds
    # negligible overhead. The check_fn results are further TTL-cached
    # (~30s) by the tool registry.
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        toolsets = cfg.get("toolsets", [])
        return "kanban" in toolsets
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _default_task_id(arg: Optional[str]) -> Optional[str]:
    """Resolve ``task_id`` arg or fall back to the env var the dispatcher set."""
    if arg:
        return arg
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    return env_tid or None


def _worker_run_id(task_id: str) -> Optional[int]:
    """Return this worker's dispatcher run id when it is scoped to task_id."""
    if os.environ.get("HERMES_KANBAN_TASK") != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _enforce_worker_task_ownership(tid: str) -> Optional[str]:
    """Reject worker-driven destructive calls on foreign task IDs.

    A process spawned by the dispatcher has ``HERMES_KANBAN_TASK`` set
    to its own task id. Tools like ``kanban_complete`` / ``kanban_block``
    / ``kanban_heartbeat`` mutate run-lifecycle state, so a buggy or
    prompt-injected worker that passed an explicit ``task_id`` for some
    other task could corrupt sibling or cross-tenant runs (see #19534).

    Orchestrator profiles (kanban toolset enabled but **no**
    ``HERMES_KANBAN_TASK`` in env) aren't subject to this check — their
    job is routing, and they sometimes legitimately close out child
    tasks or reopen blocked ones. Workers are narrowly scoped to their
    one task.

    Returns ``None`` when the call is allowed, or a tool-error string
    when it must be rejected. Callers should ``return`` the error
    verbatim.
    """
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if not env_tid:
        # Orchestrator or CLI context — no task-scope restriction.
        return None
    if tid != env_tid:
        return tool_error(
            f"worker is scoped to task {env_tid}; refusing to mutate "
            f"{tid}. Use kanban_comment to hand off information to other "
            f"tasks, or kanban_create to spawn follow-up work."
        )
    return None


def _connect():
    """Import + connect lazily so the module imports cleanly in non-kanban
    contexts (e.g. test rigs that import every tool module)."""
    from hermes_cli import kanban_db as kb
    return kb, kb.connect()


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields})


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _handle_show(args: dict, **kw) -> str:
    """Read a task's full state: task row, parents, children, comments,
    runs (attempt history), and the last N events."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    try:
        kb, conn = _connect()
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return tool_error(f"task {tid} not found")
            comments = kb.list_comments(conn, tid)
            events = kb.list_events(conn, tid)
            runs = kb.list_runs(conn, tid)
            parents = kb.parent_ids(conn, tid)
            children = kb.child_ids(conn, tid)

            def _task_dict(t):
                return {
                    "id": t.id, "title": t.title, "body": t.body,
                    "assignee": t.assignee, "status": t.status,
                    "tenant": t.tenant, "priority": t.priority,
                    "workspace_kind": t.workspace_kind,
                    "workspace_path": t.workspace_path,
                    "created_by": t.created_by, "created_at": t.created_at,
                    "started_at": t.started_at,
                    "completed_at": t.completed_at,
                    "result": t.result,
                    "current_run_id": t.current_run_id,
                }

            def _run_dict(r):
                return {
                    "id": r.id, "profile": r.profile,
                    "status": r.status, "outcome": r.outcome,
                    "summary": r.summary, "error": r.error,
                    "metadata": r.metadata,
                    "started_at": r.started_at, "ended_at": r.ended_at,
                }

            return json.dumps({
                "task": _task_dict(task),
                "parents": parents,
                "children": children,
                "comments": [
                    {"author": c.author, "body": c.body,
                     "created_at": c.created_at}
                    for c in comments
                ],
                "events": [
                    {"kind": e.kind, "payload": e.payload,
                     "created_at": e.created_at, "run_id": e.run_id}
                    for e in events[-50:]   # cap; full log via CLI
                ],
                "runs": [_run_dict(r) for r in runs],
                # Also surface the worker's own context block so the
                # agent can include it directly if it wants. This is
                # the same string build_worker_context returns to the
                # dispatcher at spawn time.
                "worker_context": kb.build_worker_context(conn, tid),
            })
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_show failed")
        return tool_error(f"kanban_show: {e}")


def _handle_complete(args: dict, **kw) -> str:
    """Mark the current task done with a structured handoff."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    summary = args.get("summary")
    metadata = args.get("metadata")
    result = args.get("result")
    created_cards = args.get("created_cards")
    if created_cards is not None:
        if isinstance(created_cards, str):
            # Accept a single id as a string for convenience.
            created_cards = [created_cards]
        if not isinstance(created_cards, (list, tuple)):
            return tool_error(
                f"created_cards must be a list of task ids, got "
                f"{type(created_cards).__name__}"
            )
        # Normalise: strings only, stripped, non-empty.
        created_cards = [
            str(c).strip() for c in created_cards if str(c).strip()
        ]
    if not (summary or result):
        return tool_error(
            "provide at least one of: summary (preferred), result"
        )
    if metadata is not None and not isinstance(metadata, dict):
        return tool_error(
            f"metadata must be an object/dict, got {type(metadata).__name__}"
        )
    try:
        kb, conn = _connect()
        try:
            try:
                ok = kb.complete_task(
                    conn, tid,
                    result=result, summary=summary, metadata=metadata,
                    created_cards=created_cards,
                    expected_run_id=_worker_run_id(tid),
                )
            except kb.HallucinatedCardsError as hall_err:
                # Structured rejection — surface the phantom ids so the
                # worker can retry with a corrected list or drop the
                # field. Audit event already landed in the DB.
                return tool_error(
                    f"kanban_complete blocked: the following created_cards "
                    f"do not exist or were not created by this worker: "
                    f"{', '.join(hall_err.phantom)}. "
                    f"Either omit them, use only ids returned from successful "
                    f"kanban_create calls, or remove the created_cards field."
                )
            if not ok:
                return tool_error(
                    f"could not complete {tid} (unknown id or already terminal)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_complete failed")
        return tool_error(f"kanban_complete: {e}")


def _handle_block(args: dict, **kw) -> str:
    """Transition the task to blocked with a reason a human will read."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    reason = args.get("reason")
    if not reason or not str(reason).strip():
        return tool_error("reason is required — explain what input you need")
    try:
        kb, conn = _connect()
        try:
            ok = kb.block_task(
                conn, tid,
                reason=reason,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not block {tid} (unknown id or not in "
                    f"running/ready)"
                )
            run = kb.latest_run(conn, tid)
            return _ok(task_id=tid, run_id=run.id if run else None)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_block failed")
        return tool_error(f"kanban_block: {e}")


def _handle_heartbeat(args: dict, **kw) -> str:
    """Signal that the worker is still alive during a long operation."""
    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error(
            "task_id is required (or set HERMES_KANBAN_TASK in the env)"
        )
    ownership_err = _enforce_worker_task_ownership(tid)
    if ownership_err:
        return ownership_err
    note = args.get("note")
    try:
        kb, conn = _connect()
        try:
            ok = kb.heartbeat_worker(
                conn,
                tid,
                note=note,
                expected_run_id=_worker_run_id(tid),
            )
            if not ok:
                return tool_error(
                    f"could not heartbeat {tid} (unknown id or not running)"
                )
            return _ok(task_id=tid)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_heartbeat failed")
        return tool_error(f"kanban_heartbeat: {e}")


def _handle_comment(args: dict, **kw) -> str:
    """Append a comment to a task's thread."""
    tid = args.get("task_id")
    if not tid:
        return tool_error(
            "task_id is required (use the current task id if that's what "
            "you mean — pulls from env but kept explicit here)"
        )
    body = args.get("body")
    if not body or not str(body).strip():
        return tool_error("body is required")
    author = args.get("author") or os.environ.get("HERMES_PROFILE") or "worker"
    try:
        kb, conn = _connect()
        try:
            cid = kb.add_comment(conn, tid, author=author, body=str(body))
            return _ok(task_id=tid, comment_id=cid)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_comment failed")
        return tool_error(f"kanban_comment: {e}")


def _handle_create(args: dict, **kw) -> str:
    """Create a child task. Orchestrator workers use this to fan out.

    ``parents`` can be a list of task ids; dependency-gated promotion
    works as usual.
    """
    title = args.get("title")
    if not title or not str(title).strip():
        return tool_error("title is required")
    assignee = args.get("assignee")
    if not assignee:
        return tool_error(
            "assignee is required — name the profile that should execute this "
            "task (the dispatcher will only spawn tasks with an assignee)"
        )
    body = args.get("body")
    parents = args.get("parents") or []
    tenant = args.get("tenant") or os.environ.get("HERMES_TENANT")
    priority = args.get("priority")
    workspace_kind = args.get("workspace_kind") or "scratch"
    workspace_path = args.get("workspace_path")
    triage = bool(args.get("triage"))
    idempotency_key = args.get("idempotency_key")
    max_runtime_seconds = args.get("max_runtime_seconds")
    skills = args.get("skills")
    if isinstance(skills, str):
        # Accept a single skill name as a string for convenience.
        skills = [skills]
    if skills is not None and not isinstance(skills, (list, tuple)):
        return tool_error(
            f"skills must be a list of skill names, got {type(skills).__name__}"
        )
    if isinstance(parents, str):
        parents = [parents]
    if not isinstance(parents, (list, tuple)):
        return tool_error(
            f"parents must be a list of task ids, got {type(parents).__name__}"
        )
    try:
        kb, conn = _connect()
        try:
            new_tid = kb.create_task(
                conn,
                title=str(title).strip(),
                body=body,
                assignee=str(assignee),
                parents=tuple(parents),
                tenant=tenant,
                priority=int(priority) if priority is not None else 0,
                workspace_kind=str(workspace_kind),
                workspace_path=workspace_path,
                triage=triage,
                idempotency_key=idempotency_key,
                max_runtime_seconds=(
                    int(max_runtime_seconds)
                    if max_runtime_seconds is not None else None
                ),
                skills=skills,
                created_by=os.environ.get("HERMES_PROFILE") or "worker",
            )
            new_task = kb.get_task(conn, new_tid)
            return _ok(
                task_id=new_tid,
                status=new_task.status if new_task else None,
            )
        finally:
            conn.close()
    except Exception as e:
        logger.exception("kanban_create failed")
        return tool_error(f"kanban_create: {e}")


def _handle_link(args: dict, **kw) -> str:
    """Add a parent→child dependency edge after the fact."""
    parent_id = args.get("parent_id")
    child_id = args.get("child_id")
    if not parent_id or not child_id:
        return tool_error("both parent_id and child_id are required")
    try:
        kb, conn = _connect()
        try:
            kb.link_tasks(conn, parent_id=parent_id, child_id=child_id)
            return _ok(parent_id=parent_id, child_id=child_id)
        finally:
            conn.close()
    except ValueError as e:
        # Covers cycle + self-parent rejections
        return tool_error(f"kanban_link: {e}")
    except Exception as e:
        logger.exception("kanban_link failed")
        return tool_error(f"kanban_link: {e}")


# ---------------------------------------------------------------------------
# Auto-team profile provisioning handlers
# ---------------------------------------------------------------------------

def _read_profile_config(profile_dir) -> dict:
    """Read a profile's ``config.yaml`` lazily; return ``{}`` on any error.

    Used by ``kanban_list_profiles`` to surface role / description /
    toolsets / skills metadata so the team-builder LLM can decide
    reuse vs create without a second tool call per profile.
    """
    cfg_path = profile_dir / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
        with open(cfg_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _handle_list_profiles(args: dict, **kw) -> str:
    """Return the live profile roster + per-profile task counts.

    Used by the team-builder skill before deciding whether to propose
    a new profile or reuse an existing one. The response is intentionally
    structured so the LLM can scan roles in O(N): each entry exposes
    ``{name, role, description, skills, toolsets, model, task_counts}``.
    """
    try:
        from hermes_cli.profiles import list_profiles
    except Exception as e:
        return tool_error(f"kanban_list_profiles: {e}")
    try:
        kb, conn = _connect()
    except Exception as e:
        return tool_error(f"kanban_list_profiles: {e}")
    try:
        # Per-profile open / running / done counts in a single pass.
        counts: dict[str, dict[str, int]] = {}
        cur = conn.execute(
            "SELECT assignee, status, COUNT(*) AS n "
            "FROM tasks WHERE assignee IS NOT NULL "
            "GROUP BY assignee, status"
        )
        for row in cur:
            bucket = counts.setdefault(row["assignee"], {"open": 0, "running": 0, "done": 0})
            st = row["status"]
            if st == "running":
                bucket["running"] += int(row["n"])
            elif st in ("done", "archived"):
                bucket["done"] += int(row["n"])
            else:
                bucket["open"] += int(row["n"])

        profiles = []
        for p in list_profiles():
            cfg = _read_profile_config(p.path)
            entry = {
                "name": p.name,
                "is_default": p.is_default,
                "model": p.model,
                "provider": p.provider,
                # Number of installed skills under <profile>/skills/.
                # The team-builder skill keys reuse-vs-create on roster
                # similarity, not on this number; it's surfaced for
                # operator dashboards.
                "skill_count": p.skill_count,
                "role": cfg.get("role"),
                "description": cfg.get("description") or cfg.get("about"),
                "toolsets": cfg.get("toolsets") or [],
                "task_counts": counts.get(p.name, {"open": 0, "running": 0, "done": 0}),
            }
            profiles.append(entry)
        return _ok(profiles=profiles)
    except Exception as e:
        logger.exception("kanban_list_profiles failed")
        return tool_error(f"kanban_list_profiles: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _handle_propose_profile(args: dict, **kw) -> str:
    """Validate a proposed profile design and write a ``profile.proposed`` event.

    Performs the hard checks (name validity, alias collision, duplicate
    profile) but does NOT create anything on disk. The returned
    ``proposal_event_id`` is what ``kanban_provision_profile`` (or the
    dashboard approve endpoint) keys off when the operator chooses to
    materialize the proposal. ``requires_human_approval`` reflects the
    current dashboard ``profile_provisioning`` mode so the LLM knows
    whether to call ``kanban_provision_profile`` itself or wait.
    """
    name = (args.get("name") or "").strip()
    role = (args.get("role") or "").strip()
    description = (args.get("description") or "").strip()
    base = (args.get("base") or "").strip() or None
    skills = args.get("skills") or []
    toolsets = args.get("toolsets") or []
    model = (args.get("model") or "").strip() or None
    if not name:
        return tool_error("name is required")
    if not role:
        return tool_error("role is required")
    if not description:
        return tool_error("description is required (1-3 sentences)")

    try:
        from hermes_cli.profiles import (
            normalize_profile_name, validate_profile_name,
            profile_exists, check_alias_collision,
        )
    except Exception as e:
        return tool_error(f"kanban_propose_profile: {e}")

    canon = normalize_profile_name(name)
    conflicts: list[dict] = []
    try:
        validate_profile_name(canon)
    except ValueError as e:
        conflicts.append({"kind": "invalid_name", "detail": str(e)})
    if profile_exists(canon):
        conflicts.append({
            "kind": "profile_exists",
            "detail": f"profile '{canon}' already exists; reuse instead of proposing.",
        })
    alias_msg = check_alias_collision(canon)
    if alias_msg:
        conflicts.append({"kind": "alias_collision", "detail": alias_msg})

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")

    try:
        kb, conn = _connect()
    except Exception as e:
        return tool_error(f"kanban_propose_profile: {e}")
    try:
        mode = kb.read_profile_provisioning_setting()
        payload = {
            "name": canon,
            "role": role,
            "description": description,
            "base": base,
            "skills": list(skills),
            "toolsets": list(toolsets),
            "model": model,
            "conflicts": conflicts,
            "ok": not conflicts,
        }
        run_id = _worker_run_id(tid)
        event_id = kb.append_event(conn, tid, "profile.proposed", payload, run_id=run_id)
        return _ok(
            proposal_event_id=int(event_id),
            ok=not conflicts,
            conflicts=conflicts,
            requires_human_approval=(mode == kb.PROVISIONING_MANUAL),
            mode=mode,
        )
    except Exception as e:
        logger.exception("kanban_propose_profile failed")
        return tool_error(f"kanban_propose_profile: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _handle_provision_profile(args: dict, **kw) -> str:
    """Materialize a previously-proposed profile, link it to the parent task.

    Hard-fuses on ``profile_provisioning='off'`` so a hallucinating
    worker LLM cannot create profiles even if it manages to reach this
    handler. In ``manual`` mode requires that the dashboard has written
    a ``profile.approved`` event for the target proposal — the team-
    builder skill handles this by waiting for the approval event before
    calling provision.

    Calls ``hermes_cli.profiles.create_profile`` with ``clone_config=True``
    so the new profile inherits config.yaml / SOUL.md / installed skills
    from its base, then merges in any per-proposal skill / toolset
    overrides, then records the entry on the parent task's
    ``created_profiles`` JSON column.
    """
    proposal_event_id = args.get("proposal_event_id")
    inline = args.get("inline") or {}

    tid = _default_task_id(args.get("task_id"))
    if not tid:
        return tool_error("task_id is required (or set HERMES_KANBAN_TASK in the env)")

    try:
        kb, conn = _connect()
    except Exception as e:
        return tool_error(f"kanban_provision_profile: {e}")

    try:
        mode = kb.read_profile_provisioning_setting()
        if mode == kb.PROVISIONING_OFF:
            return tool_error(
                "profile_provisioning is 'off'; refusing to provision. "
                "An operator must enable manual or auto mode in the "
                "kanban dashboard before profiles can be auto-created."
            )

        proposal_payload: dict
        if proposal_event_id is not None:
            row = conn.execute(
                "SELECT payload, kind FROM task_events WHERE id = ? AND task_id = ?",
                (int(proposal_event_id), tid),
            ).fetchone()
            if not row:
                return tool_error(
                    f"proposal_event_id {proposal_event_id} not found on task {tid}"
                )
            if row["kind"] != "profile.proposed":
                return tool_error(
                    f"event {proposal_event_id} is not a profile.proposed event"
                )
            try:
                proposal_payload = json.loads(row["payload"]) if row["payload"] else {}
            except Exception:
                return tool_error("proposal payload is not valid JSON")
            if not proposal_payload.get("ok", False):
                conflicts = proposal_payload.get("conflicts") or []
                return tool_error(
                    f"proposal {proposal_event_id} has unresolved conflicts: {conflicts}"
                )
            # Manual mode requires an explicit approval event written by the
            # dashboard (the human in the loop). Auto mode skips this check.
            if mode == kb.PROVISIONING_MANUAL:
                approval = conn.execute(
                    "SELECT id FROM task_events WHERE task_id = ? "
                    "AND kind = 'profile.approved' AND payload LIKE ? "
                    "ORDER BY id DESC LIMIT 1",
                    (tid, f'%"proposal_event_id": {int(proposal_event_id)}%'),
                ).fetchone()
                if not approval:
                    return tool_error(
                        "manual mode requires dashboard approval; the "
                        "proposal has not been approved yet."
                    )
        else:
            # No prior proposal — synthesize one from inline args. Useful
            # for tests + auto mode where the propose/provision pair are
            # called back-to-back. We still write the proposal event for
            # observability before provisioning.
            proposal_payload = {
                "name": (inline.get("name") or "").strip(),
                "role": (inline.get("role") or "").strip(),
                "description": (inline.get("description") or "").strip(),
                "base": (inline.get("base") or "").strip() or None,
                "skills": inline.get("skills") or [],
                "toolsets": inline.get("toolsets") or [],
                "model": (inline.get("model") or "").strip() or None,
            }
            if not proposal_payload["name"]:
                return tool_error("inline.name is required when no proposal_event_id")

        from hermes_cli.profiles import (
            normalize_profile_name, validate_profile_name,
            profile_exists, create_profile,
            seed_profile_skills,
        )
        canon = normalize_profile_name(proposal_payload["name"])
        validate_profile_name(canon)
        if profile_exists(canon):
            return tool_error(
                f"profile '{canon}' already exists; reuse instead of provisioning."
            )

        base = proposal_payload.get("base") or "default"
        try:
            profile_path = create_profile(
                canon,
                clone_from=base,
                clone_config=True,
            )
        except FileExistsError:
            return tool_error(f"profile '{canon}' already exists (race?)")
        except (FileNotFoundError, ValueError) as e:
            return tool_error(f"create_profile: {e}")

        # Best-effort skill seed; failures here don't unwind the create
        # because the profile directory is usable without it (skills can
        # be installed later by the operator).
        try:
            seed_profile_skills(profile_path, quiet=True)
        except Exception:
            logger.exception("seed_profile_skills failed for %s", canon)

        # Merge any per-proposal skill / toolset overrides into the new
        # profile's config.yaml. Only writes when changes are needed so
        # we don't reformat the inherited file unnecessarily.
        _apply_proposal_overrides(profile_path, proposal_payload)

        run_id = _worker_run_id(tid)
        kb.record_created_profile(
            conn, tid,
            profile_name=canon,
            role=proposal_payload.get("role"),
            base=base,
            run_id=run_id,
        )
        kb.append_event(
            conn, tid, "profile.created",
            {
                "name": canon,
                "role": proposal_payload.get("role"),
                "base": base,
                "profile_path": str(profile_path),
                "proposal_event_id": (
                    int(proposal_event_id) if proposal_event_id is not None else None
                ),
            },
            run_id=run_id,
        )
        return _ok(
            profile_name=canon,
            profile_path=str(profile_path),
            base=base,
        )
    except Exception as e:
        logger.exception("kanban_provision_profile failed")
        return tool_error(f"kanban_provision_profile: {e}")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _apply_proposal_overrides(profile_path, proposal: dict) -> None:
    """Merge proposal overrides (toolsets, model, role, description) into
    the freshly-cloned profile's ``config.yaml``.

    NOTE: ``skills`` from the proposal are NOT written to ``config.yaml`` —
    Hermes does not have a profile-level "always load these skills" config
    key. Preloaded skills are passed via the ``--skills`` CLI flag, which
    in the kanban path comes from the ``tasks.skills`` JSON column
    (see ``_default_spawn`` in ``hermes_cli/kanban_db.py``). When
    creating child tasks for the new profile, the team-builder calls
    ``kanban_create(skills=[...])`` to ship the per-task skill set.
    The proposal's ``skills`` list is therefore advisory metadata —
    surfaced via the proposal event payload — not a runtime config.

    Conservative: only updates keys present in the proposal, and only
    when their value differs from the cloned value.
    """
    cfg_path = profile_path / "config.yaml"
    try:
        import yaml
        existing = {}
        if cfg_path.exists():
            with open(cfg_path, "r") as f:
                existing = yaml.safe_load(f) or {}
        changed = False
        # Surface role + description on the profile so kanban_list_profiles
        # can return them to future team-builder runs.
        for key in ("role", "description"):
            val = proposal.get(key)
            if val and existing.get(key) != val:
                existing[key] = val
                changed = True
        toolsets_override = proposal.get("toolsets") or []
        if toolsets_override:
            current_ts = existing.get("toolsets") or []
            merged_ts = list(dict.fromkeys(list(current_ts) + list(toolsets_override)))
            if merged_ts != current_ts:
                existing["toolsets"] = merged_ts
                changed = True
        model = proposal.get("model")
        if model and existing.get("model") != model:
            existing["model"] = model
            changed = True
        if changed:
            with open(cfg_path, "w") as f:
                yaml.safe_dump(existing, f, sort_keys=False)
    except Exception:
        logger.exception("override merge failed for %s", profile_path)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

_DESC_TASK_ID_DEFAULT = (
    "Task id. If omitted, defaults to HERMES_KANBAN_TASK from the env "
    "(the task the dispatcher spawned you to work on)."
)

KANBAN_SHOW_SCHEMA = {
    "name": "kanban_show",
    "description": (
        "Read a task's full state — title, body, assignee, parent task "
        "handoffs, your prior attempts on this task if any, comments, "
        "and recent events. Use this to (re)orient yourself before "
        "starting work, especially on retries. The response includes a "
        "pre-formatted ``worker_context`` string suitable for inclusion "
        "verbatim in your reasoning."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
        },
        "required": [],
    },
}

KANBAN_COMPLETE_SCHEMA = {
    "name": "kanban_complete",
    "description": (
        "Mark your current task done with a structured handoff for "
        "downstream workers and humans. Prefer ``summary`` for a "
        "human-readable 1-3 sentence description of what you did; put "
        "machine-readable facts in ``metadata`` (changed_files, "
        "tests_run, decisions, findings, etc). At least one of "
        "``summary`` or ``result`` is required. If you created new "
        "tasks via ``kanban_create`` during this run, list their ids "
        "in ``created_cards`` — the kernel verifies them so phantom "
        "references are caught before they leak into downstream "
        "automation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "summary": {
                "type": "string",
                "description": (
                    "Human-readable handoff, 1-3 sentences. Appears in "
                    "Run History on the dashboard and in downstream "
                    "workers' context."
                ),
            },
            "metadata": {
                "type": "object",
                "description": (
                    "Free-form dict of structured facts about this "
                    "attempt — {\"changed_files\": [...], \"tests_run\": 12, "
                    "\"findings\": [...]}. Surfaced to downstream "
                    "workers alongside ``summary``."
                ),
            },
            "result": {
                "type": "string",
                "description": (
                    "Short result log line (legacy field, maps to "
                    "task.result). Use ``summary`` instead when "
                    "possible; this exists for compatibility with "
                    "callers that still set --result on the CLI."
                ),
            },
            "created_cards": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional structured manifest of task ids you "
                    "created via ``kanban_create`` during this run. "
                    "The kernel verifies each id exists and was "
                    "created by this worker's profile; any phantom "
                    "id blocks the completion with an error listing "
                    "what went wrong (auditable in the task's events). "
                    "Only list ids you got back from a successful "
                    "``kanban_create`` call — do not invent or "
                    "remember ids from prose. Omit the field if you "
                    "did not create any cards."
                ),
            },
        },
        "required": [],
    },
}

KANBAN_BLOCK_SCHEMA = {
    "name": "kanban_block",
    "description": (
        "Transition the task to blocked because you need human input "
        "to proceed. ``reason`` will be shown to the human on the "
        "board and included in context when someone unblocks you. "
        "Use for genuine blockers only — don't block on things you can "
        "resolve yourself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "reason": {
                "type": "string",
                "description": (
                    "What you need answered, in one or two sentences. "
                    "Don't paste the whole conversation; the human has "
                    "the board and can ask follow-ups via comments."
                ),
            },
        },
        "required": ["reason"],
    },
}

KANBAN_HEARTBEAT_SCHEMA = {
    "name": "kanban_heartbeat",
    "description": (
        "Signal that you're still alive during a long operation "
        "(training, encoding, large crawls). Call every few minutes so "
        "humans see liveness separately from PID checks. Pure side "
        "effect — no work changes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "note": {
                "type": "string",
                "description": (
                    "Optional short note describing current progress. "
                    "Shown in the event log."
                ),
            },
        },
        "required": [],
    },
}

KANBAN_COMMENT_SCHEMA = {
    "name": "kanban_comment",
    "description": (
        "Append a comment to a task's thread. Use for durable notes "
        "that should outlive this run (questions for the next worker, "
        "partial findings, rationale). Ephemeral reasoning doesn't "
        "belong here — use your normal response instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task id. Required (may be your own task or "
                    "another's — comment threads are per-task)."
                ),
            },
            "body": {
                "type": "string",
                "description": "Markdown-supported comment body.",
            },
            "author": {
                "type": "string",
                "description": (
                    "Override author name. Defaults to the current "
                    "profile (HERMES_PROFILE env)."
                ),
            },
        },
        "required": ["task_id", "body"],
    },
}

KANBAN_CREATE_SCHEMA = {
    "name": "kanban_create",
    "description": (
        "Create a new kanban task, optionally as a child of the current "
        "one (pass the current task id in ``parents``). Used by "
        "orchestrator workers to fan out — decompose work into child "
        "tasks with specific assignees, link them into a pipeline, "
        "then complete your own task. The dispatcher picks up the new "
        "tasks on its next tick and spawns the assigned profiles."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short task title (required).",
            },
            "assignee": {
                "type": "string",
                "description": (
                    "Profile name that should execute this task "
                    "(e.g. 'researcher-a', 'reviewer', 'writer'). "
                    "Required — tasks without an assignee are never "
                    "dispatched."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Opening post: full spec, acceptance criteria, "
                    "links. The assigned worker reads this as part of "
                    "its context."
                ),
            },
            "parents": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Parent task ids. The new task stays in 'todo' "
                    "until every parent reaches 'done'; then it "
                    "auto-promotes to 'ready'. Typical fan-in: list "
                    "all the researcher task ids when creating a "
                    "synthesizer task."
                ),
            },
            "tenant": {
                "type": "string",
                "description": (
                    "Optional namespace for multi-project isolation. "
                    "Defaults to HERMES_TENANT env if set."
                ),
            },
            "priority": {
                "type": "integer",
                "description": (
                    "Dispatcher tiebreaker. Higher = picked sooner "
                    "when multiple ready tasks share an assignee."
                ),
            },
            "workspace_kind": {
                "type": "string",
                "enum": ["scratch", "dir", "worktree"],
                "description": (
                    "Workspace flavor: 'scratch' (fresh tmp dir, "
                    "default), 'dir' (shared directory, requires "
                    "absolute workspace_path), 'worktree' (git worktree)."
                ),
            },
            "workspace_path": {
                "type": "string",
                "description": (
                    "Absolute path for 'dir' or 'worktree' workspace. "
                    "Relative paths are rejected at dispatch."
                ),
            },
            "triage": {
                "type": "boolean",
                "description": (
                    "If true, task lands in 'triage' instead of 'todo' "
                    "— a specifier profile is expected to flesh out "
                    "the body before work starts."
                ),
            },
            "idempotency_key": {
                "type": "string",
                "description": (
                    "If a non-archived task with this key already "
                    "exists, return that task's id instead of creating "
                    "a duplicate. Useful for retry-safe automation."
                ),
            },
            "max_runtime_seconds": {
                "type": "integer",
                "description": (
                    "Per-task runtime cap. When exceeded, the "
                    "dispatcher SIGTERMs the worker and re-queues the "
                    "task with outcome='timed_out'."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Skill names to force-load into the dispatched "
                    "worker (in addition to the built-in kanban-worker "
                    "skill). Use this to pin a task to a specialist "
                    "context — e.g. ['translation'] for a translation "
                    "task, ['github-code-review'] for a reviewer task. "
                    "The names must match skills installed on the "
                    "assignee's profile."
                ),
            },
        },
        "required": ["title", "assignee"],
    },
}

KANBAN_LINK_SCHEMA = {
    "name": "kanban_link",
    "description": (
        "Add a parent→child dependency edge after both tasks already "
        "exist. The child won't promote to 'ready' until all parents "
        "are 'done'. Cycles and self-links are rejected."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "parent_id": {"type": "string", "description": "Parent task id."},
            "child_id":  {"type": "string", "description": "Child task id."},
        },
        "required": ["parent_id", "child_id"],
    },
}

KANBAN_LIST_PROFILES_SCHEMA = {
    "name": "kanban_list_profiles",
    "description": (
        "Read the live profile roster — names, roles, descriptions, "
        "loaded skills, toolsets, models, plus per-profile open / "
        "running / done task counts. The team-builder skill calls "
        "this BEFORE deciding whether to propose a new profile, so "
        "the LLM can judge reuse vs. clone vs. create against the "
        "actual inventory rather than guessing."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

KANBAN_PROPOSE_PROFILE_SCHEMA = {
    "name": "kanban_propose_profile",
    "description": (
        "Validate a candidate profile design (name, role, description, "
        "base profile to clone from, skills, toolsets, model) and "
        "record a structured ``profile.proposed`` event on the current "
        "task. This does NOT create anything on disk; it just runs the "
        "hard checks (name validity, alias collision, duplicate "
        "profile) and persists the proposal so the dashboard can show "
        "it for human approval (manual mode) or so a follow-up "
        "``kanban_provision_profile`` call can materialize it (auto "
        "mode). Returns ``{ok, proposal_event_id, conflicts, "
        "requires_human_approval, mode}`` — when ``ok=false`` you must "
        "fix conflicts or pick a different name before proposing again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "name": {
                "type": "string",
                "description": (
                    "Lowercase profile identifier matching "
                    "[a-z0-9][a-z0-9_-]{0,63}. Should describe the role "
                    "(e.g. 'qa-engineer', 'frontend-dev') not the "
                    "person. Avoid one-shot names like 'task-123-helper'."
                ),
            },
            "role": {
                "type": "string",
                "description": (
                    "Short role title (e.g. 'Backend engineer', "
                    "'Translator'). Surfaced in dashboard lists and "
                    "future ``kanban_list_profiles`` responses."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "1-3 sentence description of the role's "
                    "responsibilities. Used by future runs of the "
                    "team-builder skill to judge whether this profile "
                    "is the right reuse candidate."
                ),
            },
            "base": {
                "type": "string",
                "description": (
                    "Source profile to clone from (defaults to "
                    "'default'). Use a more specialized base when one "
                    "exists — e.g. clone from 'backend-dev' to inherit "
                    "its toolsets when designing a 'backend-dev-react' "
                    "specialization."
                ),
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Advisory skill list captured on the proposal "
                    "payload. Hermes has no profile-level auto-load "
                    "config; pass these names to ``kanban_create`` "
                    "via its ``skills`` arg when fanning out child "
                    "tasks for the new profile so the dispatcher "
                    "loads them via ``--skills`` per spawn."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolset names to enable on the new profile "
                    "(additive over the base profile's toolsets)."
                ),
            },
            "model": {
                "type": "string",
                "description": (
                    "Model identifier override (e.g. "
                    "'claude-sonnet-4-6'). Omit to inherit the base "
                    "profile's model."
                ),
            },
        },
        "required": ["name", "role", "description"],
    },
}

KANBAN_PROVISION_PROFILE_SCHEMA = {
    "name": "kanban_provision_profile",
    "description": (
        "Materialize a previously-proposed profile (or an inline "
        "design) on disk and link it to the current task's "
        "``created_profiles`` audit trail. Hard-fuses on "
        "``profile_provisioning='off'`` — refuses to run regardless "
        "of arguments. In manual mode requires a corresponding "
        "dashboard ``profile.approved`` event before it will create "
        "the profile. In auto mode the team-builder skill calls this "
        "right after a successful ``kanban_propose_profile`` to "
        "complete the design-then-create cycle."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": _DESC_TASK_ID_DEFAULT,
            },
            "proposal_event_id": {
                "type": "integer",
                "description": (
                    "ID of the prior ``profile.proposed`` event for "
                    "this task. Required in manual mode (the dashboard "
                    "approve flow keys off this id)."
                ),
            },
            "inline": {
                "type": "object",
                "description": (
                    "Inline proposal payload for cases where "
                    "``kanban_propose_profile`` was not called first "
                    "(auto-mode shortcut, tests). Fields mirror "
                    "``kanban_propose_profile``: name (required), "
                    "role, description, base, skills, toolsets, model."
                ),
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "description": {"type": "string"},
                    "base": {"type": "string"},
                    "skills": {"type": "array", "items": {"type": "string"}},
                    "toolsets": {"type": "array", "items": {"type": "string"}},
                    "model": {"type": "string"},
                },
            },
        },
        "required": [],
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="kanban_show",
    toolset="kanban",
    schema=KANBAN_SHOW_SCHEMA,
    handler=_handle_show,
    check_fn=_check_kanban_mode,
    emoji="📋",
)

registry.register(
    name="kanban_complete",
    toolset="kanban",
    schema=KANBAN_COMPLETE_SCHEMA,
    handler=_handle_complete,
    check_fn=_check_kanban_mode,
    emoji="✔",
)

registry.register(
    name="kanban_block",
    toolset="kanban",
    schema=KANBAN_BLOCK_SCHEMA,
    handler=_handle_block,
    check_fn=_check_kanban_mode,
    emoji="⏸",
)

registry.register(
    name="kanban_heartbeat",
    toolset="kanban",
    schema=KANBAN_HEARTBEAT_SCHEMA,
    handler=_handle_heartbeat,
    check_fn=_check_kanban_mode,
    emoji="💓",
)

registry.register(
    name="kanban_comment",
    toolset="kanban",
    schema=KANBAN_COMMENT_SCHEMA,
    handler=_handle_comment,
    check_fn=_check_kanban_mode,
    emoji="💬",
)

registry.register(
    name="kanban_create",
    toolset="kanban",
    schema=KANBAN_CREATE_SCHEMA,
    handler=_handle_create,
    check_fn=_check_kanban_mode,
    emoji="➕",
)

registry.register(
    name="kanban_link",
    toolset="kanban",
    schema=KANBAN_LINK_SCHEMA,
    handler=_handle_link,
    check_fn=_check_kanban_mode,
    emoji="🔗",
)

registry.register(
    name="kanban_list_profiles",
    toolset="kanban",
    schema=KANBAN_LIST_PROFILES_SCHEMA,
    handler=_handle_list_profiles,
    check_fn=_check_kanban_mode,
    emoji="👥",
)

registry.register(
    name="kanban_propose_profile",
    toolset="kanban",
    schema=KANBAN_PROPOSE_PROFILE_SCHEMA,
    handler=_handle_propose_profile,
    check_fn=_check_kanban_mode,
    emoji="📝",
)

registry.register(
    name="kanban_provision_profile",
    toolset="kanban",
    schema=KANBAN_PROVISION_PROFILE_SCHEMA,
    handler=_handle_provision_profile,
    check_fn=_check_kanban_mode,
    emoji="🆕",
)
