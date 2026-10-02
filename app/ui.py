# Shared widget helpers, kept out of the tab modules so the main window and the
# calibration/monitor tabs can all use them without importing each other.

import html

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QLayout, QScrollArea,
                               QScroller, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget, QWidgetItem)

# Matches the old Tkinter Tooltip wrap width.
TOOLTIP_WIDTH_PX = 280

HELP_BUTTON_PX = 26

# Comfortable reading width for a help paragraph; capped to the screen at runtime.
HELP_DIALOG_WIDTH_PX = 420


class FlowLayout(QLayout):
    """A row that wraps onto further lines when the window is too narrow for it.
    Takes the same addWidget/addStretch/addSpacing calls as QHBoxLayout (the last two
    do nothing), so a row converts by swapping its class."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item):
        self._items.append(item)

    def addWidget(self, widget, *_):
        self.addChildWidget(widget)
        self.addItem(QWidgetItem(widget))

    def addStretch(self, *_):
        pass

    def addSpacing(self, *_):
        pass

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):
        visible = [i.sizeHint() for i in self._items if not i.isEmpty()]
        width = sum(s.width() for s in visible) + self.spacing() * max(0, len(visible) - 1)
        return QSize(width, max((s.height() for s in visible), default=0))

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    @staticmethod
    def _glued(item):
        """Captions stay on the line of the control that follows them."""
        widget = item.widget()
        return isinstance(widget, QLabel) or (widget is not None and (
            widget.objectName() == 'help' or widget.property('caption')))

    def _arrange(self, rect, apply):
        chunks, chunk = [], []
        for item in self._items:
            if item.isEmpty():
                continue
            chunk.append((item, item.sizeHint()))
            if not self._glued(item):
                chunks.append(chunk)
                chunk = []
        if chunk:
            chunks.append(chunk)
        lines, line, x = [], [], 0
        for chunk in chunks:
            width = sum(h.width() for _, h in chunk) + self.spacing() * (len(chunk) - 1)
            if line and x + width > rect.width():
                lines.append(line)
                line, x = [], 0
            line.extend(chunk)
            x += width + self.spacing()
        if line:
            lines.append(line)
        y = rect.y()
        for line in lines:
            height = max(h.height() for _, h in line)
            x = rect.x()
            for item, hint in line:
                if apply:
                    item.setGeometry(QRect(QPoint(x, y + (height - hint.height()) // 2), hint))
                x += hint.width() + self.spacing()
            y += height + self.spacing()
        return max(0, y - rect.y() - self.spacing())


def wrapped_label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def set_role(label, role):
    """Tag a label with a semantic colour role ('muted'/'good'/'bad') from the QSS."""
    label.setProperty('role', role)
    label.style().unpolish(label)
    label.style().polish(label)


def tip(widget, text):
    """Set a tooltip, wrapped to roughly the old Tkinter Tooltip width.

    The text is escaped: this builds an HTML fragment, so an unescaped '<' or '&'
    in a help string would silently swallow the rest of the tooltip.
    """
    widget.setToolTip('<div style="max-width:%dpx">%s</div>'
                      % (TOOLTIP_WIDTH_PX, html.escape(text)))


def show_help(parent, text, title='OSpRad'):
    """Show a help string in a dialog that is guaranteed to show all of it.

    Not a QMessageBox: how well that works depends on the platform, font and DPI.
    On some desktops these paragraphs came out clipped. A scroll area removes the
    guesswork, and also covers a phone screen too short to show a long entry.
    """
    dialog = QDialog(parent.window() if parent is not None else None)
    dialog.setWindowTitle(title)
    layout = QVBoxLayout(dialog)

    label = QLabel(text)
    label.setWordWrap(True)
    # Prose, not markup. Don't let a stray '<' swallow the rest of the text.
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(label)
    # Drag to scroll, since these are read on touchscreens too.
    QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)
    layout.addWidget(scroll, 1)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    layout.addWidget(buttons)

    # Wide enough for comfortable line lengths, tall enough for the text but never
    # taller than the screen.
    width = HELP_DIALOG_WIDTH_PX
    needed = label.heightForWidth(width - 40) if label.hasHeightForWidth() else \
        label.sizeHint().height()
    chrome = 90  # button row, margins, title bar
    height = needed + chrome
    screen = dialog.screen() or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        width = min(width, available.width() - 40)
        height = min(height, int(available.height() * 0.8))
    dialog.resize(width, max(height, 120))
    dialog.exec()


def help_button(text, title='OSpRad'):
    """A small '?' beside a caption, opening `text` in a dialog when tapped.

    Tooltips are unreachable in two of the three places this app ships: a touchscreen
    has no hover, and touch.py's long press stand in usually loses the press to
    QScroller's pan gesture, especially on QLabels, which carry most of the tooltips.
    A real button works identically on desktop, in a packaged build and on Android.
    The tooltip is kept as well, so desktop hover still shows the same text.
    """
    button = QToolButton()
    button.setObjectName('help')
    button.setText('i')
    button.setFixedSize(HELP_BUTTON_PX, HELP_BUTTON_PX)
    # What a screen reader announces.
    button.setAccessibleName('Help')
    tip(button, text)
    button.clicked.connect(lambda: show_help(button, text, title))
    return button


def captioned(caption_widget, help_text, title='OSpRad'):
    """[caption][i] as one widget, so it drops into a single existing layout slot.
    Tapping the caption itself opens the help too: it is the bigger target."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    button = help_button(help_text, title)
    caption_widget.mouseReleaseEvent = lambda event: button.click()
    caption_widget.setCursor(Qt.CursorShape.PointingHandCursor)
    row.addWidget(caption_widget, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
    row.addStretch(1)
    holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
    holder.setProperty('caption', True)
    return holder


def collapsible_group(title, start_open=False):
    """A QGroupBox whose contents fold away (Qt's checkable QGroupBox only greys out).

    Returns (group, content_layout). Reclaims vertical space that checkable
    QGroupBox still costs, which matters on a phone screen.
    """
    group = QGroupBox(title)
    group.setCheckable(True)
    group.setChecked(start_open)
    outer = QVBoxLayout(group)
    outer.setContentsMargins(0, 0, 0, 0)
    body = QWidget()
    body.setVisible(start_open)
    outer.addWidget(body)
    group.toggled.connect(body.setVisible)
    return group, QVBoxLayout(body)


PLACEHOLDER_NAMES = {'wavCoef': 'wavelengths', 'radSens': 'radiance sensitivity',
                     'irrSens': 'irradiance sensitivity', 'linCoefs': 'linearisation'}


class UnitBanner(QWidget):
    """'Unit #3, firmware v3.2.1, calibrated', shown at the top of every calibration tab.

    Which unit an action applies to used to be visible only on Unit & wheel setup
    (and, after a run, on Linearisation). The cosine and monitor tabs never showed
    it at all, so nothing on screen tied a calibration action to the unit it would
    overwrite.
    """

    HELP = ('Everything on this page applies to the unit number reported by the connected '
            'OSpRad, which is stored on its Arduino.\n\n'
            'Wavelength, sensitivity and linearisation curves are saved per unit number '
            'in calibration_data.csv. The shutter wheel positions and the unit number '
            'itself live on the Arduino.\n\n'
            '"Not yet measured" curves are placeholders from another unit: readings work, '
            'but their absolute values are approximate. To change which unit number this '
            'OSpRad reports, use Calibrate \N{RIGHTWARDS ARROW} Unit & wheel.')

    def __init__(self):
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.label = wrapped_label('')
        row.addWidget(self.label, 1)
        row.addWidget(help_button(self.HELP))
        self.set_disconnected()

    def set_config(self, config, store=None):
        """Show the connected unit. `store` adds whether it has calibration data."""
        if config is None:
            self.set_disconnected()
            return
        state = ''
        role = 'muted'
        if store is not None:
            try:
                calib = store.get(config.unit_number)
            except Exception:
                state = ', no calibration yet (start with Wavelength)'
                role = 'bad'
            else:
                # Curves borrowed from another unit are not a measurement of this one.
                borrowed = [PLACEHOLDER_NAMES[r] for r in PLACEHOLDER_NAMES
                            if r in calib.placeholders]
                if borrowed:
                    state = ', not yet measured: %s' % ', '.join(borrowed)
                else:
                    state = ', calibrated'
                    role = 'good'
                if calib.serial:
                    state = ', sensor %s%s' % (calib.serial, state)
        if not config.configured:
            state += ', wheel positions not saved'
            role = 'bad'
        self.label.setText('Unit #%d, firmware v%s%s'
                           % (config.unit_number, config.firmware, state))
        set_role(self.label, role)

    def set_disconnected(self, message='Not connected, no unit selected.'):
        self.label.setText(message)
        set_role(self.label, 'bad')
