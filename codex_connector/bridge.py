"""Authenticated loopback mailbox. HTTP threads never touch a QGIS object."""
import hmac
import json
import logging
from pathlib import Path
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, Request, build_opener

from .tasks import connection_path


class BridgeServer:
    def __init__(self, store, discovery_file=None):
        self.store = store
        self.discovery_file = discovery_file or connection_path()
        self.token = secrets.token_urlsafe(32)
        self._lock = threading.Lock()
        self._snapshot = {"querygis_ready": False, "layers": [], "project_crs": ""}
        self._codex_seen = threading.Event()
        self._server = None
        self._thread = None

    def _discovery_paths(self):
        paths = [self.discovery_file]
        # Packaged Codex may read an old redirected LocalAppData copy.
        # Publish to existing Codex package stores as well as the native path.
        base = os.environ.get("LOCALAPPDATA")
        if os.name == "nt" and base and not os.environ.get("QUERYGIS_CONNECTION_FILE"):
            base = Path(base)
            if Path(self.discovery_file) == base / "QueryGIS_with_Codex" / "connection.json":
                packages = base / "Packages"
                for package in packages.glob("OpenAI.Codex_*"):
                    if package.is_dir():
                        paths.append(package / "LocalCache" / "Local" / "QueryGIS_with_Codex" / "connection.json")
        return paths

    def _publish_discovery(self, path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)

    def set_snapshot(self, snapshot):
        with self._lock:
            self._snapshot = json.loads(json.dumps(snapshot))

    def snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._snapshot))

    @property
    def running(self):
        return self._server is not None

    @property
    def codex_connected(self):
        return self._codex_seen.is_set()

    def _check_existing(self):
        if not self.discovery_file.exists():
            return
        try:
            old = json.loads(self.discovery_file.read_text(encoding="utf-8"))
            port = old["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                return
            req = Request("http://127.0.0.1:%d/health" % port,
                          headers={"Authorization": "Bearer " + old["token"]})
            with build_opener(ProxyHandler({})).open(req, timeout=0.2) as response:
                active = json.load(response).get("service") == "querygis-with-codex"
        except Exception:
            return
        if active:
            raise RuntimeError("다른 QGIS 세션이 이미 Codex에 연결되어 있습니다")

    def start(self):
        if self.running:
            return
        self._check_existing()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "QueryGIS-Connector/0.2"

            def log_message(self, *args):
                pass

            def _reply(self, status, data):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self):
                expected_hosts = {"127.0.0.1:%d" % self.server.server_port,
                                  "localhost:%d" % self.server.server_port}
                auth = self.headers.get("Authorization", "")
                if (self.headers.get("Origin") is not None or
                        self.headers.get("Host") not in expected_hosts or
                        not auth.isascii() or
                        not hmac.compare_digest(auth, "Bearer " + bridge.token)):
                    self._reply(403, {"error": "Unauthorized local connection"})
                    return False
                if not bridge.snapshot().get("querygis_ready"):
                    self._reply(403, {"error": "Connect with Codex requires a Google account with an active Pro plan."})
                    return False
                if self.path != "/health":
                    if self.headers.get("X-QueryGIS-Codex") != "1":
                        self._reply(403, {"error": "Update the QueryGIS plugin in Codex to use this connection."})
                        return False
                    if not bridge.codex_connected:
                        self._reply(409, {"error": "Check the QueryGIS connection in Codex before submitting a task."})
                        return False
                return True

            def do_GET(self):
                if not self._authorized():
                    return
                if self.path == "/health":
                    if self.headers.get("X-QueryGIS-Codex") == "1":
                        bridge._codex_seen.set()
                    self._reply(200, {"service": "querygis-with-codex", "version": "0.2.0",
                                      **bridge.snapshot()})
                    return
                match = re.fullmatch(r"/tasks/([a-f0-9]{32})", self.path)
                if match:
                    try:
                        self._reply(200, bridge.store.public(match.group(1)))
                    except KeyError:
                        self._reply(404, {"error": "Unknown task ID"})
                else:
                    self._reply(404, {"error": "Unknown route"})

            def do_POST(self):
                if not self._authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._reply(400, {"error": "Invalid content length"})
                    return
                if not 0 < length <= 65536 or self.headers.get_content_type() != "application/json":
                    self._reply(400, {"error": "A JSON body up to 64 KB is required"})
                    return
                try:
                    payload = json.loads(self.rfile.read(length))
                    if self.path == "/tasks":
                        self._reply(202, bridge.store.submit(payload))
                        return
                    match = re.fullmatch(r"/tasks/([a-f0-9]{32})/cancel", self.path)
                    if match:
                        if payload != {}:
                            raise ValueError("Cancel request body must be empty")
                        self._reply(200, bridge.store.cancel(match.group(1)))
                        return
                    self._reply(404, {"error": "Unknown route"})
                except KeyError:
                    self._reply(404, {"error": "Unknown task ID"})
                except (ValueError, UnicodeError) as exc:
                    self._reply(400, {"error": str(exc)})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                                        name="QueryGIS-Codex-mailbox")
        self._thread.start()
        try:
            data = {"version": 2, "port": self._server.server_port, "token": self.token, "pid": os.getpid()}
            self._publish_discovery(self.discovery_file, data)
            for path in self._discovery_paths()[1:]:
                try:
                    self._publish_discovery(path, data)
                except OSError:
                    logging.getLogger(__name__).warning("Could not update Codex discovery mirror: %s", path)
        except Exception:
            self.stop()
            raise

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        for path in self._discovery_paths():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("token") == self.token:
                    path.unlink()
            except (OSError, ValueError):
                pass
