# SPDX-License-Identifier: LGPL-2.1-or-later
"""Chat widgets for the assistant panel: messages, step cards and the composer.

Qt only, with no FreeCAD imports, so the widgets can be tested headlessly. All
colors derive from the widget palette, so FreeCAD's light and dark themes both
work; the stylesheet is rebuilt when the palette changes.
"""

import math
import weakref

try:
    from PySide import QtCore, QtGui
    try:
        from PySide import QtWidgets
    except ImportError:
        QtWidgets = QtGui
except ImportError:
    try:
        from PySide2 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide6 import QtCore, QtGui, QtWidgets

MAX_ENTRIES = 400
MAX_CODE_LINES = 12
MAX_INPUT_LINES = 8
Qt = QtCore.Qt


# Colors ----------------------------------------------------------------------

def mix(first, second, amount):
    """Blend two colors; amount 0 gives first, 1 gives second."""
    return QtGui.QColor(
        round(first.red() + (second.red() - first.red()) * amount),
        round(first.green() + (second.green() - first.green()) * amount),
        round(first.blue() + (second.blue() - first.blue()) * amount))


def luminance(color):
    def channel(value):
        value /= 255.0
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4
    return 0.2126 * channel(color.red()) + 0.7152 * channel(color.green()) + 0.0722 * channel(color.blue())


def contrast(first, second):
    light, dark = sorted((luminance(first), luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


class Theme:
    """Semantic colors derived from a palette."""

    def __init__(self, palette):
        role = QtGui.QPalette
        self.window = palette.color(role.Window)
        self.base = palette.color(role.Base)
        self.text = palette.color(role.WindowText)
        self.accent = palette.color(role.Highlight)
        self.accent_text = palette.color(role.HighlightedText)
        self.dark = luminance(self.window) < 0.35
        self.surface = mix(self.window, self.text, 0.045)
        self.code = mix(self.window, self.text, 0.08)
        self.border = mix(self.window, self.text, 0.16)
        self.muted = mix(self.text, self.window, 0.38)
        self.bubble = mix(self.window, self.accent, 0.22 if self.dark else 0.13)
        # Semantic tints, adjusted for light or dark surfaces.
        self.success = QtGui.QColor("#3fb950" if self.dark else "#1a7f37")
        self.danger = QtGui.QColor("#f85149" if self.dark else "#cf222e")
        self.warning = QtGui.QColor("#d29922" if self.dark else "#9a6700")

    @staticmethod
    def css(color):
        return color.name()


def source_palette(widget):
    """The application palette, which is FreeCAD's theme.

    Not the widget's own palette (its stylesheet rewrites it, feeding old colors
    back in) and not its parent's (a dock may carry a title-bar palette)."""
    return QtWidgets.QApplication.palette()


def stylesheet(theme):
    c = Theme.css
    return """
QWidget {{ color: {text}; }}
QPlainTextEdit, QTextBrowser {{ selection-background-color: {accent};
    selection-color: {accent_text}; }}
QWidget#chatCanvas, QScrollArea#chatView, QWidget#assistantRoot, QWidget#assistantHeader,
QWidget#composerArea, QWidget#barHolder {{ background: {window}; border: none; }}
QToolButton#flat, QToolButton#headerButton {{ background: transparent; border: none;
    border-radius: 6px; padding: 3px 6px; color: {text}; }}
QToolButton#flat:hover, QToolButton#headerButton:hover {{ background: {surface}; }}
QToolButton#flat:disabled, QToolButton#headerButton:disabled {{ color: {muted}; }}
QToolButton#headerButton::menu-indicator {{ image: none; width: 0px; }}
QFrame#userBubble {{ background: {bubble}; border-radius: 12px; }}
QFrame#stepCard, QFrame#emptyState {{ background: {surface}; border: 1px solid {border};
    border-radius: 8px; }}
QFrame#notice {{ background: {surface}; border-radius: 6px; }}
QPlainTextEdit#codeBlock {{ background: {code}; border: none; border-radius: 6px;
    padding: 4px; }}
QLabel#muted, QLabel#statusLine {{ color: {muted}; }}
QFrame#composer {{ background: {base}; border: 1px solid {border}; border-radius: 12px; }}
QFrame#composer[focused="true"] {{ border-color: {accent}; }}
QPlainTextEdit#composerInput {{ background: transparent; border: none; }}
QToolButton#chip {{ background: transparent; color: {text}; border: 1px solid {border};
    border-radius: 11px; padding: 2px 8px; }}
QToolButton#chip:hover {{ background: {surface}; }}
QToolButton#chip:checked {{ background: {bubble}; border-color: {accent}; }}
QToolButton#chip::menu-indicator {{ image: none; width: 0px; }}
QToolButton#sendButton {{ background: {accent}; border: none; border-radius: 14px; }}
QToolButton#sendButton:disabled {{ background: {border}; }}
QFrame#exampleChip {{ border: 1px solid {border}; border-radius: 8px; background: {window}; }}
QFrame#exampleChip:hover {{ border-color: {accent}; }}
QFrame#actionBar {{ background: {surface}; border: 1px solid {border}; border-radius: 8px; }}
QPushButton#primary {{ background: {accent}; color: {accent_text}; border: none;
    border-radius: 6px; padding: 4px 12px; }}
QPushButton#secondary {{ background: transparent; color: {text}; border: 1px solid {border};
    border-radius: 6px; padding: 4px 12px; }}
QPushButton#secondary:hover, QPushButton#primary:hover {{ border-color: {accent}; }}
""".format(window=c(theme.window), bubble=c(theme.bubble), surface=c(theme.surface),
           border=c(theme.border), code=c(theme.code), muted=c(theme.muted), base=c(theme.base),
           accent=c(theme.accent), accent_text=c(theme.accent_text), text=c(theme.text))


class _ThemeWatcher(QtCore.QObject):
    """Re-themes registered widgets when the application palette changes.

    Qt does not send palette events to stylesheet-styled children, and FreeCAD's
    widgets are always children, so the application's paletteChanged is used."""

    def __init__(self, app):
        super().__init__(app)
        self.widgets = weakref.WeakSet()
        signal = getattr(app, "paletteChanged", None)
        if signal is not None:
            signal.connect(self._changed)

    def _changed(self, *args):
        for widget in list(self.widgets):
            try:
                widget.apply_theme()
            except RuntimeError:
                pass  # Its Qt object is already gone.


_watcher = None


def _watch(widget):
    global _watcher
    app = QtWidgets.QApplication.instance()
    if app is None:
        return
    if _watcher is None:
        _watcher = _ThemeWatcher(app)
    _watcher.widgets.add(widget)


class Themed:
    """Mixin: style from the application palette, again whenever it changes."""

    theme = None

    def apply_theme(self):
        if getattr(self, "_theming", False):
            return
        _watch(self)
        self._theming = True  # setStyleSheet itself emits palette changes.
        try:
            self.theme = Theme(source_palette(self))
            sheet = stylesheet(self.theme)
            if self.styleSheet() != sheet:  # Unchanged sheets must not restyle (and loop).
                self.setStyleSheet(sheet)
            self.themed(self.theme)
        finally:
            self._theming = False

    def themed(self, theme):
        """Subclasses refresh painted icons and child colors here."""


class ThemedWidget(QtWidgets.QWidget, Themed):
    """A container whose descendants share the chat stylesheet."""

    def __init__(self, parent=None, name="assistantRoot"):
        super().__init__(parent)
        self.setObjectName(name)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.on_theme = []  # Callbacks for colors painted outside the stylesheet.
        self.apply_theme()

    def themed(self, theme):
        for callback in getattr(self, "on_theme", []):
            callback(theme)


# Icons -----------------------------------------------------------------------

def icon(name, color, size=16):
    """Small line icons painted in the theme's text color."""
    pixmap = QtGui.QPixmap(size * 2, size * 2)
    pixmap.fill(Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    painter.scale(2, 2)
    pen = QtGui.QPen(color, 1.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(pen)
    s = float(size)
    if name == "send":
        painter.drawLine(QtCore.QPointF(s / 2, s * 0.8), QtCore.QPointF(s / 2, s * 0.22))
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(s * 0.27, s * 0.45),
                                              QtCore.QPointF(s / 2, s * 0.22),
                                              QtCore.QPointF(s * 0.73, s * 0.45)]))
    elif name == "stop":
        painter.setBrush(color)
        painter.drawRoundedRect(QtCore.QRectF(s * 0.3, s * 0.3, s * 0.4, s * 0.4), 1.5, 1.5)
    elif name == "plus":
        painter.drawLine(QtCore.QPointF(s / 2, s * 0.22), QtCore.QPointF(s / 2, s * 0.78))
        painter.drawLine(QtCore.QPointF(s * 0.22, s / 2), QtCore.QPointF(s * 0.78, s / 2))
    elif name == "more":
        painter.setBrush(color)
        for x in (0.25, 0.5, 0.75):
            painter.drawEllipse(QtCore.QPointF(s * x, s / 2), 1.1, 1.1)
    elif name in ("chevron-right", "chevron-down"):
        points = ([(0.4, 0.28), (0.62, 0.5), (0.4, 0.72)] if name == "chevron-right"
                  else [(0.28, 0.4), (0.5, 0.62), (0.72, 0.4)])
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(s * x, s * y) for x, y in points]))
    elif name == "check":
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(s * 0.24, s * 0.52),
                                              QtCore.QPointF(s * 0.43, s * 0.7),
                                              QtCore.QPointF(s * 0.76, s * 0.32)]))
    elif name == "cross":
        painter.drawLine(QtCore.QPointF(s * 0.3, s * 0.3), QtCore.QPointF(s * 0.7, s * 0.7))
        painter.drawLine(QtCore.QPointF(s * 0.7, s * 0.3), QtCore.QPointF(s * 0.3, s * 0.7))
    elif name == "pause":
        painter.drawLine(QtCore.QPointF(s * 0.4, s * 0.3), QtCore.QPointF(s * 0.4, s * 0.7))
        painter.drawLine(QtCore.QPointF(s * 0.6, s * 0.3), QtCore.QPointF(s * 0.6, s * 0.7))
    elif name == "undo":
        painter.drawArc(QtCore.QRectF(s * 0.3, s * 0.3, s * 0.45, s * 0.42), 90 * 16, -250 * 16)
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(s * 0.22, s * 0.33),
                                              QtCore.QPointF(s * 0.36, s * 0.3),
                                              QtCore.QPointF(s * 0.38, s * 0.45)]))
    elif name == "dash":
        painter.drawLine(QtCore.QPointF(s * 0.3, s / 2), QtCore.QPointF(s * 0.7, s / 2))
    elif name == "dot":
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QtCore.QPointF(s / 2, s / 2), s * 0.28, s * 0.28)
    elif name == "eye":
        painter.drawEllipse(QtCore.QRectF(s * 0.16, s * 0.32, s * 0.68, s * 0.36))
        painter.setBrush(color)
        painter.drawEllipse(QtCore.QPointF(s / 2, s / 2), s * 0.09, s * 0.09)
    elif name == "bolt":
        painter.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(s * 0.56, s * 0.16),
                                              QtCore.QPointF(s * 0.32, s * 0.54),
                                              QtCore.QPointF(s * 0.52, s * 0.54),
                                              QtCore.QPointF(s * 0.44, s * 0.84),
                                              QtCore.QPointF(s * 0.68, s * 0.46),
                                              QtCore.QPointF(s * 0.48, s * 0.46),
                                              QtCore.QPointF(s * 0.56, s * 0.16)]))
    elif name == "review":
        painter.drawRoundedRect(QtCore.QRectF(s * 0.22, s * 0.2, s * 0.56, s * 0.6), 1.5, 1.5)
        for y in (0.38, 0.5, 0.62):
            painter.drawLine(QtCore.QPointF(s * 0.34, s * y), QtCore.QPointF(s * 0.66, s * y))
    painter.end()
    return QtGui.QIcon(pixmap)


# Text ------------------------------------------------------------------------

class AutoText(QtWidgets.QTextBrowser):
    """Selectable rich text that is exactly as tall as its content."""

    def __init__(self, parent=None, markdown=False):
        super().__init__(parent)
        self.markdown = markdown
        self.setReadOnly(True)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        self.viewport().setAutoFillBackground(False)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.document().setDocumentMargin(0)
        self.setContextMenuPolicy(Qt.DefaultContextMenu)

    def set_content(self, text):
        if self.markdown:
            features = (QtGui.QTextDocument.MarkdownDialectGitHub
                        | QtGui.QTextDocument.MarkdownNoHTML)
            self.document().setMarkdown(text, features)
        else:
            self.setPlainText(text)
        self._fit()

    def ideal_width(self):
        self.document().setDefaultFont(self.font())  # Measure with the font actually used.
        self.document().setTextWidth(-1)
        width = self.document().idealWidth()
        self._fit()
        return math.ceil(width) + 2

    def _fit(self):
        width = max(40, self.viewport().width())
        self.document().setTextWidth(width)
        self.setFixedHeight(math.ceil(self.document().size().height()) + 2)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def text(self):
        return self.document().toPlainText()


class UserBubble(QtWidgets.QWidget):
    def __init__(self, text, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        self.bubble = QtWidgets.QFrame()
        self.bubble.setObjectName("userBubble")
        inner = QtWidgets.QVBoxLayout(self.bubble)
        inner.setContentsMargins(12, 8, 12, 8)
        self.label = AutoText()
        self.label.set_content(text)
        inner.addWidget(self.label)
        row.addWidget(self.bubble)

    def fit(self, available):
        self.available = available
        width = min(int(available * 0.85), self.label.ideal_width() + 26)
        self.bubble.setFixedWidth(max(80, width))
        self.label._fit()

    def event(self, event):
        # Fonts and stylesheets arrive after construction; measure again then.
        if event.type() in (QtCore.QEvent.Polish, QtCore.QEvent.FontChange,
                            QtCore.QEvent.StyleChange) and getattr(self, "available", None):
            QtCore.QTimer.singleShot(0, lambda: self.fit(self.available))
        return super().event(event)

    def text(self):
        return self.label.text()


class AssistantMessage(AutoText):
    def __init__(self, text, parent=None):
        super().__init__(parent, markdown=True)
        self.set_content(text)


class Notice(QtWidgets.QFrame):
    """A short host message: info, warning or error, with a colored edge."""

    KINDS = ("info", "warning", "error")

    def __init__(self, text, kind="info", parent=None):
        super().__init__(parent)
        self.setObjectName("notice")
        self.kind = kind if kind in self.KINDS else "info"
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 6, 10, 6)
        self.edge = QtWidgets.QFrame()
        self.edge.setFixedWidth(3)
        row.addWidget(self.edge)
        self.label = AutoText()
        self.label.set_content(text)
        row.addWidget(self.label, 1)

    def apply_theme(self, theme):
        color = {"info": theme.muted, "warning": theme.warning, "error": theme.danger}[self.kind]
        self.edge.setStyleSheet("background: {}; border-radius: 1px;".format(color.name()))

    def text(self):
        return self.label.text()


# Step cards ------------------------------------------------------------------

STATUS = {
    # outcome -> (label, icon, theme color attribute)
    "proposed": ("Proposed", "dot", "muted"),
    "running": ("Running…", "dot", "accent"),
    "review": ("Awaiting review", "pause", "warning"),
    "executed": ("Done", "check", "success"),
    "failed": ("Failed", "cross", "danger"),
    "cancelled": ("Cancelled", "dash", "muted"),
    "rejected": ("Not run", "dash", "muted"),
    "undone": ("Undone", "undo", "muted"),
}


class StepCard(QtWidgets.QFrame):
    """One modeling step: a one-line header that expands to code and results."""

    run_requested = QtCore.Signal(str)
    undo_requested = QtCore.Signal(str)

    def __init__(self, step_id, parent=None):
        super().__init__(parent)
        self.setObjectName("stepCard")
        self.step_id = step_id
        self.status = "proposed"
        self.theme = None
        self._auto_opened = set()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 8, 6)
        layout.setSpacing(6)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(6)
        self.status_icon = QtWidgets.QLabel()
        self.status_icon.setFixedSize(16, 16)
        self.title = QtWidgets.QLabel()
        self.title.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setObjectName("muted")
        self.chevron = QtWidgets.QToolButton()
        self.chevron.setObjectName("flat")
        self.chevron.setAutoRaise(True)
        self.chevron.setFixedSize(20, 20)
        self.undo_button = QtWidgets.QToolButton()
        self.undo_button.setObjectName("flat")
        self.undo_button.setText("Undo")
        self.undo_button.setAutoRaise(True)
        self.undo_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.undo_button.setToolTip("Undo this step")
        header.addWidget(self.status_icon)
        header.addWidget(self.title, 1)
        header.addWidget(self.status_label)
        header.addWidget(self.undo_button)
        header.addWidget(self.chevron)
        layout.addLayout(header)
        self.body = QtWidgets.QWidget()
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        code_row = QtWidgets.QHBoxLayout()
        code_row.addWidget(self._caption("Code"), 1)
        self.copy_button = QtWidgets.QToolButton()
        self.copy_button.setObjectName("flat")
        self.copy_button.setText("Copy")
        self.copy_button.setAutoRaise(True)
        self.copy_button.clicked.connect(self.copy_code)
        code_row.addWidget(self.copy_button)
        body.addLayout(code_row)
        self.code = QtWidgets.QPlainTextEdit()
        self.code.setObjectName("codeBlock")
        self.code.setReadOnly(True)
        self.code.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.code.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)
        self.code.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body.addWidget(self.code)
        self.details = AutoText()
        body.addWidget(self.details)
        actions = QtWidgets.QHBoxLayout()
        actions.addStretch(1)
        self.run_button = QtWidgets.QPushButton("Run step")
        self.run_button.setObjectName("primary")
        actions.addWidget(self.run_button)
        body.addLayout(actions)
        layout.addWidget(self.body)
        self.body.setVisible(False)
        self.undo_button.setVisible(False)
        self.run_button.setVisible(False)
        self.chevron.clicked.connect(self.toggle)
        self.run_button.clicked.connect(lambda: self.run_requested.emit(self.step_id))
        self.undo_button.clicked.connect(lambda: self.undo_requested.emit(self.step_id))
        self.setCursor(Qt.PointingHandCursor)

    @staticmethod
    def _caption(text):
        label = QtWidgets.QLabel(text)
        label.setObjectName("muted")
        return label

    @property
    def expanded(self):
        return not self.body.isHidden()

    def mouseReleaseEvent(self, event):
        position = event.position() if hasattr(event, "position") else event.pos()
        if position.y() < 30:  # Clicks on the header row toggle the card.
            self.toggle()
        super().mouseReleaseEvent(event)

    def toggle(self):
        self.set_expanded(not self.expanded)

    def expand(self):
        self.set_expanded(True)

    def set_expanded(self, expanded):
        self.body.setVisible(expanded)
        self._paint_chevron()
        if expanded:
            QtCore.QTimer.singleShot(0, self._fit_code)

    def update_step(self, number, message, code, status, details, can_run=False, can_undo=False,
                    summary=""):
        """summary, when given, says what the step did ("Created Box") and is the title."""
        self.status = status if status in STATUS else "proposed"
        heading = summary or (message.splitlines()[0] if message else "")
        self.full_title = "Step {} · {}".format(number, heading)
        self.title.setToolTip(message)
        self._elide_title()
        self.status_label.setText(STATUS[self.status][0])
        if self.code.toPlainText() != code:
            self.code.setPlainText(code)
            self._fit_code()
        self.details.set_content(details)
        self.details.setVisible(bool(details))
        self.run_button.setVisible(can_run)
        self.undo_button.setVisible(can_undo)
        # Failures and steps waiting for review open themselves, once.
        if self.status in ("failed", "review") and self.status not in self._auto_opened:
            self._auto_opened.add(self.status)
            self.expand()
        self.apply_theme(self.theme)

    def apply_theme(self, theme):
        self.theme = theme
        if theme is None:
            return
        label, glyph, color_name = STATUS[self.status]
        color = getattr(theme, color_name)
        self.status_icon.setPixmap(icon(glyph, color, 16).pixmap(16, 16))
        self.status_label.setStyleSheet("color: {};".format(color.name()))
        self.undo_button.setIcon(icon("undo", theme.muted))
        self._paint_chevron()

    def _paint_chevron(self):
        if self.theme is not None:
            self.chevron.setIcon(icon("chevron-down" if self.expanded else "chevron-right",
                                      self.theme.muted))

    def _fit_code(self):
        """Show up to MAX_CODE_LINES wrapped lines; longer code scrolls."""
        # A plain-text document's height is its number of visual (wrapped) lines.
        lines = int(math.ceil(self.code.document().size().height()))
        visible = max(1, min(MAX_CODE_LINES, lines))
        metrics = QtGui.QFontMetrics(self.code.font())
        frame = self.code.frameWidth() * 2 + int(self.code.document().documentMargin() * 2)
        self.code.setFixedHeight(visible * metrics.lineSpacing() + frame + 10)

    def _elide_title(self):
        width = max(40, self.title.width())
        self.title.setText(self.title.fontMetrics().elidedText(
            getattr(self, "full_title", ""), Qt.ElideRight, width))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide_title()
        if self.expanded:
            QtCore.QTimer.singleShot(0, self._fit_code)

    def copy_code(self):
        QtWidgets.QApplication.clipboard().setText(self.code.toPlainText())
        self.copy_button.setText("Copied")
        QtCore.QTimer.singleShot(1500, lambda: self.copy_button.setText("Copy"))

    def code_text(self):
        return self.code.toPlainText()

    def details_text(self):
        return self.details.text()

    def text(self):
        return "{} — {}\n{}".format(getattr(self, "full_title", ""), STATUS[self.status][0],
                                     self.details_text())


# The conversation -------------------------------------------------------------

class ExampleChip(QtWidgets.QFrame):
    """A wrapped, clickable example prompt."""
    clicked = QtCore.Signal(str)

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName("exampleChip")
        self.setCursor(Qt.PointingHandCursor)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)  # Margins survive any app stylesheet.
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        self.label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.label)

    def text(self):
        return self.label.text()

    def mouseReleaseEvent(self, event):
        self.clicked.emit(self.text())
        super().mouseReleaseEvent(event)


class EmptyState(QtWidgets.QFrame):
    example_chosen = QtCore.Signal(str)

    def __init__(self, examples, note, parent=None):
        super().__init__(parent)
        self.setObjectName("emptyState")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)
        title = QtWidgets.QLabel("What should we model?")
        font = title.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        title.setFont(font)
        layout.addWidget(title)
        self.examples = []
        for example in examples:
            chip = ExampleChip(example)
            chip.clicked.connect(self.example_chosen)
            layout.addWidget(chip)
            self.examples.append(chip)
        self.note = QtWidgets.QLabel(note)
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        self.note.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.note)


class ChatView(QtWidgets.QScrollArea, Themed):
    """The scrolling conversation. Sticks to the bottom unless you scrolled up."""

    example_chosen = QtCore.Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatView")
        self.setWidgetResizable(True)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.canvas = QtWidgets.QWidget()
        self.canvas.setObjectName("chatCanvas")
        self.column = QtWidgets.QVBoxLayout(self.canvas)
        self.column.setContentsMargins(12, 12, 12, 12)
        self.column.setSpacing(12)
        self.status_line = QtWidgets.QLabel()
        self.status_line.setObjectName("statusLine")
        self.status_line.setVisible(False)
        self.column.addWidget(self.status_line)
        self.column.addStretch(1)
        self.setWidget(self.canvas)
        self.entries = []
        self.cards = {}
        self.empty = None
        self.theme = None
        self._stick = True
        self._status_text = ""
        self._dots = 0
        self._animation = QtCore.QTimer(self)
        self._animation.setInterval(400)
        self._animation.timeout.connect(self._animate)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._scrolled)
        bar.rangeChanged.connect(self._range_changed)
        self.apply_theme()

    def themed(self, theme):
        for entry in getattr(self, "entries", []):
            if hasattr(entry, "apply_theme"):
                entry.apply_theme(theme)

    # Scrolling
    def _scrolled(self, value):
        bar = self.verticalScrollBar()
        self._stick = value >= bar.maximum() - 8

    def _range_changed(self, minimum, maximum):
        if self._stick:
            self.verticalScrollBar().setValue(maximum)

    def at_bottom(self):
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 8

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_bubbles()

    def _fit_bubbles(self):
        available = self.viewport().width() - 24
        for entry in self.entries:
            if isinstance(entry, UserBubble):
                entry.fit(available)

    # Entries
    def _add(self, widget):
        self.hide_empty_state()
        self.column.insertWidget(self.column.indexOf(self.status_line), widget)
        self.entries.append(widget)
        if hasattr(widget, "apply_theme"):
            widget.apply_theme(self.theme)
        if isinstance(widget, UserBubble):
            widget.fit(self.viewport().width() - 24)
        while len(self.entries) > MAX_ENTRIES:
            old = self.entries.pop(0)
            if isinstance(old, StepCard):
                self.cards.pop(old.step_id, None)
            old.setParent(None)
            old.deleteLater()
        return widget

    def add_user(self, text):
        self._stick = True  # Sending always shows the latest message.
        return self._add(UserBubble(text))

    def add_assistant(self, text):
        return self._add(AssistantMessage(text))

    def add_notice(self, text, kind="info"):
        return self._add(Notice(text, kind))

    def add_step(self, step_id):
        card = StepCard(step_id)
        self.cards[step_id] = card
        return self._add(card)

    def card(self, step_id):
        return self.cards.get(step_id)

    def step_cards(self):
        return [entry for entry in self.entries if isinstance(entry, StepCard)]

    def clear(self):
        for entry in self.entries:
            entry.setParent(None)
            entry.deleteLater()
        self.entries = []
        self.cards = {}
        self.set_status(None)

    def show_empty_state(self, examples, note):
        self.hide_empty_state()
        self.empty = EmptyState(examples, note)
        self.empty.example_chosen.connect(self.example_chosen)
        self.column.insertWidget(0, self.empty)

    def hide_empty_state(self):
        if self.empty is not None:
            self.empty.setParent(None)
            self.empty.deleteLater()
            self.empty = None

    # Typing indicator
    def set_status(self, text):
        self._status_text = (text or "").rstrip("…").rstrip(".")
        self.status_line.setVisible(bool(text))
        if text:
            self._dots = 0
            self._render_status()
            self._animation.start()
        else:
            self._animation.stop()

    def status_text(self):
        return self._status_text if self.status_line.isVisible() else ""

    def _animate(self):
        self._dots = (self._dots + 1) % 4
        self._render_status()

    def _render_status(self):
        self.status_line.setText(self._status_text + "." * self._dots + " " * (3 - self._dots))

    def plain_text(self):
        parts = []
        for entry in self.entries:
            prefix = {UserBubble: "You: ", AssistantMessage: "Assistant: "}.get(type(entry), "")
            parts.append(prefix + entry.text())
        return "\n\n".join(parts)


class Composer(QtWidgets.QWidget, Themed):
    """Rounded, auto-growing input with a send/stop button and option chips."""

    send_clicked = QtCore.Signal()
    stop_clicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("composerArea")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 10)
        outer.setSpacing(4)
        self.frame = QtWidgets.QFrame()
        self.frame.setObjectName("composer")
        frame = QtWidgets.QVBoxLayout(self.frame)
        frame.setContentsMargins(10, 8, 8, 8)
        frame.setSpacing(6)
        self.input = QtWidgets.QPlainTextEdit()
        self.input.setObjectName("composerInput")
        self.input.setPlaceholderText("Ask or describe a change…")
        self.input.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.input.setTabChangesFocus(True)
        frame.addWidget(self.input)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.context_chip = QtWidgets.QToolButton()
        self.context_chip.setObjectName("chip")
        self.context_chip.setText("Context")
        self.context_chip.setCheckable(True)
        self.context_chip.setChecked(True)
        self.context_chip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.context_chip.setToolTip(
            "Send a bounded summary of the model and your selection with each request: names, "
            "types, dimensions with units, placements and sketch constraints. Never file paths, "
            "free text, raw shapes or screenshots.")
        self.mode_chip = QtWidgets.QToolButton()
        self.mode_chip.setObjectName("chip")
        self.mode_chip.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        self.mode_chip.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.mode_menu = QtWidgets.QMenu(self.mode_chip)
        self.auto_action = self.mode_menu.addAction("Run steps automatically")
        self.auto_action.setCheckable(True)
        self.auto_action.setChecked(True)
        self.auto_action.setToolTip("Steps run in a separate FreeCAD process; Stop works at any time.")
        explanation = self.mode_menu.addAction(
            "Off: review each step's code, then run it inside FreeCAD")
        explanation.setEnabled(False)
        self.mode_chip.setMenu(self.mode_menu)
        self.send_button = QtWidgets.QToolButton()
        self.send_button.setObjectName("sendButton")
        self.send_button.setFixedSize(28, 28)
        self.send_button.setIconSize(QtCore.QSize(16, 16))
        row.addWidget(self.context_chip)
        row.addWidget(self.mode_chip)
        row.addStretch(1)
        row.addWidget(self.send_button)
        frame.addLayout(row)
        outer.addWidget(self.frame)
        self.context_notice = QtWidgets.QLabel(
            "Model summary off for new requests. What was already sent stays in this chat; "
            "use New chat to start clean.")
        self.context_notice.setObjectName("muted")
        self.context_notice.setWordWrap(True)
        self.context_notice.setVisible(False)
        outer.addWidget(self.context_notice)
        self.running = False
        self.theme = None
        self.input.installEventFilter(self)
        self.input.document().contentsChanged.connect(self._grow)
        self.context_chip.toggled.connect(lambda shared: self.context_notice.setVisible(not shared))
        self.auto_action.toggled.connect(lambda checked: self._update_mode())
        self.send_button.clicked.connect(self._clicked)
        self.input.textChanged.connect(self._update_send)
        self.apply_theme()
        self._grow()

    def themed(self, theme):
        if hasattr(self, "send_button"):
            self.context_chip.setIcon(icon("eye", theme.text))
            self._update_mode()
            self._update_send()

    def eventFilter(self, watched, event):
        if watched is self.input and event.type() in (QtCore.QEvent.FocusIn, QtCore.QEvent.FocusOut):
            self.frame.setProperty("focused", event.type() == QtCore.QEvent.FocusIn)
            self.frame.style().unpolish(self.frame)
            self.frame.style().polish(self.frame)
        return super().eventFilter(watched, event)

    def _update_mode(self):
        automatic = self.auto_action.isChecked()
        self.mode_chip.setText(("Auto" if automatic else "Review") + " ▾")
        self.mode_chip.setIcon(icon("bolt" if automatic else "review", self.theme.text))
        self.mode_chip.setToolTip("Steps run automatically in a separate FreeCAD process."
                                  if automatic else "Each step waits for you to review its code.")

    def _grow(self):
        total = int(self.input.document().size().height())
        lines = max(1, min(MAX_INPUT_LINES, total))
        # A scrollbar only once the input stops growing.
        self.input.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded if total > MAX_INPUT_LINES
                                              else Qt.ScrollBarAlwaysOff)
        metrics = QtGui.QFontMetrics(self.input.font())
        margins = self.input.contentsMargins()
        height = lines * metrics.lineSpacing() + int(self.input.document().documentMargin() * 2) \
            + margins.top() + margins.bottom() + 2
        self.input.setFixedHeight(height)

    def set_running(self, running):
        self.running = running
        self._update_send()

    def _update_send(self):
        if self.theme is None:
            return
        glyph = "stop" if self.running else "send"
        self.send_button.setIcon(icon(glyph, self.theme.accent_text))
        self.send_button.setToolTip("Stop" if self.running else "Send (Enter)")
        self.send_button.setEnabled(self.running or bool(self.input.toPlainText().strip()))

    def _clicked(self):
        (self.stop_clicked if self.running else self.send_clicked).emit()

    def set_text(self, text):
        self.input.setPlainText(text)
        self.input.moveCursor(QtGui.QTextCursor.End)
        self.input.setFocus()

    def set_options_enabled(self, enabled):
        self.context_chip.setEnabled(enabled)
        self.mode_chip.setEnabled(enabled)


class ActionBar(QtWidgets.QWidget, Themed):
    """A prompt for the decision the task is waiting on: review or continue."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("barHolder")
        self.setAttribute(Qt.WA_StyledBackground, True)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(10, 6, 10, 0)
        self.frame = QtWidgets.QFrame()
        self.frame.setObjectName("actionBar")
        outer.addWidget(self.frame)
        row = QtWidgets.QHBoxLayout(self.frame)
        row.setContentsMargins(10, 6, 8, 6)
        self.message = QtWidgets.QLabel()
        self.message.setWordWrap(True)
        row.addWidget(self.message, 1)
        self.stop_button = QtWidgets.QPushButton("Stop")
        self.stop_button.setObjectName("secondary")
        self.continue_button = QtWidgets.QPushButton("Continue")
        self.continue_button.setObjectName("primary")
        self.run_button = QtWidgets.QPushButton("Run step")
        self.run_button.setObjectName("primary")
        for button in (self.stop_button, self.continue_button, self.run_button):
            row.addWidget(button)
        self.setVisible(False)
        self.apply_theme()

    def show_review(self):
        self.message.setText("Review the proposed step's code, then run it.")
        self.run_button.setVisible(True)
        self.run_button.setEnabled(True)
        self.continue_button.setVisible(False)
        self.setVisible(True)

    def show_paused(self, message):
        self.message.setText(message)
        self.continue_button.setVisible(True)
        self.run_button.setVisible(False)
        self.run_button.setEnabled(False)
        self.setVisible(True)

    def hide_actions(self):
        self.run_button.setEnabled(False)
        self.continue_button.setVisible(False)
        self.setVisible(False)
