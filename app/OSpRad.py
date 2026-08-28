# Run this file to launch the app. Requires OSpRad 1.x firmware on the Arduino Nano.

import argparse
import html
import logging
import os
import shutil
import sys
import time

from PySide6.QtCore import QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QScroller, QScrollerProperties,
                               QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from matplotlib.backends.backend_qtagg import NavigationToolbar2QT

import analysis
import calibration
import datalog
import file_io
import plotting
import serial_io
import touch
from _version import __version__
from calibration_wizard import (WHEEL_ROLE_HELP, CalibrationTransferTab,
                                CosineResponseTab, LinearisationTab,
                                SensitivityTab, UnitSetupTab)
from ui import captioned, collapsible_group, help_button, tip, wrapped_label
from ui import set_role as _set_role
from monitor_calibration import MonitorCalibrationTab
from qt_worker import Worker, wait_for


def _user_data_dir():
    """Per user, always writable fallback (pip install: site packages is often not
    writable; AppImage: sys.executable sits in a read only, ephemeral FUSE mount)."""
    if sys.platform == 'win32':
        root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    elif sys.platform == 'darwin':
        root = os.path.expanduser('~/Library/Application Support')
    else:
        root = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    path = os.path.join(root, 'OSpRad')
    os.makedirs(path, exist_ok=True)
    return path


# Where calibration_data.csv and data.csv live. Resolved against this file so
# the app behaves the same however it is launched; see pyproject.toml for the
# py modules layout that motivates the per user fallback below.
if getattr(sys, 'frozen', False):
    # __file__ points inside the PyInstaller bundle, which onefile deletes on exit.
    _exe_dir = os.path.dirname(sys.executable)
    BASE_DIR = _exe_dir if os.access(_exe_dir, os.W_OK) else _user_data_dir()
else:
    _source_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(_source_dir, 'calibration_data.csv')):
        BASE_DIR = _source_dir
    else:
        BASE_DIR = _user_data_dir()
DATA_FILE = os.path.join(BASE_DIR, 'data.csv')
CALIBRATION_FILE = os.path.join(BASE_DIR, 'calibration_data.csv')

# First run: seed a writable calibration_data.csv from the read only bundled copy.
if not os.path.exists(CALIBRATION_FILE):
    if getattr(sys, 'frozen', False):
        _bundled_calibration = os.path.join(getattr(sys, '_MEIPASS', BASE_DIR), 'calibration_data.csv')
        if os.path.exists(_bundled_calibration):
            shutil.copy(_bundled_calibration, CALIBRATION_FILE)
    else:
        try:
            from _calibration_data_bundled import CSV_TEXT as _bundled_csv_text
        except ImportError:
            _bundled_csv_text = None
        if _bundled_csv_text is not None:
            with open(CALIBRATION_FILE, 'w', newline='') as f:
                f.write(_bundled_csv_text)

# Cap on the scrolling log widget so an unattended "Repeat every (s)" session
# does not grow unboundedly; QPlainTextEdit trims from the top past this.
# First entry of the port combo; any other entry is a literal port name to connect to.
PORT_AUTO = 'Auto detect'

# How long a close waits for an in flight measurement before forcing through.
CLOSE_GRACE_SECONDS = 15

# Idle port presence check. 5s not 1s: on Windows comports() is a SetupAPI
# walk of tens of milliseconds.
HEARTBEAT_MS = 5000

# How long a measurement runs before the status explains itself.
MEASURE_SLOW_HINT_SECONDS = 20

# Continuous mode: pause between one update and the next. Not a throttle (a
# measurement is orders of magnitude slower); it just hands the event loop a
# beat to repaint the canvas and notice a Stop press between frames.
CONTINUOUS_GAP_MS = 50

# Per update time past which continuous mode explains how to speed itself up.
CONTINUOUS_SLOW_SECONDS = 5

# Continuous mode drives the exposure itself, because the firmware's dark
# cache is keyed on it. Auto exposure and a live plot are mutually exclusive.
#
# Target peak, and the band around it that is left alone. The band is wide on
# purpose: re exposing costs a full measurement, so it has to be worth it.
CONTINUOUS_TARGET_PEAK = 500
CONTINUOUS_PEAK_LOW = 120
CONTINUOUS_PEAK_HIGH = 850
# Bounds on the exposure it may choose, from the firmware's own auto exposure ceiling.
CONTINUOUS_EXPOSURE_MIN = 1
CONTINUOUS_EXPOSURE_MAX = 5000
# Cap on how far one step may move, so one odd reading cannot swing the
# exposure to a value that takes several seconds to recover from.
CONTINUOUS_EXPOSURE_STEP = 8

# How often a continuous run gives up the firmware's cached dark reference
# and takes a full measurement instead. Dark current follows sensor temperature,
# so expiring it is the app's job.
CONTINUOUS_DARK_REFRESH_SECONDS = 30

LOG_MAX_LINES = 500
LOG_LEVELS = ('debug', 'info', 'warning', 'error')
LOG_LEVEL_RANK = {level: i for i, level in enumerate(LOG_LEVELS)}
LOG_COLORS = {'error': '#d1495b', 'warning': '#e9a23a', 'debug': '#7a7a7a'}

# Root of the logger tree the non GUI modules log into (serial_io, datalog, ...).
# They use stdlib logging so they do not have to import the GUI or be handed a callback.
LOGGER_NAME = 'osprad'


class _LogBridge(QObject):
    """Carries log records from any thread onto the GUI thread.

    Signal.emit is thread safe and Qt queues cross thread connections, so records
    logged from the worker arrive on the GUI thread before they touch the log
    widget. A handler calling _log() directly would corrupt QPlainTextEdit.
    """
    message = Signal(str, str)


class _QtLogHandler(logging.Handler):
    """Feeds stdlib log records into the Debug tab through _LogBridge."""

    def __init__(self, bridge):
        super().__init__()
        self._bridge = bridge

    def emit(self, record):
        level = record.levelname.lower()
        # LOG_LEVELS has no 'critical'; fold it into the most severe level it has.
        self._bridge.message.emit(record.getMessage(),
                                  'error' if level == 'critical' else level)

# Hand rolled replacement for sv_ttk (Tkinter only); "role" colours match the old styles.
# (Qt stylesheet syntax: the .in py form below is a single big string.)
LIGHT_QSS = """
QWidget { background-color: #fafafa; color: #1c1c1c; }
QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox { background-color: #ffffff; }
QLabel[role="muted"] { color: #7a7a7a; }
QLabel[role="good"] { color: #2a9d8f; }
QLabel[role="bad"] { color: #d1495b; }
QPushButton { background-color: #e6e6e6; border: 1px solid #a0a0a0;
    border-radius: 4px; padding: 4px 12px; }
QPushButton:hover { background-color: #dcdcdc; }
QPushButton:pressed { background-color: #cfcfcf; }
QPushButton:disabled { color: #a8a8a8; border-color: #d0d0d0; }
QCheckBox::indicator, QRadioButton::indicator, QGroupBox::indicator {
    width: 13px; height: 13px; border: 1px solid #7a7a7a; background-color: #ffffff; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator, QGroupBox::indicator { border-radius: 3px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked,
QGroupBox::indicator:checked { background-color: #2a9d8f; border-color: #2a9d8f; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled,
QGroupBox::indicator:disabled { border-color: #d0d0d0; }
"""
DARK_QSS = """
QWidget { background-color: #1c1c1c; color: #fafafa; }
QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox { background-color: #2b2b2b; color: #fafafa; }
QLabel[role="muted"] { color: #9a9a9a; }
QLabel[role="good"] { color: #2a9d8f; }
QLabel[role="bad"] { color: #d1495b; }
QPushButton { background-color: #3a3a3a; border: 1px solid #6a6a6a;
    border-radius: 4px; padding: 4px 12px; color: #fafafa; }
QPushButton:hover { background-color: #454545; }
QPushButton:pressed { background-color: #2f2f2f; }
QPushButton:disabled { color: #6a6a6a; border-color: #4a4a4a; }
QCheckBox::indicator, QRadioButton::indicator, QGroupBox::indicator {
    width: 13px; height: 13px; border: 1px solid #8a8a8a; background-color: #2b2b2b; }
QRadioButton::indicator { border-radius: 7px; }
QCheckBox::indicator, QGroupBox::indicator { border-radius: 3px; }
QCheckBox::indicator:checked, QRadioButton::indicator:checked,
QGroupBox::indicator:checked { background-color: #2a9d8f; border-color: #2a9d8f; }
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled,
QGroupBox::indicator:disabled { border-color: #4a4a4a; }
"""


# QSettings keys. Nothing used to persist, so every launch started light themed
# at log level "info" with the port re detected from scratch.
SETTING_DARK_MODE = 'ui/dark_mode'
SETTING_LOG_LEVEL = 'log/level'
SETTING_PORT = 'serial/preferred_port'
SETTING_MEASURE_TIMEOUT = 'serial/measure_timeout'


def _settings():
    return QSettings()


def _get_setting(key, default, type_=str):
    # type= matters: some backends return every stored value as a string, so a bool
    # would come back as 'false', which is truthy.
    try:
        return _settings().value(key, default, type=type_)
    except Exception:
        return default


def _set_setting(key, value):
    try:
        _settings().setValue(key, value)
    except Exception:
        pass  # a remembered preference is never worth failing over


class _MeasureOutcome:
    """What the measurement worker hands back to the GUI thread.

    An object rather than an exception because Worker.failed carries only str(exc),
    which loses the distinction between a dead USB link (connection is gone) and a
    calibration problem (connection is fine).
    """

    def __init__(self, ok, mode, error=None, dead=False, measurement=None, calib=None,
                 flux=None, luminance=None, peak=None, fwhm=None, chroma=None,
                 int_time=None, scans=None, pushed_time=False, pushed_scans=False):
        self.ok = ok
        self.mode = mode
        self.error = error
        self.dead = dead
        self.measurement = measurement
        self.calib = calib
        self.flux = flux
        self.luminance = luminance
        self.peak = peak
        self.fwhm = fwhm
        self.chroma = chroma
        self.int_time = int_time
        self.scans = scans
        self.pushed_time = pushed_time
        self.pushed_scans = pushed_scans


COL_WHEN, COL_LABEL, COL_MODE, COL_LUMINANCE = range(4)


class _ReadingItem(QTreeWidgetItem):
    """History row that sorts its luminance column numerically.

    QTreeWidgetItem compares columns as text, which orders 9e-05 above 1200. Useless
    for the one column people actually want to rank by.
    """

    def __lt__(self, other):
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else COL_WHEN
        if column == COL_LUMINANCE:
            try:
                return float(self.text(column)) < float(other.text(column))
            except ValueError:
                pass  # blank luminance (no calibration); fall through to text order
        return self.text(column) < other.text(column)


def _make_scroll_tab(content):
    """Wrap a tab in a QScrollArea (so a tall tab scrolls instead of clipping) with
    QScroller panning for one finger touch drag on phones.

    Vertical only. Horizontal movement is just drift: it takes turning off the
    scrollbar AND turning off QScroller's horizontal overshoot, since the kinetic
    scroller rubber bands sideways even with nothing to scroll to.
    """
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(content)

    QScroller.grabGesture(scroll.viewport(), QScroller.ScrollerGestureType.TouchGesture)
    scroller = QScroller.scroller(scroll.viewport())
    props = scroller.scrollerProperties()
    props.setScrollMetric(QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy,
                          QScrollerProperties.OvershootPolicy.OvershootAlwaysOff)
    scroller.setScrollerProperties(props)
    return scroll


class _PlotToolbar(NavigationToolbar2QT):
    """The matplotlib toolbar without its Save button.

    Its save calls figure.savefig(path) directly, which cannot write to an Android
    content:// URI and so produced 0 byte files there. The app's own "Save figure..."
    goes through file_io. Filtering by name rather than index so a matplotlib
    update that reorders the toolbar cannot silently drop the wrong tool.
    """
    toolitems = [item for item in NavigationToolbar2QT.toolitems if item[0] != 'Save']


def _fit_width(widget):
    """Let a widget be squeezed below its natural width instead of forcing the whole
    tab wider than the screen. Used for the matplotlib toolbars, whose row of buttons
    is wider than a phone in portrait."""
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())
    return widget


class OSpRadApp(QMainWindow):
    def __init__(self, port=None):
        super().__init__()
        self.setWindowTitle('OSpRad %s' % __version__)
        # Conservative small phone portrait floor; each tab also scrolls if its
        # content is still taller than this (see _make_scroll_tab).
        self.setMinimumSize(320, 480)

        # Pre selects the port combo built in _build_main_tab; None means auto detect.
        self._initial_port = port

        # Read before _build_ui: the plots are constructed with dark=self.dark_mode, so
        # restoring the theme afterwards would leave them on the wrong palette.
        self.dark_mode = _get_setting(SETTING_DARK_MODE, False, bool)
        # Held here rather than read off a widget: _log runs before the Settings tab
        # exists, and during shutdown after it is gone.
        self._log_level = _get_setting(SETTING_LOG_LEVEL, 'info')
        if self._log_level not in LOG_LEVELS:
            self._log_level = 'info'
        self.connection = None
        self.store = calibration.CalibrationStore(CALIBRATION_FILE)
        self.measurement = None
        self.reading = None
        self._last_luminance = None
        self._motor_test_angle = 30

        self._repeat_running = False
        self._repeat_next_time = None
        self._repeat_queue = []
        self._repeat_interval_secs = 300
        self._repeat_run = 0
        self._repeat_round = 0
        self._repeat_base = 'auto'
        self._repeat_both_modes = False
        self._repeat_mode = 'i'

        self._continuous_running = False
        self._continuous_queue = []
        self._continuous_frames = 0
        self._continuous_frame_started = 0.0
        self._continuous_slow_hinted = False
        # When the firmware last took a dark reference for this run. None until the
        # first full measurement of the run has started.
        self._continuous_dark_time = None
        # Measurement settings continuous mode borrowed, to hand back on stop.
        self._continuous_saved_settings = None
        # Whether it is choosing the exposure itself (the user left it on auto).
        self._continuous_manages_exposure = False
        # Whether this run has actually skipped a dark re reference, which is what
        # makes its readings unfit for the history.
        self._continuous_held_dark = False
        # Whether the measurement in flight is one of those.
        self._measure_unsaveable = False
        # Set when the shutter has been left open and should be closed as soon as
        # the link is free. See _park_wheel.
        self._park_pending = False

        self._prev_int_time = None
        self._prev_scans = None
        self._compared_offsets = set()
        self._saved_labels = set()
        # Whether the current reading has already been written to the history, so
        # Save can grey out after use without _update_controls turning it back on.
        self._reading_saved = False
        # Guards _update_controls against running mid construction, when the widgets
        # from later tabs don't exist yet.
        self._ui_ready = False
        # Populated by _build_main_tab; read by _sync_sections and _set_save_error,
        # both of which can run before it exists.
        self._sections = {}
        self._section_switching = False
        self._connect_worker = None
        self._connect_port = None
        self._measure_worker = None
        self._measure_done = None
        self._measure_cancelled = False
        self._closing = False
        self._close_deadline = None

        self._missed_port_scans = 0
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(HEARTBEAT_MS)
        self._heartbeat_timer.timeout.connect(self._check_connection_alive)

        self._measure_started = 0.0
        self._measure_label = ''
        self._measure_tick = QTimer(self)
        self._measure_tick.setInterval(1000)
        self._measure_tick.timeout.connect(self._update_measure_status)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_repeat_countdown)

        self._build_ui()
        self._install_log_bridge()
        self._apply_theme()
        self._load_saved_readings()
        QTimer.singleShot(100, self._connect)

    def _build_ui(self):
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(_make_scroll_tab(self._build_main_tab()), 'Main')
        tabs.addTab(_make_scroll_tab(self._build_history_tab()), 'History')
        tabs.addTab(_make_scroll_tab(self._build_monitor_cal_tab()), 'Monitor calibration')
        tabs.addTab(_make_scroll_tab(self._build_calibration_tab()), 'Calibration')
        tabs.addTab(_make_scroll_tab(self._build_debug_tab()), 'Debug')
        tabs.addTab(_make_scroll_tab(self._build_settings_tab()), 'Settings')
        self._ui_ready = True
        self._refresh_log_level_hint()
        self._sync_sections()

    def _running_section(self):
        """Which section, if any, currently owns the hardware."""
        if self._continuous_running:
            return 'continuous'
        if self._repeat_running:
            return 'repeat'
        return None

    def _on_section_toggled(self, name, checked):
        """Keep exactly one of Measurement, Continuous mode and Automatic repeat open.

        Qt's checkable QGroupBox has no notion of a group, so the mutual
        exclusion lives here. setChecked below re enters this handler, hence the guard.
        """
        if self._section_switching:
            return
        self._section_switching = True
        try:
            running = self._running_section()
            if running is not None and name != running:
                # A run owns its section: folding it away would take its Stop
                # button with it. Refuse the switch rather than strand the run.
                self._sections[name].setChecked(False)
                self._sections[running].setChecked(True)
            elif checked:
                for other, box in self._sections.items():
                    if other != name:
                        box.setChecked(False)
            elif not any(box.isChecked() for box in self._sections.values()):
                # Closing the last open one would leave the tab with no mode at all.
                self._sections[name].setChecked(True)
        finally:
            self._section_switching = False
        self._sync_sections()

    def _sync_sections(self):
        """Show the settings and save controls that belong to the open section."""
        if not self._ui_ready:
            return
        continuous = self._sections['continuous'].isChecked()
        repeat = self._sections['repeat'].isChecked()
        # Continuous mode takes exposure and scan count over for the run and saves
        # nothing, so neither box has anything to offer there.
        self._settings_box.setVisible(not continuous)
        self._save_box.setVisible(not continuous)
        self._save_box.setTitle('Save readings' if repeat else 'Save reading')
        self.bt_save.setVisible(not repeat)
        self.save_hint_label.setText(
            'Every reading the run saves is named after this label with a number '
            'after it, so "lamp" becomes lamp_1, lamp_2, and so on.' if repeat else
            'Names this reading in the History tab.')
        self._set_save_error(self.save_error_label.text(),
                             self.save_error_label.property('role') or 'bad')
        # Unfolding a checkable QGroupBox re enables every child, which would offer
        # a Stop for a loop that isn't running. Re deriving the states undoes that.
        self._update_controls()

    def _hardware_tabs(self):
        """The tabs that hold a connection and their own plot."""
        return (self.unit_setup_tab, self.linearisation_tab, self.sensitivity_tab,
                self.cosine_tab, self.transfer_tab, self.monitor_cal_tab)

    def _apply_theme(self):
        QApplication.instance().setStyleSheet(DARK_QSS if self.dark_mode else LIGHT_QSS)
        self.plot.apply_theme(self.dark_mode)
        self.history_plot.apply_theme(self.dark_mode)
        # These four own a plot of their own (and the cosine tab an angle diagram).
        # Without this they keep the light palette they were constructed with.
        for widget in (self.linearisation_tab, self.sensitivity_tab,
                       self.cosine_tab, self.monitor_cal_tab):
            widget.apply_theme(self.dark_mode)

    def _toggle_theme(self, checked):
        self.dark_mode = checked
        _set_setting(SETTING_DARK_MODE, checked)
        self._apply_theme()

    def _build_main_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)

        port_row = QHBoxLayout()
        port_tip = ('Which serial port to connect to. Auto detect finds the OSpRad by '
                    'itself; only pick one manually if that finds the wrong device.')
        port_row.addWidget(QLabel('Port'))
        port_row.addWidget(help_button(port_tip))
        self.port_combo = QComboBox()
        tip(self.port_combo, port_tip)
        port_row.addWidget(self.port_combo, 1)
        self.bt_refresh_ports = QPushButton('Refresh')
        self.bt_refresh_ports.clicked.connect(self._refresh_ports)
        port_row.addWidget(self.bt_refresh_ports)
        layout.addLayout(port_row)
        self._refresh_ports()

        conn_row = QHBoxLayout()
        self.conn_status_label = wrapped_label('Not connected.')
        _set_role(self.conn_status_label, 'muted')
        conn_row.addWidget(self.conn_status_label, 1)
        self.bt_connect = QPushButton('Reconnect')
        self.bt_connect.clicked.connect(self._connect)
        conn_row.addWidget(self.bt_connect)
        layout.addLayout(conn_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        # The three things the OSpRad can be doing are one accordion: exactly
        # one open at a time, so the controls on screen always belong to the mode
        # in use, and the settings below can follow the open section instead of
        # offering every mode's options to every mode.
        measure_box, measure_layout = collapsible_group('Measurement', start_open=True)
        measure_header = QHBoxLayout()
        measure_header.addWidget(wrapped_label(
            'Point the OSpRad at what you want to measure, then choose a mode.'), 1)
        measure_header.addWidget(help_button(
            'Radiance: %s\n\nIrradiance: %s'
            % (WHEEL_ROLE_HELP['R'], WHEEL_ROLE_HELP['I'])))
        measure_layout.addLayout(measure_header)

        self.bt_rad = QPushButton('Radiance')
        self.bt_rad.setMinimumHeight(36)
        self.bt_rad.setEnabled(False)
        self.bt_rad.clicked.connect(lambda: self._measure('r'))
        tip(self.bt_rad, WHEEL_ROLE_HELP['R'])
        measure_layout.addWidget(self.bt_rad)

        self.bt_irr = QPushButton('Irradiance')
        self.bt_irr.setMinimumHeight(36)
        self.bt_irr.setEnabled(False)
        self.bt_irr.clicked.connect(lambda: self._measure('i'))
        tip(self.bt_irr, WHEEL_ROLE_HELP['I'])
        measure_layout.addWidget(self.bt_irr)

        layout.addWidget(measure_box)

        # Both boxes below are collapsible: occasional features that would
        # otherwise cost permanent vertical space on a phone. Folded by default.
        live_box, live_layout = collapsible_group('Continuous mode')

        live_tip = ('Measures over and over for as long as it is running, '
                    'redrawing the plot after every update, so you can move the '
                    'OSpRad around and watch the spectrum follow it. Nothing is '
                    'saved to the history.\n\n'
                    'Tick Irradiance and/or Radiance below to choose what is '
                    'measured. With both ticked updates alternate, which is slower: '
                    'the filter wheel has to travel between the two positions, '
                    'where a single mode leaves it still.\n\n'
                    'While it runs it borrows the measurement settings above: one '
                    'scan per update, and (unless you have fixed an integration '
                    'time yourself) an exposure it picks and holds. Your settings '
                    'come back when it stops. Holding the exposure is what makes it '
                    'fast. The firmware can only reuse its dark reference while the '
                    'exposure stays put, so pointing at something much brighter or '
                    'darker costs a couple of slow updates while it re exposes.\n\n'
                    'Firmware 1.0.0 or newer is needed for any of that speed. '
                    'Older firmware measures a fresh dark reference for every update.')
        live_header = QHBoxLayout()
        live_header.addWidget(wrapped_label(
            'Measure continuously for a live plot. Nothing is saved.'), 1)
        live_header.addWidget(help_button(live_tip))
        live_layout.addLayout(live_header)

        live_row = QHBoxLayout()
        self.bt_continuous_start = QPushButton('Start')
        self.bt_continuous_start.setEnabled(False)
        self.bt_continuous_start.clicked.connect(self._start_continuous)
        tip(self.bt_continuous_start, 'Start measuring continuously.')
        live_row.addWidget(self.bt_continuous_start)
        self.bt_continuous_stop = QPushButton('Stop')
        self.bt_continuous_stop.setEnabled(False)
        self.bt_continuous_stop.clicked.connect(lambda: self._stop_continuous())
        tip(self.bt_continuous_stop, 'Stop after the update currently in flight. Use '
                                     'Cancel above to abandon that one too.')
        live_row.addWidget(self.bt_continuous_stop)
        live_row.addStretch(1)
        live_layout.addLayout(live_row)

        self.continuous_status_label = QLabel('Not running.')
        _set_role(self.continuous_status_label, 'muted')
        live_layout.addWidget(self.continuous_status_label)

        # Indented under the row above, matching the repeat box below.
        live_modes = QHBoxLayout()
        live_modes.setContentsMargins(20, 0, 0, 0)
        live_measure_label = QLabel('Measure:')
        _set_role(live_measure_label, 'muted')
        live_measure_tip = ('Which reading each update takes. Tick both to alternate '
                            'irradiance and radiance.')
        live_modes.addWidget(captioned(live_measure_label, live_measure_tip))
        self.continuous_irr_check = QCheckBox('Irradiance')
        self.continuous_irr_check.setChecked(True)
        tip(self.continuous_irr_check, 'Include an Irradiance reading in the live loop.')
        live_modes.addWidget(self.continuous_irr_check)
        self.continuous_rad_check = QCheckBox('Radiance')
        tip(self.continuous_rad_check, 'Include a Radiance reading in the live loop.')
        live_modes.addWidget(self.continuous_rad_check)
        live_modes.addStretch(1)
        live_layout.addLayout(live_modes)

        hold_row = QHBoxLayout()
        hold_row.setContentsMargins(20, 0, 0, 0)
        hold_tip = (
            'Stops the shutter wheel moving during a live run, at the cost of the '
            'readings slowly going wrong. For demonstrating only.\n\n'
            'What it turns off: the sensor reads a signal even in the dark, and '
            'that offset is measured with the shutter closed and subtracted from '
            'every reading. Normally OSpRad re measures it every %d seconds, '
            'which is the one thing that still moves the wheel once a live run is '
            'up to speed. Tick this and it measures the dark once, at the start '
            'of the run, and keeps subtracting that one.\n\n'
            'Why that goes wrong: the dark offset grows as the sensor warms up, '
            'so an old one under subtracts and the whole spectrum reads gradually '
            'too high. Drift is worst at long exposures and on a dim target, and '
            'worse the longer the run goes on. Nothing on screen looks broken, '
            'which is exactly why the numbers should not be quoted.\n\n'
            'The exposure is held too. A dark reading is only good for the '
            'exposure it was taken at, so re exposing means re measuring the '
            'dark, which means moving the wheel. Point at something much '
            'brighter and the spectrum clips flat; point at something much '
            'darker and it sinks into the noise. Neither recovers on its own. '
            'Untick this to let OSpRad re expose, at the price of the wheel '
            'moving again.\n\n'
            'Readings taken while the dark was being held cannot be saved to the '
            'history. Untick this, or take a normal Radiance or Irradiance '
            'measurement, for numbers worth keeping.'
            % CONTINUOUS_DARK_REFRESH_SECONDS)
        self.continuous_hold_dark_check = QCheckBox('Hold dark reference')
        tip(self.continuous_hold_dark_check, hold_tip)
        hold_row.addWidget(self.continuous_hold_dark_check)
        hold_row.addWidget(help_button(hold_tip))
        hold_row.addStretch(1)
        live_layout.addLayout(hold_row)

        layout.addWidget(live_box)

        repeat_box, repeat_layout = collapsible_group('Automatic repeat')

        repeat_tip = ('Takes a measurement automatically every N seconds and saves '
                      'each one to the history under the label from the Save reading '
                      'box, without asking again.\n\n'
                      'Tick Irradiance and/or Radiance below to choose what is '
                      'measured each time, then press Start. Readings all share the '
                      'same label, so tell them apart by their timestamp in the '
                      'History tab.')
        repeat_header = QHBoxLayout()
        repeat_header.addWidget(wrapped_label(
            'Measure and save on a timer, unattended.'), 1)
        repeat_header.addWidget(help_button(repeat_tip))
        repeat_layout.addLayout(repeat_header)

        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel('Every (s)'))
        self.repeat_time_edit = QLineEdit('300')
        self.repeat_time_edit.setFixedWidth(60)
        tip(self.repeat_time_edit, repeat_tip)
        repeat_row.addWidget(self.repeat_time_edit)
        self.bt_repeat_start = QPushButton('Start')
        self.bt_repeat_start.setEnabled(False)
        self.bt_repeat_start.clicked.connect(self._start_repeat)
        tip(self.bt_repeat_start, 'Start automatically taking and saving measurements '
                                  'on the interval set above.')
        repeat_row.addWidget(self.bt_repeat_start)
        self.bt_repeat_stop = QPushButton('Stop')
        self.bt_repeat_stop.setEnabled(False)
        self.bt_repeat_stop.clicked.connect(self._stop_repeat)
        tip(self.bt_repeat_stop, 'Stop automatic repeat.')
        repeat_row.addWidget(self.bt_repeat_stop)
        repeat_row.addStretch(1)
        repeat_layout.addLayout(repeat_row)

        self.repeat_status_label = QLabel('Not running.')
        _set_role(self.repeat_status_label, 'muted')
        repeat_layout.addWidget(self.repeat_status_label)

        # Indented under the row above; checkboxes only do anything while repeat runs.
        repeat_modes = QHBoxLayout()
        repeat_modes.setContentsMargins(20, 0, 0, 0)
        measure_label = QLabel('Measure:')
        _set_role(measure_label, 'muted')
        measure_tip = ('Which reading(s) each automatic repeat takes. Tick both to '
                       'record an Irradiance and a Radiance measurement every interval.')
        repeat_modes.addWidget(captioned(measure_label, measure_tip))
        self.repeat_irr_check = QCheckBox('Irradiance')
        self.repeat_irr_check.setChecked(True)
        tip(self.repeat_irr_check, 'Take an Irradiance reading on each automatic repeat.')
        repeat_modes.addWidget(self.repeat_irr_check)
        self.repeat_rad_check = QCheckBox('Radiance')
        tip(self.repeat_rad_check, 'Take a Radiance reading on each automatic repeat.')
        repeat_modes.addWidget(self.repeat_rad_check)
        repeat_modes.addStretch(1)
        repeat_layout.addLayout(repeat_modes)

        layout.addWidget(repeat_box)

        self._sections = {'measure': measure_box, 'continuous': live_box,
                          'repeat': repeat_box}
        for name, box in self._sections.items():
            box.toggled.connect(
                lambda checked, n=name: self._on_section_toggled(n, checked))

        # Outside the accordion: a measurement started from any section reports here,
        # and Cancel has to stay reachable whichever section is open.
        status_row = QHBoxLayout()
        self.measure_status_label = wrapped_label('')
        _set_role(self.measure_status_label, 'muted')
        status_row.addWidget(self.measure_status_label, 1)
        self.bt_measure_cancel = QPushButton('Cancel')
        self.bt_measure_cancel.clicked.connect(self._cancel_measure)
        tip(self.bt_measure_cancel,
            'Stop waiting for this measurement. The OSpRad is reset and '
            'reconnected, which also stops the scan it is part way through.')
        status_row.addWidget(self.bt_measure_cancel, 0, Qt.AlignmentFlag.AlignTop)
        self._measure_status_row = (self.measure_status_label, self.bt_measure_cancel)
        for widget in self._measure_status_row:
            widget.setVisible(False)
        layout.addLayout(status_row)

        # Hidden while continuous mode is the open section: it takes these over for
        # the duration of a run, so offering them there would only invite edits it
        # is about to overwrite.
        self._settings_box = QGroupBox('Measurement settings')
        settings = QGridLayout(self._settings_box)
        settings.setColumnStretch(2, 1)

        int_time_label = QLabel('Integration time (ms)')
        self.int_time_edit = QLineEdit('0')
        self.int_time_edit.setFixedWidth(70)
        int_time_tip = ('Per scan exposure in milliseconds. 0 (the default) lets the '
                        'firmware auto expose just below saturation; set a fixed value '
                        'only when repeated measurements need identical exposure.')
        tip(int_time_label, int_time_tip)
        tip(self.int_time_edit, int_time_tip)
        settings.addWidget(captioned(int_time_label, int_time_tip), 0, 0)
        settings.addWidget(self.int_time_edit, 0, 1)

        scans_label = QLabel('Scans, min / max')
        scans_row = QHBoxLayout()
        self.min_scans_edit = QLineEdit('3')
        self.min_scans_edit.setFixedWidth(45)
        self.max_scans_edit = QLineEdit('50')
        self.max_scans_edit.setFixedWidth(45)
        scans_row.addWidget(self.min_scans_edit)
        scans_row.addWidget(QLabel('/'))
        scans_row.addWidget(self.max_scans_edit)
        scans_row.addStretch(1)
        scans_tip = ('How many scans the firmware averages into one measurement. The '
                     'firmware picks a value in this range itself. Short exposures need '
                     'more repeats to fill ~1s of total sampling time.')
        tip(scans_label, scans_tip)
        tip(self.min_scans_edit, scans_tip)
        tip(self.max_scans_edit, scans_tip)
        settings.addWidget(captioned(scans_label, scans_tip), 1, 0)
        settings.addLayout(scans_row, 1, 1)
        layout.addWidget(self._settings_box)

        # One label field, two shapes. A single measurement is saved by hand, under
        # exactly this label; automatic repeat saves every reading itself and only
        # needs the label as a stem, so its Save button would have nothing to do.
        self._save_box = QGroupBox('Save reading')
        save_layout = QVBoxLayout(self._save_box)
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel('Label'))
        label_row.addWidget(help_button(
            'Names the reading in the History tab. Readings are not required to have '
            'unique labels. If you reuse one, OSpRad asks whether to keep both or '
            'replace the older reading.\n\n'
            'Under Automatic repeat the label is a stem rather than a name: every '
            'reading the run saves gets a number after it, so they can be told apart '
            'in the history.'))
        label_row.addStretch(1)
        save_layout.addLayout(label_row)
        self.save_label_edit = QLineEdit()
        # Typing a label clears a "needs a label" complaint without needing another press.
        self.save_label_edit.textChanged.connect(lambda _: self._set_save_error(''))
        save_layout.addWidget(self.save_label_edit)

        self.save_hint_label = wrapped_label('')
        _set_role(self.save_hint_label, 'muted')
        save_layout.addWidget(self.save_hint_label)

        self.bt_save = QPushButton('Save reading')
        self.bt_save.setMinimumHeight(36)
        self.bt_save.clicked.connect(self._on_save_clicked)
        tip(self.bt_save, 'Save the current reading to the history, under the label above.')
        save_layout.addWidget(self.bt_save)

        # Inline rather than a dialog: a modal on a phone hides the very field it is
        # complaining about, and this sits directly under the control that failed.
        self.save_error_label = wrapped_label('')
        _set_role(self.save_error_label, 'bad')
        self.save_error_label.setVisible(False)
        save_layout.addWidget(self.save_error_label)
        layout.addWidget(self._save_box)

        self._refresh_save_button()
        layout.addWidget(self._build_analysis())

        self.cursor_label = QLabel('')
        _set_role(self.cursor_label, 'muted')

        self.plot = plotting.SpectrumPlot(dark=self.dark_mode)
        self.plot.on_hover = lambda text: self.cursor_label.setText(text or '')
        plot_layout = QVBoxLayout()
        toolbar = _fit_width(_PlotToolbar(self.plot.canvas, content))
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(self.cursor_label)
        plot_layout.addWidget(self.plot.canvas, 1)
        layout.addLayout(plot_layout, 1)

        actions = QHBoxLayout()
        # Dark mode lives on the Settings tab now. It is an app wide preference, not
        # a main tab control, and it was stranded below the plot on a phone.
        actions.addStretch(1)
        save_fig_btn = QPushButton('Save figure...')
        # Lambda, not a bare connect: clicked emits a `checked` bool that would
        # otherwise arrive as the `plot` argument.
        save_fig_btn.clicked.connect(lambda: self._save_figure(self.plot, 'osprad-plot'))
        actions.addWidget(save_fig_btn)
        layout.addLayout(actions)

        return content

    def _build_analysis(self):
        group = QGroupBox('Analysis')
        grid = QGridLayout(group)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        tips = {
            'peak': 'Wavelength of the highest intensity. Daylight/white LEDs peak '
                    'around 450 to 550nm; incandescent bulbs peak further into the '
                    'red, often >600nm.',
            'fwhm': 'Width of the main peak at half height. A few nm = single LED/'
                    'laser line; broad or "n/a" = broadband source like daylight or '
                    'an incandescent bulb.',
            'cie_x': 'CIE 1931 chromaticity x (perceived colour, brightness '
                     'independent). Daylight ~ (0.31, 0.33); warm incandescent ~ '
                     '(0.45, 0.41).',
            'cie_y': 'CIE 1931 chromaticity y. Read together with CIE x: the pair '
                     'gives the perceived colour independently of brightness. '
                     'Daylight ~ (0.31, 0.33); warm incandescent ~ (0.45, 0.41).',
            'cct': 'Approximate "warmth" in Kelvin. ~2700K = warm/orange '
                   '(incandescent); ~5000 to 6500K = cool/blue (daylight). Shows "-" '
                   'for narrow band light, where CCT is meaningless.',
        }

        self._analysis_labels = {}
        fields = (('peak', 'Peak λ'), ('fwhm', 'FWHM'),
                  ('cie_x', 'CIE x'), ('cie_y', 'CIE y'), ('cct', 'CCT (approx.)'))
        for i, (key, caption) in enumerate(fields):
            row, col = divmod(i, 2)
            caption_label = QLabel(caption + ':')
            _set_role(caption_label, 'muted')
            tip(caption_label, tips[key])
            grid.addWidget(captioned(caption_label, tips[key]), row, col * 2)
            value = QLabel('-')
            tip(value, tips[key])
            grid.addWidget(value, row, col * 2 + 1)
            self._analysis_labels[key] = value

        return group

    def _build_history_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)

        header = QHBoxLayout()
        saved_label = QLabel('Saved readings')
        _set_role(saved_label, 'muted')
        header.addWidget(saved_label, 1)
        clear_btn = QPushButton('Clear comparison')
        clear_btn.clicked.connect(self._clear_history_plot)
        header.addWidget(clear_btn)
        layout.addLayout(header)

        self.saved_tree = QTreeWidget()
        # "When" carries date + time: it sorts chronologically as plain text, and
        # it surfaces the date, which the index has always carried but never showed.
        self.saved_tree.setHeaderLabels(['When', 'Label', 'Mode', 'Lux/cd·m²'])
        self.saved_tree.setSortingEnabled(True)
        self.saved_tree.sortByColumn(COL_WHEN, Qt.SortOrder.DescendingOrder)
        self.saved_tree.setRootIsDecorated(False)
        self.saved_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.saved_tree.setMaximumHeight(220)
        self.saved_tree.itemDoubleClicked.connect(self._on_saved_double_click)
        self.saved_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.saved_tree.customContextMenuRequested.connect(self._on_saved_context_menu)
        # There is no right button on a phone, so a long press raises the same menu.
        # Held on the viewport, which is what customContextMenuRequested reports against.
        self._tree_long_press = touch.install_long_press(
            self.saved_tree.viewport(), self._on_saved_context_menu)
        layout.addWidget(self.saved_tree)

        layout.addWidget(wrapped_label(
            'Double click (or double tap) a reading to add it to the comparison plot '
            'below; again to remove it. Right click, or press and hold on a '
            'touchscreen, for more options, including on a multi selection.'))

        self.history_plot = plotting.SpectrumPlot(dark=self.dark_mode)
        plot_layout = QVBoxLayout()
        toolbar = _fit_width(_PlotToolbar(self.history_plot.canvas, content))
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(self.history_plot.canvas, 1)
        layout.addLayout(plot_layout, 1)

        # The comparison plot had no export of its own once the toolbar's Save went.
        history_actions = QHBoxLayout()
        history_actions.addStretch(1)
        save_history_fig_btn = QPushButton('Save figure...')
        save_history_fig_btn.clicked.connect(
            lambda: self._save_figure(self.history_plot, 'osprad-comparison'))
        history_actions.addWidget(save_history_fig_btn)
        layout.addLayout(history_actions)

        return content

    # Monitor calibration is a downstream USE of an already calibrated device,
    # blocked by MonitorCalibrationTab._start() until the unit is set up, hence a
    # top level tab.

    def _build_monitor_cal_tab(self):
        self.monitor_cal_tab = MonitorCalibrationTab(self.connection, self.store)
        return self.monitor_cal_tab

    # ---------------- Calibration tab ----------------

    def _build_calibration_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(wrapped_label(
            'One time setup per unit. Unit number and wheel positions live on the '
            'Arduino; linearisation and spectral sensitivity live in '
            'calibration_data.csv.'))

        cal_tabs = QTabWidget()
        self.unit_setup_tab = UnitSetupTab(self.connection, self.store)
        self.linearisation_tab = LinearisationTab(self.connection, self.store)
        self.sensitivity_tab = SensitivityTab(self.connection, self.store)
        self.cosine_tab = CosineResponseTab(self.connection, self.store)
        self.transfer_tab = CalibrationTransferTab(self.connection, self.store, self._log)
        cal_tabs.addTab(self.unit_setup_tab, 'Unit & wheel setup')
        cal_tabs.addTab(self.linearisation_tab, 'Linearisation')
        cal_tabs.addTab(self.sensitivity_tab, 'Spectral sensitivity')
        cal_tabs.addTab(self.cosine_tab, 'Cosine response')
        cal_tabs.addTab(self.transfer_tab, 'Import & export')
        layout.addWidget(cal_tabs, 1)

        return content

    def _build_debug_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)

        components = QGroupBox('Components')
        comp_layout = QVBoxLayout(components)
        self.debug_status_label = wrapped_label('Not connected.')
        comp_layout.addWidget(self.debug_status_label)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        comp_layout.addWidget(separator)

        self.sensor_verdict_label = QLabel('? Optical sensor: unknown')
        _set_role(self.sensor_verdict_label, 'muted')
        comp_layout.addWidget(self.sensor_verdict_label)
        self.sensor_detail_label = QLabel('')
        _set_role(self.sensor_detail_label, 'muted')
        comp_layout.addWidget(self.sensor_detail_label)

        motor_row = QHBoxLayout()
        motor_row.addWidget(QLabel('Filter wheel motor'), 1)
        self.bt_motor_test = QPushButton('Test')
        self.bt_motor_test.setEnabled(False)
        self.bt_motor_test.clicked.connect(self._test_motor)
        motor_row.addWidget(self.bt_motor_test)
        comp_layout.addLayout(motor_row)
        layout.addWidget(components)

        log_header = QHBoxLayout()
        log_label = QLabel('Log')
        _set_role(log_label, 'muted')
        log_header.addWidget(log_label, 1)
        # The level control lives on the Settings tab: one setting, one control.
        self.log_level_hint = QLabel('')
        _set_role(self.log_level_hint, 'muted')
        log_header.addWidget(self.log_level_hint)
        layout.addLayout(log_header)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(LOG_MAX_LINES)
        layout.addWidget(self.log_text, 1)

        return content

    def _refresh_ports(self):
        """Repopulate the port combo from a fresh scan, keeping the current selection
        even if this scan doesn't see it. The device may just be momentarily unplugged.

        On the first call nothing is selected yet, so the starting choice comes from
        --port, else the remembered preferred port, else auto detect.
        """
        if self.port_combo.count():
            keep = self.port_combo.currentText()
        else:
            keep = self._initial_port or _get_setting(SETTING_PORT, PORT_AUTO) or PORT_AUTO
        ports = serial_io.list_ports()

        for combo in (self.port_combo, getattr(self, 'settings_port_combo', None)):
            if combo is None:
                continue
            wanted = keep if combo is self.port_combo else combo.currentText() or keep
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(PORT_AUTO)
            combo.addItems(ports)
            if wanted != PORT_AUTO and wanted not in ports:
                combo.addItem(wanted)
            idx = combo.findText(wanted)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _selected_port(self):
        text = self.port_combo.currentText()
        return None if text == PORT_AUTO else text

    def _connect(self):
        # Runs on a background QThread (qt_worker.Worker); blocking the main thread
        # would freeze the UI including the log that's supposed to show progress.
        self.connection = None
        # A reconnected (or re plugged) unit is back on firmware defaults, so the
        # cache of what we last pushed is no longer true.
        self._prev_int_time = None
        self._prev_scans = None
        self._set_connected(False)
        self._propagate_connection()
        self.bt_connect.setEnabled(False)
        self.bt_connect.setText('Connecting...')
        self.setWindowTitle('OSpRad %s' % __version__)
        self._log('Connecting...')
        self._update_conn_labels('Connecting...')
        self.sensor_verdict_label.setText('? Optical sensor: unknown')
        _set_role(self.sensor_verdict_label, 'muted')
        self.sensor_detail_label.setText('')

        # Rescan for new devices; read here since the worker thread can't touch widgets.
        self._refresh_ports()
        self._connect_port = self._selected_port()

        self._connect_worker = Worker(self._do_connect)
        self._connect_worker.succeeded.connect(self._connect_succeeded)
        self._connect_worker.failed.connect(self._connect_failed)
        self._connect_worker.start()

    def _do_connect(self):
        self.store.load()
        connection = serial_io.SerialConnection(port=self._connect_port)
        config = connection.check_firmware()
        return connection, config

    def _connect_failed(self, message):
        self.bt_connect.setEnabled(True)
        self.bt_connect.setText('Reconnect')
        self._log(message, level='error')
        self._update_conn_labels(message)

    def _connect_succeeded(self, result):
        connection, config = result
        self.connection = connection
        connection.measure_timeout = self._measure_timeout_setting()
        self._missed_port_scans = 0
        self._heartbeat_timer.start()
        _set_role(self.conn_status_label, 'muted')  # may be 'bad' from a lost connection
        self._propagate_connection(config)
        self._set_connected(True)
        self.bt_connect.setEnabled(True)
        self.bt_connect.setText('Reconnect')
        # Title (not just the log) so unit/firmware stay visible after later log messages.
        self.setWindowTitle('OSpRad %s, unit #%d, firmware v%s'
                            % (__version__, config.unit_number, config.firmware))
        status = ('Connected to unit #%d on %s (firmware v%s)'
                  % (config.unit_number, connection.port, config.firmware))
        self._log(status)
        self._update_conn_labels(status)
        self._update_sensor_status(config)
        # A cancel reconnects to stop the scan; the wheel is wherever it was abandoned.
        # Park it before anything else tries to take a measurement.
        self._park_wheel()

    def _update_sensor_status(self, config):
        detected = config.sensor_detected
        if detected is None:
            self.sensor_verdict_label.setText(
                '? Optical sensor: not checked (needs firmware 1.0.0 or newer)')
            _set_role(self.sensor_verdict_label, 'muted')
            self.sensor_detail_label.setText('')
            return
        if detected:
            self.sensor_verdict_label.setText('✓ Optical sensor: detected')
            _set_role(self.sensor_verdict_label, 'good')
        else:
            self.sensor_verdict_label.setText('✗ Optical sensor: not detected')
            _set_role(self.sensor_verdict_label, 'bad')
        self.sensor_detail_label.setText(
            'roughness %.2f / repeat %.2f = %.2f (threshold %.2f), raw ADC swing %d'
            % (config.sensor_roughness, config.sensor_repeat, config.sensor_repeat_ratio,
               serial_io.SENSOR_REPEAT_RATIO_THRESHOLD, config.sensor_scan_range))

    def _test_motor(self):
        # RC servos have no feedback wire (see serial_io sensor_self_test); the test
        # just jogs to two clearly different angles and the visible swing is the proof.
        if self.connection is None:
            return
        try:
            self.connection.jog_wheel(self._motor_test_angle)
            self._log('Test: wheel moved to %d degrees.' % self._motor_test_angle)
        except serial_io.SpecError as exc:
            self._log(str(exc), level='error')
            return
        self._motor_test_angle = 150 if self._motor_test_angle == 30 else 30

    def _update_conn_labels(self, text):
        self.conn_status_label.setText(text)
        self.debug_status_label.setText(text)

    def _propagate_connection(self, config=None):
        """Push the connection, and the config we already have, into every tab.

        Passing the config down matters: otherwise each of the six tabs would call
        get_config() itself on connect, which is six extra blocking serial round
        trips for a number the connect worker has already fetched.
        """
        for widget in self._hardware_tabs():
            widget.set_connection(self.connection, config)

    def _set_connected(self, connected):
        """Kept as the name the connect path calls; state lives in _update_controls."""
        self._update_controls()

    def _update_controls(self):
        """Single source of truth for which controls are live.

        Derived in one place from (connected, measuring, repeating, has a reading)
        because the enabled states used to be set from six different methods, which
        is exactly how a control ends up greyed out at the wrong moment.
        """
        # The main tab is built before the Debug tab, and calls this while building.
        if not self._ui_ready:
            return
        connected = self.connection is not None
        measuring = self._measure_worker is not None
        # Continuous mode is idle for the few ms between one update and the next.
        # Counting it as busy stops every control below flickering once per update.
        busy = measuring or self._continuous_running
        idle = connected and not busy

        for button in (self.bt_rad, self.bt_irr, self.bt_motor_test):
            button.setEnabled(idle)
        self.bt_connect.setEnabled(not busy)
        self.bt_continuous_start.setEnabled(idle and not self._repeat_running)
        self.bt_repeat_start.setEnabled(idle and not self._repeat_running)
        # Stop stays live so either loop can be cancelled even mid measurement.
        self.bt_continuous_stop.setEnabled(self._continuous_running)
        self.bt_repeat_stop.setEnabled(self._repeat_running)
        self.bt_save.setEnabled(
            self.reading is not None and not busy and not self._reading_saved)

    def _check_connection_alive(self):
        """Notice an unplug while idle.

        Deliberately does not talk to the device. An idle 'g' would inject
        traffic into a port shared between the measurement worker and five GUI
        thread tabs, take the busy lock, and could desynchronise reply framing.
        An unplug removes the port node, so plain enumeration answers the
        actual question without any I/O to the unit.
        """
        if self.connection is None or self._measure_worker is not None or self._closing:
            return
        try:
            present = self.connection.port in serial_io.list_ports()
        except Exception:
            return  # enumeration itself failed; try again next tick
        if present:
            self._missed_port_scans = 0
            return
        # CH340/CDC clones briefly re enumerate on reset, so require two in a row.
        self._missed_port_scans += 1
        if self._missed_port_scans >= 2:
            self._on_connection_lost('USB device %s is no longer present.'
                                     % self.connection.port)

    def _on_connection_lost(self, reason):
        """Tear down a connection that has gone away. The app used to keep claiming
        it was connected after an unplug, with the measure buttons still live."""
        if self.connection is None:
            return
        try:
            self.connection.close()
        except Exception:
            pass
        self.connection = None
        self._prev_int_time = None
        self._prev_scans = None
        self._heartbeat_timer.stop()
        self._missed_port_scans = 0
        if self._repeat_running:
            self._stop_repeat()
            self._log('Automatic repeat stopped; the OSpRad was disconnected.',
                      level='error')
        self._stop_continuous('the OSpRad was disconnected', level='error')
        self._propagate_connection()
        self._update_controls()
        self.setWindowTitle('OSpRad %s' % __version__)
        self.sensor_verdict_label.setText('? Optical sensor: unknown')
        _set_role(self.sensor_verdict_label, 'muted')
        self.sensor_detail_label.setText('')
        message = '%s Plug the OSpRad back in, then press Reconnect.' % reason
        self._update_conn_labels(message)
        _set_role(self.conn_status_label, 'bad')
        self._log(message, level='error')

    def _set_measure_status(self, text):
        self.measure_status_label.setText(text)
        for widget in self._measure_status_row:
            widget.setVisible(bool(text))
        if text:
            # Only offer Cancel while there is something to cancel.
            self.bt_measure_cancel.setEnabled(
                self._measure_worker is not None and not self._measure_cancelled)

    def _read_measure_settings(self):
        """Parse the measurement settings on the GUI thread.

        Returns (int_time, n_min, n_max, push_time, push_scans) or raises ValueError.
        The old code did these int() calls mid measurement, where a ValueError
        escaped the except clause entirely and took the measurement down with a
        traceback. The _prev_* cache is not updated here. That happens only once
        the commands have actually landed, so a failed push cannot poison it.
        """
        try:
            int_time = int(self.int_time_edit.text())
        except ValueError as exc:
            raise ValueError(
                'integration time must be a whole number of milliseconds') from exc
        if int_time < 0:
            raise ValueError('integration time cannot be negative')
        try:
            n_min = max(1, int(self.min_scans_edit.text()))
            n_max = min(50, int(self.max_scans_edit.text()))
        except ValueError as exc:
            raise ValueError('scan counts must be whole numbers') from exc
        if n_max < n_min:
            n_max = n_min
        return (int_time, n_min, n_max,
                int_time != self._prev_int_time, (n_min, n_max) != self._prev_scans)

    def _measure(self, mode, on_done=None, live=False):
        """Start a measurement on a background thread.

        on_done(ok) fires on the GUI thread when it finishes, however it finishes.
        That continuation is what lets repeat mode chain measurements instead of
        blocking. Measuring used to run on the GUI thread, so the window froze
        for the whole serial round trip with no way to show what it was doing.

        live=True is continuous mode's fast path: the firmware reuses the dark
        reference it already holds. serial_io drops it on firmware that predates it.
        """
        if self._measure_worker is not None:
            return  # already in flight; the buttons are greyed out
        if self.connection is None:
            self._log('Not connected to an OSpRad.', level='error')
            self._finish_measure(False, on_done)
            return
        try:
            settings = self._read_measure_settings()
        except ValueError as exc:
            self._log('Check the measurement settings: %s' % exc, level='error')
            self._finish_measure(False, on_done)
            return

        self._measure_done = on_done
        # Decided here rather than when the reading is published: whether a
        # reading may be saved depends on how its measurement was taken, and by
        # the time it lands the run it belonged to may already have been stopped.
        self._measure_unsaveable = self._continuous_running and self._continuous_held_dark
        self._measure_cancelled = False
        self._measure_label = 'irradiance' if mode == 'i' else 'radiance'
        self._measure_started = time.time()

        worker = Worker(self._do_measure, mode, self.connection, self.store, settings,
                        live)
        worker.succeeded.connect(self._measure_succeeded)
        # Backstop: without it an unexpected exception would leave the buttons grey.
        worker.failed.connect(self._measure_crashed)
        # Assigned before the first status update: Cancel's enabled state keys off
        # it, so drawing the status first left the button dead until the next tick.
        self._measure_worker = worker
        self._update_controls()
        self._update_measure_status()
        self._measure_tick.start()
        worker.start()

    def _do_measure(self, mode, connection, store, settings, live=False):
        """Background thread: hardware I/O plus the maths on its result.

        Returns an outcome rather than raising, because Worker.failed only carries
        str(exc) and item 7 needs to tell a dead USB link from a calibration
        problem. The flux/luminance/chromaticity work is 288 element pure Python
        looping, so it belongs off the GUI thread too.
        """
        int_time, n_min, n_max, push_time, push_scans = settings
        try:
            if push_time:
                connection.set_integration_time(int_time)
            if push_scans:
                connection.set_scan_range(n_min, n_max)
            measurement = connection.measure(mode, live=live)
            calib = store.get(measurement.unit_number)
            flux = calib.to_flux(measurement.raw_counts, mode, measurement.int_time)
            luminance = calib.luminance(flux)
            peak = analysis.peak_wavelength(calib.wavelength, flux)
            fwhm = analysis.fwhm(calib.wavelength, flux)
            chroma = calib.chromaticity(flux)
        except serial_io.SpecTimeoutError:
            return _MeasureOutcome(
                ok=False, mode=mode,
                error='The OSpRad did not finish this measurement within %d minutes.\n\n'
                      'In near darkness the firmware uses its longest exposure, which '
                      'takes several minutes. If the scene really is that dark, set a '
                      'fixed integration time instead of 0 (auto) to bound how long '
                      'a measurement can take.'
                      % (serial_io.MEASURE_TIMEOUT // 60))
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            return _MeasureOutcome(
                ok=False, mode=mode, error=str(exc),
                dead=isinstance(exc, serial_io.SpecTransportError))
        return _MeasureOutcome(
            ok=True, mode=mode, measurement=measurement, calib=calib, flux=flux,
            luminance=luminance, peak=peak, fwhm=fwhm, chroma=chroma,
            int_time=int_time, scans=(n_min, n_max),
            pushed_time=push_time, pushed_scans=push_scans)

    def _measure_succeeded(self, outcome):
        if not outcome.ok:
            # A cancel unblocks the read, which surfaces as a timeout. Reporting
            # that as an error would blame the unit for something the user asked for.
            if not self._measure_cancelled:
                self._log(outcome.error, level='error')
                if outcome.dead:
                    self._on_connection_lost(outcome.error)
            self._finish_measure(False)
            return

        # Only now that the commands have actually landed is the cache valid.
        if outcome.pushed_time:
            self._prev_int_time = outcome.int_time
        if outcome.pushed_scans:
            self._prev_scans = outcome.scans

        measurement, calib = outcome.measurement, outcome.calib
        mode = outcome.mode
        unit = 'lux' if mode == 'i' else 'cd/m\N{SUPERSCRIPT TWO}'
        amount = (f'{outcome.luminance:.3f}' if outcome.luminance > 0.1
                  else f'{outcome.luminance:.3e}')
        # The reading leads; the exposure settings sit in the smaller right hand title.
        title = ('%s   %s %s'
                 % ('Irradiance' if mode == 'i' else 'Radiance', amount, unit))
        subtitle = ('unit #%d   %s   %d ms \N{MULTIPLICATION SIGN} %d scans'
                    % (measurement.unit_number, time.strftime('%H:%M:%S'),
                       measurement.int_time, measurement.n_scans))
        if measurement.saturated:
            # Worth surfacing on the plot: saturated photosites mean the peak is
            # clipped, so the shape and the reading are both suspect.
            subtitle += '   %g saturated' % measurement.saturated
        self.plot.add_curve('live', calib.wavelength, outcome.flux, mode=mode,
                            title=title, subtitle=subtitle, style='live')
        self._show_analysis(outcome)
        # Demoted while continuous mode runs, which would otherwise push every
        # other line out of the 500 line log within a minute.
        self._log('Unit #%d   saturated photosites: %s'
                  % (measurement.unit_number, measurement.saturated),
                  level='debug' if self._continuous_running else 'info')

        self.measurement = measurement
        self.reading = datalog.format_measurement(mode, measurement, outcome.flux,
                                                  outcome.luminance, calib.wavelength)
        if self._measure_unsaveable:
            # Drawn and analysed like any other update, but not offered to Save:
            # the dark under it is of unknown age (see "Hold dark reference" help).
            self.reading = None
        self._last_luminance = outcome.luminance
        self._reading_saved = False
        self._finish_measure(True)

    def _measure_crashed(self, message):
        self._log('Measurement failed unexpectedly: %s' % message, level='error')
        self._finish_measure(False)

    def _cancel_measure(self):
        """Stop waiting for a measurement, and put the link back in a known state.

        Cancelling is not just "ignore the result": the unit carries on scanning
        and will send its DATA line minutes later, which would be read as the reply
        to whatever command came next. Reopening the port resets the Nano and
        aborts the scan, so a reconnect is the resync, and it is quick next to
        the up to 7 minute measurement being abandoned.
        """
        if self._measure_worker is None or self._measure_cancelled:
            return
        self._measure_cancelled = True
        self._park_pending = True
        self.bt_measure_cancel.setEnabled(False)
        self._measure_tick.stop()
        self._set_measure_status('Cancelling...')
        self._log('Cancelling the measurement.', level='warning')
        if self._repeat_running:
            # The chain's continuation is dropped below, so leaving repeat "running"
            # would strand it with nothing scheduled.
            self._stop_repeat()
        # Same for continuous mode, whose next update is chained off this one.
        self._stop_continuous('the update in flight was cancelled')
        if self.connection is not None:
            # Unblocks the worker's read; the reconnect below is what actually stops
            # the unit scanning.
            self.connection.cancel_read()

    def _update_measure_status(self):
        """Tick the elapsed time, and say why a dark scene takes so long.

        "Measuring radiance..." on its own gave no way to tell a working long
        exposure from a hang. In near darkness the firmware really does take
        minutes, moving the shutter wheel for every one of its scans.
        """
        elapsed = int(time.time() - self._measure_started)
        text = 'Measuring %s... %d:%02d' % (self._measure_label, elapsed // 60, elapsed % 60)
        if elapsed >= MEASURE_SLOW_HINT_SECONDS:
            text += ('\nDim light needs long exposures, so this can take several '
                     'minutes. Press Cancel to stop waiting.')
        self._set_measure_status(text)

    def _finish_measure(self, ok, on_done=None):
        """Single exit point for every path: success, device error, crash, cancel."""
        self._measure_worker = None
        self._measure_tick.stop()
        # Left standing between updates in continuous mode: clearing it hides
        # the status row, so the layout jumped once per update and Cancel came
        # and went.
        if not self._continuous_running:
            self._set_measure_status('')
        if self._measure_cancelled:
            self._measure_cancelled = False
            self._log('Measurement cancelled; reconnecting to stop the scan.')
            self._refresh_save_button()
            self._connect()
            # Nothing downstream should treat a cancel as a finished measurement.
            self._measure_done = None
            return
        self._refresh_save_button()
        self._update_controls()
        # Cleared before the call: repeat mode starts the next measurement from
        # inside this callback, and that measurement's continuation must not be
        # overwritten.
        self._park_wheel()
        callback = on_done if on_done is not None else self._measure_done
        self._measure_done = None
        if callback is not None:
            callback(ok)

    def _show_analysis(self, outcome):
        """Render numbers the worker already computed. No maths on the GUI thread."""
        self._analysis_labels['peak'].setText('%.1f nm' % outcome.peak)
        self._analysis_labels['fwhm'].setText(
            ('%.1f nm' % outcome.fwhm) if outcome.fwhm is not None else 'n/a (broadband)')
        if outcome.chroma is not None:
            x, y = outcome.chroma
            self._analysis_labels['cie_x'].setText('%.4f' % x)
            self._analysis_labels['cie_y'].setText('%.4f' % y)
            cct = calibration.cct_from_xy(x, y)
            self._analysis_labels['cct'].setText(('%d K' % round(cct)) if cct else '-')
        else:
            for key in ('cie_x', 'cie_y', 'cct'):
                self._analysis_labels[key].setText('-')

    def _clear_analysis(self):
        for label in self._analysis_labels.values():
            label.setText('-')

    def _set_save_error(self, message, role='bad'):
        self.save_error_label.setText(message)
        _set_role(self.save_error_label, role)
        # Automatic repeat has no Save button to complain about, and its
        # readings are saved for it, so "no reading yet" would be noise there.
        repeat = bool(self._sections) and self._sections['repeat'].isChecked()
        self.save_error_label.setVisible(bool(message) and not repeat)

    def _refresh_save_button(self):
        """Save is only live once there is something to save. Rather than leave a dead
        grey control with no explanation, say what is missing."""
        has_reading = self.reading is not None
        self._update_controls()
        if has_reading:
            if not self._reading_saved:
                self._set_save_error('')
        else:
            self._set_save_error(
                'No reading yet; take a Radiance or Irradiance measurement first.',
                role='muted')

    def _on_save_clicked(self):
        """Validation and prompting live here rather than in _save(), because repeat
        mode calls _save() directly. An unattended run must never stall on a modal,
        and its deliberate label reuse must not trigger the duplicate prompt."""
        if self.reading is None:
            self._set_save_error('No reading to save; take a measurement first.')
            return
        label = self.save_label_edit.text().strip()
        if not label:
            self._set_save_error('Enter a label before saving this reading.')
            self.save_label_edit.setFocus()
            return
        self._set_save_error('')

        if label in self._saved_labels:
            existing = [e.offset for e in datalog.iter_index(DATA_FILE)
                        if e.label.strip() == label]
            if existing and not self._resolve_duplicate(label, existing):
                return
        self._save(label)

    def _resolve_duplicate(self, label, existing):
        """Ask what to do about a label already in the history.

        Returns True to go ahead with the save. "Save anyway" is the default because
        replacing deletes rows, and deleting shifts every later byte offset, the keys
        the History tab identifies readings by.
        """
        box = QMessageBox(self)
        box.setWindowTitle('OSpRad')
        box.setIcon(QMessageBox.Icon.Question)
        box.setText('%d saved reading%s already labelled "%s".'
                    % (len(existing), ' is' if len(existing) == 1 else 's are', label))
        box.setInformativeText('Keep both, or replace the older one?')
        save_btn = box.addButton('Save anyway', QMessageBox.ButtonRole.AcceptRole)
        replace_btn = box.addButton('Replace', QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is replace_btn:
            try:
                datalog.delete_readings(DATA_FILE, existing)
            except OSError as exc:
                self._set_save_error('Could not replace the older reading: %s' % exc)
                return False
            # Deleting rows shifts every later offset, so every cached offset, and
            # every plotted curve keyed by one, is now stale.
            self._clear_history_plot()
            self._load_saved_readings()
            return True
        return clicked is save_btn

    def _save(self, label=None):
        if self.reading is None:
            return
        settings, data, wavelength = self.reading
        if label is None:
            label = self.save_label_edit.text().strip()
        offset = datalog.append_reading(DATA_FILE, label, self.measurement.unit_number,
                                        settings, data, wavelength)
        self._reading_saved = True
        self._update_controls()
        stamp = time.strftime('%Y-%m-%d %H:%M:%S')
        luminance_text = f'{self._last_luminance:.3g}' if self._last_luminance is not None else ''
        item = _ReadingItem([stamp, label or '(unlabelled)', self.measurement.mode,
                             luminance_text])
        item.setData(COL_WHEN, Qt.ItemDataRole.UserRole, offset)
        # The view owns the ordering now, so append rather than forcing this to the top.
        self.saved_tree.addTopLevelItem(item)
        if label:
            self._saved_labels.add(label)
        # The button just went grey with no explanation before.
        self._set_save_error('Saved as "%s".' % (label or '(unlabelled)'), role='muted')
        self._log('Saved reading "%s"' % (label or '(unlabelled)'))

    def _load_saved_readings(self):
        self.saved_tree.clear()
        self._saved_labels = set()
        # Each insert re sorts while sorting is live, so bulk load with it off.
        self.saved_tree.setSortingEnabled(False)
        for entry in datalog.iter_index(DATA_FILE):
            item = _ReadingItem(['%s %s' % (entry.date, entry.time),
                                 entry.label or '(unlabelled)', entry.mode,
                                 f'{entry.luminance:.3g}'])
            item.setData(COL_WHEN, Qt.ItemDataRole.UserRole, entry.offset)
            self.saved_tree.addTopLevelItem(item)
            if entry.label.strip():
                self._saved_labels.add(entry.label.strip())
        self.saved_tree.setSortingEnabled(True)

    def _on_saved_double_click(self, item, column):
        offset = item.data(0, Qt.ItemDataRole.UserRole)
        if offset in self._compared_offsets:
            self._remove_from_comparison(offset)
        else:
            self._add_to_comparison(offset)

    def _add_to_comparison(self, offset):
        try:
            reading = datalog.load_reading(DATA_FILE, offset)
            calib = self.store.get(reading.unit_number)
            calib._derive()  # .wavelength is normally derived as a side effect of
                              # to_flux()/luminance(); this reload path triggers neither.
        except (OSError, ValueError, calibration.CalibrationError) as exc:
            self._log(str(exc), level='error')
            return
        label_text = reading.label or '%s %s' % (reading.date, reading.time)
        # The plot's y axis label reflects only the first curve added (see
        # SpectrumPlot._redraw), so tag each curve's mode in its legend. Radiance
        # and irradiance are different physical units.
        mode_tag = 'radiance' if reading.mode == 'r' else 'irradiance'
        self.history_plot.add_curve(offset, calib.wavelength, reading.flux, mode=reading.mode,
                                    style='overlay', label='%s [%s]' % (label_text, mode_tag))
        self._compared_offsets.add(offset)
        self._log('Comparing reading "%s" (unit #%d)' % (label_text, reading.unit_number))

    def _remove_from_comparison(self, offset):
        self._compared_offsets.discard(offset)
        self.history_plot.remove_curve(offset)

    def _clear_history_plot(self):
        self.history_plot.clear_curves()
        self._compared_offsets.clear()

    # History tab: right click (or, on a touchscreen, long press) menu

    def _on_saved_context_menu(self, pos):
        item = self.saved_tree.itemAt(pos)
        if item is None:
            return
        if item not in self.saved_tree.selectedItems():
            self.saved_tree.setCurrentItem(item)

        menu = self._build_saved_menu()
        menu.exec(self.saved_tree.viewport().mapToGlobal(pos))

    def _build_saved_menu(self):
        """Built separately from _on_saved_context_menu so the contents can be
        checked without opening a modal menu."""
        selection = self.saved_tree.selectedItems()
        single = len(selection) == 1
        menu = QMenu(self)
        show_action = menu.addAction('Show this graph', self._show_only_selected_reading)
        show_action.setEnabled(single)
        menu.addAction('Add to comparison', self._compare_selected_readings)
        rename_action = menu.addAction('Rename...', self._rename_selected_reading)
        rename_action.setEnabled(single)
        menu.addAction('Export...', self._export_selected_readings)
        menu.addSeparator()
        menu.addAction('Delete...', self._delete_selected_readings)
        return menu

    def _show_only_selected_reading(self):
        """Replace whatever is on the comparison plot with just this reading. The
        common case of "show me that one", which otherwise took a Clear then Compare."""
        selection = self.saved_tree.selectedItems()
        if len(selection) != 1:
            return
        self._clear_history_plot()
        self._add_to_comparison(selection[0].data(0, Qt.ItemDataRole.UserRole))

    def _compare_selected_readings(self):
        for item in self.saved_tree.selectedItems():
            offset = item.data(0, Qt.ItemDataRole.UserRole)
            if offset not in self._compared_offsets:
                self._add_to_comparison(offset)

    def _rename_selected_reading(self):
        items = self.saved_tree.selectedItems()
        if len(items) != 1:
            return
        item = items[0]
        offset = item.data(0, Qt.ItemDataRole.UserRole)
        current_label = item.text(1)
        if current_label == '(unlabelled)':
            current_label = ''
        new_label, ok = QInputDialog.getText(self, 'OSpRad', 'Label:', text=current_label)
        if not ok:
            return
        try:
            datalog.rename_reading(DATA_FILE, offset, new_label)
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        # A label of different length shifts every later row's byte offset (see
        # datalog.rename_reading), so any compared curve may now point at the wrong row.
        self._clear_history_plot()
        self._load_saved_readings()
        self._log('Renamed reading to "%s"' % (new_label or '(unlabelled)'))

    def _delete_selected_readings(self):
        items = self.saved_tree.selectedItems()
        if not items:
            return
        offsets = [item.data(0, Qt.ItemDataRole.UserRole) for item in items]
        count = len(offsets)
        reply = QMessageBox.question(
            self, 'OSpRad', 'Delete %d selected reading%s? This cannot be undone.'
            % (count, '' if count == 1 else 's'),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            datalog.delete_readings(DATA_FILE, offsets)
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        # Deleting rows shifts every later row's byte offset. Same as in rename.
        self._clear_history_plot()
        self._load_saved_readings()
        self._log('Deleted %d reading%s' % (count, '' if count == 1 else 's'))

    def _export_selected_readings(self):
        items = self.saved_tree.selectedItems()
        if not items:
            return
        offsets = [item.data(COL_WHEN, Qt.ItemDataRole.UserRole) for item in items]
        path, _ = file_io.ask_save_path(
            self, file_io.default_name('osprad-readings', 'csv'), 'CSV file (*.csv)')
        if not path:
            return
        try:
            file_io.write_text(path, datalog.export_text(DATA_FILE, offsets))
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        self._log('Exported %d reading%s to %s'
                  % (len(offsets), '' if len(offsets) == 1 else 's', path))

    def _save_figure(self, plot=None, stem='osprad-plot'):
        plot = plot or self.plot
        path, selected = file_io.ask_save_path(
            self, file_io.default_name(stem, 'png'),
            'PNG image (*.png);;PDF document (*.pdf)')
        if not path:
            return
        # A content:// URI has no usable suffix, so the chosen filter decides the format.
        fmt = file_io.extension_for(selected, path, default='png')
        if fmt not in ('png', 'pdf'):
            fmt = 'png'
        try:
            plot.save_as_image(path, fmt)
        except (OSError, ValueError) as exc:
            self._log('Could not save the figure: %s' % exc, level='error')
            return
        self._log('Saved figure to %s' % path)

    # Continuous mode is the same chain off the completion callback shape as
    # repeat below, minus the timer and minus the save. It drives a live plot
    # while the OSpRad is being pointed around, rather than recording anything.
    def _start_continuous(self):
        if self._continuous_running or self._repeat_running or self.connection is None:
            return
        if not (self.continuous_irr_check.isChecked()
                or self.continuous_rad_check.isChecked()):
            self._log('Tick Irradiance and/or Radiance under "Measure" before starting '
                      'continuous mode.', level='error')
            return
        self._continuous_running = True
        self._continuous_queue = []
        self._continuous_frames = 0
        self._continuous_slow_hinted = False
        self._continuous_dark_time = None  # first update takes a fresh dark
        self._continuous_held_dark = False
        self._borrow_measure_settings()
        # Force exposure and scan range to be re-sent on the first update even if
        # they have not changed. The firmware drops its cached dark whenever they
        # are set, which is what makes that first update take a fresh one. It must
        # not inherit a dark left over from an earlier run.
        self._prev_int_time = None
        self._prev_scans = None
        self._update_controls()
        self.continuous_status_label.setText('Setting exposure...')
        self._log('Continuous mode started.')
        if not self.connection.supports_live_measure:
            self._log('This unit\'s firmware measures a fresh dark reference for '
                      'every update, so continuous mode refreshes every few seconds '
                      'at best. Flashing %s brings the live update path.'
                      % serial_io.FIRMWARE_HINT, level='warning')
        QTimer.singleShot(0, self._continuous_step)

    def _stop_continuous(self, reason=None, level='info'):
        """Stop after the update in flight. That one is left to finish rather than
        cancelled, because cancelling costs a reconnect (see _cancel_measure) and
        the common case is a user who has seen enough, not one waiting on a stuck scan."""
        if not self._continuous_running:
            return
        self._continuous_running = False
        self._continuous_queue = []
        if self._continuous_held_dark:
            self._log('The dark reference was held for this run, so its readings '
                      'cannot be saved. Take a Radiance or Irradiance measurement for '
                      'one that can.', level='warning')
        self._return_measure_settings()
        self._park_pending = True
        self._park_wheel()  # a no op while the last update is still in flight
        self._update_controls()
        self.continuous_status_label.setText('Not running.')
        # _finish_measure leaves the measurement status standing while continuous
        # mode runs; if nothing is in flight, this is what takes it back down.
        if self._measure_worker is None:
            self._set_measure_status('')
        self._log('Continuous mode stopped%s.' % ('; ' + reason if reason else ''),
                  level=level)

    def _park_wheel(self):
        """Leave the shutter closed once the link is free.

        Every ordinary measurement already ends on the dark position, because its
        last act is the block of dark scans. The two paths that do not are a live
        update (which finishes on the light position so the next one can stay
        there) and a measurement the user abandoned part way through. Both are
        parked here rather than left with the sensor looking at the scene.
        """
        if not self._park_pending or self._measure_worker is not None:
            return  # nothing to do, or the worker still owns the port
        self._park_pending = False
        if self.connection is None:
            return
        try:
            self.connection.park_wheel()
        except serial_io.SpecError as exc:
            # Cosmetic: the next measurement moves the wheel wherever it needs it.
            self._log('Could not park the shutter: %s' % exc, level='debug')

    def _borrow_measure_settings(self):
        """Take over the measurement settings for the run, and say so.

        Written into the widgets rather than kept on the side, so what the hardware
        is being told stays visible, and so _read_measure_settings picks them up
        without a special case. _return_measure_settings puts them back.

        Averaging is the first thing to go: the firmware asks for
        floor(1000 / exposure) scans, capped by the box, so the shipped 3/50 turns
        a 20ms exposure into 50 light scans and 50 dark ones. For a view you
        are watching rather than recording, one scan is the point.
        """
        self._continuous_saved_settings = (self.int_time_edit.text(),
                                           self.min_scans_edit.text(),
                                           self.max_scans_edit.text())
        self.min_scans_edit.setText('1')
        self.max_scans_edit.setText('1')
        # An exposure the user has fixed is theirs, and gets left alone. Auto (0)
        # is the one setting continuous mode cannot live with, so it takes that over.
        self._continuous_manages_exposure = self.int_time_edit.text().strip() == '0'
        if self._continuous_manages_exposure:
            self._log('Continuous mode is choosing the exposure and measuring one '
                      'scan per update; your settings come back when it stops.')
        else:
            self._log('Continuous mode is measuring one scan per update at your fixed '
                      '%s ms exposure; your settings come back when it stops.'
                      % self.int_time_edit.text().strip())

    def _return_measure_settings(self):
        if self._continuous_saved_settings is None:
            return
        int_time, n_min, n_max = self._continuous_saved_settings
        self.int_time_edit.setText(int_time)
        self.min_scans_edit.setText(n_min)
        self.max_scans_edit.setText(n_max)
        self._continuous_saved_settings = None
        self._continuous_manages_exposure = False

    def _continuous_track_exposure(self):
        """Keep the exposure roughly centred as the OSpRad is pointed around.

        Cheaper and steadier than the firmware's own ramp, which starts from 1ms
        and doubles: this starts from an exposure that was right a moment ago, so
        it lands in one step. What it must not do is fidget. The firmware's dark
        cache is keyed on the exposure, so every change costs a full measurement
        with both wheel moves, hence a wide do nothing band rather than a nudge
        per update.
        """
        measurement = self.measurement
        if measurement is None or not self._continuous_manages_exposure:
            return
        peak = max(measurement.raw_counts) if measurement.raw_counts else 0.0
        current = measurement.int_time

        if self.continuous_hold_dark_check.isChecked():
            # A dark is only valid for the exposure it was taken at. The
            # firmware has no room for a second one, so re exposing means re
            # measuring the dark, which means moving the wheel. So holding the
            # dark has to hold the exposure too, or the box would not do what
            # it says. The exposure this update ran at is adopted verbatim.
            if self.int_time_edit.text().strip() == '0':
                self.int_time_edit.setText(str(current))
                self._log('Continuous mode fixed the exposure at %d ms and is '
                          'holding it there, along with the dark reference, so the '
                          'shutter wheel stays still.' % current)
            return
        # The box still says 0, so the firmware ramped for this exposure
        # itself. Its answer is a good starting point, but it has to be
        # written down: left on 0 the firmware would ramp again next update,
        # and the ramp is what stops the dark reference from being reusable.
        on_auto = self.int_time_edit.text().strip() == '0'

        if measurement.saturated:
            # The peak is clipped, so it understates how far over we are and
            # the ratio below would barely move. Back off hard instead, and
            # let the in band test stop it: two steps down beats four.
            wanted = current * 0.25
        elif CONTINUOUS_PEAK_LOW <= peak <= CONTINUOUS_PEAK_HIGH:
            wanted = current  # good enough, and changing it would cost an update
        elif peak > 0:
            wanted = current * CONTINUOUS_TARGET_PEAK / peak
        else:
            wanted = current * CONTINUOUS_EXPOSURE_STEP  # nothing there at all
        wanted = max(current / CONTINUOUS_EXPOSURE_STEP,
                     min(current * CONTINUOUS_EXPOSURE_STEP, wanted))
        new = int(round(max(CONTINUOUS_EXPOSURE_MIN,
                            min(CONTINUOUS_EXPOSURE_MAX, wanted))))
        if new == current and not on_auto:
            return
        self.int_time_edit.setText(str(new))
        if on_auto:
            self._log('Continuous mode fixed the exposure at %d ms. The next '
                      'update takes a dark reference for it, then updates get fast.'
                      % new)
        else:
            self._log('Continuous mode: exposure %d -> %d ms (peak %.0f counts%s).'
                      % (current, new, peak,
                         ', saturated' if measurement.saturated else ''),
                      level='debug')

    def _continuous_exposure_note(self):
        """Warn when a held exposure has stopped fitting the scene.

        Holding the dark holds the exposure (see _continuous_track_exposure), so
        clipping and near darkness no longer correct themselves the way they do
        the rest of the time. Nothing else on screen says so: a clipped spectrum
        just looks like a flat topped one.
        """
        measurement = self.measurement
        if measurement is None or not self.continuous_hold_dark_check.isChecked():
            return ''
        peak = max(measurement.raw_counts) if measurement.raw_counts else 0.0
        if measurement.saturated:
            trouble = 'Saturated'
        elif peak < CONTINUOUS_PEAK_LOW:
            trouble = 'Very dim'
        else:
            return ''
        return ' %s; untick "Hold dark reference" to re expose.' % trouble

    def _continuous_step(self):
        if not self._continuous_running:
            return  # a singleShot that outlived Stop is a harmless no op
        if self.connection is None:
            self._stop_continuous('the OSpRad was disconnected', level='error')
            return
        if not self._continuous_queue:
            # Rebuilt every cycle, so ticking a mode mid run takes effect.
            self._continuous_queue = [
                mode for mode, check in (('i', self.continuous_irr_check),
                                         ('r', self.continuous_rad_check))
                if check.isChecked()]
        if not self._continuous_queue:
            self._stop_continuous('nothing is ticked under "Measure"', level='error')
            return
        # A full measurement re references the dark; every update in between
        # reuses it. Without the periodic refresh the firmware would keep
        # subtracting the dark it took when the run started, which is what
        # "Hold dark reference" deliberately asks for, trading correct
        # readings for a wheel that stays put.
        now = time.time()
        if self._continuous_dark_time is None:
            # First update of the run. The settings pushed at start dropped the
            # firmware's cached dark, so the next live update has to take a
            # fresh one. The dark goes first, finishing on the light position
            # with nothing left to move for the next call.
            live = True
            self._continuous_dark_time = now
        elif now - self._continuous_dark_time < CONTINUOUS_DARK_REFRESH_SECONDS:
            live = True
        elif self.continuous_hold_dark_check.isChecked():
            live = True
            # Only now has a refresh been skipped, so only now are the
            # readings suspect. Ticking and stopping inside the first interval
            # leaves them as good as any other live update.
            self._continuous_held_dark = True
        else:
            live = False
        if not live:
            self._continuous_dark_time = now
        self._continuous_frame_started = now
        self._measure(self._continuous_queue.pop(0),
                      on_done=self._continuous_measure_done, live=live)

    def _continuous_measure_done(self, ok):
        if not self._continuous_running:
            return  # Stop was pressed while that update was in flight
        if not ok:
            # The reason is already in the log. Looping on a unit that just
            # failed would repeat the same failure (and the same error line)
            # forever.
            self._stop_continuous('the last update did not complete', level='warning')
            return
        self._continuous_frames += 1
        elapsed = time.time() - self._continuous_frame_started
        # Before the status line, so it reports the exposure the next update uses.
        self._continuous_track_exposure()
        exposure = self.int_time_edit.text().strip()
        self.continuous_status_label.setText(
            'Running: %d updates, %.1fs each, %s ms exposure.%s'
            % (self._continuous_frames, elapsed, exposure, self._continuous_exposure_note()))
        if (elapsed >= CONTINUOUS_SLOW_SECONDS and not self._continuous_slow_hinted
                and not self._continuous_manages_exposure):
            # Once per run, and only when the exposure is the user's own: when
            # continuous mode picks it, a slow update means the light is genuinely
            # dim and there is no advice to give.
            self._continuous_slow_hinted = True
            self._log('Continuous mode is updating every %.0fs at your fixed %s ms '
                      'exposure. Clear the integration time to 0 to let continuous '
                      'mode choose a faster one.' % (elapsed, exposure))
        QTimer.singleShot(CONTINUOUS_GAP_MS, self._continuous_step)

    def _start_repeat(self):
        if self._repeat_running or self._continuous_running or self.connection is None:
            return
        if not self.repeat_irr_check.isChecked() and not self.repeat_rad_check.isChecked():
            self._log('Tick Irradiance and/or Radiance under "Measure" before starting '
                      'automatic repeat.', level='error')
            return
        try:
            if int(self.repeat_time_edit.text()) < 1:
                raise ValueError
        except ValueError:
            self._log('Repeat interval must be a whole number of seconds.', level='error')
            return
        self._repeat_running = True
        self._repeat_interval_secs = max(1, int(self.repeat_time_edit.text()))
        self._repeat_run += 1
        self._repeat_round = 0
        self._repeat_base = self.save_label_edit.text().strip() or 'auto'
        self._update_controls()
        self._countdown_timer.start()
        self._log('Automatic repeat started.')
        QTimer.singleShot(50, self._repeat_tick)

    def _stop_repeat(self):
        if not self._repeat_running:
            return
        self._repeat_running = False
        self._repeat_next_time = None
        self._repeat_queue = []
        self._countdown_timer.stop()
        self._update_controls()
        self.repeat_status_label.setText('Not running.')
        self._log('Automatic repeat stopped.')

    def _repeat_interval(self):
        """The interval, falling back to the last good value.

        Editing the field to something non numeric mid run used to raise inside a
        timer callback and kill the repeat silently.
        """
        try:
            value = int(self.repeat_time_edit.text())
            if value < 1:
                raise ValueError
        except ValueError:
            self._log('Repeat interval is not a whole number of seconds; keeping %ds.'
                      % self._repeat_interval_secs, level='warning')
            return self._repeat_interval_secs
        self._repeat_interval_secs = value
        return value

    # Repeat is a chain rather than a loop: each measurement now completes on a
    # worker thread, so the next step has to be started from its completion callback.
    def _repeat_tick(self):
        if not self._repeat_running:
            return  # a singleShot that outlived Stop is a harmless no op
        self._repeat_queue = [mode for mode, check in (('i', self.repeat_irr_check),
                                                       ('r', self.repeat_rad_check))
                              if check.isChecked()]
        self._repeat_both_modes = len(self._repeat_queue) > 1
        self._repeat_round += 1
        self._repeat_step()

    def _repeat_step(self):
        if not self._repeat_running:
            return
        if self.connection is None:
            self._stop_repeat()
            self._log('Automatic repeat stopped; the OSpRad was disconnected.',
                      level='error')
            return
        if not self._repeat_queue:
            self._schedule_next_tick()
            return
        self._repeat_mode = self._repeat_queue.pop(0)
        self._measure(self._repeat_mode, on_done=self._repeat_measure_done)

    def _repeat_label(self):
        """Distinct label per repeat reading: every tick used to reuse the one
        label from the box, so the whole run came back indistinguishable in the history.

        "lamp" becomes lamp_1, lamp_2; with both modes ticked the mode is added
        so a pair taken together can be told apart (lamp_i_1, lamp_r_1). An empty
        box uses "auto". A second run in the same session carries its run number,
        so it cannot collide with the first (lamp_2_1, lamp_2_i_1)."""
        parts = [self._repeat_base]
        if self._repeat_run > 1:
            parts.append(str(self._repeat_run))
        if self._repeat_both_modes:
            parts.append(self._repeat_mode)
        parts.append(str(self._repeat_round))
        return '_'.join(parts)

    def _repeat_measure_done(self, ok):
        if not self._repeat_running:
            return  # Stop was pressed while that measurement was in flight
        if ok:
            self._save(self._repeat_label())
        self._repeat_step()

    def _schedule_next_tick(self):
        interval = self._repeat_interval()
        self._repeat_next_time = time.time() + interval
        QTimer.singleShot(interval * 1000, self._repeat_tick)

    def _update_repeat_countdown(self):
        """Driven by one persistent timer. It used to re arm itself and be re invoked
        every tick, leaving one extra 1 second chain alive per tick."""
        if not self._repeat_running or self._repeat_next_time is None:
            return
        remaining = max(0, round(self._repeat_next_time - time.time()))
        mins, secs = divmod(int(remaining), 60)
        self.repeat_status_label.setText('Next measurement in %d:%02d' % (mins, secs))

    def _build_settings_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)

        appearance = QGroupBox('Appearance')
        appearance_layout = QVBoxLayout(appearance)
        self.dark_mode_check = QCheckBox('Dark mode')
        self.dark_mode_check.setChecked(self.dark_mode)
        self.dark_mode_check.toggled.connect(self._toggle_theme)
        appearance_layout.addWidget(self.dark_mode_check)
        layout.addWidget(appearance)

        logging_box = QGroupBox('Logging')
        logging_layout = QHBoxLayout(logging_box)
        level_tip = ('How much detail the Debug tab records. "debug" adds the full '
                     'serial conversation with the OSpRad; the right setting when '
                     'reporting a problem.')
        logging_layout.addWidget(QLabel('Log level'))
        logging_layout.addWidget(help_button(level_tip))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        self.log_level_combo.setCurrentText(self._log_level)
        self.log_level_combo.currentTextChanged.connect(self._on_log_level_changed)
        logging_layout.addWidget(self.log_level_combo)
        logging_layout.addStretch(1)
        layout.addWidget(logging_box)

        connection = QGroupBox('Connection')
        connection_layout = QVBoxLayout(connection)
        port_tip = ('The port to select automatically at startup. "%s" re detects the '
                    'OSpRad each time, which is usually right; pin a port only if '
                    'auto detect keeps finding the wrong device.' % PORT_AUTO)
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel('Preferred port'))
        port_row.addWidget(help_button(port_tip))
        self.settings_port_combo = QComboBox()
        tip(self.settings_port_combo, port_tip)
        self.settings_port_combo.currentTextChanged.connect(self._on_preferred_port_changed)
        port_row.addWidget(self.settings_port_combo, 1)
        refresh = QPushButton('Refresh')
        refresh.clicked.connect(self._refresh_ports)
        port_row.addWidget(refresh)
        connection_layout.addLayout(port_row)
        layout.addWidget(connection)

        measurement = QGroupBox('Measurement')
        measurement_layout = QVBoxLayout(measurement)
        timeout_tip = (
            'How long to wait for one measurement before giving up.\n\n'
            'In near darkness the firmware uses its longest exposure and takes a dark '
            'scan for every light scan, so a single reading can genuinely run for '
            'about six minutes. Below roughly %d seconds such a reading can never '
            'finish. Raise it if you measure very dim scenes; lower it if you would '
            'rather a stuck unit gave up quickly.'
            % serial_io.MEASURE_TIMEOUT)
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(QLabel('Measurement timeout (s)'))
        timeout_row.addWidget(help_button(timeout_tip))
        self.measure_timeout_edit = QLineEdit(str(self._measure_timeout_setting()))
        self.measure_timeout_edit.setFixedWidth(70)
        tip(self.measure_timeout_edit, timeout_tip)
        self.measure_timeout_edit.editingFinished.connect(self._on_measure_timeout_changed)
        timeout_row.addWidget(self.measure_timeout_edit)
        timeout_row.addStretch(1)
        measurement_layout.addLayout(timeout_row)
        self.measure_timeout_note = wrapped_label('')
        _set_role(self.measure_timeout_note, 'muted')
        measurement_layout.addWidget(self.measure_timeout_note)
        layout.addWidget(measurement)

        data = QGroupBox('Data folder')
        data_layout = QVBoxLayout(data)
        # Where readings and calibration land resolves three different ways (source
        # checkout, pip install, read only AppImage), so show which one is in effect.
        data_layout.addWidget(wrapped_label(
            'Readings (data.csv) and calibration data are stored here.'))
        path_row = QHBoxLayout()
        path_edit = QLineEdit(BASE_DIR)
        path_edit.setReadOnly(True)
        path_row.addWidget(path_edit, 1)
        copy_btn = QPushButton('Copy')
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(BASE_DIR))
        path_row.addWidget(copy_btn)
        data_layout.addLayout(path_row)
        layout.addWidget(data)

        footer = wrapped_label('Settings are saved automatically.')
        _set_role(footer, 'muted')
        layout.addWidget(footer)
        layout.addStretch(1)
        return content

    def _on_log_level_changed(self, level):
        self._log_level = level
        _set_setting(SETTING_LOG_LEVEL, level)
        self._refresh_log_level_hint()

    def _refresh_log_level_hint(self):
        self.log_level_hint.setText('Level: %s (change on the Settings tab)'
                                    % self._log_level)

    def _measure_timeout_setting(self):
        value = _get_setting(SETTING_MEASURE_TIMEOUT, serial_io.MEASURE_TIMEOUT, int)
        return max(serial_io.MEASURE_TIMEOUT_MIN,
                   min(serial_io.MEASURE_TIMEOUT_MAX, int(value)))

    def _on_measure_timeout_changed(self):
        """Validate, clamp, persist, and apply to the live connection."""
        text = self.measure_timeout_edit.text().strip()
        try:
            value = int(text)
        except ValueError:
            self.measure_timeout_note.setText(
                'Not a number; keeping %ds.' % self._measure_timeout_setting())
            _set_role(self.measure_timeout_note, 'bad')
            self.measure_timeout_edit.setText(str(self._measure_timeout_setting()))
            return

        clamped = max(serial_io.MEASURE_TIMEOUT_MIN,
                      min(serial_io.MEASURE_TIMEOUT_MAX, value))
        _set_setting(SETTING_MEASURE_TIMEOUT, clamped)
        # Applied live, so a change does not need a reconnect to take effect.
        if self.connection is not None:
            self.connection.measure_timeout = clamped
        self.measure_timeout_edit.setText(str(clamped))
        if clamped != value:
            self.measure_timeout_note.setText(
                'Clamped to %d to %ds.' % (serial_io.MEASURE_TIMEOUT_MIN,
                                           serial_io.MEASURE_TIMEOUT_MAX))
            _set_role(self.measure_timeout_note, 'bad')
        elif clamped < serial_io.MEASURE_TIMEOUT:
            self.measure_timeout_note.setText(
                'Below %ds, a measurement in near darkness cannot finish.'
                % serial_io.MEASURE_TIMEOUT)
            _set_role(self.measure_timeout_note, 'bad')
        else:
            self.measure_timeout_note.setText('')

    def _on_preferred_port_changed(self, port):
        _set_setting(SETTING_PORT, port)
        # Apply straight away so the next Connect uses it without a restart.
        if getattr(self, 'port_combo', None) is not None and \
                self.port_combo.currentText() != port:
            index = self.port_combo.findText(port)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)

    def _install_log_bridge(self):
        """Route the non GUI modules' stdlib logging into the Debug tab's log widget.

        Called after _build_ui so log_text and log_level_combo exist. The handler is
        attached at DEBUG and left there: the Level combo filters inside _log, so
        changing it needs no logger reconfiguration and the stdout/logcat mirror
        always sees everything.
        """
        self._log_bridge = _LogBridge()
        self._log_bridge.message.connect(self._log)
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False  # don't also print through the root logger
        # Idempotent: a second window (or a re run under a test driver) must not
        # double every line.
        for existing in [h for h in logger.handlers if isinstance(h, _QtLogHandler)]:
            logger.removeHandler(existing)
        logger.addHandler(_QtLogHandler(self._log_bridge))

    def _log(self, text, level='info'):
        # Mirror to stdout before the level filter: p4a routes stdout into logcat,
        # which is the only way any of this reaches an Android bug report.
        print('OSpRad[%s] %s' % (level, text), flush=True)

        if LOG_LEVEL_RANK.get(level, 1) < LOG_LEVEL_RANK.get(self._log_level, 1):
            return  # below the level chosen on the Settings tab
        stamp = time.strftime('%H:%M:%S')
        line = '[%s] %s' % (stamp, text)
        # Always an explicit colour, never appendPlainText: a plain append inherits
        # the character format of the previous line, so every info line after an
        # error came out red.
        color = LOG_COLORS.get(level) or ('#fafafa' if self.dark_mode else '#1c1c1c')
        self.log_text.appendHtml('<span style="color:%s">%s</span>'
                                 % (color, html.escape(line)))
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def closeEvent(self, event):
        self._closing = True
        self._stop_repeat()
        self._stop_continuous()
        self._countdown_timer.stop()
        self._measure_tick.stop()
        self._heartbeat_timer.stop()

        # A measurement can block for serial_io's full 90s timeout, and Qt aborts
        # the process if a running QThread is destroyed. Keep the window alive and
        # retry rather than calling wait_for() on the GUI thread, which would look
        # like a hang.
        if self._measure_worker is not None and self._measure_worker.isRunning():
            if self._close_deadline is None:
                self._close_deadline = time.time() + CLOSE_GRACE_SECONDS
            if time.time() < self._close_deadline:
                self._set_measure_status(
                    'Finishing the current measurement before closing...')
                event.ignore()
                QTimer.singleShot(250, self.close)
                return

        # These run hardware I/O on background QThreads; see qt_worker.wait_for for why.
        wait_for(self._measure_worker)
        wait_for(self._connect_worker)
        wait_for(self.monitor_cal_tab.worker)
        if self.connection is not None:
            try:
                self.connection.close()   # never closed before; leaked the port until exit
            except Exception:
                pass
        super().closeEvent(event)


def _app_icon():
    # Embedded PNG so every distribution channel (including a pip install) gets the
    # window icon; see pyproject.toml for why this isn't a packaged data file.
    try:
        from _icon_bundled import PNG_BYTES
    except ImportError:
        return QIcon()
    pixmap = QPixmap()
    pixmap.loadFromData(PNG_BYTES, 'PNG')
    return QIcon(pixmap)


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog='OSpRad')
    parser.add_argument(
        '--port', default=None,
        help='Serial port to connect to, e.g. COM5 or /dev/ttyUSB0. Overrides the '
             'GUI\'s port selector on startup. Default: auto detect (same as the GUI).')
    # parse_known_args so a Qt flag (style, platform, ...) typed ahead of ours
    # doesn't error out.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main():
    args = _parse_args(sys.argv[1:])
    app = QApplication(sys.argv)
    # Must be set before any QSettings is constructed, or the storage location is
    # platform dependent guesswork and settings appear not to persist.
    app.setOrganizationName('OSpRad')
    app.setApplicationName('OSpRad')
    app.setWindowIcon(_app_icon())
    # There is no hovering on a touchscreen, so the tooltips scattered through the
    # app would otherwise be unreachable there. Bound to the app so it outlives this scope.
    app._touch_tooltips = touch.enable_touch_tooltips(app)
    window = OSpRadApp(port=args.port)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
