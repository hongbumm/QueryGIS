import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from codex_connector.bridge import BridgeServer
from codex_connector.tasks import TaskStore


class CodexBridgeTest(unittest.TestCase):
    def test_pro_snapshot_and_explicit_handshake_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            discovery = Path(directory) / "connection.json"
            bridge = BridgeServer(TaskStore(), discovery)
            bridge.start()
            self.addCleanup(bridge.stop)
            details = json.loads(discovery.read_text(encoding="utf-8"))
            root = "http://127.0.0.1:{}".format(details["port"])
            opener = build_opener(ProxyHandler({}))

            def request(path="/health", *, codex=True, token=details["token"], body=None):
                headers = {"Authorization": "Bearer " + token}
                if codex:
                    headers["X-QueryGIS-Codex"] = "1"
                data = None
                if body is not None:
                    headers["Content-Type"] = "application/json"
                    data = json.dumps(body).encode()
                with opener.open(Request(root + path, data=data, headers=headers), timeout=2) as response:
                    return json.load(response)

            try:
                with self.assertRaises(HTTPError) as denied:
                    request()
                self.assertEqual(denied.exception.code, 403)
                denied.exception.close()
                self.assertFalse(bridge.codex_connected)

                bridge.set_snapshot({"querygis_ready": True})
                self.assertTrue(request(codex=False)["querygis_ready"])
                with self.assertRaises(HTTPError) as unauthorized:
                    request(token="invalid")
                self.assertEqual(unauthorized.exception.code, 403)
                unauthorized.exception.close()
                self.assertFalse(bridge.codex_connected)

                task = {"instruction": "List the selected layers", "request_id": "layers", "mode": "ask"}
                with self.assertRaises(HTTPError) as waiting:
                    request("/tasks", body=task)
                self.assertEqual(waiting.exception.code, 409)
                waiting.exception.close()
                self.assertTrue(request()["querygis_ready"])
                self.assertTrue(bridge.codex_connected)
                self.assertEqual(request("/tasks", body=task)["state"], "queued")
            finally:
                bridge.stop()

            self.assertFalse(discovery.exists())
