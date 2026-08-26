# Run this file to launch the app. Requires OSpRad 3.x firmware on the Arduino Nano.

import argparse
import html
import os
import shutil
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QPixmap, QTextCursor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QPushButton, QScrollArea, QScroller, QScrollerProperties,
                               QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from matplotlib.backends.backend_qtagg import NavigationToolbar2QT

import analysis
import calibration
import datalog
import plotting
import serial_io
import touch
from _version import __version__
from calibration_wizard import (CalibrationTransferTab, CosineResponseTab,
                                LinearisationTab, SensitivityTab, UnitSetupTab, tip,
                                wrapped_label)
from monitor_calibration import MonitorCalibrationTab
from qt_worker import Worker, wait_for

# Where calibration_data.csv and data.csv live. Resolved against this file so the app
# behaves the same however it is launched; see pyproject.toml for the py-modules layout
# that motivates the per-user fallback below.
if getattr(sys, 'frozen', False):
    # __file__ points inside the PyInstaller bundle, which onefile deletes on exit.
    BASE_DIR = os.path.dirname(sys.executable)
else:
    _source_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.exists(os.path.join(_source_dir, 'calibration_data.csv')):
        BASE_DIR = _source_dir
    else:
        # pip install: no CSV beside __file__, and site-packages is often not writable.
        if sys.platform == 'win32':
            _user_data_root = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
        elif sys.platform == 'darwin':
            _user_data_root = os.path.expanduser('~/Library/Application Support')
        else:
            _user_data_root = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
        BASE_DIR = os.path.join(_user_data_root, 'OSpRad')
        os.makedirs(BASE_DIR, exist_ok=True)
DATA_FILE = os.path.join(BASE_DIR, 'data.csv')
CALIBRATION_FILE = os.path.join(BASE_DIR, 'calibration_data.csv')

# First run: seed a writable calibration_data.csv from the read-only bundled copy.
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

# Cap on the scrolling log widget so an unattended "Repeat every (s)" session doesn't
# grow unboundedly; QPlainTextEdit trims from the top past this.
# First entry of the port combo; any other entry is a literal port name to connect to.
PORT_AUTO = 'Auto-detect'

LOG_MAX_LINES = 500
LOG_LEVELS = ('debug', 'info', 'warning', 'error')
LOG_LEVEL_RANK = {level: i for i, level in enumerate(LOG_LEVELS)}
LOG_COLORS = {'error': '#d1495b', 'warning': '#e9a23a', 'debug': '#7a7a7a'}

# Hand-rolled replacement for sv_ttk (Tkinter-only); "role" colours match the old styles.
LIGHT_QSS = """
QWidget { background-color: #fafafa; color: #1c1c1c; }
QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox { background-color: #ffffff; }
QLabel[role="muted"] { color: #7a7a7a; }
QLabel[role="good"] { color: #2a9d8f; }
QLabel[role="bad"] { color: #d1495b; }
"""
DARK_QSS = """
QWidget { background-color: #1c1c1c; color: #fafafa; }
QLineEdit, QPlainTextEdit, QTreeWidget, QComboBox { background-color: #2b2b2b; color: #fafafa; }
QLabel[role="muted"] { color: #9a9a9a; }
QLabel[role="good"] { color: #2a9d8f; }
QLabel[role="bad"] { color: #d1495b; }
"""


def _set_role(label, role):
    label.setProperty('role', role)
    label.style().unpolish(label)
    label.style().polish(label)


def _make_scroll_tab(content):
    """Wrap a tab in a QScrollArea (so a tall tab scrolls instead of clipping) with
    QScroller panning so a single-finger touch drag works on phone screens.

    Vertical only. Tabs are laid out to fit the width, so any horizontal movement is
    just drift - which takes both turning the scrollbar off (content is then sized to
    the viewport width) and turning off QScroller's horizontal overshoot, since the
    kinetic scroller rubber-bands sideways even with nothing to scroll to.
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


def _fit_width(widget):
    """Let a widget be squeezed below its natural width instead of forcing the whole tab
    wider than the screen. Used for the matplotlib toolbars, whose row of buttons is
    wider than a phone in portrait."""
    widget.setSizePolicy(QSizePolicy.Policy.Ignored, widget.sizePolicy().verticalPolicy())
    return widget


class OSpRadApp(QMainWindow):
    def __init__(self, port=None):
        super().__init__()
        self.setWindowTitle('OSpRad %s' % __version__)
        # Conservative small-phone-portrait floor; each tab also scrolls if its
        # content is still taller than this (see _make_scroll_tab).
        self.setMinimumSize(320, 480)

        # Pre-selects the port combo built in _build_main_tab; None means auto-detect.
        self._initial_port = port

        self.dark_mode = False
        self.connection = None
        self.store = calibration.CalibrationStore(CALIBRATION_FILE)
        self.measurement = None
        self.reading = None
        self._last_luminance = None
        self._motor_test_angle = 30

        self._repeat_running = False
        self._repeat_next_time = None

        self._prev_int_time = None
        self._prev_scans = None
        self._compared_offsets = set()
        self._connect_worker = None
        self._connect_port = None

        self._build_ui()
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

    def _apply_theme(self):
        QApplication.instance().setStyleSheet(DARK_QSS if self.dark_mode else LIGHT_QSS)
        self.plot.apply_theme(self.dark_mode)
        self.history_plot.apply_theme(self.dark_mode)

    def _toggle_theme(self, checked):
        self.dark_mode = checked
        self._apply_theme()

    def _build_main_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)

        port_row = QHBoxLayout()
        port_row.addWidget(QLabel('Port'))
        self.port_combo = QComboBox()
        tip(self.port_combo,
            'Which serial port to connect to. Auto-detect finds the OSpRad by itself; '
            'only pick one manually if that finds the wrong device.')
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

        self.bt_rad = QPushButton('Radiance')
        self.bt_rad.setMinimumHeight(36)
        self.bt_rad.setEnabled(False)
        self.bt_rad.clicked.connect(lambda: self._measure('r'))
        layout.addWidget(self.bt_rad)

        self.bt_irr = QPushButton('Irradiance')
        self.bt_irr.setMinimumHeight(36)
        self.bt_irr.setEnabled(False)
        self.bt_irr.clicked.connect(lambda: self._measure('i'))
        layout.addWidget(self.bt_irr)

        self.bt_save = QPushButton('Save reading')
        self.bt_save.setMinimumHeight(36)
        self.bt_save.clicked.connect(self._on_save_clicked)
        tip(self.bt_save, 'Save the current reading to the history, under the label below.')
        layout.addWidget(self.bt_save)

        layout.addWidget(QLabel('Label'))
        self.save_label_edit = QLineEdit()
        # Typing a label clears a "needs a label" complaint without needing another press.
        self.save_label_edit.textChanged.connect(lambda _: self._set_save_error(''))
        layout.addWidget(self.save_label_edit)

        # Inline rather than a dialog: a modal on a phone hides the very field it is
        # complaining about, and this sits directly under the control that failed.
        self.save_error_label = wrapped_label('')
        _set_role(self.save_error_label, 'bad')
        self.save_error_label.setVisible(False)
        layout.addWidget(self.save_error_label)

        self._refresh_save_button()

        settings = QGridLayout()
        settings.setColumnStretch(2, 1)

        int_time_label = QLabel('Integration time (ms), 0 = auto')
        self.int_time_edit = QLineEdit('0')
        self.int_time_edit.setFixedWidth(70)
        int_time_tip = ('Per-scan exposure in milliseconds. 0 (default) lets the firmware '
                        'auto-expose just below saturation; set a fixed value only when '
                        'repeated measurements need identical exposure.')
        tip(int_time_label, int_time_tip)
        tip(self.int_time_edit, int_time_tip)
        settings.addWidget(int_time_label, 0, 0)
        settings.addWidget(self.int_time_edit, 0, 1)

        scans_label = QLabel('Scans, min / max')
        tip(scans_label, 'placeholder')  # overwritten below once scans_tip is defined
        scans_row = QHBoxLayout()
        self.min_scans_edit = QLineEdit('3')
        self.min_scans_edit.setFixedWidth(45)
        self.max_scans_edit = QLineEdit('50')
        self.max_scans_edit.setFixedWidth(45)
        scans_row.addWidget(self.min_scans_edit)
        scans_row.addWidget(QLabel('/'))
        scans_row.addWidget(self.max_scans_edit)
        scans_row.addStretch(1)
        scans_tip = ('How many scans the firmware averages into one measurement. The firmware '
                     'picks a value in this range itself - short exposures need more repeats '
                     'to fill ~1s of total sampling time.')
        tip(scans_label, scans_tip)
        tip(self.min_scans_edit, scans_tip)
        tip(self.max_scans_edit, scans_tip)
        settings.addWidget(scans_label, 1, 0)
        settings.addLayout(scans_row, 1, 1)
        layout.addLayout(settings)

        repeat_box = QGroupBox('Automatic repeat')
        repeat_layout = QVBoxLayout(repeat_box)

        repeat_row = QHBoxLayout()
        repeat_row.addWidget(QLabel('Repeat every (s)'))
        self.repeat_time_edit = QLineEdit('300')
        self.repeat_time_edit.setFixedWidth(60)
        tip(self.repeat_time_edit, 'Seconds between automatic measurements; the mode(s) '
                                   'measured each time come from the checkboxes below.')
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

        # Indented under the row above; checkboxes only do anything while repeat is running.
        repeat_modes = QHBoxLayout()
        repeat_modes.setContentsMargins(20, 0, 0, 0)
        measure_label = QLabel('Measure:')
        _set_role(measure_label, 'muted')
        repeat_modes.addWidget(measure_label)
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
        layout.addWidget(self._build_analysis())

        self.cursor_label = QLabel('')
        _set_role(self.cursor_label, 'muted')

        self.plot = plotting.SpectrumPlot(dark=self.dark_mode)
        self.plot.on_hover = lambda text: self.cursor_label.setText(text or '')
        plot_layout = QVBoxLayout()
        toolbar = _fit_width(NavigationToolbar2QT(self.plot.canvas, content))
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(self.cursor_label)
        plot_layout.addWidget(self.plot.canvas, 1)
        layout.addLayout(plot_layout, 1)

        actions = QHBoxLayout()
        self.dark_mode_check = QCheckBox('Dark mode')
        self.dark_mode_check.toggled.connect(self._toggle_theme)
        actions.addWidget(self.dark_mode_check)
        actions.addStretch(1)
        save_fig_btn = QPushButton('Save figure...')
        save_fig_btn.clicked.connect(self._save_figure)
        actions.addWidget(save_fig_btn)
        layout.addLayout(actions)

        return content

    def _build_analysis(self):
        group = QGroupBox('Analysis')
        grid = QGridLayout(group)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)

        tips = {
            'peak': 'Wavelength of the highest intensity. Daylight/white LEDs peak around '
                    '450-550nm; incandescent bulbs peak further into the red, often >600nm.',
            'fwhm': 'Width of the main peak at half height. A few nm = single LED/laser line; '
                    'broad or "n/a" = broadband source like daylight or an incandescent bulb.',
            'cie_x': 'CIE 1931 chromaticity x (perceived colour, brightness-independent). '
                    'Daylight ~ (0.31, 0.33); warm incandescent ~ (0.45, 0.41).',
            'cie_y': 'CIE 1931 chromaticity y - see cie_x above.',
            'cct': 'Approximate "warmth" in Kelvin. ~2700K = warm/orange (incandescent); '
                   '~5000-6500K = cool/blue (daylight). Shows "-" for narrow-band light, '
                   'where CCT is meaningless.',
        }

        self._analysis_labels = {}
        fields = (('peak', 'Peak λ'), ('fwhm', 'FWHM'),
                  ('cie_x', 'CIE x'), ('cie_y', 'CIE y'), ('cct', 'CCT (approx.)'))
        for i, (key, caption) in enumerate(fields):
            r, c = divmod(i, 2)
            caption_label = QLabel(caption + ':')
            _set_role(caption_label, 'muted')
            grid.addWidget(caption_label, r, c * 2)
            value = QLabel('-')
            grid.addWidget(value, r, c * 2 + 1)
            self._analysis_labels[key] = value
            tip(caption_label, tips[key])
            tip(value, tips[key])

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
        self.saved_tree.setHeaderLabels(['Time', 'Label', 'Mode', 'Lux/cd·m²'])
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
            'Double-click (or double-tap) a reading to add it to the comparison plot '
            'below; again to remove it. Right-click - or press and hold on a touchscreen '
            '- for more options, including on a multi-selection.'))

        self.history_plot = plotting.SpectrumPlot(dark=self.dark_mode)
        plot_layout = QVBoxLayout()
        toolbar = _fit_width(NavigationToolbar2QT(self.history_plot.canvas, content))
        plot_layout.addWidget(toolbar)
        plot_layout.addWidget(self.history_plot.canvas, 1)
        layout.addLayout(plot_layout, 1)

        return content

    # Monitor calibration is a downstream USE of an already-calibrated device, blocked by
    # MonitorCalibrationTab._start() until the unit is set up - hence a top-level tab.

    def _build_monitor_cal_tab(self):
        self.monitor_cal_tab = MonitorCalibrationTab(self.connection, self.store)
        return self.monitor_cal_tab

    # ---------------- Calibration tab ----------------

    def _build_calibration_tab(self):
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.addWidget(wrapped_label(
            'One-time setup per unit. Unit number and wheel positions live on the Arduino; '
            'linearisation and spectral sensitivity live in calibration_data.csv.'))

        cal_tabs = QTabWidget()
        self.unit_setup_tab = UnitSetupTab(self.connection)
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
        level_label = QLabel('Level')
        _set_role(level_label, 'muted')
        log_header.addWidget(level_label)
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(LOG_LEVELS)
        self.log_level_combo.setCurrentText('info')
        log_header.addWidget(self.log_level_combo)
        layout.addLayout(log_header)

        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(LOG_MAX_LINES)
        layout.addWidget(self.log_text, 1)

        return content

    def _refresh_ports(self):
        """Repopulate the port combo from a fresh scan, keeping the current selection even
        if this scan doesn't see it - the device may just be momentarily unplugged."""
        keep = self.port_combo.currentText() if self.port_combo.count() else \
            (self._initial_port or PORT_AUTO)
        self.port_combo.blockSignals(True)
        self.port_combo.clear()
        self.port_combo.addItem(PORT_AUTO)
        ports = serial_io.list_ports()
        self.port_combo.addItems(ports)
        if keep != PORT_AUTO and keep not in ports:
            self.port_combo.addItem(keep)
        idx = self.port_combo.findText(keep)
        self.port_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.port_combo.blockSignals(False)

    def _selected_port(self):
        text = self.port_combo.currentText()
        return None if text == PORT_AUTO else text

    def _connect(self):
        # Runs on a background QThread (qt_worker.Worker); blocking the main thread
        # would freeze the UI including the log that's supposed to show progress.
        self.connection = None
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
        self._propagate_connection()
        self._set_connected(True)
        self.bt_connect.setEnabled(True)
        self.bt_connect.setText('Reconnect')
        # Title (not just the log) so unit/firmware stay visible after later log messages.
        self.setWindowTitle('OSpRad %s - unit #%d, firmware v%s'
                            % (__version__, config.unit_number, config.firmware))
        status = ('Connected to unit #%d on %s (firmware v%s)'
                  % (config.unit_number, connection.port, config.firmware))
        self._log(status)
        self._update_conn_labels(status)
        self._update_sensor_status(config)

    def _update_sensor_status(self, config):
        detected = config.sensor_detected
        if detected is None:
            self.sensor_verdict_label.setText(
                '? Optical sensor: not checked (needs firmware 3.2.0 or newer)')
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

    def _propagate_connection(self):
        for widget in (self.unit_setup_tab, self.linearisation_tab, self.sensitivity_tab,
                      self.cosine_tab, self.transfer_tab, self.monitor_cal_tab):
            widget.set_connection(self.connection)

    def _set_connected(self, connected):
        """Grey out hardware-dependent controls while disconnected."""
        for button in (self.bt_rad, self.bt_irr, self.bt_motor_test):
            button.setEnabled(connected)
        # Stop stays enabled so a repeat in progress can still be cancelled mid-run.
        self.bt_repeat_start.setEnabled(connected and not self._repeat_running)
        if not connected:
            self.bt_save.setEnabled(False)

    def _measure(self, mode):
        if self.connection is None:
            self._log('Not connected to an OSpRad.', level='error')
            return
        self._log('-> measure(%r)' % mode, level='debug')
        try:
            self._push_settings()
            measurement = self.connection.measure(mode)
            calib = self.store.get(measurement.unit_number)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self._log(str(exc), level='error')
            return
        self._log('<- n_scans=%d int_time=%dms saturated=%s'
                  % (measurement.n_scans, measurement.int_time, measurement.saturated),
                  level='debug')

        flux = calib.to_flux(measurement.raw_counts, mode, measurement.int_time)
        luminance = calib.luminance(flux)
        unit = 'lux' if mode == 'i' else 'cd/sqm'
        amount = f'{luminance:.3f}' if luminance > 0.1 else f'{luminance:.3e}'

        title = ('%s   Int.: %dms   Scans: %d   %s %s'
                 % ('Irradiance' if mode == 'i' else 'Radiance', measurement.int_time,
                    measurement.n_scans, amount, unit))
        self.plot.add_curve('live', calib.wavelength, flux, mode=mode, title=title, style='live')
        self._update_analysis(calib.wavelength, flux, calib)
        self._log('Unit #%d   saturated photosites: %s'
                  % (measurement.unit_number, measurement.saturated))

        self.measurement = measurement
        self.reading = datalog.format_measurement(mode, measurement, flux, luminance, calib.wavelength)
        self._last_luminance = luminance
        self._refresh_save_button()

    def _update_analysis(self, wavelength, flux, calib):
        peak = analysis.peak_wavelength(wavelength, flux)
        fw = analysis.fwhm(wavelength, flux)
        self._analysis_labels['peak'].setText('%.1f nm' % peak)
        self._analysis_labels['fwhm'].setText(
            ('%.1f nm' % fw) if fw is not None else 'n/a (broadband)')

        chroma = calib.chromaticity(flux)
        if chroma is not None:
            x, y = chroma
            cct = calibration.cct_from_xy(x, y)
            self._analysis_labels['cie_x'].setText('%.4f' % x)
            self._analysis_labels['cie_y'].setText('%.4f' % y)
            self._analysis_labels['cct'].setText(('%.0f K' % cct) if cct is not None else '-')
        else:
            self._analysis_labels['cie_x'].setText('-')
            self._analysis_labels['cie_y'].setText('-')
            self._analysis_labels['cct'].setText('-')

    def _push_settings(self):
        int_time = int(self.int_time_edit.text())
        if int_time != self._prev_int_time:
            self.connection.set_integration_time(int_time)
            self._prev_int_time = int_time
            self._log('-> integration time %dms' % int_time, level='debug')

        n_min = max(1, int(self.min_scans_edit.text()))
        n_max = min(50, int(self.max_scans_edit.text()))
        if n_max < n_min:
            n_max = n_min
        if (n_min, n_max) != self._prev_scans:
            self.connection.set_scan_range(n_min, n_max)
            self._prev_scans = (n_min, n_max)
            self._log('-> scan range %d-%d' % (n_min, n_max), level='debug')

    def _set_save_error(self, message, role='bad'):
        """Inline complaint under the Save button; an empty message hides it."""
        self.save_error_label.setText(message)
        _set_role(self.save_error_label, role)
        self.save_error_label.setVisible(bool(message))

    def _refresh_save_button(self):
        """Save is only live once there is something to save. Rather than leave a dead
        grey control with no explanation, say what is missing."""
        has_reading = self.reading is not None
        self.bt_save.setEnabled(has_reading)
        if has_reading:
            self._set_save_error('')
        else:
            self._set_save_error(
                'No reading yet - take a Radiance or Irradiance measurement first.',
                role='muted')

    def _on_save_clicked(self):
        """Validation lives here rather than in _save() because repeat mode calls _save()
        directly - an unattended session must not stall waiting for someone to type a
        label."""
        if self.reading is None:
            self._set_save_error('No reading to save - take a measurement first.')
            return
        if not self.save_label_edit.text().strip():
            self._set_save_error('Enter a label before saving this reading.')
            self.save_label_edit.setFocus()
            return
        self._set_save_error('')
        self._save()

    def _save(self):
        if self.reading is None:
            return
        settings, data, wavelength = self.reading
        label = self.save_label_edit.text()
        offset = datalog.append_reading(DATA_FILE, label, self.measurement.unit_number,
                                        settings, data, wavelength)
        self.bt_save.setEnabled(False)
        stamp = time.strftime('%H:%M:%S')
        luminance_text = f'{self._last_luminance:.3g}' if self._last_luminance is not None else ''
        item = QTreeWidgetItem([stamp, label or '(unlabelled)', self.measurement.mode, luminance_text])
        item.setData(0, Qt.ItemDataRole.UserRole, offset)
        self.saved_tree.insertTopLevelItem(0, item)
        self._log('Saved reading "%s"' % (label or '(unlabelled)'))

    def _load_saved_readings(self):
        self.saved_tree.clear()
        for entry in datalog.iter_index(DATA_FILE):
            item = QTreeWidgetItem([entry.time, entry.label or '(unlabelled)', entry.mode,
                                    f'{entry.luminance:.3g}'])
            item.setData(0, Qt.ItemDataRole.UserRole, entry.offset)
            self.saved_tree.addTopLevelItem(item)

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
        # The plot's y-axis label reflects only the first curve added (see
        # SpectrumPlot._redraw), so tag each curve's mode in its legend - radiance
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

    # History tab: right-click (or, on a touchscreen, long-press) menu

    def _on_saved_context_menu(self, pos):
        item = self.saved_tree.itemAt(pos)
        if item is None:
            return
        if item not in self.saved_tree.selectedItems():
            self.saved_tree.setCurrentItem(item)

        menu = self._build_saved_menu()
        menu.exec(self.saved_tree.viewport().mapToGlobal(pos))

    def _build_saved_menu(self):
        """Built separately from _on_saved_context_menu so the contents can be checked
        without opening a modal menu."""
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
        """Replace whatever is on the comparison plot with just this reading - the
        common case of "show me that one", which otherwise took a Clear then a Compare."""
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
        # Deleting rows shifts every later row's byte offset - same as in rename.
        self._clear_history_plot()
        self._load_saved_readings()
        self._log('Deleted %d reading%s' % (count, '' if count == 1 else 's'))

    def _export_selected_readings(self):
        items = self.saved_tree.selectedItems()
        if not items:
            return
        offsets = [item.data(0, Qt.ItemDataRole.UserRole) for item in items]
        path, _ = QFileDialog.getSaveFileName(
            self, 'OSpRad', os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), ''),
            'CSV file (*.csv)')
        if not path:
            return
        try:
            datalog.export_readings(DATA_FILE, offsets, path)
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        self._log('Exported %d reading%s to %s'
                  % (len(offsets), '' if len(offsets) == 1 else 's', path))

    def _save_figure(self):
        path, _ = QFileDialog.getSaveFileName(
            self, 'OSpRad', os.path.join(os.path.dirname(os.path.abspath(DATA_FILE)), ''),
            'PNG image (*.png);;PDF document (*.pdf)')
        if path:
            self.plot.save_as_image(path)

    def _start_repeat(self):
        if self._repeat_running or self.connection is None:
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
        self.bt_repeat_start.setEnabled(False)
        self.bt_repeat_stop.setEnabled(True)
        self._log('Automatic repeat started.')
        QTimer.singleShot(50, self._repeat_tick)

    def _stop_repeat(self):
        if not self._repeat_running:
            return
        self._repeat_running = False
        self._repeat_next_time = None
        self.bt_repeat_start.setEnabled(self.connection is not None)
        self.bt_repeat_stop.setEnabled(False)
        self.repeat_status_label.setText('Not running.')
        self._log('Automatic repeat stopped.')

    def _repeat_tick(self):
        # Every continuation below is guarded by _repeat_running, so a singleShot that
        # still fires after Stop was pressed is a harmless no-op.
        if not self._repeat_running:
            return
        if self.repeat_irr_check.isChecked():
            self._measure('i')
            self._save()
        if self.repeat_rad_check.isChecked():
            self._measure('r')
            self._save()
        if not self._repeat_running:
            return  # Stop may have fired during the _measure()/_save() calls above
        interval = max(1, int(self.repeat_time_edit.text()))
        self._repeat_next_time = time.time() + interval
        QTimer.singleShot(interval * 1000, self._repeat_tick)
        self._update_repeat_countdown()

    def _update_repeat_countdown(self):
        if not self._repeat_running or self._repeat_next_time is None:
            return
        remaining = max(0, round(self._repeat_next_time - time.time()))
        mins, secs = divmod(int(remaining), 60)
        self.repeat_status_label.setText('Next measurement in %d:%02d' % (mins, secs))
        QTimer.singleShot(1000, self._update_repeat_countdown)

    def _log(self, text, level='info'):
        # Mirror to stdout before the level filter: p4a routes stdout into logcat, which
        # is the only way any of this reaches an Android bug report.
        print('OSpRad[%s] %s' % (level, text), flush=True)

        if LOG_LEVEL_RANK.get(level, 1) < LOG_LEVEL_RANK.get(self.log_level_combo.currentText(), 1):
            return  # below the Debug tab's Level selector
        stamp = time.strftime('%H:%M:%S')
        line = '[%s] %s' % (stamp, text)
        color = LOG_COLORS.get(level)
        if color:
            self.log_text.appendHtml('<span style="color:%s">%s</span>' % (color, html.escape(line)))
        else:
            self.log_text.appendPlainText(line)
        cursor = self.log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log_text.setTextCursor(cursor)
        self.log_text.ensureCursorVisible()

    def closeEvent(self, event):
        # Both run hardware I/O on background QThreads; see qt_worker.wait_for for why.
        wait_for(self._connect_worker)
        wait_for(self.monitor_cal_tab.worker)
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
        help='Serial port to connect to, e.g. COM5 or /dev/ttyUSB0. Overrides the GUI\'s '
             'port selector on startup. Default: auto-detect (same as the GUI).')
    # parse_known_args so a Qt flag (-style, -platform, ...) ahead of ours doesn't error out.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main():
    args = _parse_args(sys.argv[1:])
    app = QApplication(sys.argv)
    app.setWindowIcon(_app_icon())
    # There is no hovering on a touchscreen, so the tooltips scattered through the app
    # would otherwise be unreachable there. Bound to the app so it outlives this scope.
    app._touch_tooltips = touch.enable_touch_tooltips(app)
    window = OSpRadApp(port=args.port)
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
