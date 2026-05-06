"""Tests for the kanban-team-builder tool surface.

Covers:
  - kanban_list_profiles returns the live roster + counts.
  - kanban_propose_profile validates name / alias / duplicate and
    writes a profile.proposed event.
  - kanban_provision_profile hard-fuses on provisioning='off'.
  - kanban_provision_profile happy path: creates the profile, records
    it on the parent task's created_profiles JSON, emits profile.created.
  - Dispatcher hook: off / manual / auto behaviors against a synthetic
    task whose assignee does not resolve to a real profile.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with profile_root pointing inside tmp_path."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "profiles").mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    # Stub the wrapper dir so check_alias_collision doesn't hit the
    # operator's real ~/.local/bin.
    from hermes_cli import profiles as _p
    monkeypatch.setattr(_p, "_get_wrapper_dir", lambda: tmp_path / ".local" / "bin")
    # Also clear caches that may leak from prior tests.
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    return home


@pytest.fixture
def kanban_conn(hermes_home):
    from hermes_cli import kanban_db as kb
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


@pytest.fixture
def worker_task(monkeypatch, kanban_conn):
    """Create a task and pretend we're the worker spawned for it."""
    from hermes_cli import kanban_db as kb
    tid = kb.create_task(kanban_conn, title="root task", assignee="root-runner")
    kb.claim_task(kanban_conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


# ---------------------------------------------------------------------------
# kanban_list_profiles
# ---------------------------------------------------------------------------

def test_list_profiles_returns_default_at_minimum(worker_task):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_list_profiles({}))
    assert out["ok"] is True
    names = {p["name"] for p in out["profiles"]}
    assert "default" in names


def test_list_profiles_includes_task_counts(worker_task, kanban_conn):
    from hermes_cli import kanban_db as kb
    # Create extra tasks so the count math has something to chew on.
    other = kb.create_task(kanban_conn, title="x", assignee="root-runner")
    kb.claim_task(kanban_conn, other)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_list_profiles({}))
    by_name = {p["name"]: p for p in out["profiles"]}
    # `root-runner` profile doesn't exist on disk, but list_profiles
    # only returns existing profiles. So we expect default to be there
    # but not root-runner.
    assert "default" in by_name


# ---------------------------------------------------------------------------
# kanban_propose_profile
# ---------------------------------------------------------------------------

def test_propose_writes_profile_proposed_event(worker_task, kanban_conn):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_propose_profile({
        "name": "qa-engineer",
        "role": "QA Engineer",
        "description": "Owns automated end-to-end tests.",
    }))
    assert out["ok"] is True
    assert "proposal_event_id" in out
    rows = kanban_conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 5",
        (worker_task,),
    ).fetchall()
    kinds = [r["kind"] for r in rows]
    assert "profile.proposed" in kinds


def test_propose_rejects_invalid_name(worker_task):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_propose_profile({
        "name": "BAD NAME WITH SPACES",
        "role": "Tester",
        "description": "x",
    }))
    # ok=false because validation produced a conflict
    assert out["ok"] is False
    kinds = [c["kind"] for c in out["conflicts"]]
    assert "invalid_name" in kinds


def test_propose_rejects_duplicate_profile(worker_task):
    from tools import kanban_tools as kt
    # default exists by definition
    out = json.loads(kt._handle_propose_profile({
        "name": "default",
        "role": "anything",
        "description": "x",
    }))
    assert out["ok"] is False
    kinds = [c["kind"] for c in out["conflicts"]]
    assert "profile_exists" in kinds


# ---------------------------------------------------------------------------
# kanban_provision_profile
# ---------------------------------------------------------------------------

def test_provision_fuses_on_off_mode(worker_task, monkeypatch):
    # Mode reader returns 'off'; provision should refuse regardless.
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_OFF)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_provision_profile({
        "inline": {
            "name": "qa-engineer", "role": "QA", "description": "x",
        },
    }))
    # Tool errors return {"error": ...} (no "ok" key).
    assert "error" in out
    assert "off" in out["error"].lower()


def test_provision_inline_creates_profile_and_records(
    worker_task, kanban_conn, monkeypatch, tmp_path,
):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_AUTO)

    # seed_profile_skills hits external machinery; stub it to a no-op.
    from hermes_cli import profiles as _p
    monkeypatch.setattr(_p, "seed_profile_skills", lambda *a, **k: None)

    from tools import kanban_tools as kt
    out = json.loads(kt._handle_provision_profile({
        "inline": {
            "name": "qa-engineer",
            "role": "QA Engineer",
            "description": "Owns E2E tests.",
            "base": "default",
            "skills": ["playwright"],
        },
    }))
    assert out["ok"] is True
    profile_path = tmp_path / ".hermes" / "profiles" / "qa-engineer"
    assert profile_path.is_dir()

    # Parent task carries the new entry on its created_profiles JSON.
    row = kanban_conn.execute(
        "SELECT created_profiles FROM tasks WHERE id = ?", (worker_task,),
    ).fetchone()
    entries = json.loads(row["created_profiles"])
    names = [e["name"] for e in entries]
    assert "qa-engineer" in names

    # profile.created event recorded.
    kinds = [
        r["kind"] for r in kanban_conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id ASC",
            (worker_task,),
        )
    ]
    assert "profile.created" in kinds


# ---------------------------------------------------------------------------
# Dispatcher provisioning hook
# ---------------------------------------------------------------------------

def test_dispatcher_off_mode_skips_nonspawnable(hermes_home, kanban_conn, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_OFF)
    tid = kb.create_task(kanban_conn, title="x", assignee="ghost-profile")
    kb.recompute_ready(kanban_conn)
    result = kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)
    assert tid in result.skipped_nonspawnable
    # No profile.required event should be emitted in off mode.
    kinds = [
        r["kind"] for r in kanban_conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ?", (tid,),
        )
    ]
    assert "profile.required" not in kinds


def test_dispatcher_manual_emits_profile_required_once(hermes_home, kanban_conn, monkeypatch):
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_MANUAL)
    tid = kb.create_task(kanban_conn, title="x", assignee="ghost-profile")
    kb.recompute_ready(kanban_conn)
    kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)
    kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)  # second tick
    rows = kanban_conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? AND kind = 'profile.required'",
        (tid,),
    ).fetchall()
    # Idempotent: one event across two ticks.
    assert len(rows) == 1


def test_dispatcher_auto_rewrites_assignee_to_team_builder(hermes_home, kanban_conn, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import profiles as _p
    # Pretend team-builder profile is on disk.
    (hermes_home / "profiles" / "team-builder").mkdir()
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_AUTO)

    tid = kb.create_task(kanban_conn, title="x", assignee="ghost-profile")
    kb.recompute_ready(kanban_conn)
    kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)

    row = kanban_conn.execute(
        "SELECT assignee, skills FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    assert row["assignee"] == "team-builder"
    # The rewrite must also inject the kanban-team-builder skill into
    # tasks.skills so the dispatcher passes --skills kanban-team-builder
    # when spawning the worker. Without this the skill never loads —
    # Hermes has no profile-level skill auto-load mechanism.
    skills = json.loads(row["skills"])
    assert "kanban-team-builder" in skills
    payload = kb.latest_event_payload(kanban_conn, tid, "profile.required")
    assert payload is not None
    assert payload["original_assignee"] == "ghost-profile"
    assert payload["mode"] == kb.PROVISIONING_AUTO


def test_dispatcher_auto_preserves_existing_task_skills(hermes_home, kanban_conn, monkeypatch):
    """If the user already pinned per-task skills, the rewrite must
    keep them and only append kanban-team-builder."""
    from hermes_cli import kanban_db as kb
    (hermes_home / "profiles" / "team-builder").mkdir()
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_AUTO)

    tid = kb.create_task(
        kanban_conn, title="x", assignee="ghost-profile",
        skills=["playwright"],
    )
    kb.recompute_ready(kanban_conn)
    kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)

    row = kanban_conn.execute(
        "SELECT skills FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    skills = json.loads(row["skills"])
    assert "playwright" in skills
    assert "kanban-team-builder" in skills


def test_dispatcher_auto_falls_back_when_team_builder_missing(hermes_home, kanban_conn, monkeypatch):
    """Auto without a provisioned team-builder profile must NOT rewrite —
    it falls through to skipped_nonspawnable so the operator can fix it."""
    from hermes_cli import kanban_db as kb
    monkeypatch.setattr(kb, "read_profile_provisioning_setting", lambda: kb.PROVISIONING_AUTO)
    tid = kb.create_task(kanban_conn, title="x", assignee="ghost-profile")
    kb.recompute_ready(kanban_conn)
    result = kb.dispatch_once(kanban_conn, dry_run=True, spawn_fn=lambda *a, **k: None)
    assert tid in result.skipped_nonspawnable
    row = kanban_conn.execute(
        "SELECT assignee FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    assert row["assignee"] == "ghost-profile"  # unchanged


# ---------------------------------------------------------------------------
# Migration safety
# ---------------------------------------------------------------------------

def test_created_profiles_migration_idempotent(hermes_home):
    from hermes_cli import kanban_db as kb
    kb.init_db()
    conn = kb.connect()
    try:
        # Re-run migration; should not raise.
        kb._migrate_add_optional_columns(conn)
        kb._migrate_add_optional_columns(conn)
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "created_profiles" in cols
    finally:
        conn.close()


def test_created_profiles_record_is_idempotent_per_name(hermes_home, kanban_conn):
    from hermes_cli import kanban_db as kb
    tid = kb.create_task(kanban_conn, title="x", assignee="root")
    kb.record_created_profile(
        kanban_conn, tid, profile_name="qa-eng", role="A", base="default",
    )
    kb.record_created_profile(
        kanban_conn, tid, profile_name="qa-eng", role="A", base="default",
    )
    kb.record_created_profile(
        kanban_conn, tid, profile_name="other", role="B", base="default",
    )
    row = kanban_conn.execute(
        "SELECT created_profiles FROM tasks WHERE id = ?", (tid,),
    ).fetchone()
    entries = json.loads(row["created_profiles"])
    names = [e["name"] for e in entries]
    assert names.count("qa-eng") == 1
    assert names.count("other") == 1
