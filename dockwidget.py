import csv
from html import escape
from math import ceil, floor, isfinite, log10
from pathlib import Path
from random import sample
from tempfile import NamedTemporaryFile

from qgis.PyQt import QtCore, QtGui, QtWidgets

from . import resources
from .result_tools import open_chart_preview, preview_report, save_chart, save_report

QsciLexerPython = QsciScintilla = None
if QtCore.QT_VERSION_STR.startswith("5."):
    try:
        from PyQt5.Qsci import QsciLexerPython, QsciScintilla
    except ImportError:
        pass


_ = QtCore.QCoreApplication.translate
UI_ASSETS = Path(__file__).resolve().parent / "ui_assets"
FONT_SIZES = {"S": 10, "M": 12, "L": 14}
DEFAULT_LAYER_HEIGHT = 270
CHART_PREVIEW_ROWS = 8


class ResultCardData:
    def __init__(self, title, columns, rows, *, chart=None, view=None, chart_type="bar"):
        self.title = str(title)
        self.query = ""
        self.analysis_target = None
        self.columns = tuple(str(column) for column in columns)
        self.rows = tuple(tuple(self._cell(value) for value in row) for row in rows)
        if not self.columns or any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Result rows must match the result columns.")
        if view not in (None, "table", "chart", "both") or (chart is None and view in ("chart", "both")):
            raise ValueError("A chart view requires category and value column indexes.")
        if chart_type not in ("bar", "line", "scatter", "histogram"):
            raise ValueError("Chart type must be 'bar', 'line', 'scatter', or 'histogram'.")

        self.show_table = view != "chart"
        self.chart_type = chart_type
        self.chart_columns = ()
        self.chart_rows = []
        if chart is not None and view != "table":
            category, value = chart
            if category == value or any(type(index) is not int or not 0 <= index < len(self.columns)
                                        for index in (category, value)):
                raise ValueError("Chart indexes must identify two different result columns.")
            self.chart_columns = (self.columns[category], self.columns[value])
            for row in self.rows:
                number = self._number(row[value])
                if chart_type == "scatter" and self._number(row[category]) is None:
                    continue
                if chart_type == "histogram" and number is not None and number < 0:
                    raise ValueError("Histogram values must be nonnegative.")
                if number is not None or chart_type in ("line", "histogram"):
                    self.chart_rows.append((row[category], row[value] if number is not None else None, number))

    @staticmethod
    def _number(value):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return number if isfinite(number) else None

    @staticmethod
    def _cell(value):
        if isinstance(value, QtCore.QVariant):
            value = None if value.isNull() else value.value()
        if isinstance(value, (QtCore.QDate, QtCore.QDateTime, QtCore.QTime)):
            return value.toString(QtCore.Qt.ISODate)
        if value is None or isinstance(value, (str, int, float)):
            return value
        return str(value)

    def write_csv(self, path, *, chart=False):
        columns = self.chart_columns if chart else self.columns
        rows = ((label, value) for label, value, _ in self.chart_rows) if chart else self.rows
        path = Path(path)
        temporary_path = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="",
                                    dir=path.parent, delete=False) as stream:
                temporary_path = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows(rows)
            temporary_path.replace(path)
            temporary_path = None
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()


class _ResultTableModel(QtCore.QAbstractTableModel):
    def __init__(self, result, parent):
        super().__init__(parent)
        self.result = result

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.result.rows)

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.result.columns)

    def headerData(self, section, orientation, role=QtCore.Qt.DisplayRole):
        if (orientation == QtCore.Qt.Horizontal and 0 <= section < len(self.result.columns)
                and role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole)):
            return self.result.columns[section]
        return None

    def data(self, index, role=QtCore.Qt.DisplayRole):
        if not index.isValid():
            return None
        value = self.result.rows[index.row()][index.column()]
        if role in (QtCore.Qt.DisplayRole, QtCore.Qt.ToolTipRole):
            return "" if value is None else str(value)
        if role == QtCore.Qt.UserRole:
            return value
        if role == QtCore.Qt.TextAlignmentRole and isinstance(value, (int, float)):
            return int(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        return None


class _ResultTableFilter(QtCore.QSortFilterProxyModel):
    def lessThan(self, left, right):
        return self._sort_key(left.data(QtCore.Qt.UserRole)) < self._sort_key(right.data(QtCore.Qt.UserRole))

    @staticmethod
    def _sort_key(value):
        if value is None:
            return (2, "")
        if isinstance(value, int) or isinstance(value, float) and isfinite(value):
            return (0, value)
        return (1, str(value).casefold())


class _ResultBar(QtWidgets.QWidget):
    def __init__(self, number, lower, upper):
        super().__init__()
        scale = max(abs(lower), abs(upper)) or 1
        span = upper / scale - lower / scale
        self.zero = (-lower / scale) / span if span else 0
        self.end = self.zero + (number / scale) / span if span else 0
        self.setFixedHeight(8)

    def set_colors(self, track, fill):
        self.track = QtGui.QColor(track)
        self.fill = QtGui.QColor(fill)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(self.track)
        painter.drawRoundedRect(QtCore.QRectF(self.rect()), 4, 4)
        painter.setBrush(self.fill)
        start = min(self.zero, self.end) * self.width()
        width = abs(self.end - self.zero) * self.width()
        if width:
            painter.drawRoundedRect(QtCore.QRectF(start, 0, width, self.height()), 4, 4)
        if 0 < self.zero < 1:
            painter.setPen(self.fill)
            x = round(self.zero * self.width())
            painter.drawLine(x, 0, x, self.height())


class _ResultPlot(QtWidgets.QWidget):
    def __init__(self, result):
        super().__init__()
        self.rows = result.chart_rows
        self.chart_type = result.chart_type
        values = [number for _, _, number in self.rows if number is not None]
        self.y_axis = self._axis(values, include_zero=self.chart_type != "scatter")
        self.x_axis = self._axis([float(label) for label, _, _ in self.rows]) if self.chart_type == "scatter" else None
        self._plot_rect = QtCore.QRectF()
        self._scatter_points = []
        self.setMinimumHeight(210)
        self.setMouseTracking(True)
        self.setAccessibleName(result.title)
        self.setAccessibleDescription(_("DockWidget", "Chart positions: {count}").format(count=len(self.rows)))

    @staticmethod
    def _axis(values, include_zero=False):
        lower, upper = min(values), max(values)
        if include_zero:
            lower, upper = min(0, lower), max(0, upper)
        scale = max(abs(lower), abs(upper)) or 1
        lower, upper = lower / scale, upper / scale
        if lower == upper:
            lower, upper = (0, 1) if include_zero else (lower - 0.1, upper + 0.1)
        return scale, lower, upper

    @staticmethod
    def _axis_labels(axis):
        scale, lower, upper = axis
        step = (upper - lower) / 4
        increment = step * scale
        values = [(lower + step * index) * scale for index in range(5)]
        if 1e-5 <= increment and scale < 1e9:
            digits = max(0, 2 - floor(log10(increment)))
            labels = [f"{value:,.{digits}f}" for value in values]
            return [label.rstrip("0").rstrip(".") for label in labels] if digits else labels
        precision = min(16, max(4, 2 - floor(log10(step))))
        return [format(value, f".{precision}g") for value in values]

    def set_colors(self, grid, text, fill, points):
        self.grid = QtGui.QColor(grid)
        self.text = QtGui.QColor(text)
        self.fill = QtGui.QColor(fill)
        font = self.font()
        font.setPointSize(max(9, points - 2))
        self.setFont(font)
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        metrics = painter.fontMetrics()
        labels = self._axis_labels(self.y_axis)
        label_width = max(metrics.horizontalAdvance(label) for label in labels)
        left = label_width + 9
        top = metrics.height() / 2
        bottom = self.height() - metrics.height() - 11
        width = max(1, self.width() - left - 8)
        height = max(1, bottom - top)
        self._plot_rect = QtCore.QRectF(left, top, width, height)

        for step, label in enumerate(labels):
            y = bottom - step * height / 4
            painter.setPen(self.grid)
            painter.drawLine(QtCore.QLineF(left, y, left + width, y))
            painter.setPen(self.text)
            painter.drawText(QtCore.QRectF(0, y - top, label_width, metrics.height()),
                             QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter, label)

        if self.x_axis:
            for step, label in enumerate(self._axis_labels(self.x_axis)):
                x = left + step * width / 4
                cell_width = min(92, width / 4)
                cell_left = max(left, min(x - cell_width / 2, left + width - cell_width))
                alignment = (QtCore.Qt.AlignLeft if step == 0 else
                             QtCore.Qt.AlignRight if step == 4 else QtCore.Qt.AlignHCenter)
                painter.setPen(self.grid)
                painter.drawLine(QtCore.QLineF(x, bottom, x, bottom + 4))
                painter.setPen(self.text)
                label = metrics.elidedText(label, QtCore.Qt.ElideRight, int(cell_width))
                painter.drawText(QtCore.QRectF(cell_left, bottom + 6, cell_width, metrics.height()),
                                 alignment | QtCore.Qt.AlignVCenter, label)

        y_scale, y_min, y_max = self.y_axis
        slot = width / len(self.rows)
        label_step = max(1, ceil(86 / slot))
        path = QtGui.QPainterPath()
        markers = []
        bars = []
        connected = False
        for index, (label, _, number) in enumerate(self.rows):
            x = left + (index + 0.5) * slot
            if not self.x_axis and index % label_step == 0:
                cell_width = min(slot * label_step, 2 * min(x, self.width() - x))
                text = metrics.elidedText("" if label is None else str(label), QtCore.Qt.ElideRight,
                                          max(0, int(cell_width) - 6))
                painter.setPen(self.text)
                painter.drawText(QtCore.QRectF(x - cell_width / 2, bottom + 6, cell_width, metrics.height()),
                                 QtCore.Qt.AlignCenter, text)
            if number is None:
                connected = False
                continue
            fraction = (number / y_scale - y_min) / (y_max - y_min)
            point = QtCore.QPointF(x, bottom - fraction * height)
            if self.x_axis:
                x_scale, x_min, x_max = self.x_axis
                point.setX(left + (float(label) / x_scale - x_min) / (x_max - x_min) * width)
                markers.append(point)
                continue
            if self.chart_type == "histogram":
                gap = min(1, slot / 5)
                bars.append(QtCore.QRectF(x - slot / 2 + gap / 2, point.y(), slot - gap, bottom - point.y()))
                continue
            if connected:
                path.lineTo(point)
            else:
                path.moveTo(point)
            if len(self.rows) <= 40 or not connected:
                markers.append(point)
            connected = True

        painter.setPen(QtGui.QPen(self.fill, 2))
        painter.drawPath(path)
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(self.fill)
        for bar in bars:
            painter.drawRect(bar)
        for point in markers:
            painter.drawEllipse(point, 3, 3)
        if self.x_axis:
            self._scatter_points = markers

    def mouseMoveEvent(self, event):
        if not self._plot_rect.contains(QtCore.QPointF(event.pos())):
            QtWidgets.QToolTip.hideText()
            return
        if self.x_axis:
            position = QtCore.QPointF(event.pos())
            index, point = min(enumerate(self._scatter_points), key=lambda item: (item[1] - position).manhattanLength())
            if (point - position).manhattanLength() > 14:
                QtWidgets.QToolTip.hideText()
                return
        else:
            fraction = (event.pos().x() - self._plot_rect.left()) / self._plot_rect.width()
            index = min(len(self.rows) - 1, int(fraction * len(self.rows)))
        label, value, _number = self.rows[index]
        value_text = str(value) if value is not None else _("DockWidget", "No value")
        label_text = "" if label is None else str(label)
        QtWidgets.QToolTip.showText(event.globalPos(), f"<qt>{escape(label_text)} · {escape(value_text)}</qt>", self)

STARTER_PROMPTS = (
    ("Create an RGB composite",
     "Create a natural-color RGB composite from the red, green, and blue bands of the selected layers."),
    ("Calculate NDVI",
     "Create an NDVI raster from the red and near-infrared bands of the selected satellite imagery."),
    ("Calculate NBR",
     "Create an NBR raster from the near-infrared and SWIR2 bands of the selected satellite imagery."),
    ("Create a slope map",
     "Calculate slope in degrees from the selected DEM and add the result as a new layer."),
    ("Create a hillshade",
     "Create a hillshade from the selected DEM using an azimuth of 315° and a sun elevation of 45°."),
    ("Create 10 m contours",
     "Extract elevation contours at 10-meter intervals from the selected DEM."),
    ("Classify slope ranges",
     "Reclassify the selected slope raster into 0–<5°, 5–<15°, 15–<30°, and 30° or steeper."),
    ("Clip imagery to a boundary",
     "Clip the selected raster using the selected polygon boundary and add the result as a new raster layer."),
    ("Find mean elevation by zone",
     "Calculate the mean elevation from the selected DEM within each selected polygon and save it in a new field."),
    ("Create a 500 m buffer",
     "Create a 500-meter buffer around each feature in the selected vector layer and add the result as a new layer."),
    ("Clip a vector layer",
     "Clip the selected vector layer using the selected polygon boundary. Ask me which layer to clip if it is unclear."),
    ("Extract overlapping areas",
     "Intersect the two selected polygon layers and keep attributes from both layers."),
    ("Count points by area",
     "Count how many features from the selected point layer fall inside each polygon of the selected polygon layer."),
    ("Create a point heatmap",
     "Create a density heatmap from the selected point layer to show where points are concentrated."),
    ("Find the nearest facilities",
     "For each point in the selected origin layer, find the nearest facility in the selected facility layer and calculate the distance. Ask me which layer contains the origins if it is unclear."),
    ("Create polygon centroids",
     "Create a centroid for each feature in the selected polygon layer and preserve the original attributes."),
    ("Merge vector layers",
     "Merge the selected vector layers into one layer and add a field showing each feature's source layer."),
    ("Fix invalid geometries",
     "Repair invalid geometries in the selected vector layer and add the result as a new layer."),
    ("Match the project CRS",
     "Reproject the selected vector layer to the current QGIS project CRS and add the result as a new layer."),
    ("Create points from X and Y",
     "Create a point layer from the X and Y columns of the selected table. Ask me if the coordinate columns or CRS are unclear."),
)


class _DockSurface(QtWidgets.QWidget):
    """Paint the scroll content and layer card independently of the host theme."""

    def set_colors(self, background, border=None):
        self._background = QtGui.QColor(background)
        self._border = QtGui.QColor(border) if border else None
        self.update()

    def paintEvent(self, event):
        if not hasattr(self, "_background"):
            return
        painter = QtGui.QPainter(self)
        if self._border is None:
            painter.fillRect(event.rect(), self._background)
        else:
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setBrush(self._background)
            painter.setPen(self._border)
            painter.drawRoundedRect(QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 12, 12)


class _QuestionInput(QtWidgets.QPlainTextEdit):
    """Fit the input to its visual lines, including lines created by wrapping."""

    MAX_VISIBLE_LINES = 6

    def __init__(self):
        super().__init__()
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._fit_timer = QtCore.QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_height)
        self.textChanged.connect(self.schedule_fit)

    def schedule_fit(self):
        self._fit_timer.start(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.schedule_fit()

    def _fit_height(self):
        document = self.document()
        margin = document.documentMargin()
        width = max(1, self.maximumViewportSize().width() - 2 * margin)
        lines = 0
        block = document.firstBlock()
        while block.isValid() and lines <= self.MAX_VISIBLE_LINES:
            layout = QtGui.QTextLayout(block.text(), self.font(), self.viewport())
            layout.setTextOption(document.defaultTextOption())
            layout.beginLayout()
            block_lines = 0
            while lines + block_lines <= self.MAX_VISIBLE_LINES:
                line = layout.createLine()
                if not line.isValid():
                    break
                line.setLineWidth(width)
                block_lines += 1
            layout.endLayout()
            lines += max(1, block_lines)
            block = block.next()

        needs_scroll = lines > self.MAX_VISIBLE_LINES
        scroll_policy = QtCore.Qt.ScrollBarAsNeeded if needs_scroll else QtCore.Qt.ScrollBarAlwaysOff
        if self.verticalScrollBarPolicy() != scroll_policy:
            self.setVerticalScrollBarPolicy(scroll_policy)
        padding = self.height() - self.viewport().height() + 2 * margin
        height = ceil(min(self.MAX_VISIBLE_LINES, max(2, lines)) * self.fontMetrics().lineSpacing() + padding)
        if self.height() != height:
            self.setFixedHeight(height)
        if needs_scroll and self.hasFocus():
            self.ensureCursorVisible()


class _ReplyText(QtWidgets.QTextEdit):
    """Wrap prose without letting wide lines or tall replies resize the conversation."""

    def __init__(self, message, chat_viewport):
        super().__init__()
        self._chat_viewport = chat_viewport
        self._resize_timer = QtCore.QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._fit_height)
        self.setObjectName("resultMessage")
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Fixed)
        self.setReadOnly(True)
        self.setTabChangesFocus(True)
        self.setAcceptRichText(False)
        self.setWordWrapMode(QtGui.QTextOption.WordWrap)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)

        self.document().documentLayout().documentSizeChanged.connect(self._schedule_resize)
        self.horizontalScrollBar().rangeChanged.connect(self._schedule_resize)
        chat_viewport.installEventFilter(self)
        self.setPlainText(message)
        self._schedule_resize()

    def eventFilter(self, obj, event):
        if obj is self._chat_viewport and event.type() == QtCore.QEvent.Resize:
            self._schedule_resize()
        return super().eventFilter(obj, event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_resize()

    def _schedule_resize(self):
        self._resize_timer.start(0)

    def _fit_height(self):
        text_height = self.document().size().height()
        frame_height = self.height() - self.viewport().height()
        chat_height = self._chat_viewport.height()
        # Leave space for the question and the collapsed code review above the reply.
        maximum = max(56, min(360, chat_height // 2, chat_height - 136))
        height = min(maximum, ceil(text_height + frame_height))
        if self.height() != height:
            self.setFixedHeight(height)


class _DockSplitterHandle(QtWidgets.QSplitterHandle):
    def paintEvent(self, event):
        pen = QtGui.QPen(self.palette().color(QtGui.QPalette.WindowText))
        color = pen.color()
        color.setAlpha(85)
        pen.setColor(color)
        pen.setWidthF(2.5)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(pen)
        x, y = self.width() / 2, self.height() / 2
        painter.drawLine(QtCore.QPointF(x - 15, y), QtCore.QPointF(x + 15, y))


class _DockSplitter(QtWidgets.QSplitter):
    def createHandle(self):
        return _DockSplitterHandle(self.orientation(), self)


class Ui_DockWidget:
    """Build the QueryGIS dock and its conversation widgets."""

    _pretendard_family = None

    def setupUi(self, dock, font_size="M", rerun_result=None):
        self._rerun_result = rerun_result
        dock.setObjectName("DockWidget")
        dock.setWindowTitle(_("DockWidget", "QueryGIS"))
        dock.resize(460, 680)
        self.dockWidgetContents = QtWidgets.QWidget(dock)
        self.dockWidgetContents.setObjectName("querygisPanel")
        self._apply_font()
        root = QtWidgets.QVBoxLayout(self.dockWidgetContents)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        header = QtWidgets.QHBoxLayout()
        icon = QtWidgets.QLabel()
        icon.setPixmap(QtGui.QIcon(":/plugins/query_gis/icon.png").pixmap(24, 24))
        header.addWidget(icon)
        self.title_label = QtWidgets.QLabel(_("DockWidget", "QueryGIS"))
        self.title_label.setObjectName("panelTitle")
        header.addWidget(self.title_label)
        header.addStretch()
        self.btn_settings = QtWidgets.QToolButton()
        self.btn_settings.setObjectName("settingsButton")
        self.btn_settings.setText(_("DockWidget", "Settings"))
        self.btn_settings.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.btn_settings.setIcon(self._settings_icon())
        self.btn_settings.setIconSize(QtCore.QSize(20, 20))
        self.btn_settings.setAccessibleName(self.btn_settings.text())
        self.btn_settings.setToolTip(self.btn_settings.text())
        self.btn_settings.setCheckable(True)
        self.btn_settings.setAutoRaise(True)
        self.btn_settings.setCursor(QtCore.Qt.PointingHandCursor)
        header.addWidget(self.btn_settings)
        root.addLayout(header)

        self.google_icon = QtGui.QIcon(str(UI_ASSETS / "google-g.png"))
        self._build_settings()

        body = QtWidgets.QWidget()
        self._stack = QtWidgets.QStackedLayout(body)
        self._stack.setStackingMode(QtWidgets.QStackedLayout.StackAll)
        root.addWidget(body, 1)
        self.workspace = QtWidgets.QWidget()
        self.workspace.setObjectName("querygisWorkspace")
        self.workspace.setAttribute(QtCore.Qt.WA_StyledBackground)
        self._stack.addWidget(self.workspace)
        main = QtWidgets.QVBoxLayout(self.workspace)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(8)

        self.chatScrollArea = QtWidgets.QScrollArea()
        self.chatScrollArea.setObjectName("conversation")
        self.chatScrollArea.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.chatScrollArea.viewport().setObjectName("conversationViewport")
        self.chatScrollArea.viewport().setAttribute(QtCore.Qt.WA_StyledBackground)
        self.chatScrollArea.setWidgetResizable(True)
        self.chatScrollArea.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        chat_scrollbar = self.chatScrollArea.verticalScrollBar()
        self._chat_scroll_maximum = chat_scrollbar.maximum()
        self._chat_follow_position = chat_scrollbar.value()
        self._latest_question = None
        self._keep_question_visible = False
        chat_scrollbar.rangeChanged.connect(self._follow_chat_growth)
        self.chat_surface = _DockSurface()
        self.chatLayout = QtWidgets.QVBoxLayout(self.chat_surface)
        self.chatLayout.setContentsMargins(2, 8, 4, 8)
        self.chatLayout.setSpacing(10)
        self.empty_chat = self._build_empty_chat()
        self.chatLayout.addWidget(self.empty_chat, 1)
        self.chatLayout.addStretch()
        self.chatScrollArea.setWidget(self.chat_surface)
        self.layers_panel = _DockSurface()
        layers_card_layout = QtWidgets.QGridLayout(self.layers_panel)
        layers_card_layout.setContentsMargins(12, 9, 12, 8)
        layers_layout = QtWidgets.QVBoxLayout()
        layers_card_layout.addLayout(layers_layout, 0, 0)
        layers_layout.setSpacing(7)
        self.layers_panel.setMinimumHeight(105)
        self.label_layers = QtWidgets.QLabel(_("DockWidget", "Selected Layers"))
        self.label_layers.setObjectName("layersTitle")
        layers_layout.addWidget(self.label_layers)
        self.layer_scroll = layer_scroll = QtWidgets.QScrollArea()
        layer_scroll.setObjectName("layerList")
        layer_scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        layer_scroll.viewport().setObjectName("layerViewport")
        layer_scroll.viewport().setAttribute(QtCore.Qt.WA_StyledBackground)
        layer_scroll.setWidgetResizable(True)
        layer_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.layer_surface = _DockSurface()
        self.layerListLayout = QtWidgets.QVBoxLayout(self.layer_surface)
        self.layerListLayout.setContentsMargins(0, 1, 0, 1)
        self.layerListLayout.setSpacing(4)
        self.layerListLayout.addStretch()
        layer_scroll.setWidget(self.layer_surface)
        layers_layout.addWidget(layer_scroll)
        self.empty_layers = QtWidgets.QLabel(_("DockWidget", "Please select layers to operate on"))
        self.empty_layers.setObjectName("emptyLayers")
        self.empty_layers.setAlignment(QtCore.Qt.AlignCenter)
        self.empty_layers.setWordWrap(True)
        self.empty_layers.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        self.empty_layers.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        layers_card_layout.addWidget(self.empty_layers, 0, 0)
        self.empty_layers.raise_()
        self.content_splitter = _DockSplitter(QtCore.Qt.Vertical)
        self.content_splitter.setObjectName("contentSplitter")
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.setHandleWidth(12)
        self.content_splitter.addWidget(self.chatScrollArea)
        self.content_splitter.addWidget(self.layers_panel)
        self.content_splitter.setStretchFactor(0, 1)
        self.content_splitter.setStretchFactor(1, 0)
        self.content_splitter.handle(1).setToolTip(_("DockWidget", "Drag to resize Selected Layers"))
        main.addWidget(self.content_splitter, 1)

        composer_panel = QtWidgets.QFrame()
        composer_panel.setObjectName("composerPanel")
        composer = QtWidgets.QVBoxLayout(composer_panel)
        composer.setContentsMargins(7, 7, 8, 7)
        composer.setSpacing(6)
        self.text_query = _QuestionInput()
        self.text_query.setObjectName("questionInput")
        self.text_query.setAccessibleName(_("DockWidget", "GIS question"))
        shortcut_text = _("DockWidget", "Ctrl + Enter to send")
        self.text_query.setAccessibleDescription(shortcut_text)
        self.text_query.setPlaceholderText(_("DockWidget", "Ask about your layers or describe an analysis…"))
        composer.addWidget(self.text_query)
        composer_footer = QtWidgets.QHBoxLayout()
        composer_footer.setContentsMargins(9, 0, 0, 0)
        shortcut_hint = QtWidgets.QLabel(shortcut_text)
        shortcut_hint.setObjectName("shortcutHint")
        composer_footer.addWidget(shortcut_hint, 1, QtCore.Qt.AlignVCenter)
        self.btn_ask = QtWidgets.QPushButton(_("DockWidget", "Ask"))
        self.btn_ask.setObjectName("askButton")
        self.btn_ask.setToolTip(_("DockWidget", "Ask and automatically run the analysis (Ctrl+Enter)"))
        self.btn_ask.setCursor(QtCore.Qt.PointingHandCursor)
        composer_footer.addWidget(self.btn_ask, 0, QtCore.Qt.AlignVCenter)
        composer.addLayout(composer_footer)
        main.addWidget(composer_panel)

        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setRange(0, 100)
        self.progressBar.setTextVisible(False)
        self.progressBar.setFixedHeight(4)
        self.progressBar.hide()
        main.addWidget(self.progressBar)
        self.etaLabel = QtWidgets.QLabel()
        self.etaLabel.setObjectName("mutedLabel")
        self.etaLabel.hide()
        main.addWidget(self.etaLabel)
        self.status_label = QtWidgets.QLabel(_("DockWidget", "Status: Ready"))
        self.status_label.setObjectName("statusLabel")
        self.status_label.setTextFormat(QtCore.Qt.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        main.addWidget(self.status_label)
        self._build_auth_overlay()
        self.set_font_size(font_size)
        dock.setWidget(self.dockWidgetContents)
        self.content_splitter.setSizes([400, DEFAULT_LAYER_HEIGHT])

    def _follow_chat_growth(self, _minimum, maximum):
        bar = self.chatScrollArea.verticalScrollBar()
        following = bar.value() >= self._chat_scroll_maximum or bar.value() == self._chat_follow_position
        self._chat_scroll_maximum = maximum
        if following:
            self._scroll_to_latest()

    def scroll_to_latest(self, keep_question_visible):
        self._keep_question_visible = keep_question_visible
        self._scroll_to_latest()

    def _scroll_to_latest(self):
        bar = self.chatScrollArea.verticalScrollBar()
        target = bar.maximum()
        if self._keep_question_visible and self._latest_question is not None:
            target = min(target, max(0, self._latest_question.y()))
        self._chat_follow_position = target
        bar.setValue(target)

    def _settings_icon(self):
        pixmap = QtGui.QPixmap(48, 48)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.transparent)
        points = (
            (10, 2), (14, 2), (15, 5), (17, 6), (20, 5), (22, 9),
            (20, 11), (20, 13), (22, 15), (20, 19), (17, 18), (15, 19),
            (14, 22), (10, 22), (9, 19), (7, 18), (4, 19), (2, 15),
            (4, 13), (4, 11), (2, 9), (4, 5), (7, 6), (9, 5),
        )
        gear = QtGui.QPainterPath()
        gear.setFillRule(QtCore.Qt.OddEvenFill)
        gear.addPolygon(QtGui.QPolygonF([QtCore.QPointF(x, y) for x, y in points]))
        gear.closeSubpath()
        gear.addEllipse(QtCore.QPointF(12, 12), 3.5, 3.5)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.fillPath(gear, self.dockWidgetContents.palette().color(QtGui.QPalette.WindowText))
        painter.end()
        return QtGui.QIcon(pixmap)

    def _close_icon(self):
        pixmap = QtGui.QPixmap(36, 36)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.transparent)
        pen = QtGui.QPen(self.dockWidgetContents.palette().color(QtGui.QPalette.WindowText))
        pen.setWidthF(1.8)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(pen)
        painter.drawLine(QtCore.QPointF(4.5, 4.5), QtCore.QPointF(13.5, 13.5))
        painter.drawLine(QtCore.QPointF(13.5, 4.5), QtCore.QPointF(4.5, 13.5))
        painter.end()
        return QtGui.QIcon(pixmap)

    def _refresh_icon(self, color):
        pixmap = QtGui.QPixmap(40, 40)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.transparent)
        pen = QtGui.QPen(QtGui.QColor(color))
        pen.setWidthF(1.7)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(pen)
        painter.drawArc(QtCore.QRectF(3.5, 3.5, 13, 13), 30 * 16, 285 * 16)
        painter.drawPolyline(QtGui.QPolygonF([
            QtCore.QPointF(15.6, 2.8), QtCore.QPointF(15.6, 6.75), QtCore.QPointF(11.7, 6.75),
        ]))
        painter.end()
        return QtGui.QIcon(pixmap)

    def _copy_icon(self, color):
        pixmap = QtGui.QPixmap(36, 36)
        pixmap.setDevicePixelRatio(2)
        pixmap.fill(QtCore.Qt.transparent)
        pen = QtGui.QPen(QtGui.QColor(color))
        pen.setWidthF(1.5)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setPen(pen)
        back = QtGui.QPainterPath(QtCore.QPointF(3, 11.5))
        back.lineTo(3, 4)
        back.quadTo(3, 2.5, 4.5, 2.5)
        back.lineTo(11.5, 2.5)
        painter.drawPath(back)
        painter.drawRoundedRect(QtCore.QRectF(6, 5.5, 9, 10), 1.5, 1.5)
        painter.end()
        return QtGui.QIcon(pixmap)

    def _build_empty_chat(self):
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addStretch()
        self.starter_title = QtWidgets.QLabel(_("DockWidget", "What would you like to do in QGIS?"))
        self.starter_title.setAlignment(QtCore.Qt.AlignCenter)
        self.starter_title.setWordWrap(True)
        layout.addWidget(self.starter_title)
        layout.addSpacing(4)
        self.starter_buttons = []
        for _index in range(3):
            button = QtWidgets.QPushButton()
            button.setObjectName("starterPrompt")
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.clicked.connect(lambda checked=False, widget=button: self._prefill_query(widget.property("promptText")))
            layout.addWidget(button, alignment=QtCore.Qt.AlignHCenter)
            self.starter_buttons.append(button)
        self.btn_more_prompts = QtWidgets.QPushButton(_("DockWidget", "what else?"))
        self.btn_more_prompts.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_more_prompts.clicked.connect(self._shuffle_starter_prompts)
        layout.addWidget(self.btn_more_prompts, alignment=QtCore.Qt.AlignHCenter)
        self._shuffle_starter_prompts()
        layout.addStretch()
        return panel

    def _shuffle_starter_prompts(self):
        visible_labels = {button.text() for button in self.starter_buttons}
        choices = [entry for entry in STARTER_PROMPTS if entry[0] not in visible_labels]
        for button, (label, prompt) in zip(self.starter_buttons, sample(choices, len(self.starter_buttons))):
            button.setText(label)
            button.setProperty("promptText", prompt)

    def _style_starter_prompts(self, font_family, points, text, accent, accent_hover, border):
        font_rule = f'font-family: "{font_family}";' if font_family else ""
        self.starter_title.setStyleSheet(
            f"{font_rule} font-size: {points + 10}pt; font-weight: bold; color: {text}; background: transparent;"
        )
        for button in self.starter_buttons:
            font = QtGui.QFont(button.font())
            if font_family:
                font.setFamily(font_family)
            font.setPointSize(points)
            font.setBold(False)
            radius = ceil((QtGui.QFontMetrics(font, button).height() + 16) / 2)
            button.setFixedHeight(radius * 2)
            button.setStyleSheet(f"""
                QPushButton {{
                    {font_rule} font-size: {points}pt; font-weight: normal;
                    background: #ffffff; color: #1b2b24; border: 1px solid {accent};
                    border-radius: {radius}px; padding: 0 14px;
                }}
                QPushButton:hover {{ background: #eaf4ee; }}
                QPushButton:focus {{ border-color: {accent_hover}; }}
                QPushButton:disabled {{ color: #52675c; border-color: {border}; }}
            """)

    def _prefill_query(self, text):
        self.text_query.setPlainText(text)
        self.text_query.moveCursor(QtGui.QTextCursor.End)
        self.text_query.setFocus()

    def _apply_font(self):
        if self._pretendard_family is None:
            for weight in ("Regular", "Bold"):
                try:
                    font_data = (UI_ASSETS / f"Pretendard-{weight}.otf").read_bytes()
                except OSError:
                    continue
                # Windows Qt can reject these OpenType faces when registered by path.
                font_id = QtGui.QFontDatabase.addApplicationFontFromData(QtCore.QByteArray(font_data))
                if font_id >= 0 and self._pretendard_family is None:
                    families = QtGui.QFontDatabase.applicationFontFamilies(font_id)
                    if families:
                        type(self)._pretendard_family = families[0]
        if self._pretendard_family:
            font = self.dockWidgetContents.font()
            font.setFamily(self._pretendard_family)
            font.setStyleStrategy(QtGui.QFont.PreferAntialias)
            self.dockWidgetContents.setFont(font)

    def set_font_size(self, size):
        self._font_points = FONT_SIZES.get(size, FONT_SIZES["M"])
        self.font_sizes.button(self._font_points).setChecked(True)
        self._apply_style(self._pretendard_family, self._font_points)
        self.text_query.schedule_fit()
        for editor in self.workspace.findChildren(QtWidgets.QWidget, "codePreview"):
            self._size_code_preview(editor)
        for table in self.workspace.findChildren(QtWidgets.QTableView, "resultTable"):
            self._size_result_table(table)

    def _size_code_preview(self, editor):
        font = editor.font()
        font.setPointSize(self._font_points)
        editor.setFont(font)
        if QsciScintilla is not None and isinstance(editor, QsciScintilla):
            background, text, muted, keyword, string, number = self._code_colors
            lexer = editor.lexer()
            lexer.setDefaultFont(font)
            lexer.setFont(font)
            lexer.setDefaultPaper(QtGui.QColor(background))
            lexer.setPaper(QtGui.QColor(background))
            lexer.setColor(QtGui.QColor(text))
            lexer.setColor(QtGui.QColor(keyword), QsciLexerPython.Keyword)
            lexer.setColor(QtGui.QColor(muted), QsciLexerPython.Comment)
            lexer.setColor(QtGui.QColor(number), QsciLexerPython.Number)
            for style in (QsciLexerPython.SingleQuotedString, QsciLexerPython.DoubleQuotedString,
                          QsciLexerPython.TripleSingleQuotedString, QsciLexerPython.TripleDoubleQuotedString):
                lexer.setColor(QtGui.QColor(string), style)
            editor.setMarginsFont(font)
            editor.setMarginsBackgroundColor(QtGui.QColor(background))
            editor.setMarginsForegroundColor(QtGui.QColor(muted))
            count = editor.lines()
            editor.setMarginWidth(0, "0" * max(2, len(str(count))) + " ")
        else:
            count = editor.blockCount()
        line_height = QtGui.QFontMetrics(font).lineSpacing()
        editor.setFixedHeight(min(16, max(3, count)) * line_height + 32)

    def _google_button(self):
        button = QtWidgets.QPushButton(_("DockWidget", "Sign in with Google"))
        button.setObjectName("googleButton")
        button.setIcon(self.google_icon)
        button.setIconSize(QtCore.QSize(18, 18))
        button.setCursor(QtCore.Qt.PointingHandCursor)
        return button

    def _build_key_entry(self, name):
        row = QtWidgets.QHBoxLayout()
        field = QtWidgets.QLineEdit()
        field.setPlaceholderText(_("DockWidget", "Paste your API key"))
        field.setEchoMode(QtWidgets.QLineEdit.Password)
        field.setAccessibleName(name)
        row.addWidget(field, 1)
        button = QtWidgets.QPushButton(_("DockWidget", "Save"))
        button.setObjectName("askButton")
        button.setEnabled(False)
        field.textChanged.connect(self._update_key_button)
        field.returnPressed.connect(button.click)
        row.addWidget(button)
        return row, field, button

    def _divider(self):
        divider = QtWidgets.QFrame()
        divider.setObjectName("accountSeparator")
        divider.setFixedHeight(1)
        return divider

    def _auth_message_label(self):
        label = QtWidgets.QLabel()
        label.setObjectName("mutedLabel")
        label.setWordWrap(True)
        label.setTextFormat(QtCore.Qt.PlainText)
        label.hide()
        return label

    def _build_settings(self):
        self._saved_api_key = ""
        self.settings_panel = QtWidgets.QFrame()
        self.settings_panel.setObjectName("settingsPanel")
        self.settings_panel.setMaximumWidth(400)
        content = QtWidgets.QVBoxLayout(self.settings_panel)
        content.setContentsMargins(16, 16, 16, 16)
        content.setSpacing(12)
        self.connect_title = QtWidgets.QLabel(_("DockWidget", "Settings"))
        self.connect_title.setObjectName("panelTitle")
        self.connect_title.setWordWrap(True)
        heading = QtWidgets.QHBoxLayout()
        heading.addWidget(self.connect_title, 1)
        self.btn_close_settings = QtWidgets.QToolButton()
        self.btn_close_settings.setObjectName("closeSettingsButton")
        self.btn_close_settings.setIcon(self._close_icon())
        self.btn_close_settings.setIconSize(QtCore.QSize(18, 18))
        self.btn_close_settings.setFixedSize(30, 30)
        self.btn_close_settings.setToolTip(_("DockWidget", "Close settings"))
        self.btn_close_settings.setAccessibleName(self.btn_close_settings.toolTip())
        self.btn_close_settings.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_close_settings.clicked.connect(lambda: self.btn_settings.setChecked(False))
        heading.addWidget(self.btn_close_settings, 0, QtCore.Qt.AlignTop)
        content.addLayout(heading)
        self.font_controls = QtWidgets.QWidget()
        font_row = QtWidgets.QHBoxLayout(self.font_controls)
        font_row.setContentsMargins(0, 0, 0, 0)
        font_label = QtWidgets.QLabel(_("DockWidget", "Font Size"))
        font_label.setObjectName("sectionLabel")
        font_row.addWidget(font_label, 1)
        self.font_sizes = QtWidgets.QButtonGroup(self.settings_panel)
        for size, points in FONT_SIZES.items():
            button = QtWidgets.QPushButton(size)
            button.setCheckable(True)
            button.setCursor(QtCore.Qt.PointingHandCursor)
            button.setAccessibleName(_("DockWidget", "Font size %1").replace("%1", size))
            self.font_sizes.addButton(button, points)
            font_row.addWidget(button)
        content.addWidget(self.font_controls)

        self.account_separator = self._divider()
        content.addWidget(self.account_separator)

        self.key_controls = QtWidgets.QWidget()
        key_layout = QtWidgets.QVBoxLayout(self.key_controls)
        key_layout.setContentsMargins(0, 0, 0, 0)
        self.label_api = QtWidgets.QLabel(_("DockWidget", "API Key"))
        self.label_api.setObjectName("sectionLabel")
        key_layout.addWidget(self.label_api)
        key_row, self.line_apikey, self.btn_save_key = self._build_key_entry(self.label_api.text())
        self.label_api.setBuddy(self.line_apikey)
        key_layout.addLayout(key_row)
        content.addWidget(self.key_controls)

        self.label_welcome_user = QtWidgets.QLabel()
        self.label_welcome_user.setObjectName("mutedLabel")
        self.label_welcome_user.setTextFormat(QtCore.Qt.PlainText)
        self.label_welcome_user.setWordWrap(True)
        self.label_welcome_user.hide()
        content.addWidget(self.label_welcome_user)

        self.subscription_panel = QtWidgets.QWidget()
        subscription_row = QtWidgets.QHBoxLayout(self.subscription_panel)
        subscription_row.setContentsMargins(0, 0, 0, 0)
        subscription_text = QtWidgets.QVBoxLayout()
        subscription_text.setSpacing(4)
        self.label_plan = QtWidgets.QLabel()
        self.label_plan.setObjectName("sectionLabel")
        self.label_calls = QtWidgets.QLabel()
        self.label_calls.setObjectName("mutedLabel")
        for label in (self.label_plan, self.label_calls):
            label.setTextFormat(QtCore.Qt.PlainText)
            label.setWordWrap(True)
            subscription_text.addWidget(label)
        subscription_row.addLayout(subscription_text, 1)
        self.btn_usage_refresh = QtWidgets.QToolButton()
        self.btn_usage_refresh.setObjectName("usageRefreshButton")
        self.btn_usage_refresh.setText(_("DockWidget", "Refresh usage"))
        self.btn_usage_refresh.setAccessibleName(self.btn_usage_refresh.text())
        self.btn_usage_refresh.setToolTip(self.btn_usage_refresh.text())
        self.btn_usage_refresh.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        self.btn_usage_refresh.setIconSize(QtCore.QSize(18, 18))
        self.btn_usage_refresh.setCursor(QtCore.Qt.PointingHandCursor)
        subscription_row.addWidget(self.btn_usage_refresh, alignment=QtCore.Qt.AlignTop)
        self.subscription_panel.hide()
        content.addWidget(self.subscription_panel)

        self.btn_google_login = self._google_button()
        content.addWidget(self.btn_google_login)
        codex_preview = QtWidgets.QWidget()
        codex_preview.setObjectName("codexPreview")
        codex_preview.setToolTip(_("DockWidget", "Coming soon"))
        codex_layout = QtWidgets.QVBoxLayout(codex_preview)
        codex_layout.setContentsMargins(0, 0, 0, 0)
        self.btn_codex = QtWidgets.QPushButton(_("DockWidget", "Connect with Codex"))
        self.btn_codex.setEnabled(False)
        self.btn_codex.setAccessibleDescription(codex_preview.toolTip())
        self.btn_codex.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        codex_blur = QtWidgets.QGraphicsBlurEffect(self.btn_codex)
        codex_blur.setBlurRadius(2)
        self.btn_codex.setGraphicsEffect(codex_blur)
        codex_layout.addWidget(self.btn_codex)
        content.addWidget(codex_preview)
        self.auth_status = self._auth_message_label()
        content.addWidget(self.auth_status)
        self.settings_panel.hide()
        self.settings_panel.setEnabled(False)

    def set_api_key(self, api_key):
        self._saved_api_key = api_key
        self.line_apikey.setText(api_key)
        self.line_enterprise_key.setText(api_key)
        self._update_key_button()

    def _update_key_button(self):
        api_key = self.line_apikey.text().strip()
        self.btn_save_key.setEnabled(api_key != self._saved_api_key)
        if hasattr(self, "line_enterprise_key"):
            entered_key = self.line_enterprise_key.text().strip()
            reuse_key = bool(entered_key) and entered_key == self._saved_api_key
            self.btn_use_enterprise_key.setText(_("DockWidget", "Use key") if reuse_key else _("DockWidget", "Save"))
            self.btn_use_enterprise_key.setEnabled(bool(entered_key))

    def _build_connection_pages(self):
        self.connection_panel = QtWidgets.QFrame()
        self.connection_panel.setObjectName("settingsPanel")
        self.connection_panel.setMaximumWidth(400)
        content = QtWidgets.QVBoxLayout(self.connection_panel)
        content.setContentsMargins(20, 26, 20, 24)
        content.setSpacing(12)

        signup = QtWidgets.QLabel(_("DockWidget", "Sign up to get free QueryGIS credits!"))
        signup.setObjectName("panelTitle")
        signup.setWordWrap(True)
        signup.setAlignment(QtCore.Qt.AlignCenter)
        content.addWidget(signup)
        self.btn_create_account = QtWidgets.QPushButton(_("DockWidget", "Create an account"))
        self.btn_create_account.setObjectName("askButton")
        self.btn_create_account.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_create_account.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl("https://www.3dlabs-querygis.co.kr/"))
        )
        content.addWidget(self.btn_create_account)
        content.addSpacing(6)
        content.addWidget(self._divider())
        content.addSpacing(6)

        existing = QtWidgets.QLabel(_("DockWidget", "Already have an account?"))
        existing.setAlignment(QtCore.Qt.AlignCenter)
        existing.setWordWrap(True)
        content.addWidget(existing)
        self.btn_connection_google = self._google_button()
        content.addWidget(self.btn_connection_google)
        content.addSpacing(6)
        content.addWidget(self._divider())
        content.addSpacing(6)
        self.btn_enterprise_access = QtWidgets.QPushButton(_("DockWidget", "Education or enterprise customer?"))
        self.btn_enterprise_access.setObjectName("connectionLink")
        self.btn_enterprise_access.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_enterprise_access.clicked.connect(lambda: self._set_enterprise_page(True))
        content.addWidget(self.btn_enterprise_access, alignment=QtCore.Qt.AlignHCenter)
        self.connection_status = self._auth_message_label()
        content.addWidget(self.connection_status)

        self.enterprise_panel = QtWidgets.QFrame()
        self.enterprise_panel.setObjectName("settingsPanel")
        self.enterprise_panel.setMaximumWidth(400)
        enterprise_layout = QtWidgets.QVBoxLayout(self.enterprise_panel)
        enterprise_layout.setContentsMargins(20, 20, 20, 20)
        enterprise_layout.setSpacing(12)
        self.btn_connection_back = QtWidgets.QPushButton(_("DockWidget", "Back"))
        self.btn_connection_back.setObjectName("connectionLink")
        self.btn_connection_back.setCursor(QtCore.Qt.PointingHandCursor)
        self.btn_connection_back.clicked.connect(lambda: self._set_enterprise_page(False))
        enterprise_layout.addWidget(self.btn_connection_back, alignment=QtCore.Qt.AlignLeft)
        title = QtWidgets.QLabel(_("DockWidget", "3DLabs QueryGIS API Key"))
        title.setObjectName("panelTitle")
        title.setWordWrap(True)
        enterprise_layout.addWidget(title)
        row, self.line_enterprise_key, self.btn_use_enterprise_key = self._build_key_entry(title.text())
        enterprise_layout.addLayout(row)

    def _set_enterprise_page(self, show):
        self._show_enterprise_key = show
        if not show:
            self.line_enterprise_key.setText(self._saved_api_key)
        self._update_overlay()
        if show:
            self.line_enterprise_key.setFocus()

    def _build_auth_overlay(self):
        self.auth_overlay = QtWidgets.QWidget()
        self.auth_overlay.setObjectName("authOverlay")
        overlay_layout = QtWidgets.QVBoxLayout(self.auth_overlay)
        overlay_layout.setContentsMargins(16, 12, 16, 12)
        overlay_layout.addStretch()
        card_row = QtWidgets.QHBoxLayout()
        card_row.addStretch(1)
        card_row.addWidget(self.settings_panel, 12)
        self._build_connection_pages()
        card_row.addWidget(self.connection_panel, 12)
        card_row.addWidget(self.enterprise_panel, 12)
        self.connection_panel.hide()
        self.enterprise_panel.hide()
        self.login_success_panel = QtWidgets.QFrame()
        self.login_success_panel.setObjectName("settingsPanel")
        self.login_success_panel.setMaximumWidth(400)
        success_layout = QtWidgets.QVBoxLayout(self.login_success_panel)
        success_layout.setContentsMargins(20, 20, 20, 20)
        success_layout.setSpacing(16)
        success_title = QtWidgets.QLabel(_("DockWidget", "Login Successful!"))
        success_title.setObjectName("panelTitle")
        success_title.setAlignment(QtCore.Qt.AlignCenter)
        success_layout.addWidget(success_title)
        self.btn_start_querygis = QtWidgets.QPushButton(_("DockWidget", "Start QueryGIS"))
        self.btn_start_querygis.setObjectName("askButton")
        self.btn_start_querygis.setCursor(QtCore.Qt.PointingHandCursor)
        success_layout.addWidget(self.btn_start_querygis)
        self.login_success_panel.hide()
        card_row.addWidget(self.login_success_panel, 12)
        card_row.addStretch(1)
        overlay_layout.addLayout(card_row)
        overlay_layout.addStretch()
        self._stack.addWidget(self.auth_overlay)
        self.auth_overlay.hide()
        self._auth_required = False
        self._show_login_success = False
        self._show_enterprise_key = False
        self.btn_settings.toggled.connect(self._update_overlay)

    def set_auth_required(self, required, show_login_success=False):
        if not required or not self._auth_required:
            self._show_enterprise_key = False
        self._auth_required = required
        self._show_login_success = show_login_success
        self.btn_settings.setEnabled(not (required or show_login_success))
        if required or show_login_success:
            self.btn_settings.setChecked(False)
        self._update_key_button()
        self._update_overlay()

    def _update_overlay(self):
        settings_open = self.btn_settings.isChecked()
        show_settings = settings_open and not (self._auth_required or self._show_login_success)
        show_connection = self._auth_required and not self._show_login_success
        active = show_settings or show_connection or self._show_login_success
        self.settings_panel.setEnabled(show_settings)
        self.settings_panel.setVisible(show_settings)
        self.connection_panel.setVisible(show_connection and not self._show_enterprise_key)
        self.enterprise_panel.setVisible(show_connection and self._show_enterprise_key)
        self.login_success_panel.setVisible(self._show_login_success)
        self.workspace.setEnabled(not active)
        if active and self.workspace.graphicsEffect() is None:
            blur = QtWidgets.QGraphicsBlurEffect(self.workspace)
            blur.setBlurRadius(7)
            self.workspace.setGraphicsEffect(blur)
        elif not active and self.workspace.graphicsEffect() is not None:
            self.workspace.setGraphicsEffect(None)
        self.auth_overlay.setVisible(active)
        self._stack.setCurrentWidget(self.auth_overlay if active else self.workspace)

    def set_auth_message(self, message):
        for label in (self.auth_status, self.connection_status):
            label.setText(message)
            label.setVisible(bool(message))

    def create_message(self, role, message):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        if role == "assistant":
            row.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
            layout.addWidget(self._code_review(message))
            return row
        if role == "assistant-result":
            layout.addWidget(self._result_card(message))
            return row
        if role != "user":
            layout.addWidget(_ReplyText(message, self.chatScrollArea.viewport()))
            return row

        label = QtWidgets.QLabel()
        label.setObjectName("userMessage")
        label.setTextFormat(QtCore.Qt.PlainText)
        label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        label.setWordWrap(True)
        label.setText(message)
        layout.addSpacing(18)
        layout.addStretch()
        layout.addWidget(label)
        self._latest_question = row
        return row

    def _result_card(self, result):
        card = QtWidgets.QFrame()
        card.setObjectName("resultCard")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 10)
        layout.setSpacing(8)

        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(result.title)
        title.setObjectName("sectionLabel")
        title.setTextFormat(QtCore.Qt.PlainText)
        title.setWordWrap(True)
        heading.addWidget(title, 1)
        save = QtWidgets.QPushButton(_("DockWidget", "Save CSV"))
        heading.addWidget(save, 0, QtCore.Qt.AlignTop)
        more = QtWidgets.QToolButton()
        more.setObjectName("resultMenuButton")
        more.setText(_("DockWidget", "More"))
        more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        more.setMenu(self._result_menu(more, card, result))
        heading.addWidget(more, 0, QtCore.Qt.AlignTop)
        layout.addLayout(heading)

        views = []
        if result.show_table:
            views.append((_("DockWidget", "Table"), self._result_table(result), False))
        if result.chart_columns:
            chart_label = {
                "bar": _("DockWidget", "Chart"),
                "line": _("DockWidget", "Line chart"),
                "scatter": _("DockWidget", "Scatter plot"),
                "histogram": _("DockWidget", "Histogram"),
            }[result.chart_type]
            views.append((chart_label, self._result_chart(result), True))

        tabs = None
        if len(views) > 1:
            tabs = QtWidgets.QTabBar()
            tabs.setObjectName("resultTabs")
            tabs.setExpanding(False)
            tabs.setDrawBase(False)
            layout.addWidget(tabs)

        for label, widget, _is_chart in views:
            if tabs is not None:
                tabs.addTab(label)
            layout.addWidget(widget)

        def select_view(index):
            for position, (_, widget, _) in enumerate(views):
                widget.setVisible(position == index)

        if tabs is not None:
            tabs.currentChanged.connect(select_view)
        select_view(0)
        save.clicked.connect(lambda: self._save_result_csv(
            card, result, views[tabs.currentIndex() if tabs is not None else 0][2]
        ))
        return card

    def _result_menu(self, button, parent, result):
        menu = QtWidgets.QMenu(button)
        if result.chart_columns:
            menu.addAction(_("DockWidget", "Open chart…"), lambda: open_chart_preview(
                parent, result.title, self._chart_preview(result)
            ))
            menu.addAction(_("DockWidget", "Save PNG…"), lambda: self._save_chart_image(parent, result, "png"))
            menu.addAction(_("DockWidget", "Save SVG…"), lambda: self._save_chart_image(parent, result, "svg"))
            menu.addSeparator()
        menu.addAction(_("DockWidget", "Save PDF report…"), lambda: self._save_result_file(
            parent, "QueryGIS-report.pdf", _("DockWidget", "Save report as PDF"),
            _("DockWidget", "PDF files (*.pdf)"), lambda path: self._with_report_chart(
                result, lambda chart: save_report(path, result, chart)
            )
        ))
        menu.addAction(_("DockWidget", "Print preview…"), lambda: self._with_report_chart(
            result, lambda chart: preview_report(parent, result, chart)
        ))
        if self._rerun_result and result.query:
            menu.addSeparator()
            menu.addAction(_("DockWidget", "Run with layer or field…"), lambda: self._rerun_result(result))
        return menu

    def _chart_preview(self, result, include_title=True):
        frame = QtWidgets.QFrame()
        frame.setObjectName("resultCard")
        frame.setStyleSheet(self.dockWidgetContents.styleSheet())
        frame.setFont(self.dockWidgetContents.font())
        layout = QtWidgets.QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        if include_title:
            title = QtWidgets.QLabel(result.title)
            title.setObjectName("sectionLabel")
            title.setTextFormat(QtCore.Qt.PlainText)
            title.setWordWrap(True)
            layout.addWidget(title)
        layout.addWidget(self._result_chart(result))
        frame.ensurePolished()
        width = 680
        height = max(layout.sizeHint().height(), layout.heightForWidth(width))
        frame.setFixedSize(width, height)
        layout.activate()
        return frame

    def _with_report_chart(self, result, action):
        try:
            action(self._chart_preview(result, include_title=False) if result.chart_columns else None)
        except (ImportError, OSError) as error:
            QtWidgets.QMessageBox.warning(self.dockWidgetContents, _("DockWidget", "Could not create report"), str(error))

    def _save_chart_image(self, parent, result, extension):
        file_filter = (_("DockWidget", "PNG images (*.png)") if extension == "png"
                       else _("DockWidget", "SVG images (*.svg)"))
        self._save_result_file(
            parent, f"QueryGIS-chart.{extension}", _("DockWidget", "Save chart"),
            file_filter, lambda path: save_chart(path, self._chart_preview(result))
        )

    def _result_table(self, result):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        search = QtWidgets.QLineEdit()
        search.setObjectName("resultTableFilter")
        search.setPlaceholderText(_("DockWidget", "Search table…"))
        search.setAccessibleName(_("DockWidget", "Search table"))
        search.setClearButtonEnabled(True)
        layout.addWidget(search)
        table = QtWidgets.QTableView()
        table.setObjectName("resultTable")
        table.setAccessibleName(result.title)
        filtered = _ResultTableFilter(table)
        filtered.setSourceModel(_ResultTableModel(result, filtered))
        filtered.setFilterCaseSensitivity(QtCore.Qt.CaseInsensitive)
        filtered.setFilterKeyColumn(-1)
        table.setModel(filtered)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        header = table.horizontalHeader()
        header.setDefaultSectionSize(120)
        header.setStretchLastSection(True)
        header.setTextElideMode(QtCore.Qt.ElideRight)
        if len(result.columns) <= 2:
            header.setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        table.setSortingEnabled(True)
        table.sortByColumn(-1, QtCore.Qt.AscendingOrder)
        layout.addWidget(table)
        count = len(result.rows)
        info = QtWidgets.QLabel()
        info.setObjectName("mutedLabel")
        layout.addWidget(info)

        def apply_filter(text):
            filtered.setFilterFixedString(text)
            matches = filtered.rowCount()
            info.setText(
                _("DockWidget", "Rows: {matches:,} of {count:,}").format(matches=matches, count=count)
                if text else _("DockWidget", "Rows: {count:,}").format(count=count)
            )
            self._size_result_table(table)

        search.textChanged.connect(apply_filter)
        apply_filter("")
        return widget

    def _size_result_table(self, table):
        font = table.font()
        font.setPointSize(self._font_points)
        table.setFont(font)
        row_height = QtGui.QFontMetrics(font).lineSpacing() + 10
        table.verticalHeader().setDefaultSectionSize(row_height)
        table.horizontalHeader().setFixedHeight(row_height)
        visible_rows = min(6, table.model().rowCount())
        table.setFixedHeight(row_height * (1 + visible_rows))

    def _result_chart(self, result):
        widget = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        measure = QtWidgets.QLabel(result.chart_columns[1])
        measure.setObjectName("mutedLabel")
        measure.setTextFormat(QtCore.Qt.PlainText)
        measure.setWordWrap(True)
        layout.addWidget(measure)

        if result.chart_type != "bar":
            if any(number is not None for _, _, number in result.chart_rows):
                chart = _ResultPlot(result)
                chart.set_colors(*self._result_plot_style)
                layout.addWidget(chart)
                if result.chart_type == "scatter":
                    axis = QtWidgets.QLabel(result.chart_columns[0])
                    axis.setObjectName("mutedLabel")
                    axis.setTextFormat(QtCore.Qt.PlainText)
                    axis.setWordWrap(True)
                    layout.addWidget(axis, 0, QtCore.Qt.AlignHCenter)
            else:
                layout.addWidget(QtWidgets.QLabel(_("DockWidget", "No numeric values to plot.")))
            return widget

        preview = result.chart_rows[:CHART_PREVIEW_ROWS]
        if not preview:
            layout.addWidget(QtWidgets.QLabel(_("DockWidget", "No numeric values to plot.")))
            return widget

        lower = min(0, min(number for _, _, number in preview))
        upper = max(0, max(number for _, _, number in preview))
        for name, value, number in preview:
            row = QtWidgets.QHBoxLayout()
            label = QtWidgets.QLabel("" if name is None else str(name))
            label.setTextFormat(QtCore.Qt.PlainText)
            label.setWordWrap(True)
            value_label = QtWidgets.QLabel(str(value))
            value_label.setTextFormat(QtCore.Qt.PlainText)
            row.addWidget(label, 1)
            row.addWidget(value_label)
            layout.addLayout(row)
            bar = _ResultBar(number, lower, upper)
            bar.set_colors(*self._result_bar_colors)
            layout.addWidget(bar)

        count = len(result.chart_rows)
        if count > len(preview):
            caption = _("DockWidget", "Showing {shown} of {count:,} values").format(shown=len(preview), count=count)
            note = QtWidgets.QLabel(caption)
            note.setObjectName("mutedLabel")
            note.setWordWrap(True)
            layout.addWidget(note)
        return widget

    def _save_result_csv(self, parent, result, chart):
        filename = "QueryGIS-chart.csv" if chart else "QueryGIS-table.csv"
        self._save_result_file(
            parent, filename, _("DockWidget", "Save results as CSV"), _("DockWidget", "CSV files (*.csv)"),
            lambda path: result.write_csv(path, chart=chart)
        )

    def _save_result_file(self, parent, filename, title, file_filter, action):
        path, _filter = QtWidgets.QFileDialog.getSaveFileName(
            parent, title, filename, file_filter
        )
        if not path:
            return
        path = Path(path)
        suffix = Path(filename).suffix
        if path.suffix.lower() != suffix:
            path = path.with_name(path.name + suffix)
            if path.exists() and QtWidgets.QMessageBox.question(
                parent, _("DockWidget", "Replace file?"),
                _("DockWidget", "Replace the existing file {name}?").format(name=path.name),
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No
            ) != QtWidgets.QMessageBox.Yes:
                return
        try:
            action(path)
        except (ImportError, OSError, csv.Error, UnicodeError) as error:
            QtWidgets.QMessageBox.warning(
                parent, _("DockWidget", "Could not save file"), str(error)
            )

    def _code_review(self, code):
        card = QtWidgets.QFrame()
        card.setObjectName("codeReview")
        layout = QtWidgets.QVBoxLayout(card)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(5)

        toolbar = QtWidgets.QHBoxLayout()
        review = QtWidgets.QToolButton()
        review.setObjectName("reviewCodeButton")
        review.setText(_("DockWidget", "Review Code"))
        review.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        review.setArrowType(QtCore.Qt.RightArrow)
        review.setCheckable(True)
        review.setCursor(QtCore.Qt.PointingHandCursor)
        toolbar.addWidget(review, 0, QtCore.Qt.AlignVCenter)
        toolbar.addStretch()
        copy = QtWidgets.QToolButton()
        copy.setObjectName("copyCodeButton")
        copy.setToolButtonStyle(QtCore.Qt.ToolButtonIconOnly)
        copy.setAccessibleName(_("DockWidget", "Copy code"))
        copy.setToolTip(copy.accessibleName())
        copy.setIcon(self._copy_code_icon)
        copy.setIconSize(QtCore.QSize(18, 18))
        copy.setFixedSize(30, 30)
        copy.setCursor(QtCore.Qt.PointingHandCursor)
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(code))
        copy.hide()
        toolbar.addWidget(copy, 0, QtCore.Qt.AlignVCenter)
        layout.addLayout(toolbar)

        if QsciScintilla is not None:
            editor = QsciScintilla()
            editor.setReadOnly(True)
            editor.setUtf8(True)
            editor.setMarginType(0, QsciScintilla.NumberMargin)
            editor.setMarginLineNumbers(0, True)
            editor.setLexer(QsciLexerPython(editor))
            editor.setText(code)
        else:
            editor = QtWidgets.QPlainTextEdit()
            editor.setReadOnly(True)
            editor.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
            editor.setPlainText(code)
        editor.setObjectName("codePreview")
        editor.setAccessibleName(_("DockWidget", "Generated Python code"))
        code_font = QtGui.QFont()
        code_font.setFamilies(["Consolas", "Menlo", "DejaVu Sans Mono", "monospace"])
        code_font.setStyleHint(QtGui.QFont.Monospace)
        editor.setFont(code_font)
        self._size_code_preview(editor)
        editor.hide()
        layout.addWidget(editor)

        def toggle_code(expanded):
            review.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
            editor.setVisible(expanded)
            copy.setVisible(expanded)

        review.toggled.connect(toggle_code)
        return card

    def _apply_style(self, font_family, points):
        palette = self.dockWidgetContents.palette()
        dark = palette.color(QtGui.QPalette.Window).lightness() < 128
        if dark:
            background, surface, inset = "#1d2523", "#26312e", "#202b27"
            text, muted, border = "#edf4f0", "#adbfb6", "#3a4a43"
            accent, accent_hover, user = "#126848", "#0e7651", "#22483a"
            veil = "rgba(19, 26, 23, 160)"
            chart_fill = "#70c5a2"
        else:
            background, surface, inset = "#f3f7f4", "#ffffff", "#eaf1ec"
            text, muted, border = "#1b2b24", "#52675c", "#d6e2db"
            accent, accent_hover, user = "#116245", "#0b5038", "#dcefe5"
            veil = "rgba(237, 245, 240, 160)"
            chart_fill = accent

        self._result_bar_colors = (inset, chart_fill)
        for bar in self.dockWidgetContents.findChildren(_ResultBar):
            bar.set_colors(*self._result_bar_colors)
        self._result_plot_style = (border, muted, chart_fill, points)
        for plot in self.dockWidgetContents.findChildren(_ResultPlot):
            plot.set_colors(*self._result_plot_style)
        self._code_colors = (background, text, muted, chart_fill,
                             "#ecc389" if dark else "#8a4300", "#c3a6ee" if dark else "#69428d")

        font_rule = f'font-family: "{font_family}";' if font_family else ""
        self.label_layers.setStyleSheet(
            f"background: transparent; color: {text}; {font_rule} font-size: {points}pt; font-weight: bold;"
        )
        warning_color = "#ff8585" if dark else "#b4232c"
        self.empty_layers.setStyleSheet(
            f"background: transparent; color: {warning_color}; {font_rule} font-size: {points}pt; font-weight: bold;"
        )
        self._style_starter_prompts(font_family, points, text, accent, accent_hover, border)
        self.btn_more_prompts.setStyleSheet(f"""
            QPushButton {{
                {font_rule} font-size: {max(9, points - 2)}pt; font-weight: normal;
                color: {muted}; background: transparent; border: 1px solid transparent;
                border-radius: 4px; padding: 2px 7px;
            }}
            QPushButton:hover {{ color: {text}; text-decoration: underline; }}
            QPushButton:focus {{ border-color: {accent}; }}
        """)
        self.chat_surface.set_colors(background)
        self.layers_panel.set_colors(surface, border)
        self.layer_surface.set_colors(surface)
        self.layer_surface.setStyleSheet(f"""
            QLabel {{ background: transparent; color: {text}; {font_rule} font-size: {points}pt; }}
            QLabel#mutedLabel {{ color: {muted}; }}
        """)
        for area, color in ((self.chatScrollArea, background), (self.layer_scroll, surface)):
            viewport = area.viewport()
            viewport_palette = viewport.palette()
            viewport_palette.setColor(QtGui.QPalette.Window, QtGui.QColor(color))
            viewport_palette.setColor(QtGui.QPalette.Base, QtGui.QColor(color))
            viewport.setPalette(viewport_palette)
            viewport.setAutoFillBackground(True)
        self.dockWidgetContents.setStyleSheet(f"""
            QLabel, QLineEdit, QPlainTextEdit#questionInput, QTextEdit#resultMessage, QPushButton, QToolButton {{
                {font_rule}
                font-size: {points}pt;
            }}
            QWidget#querygisPanel, QWidget#querygisWorkspace {{ background: {background}; }}
            QLabel {{ color: {text}; background: transparent; }}
            QLabel#panelTitle {{ font-size: {points + 3}pt; font-weight: bold; }}
            QLabel#sectionLabel {{ font-weight: 600; }}
            QLabel#mutedLabel, QLabel#statusLabel, QLabel#shortcutHint {{ color: {muted}; }}
            QLabel#shortcutHint {{ font-size: {max(9, points - 2)}pt; }}
            QLabel#statusLabel {{ padding: 1px 4px; border-radius: 5px; font-size: {max(9, points - 2)}pt; }}
            QWidget#authOverlay {{ background: {veil}; }}
            QFrame#settingsPanel, QFrame#composerPanel {{
                background: {surface}; border: 1px solid {border}; border-radius: 12px;
            }}
            QFrame#composerPanel {{ border-width: 2px; }}
            QFrame#accountSeparator {{ background: {border}; border: none; }}
            QLineEdit, QPlainTextEdit {{
                background: {surface}; color: {text};
                border: 1px solid {border}; border-radius: 8px;
            }}
            QScrollArea#conversation, QWidget#conversationViewport {{
                background: {background}; border: none;
            }}
            QScrollArea#layerList, QWidget#layerViewport {{
                background: {surface}; border: none;
            }}
            QWidget#layerRow {{ background: {inset}; border-radius: 7px; }}
            QSplitter#contentSplitter {{ background: {background}; }}
            QLineEdit, QPlainTextEdit {{ padding: 5px 7px; }}
            QPlainTextEdit#questionInput {{ border: none; background: {surface}; }}
            QPlainTextEdit#codePreview {{ background: {background}; font-size: {points}pt; }}
            QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {accent}; }}
            QTextEdit#resultMessage, QFrame#codeReview, QFrame#resultCard {{
                background: {surface}; color: {text}; border: 1px solid {border}; border-radius: 11px;
            }}
            QTableView#resultTable {{
                {font_rule} font-size: {points}pt;
                background: {surface}; color: {text};
                border: none; selection-background-color: {user}; selection-color: {text};
            }}
            QTableView#resultTable::item {{ border-bottom: 1px solid {border}; padding: 0 7px; }}
            QTableView#resultTable QHeaderView::section {{
                {font_rule} font-size: {points}pt; font-weight: bold;
                background: {inset}; color: {text}; padding: 3px 7px; border: none;
            }}
            QTabBar#resultTabs::tab {{
                {font_rule} font-size: {points}pt;
                background: {surface}; color: {muted}; border: 1px solid {border};
                border-radius: 6px; padding: 3px 10px; margin-right: 4px;
            }}
            QTabBar#resultTabs::tab:selected, QTabBar#resultTabs::tab:focus {{
                background: {user}; color: {text}; border-color: {accent};
            }}
            QLabel#userMessage, QTextEdit#resultMessage {{ padding: 8px 10px; }}
            QLabel#userMessage {{ background: {user}; border-radius: 11px; }}
            QPushButton, QToolButton {{
                background: {surface}; color: {text}; border: 1px solid {border};
                border-radius: 6px; padding: 5px 9px;
            }}
            QToolButton {{ background: transparent; border-color: transparent; }}
            QToolButton#settingsButton, QToolButton#settingsButton:hover,
            QToolButton#settingsButton:checked, QToolButton#settingsButton:focus,
            QToolButton#settingsButton:disabled {{
                background: transparent; border: none; padding: 6px;
            }}
            QToolButton#closeSettingsButton {{ border-radius: 15px; padding: 0; }}
            QToolButton#closeSettingsButton:hover {{ background: {user}; }}
            QToolButton#usageRefreshButton {{ padding: 5px; }}
            QToolButton#resultMenuButton {{ border-color: {border}; }}
            QToolButton#reviewCodeButton {{ padding: 5px 8px; }}
            QToolButton#copyCodeButton {{ padding: 0; }}
            QPushButton:hover, QToolButton:hover, QToolButton:checked {{ background: {user}; }}
            QPushButton:checked {{ background: {user}; border-color: {accent}; }}
            QPushButton:focus, QToolButton:focus {{ border-color: {accent}; }}
            QPushButton:disabled {{ background: {background}; color: {muted}; border-color: {border}; }}
            QPushButton#connectionLink {{
                background: transparent; color: {accent}; border: 1px solid transparent; padding: 4px 6px;
            }}
            QPushButton#connectionLink:hover {{ color: {text}; text-decoration: underline; }}
            QPushButton#connectionLink:focus {{ border-color: {accent}; }}
            QPushButton#connectionLink:disabled {{ color: {muted}; }}
            QPushButton#googleButton {{
                background: white; color: #1f1f1f; border-color: #747775;
                padding: 7px 12px; border-radius: 7px;
            }}
            QPushButton#googleButton:hover {{ background: #f2f2f2; }}
            QPushButton#googleButton:focus {{ border-color: {accent}; }}
            QPushButton#askButton {{
                background: {accent}; color: white; border-color: {accent};
                font-weight: bold; padding: 7px 14px;
            }}
            QPushButton#askButton:hover {{ background: {accent_hover}; }}
            QPushButton#askButton:disabled {{
                background: {inset}; color: {muted}; border-color: {border};
            }}
            QProgressBar {{ background: {inset}; border: none; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {accent}; border-radius: 2px; }}
        """)
        icon_color = "#b1b5b4" if dark else "#454948"
        self.btn_usage_refresh.setIcon(self._refresh_icon(icon_color))
        self._copy_code_icon = self._copy_icon(icon_color)
