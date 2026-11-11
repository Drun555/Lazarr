import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

from lazarr.task_progress import TaskProgress, persisted_tasks, registered_task


def test_registered_task_reports_progress_and_completion_without_eta(tmp_path):
    with registered_task(
        tmp_path, "archive", kind="offline-index", detail="Rutracker · dump.xml.xz", total_bytes=100
    ) as job:
        job.update(force=True, completed_bytes=40, scanned=200, indexed=80)
        first = persisted_tasks(tmp_path)[0]
        assert first["state"] == "running" and first["progress"] == 0.4
        assert first["scanned"] == 200 and first["indexed"] == 80
        job.update(force=True, completed_bytes=35)
        assert persisted_tasks(tmp_path)[0]["progress"] == 0.4
        job.update(force=True, completed_bytes=100, phase="Сохранение индекса")
        assert persisted_tasks(tmp_path)[0]["progress"] < 1
    final = persisted_tasks(tmp_path)[0]
    assert final["id"] == first["id"]
    assert final["state"] == "completed" and final["progress"] == 1
    assert final["finished_at"] and "eta" not in final and "owner_id" not in final


@pytest.mark.parametrize(
    "exception,state",
    [
        (ValueError("private error"), "failed"),
        (KeyboardInterrupt(), "cancelled"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_stopped_job_has_terminal_state(tmp_path, exception, state):
    with pytest.raises(type(exception)):
        with registered_task(
            tmp_path, "archive", kind="offline-index", detail="Archive", total_bytes=100
        ) as job:
            job.update(completed_bytes=40)
            raise exception
    row = persisted_tasks(tmp_path)[0]
    assert row["state"] == state and row["progress"] == 0.4
    assert "private error" not in json.dumps(row)


def test_dead_cli_process_is_not_reported_as_running(tmp_path):
    script = """import sys
from lazarr.task_progress import registered_task
with registered_task(sys.argv[1], "source", kind="offline-index", detail="CLI", total_bytes=100) as task:
    task.update(force=True, completed_bytes=25)
    print("ready", flush=True)
    sys.stdin.readline()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        row = persisted_tasks(tmp_path)[0]
        assert row["state"] == "running" and row["progress"] == 0.25
        process.kill()
        process.wait(timeout=5)
        stopped = persisted_tasks(tmp_path)[0]
        assert stopped["id"] == row["id"] and stopped["state"] == "interrupted"
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_progress_publication_is_throttled_and_invalid_records_are_ignored(tmp_path, monkeypatch):
    from lazarr import task_progress

    clock = [10.0]
    monkeypatch.setattr(task_progress.time, "monotonic", lambda: clock[0])
    path = tmp_path / "job.json"
    job = TaskProgress(path, "offline-index", "Archive", 100)
    job.update(force=True)
    before = path.read_bytes()
    job.update(completed_bytes=50)
    assert path.read_bytes() == before
    clock[0] += 1.1
    job.update(scanned=5)
    assert json.loads(path.read_text())["completed_bytes"] == 50
    (tmp_path / "invalid.json").write_text("not json")
    assert len(persisted_tasks(tmp_path)) == 1


def test_real_index_job_is_visible_and_updates_in_background_api(core, media, monkeypatch):
    from fastapi.testclient import TestClient
    from lazarr.app import create_app
    from lazarr.config import Requirements
    from lazarr.providers import rutracker_offline as offline
    from lazarr.sdk import SearchQuery
    from test_api import login
    from test_rutracker_offline import topic, write_archive

    gates = [threading.Event(), threading.Event()]
    reached = [threading.Event(), threading.Event()]
    original = offline.records

    def controlled(path, offset=0):
        count = 0
        for element in original(path, offset):
            yield element
            if not isinstance(element, offline.Checkpoint) and count < 2:
                offline.report_progress(force=True)
                reached[count].set()
                assert gates[count].wait(5)
                count += 1

    monkeypatch.setattr(offline, "records", controlled)
    with TestClient(create_app(core[0])) as client:
        login(client)
        plugins = client.app.state.ctx.plugins
        plugins.configure("rutracker", {}, True)
        plugins.transport = httpx.MockTransport(
            lambda request: pytest.fail("Offline import made HTTP request")
        )
        archive = write_archive(plugins.offline_directories("rutracker")[0], [topic(1), topic(2)])

        async def search():
            async with plugins.open("rutracker", offline=True) as provider:
                return await provider.search(
                    SearchQuery(media=media, requirements=Requirements()), offline=True
                )

        def snapshot():
            response = client.get("/api/v1/background-tasks")
            assert response.status_code == 200
            return [row for row in response.json()["items"] if row["kind"] == "offline-index"]

        with ThreadPoolExecutor(1) as executor:
            future = executor.submit(asyncio.run, search())
            try:
                assert reached[0].wait(5)
                first = snapshot()
                assert len(first) == 1 and first[0]["state"] == "running"
                assert 0 < first[0]["progress"] < 1
                assert first[0]["scanned"] == first[0]["indexed"] == 1
                assert first[0]["total_bytes"] == archive.stat().st_size
                gates[0].set()
                assert reached[1].wait(5)
                second = snapshot()[0]
                assert second["id"] == first[0]["id"] and second["scanned"] == 2
                assert "eta" not in second
            finally:
                for gate in gates:
                    gate.set()
            assert len(future.result(timeout=5).items) == 2
        final = snapshot()[0]
        assert final["state"] == "completed" and final["progress"] == 1
        asyncio.run(search())  # Reusing the ready index must not register another import.
        assert snapshot() == [final]
