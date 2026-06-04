"""Regression tests for dashboard cron job profile routing."""

import pytest
from fastapi import HTTPException


@pytest.fixture()
def isolated_profiles(tmp_path, monkeypatch):
    """Give profile discovery an isolated default home with one named profile."""
    from hermes_cli import profiles

    default_home = tmp_path / ".hermes"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"

    for home in (default_home, worker_home):
        (home / "cron").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


def test_call_cron_for_profile_routes_storage_and_restores_globals(isolated_profiles):
    from cron import jobs as cron_jobs
    from hermes_cli import web_server

    old_cron_dir = cron_jobs.CRON_DIR
    old_jobs_file = cron_jobs.JOBS_FILE
    old_output_dir = cron_jobs.OUTPUT_DIR

    job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="run scheduled task",
        schedule="every 1h",
        name="worker-alpha-scan",
    )

    assert job["profile"] == "worker_alpha"
    assert job["profile_name"] == "worker_alpha"
    assert job["hermes_home"] == str(isolated_profiles["worker_alpha"])
    assert job["is_default_profile"] is False
    assert (isolated_profiles["worker_alpha"] / "cron" / "jobs.json").exists()
    assert not (isolated_profiles["default"] / "cron" / "jobs.json").exists()

    assert cron_jobs.CRON_DIR == old_cron_dir
    assert cron_jobs.JOBS_FILE == old_jobs_file
    assert cron_jobs.OUTPUT_DIR == old_output_dir


def test_unavailable_cron_media_keeps_existing_files_playable(tmp_path):
    from hermes_cli import web_server

    existing = tmp_path / "briefing.mp3"
    existing.write_bytes(b"audio")

    unavailable = web_server._unavailable_cron_media(
        [
            {
                "content": (
                    f"MEDIA: {existing}\n"
                    "MEDIA: /tmp/hermes-cron-history-missing.mp3"
                )
            }
        ]
    )

    assert unavailable == ["/tmp/hermes-cron-history-missing.mp3"]


@pytest.mark.asyncio
async def test_list_cron_jobs_all_includes_default_and_named_profiles(isolated_profiles):
    from hermes_cli import web_server

    default_job = web_server._call_cron_for_profile(
        "default",
        "create_job",
        prompt="default heartbeat",
        schedule="every 2h",
        name="default-heartbeat",
    )
    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker heartbeat",
        schedule="every 3h",
        name="worker-alpha-heartbeat",
    )

    jobs = await web_server.list_cron_jobs(profile="all")
    by_id = {job["id"]: job for job in jobs}

    assert set(by_id) >= {default_job["id"], worker_job["id"]}
    assert by_id[default_job["id"]]["profile"] == "default"
    assert by_id[default_job["id"]]["is_default_profile"] is True
    assert by_id[default_job["id"]]["hermes_home"] == str(isolated_profiles["default"])
    assert by_id[worker_job["id"]]["profile"] == "worker_alpha"
    assert by_id[worker_job["id"]]["is_default_profile"] is False
    assert by_id[worker_job["id"]]["hermes_home"] == str(isolated_profiles["worker_alpha"])


@pytest.mark.asyncio
async def test_list_cron_jobs_specific_profile_filters_results(isolated_profiles):
    from hermes_cli import web_server

    web_server._call_cron_for_profile(
        "default",
        "create_job",
        prompt="default only",
        schedule="every 2h",
        name="default-only",
    )
    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker only",
        schedule="every 3h",
        name="worker-only",
    )

    jobs = await web_server.list_cron_jobs(profile="worker_alpha")

    assert [job["id"] for job in jobs] == [worker_job["id"]]
    assert jobs[0]["profile"] == "worker_alpha"


@pytest.mark.asyncio
async def test_cron_job_runs_return_full_profile_scoped_lineage(isolated_profiles):
    from hermes_state import SessionDB
    from hermes_cli import web_server

    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker history",
        schedule="every 3h",
        name="worker-history",
    )
    worker_home = isolated_profiles["worker_alpha"]
    root_id = f"cron_{worker_job['id']}_20260603_120000"

    db = SessionDB(worker_home / "state.db")
    try:
        db.create_session(session_id=root_id, source="cron")
        db.append_message(root_id, role="user", content="Build the report")
        db.end_session(root_id, "compression")

        db.create_session(session_id="compressed-tip", source="cron", parent_session_id=root_id)
        db.append_message("compressed-tip", role="user", content="Build the report")
        db.append_message("compressed-tip", role="assistant", content="MEDIA: /tmp/report.png")
        db.end_session("compressed-tip", "cron_complete")

        db.create_session(session_id="cron_other-job_20260603_120000", source="cron")
        db.append_message("cron_other-job_20260603_120000", role="assistant", content="not this job")
        colliding_id = f"cron_{worker_job['id']}_legacy_20260603_120000"
        db.create_session(session_id=colliding_id, source="cron")
        db.append_message(colliding_id, role="assistant", content="prefix collision")
    finally:
        db.close()

    result = await web_server.get_cron_job_runs(worker_job["id"], profile="worker_alpha")

    assert result["profile"] == "worker_alpha"
    assert result["job_id"] == worker_job["id"]
    assert len(result["runs"]) == 1
    run = result["runs"][0]
    assert run["session_id"] == root_id
    assert run["end_reason"] == "cron_complete"
    assert run["message_count"] == 2
    assert [message["content"] for message in run["messages"]] == [
        "Build the report",
        "MEDIA: /tmp/report.png",
    ]
    assert run["unavailable_media"] == ["/tmp/report.png"]
    assert all(message["timestamp"] > 0 for message in run["messages"])


@pytest.mark.asyncio
async def test_cron_job_runs_without_state_db_are_empty(isolated_profiles):
    from hermes_cli import web_server

    job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker history",
        schedule="every 3h",
        name="worker-history-empty",
    )
    state_db = isolated_profiles["worker_alpha"] / "state.db"

    result = await web_server.get_cron_job_runs(job["id"], profile="worker_alpha")

    assert result == {"job_id": job["id"], "profile": "worker_alpha", "runs": []}
    assert not state_db.exists()


@pytest.mark.asyncio
async def test_cron_mutation_without_profile_finds_named_profile_job(isolated_profiles):
    from hermes_cli import web_server

    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="managed by named profile",
        schedule="every 1h",
        name="named-profile-job",
    )

    paused = await web_server.pause_cron_job(worker_job["id"])
    assert paused["profile"] == "worker_alpha"
    assert paused["enabled"] is False

    default_jobs = await web_server.list_cron_jobs(profile="default")
    worker_jobs = await web_server.list_cron_jobs(profile="worker_alpha")

    assert default_jobs == []
    assert len(worker_jobs) == 1
    assert worker_jobs[0]["id"] == worker_job["id"]
    assert worker_jobs[0]["enabled"] is False


@pytest.mark.asyncio
async def test_update_cron_job_rejects_id_mutation(isolated_profiles):
    """Dashboard surfaces a 400 (not a 500 or silent rename) when an
    id-mutation attempt is rejected by cron/jobs.update_job."""
    from hermes_cli import web_server

    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="managed by named profile",
        schedule="every 1h",
        name="immutable-id-job",
    )

    with pytest.raises(HTTPException) as exc:
        await web_server.update_cron_job(
            worker_job["id"],
            web_server.CronJobUpdate(updates={"id": "../escape"}),
            profile="worker_alpha",
        )

    assert exc.value.status_code == 400
    assert "id" in exc.value.detail
    worker_jobs = await web_server.list_cron_jobs(profile="worker_alpha")
    assert [job["id"] for job in worker_jobs] == [worker_job["id"]]


@pytest.mark.asyncio
async def test_cron_delete_with_profile_deletes_only_target_profile(isolated_profiles):
    from hermes_cli import web_server

    default_job = web_server._call_cron_for_profile(
        "default",
        "create_job",
        prompt="same-ish default",
        schedule="every 1h",
        name="shared-name",
    )
    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="same-ish worker",
        schedule="every 1h",
        name="shared-name-worker",
    )

    deleted = await web_server.delete_cron_job(worker_job["id"], profile="worker_alpha")
    assert deleted == {"ok": True}

    remaining_default = await web_server.list_cron_jobs(profile="default")
    remaining_worker = await web_server.list_cron_jobs(profile="worker_alpha")
    assert [job["id"] for job in remaining_default] == [default_job["id"]]
    assert remaining_worker == []


@pytest.mark.asyncio
async def test_cron_profile_validation_errors(isolated_profiles):
    from hermes_cli import web_server

    with pytest.raises(HTTPException) as bad_name:
        await web_server.list_cron_jobs(profile="../bad")
    assert bad_name.value.status_code == 400

    with pytest.raises(HTTPException) as missing:
        await web_server.list_cron_jobs(profile="missing_profile")
    assert missing.value.status_code == 404
