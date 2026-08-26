# Shared widget helpers, kept out of the tab modules so the main window and the
# calibration/monitor tabs can all use them without importing each other.

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QScrollArea,
                               QScroller, QSizePolicy, QToolButton, QVBoxLayout,
                               QWidget)

# Matches the old Tkinter Tooltip wrap width.
TOOLTIP_WIDTH_PX = 280

# Small enough to read as a superscript marker on the caption it belongs to, while
# staying a usable tap target.
HELP_BUTTON_PX = 16

# Comfortable reading width for a help paragraph; capped to the screen at runtime.
HELP_DIALOG_WIDTH_PX = 420


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
    button.setText('?')
    button.setAutoRaise(True)
    button.setFixedSize(HELP_BUTTON_PX, HELP_BUTTON_PX)
    # Smaller than the caption it annotates, so it reads as a marker rather than a
    # control competing with it.
    font = button.font()
    font.setPointSizeF(max(6.5, font.pointSizeF() - 1.5))
    font.setBold(True)
    button.setFont(font)
    # How the tests find these, and what a screen reader announces.
    button.setAccessibleName('Help')
    tip(button, text)
    button.clicked.connect(lambda: show_help(button, text, title))
    return button


def captioned(caption_widget, help_text, title='OSpRad'):
    """[caption][?] as one widget, so it drops into a single existing layout slot.

    The '?' sits tight against the caption's top right, like a footnote marker,
    rather than floating as a separate control.
    """
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    row.addWidget(caption_widget, 0, Qt.AlignmentFlag.AlignBottom)
    row.addWidget(help_button(help_text, title), 0, Qt.AlignmentFlag.AlignTop)
    row.addStretch(1)
    holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
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


class UnitBanner(QWidget):
    """'Unit #3, firmware v3.2.1, calibrated', shown at the top of every calibration tab.

    Which unit an action applies to used to be visible only on Unit & wheel setup
    (and, after a run, on Linearisation). The cosine and monitor tabs never showed
    it at all, so nothing on screen tied a calibration action to the unit it would
    overwrite.
    """

    HELP = ('Everything on this tab applies to the unit number reported by the connected '
            'OSpRad, which is stored on its Arduino.\n\n'
            'Wavelength, sensitivity and linearisation curves are saved per unit number '
            'in calibration_data.csv. The shutter wheel positions and the unit number '
            'itself live on the Arduino.\n\n'
            'To change which unit number this OSpRad reports, use the Import & export tab.')

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
                state = ', no calibration data yet'
                role = 'bad'
            else:
                # A freshly installed app ships calibration for a handful of units;
                # saying "calibrated" there would claim this hardware had been measured.
                if getattr(calib, 'is_default', False):
                    state = ', using default calibration'
                    role = 'muted'
                else:
                    state = ', calibrated'
                    role = 'good'
        if not config.configured:
            state += ', wheel positions not saved'
            role = 'bad'
        self.label.setText('Unit #%d, firmware v%s%s'
                           % (config.unit_number, config.firmware, state))
        set_role(self.label, role)

    def set_disconnected(self, message='Not connected, no unit selected.'):
        self.label.setText(message)
        set_role(self.label, 'bad')
