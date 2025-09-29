try:
    from qgis.PyQt import (
        QtCore, QtGui, QtWidgets
    )
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


from qgis.utils import iface
from qgis.core import (
    QgsApplication, QgsProject, QgsMapLayer, QgsRasterLayer, QgsVectorLayer,
    QgsField, QgsFeature, QgsGeometry, QgsPointXY, QgsRectangle,
    QgsCoordinateReferenceSystem, QgsVectorFileWriter, QgsProcessingFeatureSourceDefinition,
    QgsFeatureSink, QgsFeatureRequest, QgsProcessingFeedback, QgsMessageLog,
    QgsFillSymbol, QgsSingleSymbolRenderer, QgsSymbol, QgsRendererCategory,
    QgsCategorizedSymbolRenderer, QgsPalLayerSettings, QgsTextFormat, 
    QgsTextBufferSettings, QgsVectorLayerSimpleLabeling
)
from qgis.gui import QgsMessageBar

from .resources import *
from .dockwidget import Ui_DockWidget

import os, os.path, sys, io, tempfile, traceback, base64, re, time
import builtins
import logging
import requests
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
            except requests.exceptions.Timeout:
                self.error.emit("Request timeout - server did not respond in time")
            except requests.exceptions.ConnectionError:
                self.error.emit(f"Cannot connect to backend server at {self.backend_url}")
            except requests.exceptions.RequestException as e:
                self.error.emit(f"Network error: {e}")
        except Exception as e:
            self.error.emit(f"Worker error: {e}\n{traceback.format_exc()}")



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
        
        self.loading = False
        self.loading_index = 0
        self._last_status_update_ms = 0

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
            QTimer.singleShot(1000, self.hide_progress)

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
        
        scope['processing_feedback'] = _UIFeedback(
            self.update_wave_message, 
            label="Processing..."
        )

        proc_mod = scope['processing']
        if proc_mod and hasattr(proc_mod, 'run'):
            proxy = _RunProgressProxy(self.update_wave_message)
            try:
                scope['_orig_processing_run'] = proc_mod.run
                proc_mod.run = proxy.wrap(proc_mod.run)
            except Exception as e:
                logger.warning(f"Failed to wrap processing.run: {e}")
        
        return scope
    
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
        if (self.ui and obj == self.ui.text_query and 
            event.type() == QEvent.KeyPress):
            if (event.key() == Qt.Key_Return and 
                event.modifiers() == Qt.ControlModifier):
                self.process_query()
                return True
        return super().eventFilter(obj, event)

    def toggle_ask_run(self):
        if self.ui and self.ui.chk_ask_run.isChecked():
            self.ui.btn_ask.setText("Ask and Run\n(Ctrl+Enter)")
        elif self.ui:
            self.ui.btn_ask.setText("Ask\n(Ctrl+Enter)")

    def _collect_field_samples(self, vlayer, limit_values=5, scan_limit=500):
        try:
            if not vlayer or vlayer.type() != QgsMapLayer.VectorLayer:
                return []
            fields = vlayer.fields()
            field_count = len(fields)
            if field_count == 0:
                return []

            buckets = [[] for _ in range(field_count)]
            req = QgsFeatureRequest()
            req.setFlags(QgsFeatureRequest.NoGeometry)
            req.setSubsetOfAttributes(list(range(field_count)))

            def safe_to_text(val, max_len=120):
                if val is None:
                    return None
                try:
                    if isinstance(val, (bytes, bytearray)):
                        try:
                            s = val.decode("utf-8", errors="replace")
                        except Exception:
                            s = val.decode("latin1", errors="replace")
                        return s[:max_len]
                    from datetime import date, datetime, time
                    if isinstance(val, (date, datetime, time)):
                        return str(val)[:max_len]
                    if isinstance(val, (list, tuple, dict, set)):
                        return str(val)[:max_len]
                    return str(val)[:max_len]
                except Exception:
                    try:
                        return repr(val)[:max_len]
                    except Exception:
                        return None

            count = 0
            for f in vlayer.getFeatures(req):
                count += 1
                attrs = f.attributes() if hasattr(f, "attributes") else []
                if not attrs:
                    if count >= scan_limit:
                        break
                    continue
                upto = min(field_count, len(attrs))
                for idx in range(upto):
                    val = attrs[idx]
                    if val in (None, ""):
                        continue
                    txt = safe_to_text(val)
                    if txt in (None, ""):
                        continue
                    b = buckets[idx]
                    if len(b) < limit_values and txt not in b:
                        b.append(txt)
                if all(len(b) >= limit_values for b in buckets) or count >= scan_limit:
                    break

            result = []
            for i, fld in enumerate(fields):
                result.append({
                    "name": fld.name(),
                    "samples": buckets[i]
                })
            return result
        except Exception as e:
            logger.error(f"Field sampling error: {e}")
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

            run_btn.clicked.connect(
                lambda _, edit=text_edit: self.run_message_from_chat(edit.toPlainText())
            )
            copy_btn.clicked.connect(
                lambda _, edit=text_edit: self.copy_to_clipboard(edit.toPlainText())
            )

        return msg_widget
    
    def _extract_non_code_text(self, text: str) -> str:
        import re
        pattern = re.compile(r"```(?:python)?\s*([\s\S]*?)```", re.IGNORECASE)
        return pattern.sub("", text).strip()

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
        final_code = self._prepare_code_for_execution(code)
        self.run_code_string(final_code)

    def _prepare_code_for_execution(self, raw_code):
        imports = [
            "from qgis.core import *",
            "from qgis.gui import *", 
            "from qgis.analysis import *",
            "from qgis.processing import *",
            "from qgis.utils import *",
            "import processing",
            "try:",
            "    from qgis.PyQt5.QtCore import *",
            "    from qgis.PyQt5.QtGui import *",
            "    from qgis.PyQt5.QtWidgets import *",
            "except ImportError:",
            "    from PyQt5.QtCore import *",
            "    from PyQt5.QtGui import *",
            "    from PyQt5.QtWidgets import *",
            "import tempfile",
            "import os",
            "import random",
            "iface = qgis.utils.iface",
            ""
        ]
        
        add_imports = "\n".join(imports)
        return add_imports + raw_code
    
    def handle_response(self, response_text: str):
            try:
                if not self.ui:
                    return

                display_text, code_blocks = self._parse_backend_response(response_text)

                try:
                    should_run = bool(self.ui.chk_ask_run.isChecked())
                except Exception:
                    should_run = False

                if code_blocks:
                    filtered = [b.strip() for b in code_blocks if b and b.strip()]
                    if filtered:
                        chosen = filtered[-1]
                        self.append_chat_message("assistant", chosen)
                        if should_run:
                            self.start_wave_progress("Executing code")
                            final_code = self._prepare_code_for_execution(chosen)
                            self.run_code_string(final_code)
                else:
                    self.append_chat_message("assistant-print", display_text.strip())

                self.ui.status_label.setText("Response processed")
                self.ui.status_label.setStyleSheet(
                    f"background-color: {self.success_status_color}; color: black;"
                )
            finally:
                self.stop_wave_progress("Done")
                if self.ui:
                    self.ui.btn_ask.setEnabled(True)

    def handle_error(self, error_message: str):
        try:
            if not self.ui:
                return

            msg = str(error_message).strip()
            if not msg:
                msg = "Unknown error"

            self.append_chat_message("assistant-print", f"Error:\n{msg}")

            self.ui.status_label.setText("Request failed")
            self.ui.status_label.setStyleSheet(
                f"background-color: {self.error_status_color}; color: white;"
            )

        finally:
            self.stop_wave_progress("Error")
            if self.ui:
                self.ui.btn_ask.setEnabled(True)

    def _build_context_text(self, ctx: dict) -> str:
        if not ctx:
            return "No context."

        lines = []

        p = ctx.get("project", {})
        lines.append("======== Project Info ========")
        for k in ("title", "fileName", "layerCount", "crs", "homePath"):
            if k in p:
                lines.append(f"  {k}: {p[k]}")
        lines.append("")


        layers = ctx.get("layers", [])
        lines.append("======== All Layers in Project ========")
        if layers:
            for li in layers:
                lines.append(f"  Layer Name: {li.get('name','(unknown)')}")
                for k, v in li.items():
                    if k == "name":
                        continue
                    if k == "fields" and isinstance(v, list) and len(v) > 12:
                        v = v[:12] + ["..."]
                    lines.append(f"    {k}: {v}")
                lines.append("  ----------------------")
        else:
            lines.append("  No layers in the project.")
        lines.append("")

        lines.append("======== Active Layer Info ========")
        al = ctx.get("active_layer")
        if isinstance(al, str):
            lines.append(f"  name: {al}")
        elif al:
            for k, v in al.items():
                lines.append(f"  {k}: {v}")
        else:
            lines.append("  No active layer selected.")
        lines.append("")

        view = ctx.get("view", {})
        lines.append("======== Current Map View Info ========")
        for k, v in view.items():
            lines.append(f"  {k}: {v}")

        return "\n".join(lines)


    def _build_prompt(self, user_input: str, context: dict = None, history: list = None, max_history: int = 6):
        lines = []

        lines.append("======= QueryGIS Instruction =======")
        lines.append(
            "You are a QGIS code assistant. Write runnable PyQGIS code blocks.\n"
            "- Prefer using existing layers by name.\n"
            "- Print helpful messages on errors."
        )
        lines.append("")

        if context:
            ctx = dict(context)
            try:
                for li in ctx.get("layers", []):
                    if "fields" in li and isinstance(li["fields"], list) and len(li["fields"]) > 12:
                        li["fields"] = li["fields"][:12] + ["..."]
            except Exception:
                pass

            lines.append("======= QGIS Context =======")
            lines.append(json.dumps(ctx, ensure_ascii=False, indent=2))
            lines.append("")


        if history:
            subset = history[-max_history:]
            lines.append("======= Chat History (latest) =======")
            for h in subset:
                role = h.get("role", "user")
                content = (h.get("content") or "").strip()
                if not content:
                    continue
                if len(content) > 1200:
                    content = content[:1200] + " …(truncated)"
                lines.append(f"[{role}] {content}")
            lines.append("")

        lines.append("======= User's Request =======")
        lines.append(user_input.strip())
        lines.append("")

        lines.append("======= Output Format Hint =======")
        lines.append("Return Python code for QGIS. If explanation is needed, put it above the code block.")
        lines.append("If code is returned, wrap it in triple backticks with `python`.")
        lines.append("")

        return "\n".join(lines)



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

            if "processing.run" in code_string:
                code_string = self._inject_processing_feedback(code_string)

            self.update_wave_message("Executing code")
            exec(code_string, exec_scope)
            execution_success = True

            output = captured_output.getvalue().strip()
            
            self.ui.status_label.setText("Code execution succeeded!")
            self.ui.status_label.setStyleSheet(
                f"background-color: {self.success_status_color}; color: black;"
            )
            
            if output:
                self.append_chat_message("assistant-print", f"Print output:\n{output}")
            
            self.stop_wave_progress("Execution completed successfully!")
            
        except Exception as e:
            execution_success = False
            error_details = traceback.format_exc()
            logger.error(f"Code execution error: {error_details}")
            
            self.ui.status_label.setText(f"Execution Error: {str(e)}")
            self.ui.status_label.setStyleSheet(
                f"background-color: {self.error_status_color}; color: white;"
            )
            self.append_chat_message("assistant-print", f"Execution Error:\n{error_details}")
            self.stop_wave_progress("Error occurred")
            
        finally:
            end_time = time.time()
            execution_time = end_time - start_time
            
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
            
            self._add_execution_result_to_chat(execution_success, execution_time, error_details)

    def _collect_qgis_context(self):
        project = QgsProject.instance()

        project_info = {
            "title": project.title(),
            "fileName": project.fileName(),
            "layerCount": len(project.mapLayers()),
            "crs": project.crs().authid(),
            "homePath": project.homePath(),
        }

        layers_info = []
        for layer_id, layer_obj in project.mapLayers().items():
            layer_info = {
                "name": layer_obj.name(),
                "id": layer_id,
                "type": ("VectorLayer" if layer_obj.type() == QgsMapLayer.VectorLayer else
                        "RasterLayer" if layer_obj.type() == QgsMapLayer.RasterLayer else "Unknown"),
                "crs": layer_obj.crs().authid(),
                "source": layer_obj.source()
            }

            if isinstance(layer_obj, QgsVectorLayer):
                layer_info["featureCount"] = layer_obj.featureCount()
                layer_info["fields"] = [f.name() for f in layer_obj.fields()]
                layer_info["fieldSamples"] = self._collect_field_samples(layer_obj, limit_values=5, scan_limit=500)
            elif isinstance(layer_obj, QgsRasterLayer):
                layer_info["width"] = layer_obj.width()
                layer_info["height"] = layer_obj.height()
                layer_info["bandCount"] = layer_obj.bandCount()

            layers_info.append(layer_info)

        active_layer = self.iface.activeLayer()
        active_layer_name = active_layer.name() if active_layer else None

        map_canvas = self.iface.mapCanvas()
        visible_extent = map_canvas.extent()
        view_info = {
            "extent": {
                "xMin": visible_extent.xMinimum(),
                "yMin": visible_extent.yMinimum(),
                "xMax": visible_extent.xMaximum(),
                "yMax": visible_extent.yMaximum(),
            },
            "scale": map_canvas.scale(),
            "crs": map_canvas.mapSettings().destinationCrs().authid()
        }

        return {
            "project": project_info,
            "layers": layers_info,
            "active_layer": active_layer_name,
            "view": view_info
        }

    def _add_execution_result_to_chat(self, execution_success, execution_time, error_details=None):
        if not self.ui:
            return

        if execution_time < 1:
            time_str = f"{execution_time*1000:.0f}ms"
        elif execution_time < 60:
            time_str = f"{execution_time:.1f}s"
        else:
            m = int(execution_time // 60)
            s = execution_time % 60
            time_str = f"{m}m {s:.1f}s"

        if execution_success:
            last = self.chat_history[-1] if self.chat_history else None
            if not last or last.get("role") != "assistant-print":
                self.append_chat_message("assistant-print", f"Execution complete · {time_str}")
        else:
            last_line = ""
            if error_details:
                lines = error_details.strip().split("\n")
                if lines:
                    last_line = lines[-1].strip()
            self.append_chat_message("assistant-print", f"Execution failed · {time_str}\n{last_line}")

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
    
    def process_query(self):
        if not self.ui:
            self.iface.messageBar().pushMessage("Error", "UI not initialized.", level=QgsMessageBar.Critical)
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

        self.append_chat_message("user", user_input)
        self.ui.text_query.clear()
        self.ui.btn_ask.setEnabled(False)

        try:
            model_name = "gemini-2.5-flash"

            context_dict = self._collect_qgis_context()
            context_text = self._build_context_text(context_dict)

            payload = {
                "api_key": api_key,
                "context": context_text,
                "user_input": user_input,
                "model": model_name
            }

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


