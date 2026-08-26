# Unit setup and calibration tabs, embedded in the main window's Calibration tab.
# Each tab is a self-contained QWidget built once at startup and kept alive for
# the app's lifetime; set_connection() handles the connection going from None to a
# real SerialConnection and back across a Reconnect.

import math

from PySide6.QtCore import Qt, QTimer, QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QFileDialog,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QRadioButton, QSlider,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

import numpy as np

import calibration
import calibration_io
import plotting
import serial_io

PIXELS = calibration.PIXELS
SAT_VALUE = 1000  # firmware's over-exposure threshold, used to normalise linCoefs

# Servo/wheel mechanism stalls outside this range on the reference unit; see serial_io.
WHEEL_MIN_ANGLE = 15
WHEEL_MAX_ANGLE = 165

WHEEL_ROLE_HELP = {
    'D': "Blocks all light (closed shutter). Used to measure the sensor's own dark-current "
         "baseline, which the firmware subtracts from every Radiance/Irradiance reading.",
    'I': "Positions the cosine-corrected diffuser over the sensor. Measures light from the "
         "whole sky/hemisphere above a surface - ambient light. This is what the main "
         "window's 'Irradiance' button measures.",
    'R': "Positions the clear aperture - a narrow, aimable field of view. Measures light "
         "from a specific point or surface, like a spot brightness meter. This is what "
         "the main window's 'Radiance' button measures.",
}

WHEEL_ROLE_NAMES = {'D': 'Dark', 'I': 'Irradiance', 'R': 'Radiance'}


class CalibrationFitError(Exception):
    pass


def wrapped_label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    return label


def tip(widget, text):
    """Set a tooltip with a wrap length approximating the old Tkinter Tooltip (280px)."""
    widget.setToolTip('<div style="max-width:280px">%s</div>' % text)


def collapsible_group(title, start_open=False):
    """A QGroupBox whose contents fold away (Qt's checkable QGroupBox only greys out).

    Returns (group, content_layout). The point is to reclaim vertical space that
    checkable QGroupBox still costs, which matters on a phone screen.
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


def _read_number_file(path):
    with open(path) as handle:
        text = handle.read()
    for sep in (',', ';', '\t', '\n'):
        text = text.replace(sep, ' ')
    values = []
    for token in text.split():
        try:
            values.append(float(token))
        except ValueError:
            pass
    return values


def golden_section_minimize(objective, lo, hi, tol=1e-10, max_iter=500):
    """Bounded scalar minimizer for LinearisationTab._fit()'s single-parameter fit,
    hand-rolled in place of scipy.optimize.least_squares (scipy has no Android build).
    """
    invphi = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c = b - invphi * (b - a)
    d = a + invphi * (b - a)
    fc = objective(c)
    fd = objective(d)
    for _ in range(max_iter):
        if abs(b - a) < tol:
            break
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - invphi * (b - a)
            fc = objective(c)
        else:
            a, c, fc = c, d, fd
            d = a + invphi * (b - a)
            fd = objective(d)
    return (a + b) / 2


def gaussian_smooth(values, sigma):
    """Gaussian kernel smoothing, matching the calibration spreadsheet."""
    if sigma <= 0:
        return list(values)
    radius = max(1, int(math.ceil(sigma * 3)))
    offsets = np.arange(-radius, radius + 1)
    kernel = np.exp(-(offsets ** 2) / (2.0 * sigma ** 2))
    kernel /= kernel.sum()
    padded = np.pad(np.asarray(values, dtype=float), radius, mode='edge')
    return list(np.convolve(padded, kernel, mode='valid'))


class UnitSetupTab(QWidget):
    """Set the three shutter-wheel positions, saved to the Arduino's EEPROM."""

    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self._connected_widgets = []
        self._last_sent_angle = None
        self._saved_angles = {'D': None, 'I': None, 'R': None}
        self._role_value_labels = {}
        self._jog_timer = QTimer(self)
        self._jog_timer.setSingleShot(True)
        self._jog_timer.timeout.connect(self._jog_now)

        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(
            'Set this unit\'s shutter-wheel positions. Stored on the Arduino, so this '
            'only needs doing once per unit - no reflashing. (Unit number lives on the '
            'Import & export tab.)'))

        self.config_label = wrapped_label('')
        layout.addWidget(self.config_label)

        wheel_group = QGroupBox('Shutter wheel')
        wheel_layout = QVBoxLayout(wheel_group)
        wheel_layout.addWidget(wrapped_label(
            'Move the wheel with the slider, then save each position once it lines up. '
            'Dark = closed (centre); irradiance = cosine diffuser; radiance = clear aperture.'))

        slider_row = QHBoxLayout()
        minus_btn = QPushButton('-')
        minus_btn.setFixedWidth(32)
        minus_btn.clicked.connect(lambda: self._nudge(-1))
        slider_row.addWidget(minus_btn)
        self.angle_slider = QSlider(Qt.Orientation.Horizontal)
        self.angle_slider.setMinimum(WHEEL_MIN_ANGLE)
        self.angle_slider.setMaximum(WHEEL_MAX_ANGLE)
        self.angle_slider.setValue(90)
        self.angle_slider.valueChanged.connect(self._on_slider_changed)
        self.angle_slider.sliderReleased.connect(self._jog_now)
        slider_row.addWidget(self.angle_slider, 1)
        plus_btn = QPushButton('+')
        plus_btn.setFixedWidth(32)
        plus_btn.clicked.connect(lambda: self._nudge(1))
        slider_row.addWidget(plus_btn)
        wheel_layout.addLayout(slider_row)
        self._connected_widgets += [minus_btn, self.angle_slider, plus_btn]

        entry_row = QHBoxLayout()
        entry_row.addWidget(QLabel('Angle'))
        self.angle_edit = QLineEdit(str(self.angle_slider.value()))
        self.angle_edit.setFixedWidth(60)
        entry_row.addWidget(self.angle_edit)
        go_btn = QPushButton('Go')
        go_btn.clicked.connect(self._jog_from_entry)
        entry_row.addWidget(go_btn)
        entry_row.addStretch(1)
        wheel_layout.addLayout(entry_row)
        self._connected_widgets += [self.angle_edit, go_btn]

        # Stored positions per role, with "Go" to jog the wheel there for a visual
        # check and "Set as..." to overwrite from the current slider position.
        positions_layout = QGridLayout()
        for i, role in enumerate(('D', 'I', 'R')):
            role_name = WHEEL_ROLE_NAMES[role]
            positions_layout.addWidget(QLabel(role_name + ':'), i, 0)
            value_label = QLabel('-')
            positions_layout.addWidget(value_label, i, 1)
            self._role_value_labels[role] = value_label

            go_role_btn = QPushButton('Go')
            go_role_btn.setFixedWidth(48)
            go_role_btn.clicked.connect(lambda checked=False, r=role: self._go_to_position(r))
            go_role_btn.setToolTip('Move the wheel to the saved %s position.' % role_name)
            positions_layout.addWidget(go_role_btn, i, 2)
            self._connected_widgets.append(go_role_btn)

            set_btn = QPushButton('Set as %s' % role_name)
            set_btn.clicked.connect(lambda checked=False, r=role: self._save_position(r))
            tip(set_btn, WHEEL_ROLE_HELP[role])
            positions_layout.addWidget(set_btn, i, 3)
            self._connected_widgets.append(set_btn)
        wheel_layout.addLayout(positions_layout)
        layout.addWidget(wheel_group)

        self.status = wrapped_label('')
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        # Physical wheel may have moved between sessions or the unit been reflashed.
        self._last_sent_angle = None
        for w in self._connected_widgets:
            w.setEnabled(connection is not None)
        if connection:
            self._refresh()
        else:
            self.config_label.setText('Not connected.')
            self.status.setText('')
            for label in self._role_value_labels.values():
                label.setText('-')

    def _refresh(self):
        try:
            config = self.connection.get_config()
        except serial_io.SpecError as exc:
            self.config_label.setText(str(exc))
            return
        state = 'configured' if config.configured else 'not yet configured (firmware defaults)'
        self.config_label.setText(
            'Unit #%d - Firmware v%s, %s' % (config.unit_number, config.firmware, state))

        self._saved_angles = {'D': config.dark, 'I': config.irr, 'R': config.rad}
        for role, label in self._role_value_labels.items():
            angle = self._saved_angles[role]
            if angle is None or angle < WHEEL_MIN_ANGLE or angle > WHEEL_MAX_ANGLE:
                label.setText('not set')
            else:
                label.setText('%d deg' % angle)

    def _nudge(self, delta):
        self.angle_slider.setValue(
            max(WHEEL_MIN_ANGLE, min(WHEEL_MAX_ANGLE, self.angle_slider.value() + delta)))
        self._jog_now()

    def _on_slider_changed(self, value):
        # Sync the free-typed entry with the slider (they shared one Tkinter var in
        # the old UI; here they're synced by hand).
        self.angle_edit.blockSignals(True)
        self.angle_edit.setText(str(value))
        self.angle_edit.blockSignals(False)
        self._jog_timer.start(150)  # debounce: a drag fires this far faster than serial

    def _jog_from_entry(self):
        try:
            angle = int(float(self.angle_edit.text()))
        except ValueError:
            return
        angle = max(WHEEL_MIN_ANGLE, min(WHEEL_MAX_ANGLE, angle))
        self.angle_slider.setValue(angle)
        self._jog_now()

    def _jog_now(self):
        self._jog_timer.stop()
        self._jog()

    def _jog(self):
        angle = self.angle_slider.value()
        if angle == self._last_sent_angle:
            return
        try:
            self.connection.jog_wheel(angle)
        except serial_io.SpecError as exc:
            self.status.setText(str(exc))
            return
        self._last_sent_angle = angle

    def _save_position(self, role):
        try:
            self.connection.save_wheel_position(role)
        except serial_io.SpecError as exc:
            self.status.setText(str(exc))
            return
        self.status.setText('Position saved at %d degrees.' % self.angle_slider.value())
        self._refresh()

    def _go_to_position(self, role):
        angle = self._saved_angles.get(role)
        role_name = WHEEL_ROLE_NAMES[role]
        if angle is None or angle < WHEEL_MIN_ANGLE or angle > WHEEL_MAX_ANGLE:
            self.status.setText('%s position has not been saved yet - move the wheel '
                                'and click "Set as %s" first.' % (role_name, role_name))
            return
        self.angle_slider.setValue(angle)
        self._jog_now()
        self.status.setText('Moved to saved %s position (%d degrees).' % (role_name, angle))


class LinearisationTab(QWidget):
    """Fit linCoefs by measuring one steady source across a range of integration times."""

    def __init__(self, connection, store):
        super().__init__()
        self.connection = connection
        self.store = store
        self.result = None

        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(
            'Point the OSpRad at a steady, non-flickering source (daylight or an '
            'incandescent lamp; avoid most LED/fluorescent lighting) and keep both '
            'still. The wizard measures across a range of integration times and fits '
            'the curve so doubling exposure doubles the reported signal.'))

        controls = QHBoxLayout()
        self.radio_r = QRadioButton('Radiance')
        self.radio_r.setChecked(True)
        self.radio_i = QRadioButton('Irradiance')
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.radio_r)
        mode_group.addButton(self.radio_i)
        controls.addWidget(self.radio_r)
        controls.addWidget(self.radio_i)
        self.run_button = QPushButton('Run measurements')
        self.run_button.clicked.connect(self._run)
        controls.addWidget(self.run_button)
        self.save_button = QPushButton('Save')
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        controls.addWidget(self.save_button)
        controls.addStretch(1)
        layout.addLayout(controls)

        self.status = wrapped_label('')
        layout.addWidget(self.status)

        self.plot = plotting.SpectrumPlot()
        layout.addWidget(self.plot.canvas, 1)

        self.set_connection(connection)

    def _mode(self):
        return 'r' if self.radio_r.isChecked() else 'i'

    def set_connection(self, connection):
        self.connection = connection
        self.run_button.setEnabled(connection is not None)
        if not connection:
            self.status.setText('Not connected.')

    def _run(self):
        self.run_button.setEnabled(False)
        self.save_button.setEnabled(False)
        try:
            samples = self._collect()
        except serial_io.SpecError as exc:
            self.status.setText(str(exc))
            return
        finally:
            self.run_button.setEnabled(True)

        if len(samples) < 3:
            self.status.setText('Not enough usable measurements - try a brighter or '
                                'dimmer source so the sensor is neither dark nor saturated.')
            return

        unit_number = samples[0][2]
        try:
            coefs = self._fit(samples)
        except CalibrationFitError as exc:
            self.status.setText(str(exc))
            return
        self.result = (unit_number, coefs)
        self.status.setText('Unit #%d: fitted a = %.5f, b = %.5f from %d integration '
                            'times. A good fit gives near-horizontal lines below.'
                            % (unit_number, coefs[0], coefs[1], len(samples)))
        self._show(samples, coefs)
        self.save_button.setEnabled(True)

    def _collect(self):
        mode = self._mode()

        # Let the unit pick a well-exposed time, then sweep around it so the ladder
        # suits the source in front of the sensor rather than assuming one.
        self.status.setText('Finding a good exposure...')
        QApplication.processEvents()
        self.connection.set_integration_time(0)
        reference = self.connection.measure(mode)

        int_times = sorted({max(1, int(round(reference.int_time * f)))
                            for f in (0.05, 0.1, 0.2, 0.35, 0.55, 0.75, 1.0)})

        samples = []
        for index, int_time in enumerate(int_times, 1):
            self.status.setText('Measuring %d of %d at %d ms...'
                                % (index, len(int_times), int_time))
            QApplication.processEvents()
            self.connection.set_integration_time(int_time)
            measurement = self.connection.measure(mode)
            counts = np.asarray(measurement.raw_counts)
            if measurement.saturated > 2 or counts.max() < 20:
                continue  # over-exposed or too dark to be useful
            samples.append((int_time, counts, measurement.unit_number))

        self.connection.set_integration_time(0)
        self.status.setText('')
        return samples

    @staticmethod
    def _usable_mask(counts):
        """Per-reading mask of well-exposed photosites, keeping pixels seen at 2+ exposures."""
        for threshold in (30, 10, 3):
            mask = (counts > threshold) & (counts < SAT_VALUE)
            mask &= (mask.sum(axis=0) >= 2)
            if mask.sum() >= 40:
                return mask
        return mask

    @classmethod
    def _fit(cls, samples):
        """Fit the linearisation coefficients so that signal per ms is flat across
        integration times. Only b changes the curve shape; a is pinned by convention
        so the correction equals 1.0 at the saturation threshold."""
        times = np.array([t for t, _, _ in samples], dtype=float)
        counts = np.stack([c for _, c, _ in samples])  # (n_times, PIXELS)
        mask = cls._usable_mask(counts)
        if mask.sum() < 10:
            raise CalibrationFitError(
                'Not enough well-exposed photosites across the integration-time sweep. '
                'Try a source that is brighter, steadier, or more evenly spread across '
                'the spectrum.')

        weights = mask.astype(float)
        per_pixel = weights.sum(axis=0)
        safe_counts = np.where(mask, counts, 1.0)

        def residuals(params):
            b = params[0]
            linear = safe_counts / np.log((safe_counts + 1) * b)
            rate = linear / times[:, None]
            reference = (rate * weights).sum(axis=0) / np.where(per_pixel > 0, per_pixel, 1.0)
            reference = np.where(reference > 0, reference, 1.0)
            return ((rate - reference) / reference)[mask]

        def objective(b):
            return float(np.sum(residuals([b]) ** 2))

        b = golden_section_minimize(objective, 1e-3, 1e6)
        a = 1.0 / math.log((SAT_VALUE + 1) * b)
        return [a, b]

    def _show(self, samples, coefs):
        times = np.array([t for t, _, _ in samples], dtype=float)
        counts = np.stack([c for _, c, _ in samples])
        mask = self._usable_mask(counts)
        usable = mask.all(axis=0)
        if usable.sum() < 5:
            usable = mask.sum(axis=0) >= max(2, len(samples) - 1)

        ax = self.plot.ax
        ax.clear()
        self.plot._style_axes()
        ax.set_xlabel('Integration time (ms)')
        ax.set_ylabel('Signal per ms (normalised)')
        ax.set_title('Linearisation check - flat is good', fontsize=10)

        # Plot only photosites well exposed at every step, so each line spans the sweep.
        raw_rate = counts[:, usable] / times[:, None]
        linear = np.array([[calibration.linearize(v, coefs) for v in row]
                           for row in counts[:, usable]])
        lin_rate = linear / times[:, None]

        step = max(1, raw_rate.shape[1] // 40)
        for series, color, label in ((raw_rate, '#d1495b', 'raw counts'),
                                     (lin_rate, '#2e86ab', 'linearised')):
            normalised = series / series.mean(axis=0)
            for column in range(0, series.shape[1], step):
                ax.plot(times, normalised[:, column], color=color, alpha=0.35, linewidth=0.9)
            ax.plot([], [], color=color, label=label)
        ax.set_xscale('log')
        ax.legend(fontsize=9)
        self.plot.canvas.draw()

    def _save(self):
        unit_number, coefs = self.result
        try:
            calib = self.store.get(unit_number)
        except calibration.CalibrationError:
            QMessageBox.critical(self, 'OSpRad', (
                'Unit #%d has no calibration data yet. Add its wavelength coefficients and '
                'sensitivity curves first, then save the linearisation.' % unit_number))
            return

        reply = QMessageBox.question(self, 'OSpRad', (
            'Replace the linearisation coefficients for unit #%d?\n\n'
            'Old: a = %.5f, b = %.5f\nNew: a = %.5f, b = %.5f\n\n'
            'Measurements are scaled by these coefficients, so afterwards you should '
            're-derive the spectral sensitivity, or rescale it against a known reading '
            'on the Spectral sensitivity tab.'
            % (unit_number, calib.lin_coefs[0], calib.lin_coefs[1], coefs[0], coefs[1])),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Ok:
            return

        # linCoefs set the overall scale of the linearised signal - the sensitivity
        # curves are only valid for the coefficients they were derived against.
        calib.lin_coefs = coefs
        self.store.save_unit(calib)
        self.status.setText('Saved linearisation coefficients for unit #%d. Re-check the '
                            'spectral sensitivity next.' % unit_number)


class SensitivityTab(QWidget):
    """Import, rescale, or derive radSens / irrSens."""

    def __init__(self, connection, store):
        super().__init__()
        self.connection = connection
        self.store = store
        self.pending = None

        layout = QVBoxLayout(self)

        head = QHBoxLayout()
        head.addWidget(QLabel('Calibrating:'))
        self.radio_r = QRadioButton('Radiance')
        self.radio_r.setChecked(True)
        self.radio_i = QRadioButton('Irradiance')
        mode_group = QButtonGroup(self)
        mode_group.addButton(self.radio_r)
        mode_group.addButton(self.radio_i)
        head.addWidget(self.radio_r)
        head.addWidget(self.radio_i)
        head.addStretch(1)
        layout.addLayout(head)

        # Three routes, ordered easiest-to-hardest: a file you already have; one
        # reference reading; a full reference spectrum.
        layout.addWidget(wrapped_label(
            'Three ways to set the curve - pick whichever matches what you have. Each '
            'previews the result below; nothing is written until you press Save.'))

        importer = QGroupBox('1. Load a curve from a file')
        importer_layout = QVBoxLayout(importer)
        importer_layout.addWidget(wrapped_label(
            'If you already have %d sensitivity values - for example exported from the '
            '"sensitivity FINAL" sheet of calibration_calculations.ods.'
            % PIXELS))
        self.import_button = QPushButton('Choose file...')
        self.import_button.clicked.connect(self._import)
        importer_layout.addWidget(self.import_button, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(importer)

        rescale = QGroupBox('2. Rescale to a known reading')
        rescale_layout = QVBoxLayout(rescale)
        rescale_layout.addWidget(wrapped_label(
            'If you have one trusted reading. Keeps the existing spectral shape and '
            'only corrects its overall level: measure a source whose luminance (cd/sqm) '
            'or illuminance (lux) you already know, and enter that value here.'))
        rescale_row = QHBoxLayout()
        rescale_row.addWidget(QLabel('Known value'))
        self.reference_value_edit = QLineEdit()
        self.reference_value_edit.setFixedWidth(100)
        rescale_row.addWidget(self.reference_value_edit)
        self.rescale_button = QPushButton('Measure and rescale')
        self.rescale_button.clicked.connect(self._rescale)
        rescale_row.addWidget(self.rescale_button)
        rescale_row.addStretch(1)
        rescale_layout.addLayout(rescale_row)
        layout.addWidget(rescale)

        # Folded by default: needs a reference spectroradiometer (rarest route).
        derive, derive_layout = collapsible_group(
            '3. Derive from a reference spectrum (advanced)')
        derive_layout.addWidget(wrapped_label(
            'Requires a reference spectroradiometer. Provide the true spectrum of a '
            'light source as a two-column file (wavelength in nm, then W/(sr*sqm*nm) for '
            'radiance or W/(sqm*nm) for irradiance). The OSpRad measures the same source '
            'and the ratio gives its spectral sensitivity.'))
        derive_row = QHBoxLayout()
        derive_row.addWidget(QLabel('Smoothing sigma'))
        self.sigma_edit = QLineEdit('2')
        self.sigma_edit.setFixedWidth(50)
        derive_row.addWidget(self.sigma_edit)
        self.derive_button = QPushButton('Choose reference spectrum and measure...')
        self.derive_button.clicked.connect(self._derive)
        derive_row.addWidget(self.derive_button)
        derive_row.addStretch(1)
        derive_layout.addLayout(derive_row)
        layout.addWidget(derive)

        actions = QHBoxLayout()
        self.status = wrapped_label('')
        actions.addWidget(self.status, 1)
        self.save_button = QPushButton('Save')
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        self.plot = plotting.SpectrumPlot()
        layout.addWidget(self.plot.canvas, 1)

        self.set_connection(connection)

    def _mode(self):
        return 'r' if self.radio_r.isChecked() else 'i'

    def set_connection(self, connection):
        self.connection = connection
        enabled = connection is not None
        for w in (self.import_button, self.rescale_button, self.derive_button):
            w.setEnabled(enabled)
        if not connection:
            self.status.setText('Not connected.')

    def _current_unit(self):
        config = self.connection.get_config()
        return self.store.get(config.unit_number)

    def _check_exposure(self, measurement):
        """Reject over-exposed measurements - saturated photosites read low and would
        silently corrupt a calibration. Returns False after setting self.status."""
        if measurement.saturated > 0:
            self.status.setText(
                'Measurement is over-exposed (%g saturated photosites), so it cannot be '
                'used for calibration. Dim the source, or set a shorter integration time '
                'on the main window.' % measurement.saturated)
            return False
        return True

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'OSpRad', '', 'Data files (*.csv *.txt *.tsv);;All files (*)')
        if not path:
            return
        values = _read_number_file(path)
        if len(values) != PIXELS:
            QMessageBox.critical(self, 'OSpRad', 'Expected %d values but the file has %d.'
                                 % (PIXELS, len(values)))
            return
        try:
            calib = self._current_unit()
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.setText(str(exc))
            return
        self._stage(calib, values, 'Imported %d values from file.' % PIXELS)

    def _rescale(self):
        try:
            reference = float(self.reference_value_edit.text())
            if reference <= 0:
                raise ValueError
        except ValueError:
            QMessageBox.critical(self, 'OSpRad', 'Enter the known reference value first.')
            return

        mode = self._mode()
        try:
            calib = self._current_unit()
            self.status.setText('Measuring...')
            QApplication.processEvents()
            measurement = self.connection.measure(mode)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.setText(str(exc))
            return
        if not self._check_exposure(measurement):
            return

        flux = calib.to_flux(measurement.raw_counts, mode, measurement.int_time)
        measured = calib.luminance(flux)
        if measured <= 0:
            self.status.setText('Measured signal is zero - check the shutter positions.')
            return

        factor = measured / reference
        values = [v * factor for v in calib.sensitivity(mode)]
        self._stage(calib, values, 'Measured %.4g, reference %.4g - sensitivity scaled by %.4f.'
                    % (measured, reference, factor))

    def _derive(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Reference spectrum (wavelength, flux)', '',
            'Data files (*.csv *.txt *.tsv);;All files (*)')
        if not path:
            return
        numbers = _read_number_file(path)
        if len(numbers) < 4 or len(numbers) % 2:
            QMessageBox.critical(self, 'OSpRad', 'Expected two columns: wavelength and flux.')
            return
        reference = np.array(numbers).reshape(-1, 2)
        reference = reference[reference[:, 0].argsort()]

        mode = self._mode()
        try:
            calib = self._current_unit()
            self.status.setText('Measuring the reference source...')
            QApplication.processEvents()
            measurement = self.connection.measure(mode)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.setText(str(exc))
            return
        if not self._check_exposure(measurement):
            return

        calib._derive()
        wavelength = np.asarray(calib.wavelength)
        bins = np.asarray(calib.wavelength_bins)
        expected = np.interp(wavelength, reference[:, 0], reference[:, 1],
                             left=np.nan, right=np.nan)

        linear = np.array([calibration.linearize(c, calib.lin_coefs)
                           for c in measurement.raw_counts])
        with np.errstate(divide='ignore', invalid='ignore'):
            sens = linear / (expected * measurement.int_time * bins)
        sens[~np.isfinite(sens)] = 0.0
        sens[sens < 0] = 0.0

        try:
            sigma = float(self.sigma_edit.text())
        except ValueError:
            sigma = 0.0
        smoothed = np.array(gaussian_smooth(sens, sigma))
        smoothed[sens <= 0] = 0.0

        covered = int(np.count_nonzero(smoothed))
        self._stage(calib, list(smoothed), (
            'Derived sensitivity across %d of %d photosites (the rest fall outside the '
            'reference spectrum\'s wavelength range and are left at zero).'
            % (covered, PIXELS)))

    def _stage(self, calib, values, message):
        self.pending = (calib, self._mode(), list(values))
        self.status.setText(message + ' Review the curve, then Save.')
        self.save_button.setEnabled(True)
        calib._derive()
        self.plot.update(calib.wavelength, values, None,
                         '%s sensitivity - unit #%d'
                         % ('Irradiance' if self._mode() == 'i' else 'Radiance',
                            calib.unit_number))

    def _save(self):
        calib, mode, values = self.pending
        if mode == 'i':
            calib.irr_sens = values
        else:
            calib.rad_sens = values
        self.store.save_unit(calib)
        self.save_button.setEnabled(False)
        self.status.setText('Saved %s sensitivity for unit #%d.'
                            % ('irradiance' if mode == 'i' else 'radiance', calib.unit_number))


class _AngleDiagram(QWidget):
    """Top-down schematic: sensor at centre, dashed 0-degree reference, solid line +
    light-source icon at the requested angle. Easier to eyeball than the number alone."""

    def __init__(self):
        super().__init__()
        self.setFixedSize(150, 130)
        self.angle = 0.0

    def set_angle(self, angle):
        self.angle = angle
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, radius = 75, 100, 65

        painter.setPen(QPen(QColor('#999999'), 1, Qt.PenStyle.DashLine))
        painter.drawLine(cx, cy, cx, cy - radius)

        angle = min(max(self.angle, 0), 90)
        rad = math.radians(angle)
        ex = cx + radius * math.sin(rad)
        ey = cy - radius * math.cos(rad)

        if angle > 0.5:
            # Qt angles are in 1/16ths of a degree, positive = counterclockwise from
            # 3 o'clock - opposite of Tk's create_arc, so this sweeps from 12 o'clock
            # (90*16) clockwise (negative span) by `angle`.
            painter.setPen(QPen(QColor('#2a9d8f'), 2))
            painter.drawArc(QRectF(cx - 24, cy - 24, 48, 48), 90 * 16, -int(round(angle * 16)))

        painter.setPen(QPen(QColor('#e9a23a'), 2))
        painter.drawLine(QPointF(cx, cy), QPointF(ex, ey))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#e9a23a'))
        painter.drawEllipse(QPointF(ex, ey), 6, 6)
        painter.setPen(QColor('#e9a23a'))
        painter.drawText(QRectF(ex - 30, ey - 28, 60, 16), Qt.AlignmentFlag.AlignCenter, 'light')

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor('#2e86ab'))
        painter.drawEllipse(QPointF(cx, cy), 7, 7)
        painter.setPen(QColor('#7a7a7a'))
        painter.drawText(QRectF(cx - 30, cy + 8, 60, 16), Qt.AlignmentFlag.AlignCenter, 'sensor')
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#1c1c1c'))
        painter.drawText(QRectF(cx - 30, cy + 22, 60, 16), Qt.AlignmentFlag.AlignCenter,
                         '%.0f\N{DEGREE SIGN}' % angle)


class CosineResponseTab(QWidget):
    """Checks the cosine diffuser's angular response (not its colour - that's the
    Monitor calibration tab's job). Measures Irradiance at a series of incidence
    angles against a fixed, distant source, then plots the response normalised to
    the 0-degree reading against the ideal cos(angle) falloff."""

    # Common, easy-to-eyeball angles with a plain-language sense of "off straight-on".
    PRESET_ANGLES = ((0, 'Straight on'), (15, 'Slight angle'), (30, 'A third turn'),
                     (45, 'Diagonal'), (60, 'Steep angle'), (75, 'Nearly edge-on'),
                     (90, 'Edge-on'))

    def __init__(self, connection, store):
        super().__init__()
        self.connection = connection
        self.store = store
        self.results = []  # [(angle_deg, lux), ...]

        layout = QVBoxLayout(self)
        layout.addWidget(wrapped_label(
            'Point the sensor at a small, distant, steady source (direct sunlight is '
            'ideal; indoors a bare bulb several metres away works) and keep source and '
            'distance fixed throughout. No protractor needed: a fist at arm\'s length '
            'spans ~10 degrees, so 3 fists = ~30, 4-5 = ~45, 6 = ~60.'))
        layout.addWidget(wrapped_label(
            'Start with "Straight on" - that reading is the reference every other angle '
            'is compared against. A well-behaved cosine diffuser reads close to '
            'cos(angle) of the reference: 87% at 30 deg, 71% at 45, 50% at 60. '
            'Consistent under-response at high angles is the classic failure mode '
            '(tape too thick, or shadowed by the housing).'))

        picker = QHBoxLayout()
        self.diagram = _AngleDiagram()
        picker.addWidget(self.diagram)
        preset_grid = QGridLayout()
        self._preset_buttons = []
        for i, (angle, label) in enumerate(self.PRESET_ANGLES):
            r, c = divmod(i, 4)
            btn = QPushButton('%d\N{DEGREE SIGN}\n%s' % (angle, label))
            btn.clicked.connect(lambda checked=False, a=angle: self._set_angle(a))
            preset_grid.addWidget(btn, r, c)
            self._preset_buttons.append(btn)
        picker.addLayout(preset_grid)
        picker.addStretch(1)
        layout.addLayout(picker)

        entry_row = QHBoxLayout()
        entry_row.addWidget(QLabel('Angle (degrees)'))
        self.angle_edit = QLineEdit('0')
        self.angle_edit.setFixedWidth(60)
        self.angle_edit.setToolTip('Type an exact angle instead of using a preset, if '
                                   'you have a protractor or jig for precise positioning.')
        self.angle_edit.textChanged.connect(self._on_angle_typed)
        entry_row.addWidget(self.angle_edit)
        self.measure_button = QPushButton('Measure')
        self.measure_button.clicked.connect(self._measure)
        entry_row.addWidget(self.measure_button)
        self.clear_button = QPushButton('Clear results')
        self.clear_button.clicked.connect(self._clear)
        entry_row.addWidget(self.clear_button)
        entry_row.addStretch(1)
        layout.addLayout(entry_row)

        self.status = wrapped_label('')
        layout.addWidget(self.status)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['Angle', 'Lux', 'Measured ratio', 'Ideal cos(angle)', 'Deviation'])
        self.tree.setRootIsDecorated(False)
        self.tree.setMaximumHeight(160)
        layout.addWidget(self.tree)

        self.plot = plotting.SpectrumPlot()
        layout.addWidget(self.plot.canvas, 1)

        self._connected_widgets = [self.angle_edit, self.measure_button] + self._preset_buttons
        self.diagram.set_angle(0)
        self.set_connection(connection)
        self._refresh()

    def _set_angle(self, angle):
        self.angle_edit.setText(str(angle))
        self.diagram.set_angle(angle)
        if angle == 0:
            self.status.setText('Point the sensor straight at the source, then press Measure.')
        else:
            self.status.setText('Point the sensor about %d\N{DEGREE SIGN} off straight-on '
                                '(see the diagram), then press Measure.' % angle)

    def _on_angle_typed(self, text):
        try:
            angle = float(text)
        except ValueError:
            return
        self.diagram.set_angle(angle)

    def set_connection(self, connection):
        self.connection = connection
        for w in self._connected_widgets:
            w.setEnabled(connection is not None)
        if not connection:
            self.status.setText('Not connected.')

    def _measure(self):
        try:
            angle = float(self.angle_edit.text())
            if angle < 0:
                raise ValueError
        except ValueError:
            self.status.setText('Angle must be a number >= 0.')
            return

        try:
            measurement = self.connection.measure('i')
            calib = self.store.get(measurement.unit_number)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.setText(str(exc))
            return
        flux = calib.to_flux(measurement.raw_counts, 'i', measurement.int_time)
        lux = calib.luminance(flux)

        self.results.append((angle, lux))
        self.results.sort(key=lambda r: r[0])
        if any(a == 0 for a, _ in self.results):
            self.status.setText('Measured %.1f lux at %.1f degrees.' % (lux, angle))
        else:
            self.status.setText(
                'Measured %.1f lux at %.1f degrees. Measure at 0 degrees (facing the '
                'source directly) too, to use as the reference.' % (lux, angle))
        self._refresh()

    def _clear(self):
        self.results = []
        self.status.setText('')
        self._refresh()

    def _refresh(self):
        self.tree.clear()
        zero_readings = [lux for a, lux in self.results if a == 0]
        reference = zero_readings[0] if zero_readings else None

        for angle, lux in self.results:
            ideal = math.cos(math.radians(angle))
            if reference and reference > 0:
                ratio = lux / reference
                ratio_text = '%.3f' % ratio
                deviation = '%+.0f%%' % ((ratio - ideal) / ideal * 100) if ideal > 0 else '-'
            else:
                ratio_text = deviation = '-'
            self.tree.addTopLevelItem(QTreeWidgetItem([
                '%.1f' % angle, '%.3g' % lux, ratio_text, '%.3f' % ideal, deviation]))

        self._show_plot(reference)

    def _show_plot(self, reference):
        ax = self.plot.ax
        ax.clear()
        self.plot._style_axes()
        ax.set_xlabel('Angle from source (degrees)')
        ax.set_ylabel('Normalised response')
        ax.set_title('Cosine response - closer to the dashed line is better', fontsize=10)

        angles_ideal = np.linspace(0, 90, 91)
        ax.plot(angles_ideal, np.cos(np.radians(angles_ideal)), color=self.plot.colors['grid'],
               linewidth=1.5, linestyle='--', label='ideal cos(angle)')

        if reference and reference > 0 and self.results:
            angles = [a for a, _ in self.results]
            ratios = [lux / reference for _, lux in self.results]
            ax.plot(angles, ratios, color='#2e86ab', marker='o', markersize=4,
                   linewidth=1.2, label='measured')

        ax.set_xlim(0, 90)
        ax.set_ylim(0, 1.05)
        ax.legend(fontsize=9)
        self.plot.canvas.draw()


class CalibrationTransferTab(QWidget):
    """Unit number, plus export/import of everything else as one JSON file.

    Calibrated transfer lives here (not on the tab that produces each value) so there
    is one obvious place to back a unit up from and restore it to. The unit number
    sits here too: it's what ties the two halves together - CSV rows are looked up by
    it, and it's stored on the Arduino alongside the wheel positions.
    """

    def __init__(self, connection, store, log=None):
        super().__init__()
        self.connection = connection
        self.store = store
        self._log = log or (lambda text, level='info': None)

        layout = QVBoxLayout(self)

        unit_group = QGroupBox('Unit number')
        unit_layout = QVBoxLayout(unit_group)
        unit_layout.addWidget(wrapped_label(
            'Each unit needs its own ID, used to look up its calibration data. Stored on the Arduino.'))
        unit_row = QHBoxLayout()
        self.unit_number_edit = QLineEdit()
        self.unit_number_edit.setFixedWidth(80)
        unit_row.addWidget(self.unit_number_edit)
        self.save_unit_button = QPushButton('Save to unit')
        self.save_unit_button.clicked.connect(self._save_unit)
        unit_row.addWidget(self.save_unit_button)
        unit_row.addStretch(1)
        unit_layout.addLayout(unit_row)
        layout.addWidget(unit_group)

        select_group = QGroupBox('Include')
        select_layout = QVBoxLayout(select_group)
        select_layout.addWidget(wrapped_label(
            'Applies to both buttons below. Everything is included by default; untick '
            'anything you want to leave out of an export, or leave untouched on import.'))
        self.field_checks = {}
        for key in calibration_io.ALL_FIELDS:
            check = QCheckBox(calibration_io.FIELD_LABELS[key])
            check.setChecked(True)
            self.field_checks[key] = check
            select_layout.addWidget(check)
        tip(self.field_checks[calibration_io.WHEEL_FIELD],
            'Read from (and written to) the Arduino, not calibration_data.csv - this '
            'one needs a connected unit.')
        layout.addWidget(select_group)

        transfer_row = QHBoxLayout()
        self.export_button = QPushButton('Export to file...')
        self.export_button.clicked.connect(self._export)
        transfer_row.addWidget(self.export_button)
        self.import_button = QPushButton('Import from file...')
        self.import_button.clicked.connect(self._import)
        transfer_row.addWidget(self.import_button)
        transfer_row.addStretch(1)
        layout.addLayout(transfer_row)

        self.status = wrapped_label('')
        layout.addWidget(self.status)
        layout.addStretch(1)

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        self.save_unit_button.setEnabled(connection is not None)
        self.unit_number_edit.setEnabled(connection is not None)
        if connection is None:
            self.unit_number_edit.clear()
            self.status.setText(
                'Not connected - exports cover calibration_data.csv only; the unit number '
                'and wheel positions can\'t be read or written.')
            return
        self.status.setText('')
        try:
            config = connection.get_config()
        except serial_io.SpecError as exc:
            self.status.setText(str(exc))
            return
        self.unit_number_edit.setText(str(config.unit_number))

    def _selected_fields(self):
        return [key for key, check in self.field_checks.items() if check.isChecked()]

    def _save_unit(self):
        try:
            self.connection.set_unit_number(int(self.unit_number_edit.text()))
        except (serial_io.SpecError, ValueError) as exc:
            self.status.setText(str(exc))
            return
        self.status.setText('Unit number saved to the Arduino.')
        self._log('Unit number set to %s.' % self.unit_number_edit.text())

    # export

    def _export(self):
        fields = self._selected_fields()
        if not fields:
            self.status.setText('Nothing selected to export - tick at least one item above.')
            return

        config = None
        if self.connection is not None:
            try:
                config = self.connection.get_config()
            except serial_io.SpecError as exc:
                self.status.setText(str(exc))
                return
            unit_number = config.unit_number
        else:
            try:
                unit_number = int(self.unit_number_edit.text())
            except ValueError:
                self.status.setText(
                    'Connect a unit to export, or type the unit number above first.')
                return

        csv_fields = [key for key in fields if key != calibration_io.WHEEL_FIELD]
        calib = None
        if csv_fields:
            try:
                calib = self.store.get(unit_number)
            except calibration.CalibrationError as exc:
                self.status.setText(str(exc))
                return
        if calib is None:
            # Wheel positions only - build_export still needs a CalibrationSet to read
            # the unit number off, but doesn't touch the (unused) curves.
            calib = calibration.CalibrationSet(unit_number, [], [], [], [])

        path, _ = QFileDialog.getSaveFileName(
            self, 'Export calibration', 'osprad-unit%d-calibration.json' % unit_number,
            'OSpRad calibration (*.json)')
        if not path:
            return
        try:
            calibration_io.write_file(path, calib, config, fields)
        except OSError as exc:
            self.status.setText('Could not write %s: %s' % (path, exc))
            return

        written = [calibration_io.FIELD_LABELS[key] for key in calibration_io.ALL_FIELDS
                   if key in fields
                   and (key != calibration_io.WHEEL_FIELD or config is not None)]
        skipped = (calibration_io.WHEEL_FIELD in fields and config is None)
        message = 'Exported unit #%d: %s.' % (unit_number, ', '.join(written).lower())
        if skipped:
            message += (' Wheel positions were skipped - they can only be read from a '
                        'connected unit.')
        self.status.setText(message)
        self._log('Exported unit #%d calibration to %s' % (unit_number, path),
                  level='warning' if skipped else 'info')

    # import

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Import calibration', '', 'OSpRad calibration (*.json)')
        if not path:
            return
        try:
            imported = calibration_io.read_file(path)
        except calibration_io.CalibrationIOError as exc:
            self.status.setText(str(exc))
            QMessageBox.critical(self, 'OSpRad', str(exc))
            return

        selected = self._selected_fields()
        available = imported.available_fields()
        applying = [key for key in available if key in selected]
        if not applying:
            self.status.setText(
                'Nothing to import: this file contains %s, none of which is ticked above.'
                % ', '.join(calibration_io.FIELD_LABELS[k].lower() for k in available))
            return

        wants_wheel = calibration_io.WHEEL_FIELD in applying
        csv_fields = [key for key in applying if key != calibration_io.WHEEL_FIELD]

        merged = None
        if csv_fields:
            try:
                merged = calibration_io.merge(self.store, imported, csv_fields)
            except calibration_io.CalibrationIOError as exc:
                self.status.setText(str(exc))
                QMessageBox.critical(self, 'OSpRad', str(exc))
                return

        message = ['Import into unit #%d?' % imported.unit_number, '']
        if csv_fields:
            replacing = imported.unit_number in self.store.units
            message.append('%s in calibration_data.csv:'
                           % ('Replaces' if replacing else 'Adds'))
            for key in csv_fields:
                message.append('  - %s' % calibration_io.FIELD_LABELS[key])
        if wants_wheel and self.connection is not None:
            message.append('')
            message.append('Writes the wheel positions (dark %(dark)d, irradiance '
                           '%(irr)d, radiance %(rad)d) to the connected Arduino, moving '
                           'the wheel to each in turn.' % imported.wheel)
        elif wants_wheel:
            message.append('')
            message.append('The wheel positions in this file will be skipped - nothing '
                           'is connected to write them to.')

        reply = QMessageBox.question(
            self, 'OSpRad', '\n'.join(message),
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Ok:
            return

        done = []
        if merged is not None:
            try:
                self.store.save_unit(merged)
            except OSError as exc:
                self.status.setText('Could not write calibration data: %s' % exc)
                return
            done += [calibration_io.FIELD_LABELS[key] for key in csv_fields]

        if wants_wheel and self.connection is not None:
            try:
                calibration_io.apply_wheel_positions(self.connection, imported.wheel)
            except serial_io.SpecError as exc:
                # The CSV half is already saved by this point, so say what landed
                # rather than implying the whole import failed.
                self.status.setText(
                    'Imported %s, but writing the wheel positions to the Arduino failed: %s'
                    % (', '.join(done).lower(), exc))
                self._log('Wheel positions failed to import: %s' % exc, level='error')
                return
            done.append(calibration_io.FIELD_LABELS[calibration_io.WHEEL_FIELD])

        self.status.setText('Imported into unit #%d: %s.'
                            % (imported.unit_number, ', '.join(done).lower()))
        self._log('Imported calibration for unit #%d from %s (%s)'
                  % (imported.unit_number, path, ', '.join(done).lower()))
