import os, os.path, sys, io, tempfile, traceback, base64, re, time, uuid
import builtins
import logging
import requests
import json
from qgis.utils import iface
from qgis.core import (
    Qgis,
    QgsApplication, QgsProject, QgsMapLayer, QgsRasterLayer, QgsVectorLayer,
    QgsField, QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsProcessingFeatureSourceDefinition,
    QgsFeatureSink, QgsFeatureRequest, QgsProcessingFeedback, QgsMessageLog,
    QgsFillSymbol, QgsSingleSymbolRenderer, QgsSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer,
    QgsPalLayerSettings, QgsTextFormat, QgsTextBufferSettings, QgsVectorLayerSimpleLabeling,
    QgsProperty, QgsWkbTypes
)

try:
    from qgis.PyQt import QtCore, QtGui, QtWidgets
    from qgis.PyQt.QtCore import (
        QSettings, QTranslator, QCoreApplication, Qt, QTimer, QThread,
        pyqtSignal, QEvent, QVariant, QObject
    )
    from qgis.PyQt.QtGui import QIcon, QColor, QFont
    from qgis.PyQt.QtWidgets import (
        QAction, QDockWidget, QLineEdit, QWidget, QHBoxLayout, QLabel,
        QPushButton, QApplication, QTextEdit
    )
except ImportError:
    from PyQt5.QtCore import (
        QSettings, QTranslator, QCoreApplication, Qt, QTimer, QThread,
        pyqtSignal, QEvent, QVariant, QObject
    )
    from PyQt5.QtGui import QIcon, QColor, QFont
    from PyQt5.QtWidgets import (
        QAction, QDockWidget, QLineEdit, QWidget, QHBoxLayout, QLabel,
        QPushButton, QApplication, QTextEdit
    )

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .resources import *
from .dockwidget import Ui_DockWidget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ENABLE_REMOTE_LOG = True
REPORT_ENDPOINT = "https://www.querygis.com/report-error"
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

    row = {
        "ts": QtCore.QDateTime.currentDateTimeUtc().toString(Qt.ISODateWithMs),
        "user_input": _mask_sensitive(user_query or ""),
        "client_ip": "127.0.0.1",
        "context": _mask_sensitive(context_text or ""),
        "generated_code": _mask_sensitive(generated_code or ""),
        "error_message": _mask_sensitive(error_message or ""),
        "metadata": (metadata or {}),
        "cache_name": None,
        "phase": phase,
    }

    pretty_json = json.dumps(row, ensure_ascii=False, indent=2)
    print("[LOG PREPARED]:")
    print(pretty_json)

    if not ENABLE_REMOTE_LOG:
        return

    if query_gis_instance:
        query_gis_instance._send_log_async(row)
        print("[LOG dispatched to background worker]")
        return

    last_err = None
    for attempt in range(1, REPORT_RETRIES + 2):
        try:
            r = requests.post(
                REPORT_ENDPOINT,
                json=row,
                timeout=REPORT_TIMEOUT_SEC,
                headers={"Content-Type": "application/json"},
            )
            if r.status_code < 300:
                print(f"[LOG SENT] attempt={attempt} status={r.status_code}")
                return
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
        time.sleep(0.2)

    print(f"[LOG FAILED] endpoint={REPORT_ENDPOINT} error={last_err}")
    try:
        iface.messageBar().pushWarning("QueryGIS Log",
            f"Failed to send log to server: {last_err}")
    except Exception:
        pass

class WaveProgressManager:
    def __init__(self, update_callback):
        self.update_callback = update_callback
        self.animation_timer = QTimer()
        self.animation_timer.timeout.connect(self._animate_wave)
        self.wave_position = 0
        self.current_message = ""
        self.is_active = False

    def start_wave(self, message="Processing"):
        self.current_message = message
        self.is_active = True
        self.wave_position = 0
        self.animation_timer.start(200)
        self._update_display()

    def update_message(self, message):
        self.current_message = str(message)
        if self.is_active:
            self._update_display()

    def stop_wave(self, final_message="Complete"):
        self.is_active = False
        self.animation_timer.stop()
        self.update_callback(final_message, True)

    def _animate_wave(self):
        if not self.is_active:
            return
        self.wave_position = (self.wave_position + 1) % 8
        self._update_display()

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

class BackendWorker(QThread):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    step_update = pyqtSignal(str)

    def __init__(self, payload, backend_url="https://www.querygis.com/chat", timeout_sec=180):
        super().__init__()
        self.payload = payload
        self.backend_url = backend_url
        self.timeout_sec = timeout_sec
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

            self.step_update.emit("Connecting to backend server")
            if self._is_cancelled:
                return

            self.step_update.emit("Sending request to server")
            try:
                resp = session.post(
                    self.backend_url,
                    json=self.payload,
                    timeout=self.timeout_sec
                )
                
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, (dict, list)):
                            text = json.dumps(data, ensure_ascii=False)
                        else:
                            text = str(data)
                    except Exception:
                        text = resp.text
                    self.step_update.emit("Processing response")
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
                        metadata={"plugin_version": "QueryGIS-Plugin/1.3"}
                    )

            except requests.exceptions.Timeout:
                self.error.emit("Request timeout - server did not respond in time")
                _send_error_report(self._user_input, self._context_text, "", "Timeout to backend",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.3"})

            except requests.exceptions.ConnectionError:
                self.error.emit(f"Cannot connect to backend server at {self.backend_url}")
                _send_error_report(self._user_input, self._context_text, "", "ConnectionError to backend",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.3"})

            except requests.exceptions.RequestException as e:
                self.error.emit(f"Network error: {e}")
                _send_error_report(self._user_input, self._context_text, "", f"RequestException: {e}",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.3"})

        except Exception as e:
            self.error.emit(f"Worker error: {e}\n{traceback.format_exc()}")
            _send_error_report(self._user_input, self._context_text, "", f"Worker error: {e}",
                               self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.3"})
        finally:
            if session:
                session.close()

class _UIFeedback(QgsProcessingFeedback):
    def __init__(self, update_fn, label="Working"):
        super().__init__()
        self._update = update_fn
        self._label = label
    def setProgress(self, p):
        super().setProgress(p)
        self._update(f"{self._label} {p:.0f}%")
    def pushInfo(self, info):
        super().pushInfo(info)
        if info:
            self._update(str(info))

class _RunProgressProxy:
    def __init__(self, ui_update_fn):
        self._update = ui_update_fn
        self._calls_seen = 0
        self._calls_done = 0
        self._last_ui_ms = 0
    def _maybe_update(self, text):
        now = int(time.time()*1000)
        if now - self._last_ui_ms >= 250:
            self._last_ui_ms = now
            self._update(text)
    def wrap(self, real_run):
        def _wrapped(alg_id, params, context=None, feedback=None, **kwargs):
            self._calls_seen += 1
            self._maybe_update(f"Processing started… (step {self._calls_seen})")
            try:
                res = self._safe_run(real_run, alg_id, params, context=context, feedback=feedback)
                self._calls_done += 1
                self._maybe_update(f"Processing in progress… {self._calls_done} done")
                if self._calls_done == self._calls_seen:
                    self._maybe_update("Processing complete")
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

        self.default_status_color = "#F0F0F0"
        self.success_status_color = "#66FF66"
        self.error_status_color = "#FF3333"

        self.ui = None
        self.worker = None
        self.wave_manager = None
        self.logging_workers = []
        self._last_status_update_ms = 0
        self._last_context_text = ""
        self._last_generated_code = ""
        self._current_run_id = None
        self._last_soft_error_info = None
        self._request_attempt = 0
        self._request_user_input = ""
        self._request_api_key = ""
        self._request_model = "gemini-3-flash-preview"
        self._request_should_run = False
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

    def _send_log_async(self, row_data):
        if not ENABLE_REMOTE_LOG:
            return
        worker = LoggingWorker(REPORT_ENDPOINT, row_data, REPORT_TIMEOUT_SEC)
        
        worker.finished.connect(lambda w=worker: self._on_logging_worker_finished(w))
        
        self.logging_workers.append(worker)
        worker.start()

    def _on_logging_worker_finished(self, worker_instance):
        try:
            if worker_instance in self.logging_workers:
                self.logging_workers.remove(worker_instance)
        except Exception:
            pass

    def _extract_error_summary(self, stdout_output, qgis_errors, qgis_log, original_error=""):
        for line in (stdout_output or "").split('\n'):
            line = line.strip()
            if '❌' in line:
                clean_line = line.replace('❌', '').replace('오류 발생:', '').strip()
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
        
        return "코드 실행 중 오류가 발생했습니다"

    def execute_with_self_correction(self, code, scope, user_input, context, retry_count=0):
        MAX_RETRIES = 2
        FIX_URL = "https://www.querygis.com/fix-code"

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
            if retry_count > 0 and isinstance(sys.stdout, io.StringIO):
                sys.stdout.truncate(0)
                sys.stdout.seek(0)

            start_log_pos = 0
            if isinstance(sys.stdout, io.StringIO):
                start_log_pos = sys.stdout.tell()

            exec(code, scope)
            
            current_output = ""
            if isinstance(sys.stdout, io.StringIO):
                sys.stdout.seek(start_log_pos)
                current_output = sys.stdout.read()
                sys.stdout.seek(0, io.SEEK_END)

            output_lines = [l.strip() for l in current_output.split('\n') if l.strip()]
            last_meaningful_line = output_lines[-1] if output_lines else ""
            
            success_keywords = ["✓", "완료!", "성공", "Complete", "finished", "successfully"]
            if any(s in last_meaningful_line for s in success_keywords):
                self._last_soft_error_info = None
                return 
            
            fail_keywords = ["❌", "실패", "Error:", "Exception", "찾을 수 없습니다", "오류", "Traceback"]
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

            full_error_for_ai = f"""=== ERROR DETECTED ===
Error Type: {type(e).__name__}
Message: {str(e)}

=== EXECUTION OUTPUT ===
{stdout_output}

=== QGIS LOG ===
{qgis_errors}
"""
            error_for_fix = full_error_for_ai[-1000:] if len(full_error_for_ai) > 1000 else full_error_for_ai
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
            self.update_wave_message(f"자동 수정 중... ({retry_count + 1}/{MAX_RETRIES})")

            thinking_strategy = "LOW" if retry_count == 0 else "HIGH"
            
            api_key = self.load_api_key()
            payload = {
                "api_key": api_key,
                "context": context_for_fix,
                "user_input": user_input,
                "broken_code": broken_code_for_fix,
                "error_message": error_for_fix,
                "model": "gemini-3-flash-preview",
                "thinking_level": thinking_strategy
            }

            try:
                response = requests.post(FIX_URL, json=payload, timeout=150)
                
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
                                    "plugin_version": "QueryGIS-Plugin/1.3",
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
        if not self.wave_manager:
            self.wave_manager = WaveProgressManager(self._update_wave_ui)
        if not self.ui.progressBar.isVisible():
            self.ui.progressBar.setVisible(True)
        self.ui.progressBar.setRange(0, 0)
        self.wave_manager.start_wave(message)

    def update_wave_message(self, message, progress=None):
        if not self.wave_manager:
            return
        now = int(time.time() * 1000)
        if now - self._last_status_update_ms < 120:
            return
        self._last_status_update_ms = now
        self.wave_manager.update_message(str(message))

    def stop_wave_progress(self, final_message="Complete"):
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

    def hide_progress(self):
        if self.wave_manager:
            self.wave_manager.stop_wave()
        if self.ui:
            self.ui.progressBar.setVisible(False)
            self.ui.progressBar.setValue(0)
            if self.ui.status_label.text() != "Status: Ready":
                self.ui.status_label.setText("Status: Ready")
            QApplication.processEvents()

    def tr(self, message):
        return QCoreApplication.translate('QueryGIS', message)

    def add_action(self, icon_path, text, callback, enabled_flag=True,
                   add_to_menu=True, add_to_toolbar=True, status_tip=None,
                   whats_this=None, parent=None):
        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
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
        icon_path = ':/plugins/query_gis/icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'QueryGIS'),
            callback=self.run,
            parent=self.iface.mainWindow()
        )

    def unload(self):
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
            self.dockwidget.setAttribute(Qt.WA_DeleteOnClose, True)
            self.dockwidget.setObjectName("QueryGISDockWidget")
            self.dockwidget.destroyed.connect(self._on_dockwidget_destroyed)

            self.ui = Ui_DockWidget()
            self.ui.setupUi(self.dockwidget)

            self.ui.line_apikey.setVisible(True)
            self.ui.line_apikey.setPlaceholderText("Enter your API key")
            self.ui.line_apikey.setEchoMode(QLineEdit.Password)

            self.ui.btn_ask.clicked.connect(self.process_query)
            self.ui.chk_ask_run.stateChanged.connect(self.toggle_ask_run)
            self.ui.text_query.installEventFilter(self)
            self.ui.chk_ask_run.setChecked(True)
            self.toggle_ask_run()

            saved_api_key = self.load_api_key()
            if saved_api_key:
                self.ui.line_apikey.setText(saved_api_key)

            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)

        self.dockwidget.show()

    def _on_dockwidget_destroyed(self):
        self.dockwidget = None
        self.ui = None

    def eventFilter(self, obj, event):
        if (self.ui and obj == self.ui.text_query and event.type() == QEvent.KeyPress):
            if (event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier):
                self.process_query()
                return True
        return super().eventFilter(obj, event)

    def toggle_ask_run(self):
        if self.ui and self.ui.chk_ask_run.isChecked():
            self.ui.btn_ask.setText("Ask and Run\n(Ctrl+Enter)")
        elif self.ui:
            self.ui.btn_ask.setText("Ask\n(Ctrl+Enter)")

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

    def add_chat_message(self, role, message):
        msg_widget = QWidget()
        layout = QHBoxLayout(msg_widget)
        layout.setContentsMargins(10, 5, 10, 5)

        if role == "user":
            label = QLabel(message)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet(
                "background-color: #1AC85C; color: white; border: none; border-radius: 10px; "
                "padding: 8px; font-family: '맑은 고딕'; font-size: 12px;"
            )
            layout.addStretch()
            layout.addWidget(label)

        elif role == "assistant-print":
            label = QLabel(message)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setStyleSheet(
                "background-color: #D9D9D9; color: black; border: none; border-radius: 10px; "
                "padding: 8px; font-style: italic;"
            )
            copy_btn = QPushButton("Copy")
            copy_btn.setMaximumSize(40, 25)
            copy_btn.clicked.connect(lambda _, text=message: self.copy_to_clipboard(text))
            layout.addWidget(label)
            layout.addWidget(copy_btn)
            layout.addStretch()

        else:
            text_edit = QTextEdit()
            text_edit.setPlainText(message)
            text_edit.setReadOnly(False)
            text_edit.document().documentLayout().documentSizeChanged.connect(
                lambda size, te=text_edit: te.setMinimumHeight(
                    int(size.height()) + te.contentsMargins().top() +
                    te.contentsMargins().bottom() + 5
                )
            )
            text_edit.setMinimumHeight(
                int(text_edit.document().size().height()) +
                text_edit.contentsMargins().top() +
                text_edit.contentsMargins().bottom() + 5
            )
            text_edit.setStyleSheet(
                "QTextEdit {background-color: #D9D9D9; color: black; border: none; "
                "border-radius: 10px; padding: 8px;}"
            )
            text_edit.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            text_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            layout.addWidget(text_edit, 1)
            run_btn = QPushButton("Run")
            run_btn.setMaximumSize(40, 25)
            layout.addWidget(run_btn)
            copy_btn = QPushButton("Copy")
            copy_btn.setMaximumSize(40, 25)
            layout.addWidget(copy_btn)
            layout.addStretch()
            run_btn.clicked.connect(lambda _, edit=text_edit: self.run_message_from_chat(edit.toPlainText()))
            copy_btn.clicked.connect(lambda _, edit=text_edit: self.copy_to_clipboard(edit.toPlainText()))
        return msg_widget

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
            self.chat_history.append({"role": role, "content": message})
            msg_widget = self.add_chat_message(role, message)
            self.ui.chatLayout.insertWidget(self.ui.chatLayout.count() - 1, msg_widget)
        finally:
            if container:
                container.setUpdatesEnabled(True)
            sa.viewport().setUpdatesEnabled(True)
            sa.setUpdatesEnabled(True)
        QTimer.singleShot(100, self.scroll_to_bottom)

    def scroll_to_bottom(self):
        if not self.ui:
            return
        QApplication.processEvents()
        sb = self.ui.chatScrollArea.verticalScrollBar()
        if sb:
            sb.setValue(sb.maximum())

    def copy_to_clipboard(self, text):
        QApplication.clipboard().setText(text)

    def run_message_from_chat(self, code):
        self.start_wave_progress("Preparing to execute code")
        final_code = self._prepend_runtime_imports(code)
        self.run_code_string(final_code)

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
        self._last_token_count = None
        self._last_response_mode = ""
        self._last_prompt_full = ""
        tool_request = None
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
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
                        "plugin_version": "QueryGIS-Plugin/1.3",
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

        display_text, code_blocks = self._parse_backend_response(response_text)
        should_run = self._request_should_run if self._request_attempt else (bool(self.ui.chk_ask_run.isChecked()) if self.ui else False)

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
                            "plugin_version": "QueryGIS-Plugin/1.3",
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
                if should_run:
                    self.start_wave_progress("Executing code")
                    final_code = self._prepend_runtime_imports(chosen)
                    success = self.run_code_string(final_code)
                    if (not success) and self._retry_on_execution_failure:
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

        if self.ui:
            self.ui.status_label.setText("Response processed")
            self.ui.status_label.setStyleSheet(f"background-color: {self.success_status_color}; color: black;")
            self.stop_wave_progress("Done")
            self.ui.btn_ask.setEnabled(True)
        if self._pending_attempt_start:
            self._pending_attempt_start = False
            return
        if self.worker and self.worker.isRunning():
            return
        self._request_attempt = 0

    def handle_error(self, error_message: str):
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
            "api_key": self._request_api_key,
            "context": context_text or "",
            "user_input": self._request_user_input,
            "model": self._request_model,
            "mode": mode
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

        self.worker = BackendWorker(payload, backend_url="https://www.querygis.com/chat", timeout_sec=120)
        self.worker.step_update.connect(self.update_wave_message)
        self.worker.finished.connect(self.handle_response)
        self.worker.error.connect(self.handle_error)
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
                        "plugin_version": "QueryGIS-Plugin/1.3",
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
        FIX_URL = "https://www.querygis.com/fix-code"
        
        api_key = self.load_api_key()
        payload = {
            "api_key": api_key,
            "context": context,
            "user_input": user_input,
            "broken_code": broken_code[:2000],  # 길이 제한
            "error_message": f"SYNTAX ERROR:\n{error_message}",
            "model": "gemini-3-flash-preview",
            "thinking_level": "HIGH"
        }
        
        try:
            self.update_wave_message("Fixing syntax error...")
            response = requests.post(FIX_URL, json=payload, timeout=60)
            
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
                                "plugin_version": "QueryGIS-Plugin/1.3",
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
                        except Exception as fix_err:
                            print(f"[SYNTAX FIX FAILED] {fix_err}")
                
                self.append_chat_message("assistant-print", err_msg)
                if self.ui:
                    self.ui.status_label.setText("Syntax Error")
                    self.ui.status_label.setStyleSheet(
                        f"background-color: {self.error_status_color}; color: white;"
                    )
                self.stop_wave_progress("Syntax error occurred")
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
            
            self.ui.status_label.setText("Code execution succeeded!")
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
                    "plugin_version": "QueryGIS-Plugin/1.3",
                    "run_id": self._current_run_id,
                    "elapsed_sec": elapsed,
                    "attempt": self._request_attempt or None,
                    "mode": self._last_response_mode or None,
                    "token_count": self._last_token_count
                },
                query_gis_instance=self
            )
            
            if final_output:
                self.append_chat_message("assistant-print", f"Output:\n{final_output}")
            
            self.stop_wave_progress("Execution completed!")
            self._add_execution_result_to_chat(True, elapsed)
            return True
        
        except Exception as e:
            elapsed = time.time() - start_time
            tb_text = traceback.format_exc()
            partial_output = main_buffer.getvalue().strip()
            self._last_execution_error_message = tb_text
            
            self.ui.status_label.setText("Execution Error")
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
                    "plugin_version": "QueryGIS-Plugin/1.3",
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
            
            self.stop_wave_progress("Error occurred")
            self._add_execution_result_to_chat(False, elapsed)
            return False
        
        finally:
            sys.stdout = original_stdout
            main_buffer.close()

    def get_execution_scope(self):
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
            'tempfile': tempfile,
            'os': os,
            'processing': sys.modules.get('processing'),
            '__builtins__': builtins,
            'find_layer_by_keyword': self.find_layer_by_keyword,
            'get_layer_safe': self.get_layer_safe,
            'shorten_layer_name': self.shorten_layer_name
        }
        
        scope['processing_feedback'] = _UIFeedback(self.update_wave_message, label="Processing...")
        proc_mod = scope['processing']
        if proc_mod and hasattr(proc_mod, 'run'):
            proxy = _RunProgressProxy(self.update_wave_message)
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

        print(f"레이어 '{layer_name}'를 찾을 수 없습니다.")
        print("사용 가능한 레이어:")
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
        if looks_like_code:
            code_blocks.append(text.strip())
        return code_blocks

    def _parse_backend_response(self, response_text: str):
        display_text = response_text
        code_blocks = []
        try:
            data = json.loads(response_text)
            if isinstance(data, dict):
                candidate = None
                if "output" in data:
                    out = data["output"]
                    if isinstance(out, dict) and "text" in out:
                        candidate = out["text"]
                if candidate is None and "response" in data:
                    candidate = data["response"]
                if candidate is None and "text" in data:
                    candidate = data["text"]
                if candidate is None and "choices" in data and isinstance(data["choices"], list) and data["choices"]:
                    ch = data["choices"][0]
                    if isinstance(ch, dict):
                        if "message" in ch and isinstance(ch["message"], dict) and "content" in ch["message"]:
                            candidate = ch["message"]["content"]
                        elif "text" in ch:
                            candidate = ch["text"]
                if candidate is not None:
                    display_text = str(candidate)
                else:
                    display_text = json.dumps(data, ensure_ascii=False, indent=2)
            else:
                display_text = json.dumps(data, ensure_ascii=False, indent=2)
        except Exception:
            display_text = response_text
        code_blocks = self._extract_code_blocks(display_text)
        return display_text, code_blocks

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
                    "type": ("vector" if lyr.type() == QgsMapLayer.VectorLayer
                            else "raster" if lyr.type() == QgsMapLayer.RasterLayer
                            else "pointcloud" if getattr(QgsMapLayer, 'PointCloudLayer', 3) == lyr.type()
                            else "unknown"),
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
                    "type": ("vector" if lyr.type() == QgsMapLayer.VectorLayer
                            else "raster" if lyr.type() == QgsMapLayer.RasterLayer
                            else "pointcloud" if getattr(QgsMapLayer, 'PointCloudLayer', 3) == lyr.type()
                            else "unknown"),
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
        try:
            active = self.iface.activeLayer() if self.iface else None
        except Exception:
            active = None
        if not active:
            try:
                all_layers = list(p.mapLayers().values())
                active = all_layers[0] if all_layers else None
            except Exception:
                active = None

        if active:
            try:
                info = {
                    "name": active.name(),
                    "type": ("vector" if active.type() == QgsMapLayer.VectorLayer
                            else "raster" if active.type() == QgsMapLayer.RasterLayer
                            else "pointcloud" if getattr(QgsMapLayer, 'PointCloudLayer', 3) == active.type()
                            else "unknown"),
                    "crs": active.crs().authid() if hasattr(active, "crs") else None,
                    "provider": getattr(active, "providerType", lambda: None)(),
                    "source": getattr(active, "source", lambda: None)(),
                    "metadata": self._collect_layer_metadata(active)
                }
                if isinstance(active, QgsVectorLayer):
                    info["geometry"] = QgsWkbTypes.displayString(active.wkbType())
                    info["feature_count"] = active.featureCount()
                    info["is_csv"] = (str(info.get("provider") or "").lower() == "delimitedtext")
                    info["fields"] = [f.name() for f in active.fields()]
                    info["feature_samples"] = self._collect_vector_feature_rows(active, max_rows=max_rows)
                    try:
                        ext = active.extent()
                        info["extent"] = [ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()]
                    except Exception:
                        pass
                elif isinstance(active, QgsRasterLayer):
                    try:
                        ext = active.extent()
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
                        info["band_count"] = active.bandCount()
                        info["width"] = active.width()
                        info["height"] = active.height()
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
                    "type": ("vector" if active.type() == QgsMapLayer.VectorLayer
                             else "raster" if active.type() == QgsMapLayer.RasterLayer
                             else "unknown")
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
                                "type": "raster" if layer.type() == QgsMapLayer.RasterLayer else "unknown"
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
                self.append_chat_message("assistant-print", f"Execution complete · {time_str}")
        else:
            self.append_chat_message("assistant-print", f"Execution failed · {time_str}")

    def process_query(self):
        if not self.ui:
            self.iface.messageBar().pushMessage("Error", "UI not initialized.", level=Qgis.Critical)
            return

        self.start_wave_progress("Processing query")
        user_input = self.ui.text_query.toPlainText().strip()
        if not user_input:
            self.ui.status_label.setText("Query is empty!")
            self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
            self.hide_progress()
            return

        api_key = self.ui.line_apikey.text().strip()
        if not api_key:
            self.ui.status_label.setText("API Key is required!")
            self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
            self.hide_progress()
            return

        saved_key = self.load_api_key()
        if api_key != saved_key:
            self.save_api_key(api_key)

        self._current_run_id = uuid.uuid4().hex[:12]

        self.append_chat_message("user", user_input)
        self.ui.text_query.clear()
        self.ui.btn_ask.setEnabled(False)

        try:
            model_name = "gemini-3-flash-preview"
            self._request_attempt = 1
            self._request_user_input = user_input
            self._request_api_key = api_key
            self._request_model = model_name
            self._request_should_run = bool(self.ui.chk_ask_run.isChecked())
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
                    "plugin_version": "QueryGIS-Plugin/1.3",
                    "qgis_version": Qgis.QGIS_VERSION,
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
        except Exception as e:
            logger.error(f"Query processing error: {e}")
            self.handle_error(f"Query processing failed: {str(e)}")
