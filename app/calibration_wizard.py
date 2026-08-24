# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad
#
# Unit setup and calibration tabs, embedded in the main window's Calibration tab
# (see OSpRad.py). Each tab is a self-contained ttk.Frame built once at startup
# and kept alive for the app's lifetime; set_connection() handles the connection
# going from None to a real SerialConnection and back across a Reconnect.

import math
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from scipy.optimize import least_squares

import calibration
import plotting
import serial_io

PIXELS = calibration.PIXELS
SAT_VALUE = 1000  # firmware's over-exposure threshold, used to normalise linCoefs
PAD = 10

# Servo/wheel mechanism stalls outside this range on the reference unit.
WHEEL_MIN_ANGLE = 15
WHEEL_MAX_ANGLE = 165

WHEEL_ROLE_HELP = {
    'D': "Blocks all light (closed shutter). Used to measure the sensor's own dark-current "
         "noise baseline, which the firmware subtracts from every Radiance/Irradiance reading.",
    'I': "Positions the cosine-corrected diffuser over the sensor. Measures light arriving at "
         "a surface from the whole sky/hemisphere above it - ambient light level. This is what "
         "the main window's 'Irradiance' button measures.",
    'R': "Positions the clear aperture, giving the sensor a narrow, aimable field of view. "
         "Measures light from a specific point or surface you're pointing at, like a spot "
         "brightness meter. This is what the main window's 'Radiance' button measures.",
}

WHEEL_ROLE_NAMES = {'D': 'Dark', 'I': 'Irradiance', 'R': 'Radiance'}


class CalibrationFitError(Exception):
    pass


class Tooltip:
    """Minimal hover tooltip - Tkinter has no built-in one."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind('<Enter>', self._show)
        widget.bind('<Leave>', self._hide)

    def _show(self, event=None):
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry('+%d+%d' % (x, y))
        tk.Label(self.tip, text=self.text, justify='left', background='#ffffe0',
                relief='solid', borderwidth=1, wraplength=280, padx=6, pady=4,
                font=('TkDefaultFont', 9)).pack()

    def _hide(self, event=None):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


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


class UnitSetupTab(ttk.Frame):
    """Set the unit number and the three shutter-wheel positions, saved to EEPROM."""

    def __init__(self, parent, connection):
        super().__init__(parent, padding=PAD)
        self.connection = connection
        self.angle = tk.IntVar(value=90)
        self.unit_number = tk.StringVar()
        self._connected_widgets = []
        self._jog_after_id = None
        self._last_sent_angle = None
        self._saved_angles = {'D': None, 'I': None, 'R': None}
        self._role_value_labels = {}
        self.columnconfigure(0, weight=1)

        ttk.Label(self, wraplength=650, justify='left', text=(
            'Set this unit\'s ID and its shutter-wheel positions. Everything here is '
            'stored on the Arduino itself, so it only needs doing once per unit - no '
            'reflashing required.')).grid(row=0, column=0, sticky='w')

        self.config_label = ttk.Label(self, justify='left')
        self.config_label.grid(row=1, column=0, sticky='w', pady=(PAD, 0))

        unit = ttk.LabelFrame(self, text='Unit number', padding=PAD)
        unit.grid(row=2, column=0, sticky='ew', pady=(PAD, 0))
        ttk.Label(unit, text='Used to look up this unit\'s calibration data.').grid(
            row=0, column=0, columnspan=2, sticky='w')
        ttk.Entry(unit, textvariable=self.unit_number, width=8).grid(
            row=1, column=0, sticky='w', pady=(6, 0))
        save_unit_btn = ttk.Button(unit, text='Save unit number', command=self._save_unit)
        save_unit_btn.grid(row=1, column=1, sticky='w', padx=PAD, pady=(6, 0))
        self._connected_widgets.append(save_unit_btn)

        wheel = ttk.LabelFrame(self, text='Shutter wheel', padding=PAD)
        wheel.grid(row=3, column=0, sticky='ew', pady=(PAD, 0))
        wheel.columnconfigure(1, weight=1)
        ttk.Label(wheel, wraplength=620, justify='left', text=(
            'Move the wheel with the slider, then save each position once it lines up. '
            'Dark is the closed (central) position; irradiance uses the cosine diffuser; '
            'radiance is the clear aperture.')).grid(row=0, column=0, columnspan=3, sticky='w')

        minus_btn = ttk.Button(wheel, text='-', width=3, command=lambda: self._nudge(-1))
        minus_btn.grid(row=1, column=0, pady=(PAD, 0))
        # Debounce the drag: ttk.Scale fires command on every tick, far faster than the
        # servo/serial can keep up. Snap to whole degrees since ttk.Scale has no
        # built-in step/resolution (unlike classic tk.Scale).
        scale = ttk.Scale(wheel, from_=WHEEL_MIN_ANGLE, to=WHEEL_MAX_ANGLE,
                          variable=self.angle, command=self._on_scale_drag)
        scale.bind('<ButtonRelease-1>', lambda e: self._jog_now())
        scale.grid(row=1, column=1, sticky='ew', padx=6, pady=(PAD, 0))
        plus_btn = ttk.Button(wheel, text='+', width=3, command=lambda: self._nudge(1))
        plus_btn.grid(row=1, column=2, pady=(PAD, 0))
        self._connected_widgets += [minus_btn, scale, plus_btn]

        entry_row = ttk.Frame(wheel)
        entry_row.grid(row=2, column=0, columnspan=3, sticky='w', pady=(6, 0))
        ttk.Label(entry_row, text='Angle').grid(row=0, column=0)
        angle_entry = ttk.Entry(entry_row, textvariable=self.angle, width=6)
        angle_entry.grid(row=0, column=1, padx=6)
        go_btn = ttk.Button(entry_row, text='Go', command=self._jog)
        go_btn.grid(row=0, column=2)
        self._connected_widgets += [angle_entry, go_btn]

        # Show what's stored on the Arduino for each role, with "Go" to move the wheel
        # there for a quick visual check and "Set as..." to overwrite from the slider.
        positions = ttk.Frame(wheel)
        positions.grid(row=3, column=0, columnspan=3, sticky='w', pady=(PAD, 0))
        for i, role in enumerate(('D', 'I', 'R')):
            role_name = WHEEL_ROLE_NAMES[role]
            ttk.Label(positions, text=role_name + ':').grid(
                row=i, column=0, sticky='w', pady=(2, 0))
            value_label = ttk.Label(positions, text='-', width=8, style='Muted.TLabel')
            value_label.grid(row=i, column=1, sticky='w', padx=(6, 0), pady=(2, 0))
            self._role_value_labels[role] = value_label

            go_btn = ttk.Button(positions, text='Go', width=5,
                                command=lambda r=role: self._go_to_position(r))
            go_btn.grid(row=i, column=2, padx=(6, 0), pady=(2, 0))
            Tooltip(go_btn, 'Move the wheel to the saved %s position.' % role_name)
            self._connected_widgets.append(go_btn)

            set_btn = ttk.Button(positions, text='Set as %s' % role_name,
                                 command=lambda r=role: self._save_position(r))
            set_btn.grid(row=i, column=3, padx=(6, 0), pady=(2, 0))
            Tooltip(set_btn, WHEEL_ROLE_HELP[role])
            self._connected_widgets.append(set_btn)

        self.status = ttk.Label(self, justify='left', wraplength=650)
        self.status.grid(row=4, column=0, sticky='w', pady=(PAD, 0))

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        # Don't trust our memory of the last commanded angle - the physical wheel
        # may have been moved between sessions, or the unit reflashed.
        self._last_sent_angle = None
        state = 'normal' if connection else 'disabled'
        for w in self._connected_widgets:
            w['state'] = state
        if connection:
            self._refresh()
        else:
            self.config_label.config(text='Not connected.')
            self.status.config(text='')
            for label in self._role_value_labels.values():
                label.config(text='-')

    def _refresh(self):
        try:
            config = self.connection.get_config()
        except serial_io.SpecError as exc:
            self.config_label.config(text=str(exc))
            return
        self.unit_number.set(str(config.unit_number))
        state = 'configured' if config.configured else 'not yet configured (firmware defaults)'
        self.config_label.config(text=(
            'Unit #%d - Firmware v%s, %s' % (config.unit_number, config.firmware, state)))

        self._saved_angles = {'D': config.dark, 'I': config.irr, 'R': config.rad}
        for role, label in self._role_value_labels.items():
            angle = self._saved_angles[role]
            if angle is None or angle < WHEEL_MIN_ANGLE or angle > WHEEL_MAX_ANGLE:
                label.config(text='not set')
            else:
                label.config(text='%d deg' % angle)

    def _nudge(self, delta):
        self.angle.set(max(WHEEL_MIN_ANGLE, min(WHEEL_MAX_ANGLE, self.angle.get() + delta)))
        self._jog()

    def _on_scale_drag(self, value):
        # Snap to whole degrees during drag (ttk.Scale is continuous otherwise),
        # then debounce the actual serial send.
        rounded = round(float(value))
        if rounded != self.angle.get():
            self.angle.set(rounded)
        self._schedule_jog()

    def _schedule_jog(self):
        if self._jog_after_id is not None:
            self.after_cancel(self._jog_after_id)
        self._jog_after_id = self.after(150, self._jog_now)

    def _jog_now(self):
        self._jog_after_id = None
        self._jog()

    def _jog(self):
        if self._jog_after_id is not None:
            self.after_cancel(self._jog_after_id)
            self._jog_after_id = None
        angle = max(WHEEL_MIN_ANGLE, min(WHEEL_MAX_ANGLE, int(self.angle.get())))
        self.angle.set(angle)
        if angle == self._last_sent_angle:
            # ttk.Scale re-fires its `command` on ANY change to its variable,
            # including .set() calls from _nudge/_save_position/Go. Without this
            # guard, a manual action can trigger a second redundant send after
            # _jog() already moved the servo to that same angle.
            return
        try:
            self.connection.jog_wheel(angle)
        except serial_io.SpecError as exc:
            self.status.config(text=str(exc))
            return
        self._last_sent_angle = angle

    def _save_unit(self):
        try:
            self.connection.set_unit_number(int(self.unit_number.get()))
        except (serial_io.SpecError, ValueError) as exc:
            self.status.config(text=str(exc))
            return
        self.status.config(text='Unit number saved.')
        self._refresh()

    def _save_position(self, role):
        try:
            self.connection.save_wheel_position(role)
        except serial_io.SpecError as exc:
            self.status.config(text=str(exc))
            return
        self.status.config(text='Position saved at %d degrees.' % self.angle.get())
        self._refresh()

    def _go_to_position(self, role):
        angle = self._saved_angles.get(role)
        role_name = WHEEL_ROLE_NAMES[role]
        if angle is None or angle < WHEEL_MIN_ANGLE or angle > WHEEL_MAX_ANGLE:
            self.status.config(text=('%s position has not been saved yet - move the wheel '
                                     'and click "Set as %s" first.' % (role_name, role_name)))
            return
        self.angle.set(angle)
        self._jog()
        self.status.config(text='Moved to saved %s position (%d degrees).' % (role_name, angle))


class LinearisationTab(ttk.Frame):
    """Fit linCoefs by measuring one steady source across a range of integration times."""

    def __init__(self, parent, connection, store):
        super().__init__(parent, padding=PAD)
        self.connection = connection
        self.store = store
        self.mode = tk.StringVar(value='r')
        self.result = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, wraplength=650, justify='left', text=(
            'Point the OSpRad at a steady, non-flickering light source - daylight or an '
            'incandescent lamp work well, but avoid most LED and fluorescent lighting. '
            'Keep the unit and the light still throughout. The wizard measures the same '
            'source at a range of integration times and fits the linearisation curve so '
            'that doubling the exposure doubles the reported signal.')).grid(
                row=0, column=0, sticky='w')

        controls = ttk.Frame(self)
        controls.grid(row=1, column=0, sticky='ew', pady=(PAD, 0))
        ttk.Radiobutton(controls, text='Radiance', variable=self.mode, value='r').grid(row=0, column=0)
        ttk.Radiobutton(controls, text='Irradiance', variable=self.mode, value='i').grid(
            row=0, column=1, padx=(PAD, 0))
        self.run_button = ttk.Button(controls, text='Run measurements', command=self._run)
        self.run_button.grid(row=0, column=2, padx=(PAD * 2, 0))
        self.save_button = ttk.Button(controls, text='Save', command=self._save, state='disabled')
        self.save_button.grid(row=0, column=3, padx=(PAD, 0))

        self.status = ttk.Label(self, justify='left', wraplength=650)
        self.status.grid(row=2, column=0, sticky='w', pady=(PAD, 0))

        plot_frame = ttk.Frame(self)
        plot_frame.grid(row=4, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.plot = plotting.SpectrumPlot(plot_frame)
        self.plot.widget.grid(row=0, column=0, sticky='nsew')

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        self.run_button['state'] = 'normal' if connection else 'disabled'
        if not connection:
            self.status.config(text='Not connected.')

    def _run(self):
        self.run_button['state'] = 'disabled'
        self.save_button['state'] = 'disabled'
        try:
            samples = self._collect()
        except serial_io.SpecError as exc:
            self.status.config(text=str(exc))
            return
        finally:
            self.run_button['state'] = 'normal'

        if len(samples) < 3:
            self.status.config(text='Not enough usable measurements - try a brighter or '
                                    'dimmer source so the sensor is neither dark nor saturated.')
            return

        unit_number = samples[0][2]
        try:
            coefs = self._fit(samples)
        except CalibrationFitError as exc:
            self.status.config(text=str(exc))
            return
        self.result = (unit_number, coefs)
        self.status.config(text=('Unit #%d: fitted a = %.5f, b = %.5f from %d integration '
                                 'times. A good fit gives near-horizontal lines below.'
                                 % (unit_number, coefs[0], coefs[1], len(samples))))
        self._show(samples, coefs)
        self.save_button['state'] = 'normal'

    def _collect(self):
        mode = self.mode.get()

        # Let the unit pick a well-exposed integration time, then sweep around it so
        # the ladder suits the source in front of the sensor rather than assuming one.
        self.status.config(text='Finding a good exposure...')
        self.update_idletasks()
        self.connection.set_integration_time(0)
        reference = self.connection.measure(mode)

        int_times = sorted({max(1, int(round(reference.int_time * f)))
                            for f in (0.05, 0.1, 0.2, 0.35, 0.55, 0.75, 1.0)})

        samples = []
        for index, int_time in enumerate(int_times, 1):
            self.status.config(text='Measuring %d of %d at %d ms...'
                                    % (index, len(int_times), int_time))
            self.update_idletasks()
            self.connection.set_integration_time(int_time)
            measurement = self.connection.measure(mode)
            counts = np.asarray(measurement.raw_counts)
            if measurement.saturated > 2 or counts.max() < 20:
                continue  # over-exposed or too dark to be useful
            samples.append((int_time, counts, measurement.unit_number))

        self.connection.set_integration_time(0)
        self.status.config(text='')
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
        """Fit b so that signal per millisecond is flat across integration times.

        Only b changes the shape of the correction; a is a pure scale factor that
        cancels out against the sensitivity calibration, so it is pinned by the
        convention that the correction is 1.0 at the saturation threshold.
        """
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

        fit = least_squares(residuals, x0=[3.0], bounds=([1e-3], [1e6]))
        b = float(fit.x[0])
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
            messagebox.showerror('OSpRad', (
                'Unit #%d has no calibration data yet. Add its wavelength coefficients and '
                'sensitivity curves first, then save the linearisation.' % unit_number))
            return

        if not messagebox.askokcancel('OSpRad', (
                'Replace the linearisation coefficients for unit #%d?\n\n'
                'Old: a = %.5f, b = %.5f\nNew: a = %.5f, b = %.5f\n\n'
                'Measurements are scaled by these coefficients, so afterwards you should '
                're-derive the spectral sensitivity, or rescale it against a known reading '
                'on the Spectral sensitivity tab.'
                % (unit_number, calib.lin_coefs[0], calib.lin_coefs[1], coefs[0], coefs[1]))):
            return

        # linCoefs set the overall scale of the linearised signal, so the sensitivity
        # curves are only valid for the coefficients they were derived against.
        calib.lin_coefs = coefs
        self.store.save_unit(calib)
        self.status.config(text=('Saved linearisation coefficients for unit #%d. Re-check the '
                                 'spectral sensitivity next.' % unit_number))


class SensitivityTab(ttk.Frame):
    """Import, rescale, or derive radSens / irrSens."""

    def __init__(self, parent, connection, store):
        super().__init__(parent, padding=PAD)
        self.connection = connection
        self.store = store
        self.mode = tk.StringVar(value='r')
        self.sigma = tk.StringVar(value='2')
        self.reference_value = tk.StringVar()
        self.pending = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        head = ttk.Frame(self)
        head.grid(row=0, column=0, sticky='ew')
        ttk.Label(head, text='Calibrating:').grid(row=0, column=0)
        ttk.Radiobutton(head, text='Radiance', variable=self.mode, value='r').grid(
            row=0, column=1, padx=(PAD, 0))
        ttk.Radiobutton(head, text='Irradiance', variable=self.mode, value='i').grid(
            row=0, column=2, padx=(PAD, 0))

        importer = ttk.LabelFrame(self, text='Import a sensitivity curve', padding=PAD)
        importer.grid(row=1, column=0, sticky='ew', pady=(PAD, 0))
        ttk.Label(importer, wraplength=620, justify='left', text=(
            'Load %d sensitivity values from a text or CSV file - for example exported '
            'from the "sensitivity FINAL" sheet of calibration_calculations.ods.' % PIXELS
        )).grid(row=0, column=0, sticky='w')
        self.import_button = ttk.Button(importer, text='Choose file...', command=self._import)
        self.import_button.grid(row=1, column=0, sticky='w', pady=(6, 0))

        rescale = ttk.LabelFrame(self, text='Rescale against a known reading', padding=PAD)
        rescale.grid(row=2, column=0, sticky='ew', pady=(PAD, 0))
        ttk.Label(rescale, wraplength=620, justify='left', text=(
            'Keeps the existing spectral shape but corrects its overall level. Measure a '
            'source whose luminance (cd/sqm) or illuminance (lux) you already know, and '
            'enter that reference value.')).grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Entry(rescale, textvariable=self.reference_value, width=12).grid(
            row=1, column=0, sticky='w', pady=(6, 0))
        self.rescale_button = ttk.Button(rescale, text='Measure and rescale', command=self._rescale)
        self.rescale_button.grid(row=1, column=1, sticky='w', padx=PAD, pady=(6, 0))

        derive = ttk.LabelFrame(self, text='Derive from a reference spectrum (advanced)',
                                padding=PAD)
        derive.grid(row=3, column=0, sticky='ew', pady=(PAD, 0))
        ttk.Label(derive, wraplength=620, justify='left', text=(
            'Requires a reference spectroradiometer. Provide the true spectrum of a light '
            'source as a two-column file (wavelength in nm, then W/(sr*sqm*nm) for radiance '
            'or W/(sqm*nm) for irradiance). The OSpRad then measures the same source and '
            'the ratio gives its spectral sensitivity.')).grid(
                row=0, column=0, columnspan=3, sticky='w')
        ttk.Label(derive, text='Smoothing sigma').grid(row=1, column=0, sticky='w', pady=(6, 0))
        ttk.Entry(derive, textvariable=self.sigma, width=6).grid(
            row=1, column=1, sticky='w', pady=(6, 0))
        self.derive_button = ttk.Button(derive, text='Choose reference spectrum and measure...',
                                        command=self._derive)
        self.derive_button.grid(row=1, column=2, sticky='w', padx=PAD, pady=(6, 0))

        actions = ttk.Frame(self)
        actions.grid(row=4, column=0, sticky='ew', pady=(PAD, 0))
        actions.columnconfigure(0, weight=1)
        self.status = ttk.Label(actions, justify='left', wraplength=560)
        self.status.grid(row=0, column=0, sticky='w')
        self.save_button = ttk.Button(actions, text='Save', command=self._save, state='disabled')
        self.save_button.grid(row=0, column=1, sticky='e')

        plot_frame = ttk.Frame(self)
        plot_frame.grid(row=5, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.plot = plotting.SpectrumPlot(plot_frame)
        self.plot.widget.grid(row=0, column=0, sticky='nsew')

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        state = 'normal' if connection else 'disabled'
        for w in (self.import_button, self.rescale_button, self.derive_button):
            w['state'] = state
        if not connection:
            self.status.config(text='Not connected.')

    def _current_unit(self):
        config = self.connection.get_config()
        return self.store.get(config.unit_number)

    def _check_exposure(self, measurement):
        """Reject over-exposed measurements - saturated photosites read low and would
        silently corrupt a calibration."""
        if measurement.saturated > 0:
            self.status.config(text=(
                'Measurement is over-exposed (%g saturated photosites), so it cannot be '
                'used for calibration. Dim the source, or set a shorter integration time '
                'on the main window.' % measurement.saturated))
            return False
        return True

    def _import(self):
        path = filedialog.askopenfilename(
            filetypes=[('Data files', '*.csv *.txt *.tsv'), ('All files', '*.*')])
        if not path:
            return
        values = _read_number_file(path)
        if len(values) != PIXELS:
            messagebox.showerror('OSpRad', 'Expected %d values but the file has %d.'
                                 % (PIXELS, len(values)))
            return
        try:
            calib = self._current_unit()
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.config(text=str(exc))
            return
        self._stage(calib, values, 'Imported %d values from file.' % PIXELS)

    def _rescale(self):
        try:
            reference = float(self.reference_value.get())
            if reference <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('OSpRad', 'Enter the known reference value first.')
            return

        mode = self.mode.get()
        try:
            calib = self._current_unit()
            self.status.config(text='Measuring...')
            self.update_idletasks()
            measurement = self.connection.measure(mode)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.config(text=str(exc))
            return
        if not self._check_exposure(measurement):
            return

        flux = calib.to_flux(measurement.raw_counts, mode, measurement.int_time)
        measured = calib.luminance(flux)
        if measured <= 0:
            self.status.config(text='Measured signal is zero - check the shutter positions.')
            return

        factor = measured / reference
        values = [v * factor for v in calib.sensitivity(mode)]
        self._stage(calib, values, 'Measured %.4g, reference %.4g - sensitivity scaled by %.4f.'
                    % (measured, reference, factor))

    def _derive(self):
        path = filedialog.askopenfilename(
            title='Reference spectrum (wavelength, flux)',
            filetypes=[('Data files', '*.csv *.txt *.tsv'), ('All files', '*.*')])
        if not path:
            return
        numbers = _read_number_file(path)
        if len(numbers) < 4 or len(numbers) % 2:
            messagebox.showerror('OSpRad', 'Expected two columns: wavelength and flux.')
            return
        reference = np.array(numbers).reshape(-1, 2)
        reference = reference[reference[:, 0].argsort()]

        mode = self.mode.get()
        try:
            calib = self._current_unit()
            self.status.config(text='Measuring the reference source...')
            self.update_idletasks()
            measurement = self.connection.measure(mode)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.config(text=str(exc))
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
            sigma = float(self.sigma.get())
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
        self.pending = (calib, self.mode.get(), list(values))
        self.status.config(text=message + ' Review the curve, then Save.')
        self.save_button['state'] = 'normal'
        calib._derive()
        self.plot.update(calib.wavelength, values, None,
                         '%s sensitivity - unit #%d'
                         % ('Irradiance' if self.mode.get() == 'i' else 'Radiance',
                            calib.unit_number))

    def _save(self):
        calib, mode, values = self.pending
        if mode == 'i':
            calib.irr_sens = values
        else:
            calib.rad_sens = values
        self.store.save_unit(calib)
        self.save_button['state'] = 'disabled'
        self.status.config(text='Saved %s sensitivity for unit #%d.'
                           % ('irradiance' if mode == 'i' else 'radiance', calib.unit_number))


class CosineResponseTab(ttk.Frame):
    """Checks the cosine diffuser's angular response (not its colour - that's the
    Monitor calibration tab's job). Measures Irradiance at a series of incidence
    angles against a fixed, distant source, then plots the response normalised to
    the 0-degree reading against the ideal cos(angle) falloff a well-behaved cosine
    corrector should follow."""

    # Common, easy-to-eyeball angles - the labels give a plain-language sense of
    # "how far off straight-on" without needing a protractor.
    PRESET_ANGLES = ((0, 'Straight on'), (15, 'Slight angle'), (30, 'A third turn'),
                     (45, 'Diagonal'), (60, 'Steep angle'), (75, 'Nearly edge-on'),
                     (90, 'Edge-on'))

    def __init__(self, parent, connection, store):
        super().__init__(parent, padding=PAD)
        self.connection = connection
        self.store = store
        self.angle_deg = tk.StringVar(value='0')
        self.results = []  # [(angle_deg, lux), ...]
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        ttk.Label(self, wraplength=650, justify='left', text=(
            'Point the sensor at a small, distant, steady light source - direct '
            'sunlight on a clear day is ideal; indoors, a bare bulb several metres '
            'away works - and keep the same source and distance throughout. No '
            'protractor needed: hold your fist at arm\'s length - it spans roughly '
            '10 degrees, so 3 fists is about 30, 4-5 fists about 45, 6 about 60. Pick '
            'the angle you\'re aiming for below, point the sensor there by eye, then '
            'press Measure. Start with "Straight on" - that reading is the reference '
            'every other angle gets compared against. A well-behaved cosine diffuser '
            'reads close to cos(angle) of that reference - 87% at 30 degrees, 71% at '
            '45, 50% at 60. Consistent under-response at high angles is the classic '
            'failure mode (tape too thick, or shadowed by the housing).')).grid(
                row=0, column=0, sticky='w')

        picker = ttk.Frame(self)
        picker.grid(row=1, column=0, sticky='w', pady=(PAD, 0))

        self.diagram = tk.Canvas(picker, width=150, height=130, highlightthickness=0)
        self.diagram.grid(row=0, column=0, padx=(0, PAD))

        preset_grid = ttk.Frame(picker)
        preset_grid.grid(row=0, column=1, sticky='w')
        self._preset_buttons = []
        for i, (angle, label) in enumerate(self.PRESET_ANGLES):
            r, c = divmod(i, 4)
            btn = ttk.Button(preset_grid, text='%d°\n%s' % (angle, label), width=11,
                             command=lambda a=angle: self._set_angle(a))
            btn.grid(row=r, column=c, padx=(0, 4), pady=(0, 4))
            self._preset_buttons.append(btn)

        entry_row = ttk.Frame(self)
        entry_row.grid(row=2, column=0, sticky='w', pady=(PAD, 0))
        ttk.Label(entry_row, text='Angle (degrees)').grid(row=0, column=0)
        angle_entry = ttk.Entry(entry_row, textvariable=self.angle_deg, width=6)
        angle_entry.grid(row=0, column=1, padx=(6, 0))
        Tooltip(angle_entry, 'Type an exact angle instead of a preset above, if you '
                             'have a protractor or jig to position the sensor precisely.')
        angle_entry.bind('<KeyRelease>', self._on_angle_typed)
        self.measure_button = ttk.Button(entry_row, text='Measure', command=self._measure)
        self.measure_button.grid(row=0, column=2, padx=(PAD, 0))
        self.clear_button = ttk.Button(entry_row, text='Clear results', command=self._clear)
        self.clear_button.grid(row=0, column=3, padx=(6, 0))

        self.status = ttk.Label(self, justify='left', wraplength=650)
        self.status.grid(row=3, column=0, sticky='w', pady=(PAD, 0))

        tree_frame = ttk.Frame(self)
        tree_frame.grid(row=4, column=0, sticky='ew', pady=(PAD, 0))
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            tree_frame, columns=('angle', 'lux', 'ratio', 'ideal', 'deviation'),
            show='headings', height=6, selectmode='browse')
        for col, text, width in (('angle', 'Angle', 60), ('lux', 'Lux', 90),
                                  ('ratio', 'Measured ratio', 110),
                                  ('ideal', 'Ideal cos(angle)', 110),
                                  ('deviation', 'Deviation', 90)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor='center')
        self.tree.grid(row=0, column=0, sticky='ew')
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.tree.yview)
        tree_scroll.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=tree_scroll.set)

        plot_frame = ttk.Frame(self)
        plot_frame.grid(row=5, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.plot = plotting.SpectrumPlot(plot_frame)
        self.plot.widget.grid(row=0, column=0, sticky='nsew')

        self._connected_widgets = [angle_entry, self.measure_button] + self._preset_buttons
        self._draw_diagram(0)
        self.set_connection(connection)
        self._refresh()

    def _set_angle(self, angle):
        self.angle_deg.set(str(angle))
        self._draw_diagram(angle)
        if angle == 0:
            self.status.config(text='Point the sensor straight at the light source, then '
                                    'press Measure.')
        else:
            self.status.config(text=('Point the sensor about %d\N{DEGREE SIGN} away from '
                                     'straight-on (see the diagram), then press Measure.'
                                     % angle))

    def _on_angle_typed(self, event):
        try:
            angle = float(self.angle_deg.get())
        except ValueError:
            return
        self._draw_diagram(angle)

    def _draw_diagram(self, angle):
        """Top-down schematic: sensor at the centre, dashed 0-degree reference line,
        and a solid line + light-source icon at the requested angle. Easier to
        eyeball than the number alone when positioning by hand."""
        c = self.diagram
        c.delete('all')
        cx, cy = 75, 100
        radius = 65
        c.create_line(cx, cy, cx, cy - radius, dash=(3, 2), fill='#999999')
        rad = math.radians(min(max(angle, 0), 90))
        ex = cx + radius * math.sin(rad)
        ey = cy - radius * math.cos(rad)
        if angle > 0.5:
            c.create_arc(cx - 24, cy - 24, cx + 24, cy + 24, start=90 - min(angle, 90),
                        extent=min(angle, 90), style='arc', outline='#2a9d8f', width=2)
        c.create_line(cx, cy, ex, ey, width=2, fill='#e9a23a')
        c.create_oval(ex - 6, ey - 6, ex + 6, ey + 6, fill='#e9a23a', outline='')
        c.create_text(ex, ey - 12, text='light', fill='#e9a23a', font=('TkDefaultFont', 8))
        c.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill='#2e86ab', outline='')
        c.create_text(cx, cy + 14, text='sensor', fill='#7a7a7a', font=('TkDefaultFont', 8))
        c.create_text(cx, cy + 28, text='%.0f\N{DEGREE SIGN}' % angle,
                     fill='#1c1c1c', font=('TkDefaultFont', 10, 'bold'))

    def set_connection(self, connection):
        self.connection = connection
        state = 'normal' if connection else 'disabled'
        for w in self._connected_widgets:
            w['state'] = state
        if not connection:
            self.status.config(text='Not connected.')

    def _measure(self):
        try:
            angle = float(self.angle_deg.get())
            if angle < 0:
                raise ValueError
        except ValueError:
            self.status.config(text='Angle must be a number >= 0.')
            return

        try:
            measurement = self.connection.measure('i')
            calib = self.store.get(measurement.unit_number)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.status.config(text=str(exc))
            return
        flux = calib.to_flux(measurement.raw_counts, 'i', measurement.int_time)
        lux = calib.luminance(flux)

        self.results.append((angle, lux))
        self.results.sort(key=lambda r: r[0])
        if any(a == 0 for a, _ in self.results):
            self.status.config(text='Measured %.1f lux at %.1f degrees.' % (lux, angle))
        else:
            self.status.config(text=(
                'Measured %.1f lux at %.1f degrees. Measure at 0 degrees (facing the '
                'source directly) too, to use as the reference.' % (lux, angle)))
        self._refresh()

    def _clear(self):
        self.results = []
        self.status.config(text='')
        self._refresh()

    def _refresh(self):
        self.tree.delete(*self.tree.get_children())
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
            self.tree.insert('', 'end', values=(
                '%.1f' % angle, '%.3g' % lux, ratio_text, '%.3f' % ideal, deviation))

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
