import os, os.path, sys, io, tempfile, traceback, base64, re, time, uuid, http.server, urllib.parse
import builtins
import ast
import logging
import requests
import json
import secrets
import hashlib
from pathlib import Path
from qgis.utils import iface
from qgis.core import (
    Qgis,
    QgsApplication, QgsProject, QgsMapLayer, QgsPointCloudLayer, QgsRasterLayer, QgsVectorLayer,
    QgsField, QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsProcessingFeatureSourceDefinition,
    QgsFeatureSink, QgsFeatureRequest, QgsProcessingFeedback, QgsMessageLog,
    QgsFillSymbol, QgsSingleSymbolRenderer, QgsSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings, QgsVectorLayerSimpleLabeling,
    QgsProperty, QgsWkbTypes
)

from qgis.PyQt import QtCore, QtGui, QtNetwork, QtWidgets
from qgis.PyQt.QtCore import (
    QSettings, QTranslator, QCoreApplication, Qt, QTimer, QThread,
    pyqtSignal, QEvent, QMetaType, QVariant, QObject
)
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QDockWidget, QWidget, QHBoxLayout, QLabel, QApplication

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .resources import *
from .codex_connector.controller import CodexConnection
from .dockwidget import ResultCardData, Ui_DockWidget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ENABLE_REMOTE_LOG = True
CURRENT_VERSION = "1.9.19"
RESULT_CAPABILITIES = {"result_cards": True, "line_charts": True, "distribution_charts": True}


def _backend_base_url():
    url = os.environ.get("QUERYGIS_BACKEND_URL", "").strip().rstrip("/") or "https://querygis.com"
    parsed = urllib.parse.urlsplit(url)
    is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path or parsed.query or parsed.fragment
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback))
    ):
        raise ValueError("QUERYGIS_BACKEND_URL must be an HTTPS origin or a local HTTP origin.")
    return url


def _runtime_metadata():
    version_int = Qgis.versionInt()
    return {
        "qgis_version": f"{version_int // 10000}.{version_int // 100 % 100}.{version_int % 100}",
        "qgis_version_int": version_int,
        "qt_major": QtCore.QT_VERSION >> 16,
    }


def _conversation_excerpt(text, limit):
    compact = " ".join(text.split())
    return compact if len(compact) <= limit else compact[:limit - 1].rstrip() + "…"


def _layer_kind(layer):
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    if isinstance(layer, QgsPointCloudLayer):
        return "pointcloud"
    return "unknown"


BACKEND_BASE_URL = _backend_base_url()
CHAT_ENDPOINT = f"{BACKEND_BASE_URL}/chat"
FIX_ENDPOINT = f"{BACKEND_BASE_URL}/fix-code"
REPORT_ENDPOINT = f"{BACKEND_BASE_URL}/report-error"
SUBSCRIPTION_USAGE_ENDPOINT = f"{BACKEND_BASE_URL}/subscription/usage"
OAUTH_REDIRECT_URI = "http://localhost:5055"
COGNITO_DOMAIN = "querygis-company-payment-auth-250847881209-20260617.auth.ap-northeast-2.amazoncognito.com"
COGNITO_CLIENT_ID = "7jpb08126rc270cj6ak4ebofer"
COGNITO_OAUTH_URL = f"https://{COGNITO_DOMAIN}/oauth2"
REPORT_TIMEOUT_SEC = 5
REPORT_RETRIES = 2

class _SoftErrorSignal(Exception):
    pass

class AutoVerifyWrapper:
    def __init__(self, obj):
        object.__setattr__(self, '_obj', obj)
        object.__setattr__(self, '_type', type(obj).__name__)
    
    def __getattr__(self, name):
        obj = object.__getattribute__(self, '_obj')
        obj_type = object.__getattribute__(self, '_type')
        
        if not hasattr(obj, name):
            return self._handle_missing(name)
        
        attr = getattr(obj, name)
        
        if callable(attr):
            def safe_wrapper(*args, **kwargs):
                try:
                    return attr(*args, **kwargs)
                except (AttributeError, TypeError) as e:
                    print(f"\n{obj_type}.{name}() failed: {e}")
                    return self._try_alternatives(name, args, kwargs)
            return safe_wrapper
        
        return attr
    
    def _handle_missing(self, name):
        obj = object.__getattribute__(self, '_obj')
        obj_type = object.__getattribute__(self, '_type')
        
        print(f"\n{obj_type}.{name} does not exist")
        
        all_attrs = [a for a in dir(obj) if not a.startswith('_')]
        name_lower = name.lower()
        similar = [a for a in all_attrs if name_lower in a.lower() or a.lower() in name_lower]
        
        if similar:
            print(f"Similar: {similar[:5]}")
            print(f"Using: {similar[0]}")
            return getattr(obj, similar[0])
        
        print(f"Available: {all_attrs[:20]}")
        
        def dummy(*args, **kwargs):
            print(f"Cannot execute {obj_type}.{name}")
            return None
        return dummy
    
    def _try_alternatives(self, method_name, args, kwargs):
        obj = object.__getattribute__(self, '_obj')
        obj_type = object.__getattribute__(self, '_type')
        
        all_methods = [m for m in dir(obj) if not m.startswith('_') and callable(getattr(obj, m, None))]
        keywords = method_name.lower().replace('_', ' ').split()
        
        candidates = [m for m in all_methods if all(kw in m.lower() for kw in keywords)]
        
        if candidates:
            print(f"Trying: {candidates[:3]}")
            for alt in candidates[:3]:
                try:
                    result = getattr(obj, alt)(*args, **kwargs)
                    print(f"✓ {obj_type}.{alt}() succeeded!")
                    return result
                except Exception as e:
                    print(f"{alt}() failed: {e}")
        
        print(f"All alternatives failed")
        print(f"Available: {all_methods[:15]}")
        return None
    
    def __setattr__(self, name, value):
        if name in ('_obj', '_type'):
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, '_obj'), name, value)
    
    def __repr__(self):
        obj_type = object.__getattribute__(self, '_type')
        return f"<SafeWrapper({obj_type})>"


def auto_wrap_scope(scope):
    def make_safe_class(original_class):
        def safe_constructor(*args, **kwargs):
            obj = original_class(*args, **kwargs)
            return AutoVerifyWrapper(obj)
        safe_constructor.__name__ = original_class.__name__
        return safe_constructor
    
    wrapped_scope = {}
    for key, value in scope.items():
        if isinstance(value, type) and key.startswith('Qgs'):
            wrapped_scope[key] = make_safe_class(value)
        else:
            wrapped_scope[key] = value
    
    return wrapped_scope

class SmartQgsImporter(dict):
    def __init__(self, scope):
        super().__init__(scope)
        self._qgis_core = None
    
    def __getitem__(self, key):
        try:
            return super().__getitem__(key)
        except KeyError:
            pass
        
        if not key.startswith('Qgs'):
            raise KeyError(f"'{key}' not found in scope")
        
        if self._qgis_core is None:
            import sys
            self._qgis_core = sys.modules.get('qgis.core')
        
        if self._qgis_core and hasattr(self._qgis_core, key):
            real_class = getattr(self._qgis_core, key)
            
            def safe_constructor(*args, **kwargs):
                obj = real_class(*args, **kwargs)
                return AutoVerifyWrapper(obj)
            
            safe_constructor.__name__ = key
            self[key] = safe_constructor 
            
            print(f"Auto-imported: {key}")
            return safe_constructor
        
        raise KeyError(f"'{key}' not found in qgis.core")
    
    def __contains__(self, key):
        if super().__contains__(key):
            return True
        if not key.startswith('Qgs'):
            return False
        if self._qgis_core is None:
            import sys
            self._qgis_core = sys.modules.get('qgis.core')
        return self._qgis_core and hasattr(self._qgis_core, key)

def _mask_sensitive(s: str) -> str:
    if not s:
        return s
    try:
        s = re.sub(r'AIza[0-9A-Za-z\-_]{35}', '***REDACTED_GOOGLE_KEY***', s)
        s = re.sub(r'AKIA[0-9A-Z]{16}', '***REDACTED_AWS_AK***', s)
        s = re.sub(r'(?<![A-Za-z0-9])[A-Za-z0-9/\+=]{40}(?![A-Za-z0-9])', '***REDACTED_AWS_SK***', s)
        s = re.sub(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}', '***REDACTED_JWT***', s)
        return s
    except Exception:
        return s

def _send_error_report(user_query: str,
                       context_text: str,
                       generated_code: str,
                       error_message: str,
                       model_name: str = "gemini-3-flash-preview",
                       phase: str = "execution",
                       metadata: dict = None,
                       query_gis_instance: 'QueryGIS' = None): 
    # CRITICAL TEST: Silence this function entirely to prevent any potential 
    # recursive threading or stdout issues that cause Access Violation.
    pass

import random

class WaveProgressManager:
    def __init__(self, update_callback, eta_callback=None):
        self.update_callback = update_callback
        self.eta_callback = eta_callback
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self._animate_wave)
        self.wave_position = 0
        self.current_message = ""
        self.is_active = False
        self.start_time = None
        
        self.dynamic_messages = [
            "Analyzing project layers...",
            "Syncing with GIS engine...",
            "Optimizing spatial logic...",
            "Extracting metadata context...",
            "Formatting query results...",
            "Applying geometry constraints...",
            "Resolving coordinate systems...",
            "Preparing AI execution environment...",
            "Generating Python code blocks...",
            "Refining response structure..."
        ]
        self.message_cycle_timer = QTimer()
        self.message_cycle_timer.timeout.connect(self._cycle_message)

    def start_wave(self, message="Processing"):
        self.current_message = message
        self.is_active = True
        self.wave_position = 0
        self.start_time = time.time()
        self.animation_timer.start(200)
        self.message_cycle_timer.start(3000)  # Cycle descriptive text every 3 seconds
        self._update_display()

    def update_message(self, message):
        self.current_message = str(message)
        if self.is_active:
            self._update_display()

    def stop_wave(self, final_message="Complete"):
        self.is_active = False
        self.animation_timer.stop()
        self.message_cycle_timer.stop()
        self.update_callback(final_message, True)
        if self.eta_callback:
            self.eta_callback("", False) # Hide ETA

    def _animate_wave(self):
        if not self.is_active:
            return
        self.wave_position = (self.wave_position + 1) % 8
        self._update_display()
        
        if self.eta_callback and self.start_time:
            elapsed = time.time() - self.start_time
            self.eta_callback(f"Elapsed: {elapsed:.1f}s", True)

    def _cycle_message(self):
        if self.is_active:
            # Randomly pick a descriptive message to keep the UI alive
            self.current_message = random.choice(self.dynamic_messages)

    def _update_display(self):
        wave_chars = ["⣾", "⣽", "⣻", "⢿", "⡿", "⣟", "⣯", "⣷"]
        char_index = self.wave_position % len(wave_chars)
        display_text = f"{wave_chars[char_index]} {self.current_message}"
        self.update_callback(display_text, False)

class LoggingWorker(QThread):
    def __init__(self, endpoint, json_data, timeout):
        super().__init__()
        self.endpoint = endpoint
        self.json_data = json_data
        self.timeout = timeout
        self.session = None

    def run(self):
        try:
            self.session = requests.Session()
            retry = Retry(total=1, connect=1, read=1, backoff_factor=0.1)
            adapter = HTTPAdapter(max_retries=retry)
            self.session.mount("https://", adapter)
            self.session.mount("http://", adapter)
            
            self.session.post(
                self.endpoint,
                json=self.json_data,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"}
            )
        except Exception as e:
            pass
        finally:
            if self.session:
                self.session.close()

class OAuthCallbackWorker(QThread):
    ready = pyqtSignal()
    verifying = pyqtSignal()
    authenticated = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, port, state, code_verifier):
        super().__init__()
        self.port = port
        self.state = state
        self.code_verifier = code_verifier

    def run(self):
        code = None
        callback_error = False
        expected_state = self.state

        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(2)
                super().setup()

            def do_GET(self):
                nonlocal code, callback_error
                url = urllib.parse.urlsplit(self.path)
                params = urllib.parse.parse_qs(url.query)
                valid = url.path == "/" and params.get("state") == [expected_state]
                message = b"Invalid QueryGIS login callback."
                content_type = "text/plain; charset=utf-8"
                status = 400
                if valid:
                    code = params.get("code", [None])[0]
                    callback_error = not code and "error" in params
                    if code or callback_error:
                        status = 200
                        message = b"Return to QGIS to finish signing in."
                    if code:
                        assets = Path(__file__).resolve().parent / "ui_assets"
                        try:
                            page = (assets / "oauth-welcome.html").read_bytes()
                            mascot = base64.b64encode((assets / "querygis-hello.png").read_bytes())
                        except OSError:
                            pass  # The sign-in still works if packaged artwork is missing.
                        else:
                            message = page.replace(b"{{MASCOT_BASE64}}", mascot)
                            content_type = "text/html; charset=utf-8"

                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(message)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(message)

            def log_message(self, *args):
                pass

        try:
            with http.server.HTTPServer(("127.0.0.1", self.port), Handler) as server:
                server.timeout = 0.25
                self.ready.emit()
                deadline = time.monotonic() + 180
                while not (code or callback_error or self.isInterruptionRequested()) and time.monotonic() < deadline:
                    server.handle_request()
        except OSError:
            self.failed.emit("Could not start the Google sign-in callback. Please try again.")
            return

        if self.isInterruptionRequested():
            return
        if callback_error or not code:
            self.failed.emit("Google sign-in was canceled or expired. Please try again.")
            return

        self.verifying.emit()
        try:
            response = requests.post(
                f"{COGNITO_OAUTH_URL}/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": COGNITO_CLIENT_ID,
                    "code": code,
                    "redirect_uri": OAUTH_REDIRECT_URI,
                    "code_verifier": self.code_verifier,
                },
                timeout=(4, 8),
                allow_redirects=False,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected Cognito token response")
            tokens = response.json()
            id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
            access_token = tokens.get("access_token") if isinstance(tokens, dict) else None
            if not all(isinstance(token, str) and token for token in (id_token, access_token)):
                raise ValueError("Missing Cognito tokens")
        except (requests.exceptions.RequestException, ValueError):
            if not self.isInterruptionRequested():
                self.failed.emit("Could not exchange the sign-in code with Cognito. Please try again.")
            return

        if self.isInterruptionRequested():
            return
        try:
            response = requests.get(
                f"{COGNITO_OAUTH_URL}/userInfo",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=(4, 8),
                allow_redirects=False,
            )
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Unexpected Cognito profile response")
            profile = response.json()
            sub = profile.get("sub") if isinstance(profile, dict) else None
            if not isinstance(sub, str) or not sub:
                raise ValueError("Missing Cognito subject")
        except (requests.exceptions.RequestException, ValueError):
            if not self.isInterruptionRequested():
                self.failed.emit("Could not load your account from Cognito. Please try again.")
            return

        if not self.isInterruptionRequested():
            self.authenticated.emit({
                "sub": sub,
                "email": profile.get("email"),
                "name": profile.get("name") or profile.get("username"),
                "id_token": id_token,
            })


class QueryRequestDenied(Exception):
    pass


class BackendWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    step_update = pyqtSignal(str)
    auth_expired = pyqtSignal()

    def __init__(self, payload, backend_url=CHAT_ENDPOINT, timeout_sec=180, headers=None):
        super().__init__()
        self.payload = payload
        self.backend_url = backend_url
        self.timeout_sec = timeout_sec
        self.headers = headers
        self._is_cancelled = False

        self._user_input = payload.get("user_input", "")
        self._context_text = payload.get("context", "")
        self._model_name = payload.get("model", "gemini-3-flash-preview")

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        session = None
        try:
            session = requests.Session()
            retry = Retry(
                total=2, connect=2, read=2,
                backoff_factor=0.2,
                status_forcelist=(502, 503, 504)
            )
            adapter = HTTPAdapter(pool_connections=2, pool_maxsize=5, max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            session.headers.update({
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "Content-Type": "application/json"
            })
        except Exception as e:
            self.error.emit(f"Failed to create request session: {e}")
            return
            
        try:
            if self._is_cancelled:
                return

            self.step_update.emit("Connecting to QueryGIS server...")
            if self._is_cancelled:
                return

            self.step_update.emit("Dispatched request to AI engine")
            try:
                resp = session.post(
                    self.backend_url,
                    json=self.payload,
                    headers=self.headers,
                    timeout=self.timeout_sec,
                    allow_redirects=False,
                )
                if resp.status_code == 401 and self.headers:
                    self.auth_expired.emit()
                    return
                
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, (dict, list)):
                            text = json.dumps(data, ensure_ascii=False)
                        else:
                            text = str(data)
                    except Exception:
                        text = resp.text
                    self.step_update.emit("Synthesizing response...")
                    self.finished.emit(text)
                else:
                    try:
                        ejson = resp.json()
                        msg = ejson.get("error") or ejson.get("message") or str(ejson)
                    except:
                        msg = resp.text[:300]
                    self.error.emit(f"Server error {resp.status_code}: {msg}")

                    _send_error_report(
                        user_query=self._user_input,
                        context_text=self._context_text,
                        generated_code="",
                        error_message=f"Server error {resp.status_code}: {msg}",
                        model_name=self._model_name,
                        phase="llm_call",
                        metadata={"plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}"}
                    )

            except requests.exceptions.Timeout:
                self.error.emit("Request timeout - server did not respond in time.")
                # We still try to report timeout because the server might just be slow, not dead
                try:
                    _send_error_report(self._user_input, self._context_text, "", "Timeout to backend",
                                       self._model_name, "llm_call", {"plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}"})
                except: pass

            except requests.exceptions.ConnectionError:
                self.error.emit(f"Could not reach {BACKEND_BASE_URL}. Check your connection and try again.")

            except requests.exceptions.RequestException as e:
                self.error.emit(f"Network error: {str(e)}")
                try:
                    _send_error_report(self._user_input, self._context_text, "", f"RequestException: {e}",
                                       self._model_name, "llm_call", {"plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}"})
                except: pass

        except Exception as e:
            logger.error(f"FATAL RequestsWorker crash: {str(e)}")
            try:
                self.error.emit(f"Worker crashed: {str(e)}")
            except:
                pass
        finally:
            if session:
                session.close()

class UiSafeBridge(QObject):
    requested = pyqtSignal(str, object) # message, progress

class _UIFeedback(QgsProcessingFeedback):
    def __init__(self, bridge, label="Working"):
        super().__init__()
        self._bridge = bridge
        self._label = label
    def setProgress(self, p):
        super().setProgress(p)
        # Safely request UI update via signal
        self._bridge.requested.emit(f"{self._label} {p:.0f}%", p)
    def pushInfo(self, info):
        super().pushInfo(info)
        if info:
            self._bridge.requested.emit(str(info), None)

class _RunProgressProxy:
    def __init__(self, bridge, scope=None):
        self._bridge = bridge
        self._scope = scope or {}
        self._calls_seen = 0
        self._calls_done = 0
        self._last_ui_ms = 0
    def _maybe_update(self, text, progress=None):
        now = int(time.time()*1000)
        # Throttled UI updates via signal
        if now - self._last_ui_ms >= 200: 
            self._last_ui_ms = now
            self._bridge.requested.emit(text, progress)
    def wrap(self, real_run):
        def _wrapped(alg_id, params, context=None, feedback=None, **kwargs):
            # If AI code didn't provide feedback, inject our UI feedback if available in scope
            if feedback is None and 'processing_feedback' in self._scope:
                feedback = self._scope['processing_feedback']
            
            self._calls_seen += 1
            self._maybe_update(f"Processing step {self._calls_seen}…")
            try:
                res = self._safe_run(real_run, alg_id, params, context=context, feedback=feedback)
                self._calls_done += 1
                self._maybe_update(f"Step {self._calls_done} complete")
                if self._calls_done == self._calls_seen:
                    self._maybe_update("Analysis complete", progress=100)
                return res
            except Exception:
                self._maybe_update("Processing failed")
                raise
        return _wrapped
    @staticmethod
    def _safe_run(real_run, alg_id, params, context=None, feedback=None):
        try:
            return real_run(alg_id, params, context=context, feedback=feedback, is_child_algorithm=False)
        except TypeError:
            pass
        try:
            return real_run(alg_id, params, context=context, feedback=feedback, is_child=False)
        except TypeError:
            pass
        return real_run(alg_id, params, context=context, feedback=feedback)


class QgsMessageLogCapture(QObject):
    def __init__(self):
        super().__init__()
        self.messages = []
        self._connected = False
    
    def start(self):
        if not self._connected:
            QgsApplication.messageLog().messageReceived.connect(self._on_message)
            self._connected = True
        self.messages = []
    
    def stop(self):
        if self._connected:
            try:
                QgsApplication.messageLog().messageReceived.disconnect(self._on_message)
            except:
                pass
            self._connected = False
    
    def _on_message(self, message, tag, level):
        level_str = ["INFO", "WARNING", "CRITICAL"][level] if level < 3 else "UNKNOWN"
        self.messages.append(f"[{tag}:{level_str}] {message}")
    
    def get_messages(self, last_n=30):
        return '\n'.join(self.messages[-last_n:])
    
    def get_errors_only(self):
        return '\n'.join([m for m in self.messages if 'WARNING' in m or 'CRITICAL' in m])


class QueryGIS(QObject):
    def __init__(self, iface_obj):
        super().__init__()
        self.iface = iface_obj
        self.plugin_dir = os.path.dirname(__file__)
        self.menu = self.tr(u'&QueryGIS')
        self.actions = []
        self.dockwidget = None
        self.chat_history = []
        self._previous_turn = None
        self._request_previous_turn = None
        self._request_remembers_turn = False

        self.success_status_color = "#dcefe5"
        self.error_status_color = "#a72f36"
        self.warning_status_color = "#f6e5bd"

        self.ui = None
        self.worker = None
        self.wave_manager = None
        # Terminated risky worker tracking
        self._last_status_update_ms = 0
        self._last_context_text = ""
        self._last_generated_code = ""
        self._current_run_id = None
        self._last_counted_lite_run_id = None
        self._last_soft_error_info = None
        self._request_attempt = 0
        self._request_user_input = ""
        self._request_api_key = ""
        self._request_id_token = None
        self._request_model = "gemini-3-flash-preview"
        self._request_tool_info = ""
        self._request_error_message = ""
        self._last_execution_error_message = ""
        self._last_token_count = None
        self._last_response_mode = ""
        self._tool_request_rounds = 0
        self._last_prompt_full = ""
        self._retry_on_execution_failure = True
        self._retry_on_empty_response = False
        self._last_cache_used = None
        self._execution_advance_triggered = False
        self._pending_attempt_start = False

        self.user_oauth_sub = None
        self._id_token = None
        self._login_confirmation_pending = False
        self._connection_required_after_logout = False
        self.user_info = {}
        self.auth_worker = None
        self._usage_network = QtNetwork.QNetworkAccessManager(self)
        self._usage_reply = None
        self._codex_plan_verified = False
        self._codex_connect_pending = False
        self._codex_connection = CodexConnection(self)

        # UI Bridge for thread-safe/re-entrancy-safe updates
        self.ui_bridge = UiSafeBridge()
        self.ui_bridge.requested.connect(self.update_wave_message, Qt.ConnectionType.QueuedConnection)
    def _send_log_async(self, row_data):
        if not ENABLE_REMOTE_LOG:
            return
        # Fire and forget - do NOT track in a list or connect to finished signals
        # as it causes Access Violation crashes in QGIS during event loop cleanup
        worker = LoggingWorker(REPORT_ENDPOINT, row_data, REPORT_TIMEOUT_SEC)
        worker.finished.connect(worker.deleteLater) # Safe cleanup
        worker.start()

    def _extract_error_summary(self, stdout_output, qgis_errors, qgis_log, original_error=""):
        for line in (stdout_output or "").split('\n'):
            line = line.strip()
            if '❌' in line:
                clean_line = line.replace('❌', '').replace('Error occurred:', '').strip()
                if clean_line:
                    return clean_line
        
        for line in (qgis_errors or "").split('\n'):
            line = line.strip()
            if 'ERROR' in line.upper() and 'WARNING' not in line:
                if 'ERROR' in line:
                    parts = line.split('ERROR', 1)
                    if len(parts) > 1:
                        return f"GDAL Error: {parts[1].strip().lstrip('0123456789: ')}"
        
        if "SOFT_ERROR_DETECTED" not in original_error and original_error:
            lines = original_error.strip().split('\n')
            for line in reversed(lines):
                line = line.strip()
                if line and not line.startswith(' ') and "SOFT_ERROR" not in line:
                    return line
        
        return "An error occurred during code execution"

    def execute_with_self_correction(self, code, scope, user_input, context, retry_count=0):
        if self._codex_connection.cancelled:
            raise QueryRequestDenied("Codex request cancelled.")
        MAX_RETRIES = 2

        log_capture = QgsMessageLogCapture()
        log_capture.start()

        newly_added_layers = []
        def on_layers_added(layers):
            for layer in layers:
                if layer and layer.isValid():
                    newly_added_layers.append(layer.id())

        try:
            QgsProject.instance().layersAdded.connect(on_layers_added)
        except:
            pass 

        try:
            result_cards = scope.get("_querygis_result_cards")
            if result_cards is not None:
                result_cards.clear()
            if retry_count > 0 and isinstance(sys.stdout, io.StringIO):
                sys.stdout.truncate(0)
                sys.stdout.seek(0)

            start_log_pos = 0
            if isinstance(sys.stdout, io.StringIO):
                start_log_pos = sys.stdout.tell()

            if not self._codex_connection.mark_running():
                raise QueryRequestDenied("Codex request cancelled.")
            exec(code, scope)
            
            current_output = ""
            if isinstance(sys.stdout, io.StringIO):
                sys.stdout.seek(start_log_pos)
                current_output = sys.stdout.read()
                sys.stdout.seek(0, io.SEEK_END)

            output_lines = [l.strip() for l in current_output.split('\n') if l.strip()]
            last_meaningful_line = output_lines[-1] if output_lines else ""
            
            success_keywords = ["✓", "Done!", "Success", "Complete", "finished", "successfully"]
            if any(s in last_meaningful_line for s in success_keywords):
                self._last_soft_error_info = None
                return 
            
            fail_keywords = ["❌", "Fail", "Error:", "Exception", "Not found", "Error", "Traceback"]
            has_failure_sign = any(f in last_meaningful_line for f in fail_keywords)
            has_traceback = "Traceback (most recent" in current_output

            if has_failure_sign or has_traceback:
                if newly_added_layers:
                    QgsProject.instance().removeMapLayers(newly_added_layers)
                    newly_added_layers.clear()

                self._last_soft_error_info = {
                    "stdout": current_output,
                    "qgis_errors": log_capture.get_errors_only(),
                    "qgis_log": log_capture.get_messages()
                }
                
                print(f"[SOFT ERROR DETECTED] Retry {retry_count + 1}/{MAX_RETRIES}")
                raise _SoftErrorSignal("Error detected in output")
            
            self._last_soft_error_info = None
            return

        except QueryRequestDenied:
            raise
        except (_SoftErrorSignal, Exception) as e:
            if newly_added_layers:
                QgsProject.instance().removeMapLayers(newly_added_layers)
            
            if retry_count >= MAX_RETRIES:
                qgis_log = log_capture.get_messages()
                qgis_errors = log_capture.get_errors_only()
                
                stdout_output = ""
                if isinstance(sys.stdout, io.StringIO):
                    sys.stdout.seek(0)
                    stdout_output = sys.stdout.read()

                if self._last_soft_error_info:
                    stdout_output = self._last_soft_error_info.get("stdout", stdout_output)
                    qgis_errors = self._last_soft_error_info.get("qgis_errors", qgis_errors)
                
                error_summary = self._extract_error_summary(stdout_output, qgis_errors, qgis_log, str(e))
                
                current_attempt = self._request_attempt
                if current_attempt == 1:
                    if self._advance_attempt(error_summary):
                        self._execution_advance_triggered = True
                        raise Exception("Moving to Attempt 2 after fix failure") from None
                raise Exception(error_summary) from None

            qgis_log = log_capture.get_messages()
            qgis_errors = log_capture.get_errors_only()
            stdout_output = ""
            if isinstance(sys.stdout, io.StringIO):
                sys.stdout.seek(0)
                stdout_output = sys.stdout.read()

            # Limit stdout and qgis log size for the prompt
            limited_stdout = (stdout_output or "")[-500:] if stdout_output and len(stdout_output) > 500 else (stdout_output or "")
            limited_qgis = (qgis_errors or "")[-500:] if qgis_errors and len(qgis_errors) > 500 else (qgis_errors or "")

            full_error_for_ai = f"""=== ERROR DETECTED ===
Error Type: {type(e).__name__}
Message: {str(e)}

=== EXECUTION OUTPUT (Last 500 chars) ===
{limited_stdout}

=== QGIS LOG (Last 500 chars) ===
{limited_qgis}
"""
            error_for_fix = full_error_for_ai

            # Report the failure that triggered self-correction
            try:
                _send_error_report(
                    user_query=user_input,
                    context_text="",
                    generated_code=code,
                    error_message=full_error_for_ai,
                    model_name="gemini-3-flash-preview",
                    phase="execution_failure",
                    metadata={
                        "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                        "run_id": self._current_run_id,
                        "attempt": self._request_attempt or None,
                        "fix_round": retry_count + 1,
                        "is_retry_trigger": True
                    },
                    query_gis_instance=self
                )
            except Exception:
                pass

            broken_lines = code.splitlines()
            if len(broken_lines) > 500:
                broken_code_for_fix = "\n".join(broken_lines[:500])
            else:
                broken_code_for_fix = code
            try:
                fix_ctx = self._collect_qgis_context_active(max_rows=3)
                context_for_fix = self._build_context_text(fix_ctx)
            except Exception:
                context_for_fix = context
            self.update_wave_message(f"Auto-correcting logic... ({retry_count + 1}/{MAX_RETRIES})")

            thinking_strategy = "LOW" if retry_count == 0 else "HIGH"
            
            payload = {
                **self._request_payload_metadata(),
                "context": context_for_fix,
                "user_input": user_input,
                "broken_code": broken_code_for_fix,
                "error_message": error_for_fix,
                "model": "gemini-3-flash-preview",
                "thinking_level": thinking_strategy
            }

            try:
                response = requests.post(FIX_ENDPOINT, json=payload, headers=self._request_auth_headers(),
                                         timeout=150, allow_redirects=False)
                self._check_fix_authorization(response)
                
                if response.status_code == 200:
                    data = response.json()
                    if "output" in data and "text" in data["output"]:
                        fixed_code = data["output"]["text"]
                        token_count = None
                        try:
                            token_count = data.get("token_count")
                        except Exception:
                            token_count = None
                        
                        if "from qgis.core import" not in fixed_code:
                            fixed_code = self._prepend_runtime_imports(fixed_code)

                        try:
                            _send_error_report(
                                user_query=user_input,
                                context_text="",
                                generated_code=fixed_code,
                                error_message="[FIX_CODE] Code fixed.",
                                model_name="gemini-3-flash-preview",
                                phase="fix_code",
                                metadata={
                                    "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                                    "run_id": self._current_run_id,
                                    "attempt": self._request_attempt or None,
                                    "fix_round": retry_count + 1,
                                    "token_count": token_count
                                },
                                query_gis_instance=self
                            )
                        except Exception:
                            pass
                        
                        return self.execute_with_self_correction(
                            fixed_code, scope, user_input, context, retry_count + 1
                        )
                
                raise Exception("Fix server request failed")
                
            except QueryRequestDenied:
                raise
            except Exception as req_err:
                current_attempt = self._request_attempt
                if current_attempt == 1:
                    if self._advance_attempt(f"Fix request failed: {str(req_err)}"):
                        self._execution_advance_triggered = True
                        raise Exception("Fix failed; moving to Attempt 2") from None
                
                raise Exception(f"Recovery failed: {str(req_err)}") from None

        finally:
            log_capture.stop()
            try:
                QgsProject.instance().layersAdded.disconnect(on_layers_added)
            except:
                pass

    def start_wave_progress(self, message="Processing"):
        if not self.ui:
            return
            
        # Lock the layer list interaction
        if self.iface and self.iface.layerTreeView():
            self.iface.layerTreeView().setEnabled(False)
            
        if not self.wave_manager:
            self.wave_manager = WaveProgressManager(self._update_wave_ui, self._update_eta_ui)
        if not self.ui.progressBar.isVisible():
            self.ui.progressBar.setVisible(True)
        self.ui.progressBar.setRange(0, 0)
        
        # Change cursor to wait during request
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        
        self.wave_manager.start_wave(message)

    def update_wave_message(self, message, progress=None):
        if not self.wave_manager:
            return
        
        now = int(time.time() * 1000)

        if progress is not None:
            if self.ui:
                if self.ui.progressBar.minimum() != 0 or self.ui.progressBar.maximum() != 100:
                    self.ui.progressBar.setRange(0, 100)
                self.ui.progressBar.setValue(int(progress))
            # If progress is provided, we can skip the rate limit to ensure smooth display
        elif now - self._last_status_update_ms < 120:
            return
            
        self._last_status_update_ms = now
        self.wave_manager.update_message(str(message))

    def stop_wave_progress(self, final_message="Complete"):
        # Unlock the layer list interaction
        if self.iface and self.iface.layerTreeView():
            self.iface.layerTreeView().setEnabled(True)
            
        if self.wave_manager:
            self.wave_manager.stop_wave(final_message)
        if self.ui:
            self.ui.progressBar.setRange(0, 100)
            self.ui.progressBar.setValue(100)
            QTimer.singleShot(600, self.hide_progress)

    def _update_wave_ui(self, message, is_final):
        if not self.ui:
            return
        if self.ui.status_label.text() != message:
            self.ui.status_label.setText(message)
        if is_final:
            self.ui.progressBar.setRange(0, 100)
            self.ui.progressBar.setValue(100)
            # Restore cursor when final
            while QApplication.overrideCursor():
                QApplication.restoreOverrideCursor()

    def _update_eta_ui(self, message, visible):
        if not self.ui:
            return
        if self.ui.etaLabel.text() != message:
            self.ui.etaLabel.setText(message)
        if self.ui.etaLabel.isVisible() != visible:
            self.ui.etaLabel.setVisible(visible)

    def hide_progress(self):
        if self.wave_manager:
            self.wave_manager.stop_wave()
        if self.ui:
            self.ui.progressBar.setVisible(False)
            self.ui.progressBar.setValue(0)
            if self.ui.status_label.text() != "Status: Ready":
                self.ui.status_label.setText("Status: Ready")
                self.ui.status_label.setStyleSheet("")

    def tr(self, message):
        return QCoreApplication.translate('QueryGIS', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True, status_tip=None,
                   whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action_class = getattr(QtGui, "QAction", None) or QtWidgets.QAction
        action = action_class(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)
        if status_tip is not None:
            action.setStatusTip(status_tip)
        if whats_this is not None:
            action.setWhatsThis(whats_this)
        if add_to_toolbar:
            self.iface.addToolBarIcon(action)
        if add_to_menu:
            self.iface.addPluginToMenu(self.menu, action)
        self.actions.append(action)
        return action

    def initGui(self):
        QgsProject.instance().cleared.connect(self._clear_conversation)
        icon_path = ':/plugins/query_gis/icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'QueryGIS'),
            callback=self.run,
            parent=self.iface.mainWindow()
        )

    def unload(self):
        QgsProject.instance().cleared.disconnect(self._clear_conversation)
        self._stop_codex_connection()
        self._cancel_subscription_usage()
        if self.auth_worker and self.auth_worker.isRunning():
            self.auth_worker.requestInterruption()
            self.auth_worker.wait(15000)
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.quit()
            self.worker.wait(5000)
        
        if self.dockwidget:
            self.iface.removeDockWidget(self.dockwidget)
            self.dockwidget.deleteLater()
            self.dockwidget = None
        for action in self.actions:
            self.iface.removePluginMenu(self.tr(u'&QueryGIS'), action)
            self.iface.removeToolBarIcon(action)
        self.actions = []

    def run(self):
        if not self.dockwidget:
            self.dockwidget = QDockWidget("QueryGIS (Backend)", self.iface.mainWindow())
            self.dockwidget.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self.dockwidget.setObjectName("QueryGISDockWidget")
            self.dockwidget.destroyed.connect(self._on_dockwidget_destroyed)

            self.ui = Ui_DockWidget()
            font_size = QSettings().value("QueryGIS/font_size", "M", type=str)
            self.ui.setupUi(self.dockwidget, font_size)

            self.ui.btn_ask.clicked.connect(self.process_query)
            self.ui.btn_google_login.clicked.connect(self.handle_google_login)
            self.ui.btn_connection_google.clicked.connect(self.handle_google_login)
            self.ui.btn_usage_refresh.clicked.connect(lambda: self.refresh_subscription_usage())
            self.ui.btn_codex.clicked.connect(self._toggle_codex_connection)
            self.ui.btn_start_querygis.clicked.connect(self._start_querygis)
            self.ui.btn_save_key.clicked.connect(lambda: self._save_settings_key(self.ui.line_apikey))
            self.ui.btn_use_enterprise_key.clicked.connect(lambda: self._save_settings_key(self.ui.line_enterprise_key))
            self.ui.font_sizes.buttonClicked.connect(lambda button: self._save_font_size(button.text()))
            self.ui.text_query.installEventFilter(self)

            self.ui.set_api_key(self.load_api_key())
            self._refresh_auth_ui()
            if self._id_token:
                self.refresh_subscription_usage()

            # Refresh and Connect Layer List signals
            self.refresh_layer_list()
            QgsProject.instance().layersAdded.connect(self.refresh_layer_list)
            QgsProject.instance().layersRemoved.connect(self.refresh_layer_list)
            self.iface.currentLayerChanged.connect(self.refresh_layer_list)
            
            # Connect to selection model for multi-selection updates
            tree_view = self.iface.layerTreeView()
            if tree_view and tree_view.selectionModel():
                tree_view.selectionModel().selectionChanged.connect(self.refresh_layer_list)

            self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dockwidget)

        self.dockwidget.show()

    def _on_dockwidget_destroyed(self):
        self._codex_plan_verified = False
        self._stop_codex_connection()
        self._cancel_subscription_usage()
        if self.auth_worker and self.auth_worker.isRunning():
            self.auth_worker.requestInterruption()
        self.dockwidget = None
        self.ui = None

    def eventFilter(self, obj, event):
        if (self.ui and obj == self.ui.text_query and event.type() == QEvent.Type.KeyPress):
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() in (
                Qt.KeyboardModifier.ControlModifier, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.KeypadModifier
            ):
                self.process_query()
                return True
        return super().eventFilter(obj, event)

    def _collect_field_samples(self, vlayer, max_samples=3, scan_limit=50):
        fields_info = []
        
        field_samples = {}
        for field in vlayer.fields():
            field_samples[field.name()] = {
                "name": field.name(),
                "type": field.typeName(),
                "samples": set()
            }
        
        scan_count = 0
        for feature in vlayer.getFeatures():
            if scan_count >= scan_limit:
                break
            
            for field_name, field_info in field_samples.items():
                if len(field_info["samples"]) < max_samples:
                    value = feature[field_name]
                    if value is not None and value != '':
                        field_info["samples"].add(str(value)[:100])
            
            scan_count += 1
    
        for field_name in field_samples:
            field_samples[field_name]["samples"] = list(field_samples[field_name]["samples"])
        
        return list(field_samples.values())

    def _clear_conversation(self):
        self._previous_turn = None
        self._request_previous_turn = None
        self._request_remembers_turn = False

    def _remember_turn(self, result):
        if self._request_remembers_turn:
            self._previous_turn = {
                "user": _conversation_excerpt(self._request_user_input, 120),
                "result": _conversation_excerpt(result, 80),
            }

    def append_chat_message(self, role, message):
        if not self.ui:
            return
        sa = self.ui.chatScrollArea
        sa.setUpdatesEnabled(False)
        sa.viewport().setUpdatesEnabled(False)
        container = self.ui.chatLayout.parentWidget()
        if container:
            container.setUpdatesEnabled(False)
        try:
            self.chat_history.append({"role": role, "content": message.title if role == "assistant-result" else message})
            self._codex_connection.add_message(role, message)
            self.ui.empty_chat.hide()
            msg_widget = self.ui.create_message(role, message)
            self.ui.chatLayout.insertWidget(self.ui.chatLayout.count() - 1, msg_widget)
        finally:
            if container:
                container.setUpdatesEnabled(True)
            sa.viewport().setUpdatesEnabled(True)
            sa.setUpdatesEnabled(True)
        self.ui.chatLayout.activate()
        self.ui.scroll_to_latest(keep_question_visible=(role != "user"))

    def refresh_layer_list(self):
        if not self.ui or not hasattr(self.ui, 'layerListLayout'):
            return
            
        # Clear existing widgets (keeping the spacer at the end)
        layout = self.ui.layerListLayout
        while layout.count() > 1:
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        
        layers = []
        try:
            # Use layerTreeView to get all selected layers (multi-selection support)
            layers = self.iface.layerTreeView().selectedLayers()
        except Exception:
            # Fallback to single active layer if tree view fails
            active_layer = self.iface.activeLayer()
            if active_layer:
                layers = [active_layer]
        
        self.ui.empty_layers.setVisible(not layers)

        for lyr in layers:
            lyr_frame = QWidget()
            lyr_frame.setObjectName("layerRow")
            lyr_layout = QHBoxLayout(lyr_frame)
            lyr_layout.setContentsMargins(8, 4, 8, 4)
            lyr_layout.setSpacing(8)
            
            # Type Icon Label
            source = lyr.source().lower()
            provider = ""
            if hasattr(lyr, "dataProvider") and lyr.dataProvider():
                provider = lyr.dataProvider().name().lower()
            
            is_table = any(ext in source for ext in [".csv", ".xlsx", ".xls", ".txt"]) or provider == "delimitedtext"
            type_symbol = "📊" if is_table else {
                "vector": "🟦", "raster": "🟩", "pointcloud": "☁️",
            }.get(_layer_kind(lyr), "📁")
            
            type_label = QLabel(type_symbol)
            lyr_layout.addWidget(type_label)
            
            # Layer Name Label (Static)
            name_label = QLabel(lyr.name())
            name_label.setTextFormat(Qt.TextFormat.PlainText)
            name_label.setWordWrap(True)
            lyr_layout.addWidget(name_label, 1)
            
            layout.insertWidget(layout.count() - 1, lyr_frame)

    def _prepend_runtime_imports(self, raw_code: str) -> str:
        pre = [
            "from qgis.core import *",
            "from qgis.gui import *",
            "from qgis.analysis import *",
            "import processing",
            "from qgis.utils import iface",
            "import tempfile, os, random",
            ""
        ]
        return "\n".join(pre) + raw_code

    def handle_response(self, response_text: str):
        if self._finish_cancelled_codex_request():
            return
        self._last_token_count = None
        self._last_response_mode = ""
        self._last_prompt_full = ""
        tool_request = None
        show_lite_notice = False
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                if data.get("error") == "subscription_required":
                    self.handle_error(data["output"]["text"])
                    return
                if "cache_used" in data:
                    try:
                        self._last_cache_used = bool(data.get("cache_used"))
                    except Exception:
                        self._last_cache_used = None
                if "token_count" in data:
                    try:
                        self._last_token_count = int(data.get("token_count"))
                    except Exception:
                        self._last_token_count = None
                self._last_response_mode = str(data.get("mode") or "")
                tool_request = data.get("tool_request")
                show_lite_notice = data.get("lite_plan_notice") is True
                if "prompt_full" in data and isinstance(data.get("prompt_full"), str):
                    self._last_prompt_full = data.get("prompt_full")
        except Exception:
            pass
        if tool_request and self._handle_tool_request(tool_request):
            try:
                last_user = ""
                for m in reversed(self.chat_history):
                    if m.get("role") == "user":
                        last_user = m.get("content", "")
                        break
                _send_error_report(
                    user_query=last_user,
                    context_text="",
                    generated_code="",
                    error_message="[TOOL_REQUEST] Client info requested.",
                    model_name="gemini-3-flash-preview",
                    phase="tool_request",
                    metadata={
                        "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                        "run_id": self._current_run_id,
                        "attempt": self._request_attempt or None,
                        "mode": self._last_response_mode or None,
                        "token_count": self._last_token_count,
                        "prompt_full": self._last_prompt_full or None,
                        "tool_request": tool_request
                    },
                    query_gis_instance=self
                )
            except Exception:
                pass
            return
        if tool_request and self._codex_connection.active_task:
            self.handle_error("Could not provide the context requested by QueryGIS.")
            return

        display_text, code_blocks = self._parse_backend_response(response_text)
        completed = bool(display_text.strip())
        executed = False

        chosen = None
        if code_blocks:
            filtered = [b.strip() for b in code_blocks if b and b.strip()]
            if filtered:
                chosen = filtered[-1]
                self._last_generated_code = chosen
                try:
                    last_user = ""
                    for m in reversed(self.chat_history):
                        if m.get("role") == "user":
                            last_user = m.get("content", "")
                            break
                    _send_error_report(
                        user_query=last_user,
                        context_text="",
                        generated_code=chosen,
                        error_message=f"[AI_CODE_LEN={len(chosen)}] Code received.",
                        model_name="gemini-3-flash-preview",
                        phase="ai_answer",
                        metadata={
                            "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                            "run_id": self._current_run_id,
                            "attempt": self._request_attempt or None,
                            "mode": self._last_response_mode or None,
                            "token_count": self._last_token_count,
                            "cache_used": self._last_cache_used,
                            "prompt_full": self._last_prompt_full or None
                        },
                        query_gis_instance=self
                    )
                except Exception:
                    pass

                self.append_chat_message("assistant", chosen)
                if self._codex_connection.task_mode == "ask":
                    self.append_chat_message("assistant-print", self.tr("Code prepared. Open Review Code in QueryGIS to inspect it."))
                else:
                    if self._finish_cancelled_codex_request():
                        return
                    self.start_wave_progress("Executing code")
                    final_code = self._prepend_runtime_imports(chosen)
                    try:
                        completed = self.run_code_string(final_code)
                    except QueryRequestDenied as denied:
                        self.handle_error(str(denied))
                        return
                    executed = completed
                    if self._finish_cancelled_codex_request(executed=executed):
                        return
                    if not completed and self._retry_on_execution_failure:
                        if self._execution_advance_triggered:
                            self._execution_advance_triggered = False
                            return
                        if self._advance_attempt(self._last_execution_error_message or "Execution failed"):
                            return
            else:
                if (not display_text.strip()) and self._retry_on_empty_response:
                    if self._advance_attempt("Empty response from server"):
                        return
                self.append_chat_message("assistant-print", display_text.strip())
        else:
            if (not display_text.strip()) and self._retry_on_empty_response:
                if self._advance_attempt("Empty response from server"):
                    return
            self.append_chat_message("assistant-print", display_text.strip())

        if completed and chosen is None:
            self._remember_turn(display_text)
        if completed and show_lite_notice:
            self._maybe_show_lite_plan_notice()
        self._codex_connection.finish(completed, executed=executed,
                                      error="" if completed else self._last_execution_error_message)

        if self.ui:
            self.ui.status_label.setText("Intelligence received")
            self.ui.status_label.setStyleSheet(f"background-color: {self.success_status_color}; color: black;")
            self.stop_wave_progress("Finished")
            self.ui.btn_ask.setEnabled(True)
        if self._pending_attempt_start:
            self._pending_attempt_start = False
            return
        if self.worker and self.worker.isRunning():
            return
        self._request_attempt = 0

    def _maybe_show_lite_plan_notice(self):
        if not self.ui or not self.user_oauth_sub or not self._request_id_token or self._request_id_token != self._id_token:
            return
        if not self._current_run_id or self._last_counted_lite_run_id == self._current_run_id:
            return
        self._last_counted_lite_run_id = self._current_run_id

        account_key = hashlib.sha256(self.user_oauth_sub.encode("utf-8")).hexdigest()
        key = f"QueryGIS/lite_notice_count/{account_key}"
        settings = QSettings()
        count = (settings.value(key, 0, type=int) + 1) % 3
        settings.setValue(key, count)
        if count:
            return
        messages = (
            self.tr("The Lite model can miss details in complex requests. Upgrade to a more capable model for better results."),
            self.tr("GIS data can be challenging for AI to interpret. Upgrade to a model that can better understand your layers and spatial data."),
            self.tr("Higher-tier plans include higher monthly request limits. Upgrade to run more analyses each month."),
            self.tr("Working on a multi-step QGIS task? Upgrade for better support with workflows that combine several operations."),
            self.tr("Working with multiple layers or coordinate systems? Upgrade to a more capable model for help with the details that matter."),
        )
        self.append_chat_message("assistant-lite-plan", random.choice(messages))

    def handle_error(self, error_message: str):
        if self._finish_cancelled_codex_request():
            return
        self._codex_connection.finish(False, error=error_message)
        if not self.ui:
            return
        msg = str(error_message).strip() or "Unknown error"
        self.append_chat_message("assistant-print", f"Error:\n{msg}")
        self.ui.status_label.setText("Request failed")
        self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
        self.stop_wave_progress("Error")
        self.ui.btn_ask.setEnabled(True)
        self._request_attempt = 0

    def _build_context_text(self, ctx: dict) -> str:
        try:
            trimmed = {"project": ctx.get("project", {}), "layers": []}
            for li in ctx.get("layers", []):
                li2 = dict(li)
                if isinstance(li2.get("fields"), list) and li2["fields"]:
                    if not isinstance(li2["fields"][0], dict) and len(li2["fields"]) > 20:
                        li2["fields"] = li2["fields"][:20] + ["..."]
                trimmed["layers"].append(li2)
            return json.dumps(trimmed, ensure_ascii=False, default=str)
        except Exception:
            try:
                fallback = {"project": self._collect_project_metadata(), "layers": []}
                return json.dumps(fallback, ensure_ascii=False, default=str)
            except Exception:
                return ""

    def _build_backend_payload(self, mode, context_text="", tool_info="", error_message="", tool_request=None, tool_data=None):
        payload = {
            **self._request_payload_metadata(),
            "context": context_text or "",
            "user_input": self._request_user_input,
            "model": self._request_model,
            "mode": mode,
            "version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
        }
        if tool_info:
            payload["tool_info"] = tool_info
        if error_message:
            payload["error_message"] = error_message
        if tool_request:
            payload["tool_request"] = tool_request
        if tool_data:
            payload["tool_data"] = tool_data
        return payload

    def _start_backend_attempt(self, mode, context_text="", tool_info="", error_message="", tool_request=None, tool_data=None):
        if self._finish_cancelled_codex_request():
            return
        self._codex_connection.mark_planning()
        payload = self._build_backend_payload(
            mode=mode,
            context_text=context_text,
            tool_info=tool_info,
            error_message=error_message,
            tool_request=tool_request,
            tool_data=tool_data
        )

        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.worker.quit()
            self.worker.wait(3000)

        self.worker = BackendWorker(payload, timeout_sec=120, headers=self._request_auth_headers())
        self.worker.step_update.connect(self.update_wave_message)
        self.worker.finished.connect(self.handle_response)
        self.worker.error.connect(self.handle_error)
        self.worker.auth_expired.connect(self._handle_expired_login)
        self.worker.start()

    def _advance_attempt(self, reason):
        """재시도 전략: 2단계로 축소 (info_light 제거)"""
        
        # ========== 최대 2회로 제한 ==========
        if self._request_attempt >= 2:
            return False

        self._request_attempt += 1
        self._request_error_message = reason or ""

        if self._request_attempt == 2:
            self.update_wave_message("Retrying with full context + RAG (2/2)")
            
            context_dict = self._collect_qgis_context()
            context_text = self._build_context_text(context_dict)
            self._last_context_text = context_text
            
            if not self._request_tool_info:
                self._request_tool_info = self._collect_tool_info()
            
            self._pending_attempt_start = True
            
            try:
                _send_error_report(
                    user_query=self._request_user_input,
                    context_text="",
                    generated_code="",
                    error_message=f"Advancing to Attempt 2: {self._request_error_message}",
                    model_name=self._request_model,
                    phase="attempt_start",
                    metadata={
                        "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                        "run_id": self._current_run_id,
                        "attempt": 2,
                        "mode": "rag_full"
                    },
                    query_gis_instance=self
                )
            except Exception:
                pass
            
            self._start_backend_attempt(
                mode="rag_full",
                context_text=context_text,
                tool_info=self._request_tool_info,
                error_message=self._request_error_message
            )
            return True

        return False
    
    def _call_syntax_fixer(self, broken_code, error_message, user_input, context):
        """Syntax Error 전용 Fix 서버 호출"""
        payload = {
            **self._request_payload_metadata(),
            "context": context,
            "user_input": user_input,
            "broken_code": broken_code[:2000],  # 길이 제한
            "error_message": f"SYNTAX ERROR:\n{error_message}",
            "model": "gemini-3-flash-preview",
            "thinking_level": "HIGH"
        }
        
        try:
            self.update_wave_message("Fixing syntax error...")
            response = requests.post(FIX_ENDPOINT, json=payload, headers=self._request_auth_headers(),
                                     timeout=60, allow_redirects=False)
            self._check_fix_authorization(response)
            
            if response.status_code == 200:
                data = response.json()
                if "output" in data and "text" in data["output"]:
                    fixed_code = data["output"]["text"]
                    
                    # Import 추가
                    if "from qgis.core import" not in fixed_code:
                        fixed_code = self._prepend_runtime_imports(fixed_code)
                    
                    # 로그 전송
                    try:
                        _send_error_report(
                            user_query=user_input,
                            context_text="",
                            generated_code=fixed_code,
                            error_message="[SYNTAX_FIX] Code fixed.",
                            model_name="gemini-3-flash-preview",
                            phase="syntax_fix",
                            metadata={
                                "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                                "run_id": self._current_run_id,
                                "attempt": self._request_attempt or None,
                                "token_count": data.get("token_count")
                            },
                            query_gis_instance=self
                        )
                    except Exception:
                        pass
                    
                    return fixed_code
            
            return None
        
        except QueryRequestDenied:
            raise
        except Exception as e:
            print(f"[SYNTAX FIX ERROR] {e}")
            return None

    def _wrap_return_if_needed(self, code_string: str):
        try:
            compile(code_string, "<string>", "exec")
            return code_string, False
        except SyntaxError as e:
            if "'return' outside function" not in str(e):
                raise

            lines = code_string.splitlines()
            import_re = re.compile(r"^(from\s+[\w\.]+\s+import\b.*|import\s+.+)")

            header = []
            body = []

            paren_depth = 0
            in_import_block = False
            for line in lines:
                stripped = line.lstrip()

                if in_import_block:
                    header.append(line.lstrip())
                    paren_depth += line.count("(") - line.count(")")
                    if paren_depth <= 0:
                        in_import_block = False
                    continue

                if import_re.match(stripped):
                    header.append(line.lstrip())
                    paren_depth = line.count("(") - line.count(")")
                    in_import_block = paren_depth > 0
                    continue

                body.append(line)

            if not body:
                body = [line for line in lines if not import_re.match(line.strip())]
                header = [line for line in lines if import_re.match(line.strip())]

            indented = []
            for line in body:
                if line.strip():
                    indented.append("    " + line)
                else:
                    indented.append("")

            wrapped = (
                "\n".join(header)
                + ("\n" if header else "")
                + "def __auto_main__():\n"
                + "\n".join(indented)
                + "\n__auto_ret__ = __auto_main__()\n"
                + "if __auto_ret__ is not None:\n"
                + "    print(__auto_ret__)\n"
            )

            compile(wrapped, "<string>", "exec")
            return wrapped, True

    def run_code_string(self, code_string):
        if not self.ui:
            return False

        self.start_wave_progress("Preparing code execution")
        self._last_execution_error_message = ""
        
        if "processing.run" in code_string:
            code_string = self._inject_processing_feedback(code_string)
        
            try:
                code_string, _ = self._wrap_return_if_needed(code_string)
            except SyntaxError as e:
                err_msg = f"Syntax error before execution: {e}"
                self._last_execution_error_message = err_msg
                
                if self._retry_on_execution_failure:
                    if self._request_attempt == 1:
                        if self._advance_attempt(err_msg):
                            self._execution_advance_triggered = True
                            if self.ui:
                                self.ui.status_label.setStyleSheet(f"background-color: {self.warning_status_color}; color: black;")
                            return False
                    if self._request_attempt <= 2:
                        try:
                            fixed_code = self._call_syntax_fixer(
                                code_string, 
                                err_msg,
                                self._request_user_input,
                                self._last_context_text
                            )
                            if fixed_code:
                                print(f"[SYNTAX FIX] Retrying with fixed code")
                                return self.run_code_string(fixed_code)
                        except QueryRequestDenied:
                            raise
                        except Exception as fix_err:
                            print(f"[SYNTAX FIX FAILED] {fix_err}")
                
                self.append_chat_message("assistant-print", err_msg)
                if self.ui:
                    self.ui.status_label.setText("Execution failed")
                    self.ui.status_label.setStyleSheet(
                        f"background-color: {self.error_status_color}; color: white;"
                    )
                self.stop_wave_progress("Analysis failed")
                return False
        
        main_buffer = io.StringIO()
        original_stdout = sys.stdout
        start_time = time.time()
        
        try:
            sys.stdout = main_buffer
            scope = self.get_execution_scope()
            
            last_user_input = ""
            for m in reversed(self.chat_history):
                if m.get("role") == "user":
                    last_user_input = m.get("content", "")
                    break
            
            current_context = self._last_context_text or "{}"
            
            self.execute_with_self_correction(
                code_string, scope, last_user_input, current_context
            )
            
            final_output = main_buffer.getvalue().strip()
            elapsed = time.time() - start_time
            
            self.ui.status_label.setText("Success!")
            self.ui.status_label.setStyleSheet(
                f"background-color: {self.success_status_color}; color: black;"
            )
            
            _send_error_report(
                user_query=last_user_input,
                context_text="",
                generated_code=code_string,
                error_message=("SUCCESS" + (f"\nPRINT:\n{final_output}" if final_output else "")),
                model_name="gemini-3-flash-preview",
                phase="execution_result",
                metadata={
                    "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                    "run_id": self._current_run_id,
                    "elapsed_sec": elapsed,
                    "attempt": self._request_attempt or None,
                    "mode": self._last_response_mode or None,
                    "token_count": self._last_token_count
                },
                query_gis_instance=self
            )
            
            result_cards = scope["_querygis_result_cards"]
            for result in result_cards:
                self.append_chat_message("assistant-result", result)
            if final_output:
                self.append_chat_message("assistant-print", f"Output:\n{final_output}")
            
            self.stop_wave_progress("Task Complete")
            self._add_execution_result_to_chat(True, elapsed)
            result_title = result_cards[0].title if result_cards else ""
            self._remember_turn(result_title or final_output or "Execution completed.")
            return True
        
        except QueryRequestDenied:
            raise
        except Exception as e:
            elapsed = time.time() - start_time
            tb_text = traceback.format_exc()
            partial_output = main_buffer.getvalue().strip()
            self._last_execution_error_message = tb_text
            
            # If we just triggered a major attempt advance, don't show error UI yet
            if self._execution_advance_triggered:
                if self.ui:
                    self.ui.status_label.setText("Scaling context & Retrying...")
                    self.ui.status_label.setStyleSheet(f"background-color: {self.warning_status_color}; color: black;")
                return False

            if self.ui:
                self.ui.status_label.setText("Execution failed")
                self.ui.status_label.setStyleSheet(
                    f"background-color: {self.error_status_color}; color: white;"
                )
            
            error_display = f"Error:\n{tb_text}"
            if partial_output:
                error_display += f"\n\nPartial output:\n{partial_output}"
            
            self.append_chat_message("assistant-print", error_display)
            
            _send_error_report(
                user_query=last_user_input,
                context_text="",
                generated_code=code_string,
                error_message=tb_text,
                model_name="gemini-3-flash-preview",
                phase="execution_result",
                metadata={
                    "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                    "run_id": self._current_run_id,
                    "elapsed_sec": elapsed,
                    "final_error": True,
                    "partial_output": partial_output[:500] if partial_output else None,
                    "attempt": self._request_attempt or None,
                    "mode": self._last_response_mode or None,
                    "token_count": self._last_token_count
                },
                query_gis_instance=self
            )
            
            self.stop_wave_progress("Analysis failed")
            self._add_execution_result_to_chat(False, elapsed)
            return False
        
        finally:
            sys.stdout = original_stdout
            main_buffer.close()

    def get_execution_scope(self):
        result_cards = []

        def show_result(title, columns, rows, *, chart=None, view=None, chart_type="bar"):
            result_cards.append(ResultCardData(title, columns, rows, chart=chart, view=view, chart_type=chart_type))

        scope = {
            'iface': self.iface,
            'qgis': sys.modules['qgis'],
            'QgsProject': QgsProject,
            'QgsMapLayer': QgsMapLayer,
            'QgsVectorLayer': QgsVectorLayer,
            'QgsRasterLayer': QgsRasterLayer,
            'QgsApplication': QgsApplication,
            'QgsProcessingFeatureSourceDefinition': QgsProcessingFeatureSourceDefinition,
            'QgsFeatureSink': QgsFeatureSink,
            'QgsFeatureRequest': QgsFeatureRequest,
            'QgsGeometry': QgsGeometry,
            'QgsPointXY': QgsPointXY,
            'QgsRectangle': QgsRectangle,
            'QgsCoordinateReferenceSystem': QgsCoordinateReferenceSystem,
            'QgsVectorFileWriter': QgsVectorFileWriter,
            'QgsField': QgsField,
            'QgsFeature': QgsFeature,
            'QgsFillSymbol': QgsFillSymbol,
            'QgsSymbol': QgsSymbol,
            'QgsSingleSymbolRenderer': QgsSingleSymbolRenderer,
            'QgsCategorizedSymbolRenderer': QgsCategorizedSymbolRenderer,
            'QgsRendererCategory': QgsRendererCategory,
            'QgsPalLayerSettings': QgsPalLayerSettings,
            'QgsTextFormat': QgsTextFormat,
            'QgsTextBufferSettings': QgsTextBufferSettings,
            'QgsVectorLayerSimpleLabeling': QgsVectorLayerSimpleLabeling,
            'QgsProperty': QgsProperty,
            'QgsWkbTypes': QgsWkbTypes,
            'QVariant': QVariant,
            'QMetaType': QMetaType,
            'tempfile': tempfile,
            'os': os,
            'processing': sys.modules.get('processing'),
            '__builtins__': builtins,
            'find_layer_by_keyword': self.find_layer_by_keyword,
            'get_layer_safe': self.get_layer_safe,
            'shorten_layer_name': self.shorten_layer_name
        }
        scope.update(show_result=show_result, _querygis_result_cards=result_cards)
        
        # Prepare safely-wrapped processing environment
        scope['processing_feedback'] = _UIFeedback(self.ui_bridge, label="Processing...")
        proc_mod = scope['processing']
        if proc_mod and hasattr(proc_mod, 'run'):
            proxy = _RunProgressProxy(self.ui_bridge, scope=scope)
            try:
                scope['_orig_processing_run'] = proc_mod.run
                proc_mod.run = proxy.wrap(proc_mod.run)
            except Exception as e:
                logger.warning(f"Failed to wrap processing.run: {e}")
        
        wrapped_scope = auto_wrap_scope(scope)

        return SmartQgsImporter(wrapped_scope)

    def _inject_processing_feedback(self, code_string: str) -> str:
        out_lines = []
        for line in code_string.splitlines():
            tmp, in_s, q = [], False, ''
            for ch in line:
                if ch in ('"', "'"):
                    if not in_s:
                        in_s, q = True, ch
                    elif q == ch:
                        in_s = False
                tmp.append(ch)
            safe_line = ''.join(tmp)
            if 'processing.run(' in safe_line and 'feedback=' not in safe_line:
                i = safe_line.index('processing.run(') + len('processing.run(')
                depth, j = 1, i
                while j < len(safe_line) and depth > 0:
                    if safe_line[j] == '(':
                        depth += 1
                    elif safe_line[j] == ')':
                        depth -= 1
                    j += 1
                insert_at = line.rfind(')', 0, j)
                if insert_at != -1:
                    line = line[:insert_at] + (', feedback=processing_feedback') + line[insert_at:]
            out_lines.append(line)
        return '\n'.join(out_lines)

    def find_layer_by_keyword(self, keyword):
        project = QgsProject.instance()

        exact_layers = project.mapLayersByName(keyword)
        if exact_layers:
            return exact_layers[0]

        matching_layers = []
        for layer in project.mapLayers().values():
            if keyword.lower() in layer.name().lower():
                matching_layers.append(layer)
        if matching_layers:
            return matching_layers[0]

        keywords = keyword.replace('_', ' ').split()
        for layer in project.mapLayers().values():
            layer_name_lower = layer.name().lower()
            if any(kw.lower() in layer_name_lower for kw in keywords):
                return layer

        return None

    def get_layer_safe(self, layer_name):
        layers = QgsProject.instance().mapLayersByName(layer_name)
        if layers:
            return layers[0]

        base_name = os.path.splitext(layer_name)[0]
        layers = QgsProject.instance().mapLayersByName(base_name)
        if layers:
            return layers[0]

        found_layer = self.find_layer_by_keyword(layer_name)
        if found_layer:
            return found_layer

        print(f"Layer '{layer_name}' not found.")
        print("Available layers:")
        for layer in QgsProject.instance().mapLayers().values():
            print(f"  - {layer.name()}")

        return None

    def shorten_layer_name(self, long_name, max_len=50):
        if len(long_name) > max_len:
            return long_name[:max_len-3] + "..."
        return long_name

    def _extract_non_code_text(self, text: str) -> str:
        import re
        pattern = re.compile(r"```(?:python)?\s*([\s\S]*?)```", re.IGNORECASE)
        return pattern.sub("", text).strip()

    def _extract_code_blocks(self, text: str):
        import re
        if not text:
            return []
        code_blocks = []
        fence_pattern = re.compile(r"```(?:python)?\s*([\s\S]*?)```", re.IGNORECASE)
        for m in fence_pattern.finditer(text):
            block = m.group(1).strip()
            if block:
                code_blocks.append(block)
        if code_blocks:
            return code_blocks
        looks_like_code = (
            "\n" in text and (
                "Qgs" in text or
                "processing.run" in text or
                "iface" in text or
                text.lstrip().startswith(("try:", "import ", "from "))
            )
        )
        if not looks_like_code and re.search(r"\b(?:print|show_result)\s*\(", text):
            try:
                looks_like_code = any(
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("print", "show_result") for node in ast.walk(ast.parse(text.strip()))
                )
            except SyntaxError:
                pass
        if looks_like_code:
            code_blocks.append(text.strip())
        return code_blocks

    @staticmethod
    def _gemini_answer_text(value):
        if isinstance(value, str) and value.lstrip().startswith("{"):
            try:
                value = json.loads(value)
            except ValueError:
                return None
        if not isinstance(value, dict) or not isinstance(value.get("candidates"), list):
            return None

        for candidate in value["candidates"]:
            content = candidate.get("content") if isinstance(candidate, dict) else None
            parts = content.get("parts") if isinstance(content, dict) else None
            if not isinstance(parts, list):
                continue
            answer = "".join(
                part["text"] for part in parts
                if isinstance(part, dict) and not part.get("thought") and isinstance(part.get("text"), str)
            )
            if answer.strip():
                return answer
        return ""

    def _parse_backend_response(self, response_text: str):
        try:
            data = json.loads(response_text)
        except ValueError:
            data = response_text

        candidate = None
        if isinstance(data, dict):
            out = data.get("output")
            if isinstance(out, str):
                candidate = out
            elif isinstance(out, dict):
                if "text" in out:
                    candidate = out["text"]
                elif isinstance(out.get("candidates"), list):
                    candidate = out
            if candidate is None:
                candidate = data.get("response")
            if candidate is None:
                candidate = data.get("text")
            choices = data.get("choices")
            if candidate is None and isinstance(choices, list) and choices and isinstance(choices[0], dict):
                choice = choices[0]
                message = choice.get("message")
                candidate = message["content"] if isinstance(message, dict) and "content" in message else choice.get("text")

        gemini_text = self._gemini_answer_text(data if candidate is None else candidate)
        if gemini_text is not None:
            display_text = gemini_text or self.tr("The model did not return an answer.")
        elif candidate is not None:
            display_text = str(candidate)
        else:
            display_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2)

        code_blocks = self._extract_code_blocks(display_text)
        if len(code_blocks) <= 1:
            code = code_blocks[0] if code_blocks else display_text.strip()
            printed_text = self._extract_literal_print_text(code)
            if printed_text is not None:
                prose = self._extract_non_code_text(display_text) if code != display_text.strip() else ""
                return "\n\n".join(part for part in (prose, printed_text) if part), []
        return display_text, code_blocks

    @classmethod
    def _extract_literal_print_text(cls, code: str):
        if "print" not in code:
            return None
        try:
            statements = ast.parse(code).body
        except (SyntaxError, ValueError):
            return None
        bindings = {}
        lines = []
        for statement in statements:
            if isinstance(statement, ast.Assign):
                if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
                    return None
                name = statement.targets[0].id
                value = cls._literal_output_value(statement.value, bindings)
                if name == "print" or value is None:
                    return None
                bindings[name] = value
                continue

            if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
                return None
            call = statement.value
            if not isinstance(call.func, ast.Name) or call.func.id != "print" or call.keywords:
                return None
            args = [cls._literal_output_value(arg, bindings) for arg in call.args]
            if not all(isinstance(arg, str) for arg in args):
                return None
            lines.append(" ".join(args))
        return "\n".join(lines) if lines else None

    @classmethod
    def _literal_output_value(cls, node, bindings):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return bindings.get(node.id)
        if isinstance(node, (ast.List, ast.Tuple)) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts
        ):
            return tuple(item.value for item in node.elts)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
            and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str)
            and len(node.args) == 1
            and not node.keywords
        ):
            items = cls._literal_output_value(node.args[0], bindings)
            if isinstance(items, tuple):
                return node.func.value.value.join(items)
        return None

    def save_api_key(self, api_key):
        settings = QSettings()
        encoded_key = base64.b64encode(api_key.encode('utf-8')).decode('utf-8')
        settings.setValue("QueryGIS/api_key", encoded_key)

    def load_api_key(self):
        settings = QSettings()
        encoded_key = settings.value("QueryGIS/api_key", "")
        if encoded_key:
            try:
                return base64.b64decode(encoded_key.encode('utf-8')).decode('utf-8')
            except:
                return ""
        return ""

    def _normalize_metadata_keywords(self, md):
        kw = []
        try:
            raw_kw = md.keywords()
            if isinstance(raw_kw, dict):
                for _, v in raw_kw.items():
                    if isinstance(v, (list, tuple)):
                        kw.extend([str(x) for x in v])
                    elif v is not None:
                        kw.append(str(v))
            elif isinstance(raw_kw, (list, tuple)):
                kw = [str(x) for x in raw_kw]
            elif raw_kw:
                kw = [str(raw_kw)]
        except Exception:
            kw = []
        return kw

    def _collect_layer_metadata(self, layer):
        layer_meta = {}
        try:
            if hasattr(layer, "metadata"):
                md = layer.metadata()
                layer_meta = {
                    "identifier": getattr(md, "identifier", lambda: "")(),
                    "title": getattr(md, "title", lambda: "")(),
                    "abstract": getattr(md, "abstract", lambda: "")(),
                    "keywords": self._normalize_metadata_keywords(md)
                }
        except Exception:
            layer_meta = {}
        return layer_meta

    def _collect_project_metadata(self):
        p = QgsProject.instance()
        meta = {
            "crs": p.crs().authid(),
            "title": getattr(p, "title", lambda: "")(),
            "file_name": getattr(p, "fileName", lambda: "")(),
            "home_path": getattr(p, "homePath", lambda: "")()
        }
        try:
            if hasattr(p, "metadata"):
                md = p.metadata()
                meta["metadata"] = {
                    "identifier": getattr(md, "identifier", lambda: "")(),
                    "title": getattr(md, "title", lambda: "")(),
                    "abstract": getattr(md, "abstract", lambda: "")(),
                    "keywords": self._normalize_metadata_keywords(md)
                }
        except Exception:
            pass
        return meta

    def _collect_vector_feature_rows(self, vlayer, max_rows=5, max_value_len=120):
        rows = []
        try:
            fields = [f.name() for f in vlayer.fields()]
        except Exception:
            fields = []
        for i, feat in enumerate(vlayer.getFeatures()):
            if i >= max_rows:
                break
            row = {}
            for fname in fields:
                try:
                    val = feat[fname]
                except Exception:
                    val = None
                if val is None:
                    row[fname] = None
                else:
                    sval = str(val)
                    row[fname] = sval[:max_value_len]
            rows.append(row)
        return rows

    def _collect_qgis_context(self):
        p = QgsProject.instance()
        layers_info = []
        active_id = None
        try:
            active = self.iface.activeLayer() if self.iface else None
            if active:
                active_id = active.id()
        except Exception:
            active_id = None
        
        for lyr in p.mapLayers().values():
            try:
                info = {
                    "name": lyr.name(),
                    "type": _layer_kind(lyr),
                    "crs": lyr.crs().authid() if hasattr(lyr, "crs") else None,
                    "provider": getattr(lyr, "providerType", lambda: None)(),
                    "source": getattr(lyr, "source", lambda: None)(),
                    "metadata": self._collect_layer_metadata(lyr)
                }
                
                if isinstance(lyr, QgsVectorLayer):
                    info["geometry"] = QgsWkbTypes.displayString(lyr.wkbType())
                    info["feature_count"] = lyr.featureCount()
                    info["fields"] = [f.name() for f in lyr.fields()]
                    info["is_csv"] = (str(info.get("provider") or "").lower() == "delimitedtext")
                    max_rows = 5 if (active_id and lyr.id() == active_id) else 3
                    info["feature_samples"] = self._collect_vector_feature_rows(lyr, max_rows=max_rows)
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                    except Exception:
                        pass
                elif isinstance(lyr, QgsRasterLayer):
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                        info["extent_corners"] = {
                            "top_left": [ext.xMinimum(), ext.yMaximum()],
                            "top_right": [ext.xMaximum(), ext.yMaximum()],
                            "bottom_left": [ext.xMinimum(), ext.yMinimum()],
                            "bottom_right": [ext.xMaximum(), ext.yMinimum()]
                        }
                    except Exception:
                        pass
                    try:
                        info["band_count"] = lyr.bandCount()
                        info["width"] = lyr.width()
                        info["height"] = lyr.height()
                    except Exception:
                        pass
                    
                layers_info.append(info)
            except Exception:
                pass

        return {
            "project": self._collect_project_metadata(),
            "layers": layers_info
        }

    def _collect_qgis_context_light(self):
        p = QgsProject.instance()
        layers_info = []

        for lyr in p.mapLayers().values():
            try:
                info = {
                    "name": lyr.name(),
                    "type": _layer_kind(lyr),
                    "crs": lyr.crs().authid() if hasattr(lyr, "crs") else None,
                    "provider": getattr(lyr, "providerType", lambda: None)(),
                    "source": getattr(lyr, "source", lambda: None)(),
                    "metadata": self._collect_layer_metadata(lyr)
                }
                if isinstance(lyr, QgsVectorLayer):
                    info["geometry"] = QgsWkbTypes.displayString(lyr.wkbType())
                    info["feature_count"] = lyr.featureCount()
                    info["is_csv"] = (str(info.get("provider") or "").lower() == "delimitedtext")
                    info["fields"] = [f.name() for f in lyr.fields()]
                    info["feature_samples"] = self._collect_vector_feature_rows(lyr, max_rows=3)
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                    except Exception:
                        pass
                elif isinstance(lyr, QgsRasterLayer):
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                        info["extent_corners"] = {
                            "top_left": [ext.xMinimum(), ext.yMaximum()],
                            "top_right": [ext.xMaximum(), ext.yMaximum()],
                            "bottom_left": [ext.xMinimum(), ext.yMinimum()],
                            "bottom_right": [ext.xMaximum(), ext.yMinimum()]
                        }
                    except Exception:
                        pass
                    try:
                        info["band_count"] = lyr.bandCount()
                        info["width"] = lyr.width()
                        info["height"] = lyr.height()
                    except Exception:
                        pass
                layers_info.append(info)
            except Exception:
                pass

        return {
            "project": self._collect_project_metadata(),
            "layers": layers_info
        }

    def _collect_qgis_context_active(self, max_rows=5):
        p = QgsProject.instance()
        layers_info = []
        
        selected_layers = []
        try:
            if self.iface:
                # Get all selected layers from the tree view
                selected_layers = self.iface.layerTreeView().selectedLayers()
        except Exception:
            selected_layers = []

        # Fallback 1: Active Layer if no selection
        if not selected_layers:
            try:
                active = self.iface.activeLayer() if self.iface else None
                if active:
                    selected_layers = [active]
            except Exception:
                pass

        # Fallback 2: First layer in project
        if not selected_layers:
            try:
                all_layers = list(p.mapLayers().values())
                if all_layers:
                    selected_layers = [all_layers[0]]
            except Exception:
                pass

        for lyr in selected_layers:
            if not lyr:
                continue
            
            try:
                info = {
                    "name": lyr.name(),
                    "type": _layer_kind(lyr),
                    "crs": lyr.crs().authid() if hasattr(lyr, "crs") else None,
                    "provider": getattr(lyr, "providerType", lambda: None)(),
                    "source": getattr(lyr, "source", lambda: None)(),
                    "metadata": self._collect_layer_metadata(lyr)
                }
                if isinstance(lyr, QgsVectorLayer):
                    info["geometry"] = QgsWkbTypes.displayString(lyr.wkbType())
                    info["feature_count"] = lyr.featureCount()
                    info["is_csv"] = (str(info.get("provider") or "").lower() == "delimitedtext")
                    info["fields"] = [f.name() for f in lyr.fields()]
                    info["feature_samples"] = self._collect_vector_feature_rows(lyr, max_rows=max_rows)
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                    except Exception:
                        pass
                elif isinstance(lyr, QgsRasterLayer):
                    try:
                        ext = lyr.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                        info["extent_corners"] = {
                            "top_left": [ext.xMinimum(), ext.yMaximum()],
                            "top_right": [ext.xMaximum(), ext.yMaximum()],
                            "bottom_left": [ext.xMinimum(), ext.yMinimum()],
                            "bottom_right": [ext.xMaximum(), ext.yMinimum()]
                        }
                    except Exception:
                        pass
                    try:
                        info["band_count"] = lyr.bandCount()
                        info["width"] = lyr.width()
                        info["height"] = lyr.height()
                    except Exception:
                        pass
                layers_info.append(info)
            except Exception:
                pass

        return {
            "project": self._collect_project_metadata(),
            "layers": layers_info
        }

    def _collect_tool_info(self):
        info = {}
        try:
            canvas = self.iface.mapCanvas() if self.iface else None
            if canvas:
                ext = canvas.extent()
                if ext:
                    info["map_extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
        except Exception:
            pass

        try:
            active = self.iface.activeLayer() if self.iface else None
            if active:
                active_info = {
                    "name": active.name(),
                    "type": _layer_kind(active)
                }
                if isinstance(active, QgsVectorLayer):
                    active_info["geometry"] = QgsWkbTypes.displayString(active.wkbType())
                    active_info["fields"] = [f.name() for f in active.fields()]
                    try:
                        active_info["selected_count"] = active.selectedFeatureCount()
                    except Exception:
                        pass
                info["active_layer"] = active_info
        except Exception:
            pass

        try:
            p = QgsProject.instance()
            info["project_crs"] = p.crs().authid()
        except Exception:
            pass

        try:
            return json.dumps(info, ensure_ascii=False)
        except Exception:
            return ""

    def _collect_tool_data(self, tools):
        data = {}
        for tool in tools or []:
            try:
                name = tool.get("name")
                params = tool.get("params") or {}
            except Exception:
                continue
            if not name:
                continue

            if name == "context_light":
                ctx = self._collect_qgis_context_light()
                data[name] = ctx
            elif name == "context_full":
                ctx = self._collect_qgis_context()
                data[name] = ctx
            elif name == "tool_info":
                data[name] = self._collect_tool_info()
            elif name == "map_extent":
                try:
                    canvas = self.iface.mapCanvas() if self.iface else None
                    ext = canvas.extent() if canvas else None
                    if ext:
                        data[name] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                except Exception:
                    pass
            elif name == "active_layer_fields":
                try:
                    active = self.iface.activeLayer() if self.iface else None
                    if active and isinstance(active, QgsVectorLayer):
                        data[name] = {
                            "name": active.name(),
                            "geometry": QgsWkbTypes.displayString(active.wkbType()),
                            "fields": [f.name() for f in active.fields()]
                        }
                except Exception:
                    pass
            elif name == "layer_by_name":
                try:
                    target = params.get("name")
                    if target:
                        layer = self.find_layer_by_keyword(target)
                        if layer and isinstance(layer, QgsVectorLayer):
                            data[name] = {
                                "name": layer.name(),
                                "geometry": QgsWkbTypes.displayString(layer.wkbType()),
                                "fields": [f.name() for f in layer.fields()],
                                "feature_count": layer.featureCount()
                            }
                        elif layer:
                            data[name] = {
                                "name": layer.name(),
                                "type": "raster" if isinstance(layer, QgsRasterLayer) else "unknown"
                            }
                except Exception:
                    pass
        return data

    def _handle_tool_request(self, tool_request):
        if self._tool_request_rounds >= 2:
            return False
        tools = tool_request.get("tools") if isinstance(tool_request, dict) else None
        if not tools:
            return False
        tool_data = self._collect_tool_data(tools)
        if not tool_data:
            return False
        self._tool_request_rounds += 1
        self.update_wave_message("Collecting requested info")
        self._start_backend_attempt(
            mode="tool_followup",
            context_text=self._last_context_text or "",
            tool_info=self._request_tool_info or "",
            error_message=self._request_error_message or "",
            tool_request=tool_request,
            tool_data=tool_data
        )
        return True

    def _add_execution_result_to_chat(self, execution_success, seconds):
        if not self.ui:
            return
        if seconds < 1:
            time_str = f"{seconds*1000:.0f}ms"
        elif seconds < 60:
            time_str = f"{seconds:.1f}s"
        else:
            m = int(seconds // 60); s = seconds % 60
            time_str = f"{m}m {s:.1f}s"
        if execution_success:
            last = self.chat_history[-1] if self.chat_history else None
            if not last or last.get("role") != "assistant-print":
                self.append_chat_message("assistant-print", f"Finished in {time_str}")
        else:
            self.append_chat_message("assistant-print", f"Failed after {time_str}")

    def _refresh_auth_ui(self):
        if not self.ui:
            return
        signed_in = bool(self.user_oauth_sub)
        account = self.user_info.get("email") or self.user_info.get("name") or ""
        self.ui.btn_google_login.setText(self.tr("Sign out") if signed_in else self.tr("Sign in with Google"))
        self.ui.btn_google_login.setIcon(QtGui.QIcon() if signed_in else self.ui.google_icon)
        self.ui.label_welcome_user.setText(account)
        self.ui.label_welcome_user.setVisible(signed_in and bool(account))
        self.ui.subscription_panel.setVisible(signed_in)
        self.ui.key_controls.setVisible(not signed_in)
        self.ui.set_auth_required(
            not signed_in and (self._connection_required_after_logout or not self.load_api_key().strip()),
            show_login_success=self._login_confirmation_pending,
        )
        self._update_codex_ui()

    def _save_settings_key(self, field):
        api_key = field.text().strip()
        if api_key != self.load_api_key():
            self._clear_conversation()
        self.save_api_key(api_key)
        self.ui.set_api_key(api_key)
        if api_key:
            self._connection_required_after_logout = False
        self._refresh_auth_ui()
        self.ui.set_auth_message(self.tr("API key saved.") if api_key else self.tr("API key cleared."))

    def _save_font_size(self, size):
        self.ui.set_font_size(size)
        QSettings().setValue("QueryGIS/font_size", size)

    def _request_payload_metadata(self):
        payload = {
            "api_key": self._request_api_key,
            **RESULT_CAPABILITIES,
            "runtime": _runtime_metadata(),
            "run_id": self._current_run_id,
        }
        if self._request_previous_turn:
            payload["previous_turn"] = self._request_previous_turn
        return payload

    def _request_auth_headers(self):
        if self._request_id_token:
            return {"Authorization": f"Bearer {self._request_id_token}"}
        return {}

    def _handle_expired_login(self):
        if self._id_token == self._request_id_token:
            self.handle_logout()
        self.handle_error(self.tr("Your Google session expired. Please sign in again."))

    def _check_fix_authorization(self, response):
        if response.status_code == 401 and self._request_id_token:
            if self._id_token == self._request_id_token:
                self.handle_logout()
            raise QueryRequestDenied(self.tr("Your Google session expired. Please sign in again."))
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and data.get("error") == "subscription_required":
                raise QueryRequestDenied(data["output"]["text"])

    def _set_google_sign_in_message(self, message):
        if not self.ui:
            return
        self.ui.status_label.setStyleSheet("")
        self.ui.status_label.setText(message)
        self.ui.set_auth_message(message)

    def _set_google_buttons_enabled(self, enabled):
        if self.ui:
            self.ui.btn_google_login.setEnabled(enabled)
            self.ui.btn_connection_google.setEnabled(enabled)
            self.ui.btn_enterprise_access.setEnabled(enabled)

    def handle_google_login(self):
        if self.user_oauth_sub:
            self.handle_logout()
            return
        if self.auth_worker and self.auth_worker.isRunning():
            return

        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        params = urllib.parse.urlencode({
            "client_id": COGNITO_CLIENT_ID,
            "response_type": "code",
            "scope": "email openid profile",
            "redirect_uri": OAUTH_REDIRECT_URI,
            "identity_provider": "Google",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        auth_url = f"{COGNITO_OAUTH_URL}/authorize?{params}"
        worker = OAuthCallbackWorker(urllib.parse.urlsplit(OAUTH_REDIRECT_URI).port, state, verifier)
        self.auth_worker = worker
        worker.ready.connect(lambda: self._open_google_sign_in(worker, auth_url))
        worker.verifying.connect(lambda: self._set_google_sign_in_message(self.tr("Verifying Google sign-in…")))
        worker.authenticated.connect(self._complete_google_sign_in)
        worker.failed.connect(self._set_google_sign_in_message)
        worker.finished.connect(lambda: self._set_google_buttons_enabled(True))
        self._set_google_buttons_enabled(False)
        self._set_google_sign_in_message(self.tr("Continue in your browser…"))
        worker.start()

    def _open_google_sign_in(self, worker, url):
        if not worker.isInterruptionRequested() and not QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)):
            worker.requestInterruption()
            self._set_google_sign_in_message(self.tr("Could not open your browser. Please try again."))

    def _complete_google_sign_in(self, profile):
        self.set_user_auth(profile["sub"], profile.get("email"), profile.get("name"), profile["id_token"])

    def handle_logout(self):
        self._clear_conversation()
        self._codex_plan_verified = False
        self._stop_codex_connection()
        self._cancel_subscription_usage()
        self.user_oauth_sub = None
        self._id_token = None
        self._login_confirmation_pending = False
        self._connection_required_after_logout = True
        self.user_info = {}
        self._refresh_auth_ui()
        if self.ui:
            self.ui.set_auth_message("")
            self.ui.status_label.setStyleSheet("")
            self.ui.status_label.setText(self.tr("Signed out"))

    def set_user_auth(self, sub, email, name, id_token):
        if sub != self.user_oauth_sub:
            self._clear_conversation()
        self._codex_plan_verified = False
        self._stop_codex_connection()
        self._cancel_subscription_usage()
        self.user_oauth_sub = sub
        self._id_token = id_token
        self._login_confirmation_pending = True
        self._connection_required_after_logout = False
        self.user_info = {"email": email, "name": name}
        self._refresh_auth_ui()
        if self.ui:
            self.ui.set_auth_message("")
            self.ui.status_label.setText(self.tr("Signed in"))
            self.ui.status_label.setStyleSheet("background-color: #dcefe5; color: #155724;")
            self.refresh_subscription_usage()

    def _start_querygis(self):
        self._login_confirmation_pending = False
        self._refresh_auth_ui()

    def _can_connect_to_codex(self):
        return bool(self.ui and self.user_oauth_sub and self._id_token and self._codex_plan_verified)

    def _can_start_codex_task(self):
        return (
            self._can_connect_to_codex()
            and not self.ui.btn_ask.testAttribute(Qt.WidgetAttribute.WA_ForceDisabled)
            and not (self.worker and self.worker.isRunning())
        )

    def _update_codex_ui(self, message=""):
        if not self.ui:
            return
        button_text = None
        if self._codex_connect_pending:
            button_text = self.tr("Cancel connection")
            message = self.tr("Checking your Pro plan…")
        elif self._codex_connection.bridge:
            button_text = self.tr("Disconnect Codex")
            message = (self.tr("Connected to Codex") if self._codex_connection.connected else
                       self.tr("Waiting for Codex… Ask Codex to check the QueryGIS connection."))
        self.ui.set_codex_connection(self._can_connect_to_codex(), button_text, message)

    def _stop_codex_connection(self):
        self._codex_connect_pending = False
        self._codex_connection.stop()

    def _toggle_codex_connection(self):
        if self._codex_connect_pending or self._codex_connection.bridge:
            self._stop_codex_connection()
            self._update_codex_ui()
        elif self._can_connect_to_codex():
            if self._codex_connection.active_task:
                self._update_codex_ui(self.tr("Wait for the previous Codex task to finish before reconnecting."))
                return
            self._codex_connect_pending = True
            self._update_codex_ui()
            self.refresh_subscription_usage(quiet=True)

    def _finish_cancelled_codex_request(self, executed=False):
        if not self._codex_connection.cancelled:
            return False
        self._codex_connection.finish(False, executed=executed)
        self._request_attempt = 0
        self._pending_attempt_start = False
        if self.ui:
            self.stop_wave_progress("Cancelled")
            self.ui.btn_ask.setEnabled(True)
            self.ui.status_label.setText(self.tr("Codex task cancelled."))
        return True

    def refresh_subscription_usage(self, *, quiet=False):
        if not self.ui or not self._id_token or self._usage_reply is not None:
            return

        self.ui.btn_usage_refresh.setEnabled(False)
        if not quiet:
            self.ui.label_plan.setText(self.tr("Loading subscription…"))
            self.ui.label_calls.clear()
        request = QtNetwork.QNetworkRequest(QtCore.QUrl(SUBSCRIPTION_USAGE_ENDPOINT))
        request.setRawHeader(b"Authorization", f"Bearer {self._id_token}".encode("ascii"))
        request.setAttribute(QtNetwork.QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QtNetwork.QNetworkRequest.RedirectPolicy.ManualRedirectPolicy)
        reply = self._usage_network.get(request)
        self._usage_reply = reply
        timeout = QTimer(reply)
        timeout.setSingleShot(True)
        timeout.timeout.connect(reply.abort)
        reply.finished.connect(lambda: self._finish_subscription_usage(reply))
        timeout.start(15000)

    def _cancel_subscription_usage(self):
        reply, self._usage_reply = self._usage_reply, None
        if reply is not None:
            reply.abort()

    def _finish_subscription_usage(self, reply):
        reply.deleteLater()
        if reply is not self._usage_reply:
            return
        self._usage_reply = None
        if not self.ui:
            return

        self.ui.btn_usage_refresh.setEnabled(True)
        status = reply.attribute(QtNetwork.QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        message = self.tr("Could not load subscription. Try refreshing.")
        self.ui.label_calls.clear()
        is_pro = False
        if status == 401:
            message = self.tr("Your Google session expired. Sign out and sign in again.")
        elif status == 200 and reply.error() == QtNetwork.QNetworkReply.NetworkError.NoError:
            try:
                usage = json.loads(bytes(reply.readAll()))
                if usage["status"] == "active":
                    plan, calls, limit, remaining = (usage[key] for key in ("plan_name", "calls", "limit", "remaining"))
                    message = self.tr("Your plan: {plan}").format(plan=plan)
                    self.ui.label_calls.setText(
                        self.tr("Calls: {calls:,} / {limit:,} used · {remaining:,} remaining").format(
                            calls=calls, limit=limit, remaining=remaining
                        )
                    )
                    is_pro = isinstance(plan, str) and plan.strip().casefold() == "pro"
                elif usage["status"] == "not_registered":
                    message = self.tr("Sign up on the AICM website to see your plan.")
                elif usage["status"] == "no_subscription":
                    message = self.tr("No active subscription.")
            except (KeyError, TypeError, ValueError):
                message = self.tr("Could not read subscription information. Try refreshing.")
        self.ui.label_plan.setText(message)
        self._codex_plan_verified = is_pro
        self._codex_connection.subscription_checked()
        if self._codex_connect_pending:
            self._codex_connect_pending = False
            if self._can_connect_to_codex():
                self._codex_connection.start()
                return
            self._update_codex_ui(self.tr("Could not confirm an active Pro plan."))
        else:
            self._update_codex_ui()


    def process_query(self):
        self._submit_query()

    def _submit_query(self, *, codex_instruction=None):
        if not self.ui:
            self.iface.messageBar().pushMessage("Error", "UI not initialized.", level=Qgis.MessageLevel.Critical)
            return False

        from_codex = codex_instruction is not None
        if from_codex:
            if not self._codex_connection.active_task or not self._can_start_codex_task():
                return False
        elif not self.ui.btn_ask.isEnabled() or self._codex_connection.active_task:
            return False

        user_input = (codex_instruction if from_codex else self.ui.text_query.toPlainText()).strip()
        if not user_input:
            self.ui.status_label.setText("Query is empty!")
            self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
            return False

        id_token = self._id_token
        api_key = "" if id_token else self.load_api_key().strip()
        if not (api_key or id_token):
            self._refresh_auth_ui()
            return False

        self.ui.status_label.setStyleSheet("")
        self.start_wave_progress("Processing query")

        self._current_run_id = uuid.uuid4().hex[:12]
        self._request_remembers_turn = not from_codex
        self._request_previous_turn = self._previous_turn if self._request_remembers_turn else None

        self.append_chat_message("user", user_input)
        if not from_codex:
            self.ui.text_query.clear()
        self.ui.btn_ask.setEnabled(False)

        # REDACTED: Usage check (v1.6 handles this server-side to prevent UI freeze)

        try:
            model_name = "gemini-3-flash-preview"
            self._request_attempt = 1
            self._request_user_input = user_input
            self._request_api_key = api_key
            self._request_id_token = id_token
            self._request_model = model_name
            self._request_tool_info = self._collect_tool_info()
            self._request_error_message = ""
            self._last_execution_error_message = ""
            
            context_dict = self._collect_qgis_context_active()
            context_text = self._build_context_text(context_dict)
            print("[DEBUG] context_text_len:", len(context_text or ""))
            print("[DEBUG] context_text_preview:\n", (context_text or "")[:2000])
            self._last_context_text = context_text
            self._tool_request_rounds = 0
            self._last_prompt_full = ""
            self._execution_advance_triggered = False

            _send_error_report(
                user_query=user_input,
                context_text="",
                generated_code="",
                error_message="User query dispatched to backend.",
                model_name=model_name,
                phase="user_query",
                metadata={
                    "model": model_name,
                    "phase": "user_query",
                    "plugin_version": f"QueryGIS-Plugin/{CURRENT_VERSION}",
                    **_runtime_metadata(),
                    "os": os.name,
                    "run_id": self._current_run_id,
                    "attempt": self._request_attempt,
                    "mode": "instruction_only",
                    "max_attempts": 2  # ← 3에서 2로 변경 (로그용)
                },
                query_gis_instance=self
            )

            self._start_backend_attempt(
                mode="instruction_only",
                context_text=context_text,
                tool_info=self._request_tool_info
            )
            return True
        except Exception as e:
            logger.error(f"Query processing error: {e}")
            self.handle_error(f"Query processing failed: {str(e)}")
            return False
