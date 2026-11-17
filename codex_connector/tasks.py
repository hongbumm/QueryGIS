"""Thread-safe task mailbox. This module never imports or calls QGIS."""
import copy
import threading
import time
import uuid

TERMINAL = {"succeeded", "failed", "cancelled"}
TRANSITIONS = {
    "queued": {"planning", "failed", "cancelled"},
    "planning": {"running", "succeeded", "failed", "cancelled"},
    "running": {"planning", "succeeded", "failed", "cancelled"},
}


class TaskStore:
    def __init__(self, limit=100):
        self._lock = threading.RLock()
        self._tasks = {}
        self._requests = {}
        self._limit = limit

    def submit(self, payload, origin="codex"):
        if not isinstance(payload, dict):
            raise ValueError("A JSON object is required")
        unknown = set(payload) - {"instruction", "request_id", "mode"}
        if unknown:
            raise ValueError("Unsupported task fields: " + ", ".join(sorted(unknown)))
        instruction = payload.get("instruction")
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= 12000:
            raise ValueError("instruction must contain 1 to 12000 characters")
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise ValueError("A stable request_id is required for safe retries")
        mode = payload.get("mode", "run")
        if mode not in ("ask", "run"):
            raise ValueError("mode must be ask or run")
        normalized = {"instruction": instruction.strip(), "mode": mode}
        with self._lock:
            if request_id in self._requests:
                old = self._tasks[self._requests[request_id]]
                if old["payload"] != normalized or old["origin"] != origin:
                    raise ValueError("request_id was already used for a different task")
                return self.public(old["id"])
            # Keep request IDs for this session: a retry must never re-run an evicted task.
            if len(self._tasks) >= self._limit:
                raise ValueError("Session task limit reached; reconnect after current tasks finish")
            task_id = uuid.uuid4().hex
            self._tasks[task_id] = {
                "id": task_id, "origin": origin, "payload": copy.deepcopy(normalized),
                "state": "queued", "message": "QueryGIS 작업 대기 중",
                "created_at": time.time(), "updated_at": time.time(),
                "result": None, "cancel_event": threading.Event(),
            }
            self._requests[request_id] = task_id
            return self.public(task_id)

    def public(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            return copy.deepcopy({
                k: task[k] for k in ("id", "origin", "state", "message", "created_at", "updated_at", "result")
            }) | {"cancel_requested": task["cancel_event"].is_set()}

    def details(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            return {"payload": copy.deepcopy(task["payload"]), "origin": task["origin"],
                    "cancel_event": task["cancel_event"]}

    def next_queued(self):
        with self._lock:
            return next((i for i, t in self._tasks.items() if t["state"] == "queued"), None)

    def update(self, task_id, state, message, result=None):
        with self._lock:
            task = self._tasks[task_id]
            if task["state"] in TERMINAL or (state != task["state"] and state not in TRANSITIONS.get(task["state"], set())):
                return False
            task.update(state=state, message=message, updated_at=time.time())
            if result is not None:
                task["result"] = copy.deepcopy(result)
            return True

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks[task_id]
            if task["state"] not in TERMINAL:
                task["cancel_event"].set()
                if task["state"] in {"queued", "planning"}:
                    self.update(task_id, "cancelled", "Task cancelled.", {"executed": False, "layers": []})
                else:
                    task["message"] = "취소 요청됨: 실행 중인 처리가 종료될 때 반영됩니다"
            return self.public(task_id)

    def cancel_all(self):
        with self._lock:
            for task_id in self._tasks:
                self.cancel(task_id)


def connection_path():
    import os
    from pathlib import Path
    if os.environ.get("QUERYGIS_CONNECTION_FILE"):
        return Path(os.environ["QUERYGIS_CONNECTION_FILE"]).expanduser()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "QueryGIS_with_Codex" / "connection.json"
