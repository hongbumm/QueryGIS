# -*- coding: utf-8 -*-
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
    QgsProperty
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

from .resources import *
from .dockwidget import Ui_DockWidget

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


ENABLE_REMOTE_LOG = True
REPORT_ENDPOINT = "https://www.querygis.com/report-error" 
REPORT_TIMEOUT_SEC = 5
REPORT_RETRIES = 2

def _mask_sensitive(s: str) -> str:
    """로그에 포함될 수 있는 키/토큰을 마스킹."""
    if not s:
        return s
    try:
        s = re.sub(r'AIza[0-9A-Za-z\-_]{35}', '***REDACTED_GOOGLE_KEY***', s)  # Google API Key(근사)
        s = re.sub(r'AKIA[0-9A-Z]{16}', '***REDACTED_AWS_AK***', s)            # AWS Access Key Id
        s = re.sub(r'(?<![A-Za-z0-9])[A-Za-z0-9/\+=]{40}(?![A-Za-z0-9])', '***REDACTED_AWS_SK***', s)  # AWS Secret
        s = re.sub(r'eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}', '***REDACTED_JWT***', s)  # JWT
        return s
    except Exception:
        return s

def _send_error_report(user_query: str,
                       context_text: str,
                       generated_code: str,
                       error_message: str,
                       model_name: str = "gemini-2.5-flash",
                       phase: str = "execution",
                       metadata: dict = None):

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

        # 에러 리포트용 캐시
        self._user_input = payload.get("user_input", "")
        self._context_text = payload.get("context", "")
        self._model_name = payload.get("model", "gemini-2.5-flash")

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        try:
            if self._is_cancelled:
                return

            self.step_update.emit("Connecting to backend server")
            if self._is_cancelled:
                return

            self.step_update.emit("Sending request to server")
            try:
                resp = requests.post(
                    self.backend_url,
                    json=self.payload,
                    headers={"Content-Type": "application/json"},
                    timeout=self.timeout_sec
                )
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            text = data.get("response") or data.get("text") or json.dumps(data, ensure_ascii=False)
                        else:
                            text = json.dumps(data, ensure_ascii=False)
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
                        metadata={"plugin_version": "QueryGIS-Plugin/1.2"}
                    )

            except requests.exceptions.Timeout:
                self.error.emit("Request timeout - server did not respond in time")
                _send_error_report(self._user_input, self._context_text, "", "Timeout to backend",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.2"})

            except requests.exceptions.ConnectionError:
                self.error.emit(f"Cannot connect to backend server at {self.backend_url}")
                _send_error_report(self._user_input, self._context_text, "", "ConnectionError to backend",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.2"})

            except requests.exceptions.RequestException as e:
                self.error.emit(f"Network error: {e}")
                _send_error_report(self._user_input, self._context_text, "", f"RequestException: {e}",
                                   self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.2"})

        except Exception as e:
            self.error.emit(f"Worker error: {e}\n{traceback.format_exc()}")
            _send_error_report(self._user_input, self._context_text, "", f"Worker error: {e}",
                               self._model_name, "llm_call", {"plugin_version": "QueryGIS-Plugin/1.2"})

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

        self._last_status_update_ms = 0
        self._last_context_text = ""
        self._last_generated_code = ""
        self._current_run_id = None  # 각 요청 묶음 식별자

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
            text=self.tr(u'QueryGIS Backend'),
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

            self.ui.chk_reason.setVisible(True)
            self.ui.chk_rag.setVisible(False)

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

    # ---- 안전한 필드 샘플 수집(로그 최소화 위해 사용 안 함) ----
    def _collect_field_samples(self, vlayer, limit_values=5, scan_limit=500):
        try:
            return []
        except Exception:
            return []

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
        # 백엔드 텍스트 파싱
        display_text, code_blocks = self._parse_backend_response(response_text)
        should_run = bool(self.ui.chk_ask_run.isChecked()) if self.ui else False

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
                        model_name="gemini-2.5-flash",
                        phase="ai_answer",
                        metadata={"plugin_version": "QueryGIS-Plugin/1.2", "run_id": self._current_run_id}
                    )
                except Exception:
                    pass

                self.append_chat_message("assistant", chosen)
                if should_run:
                    self.start_wave_progress("Executing code")
                    final_code = self._prepend_runtime_imports(chosen)
                    self.run_code_string(final_code)
            else:
                self.append_chat_message("assistant-print", display_text.strip())
        else:
            self.append_chat_message("assistant-print", display_text.strip())

        if self.ui:
            self.ui.status_label.setText("Response processed")
            self.ui.status_label.setStyleSheet(f"background-color: {self.success_status_color}; color: black;")
            self.stop_wave_progress("Done")
            self.ui.btn_ask.setEnabled(True)

    def handle_error(self, error_message: str):
        if not self.ui:
            return
        msg = str(error_message).strip() or "Unknown error"
        self.append_chat_message("assistant-print", f"Error:\n{msg}")
        self.ui.status_label.setText("Request failed")
        self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
        self.stop_wave_progress("Error")
        self.ui.btn_ask.setEnabled(True)

    def _build_context_text(self, ctx: dict) -> str:
        # 짧게 쓰고 싶으면 "" 반환 유지
        return ""

    def run_code_string(self, code_string):
        if not self.ui:
            return
        self.start_wave_progress("Preparing code execution")
        start_time = time.time()
        old_stdout = sys.stdout
        captured_output = io.StringIO()
        exec_scope = None
        execution_success = False
        error_details = None

        try:
            self.update_wave_message("Setting up environment")
            sys.stdout = captured_output
            exec_scope = self.get_execution_scope()

            # processing.run 래핑 주입
            if "processing.run" in code_string:
                code_string = self._inject_processing_feedback(code_string)

            self.update_wave_message("Executing code")
            exec(code_string, exec_scope)
            execution_success = True
            output = captured_output.getvalue().strip()

            # 성공 보고
            try:
                last_user = ""
                for m in reversed(self.chat_history):
                    if m.get("role") == "user":
                        last_user = m.get("content", "")
                        break
                _send_error_report(
                    user_query=last_user,
                    context_text="",
                    generated_code=getattr(self, "_last_generated_code", "") or code_string,
                    error_message=("SUCCESS" + (f"\nPRINT:\n{output}" if output else "")),
                    model_name="gemini-2.5-flash",
                    phase="execution_result",
                    metadata={"plugin_version": "QueryGIS-Plugin/1.2", "run_id": self._current_run_id}
                )
            except Exception:
                pass

            self.ui.status_label.setText("Code execution succeeded!")
            self.ui.status_label.setStyleSheet(f"background-color: {self.success_status_color}; color: black;")
            if output:
                self.append_chat_message("assistant-print", f"Print output:\n{output}")
            self.stop_wave_progress("Execution completed successfully!")

        except Exception as exc:
            execution_success = False
            error_details = traceback.format_exc()

            # 실패 보고
            try:
                last_user = ""
                for m in reversed(self.chat_history):
                    if m.get("role") == "user":
                        last_user = m.get("content", "")
                        break
                _send_error_report(
                    user_query=last_user,
                    context_text="",
                    generated_code=getattr(self, "_last_generated_code", "") or code_string,
                    error_message=error_details,
                    model_name="gemini-2.5-flash",
                    phase="execution_result",
                    metadata={"plugin_version": "QueryGIS-Plugin/1.2", "run_id": self._current_run_id}
                )
            except Exception:
                pass

            self.ui.status_label.setText(f"Execution Error")
            self.ui.status_label.setStyleSheet(f"background-color: {self.error_status_color}; color: white;")
            self.append_chat_message("assistant-print", f"Execution Error:\n{error_details}")
            self.stop_wave_progress("Error occurred")
        finally:
            end_time = time.time()
            sys.stdout = old_stdout
            captured_output.close()
            if exec_scope:
                try:
                    proc_mod = exec_scope.get('processing')
                    orig = exec_scope.get('_orig_processing_run')
                    if proc_mod and orig:
                        proc_mod.run = orig
                except Exception as e:
                    logger.warning(f"Failed to restore processing.run: {e}")
            self._add_execution_result_to_chat(execution_success, end_time - start_time)

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
        return scope

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
        for layer in project.mapLayers().values():
            if keyword.lower() in layer.name().lower():
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

    def _collect_qgis_context(self):
        return {}

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
            model_name = "gemini-2.5-flash"

            context_text = ""

            self._last_context_text = context_text

            payload = {
                "api_key": api_key,
                "context": context_text,
                "user_input": user_input,
                "model": model_name
            }

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
                    "plugin_version": "QueryGIS-Plugin/1.2",
                    "qgis_version": Qgis.QGIS_VERSION,
                    "os": os.name,
                    "run_id": self._current_run_id
                }
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
        except Exception as e:
            logger.error(f"Query processing error: {e}")
            self.handle_error(f"Query processing failed: {str(e)}")
