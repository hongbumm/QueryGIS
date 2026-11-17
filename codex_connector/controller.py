"""Run the local bridge and dispatch confirmed Pro requests on the QGIS UI thread."""
import time

from qgis.PyQt.QtCore import QObject, QTimer
from qgis.core import QgsProject

from .bridge import BridgeServer
from .tasks import TaskStore


class CodexConnection(QObject):
    def __init__(self, querygis):
        super().__init__(querygis)
        self.querygis = querygis
        self.bridge = None
        self.store = None
        self.active_task = None
        self.task_mode = None
        self._checking_task = None
        self._last_plan_check = 0
        self._reported_connection = False
        self._before_layers = set()
        self._messages = []
        self.timer = QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self._tick)

    @property
    def connected(self):
        return bool(self.bridge and self.bridge.codex_connected)

    @property
    def cancelled(self):
        return bool(self.active_task and self.store.details(self.active_task)["cancel_event"].is_set())

    def start(self):
        if self.bridge or self.active_task or not self.querygis._can_connect_to_codex():
            return False
        self.store = TaskStore()
        bridge = BridgeServer(self.store)
        self.bridge = bridge
        self._reported_connection = False
        self._last_plan_check = time.monotonic()
        try:
            self._refresh_snapshot()
            bridge.start()
        except (OSError, RuntimeError) as error:
            self.stop()
            self.querygis._update_codex_ui(str(error))
            return False
        self.timer.start()
        self.querygis._update_codex_ui()
        return True

    def stop(self):
        self.timer.stop()
        self._checking_task = None
        bridge, self.bridge = self.bridge, None
        if bridge:
            bridge.set_snapshot({"querygis_ready": False})
            bridge.stop()
            self.store.cancel_all()

    def _refresh_snapshot(self):
        project = QgsProject.instance()
        self.bridge.set_snapshot({
            "querygis_ready": self.querygis._can_connect_to_codex(),
            "layers": [{"id": layer.id(), "name": layer.name(), "crs": layer.crs().authid()}
                       for layer in project.mapLayers().values()],
            "project_crs": project.crs().authid(),
        })

    def _tick(self):
        if not self.bridge:
            return
        if not self.querygis._can_connect_to_codex():
            self.stop()
            self.querygis._update_codex_ui()
            return
        if self.connected and not self._reported_connection:
            self._reported_connection = True
            self.querygis._update_codex_ui()
        self._refresh_snapshot()
        if self.connected and not self.active_task and not self._checking_task and self.querygis._can_start_codex_task():
            self._checking_task = self.store.next_queued()
            if self._checking_task:
                self.querygis.refresh_subscription_usage(quiet=True)
                return
        if time.monotonic() - self._last_plan_check >= 60:
            self.querygis.refresh_subscription_usage(quiet=True)

    def subscription_checked(self):
        if not self.bridge:
            return
        if not self.querygis._can_connect_to_codex():
            self.stop()
            return
        self._last_plan_check = time.monotonic()
        task_id, self._checking_task = self._checking_task, None
        if not task_id or not self.querygis._can_start_codex_task():
            return
        if not self.store.update(task_id, "planning", "Preparing the QueryGIS request."):
            return

        task = self.store.details(task_id)["payload"]
        self.active_task = task_id
        self.task_mode = task["mode"]
        self._messages = []
        self._before_layers = set(QgsProject.instance().mapLayers())
        if not self.querygis._submit_query(codex_instruction=task["instruction"]):
            self.finish(False, error="QueryGIS could not start the Codex request.")

    def mark_planning(self):
        if self.active_task:
            self.store.update(self.active_task, "planning", "Waiting for QueryGIS to prepare the response.")

    def mark_running(self):
        return not self.active_task or self.store.update(self.active_task, "running", "Running the analysis in QGIS.")

    def add_message(self, role, message):
        if self.active_task and role in ("assistant-print", "assistant-result"):
            self._messages.append(message.title if role == "assistant-result" else message)

    def finish(self, success, *, executed=False, error=""):
        task_id, self.active_task = self.active_task, None
        self.task_mode = None
        if not task_id:
            return
        task = self.store.public(task_id)
        if task["state"] == "cancelled":
            return
        project = QgsProject.instance()
        layers = [
            {"id": layer_id, "name": layer.name(), "crs": layer.crs().authid(), "valid": layer.isValid()}
            for layer_id, layer in project.mapLayers().items() if layer_id not in self._before_layers
        ]
        if task["cancel_requested"]:
            state, message = "cancelled", "Task cancelled. Changes completed before cancellation may still be present."
        elif success:
            state, message = "succeeded", "QueryGIS task completed."
        else:
            state, message = "failed", "QueryGIS task failed."
        result = {"executed": executed, "layers": layers, "message": "\n".join(self._messages)[-16000:] or message}
        if error:
            result["error"] = error
        self.store.update(task_id, state, message, result)
