import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from event_store import EventStore
from task_engine import TaskManager


class DurableTaskGatewayTests(unittest.TestCase):
    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _request(base_url: str, path: str, *, method: str = "GET", body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            base_url + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "Authorization": "Bearer durable-test-token"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))

    def _start_server(self, workspace: Path, db_path: Path, skills_path: Path) -> tuple[subprocess.Popen, str]:
        repository = Path(__file__).parents[1]
        port = self._free_port()
        env = os.environ.copy()
        env.update({
            "HOME": str(workspace / "home"),
            "KYROZEN_DB_PATH": str(db_path),
            "KYROZEN_DISABLE_VECTOR_INDEX": "1",
            "KYROZEN_PROVIDER": "ollama",
            "KYROZEN_BASE_URL": "http://127.0.0.1:11434/v1",
            "KYROZEN_SERVER_TOKEN": "durable-test-token",
            "KYROZEN_SKILLS_DIR": str(skills_path),
        })
        for variable in ("KYROZEN_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            env.pop(variable, None)
        process = subprocess.Popen(
            [sys.executable, str(repository / "server.py"), "--project", str(workspace),
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                self.fail(f"Gateway exited before becoming ready: {output}")
            try:
                health = self._request(base_url, "/api/health")
                if health.get("status") in {"ok", "degraded"}:
                    return process, base_url
            except (OSError, urllib.error.URLError):
                time.sleep(0.1)
        process.terminate()
        process.wait(timeout=5)
        self.fail("Gateway did not become ready")

    @staticmethod
    def _stop_server(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def test_gateway_recovers_scoped_running_task_and_executes_api_task(self):
        with tempfile.TemporaryDirectory(prefix="openkyrozen-task-gateway-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "home").mkdir()
            db_path = root / "state.sqlite3"
            skills_path = root / "skills"
            store = EventStore(db_path)
            recovery_manager = TaskManager(
                store, user_id="local", workspace_id="default", session_id="restart-session"
            )
            recovery_index = recovery_manager.add_task(
                "recover a running file task",
                checkpoint={"action": "write_file", "args": "recovered.txt|after restart"},
            )
            recovery_manager.set_status(recovery_index, "running")
            failed_manager = TaskManager(
                store, user_id="local", workspace_id="default", session_id="resume-session"
            )
            failed_index = failed_manager.add_task("resume only when requested")
            failed_manager.set_status(failed_index, "failed")
            failed_id = failed_manager.tasks[failed_index]["id"]

            process, base_url = self._start_server(workspace, db_path, skills_path)
            try:
                deadline = time.monotonic() + 10
                recovered = self._request(base_url, "/api/v2/tasks?session_id=restart-session")
                while time.monotonic() < deadline:
                    recovered = self._request(base_url, "/api/v2/tasks?session_id=restart-session")
                    if recovered["tasks"][0]["status"] == "succeeded":
                        break
                    time.sleep(0.1)
                self.assertEqual(recovered["tasks"][0]["status"], "succeeded")
                self.assertEqual((workspace / "recovered.txt").read_text(encoding="utf-8"), "after restart")

                created = self._request(
                    base_url,
                    "/api/v2/tasks",
                    method="POST",
                    body={
                        "description": "write a live task marker",
                        "session_id": "task-live",
                        "action": "write_file",
                        "args": "task-runtime.txt|created by durable worker",
                        "acceptance": ["marker exists"],
                    },
                )
                live_id = created["task"]["id"]
                deadline = time.monotonic() + 10
                live = created["task"]
                while time.monotonic() < deadline:
                    live = self._request(base_url, "/api/v2/tasks?session_id=task-live")["tasks"][0]
                    if live["status"] == "succeeded":
                        break
                    time.sleep(0.1)
                self.assertEqual(live["id"], live_id)
                self.assertEqual(live["status"], "succeeded")
                self.assertEqual(
                    (workspace / "task-runtime.txt").read_text(encoding="utf-8"),
                    "created by durable worker",
                )

                before_resume = self._request(base_url, "/api/v2/tasks?session_id=resume-session")
                self.assertEqual(before_resume["tasks"][0]["status"], "failed")
                resumed = self._request(
                    base_url, f"/api/v2/tasks/{failed_id}/resume", method="POST", body={}
                )
                self.assertEqual(resumed["task"]["id"], failed_id)
                self.assertEqual(resumed["task"]["status"], "pending")

                events = self._request(base_url, "/api/v2/events?session_id=restart-session")["events"]
                event_types = {event["event_type"] for event in events}
                self.assertIn("task.recovered", event_types)
                self.assertIn("task.execution_completed", event_types)
            finally:
                self._stop_server(process)


if __name__ == "__main__":
    unittest.main()
