# Touch stand-ins for two things a touchscreen has no way to express: hovering (which is
# how Qt tooltips are normally triggered) and a right button (which is how context menus
# are). A long press covers both.
#
# Everything here is gated on the press having been synthesised from a touch event, so a
# desktop mouse keeps its ordinary hover-tooltip and right-click behaviour untouched and
# a touchscreen laptop gets the affordances for free.

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtWidgets import QToolTip

# Long enough not to fire on a tap, short enough not to feel broken. Matches the usual
# Android long-press feel (~500ms).
LONG_PRESS_MS = 500

# A press that wanders further than this is the start of a scroll, not a long press.
MOVE_TOLERANCE_PX = 12


def is_touch(event):
    """True if Qt synthesised this mouse event from a touch, rather than a real mouse."""
    return event.source() != Qt.MouseEventSource.MouseEventNotSynthesized


class _LongPress(QObject):
    """Calls back once a press has been held still for LONG_PRESS_MS.

    Never consumes the event: whatever the widget normally does with a press, tap or
    drag still happens. The callback is additive.
    """

    def __init__(self, target, callback):
        super().__init__(target)
        self._callback = callback
        self._origin = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LONG_PRESS_MS)
        self._timer.timeout.connect(self._fire)
        target.installEventFilter(self)

    def _fire(self):
        if self._origin is not None:
            origin, self._origin = self._origin, None
            self._callback(origin)

    def _cancel(self):
        self._timer.stop()
        self._origin = None

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            if is_touch(event):
                self._origin = event.position().toPoint()
                self._timer.start()
        elif kind == QEvent.Type.MouseMove and self._origin is not None:
            moved = (event.position().toPoint() - self._origin).manhattanLength()
            if moved > MOVE_TOLERANCE_PX:
                self._cancel()
        elif kind in (QEvent.Type.MouseButtonRelease, QEvent.Type.Leave):
            self._cancel()
        return False


def install_long_press(widget, callback):
    """Call callback(pos) when `widget` is long-pressed. pos is widget-local, matching
    what QWidget.customContextMenuRequested would have delivered."""
    return _LongPress(widget, callback)


class _TouchTooltips(QObject):
    """Application-wide: long-press any widget carrying a tooltip to read it.

    Deliberately only consults the pressed widget's own tooltip rather than walking up
    its parents - every tip() in this app is set on the leaf widget, and a walk would
    surface an ancestor's tooltip for unrelated children.
    """

    def __init__(self, app):
        super().__init__(app)
        self._widget = None
        self._global_pos = None
        self._origin = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(LONG_PRESS_MS)
        self._timer.timeout.connect(self._show)
        app.installEventFilter(self)

    def _show(self):
        widget, pos = self._widget, self._global_pos
        self._reset()
        if widget is not None and widget.toolTip():
            QToolTip.showText(pos, widget.toolTip(), widget)

    def _reset(self):
        self._timer.stop()
        self._widget = None
        self._global_pos = None
        self._origin = None

    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress:
            self._reset()
            if is_touch(event) and hasattr(obj, 'toolTip') and obj.toolTip():
                self._widget = obj
                self._origin = event.position().toPoint()
                self._global_pos = event.globalPosition().toPoint()
                self._timer.start()
        elif kind == QEvent.Type.MouseMove and self._origin is not None:
            moved = (event.position().toPoint() - self._origin).manhattanLength()
            if moved > MOVE_TOLERANCE_PX:
                self._reset()
        elif kind == QEvent.Type.MouseButtonRelease:
            self._reset()
        return False


def enable_touch_tooltips(app):
    """Install the app-wide long-press-to-see-a-tooltip filter. Keep the return value
    alive for as long as the app is."""
    return _TouchTooltips(app)
