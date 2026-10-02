# App wide look: colour tokens, the stylesheet built from them, the bundled font and
# the light/dark/system switch. Fusion everywhere, so every OS draws the same widgets.

import base64
import logging
import os
import tempfile

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPainter, QPalette, \
    QPen, QPixmap

log = logging.getLogger('osprad.theme')

FONT_FAMILY = 'Inter'
BASE_PX = 14

LIGHT = {
    'bg': '#ffffff', 'card': '#f5f5f8', 'field': '#ffffff', 'line': '#e3e3e8',
    'text': '#111111', 'muted': '#8e8e93', 'primary': '#111111', 'on_primary': '#ffffff',
    'secondary': '#e9e9ee', 'hover': '#dedee4', 'nav': '#fafafa', 'edge': '#c4c4cc',
    'good': '#1e8e3e', 'bad': '#d93025', 'warn': '#c77700',
}
DARK = {
    'bg': '#0f0f10', 'card': '#1c1c1e', 'field': '#2c2c2e', 'line': '#3a3a3c',
    'text': '#f2f2f7', 'muted': '#8e8e93', 'primary': '#f2f2f7', 'on_primary': '#111111',
    'secondary': '#2c2c2e', 'hover': '#3a3a3c', 'nav': '#161618', 'edge': '#5a5a5e',
    'good': '#30d158', 'bad': '#ff453a', 'warn': '#ffd60a',
}

MODES = ('system', 'light', 'dark')


def load_font(app):
    """Register the bundled Inter and make it the app font; system font if that fails."""
    try:
        from _font_bundled import FONTS
    except ImportError:
        FONTS = {}
    loaded = [QFontDatabase.addApplicationFontFromData(base64.b64decode(data)) >= 0
              for data in FONTS.values()]
    if not loaded or not all(loaded):
        log.warning('Bundled font not loaded; using the system font.')
        return
    font = QFont(FONT_FAMILY)
    font.setPixelSize(BASE_PX)
    app.setFont(font)
    try:  # the plots too, so their labels match the rest of the app
        from matplotlib import font_manager, rcParams
        for name, data in FONTS.items():
            path = os.path.join(_pixmap_dir(), name + '.ttf')
            if not os.path.exists(path):
                with open(path, 'wb') as f:
                    f.write(base64.b64decode(data))
            font_manager.fontManager.addfont(path)
        rcParams['font.family'] = FONT_FAMILY
        # Inter ships Regular and SemiBold only; matplotlib's nearest weight is fine.
        logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)
    except Exception as exc:  # noqa: BLE001
        log.warning('Plots keep their default font: %s', exc)


def is_dark(mode):
    if mode == 'dark':
        return True
    if mode == 'light':
        return False
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def _pixmap_dir():
    path = os.path.join(tempfile.gettempdir(), 'osprad-theme')
    os.makedirs(path, exist_ok=True)
    return path


def _icon_file(name, color, draw):
    """Paint a small indicator image once; QSS can only take images by file path."""
    path = os.path.join(_pixmap_dir(), '%s-%s.png' % (name, color.lstrip('#')))
    if not os.path.exists(path):
        pixmap = QPixmap(48, 48)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        draw(painter)
        painter.end()
        pixmap.save(path)
    return path.replace('\\', '/')


def _check(p):
    p.drawPolyline([QPointF(12, 25), QPointF(20, 33), QPointF(36, 15)])


def _dot(p):
    p.setBrush(p.pen().color())
    p.drawEllipse(QPointF(24, 24), 8, 8)


def _chevron_down(p):
    p.drawPolyline([QPointF(14, 19), QPointF(24, 29), QPointF(34, 19)])


def _chevron_right(p):
    p.drawPolyline([QPointF(19, 14), QPointF(29, 24), QPointF(19, 34)])


def stylesheet(t):
    check = _icon_file('check', t['on_primary'], _check)
    dot = _icon_file('dot', t['on_primary'], _dot)
    down = _icon_file('down', t['text'], _chevron_down)
    right = _icon_file('right', t['text'], _chevron_right)
    return """
QWidget { color: %(text)s; }
QDialog { background: %(card)s; border: 1px solid %(edge)s; }
QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }
QLabel[role="muted"] { color: %(muted)s; }
QLabel[role="good"] { color: %(good)s; }
QLabel[role="bad"] { color: %(bad)s; }
QLabel[role="title"] { font-size: 20px; font-weight: 600; }
QLabel[role="value"] { font-size: 34px; font-weight: 600; }
QLabel[role="unit"] { font-size: 16px; color: %(muted)s; }

QPushButton { background: %(secondary)s; color: %(text)s; border: none; border-radius: 20px;
    padding: 0 18px; min-height: 40px; font-weight: 600; }
QPushButton:hover { background: %(hover)s; }
QPushButton:disabled { color: %(muted)s; }
QPushButton[role="primary"] { background: %(primary)s; color: %(on_primary)s; }
QPushButton[role="primary"]:disabled { background: %(hover)s; color: %(muted)s; }

QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: %(field)s; border: 1px solid %(line)s;
    border-radius: 10px; padding: 0 10px; min-height: 38px; }
QLineEdit:focus, QComboBox:focus { border-color: %(text)s; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox::down-arrow { image: url("%(down)s"); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background: %(field)s; border: 1px solid %(line)s;
    selection-background-color: %(secondary)s; selection-color: %(text)s; padding: 4px; }
QPlainTextEdit, QTextEdit { background: %(field)s; border: 1px solid %(line)s; border-radius: 10px; }

QCheckBox, QRadioButton { spacing: 10px; min-height: 32px; }
QCheckBox::indicator, QRadioButton::indicator { width: 20px; height: 20px;
    border: 2px solid %(line)s; background: %(field)s; }
QCheckBox::indicator { border-radius: 6px; }
QRadioButton::indicator { border-radius: 12px; }
QCheckBox::indicator:checked { background: %(primary)s; border-color: %(primary)s;
    image: url("%(check)s"); }
QRadioButton::indicator:checked { background: %(primary)s; border-color: %(primary)s;
    image: url("%(dot)s"); }

QGroupBox { background: %(card)s; border: none; border-radius: 14px; margin-top: 0;
    padding: 40px 12px 10px 12px; font-weight: 600; }
QGroupBox:unchecked { padding: 44px 12px 0 12px; }
QGroupBox::title { subcontrol-origin: padding; subcontrol-position: top left; left: 14px;
    top: 13px; }
QGroupBox::indicator { width: 18px; height: 18px; border: none; background: transparent; }
QGroupBox::indicator:checked { image: url("%(down)s"); }
QGroupBox::indicator:unchecked { image: url("%(right)s"); }
QFrame#card { background: %(card)s; border-radius: 14px; }

QTreeWidget, QListWidget { background: %(field)s; border: 1px solid %(line)s; border-radius: 12px;
    outline: none; }
QTreeWidget::item, QListWidget::item { padding: 8px 4px; }
QTreeWidget::item:selected, QListWidget::item:selected { background: %(secondary)s; color: %(text)s; }
QHeaderView::section { background: %(field)s; color: %(muted)s; border: none;
    border-bottom: 1px solid %(line)s; padding: 6px 4px; font-weight: 600; }

QScrollBar:vertical { width: 8px; background: transparent; margin: 2px; }
QScrollBar:horizontal { height: 8px; background: transparent; margin: 2px; }
QScrollBar::handle { background: %(line)s; border-radius: 4px; min-height: 30px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {
    width: 0; height: 0; background: none; }

QSlider::groove:horizontal { height: 6px; background: %(secondary)s; border-radius: 3px; }
QSlider::sub-page:horizontal { background: %(primary)s; border-radius: 3px; }
QSlider::handle:horizontal { width: 24px; height: 24px; margin: -9px 0; border-radius: 12px;
    background: %(field)s; border: 2px solid %(primary)s; }

QMenu { background: %(field)s; border: 1px solid %(line)s; border-radius: 12px; padding: 6px; }
QMenu::item { padding: 10px 18px; border-radius: 8px; }
QMenu::item:selected { background: %(secondary)s; }
QMenu::item:disabled { color: %(muted)s; }
QMenu::separator { height: 1px; background: %(line)s; margin: 4px 8px; }
QToolTip { background: %(text)s; color: %(bg)s; border: none; border-radius: 8px; padding: 6px 8px; }
QProgressBar { background: %(secondary)s; border: none; border-radius: 6px; height: 12px;
    text-align: center; }
QProgressBar::chunk { background: %(primary)s; border-radius: 6px; }

QToolButton#help { background: %(secondary)s; color: %(muted)s; border: none; border-radius: 13px;
    font-weight: 700; font-style: italic; }
QToolButton#chip { background: %(secondary)s; border: none; border-radius: 14px;
    padding: 0 12px; min-height: 28px; max-height: 28px; font-weight: 600; }
QWidget#nav { background: %(nav)s; }
QToolButton#navButton { background: transparent; border: none; color: %(muted)s;
    padding: 6px 2px; font-size: 12px; }
QToolButton#navButton:checked { color: %(text)s; font-weight: 600; }
QToolButton#flat { background: transparent; border: none; border-radius: 18px; padding: 4px; }
QToolButton#flat:hover { background: %(secondary)s; }
""" % dict(t, check=check, dot=dot, down=down, right=right)


def apply(app, mode):
    """Apply light, dark or the system's choice. Returns whether dark was chosen."""
    dark = is_dark(mode)
    t = DARK if dark else LIGHT
    app.setStyle('Fusion')
    palette = QPalette()
    for role, key in ((QPalette.ColorRole.Window, 'bg'), (QPalette.ColorRole.Base, 'bg'),
                      (QPalette.ColorRole.AlternateBase, 'card'),
                      (QPalette.ColorRole.WindowText, 'text'), (QPalette.ColorRole.Text, 'text'),
                      (QPalette.ColorRole.ButtonText, 'text'), (QPalette.ColorRole.Button, 'secondary'),
                      (QPalette.ColorRole.Highlight, 'secondary'),
                      (QPalette.ColorRole.HighlightedText, 'text'),
                      (QPalette.ColorRole.PlaceholderText, 'muted'),
                      (QPalette.ColorRole.ToolTipBase, 'text'), (QPalette.ColorRole.ToolTipText, 'bg')):
        palette.setColor(role, QColor(t[key]))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(t['muted']))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(t['muted']))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(t['muted']))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(t))
    return dark


def tokens(dark):
    return DARK if dark else LIGHT
