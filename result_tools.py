from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory

from qgis.PyQt import QtCore, QtGui, QtWidgets


_ = QtCore.QCoreApplication.translate


class ChartZoomView(QtWidgets.QGraphicsView):
    def __init__(self, chart, parent):
        super().__init__(parent)
        scene = QtWidgets.QGraphicsScene(self)
        scene.addWidget(chart)
        scene.setSceneRect(QtCore.QRectF(chart.rect()))
        self.setScene(scene)
        self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)
        self.setRenderHint(QtGui.QPainter.Antialiasing)
        self.setAccessibleName(_("DockWidget", "Zoomable chart"))
        self._zoom_level = 0

    def fit_chart(self):
        self.resetTransform()
        self.fitInView(self.sceneRect(), QtCore.Qt.KeepAspectRatio)
        self._zoom_level = 0

    def zoom(self, steps):
        level = min(10, max(-4, self._zoom_level + steps))
        factor = 1.25 ** (level - self._zoom_level)
        self.scale(factor, factor)
        self._zoom_level = level

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            super().wheelEvent(event)
            return
        self.zoom(1 if delta > 0 else -1)
        event.accept()


def open_chart_preview(parent, title, chart):
    dialog = QtWidgets.QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(780, 550)
    layout = QtWidgets.QVBoxLayout(dialog)
    view = ChartZoomView(chart, dialog)
    controls = QtWidgets.QHBoxLayout()
    for label, action in (
        (_("DockWidget", "Zoom in"), lambda: view.zoom(1)),
        (_("DockWidget", "Zoom out"), lambda: view.zoom(-1)),
        (_("DockWidget", "Fit"), view.fit_chart),
    ):
        button = QtWidgets.QPushButton(label)
        button.clicked.connect(action)
        controls.addWidget(button)
    controls.addStretch()
    layout.addLayout(controls)
    layout.addWidget(view)
    QtCore.QTimer.singleShot(0, view.fit_chart)
    dialog.exec_()


def _chart_image(chart):
    image = QtGui.QImage(chart.size() * 2, QtGui.QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(2)
    image.fill(QtCore.Qt.transparent)
    chart.render(image)
    return image


def save_chart(path, chart):
    path = Path(path)
    output = QtCore.QSaveFile(str(path))
    if not output.open(QtCore.QIODevice.WriteOnly):
        raise OSError(output.errorString())
    if path.suffix.lower() == ".svg":
        from qgis.PyQt.QtSvg import QSvgGenerator

        generator = QSvgGenerator()
        generator.setOutputDevice(output)
        generator.setSize(chart.size())
        generator.setViewBox(chart.rect())
        painter = QtGui.QPainter()
        if not painter.begin(generator):
            raise OSError(_("DockWidget", "Could not start the SVG export."))
        try:
            chart.render(painter)
        finally:
            painter.end()
    elif not _chart_image(chart).save(output, "PNG"):
        raise OSError(_("DockWidget", "Could not encode the PNG image."))
    if not output.commit():
        raise OSError(output.errorString())


def _report_document(result, chart, printer):
    document = QtGui.QTextDocument()
    document.setDocumentMargin(4)
    page_size = printer.pageRect(printer.DevicePixel).size()
    document.setPageSize(QtCore.QSizeF(page_size))
    parts = [f"<h1>{escape(result.title)}</h1>"]
    if chart is not None:
        resource = QtCore.QUrl("querygis:result-chart")
        document.addResource(QtGui.QTextDocument.ImageResource, resource, _chart_image(chart))
        width = min(chart.width(), max(1, int(page_size.width() - 2 * document.documentMargin())))
        parts.append(f'<p><img src="{resource.toString()}" width="{width}"></p>')
    if result.show_table:
        parts.append('<table width="100%" border="0.5" cellspacing="0" cellpadding="4"><thead><tr>')
        parts.extend(f'<th bgcolor="#eaf1ec">{escape(column)}</th>' for column in result.columns)
        parts.append("</tr></thead><tbody>")
        for row in result.rows:
            parts.append("<tr>")
            parts.extend(f"<td>{escape('' if value is None else str(value))}</td>" for value in row)
            parts.append("</tr>")
        parts.append("</tbody></table>")
    document.setHtml("".join(parts))
    return document


def save_report(path, result, chart):
    from qgis.PyQt.QtPrintSupport import QPrinter

    path = Path(path)
    printer = QPrinter(QPrinter.ScreenResolution)
    printer.setOutputFormat(QPrinter.PdfFormat)
    printer.setPageMargins(18, 18, 18, 18, QPrinter.Millimeter)
    with TemporaryDirectory(dir=path.parent) as directory:
        temporary_path = Path(directory) / "result.pdf"
        printer.setOutputFileName(str(temporary_path))
        _report_document(result, chart, printer).print_(printer)
        if not temporary_path.is_file() or not temporary_path.stat().st_size:
            raise OSError(_("DockWidget", "Could not write the PDF report."))
        temporary_path.replace(path)


def preview_report(parent, result, chart):
    from qgis.PyQt.QtPrintSupport import QPrintPreviewDialog, QPrinter

    printer = QPrinter(QPrinter.ScreenResolution)
    printer.setPageMargins(18, 18, 18, 18, QPrinter.Millimeter)
    dialog = QPrintPreviewDialog(printer, parent)
    dialog.paintRequested.connect(lambda device: _report_document(result, chart, device).print_(device))
    dialog.exec_()
