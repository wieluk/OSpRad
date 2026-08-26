# Monitor calibration: steps a fullscreen patch through each R/G/B channel at a ladder
# of levels, measures the spectrum at each step, and exports a plain CSV or a fitted
# PsychCal .mat.
#
# Psychtoolbox: the .mat loads directly via `cal = LoadCalFile(...)` (no MATLAB-side
# step, matching CalibrateMonSpd). The linear device model (P_device/T_device/raw
# gamma weights) ports PTB's CalibrateFitLinMod/FindModelWeights exactly. The tone
# curve is a monotone PCHIP, NOT a port of PTB's CalibrateFitGamma: that algorithm
# wasn't confirmable from source, and a partial reimplementation risked a
# plausible-but-wrong curve. cal.describe.gamma.fitType says 'OSpRad-pchip' so the
# difference is visible rather than implied away. T_device/T_ambient use OSpRad's own
# analytic CIE 1931 approximation, close to but not bit-identical with PTB's tables.

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QVBoxLayout, QWidget)

import numpy as np

import calibration
import plotting
import serial_io
from calibration_wizard import WHEEL_MAX_ANGLE, WHEEL_MIN_ANGLE, tip, wrapped_label
from qt_worker import Worker

# Default Psychtoolbox wavelength sampling spec S = [startWL, deltaWL, numWL], matching
# CalibrateMonSpd's own common default (380:4:780nm, 101 samples).
DEFAULT_PTB_S = (380, 4, 101)


def resample_to_ptb_grid(wavelength, flux, s_spec):
    """Linearly interpolate the sensor's uneven wavelength axis onto PTB's uniform
    S = [start, delta, n] grid; edges outside the sensor's range fill with 0."""
    start, delta, n = s_spec
    grid = start + delta * np.arange(n)
    resampled = np.interp(grid, wavelength, flux, left=0.0, right=0.0)
    return grid, resampled


def fit_linear_device_model(mon_by_channel):
    """Port PTB's CalibrateFitLinMod for the single-primary-basis case (cal.nPrimaryBases=1):
    P_device is each channel's spectrum at its highest level; raw_gamma weights are
    the least-squares projection of each level's spectrum onto that basis (`lstsq`)."""
    n_channels = len(mon_by_channel)
    n_meas = len(mon_by_channel[0])
    s3 = len(mon_by_channel[0][0])
    p_device = np.zeros((s3, n_channels))
    raw_gamma = np.zeros((n_meas, n_channels))
    for ch in range(n_channels):
        levels = np.stack(mon_by_channel[ch], axis=1)  # [S3, nMeas]
        basis = levels[:, -1]
        p_device[:, ch] = basis
        denom = float(basis @ basis)
        raw_gamma[:, ch] = (basis @ levels) / denom if denom > 0 else 0.0
    return p_device, raw_gamma


def _pchip_sign(v):
    if v > 0:
        return 1.0
    if v < 0:
        return -1.0
    return 0.0


def _pchip_end_slope(h0, h1, m0, m1):
    """One-sided three-point derivative estimate, clipped to preserve monotonicity -
    matches scipy.interpolate.PchipInterpolator's edge-case handling exactly."""
    d = ((2 * h0 + h1) * m0 - h0 * m1) / (h0 + h1)
    if _pchip_sign(d) != _pchip_sign(m0):
        return 0.0
    if _pchip_sign(m0) != _pchip_sign(m1) and abs(d) > 3.0 * abs(m0):
        return 3.0 * m0
    return d


def _pchip_slopes(x, y):
    n = len(x)
    h = np.diff(x)
    delta = np.diff(y) / h
    d = np.zeros(n)
    if n == 2:
        d[:] = delta[0]
        return d
    for i in range(1, n - 1):
        if delta[i - 1] * delta[i] <= 0 or _pchip_sign(delta[i - 1]) != _pchip_sign(delta[i]):
            d[i] = 0.0
        else:
            w1 = 2 * h[i] + h[i - 1]
            w2 = h[i] + 2 * h[i - 1]
            d[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])
    d[0] = _pchip_end_slope(h[0], h[1], delta[0], delta[1])
    d[-1] = _pchip_end_slope(h[-1], h[-2], delta[-1], delta[-2])
    return d


class MonotonePCHIP:
    """Fritsch-Carlson monotone cubic Hermite interpolation, hand-rolled because scipy
    has no Android build. Just enough for fit_gamma_curve()'s increasing-x,
    non-decreasing-y input; returns NaN outside the input range."""

    def __init__(self, x, y):
        self.x = np.asarray(x, dtype=float)
        self.y = np.asarray(y, dtype=float)
        self.d = _pchip_slopes(self.x, self.y)

    def __call__(self, xi):
        xi = np.asarray(xi, dtype=float)
        idx = np.clip(np.searchsorted(self.x, xi) - 1, 0, len(self.x) - 2)
        x0, x1 = self.x[idx], self.x[idx + 1]
        y0, y1 = self.y[idx], self.y[idx + 1]
        d0, d1 = self.d[idx], self.d[idx + 1]
        h = x1 - x0
        t = (xi - x0) / h
        t2, t3 = t * t, t * t * t
        h00 = 2 * t3 - 3 * t2 + 1
        h10 = t3 - 2 * t2 + t
        h01 = -2 * t3 + 3 * t2
        h11 = t3 - t2
        result = h00 * y0 + h10 * h * d0 + h01 * y1 + h11 * h * d1
        return np.where((xi < self.x[0]) | (xi > self.x[-1]), np.nan, result)


def fit_gamma_curve(gamma_input, raw_gamma, n_output=1024):
    """Monotone PCHIP interpolation through the measured (input, raw-gamma) points per
    channel (see module docstring for why this isn't PTB's CalibrateFitGamma). Values
    forced non-decreasing before fitting; anchored at (0, 0) since level 0 is covered
    by the separate ambient measurement."""
    gamma_output = np.linspace(0.0, 1.0, n_output)
    n_channels = raw_gamma.shape[1]
    table = np.zeros((n_output, n_channels))
    for ch in range(n_channels):
        x = np.concatenate([[0.0], gamma_input])
        y = np.concatenate([[0.0], np.clip(raw_gamma[:, ch], 0.0, None)])
        y = np.maximum.accumulate(y)
        if y[-1] > 0:
            y = y / y[-1]
        interp = MonotonePCHIP(x, y)
        fitted = interp(gamma_output)
        fitted = np.nan_to_num(fitted, nan=0.0)
        table[:, ch] = np.clip(fitted, 0.0, 1.0)
    return gamma_output.reshape(-1, 1), table


def build_t_device(wavelength_grid):
    """CIE 1931 XYZ colour-matching functions at wavelength_grid, as PTB's T_device/
    T_ambient. Reuses calibration.py's analytic piecewise-Gaussian approximation -
    close to PTB's tabulated data but not bit-identical."""
    x = np.array([calibration._piecewise_gaussian(w, calibration.CIE_X_COEFS)
                 for w in wavelength_grid])
    y = np.array([calibration._piecewise_gaussian(w, calibration.CIE_Y_COEFS)
                 for w in wavelength_grid])
    z = np.array([calibration._piecewise_gaussian(w, calibration.CIE_Z_COEFS)
                 for w in wavelength_grid])
    return np.vstack([x, y, z])


class PatchWindow(QWidget):
    """Borderless fullscreen solid-colour window for the sweep. Status text is drawn
    over a small dark backing panel so it stays readable on any patch colour."""

    def __init__(self, on_cancel):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self._on_cancel = on_cancel

        self.status_label = QLabel(self)
        self.status_label.setStyleSheet(
            'color: white; background-color: rgba(0, 0, 0, 180); padding: 6px; font-size: 12pt;')
        self.status_label.move(10, 10)

        self.set_color(0, 0, 0)
        self.showFullScreen()
        self.setFocus()

    def set_color(self, r, g, b):
        self.setStyleSheet('background-color: rgb(%d, %d, %d);' % (r, g, b))

    def set_status(self, text):
        self.status_label.setText(text)
        self.status_label.adjustSize()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
        else:
            super().keyPressEvent(event)


class MonitorCalibrationTab(QWidget):
    """Sweeps a fullscreen patch through R/G/B levels, measuring the display's
    spectrum (Radiance mode - narrow, aimable FOV pointed at the screen) at each
    step. Exports a plain CSV or a Psychtoolbox-ready calibration .mat; see the
    module docstring for the .mat export choices."""

    CHANNEL_NAMES = ('Red', 'Green', 'Blue')
    CHANNEL_COLORS = ('#d1495b', '#2a9d8f', '#2e86ab')

    def __init__(self, connection, store):
        super().__init__()
        self.connection = connection
        self.store = store
        self.comment = ''  # no UI control yet; carried through to the .mat export as-is
        self._cancelled = False
        self._patch_window = None
        self._result = None  # set on a completed sweep; consumed by the Export buttons
        self.worker = None

        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(
            'Point the OSpRad at the screen (Radiance mode - narrow FOV, so aim it at '
            'roughly where stimuli will appear) and press Start. A fullscreen patch '
            'steps through black, then each of Red/Green/Blue at a ladder of levels, '
            "measuring the spectrum at every step - same workflow as CalibrateMonSpd. "
            'Takes a few minutes; each step needs a moment to auto-expose (longer for dim levels).'))

        settings = QHBoxLayout()
        levels_label = QLabel('Levels per channel')
        settings.addWidget(levels_label)
        self.n_levels_edit = QLineEdit('11')
        self.n_levels_edit.setFixedWidth(50)
        levels_tip = ('Brightness steps per channel (evenly spaced from just above '
                      'black to full). More = better gamma fit but proportionally longer.')
        tip(levels_label, levels_tip)
        tip(self.n_levels_edit, levels_tip)
        settings.addWidget(self.n_levels_edit)
        settings.addSpacing(16)
        settle_label = QLabel('Settle time (ms)')
        settings.addWidget(settle_label)
        self.settle_ms_edit = QLineEdit('300')
        self.settle_ms_edit.setFixedWidth(50)
        settle_tip = ('Pause after each patch colour change, before measuring, so the '
                      'display has finished redrawing. Raise if your monitor/compositor '
                      'is slow to settle.')
        tip(settle_label, settle_tip)
        tip(self.settle_ms_edit, settle_tip)
        settings.addWidget(self.settle_ms_edit)
        settings.addStretch(1)
        layout.addLayout(settings)

        btn_row = QHBoxLayout()
        self.start_button = QPushButton('Start')
        self.start_button.clicked.connect(self._start)
        btn_row.addWidget(self.start_button)
        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel)
        btn_row.addWidget(self.cancel_button)
        btn_row.addSpacing(16)
        self.export_csv_button = QPushButton('Export CSV...')
        self.export_csv_button.setEnabled(False)
        self.export_csv_button.clicked.connect(self._export_csv)
        btn_row.addWidget(self.export_csv_button)
        self.export_ptb_button = QPushButton('Export for Psychtoolbox...')
        self.export_ptb_button.setEnabled(False)
        self.export_ptb_button.clicked.connect(self._export_ptb)
        tip(self.export_ptb_button, (
            'Writes a fitted PsychCal .mat file; load directly with '
            'cal = LoadCalFile(...) in Psychtoolbox (no MATLAB-side step). '
            'See the module docstring for the linear-model and tone-curve choices.'))
        btn_row.addWidget(self.export_ptb_button)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.status = wrapped_label('')
        layout.addWidget(self.status)

        self.plot = plotting.SpectrumPlot()
        layout.addWidget(self.plot.canvas, 1)

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        self.start_button.setEnabled(connection is not None)
        if not connection:
            self.status.setText('Not connected.')

    # sweep

    def _start(self):
        try:
            n_levels = int(self.n_levels_edit.text())
            settle_ms = max(0, int(self.settle_ms_edit.text()))
            if n_levels < 2:
                raise ValueError
        except ValueError:
            self.status.setText('Levels per channel must be a whole number >= 2.')
            return

        try:
            config = self.connection.get_config()
        except serial_io.SpecError as exc:
            self.status.setText(str(exc))
            return

        # Downstream USE of an already-calibrated device; block outright (not warn)
        # until both preconditions below hold. If Dark and Radiance resolve to the
        # same (or any unsaved) physical angle, every reading is dark-minus-itself.
        angles = (config.dark, config.irr, config.rad)
        if any(a < WHEEL_MIN_ANGLE or a > WHEEL_MAX_ANGLE for a in angles):
            QMessageBox.critical(self, 'OSpRad', (
                "This unit's shutter wheel positions haven't all been saved yet. Finish "
                'Calibration -> Unit & wheel setup first - without a real Dark position, '
                'the dark-frame subtraction has nothing meaningful to subtract, so every '
                'measurement here would be near-zero noise, not a real spectrum.'))
            return

        try:
            self.store.get(config.unit_number)
        except calibration.CalibrationError:
            QMessageBox.critical(self, 'OSpRad', (
                'Unit #%d has no wavelength/sensitivity/linearisation calibration in '
                '%s yet. Finish device calibration (Calibration tab) before measuring a '
                'monitor with it.' % (config.unit_number, self.store.path)))
            return

        self._cancelled = False
        self._settle_ms = settle_ms
        self._n_levels = n_levels
        self._sweep_steps = self._build_step_list(n_levels)
        self._sweep_index = 0
        self._sweep_results = {'ambient': None, 'mon_by_channel': [[], [], []]}
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.export_csv_button.setEnabled(False)
        self.export_ptb_button.setEnabled(False)
        self._result = None

        self.connection.set_integration_time(0)  # let each step auto-expose
        self._patch_window = PatchWindow(on_cancel=self._cancel)
        self._advance_sweep()

    def _build_step_list(self, n_levels):
        """('ambient' | channel-index, (r,g,b), status text) per step. Gamma levels
        exclude 0 (linspace(0,1,n+1)[1:]), matching PTB's rawGammaInput convention -
        black is covered once, separately, by the ambient step."""
        steps = [('ambient', (0, 0, 0), 'Measuring ambient (black screen)...')]
        gamma_input = np.linspace(0.0, 1.0, n_levels + 1)[1:]
        levels_255 = [int(round(f * 255)) for f in gamma_input]
        total = n_levels * 3
        step_num = 0
        for ch in range(3):
            for level in levels_255:
                step_num += 1
                rgb = [0, 0, 0]
                rgb[ch] = level
                text = ('Measuring %s %d/255 (step %d/%d)...'
                       % (self.CHANNEL_NAMES[ch], level, step_num, total))
                steps.append((ch, tuple(rgb), text))
        return steps

    def _cancel(self):
        if self._cancelled:
            return
        self._cancelled = True
        self.status.setText('Cancelling - finishing current measurement...')

    def _advance_sweep(self):
        """Only waits via QTimer.singleShot() (never blocks), so the event loop stays
        responsive to Escape/Cancel for the whole sweep. Worst case: Cancel takes
        effect after the one measurement already in flight (a blocking serial call
        can't be safely interrupted mid-read)."""
        if self._cancelled:
            self._finish_sweep(outcome='cancelled')
            return
        if self._sweep_index >= len(self._sweep_steps):
            self._finish_sweep(outcome='done')
            return
        kind, rgb, text = self._sweep_steps[self._sweep_index]
        self._patch_window.set_color(*rgb)
        self._patch_window.set_status('%s\n%s' % (text, rgb))
        self.status.setText(text)
        QTimer.singleShot(self._settle_ms, lambda: self._start_measurement(kind))

    def _start_measurement(self, kind):
        if self._cancelled:
            self._finish_sweep(outcome='cancelled')
            return
        self.worker = Worker(self._measure_and_resample, kind, self.connection, self.store)
        self.worker.succeeded.connect(self._measurement_done)
        self.worker.failed.connect(self._measurement_failed)
        self.worker.start()

    @staticmethod
    def _measure_and_resample(kind, connection, store):
        """Run on a background thread (qt_worker.Worker): hardware I/O and calibration
        math only; Qt widgets are not thread-safe."""
        measurement = connection.measure('r')
        calib = store.get(measurement.unit_number)
        flux = calib.to_flux(measurement.raw_counts, 'r', measurement.int_time)
        _, resampled = resample_to_ptb_grid(calib.wavelength, flux, DEFAULT_PTB_S)
        return kind, resampled

    def _measurement_done(self, result):
        kind, spd = result
        if kind == 'ambient':
            self._sweep_results['ambient'] = spd
        else:
            self._sweep_results['mon_by_channel'][kind].append(spd)
        self._sweep_index += 1
        self._advance_sweep()

    def _measurement_failed(self, message):
        self.status.setText(message)
        self._cancelled = True
        self._finish_sweep(outcome='failed')

    def _finish_sweep(self, outcome):
        if self._patch_window is not None:
            self._patch_window.close()
            self._patch_window = None
        if self.connection is not None:
            self.connection.set_integration_time(0)
        self.start_button.setEnabled(self.connection is not None)
        self.cancel_button.setEnabled(False)

        if outcome == 'failed':
            return  # status already set by _measurement_failed
        if outcome == 'cancelled':
            self.status.setText('Cancelled.')
            return

        ambient = self._sweep_results['ambient']
        mon_by_channel = self._sweep_results['mon_by_channel']
        gamma_input = np.linspace(0.0, 1.0, self._n_levels + 1)[1:]
        self._result = {
            's_spec': DEFAULT_PTB_S,
            'gamma_input': gamma_input,
            'mon_by_channel': mon_by_channel,
            'ambient': ambient,
            'n_levels': self._n_levels,
        }
        total_steps = len(self._sweep_steps)
        self.status.setText('Done - %d measurements. Export below, or run another sweep.'
                            % total_steps)
        self.export_csv_button.setEnabled(True)
        self.export_ptb_button.setEnabled(True)
        self._show_preview()

    # preview

    def _show_preview(self):
        grid, _ = resample_to_ptb_grid([0], [0], self._result['s_spec'])
        gamma_input = self._result['gamma_input']
        levels_255 = gamma_input * 255

        ax = self.plot.ax
        ax.clear()
        self.plot._style_axes()
        ax.set_xlabel('Input level (0-255)')
        ax.set_ylabel('Relative luminance (sum of resampled spectrum)')
        ax.set_title('Gamma sweep preview - should rise smoothly per channel', fontsize=10)
        for ch in range(3):
            totals = [float(np.sum(spd)) for spd in self._result['mon_by_channel'][ch]]
            ax.plot(levels_255, totals, color=self.CHANNEL_COLORS[ch], marker='o',
                   markersize=3, linewidth=1.2, label=self.CHANNEL_NAMES[ch])
        ax.legend(fontsize=9)
        self.plot.canvas.draw()

    # export

    def _export_csv(self):
        if self._result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'OSpRad', 'monitor_calibration.csv', 'CSV (*.csv)')
        if not path:
            return
        start, delta, n = self._result['s_spec']
        grid = start + delta * np.arange(n)
        with open(path, 'w') as handle:
            handle.write('# channel,level_255,' + ','.join('%.1f' % w for w in grid) + '\n')
            handle.write('ambient,0,' + ','.join('%.6g' % v for v in self._result['ambient'])
                        + '\n')
            for ch, name in enumerate(self.CHANNEL_NAMES):
                levels_255 = self._result['gamma_input'] * 255
                for level, spd in zip(levels_255, self._result['mon_by_channel'][ch]):
                    handle.write('%s,%.1f,' % (name, level)
                                + ','.join('%.6g' % v for v in spd) + '\n')
        self.status.setText('Saved %s' % path)

    def _export_ptb(self):
        if self._result is None:
            return
        try:
            import scipy.io as sio
        except ImportError:
            QMessageBox.critical(self, 'OSpRad', 'scipy is required for the .mat export.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'OSpRad', 'monitor_calibration.mat', 'MATLAB file (*.mat)')
        if not path:
            return

        s_spec = self._result['s_spec']
        s3 = s_spec[2]
        n_levels = self._result['n_levels']
        wavelength_grid = s_spec[0] + s_spec[1] * np.arange(s3)
        mon_by_channel = self._result['mon_by_channel']

        mon = np.zeros((s3 * n_levels, 3))
        for ch in range(3):
            for i, spd in enumerate(mon_by_channel[ch]):
                mon[i * s3:(i + 1) * s3, ch] = spd

        p_device, raw_gamma = fit_linear_device_model(mon_by_channel)
        gamma_input = self._result['gamma_input']
        gamma_output, gamma_table = fit_gamma_curve(gamma_input, raw_gamma)
        t_device = build_t_device(wavelength_grid)

        cal = {
            'describe': {
                'S': np.array(s_spec, dtype=float).reshape(1, 3),
                'caltype': 'monitor',
        'program': 'OSpRad MonitorCalibrationTab (fit: linear device model ported from '
                  'PTB CalibrateFitLinMod; tone curve: monotone PCHIP, not PTB '
                  "CalibrateFitGamma - see monitor_calibration.py module docstring)",
                'date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'comment': self.comment,
                'nAverage': 1.0,
                'nMeas': float(n_levels),
                'gamma': {'fitType': 'OSpRad-pchip'},
            },
            'nDevices': 3.0,
            'nPrimaryBases': 1.0,
            'rawdata': {
                'mon': mon,
                'rawGammaInput': gamma_input.reshape(-1, 1),
                'rawGammaTable': raw_gamma,
            },
            'S_device': np.array(s_spec, dtype=float).reshape(1, 3),
            'P_device': p_device,
            'T_device': t_device,
            'gammaInput': gamma_output,
            'gammaTable': gamma_table,
            'gammaFormat': 0.0,
            'P_ambient': np.asarray(self._result['ambient'], dtype=float).reshape(-1, 1),
            'S_ambient': np.array(s_spec, dtype=float).reshape(1, 3),
            'T_ambient': t_device,
        }
        sio.savemat(path, {'cal': cal})
        self.status.setText(('Saved %s - a fitted PsychCal file, load directly with '
                             'cal = LoadCalFile(...) in Psychtoolbox.' % path))
