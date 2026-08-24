# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad
#
# Monitor calibration: steps a fullscreen patch through each R/G/B channel at a
# ladder of levels, measures the spectrum at each step, and exports either a plain
# CSV or an already-fitted PsychCal-format .mat file (loadable directly in PTB via
# LoadCalFile - no MATLAB-side step). Linear device model (P_device/T_device/raw
# gamma weights) ports PTB's CalibrateFitLinMod/FindModelWeights exactly. Tone
# curve is a monotone PCHIP, NOT a port of PTB's CalibrateFitGamma - that one's
# 'crtPolyLinear' algorithm wasn't fully confirmable from source so reimplementing
# it risked silently producing a plausible-but-wrong curve; PCHIP is faithful to
# the measured points and is labelled 'OSpRad-pchip' in cal.describe.gamma.fitType
# so the choice is inspectable. See psychtoolbox/README.md for the user-facing
# overview.

import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import numpy as np
from scipy.interpolate import PchipInterpolator

import calibration
import plotting
import serial_io
from calibration_wizard import Tooltip, WHEEL_MAX_ANGLE, WHEEL_MIN_ANGLE

PAD = 10

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
    P_device is each channel's spectrum at its highest level, raw_gamma weights are the
    least-squares projection of each level's spectrum onto that basis (`B \\ input` in MATLAB,
    `lstsq` here).
    """
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


def fit_gamma_curve(gamma_input, raw_gamma, n_output=1024):
    """Monotone PCHIP interpolation through the measured (input, raw-gamma) points per
    channel (see module docstring for why this isn't PTB's CalibrateFitGamma). Values
    forced non-decreasing before fitting; anchored at (0, 0) since level 0 is covered
    by the separate ambient measurement, not by the gamma sweep itself.
    """
    gamma_output = np.linspace(0.0, 1.0, n_output)
    n_channels = raw_gamma.shape[1]
    table = np.zeros((n_output, n_channels))
    for ch in range(n_channels):
        x = np.concatenate([[0.0], gamma_input])
        y = np.concatenate([[0.0], np.clip(raw_gamma[:, ch], 0.0, None)])
        y = np.maximum.accumulate(y)
        if y[-1] > 0:
            y = y / y[-1]
        interp = PchipInterpolator(x, y, extrapolate=False)
        fitted = interp(gamma_output)
        fitted = np.nan_to_num(fitted, nan=0.0)
        table[:, ch] = np.clip(fitted, 0.0, 1.0)
    return gamma_output.reshape(-1, 1), table


def build_t_device(wavelength_grid):
    """CIE 1931 XYZ colour-matching functions sampled at wavelength_grid, as PTB's
    T_device/T_ambient fields. Reuses calibration.py's analytic piecewise-Gaussian
    CIE approximation - close to PTB's tabulated data but not guaranteed bit-identical."""
    x = np.array([calibration._piecewise_gaussian(w, calibration.CIE_X_COEFS)
                 for w in wavelength_grid])
    y = np.array([calibration._piecewise_gaussian(w, calibration.CIE_Y_COEFS)
                 for w in wavelength_grid])
    z = np.array([calibration._piecewise_gaussian(w, calibration.CIE_Z_COEFS)
                 for w in wavelength_grid])
    return np.vstack([x, y, z])


class PatchWindow(tk.Toplevel):
    """Borderless fullscreen solid-colour window for the sweep. Status text is drawn
    over a small dark backing rectangle so it stays readable on any patch colour."""

    def __init__(self, master, on_cancel):
        super().__init__(master)
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry('%dx%d+0+0' % (w, h))
        self.canvas = tk.Canvas(self, bg='black', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self._backing = self.canvas.create_rectangle(0, 0, 320, 40, fill='black', outline='')
        self._status_text = self.canvas.create_text(
            10, 10, anchor='nw', fill='white', font=('TkDefaultFont', 12), text='')
        self._on_cancel = on_cancel
        self.bind('<Escape>', lambda e: self._on_cancel())
        self.focus_force()

    def set_color(self, r, g, b):
        self.canvas.configure(bg='#%02x%02x%02x' % (r, g, b))

    def set_status(self, text):
        self.canvas.itemconfig(self._status_text, text=text)
        bbox = self.canvas.bbox(self._status_text)
        if bbox:
            self.canvas.coords(self._backing, bbox[0] - 6, bbox[1] - 4, bbox[2] + 6, bbox[3] + 4)


class MonitorCalibrationTab(ttk.Frame):
    """Sweeps a fullscreen patch through R/G/B gun levels, measuring the display's
    spectrum (Radiance mode - a narrow, aimable field of view pointed at the screen)
    at each step, then exports either a plain CSV or a Psychtoolbox-ready raw
    calibration .mat file. See the module docstring for why the .mat export is
    deliberately unfitted raw data rather than a from-scratch gamma/model fit."""

    CHANNEL_NAMES = ('Red', 'Green', 'Blue')
    CHANNEL_COLORS = ('#d1495b', '#2a9d8f', '#2e86ab')

    def __init__(self, parent, connection, store):
        super().__init__(parent, padding=PAD)
        self.connection = connection
        self.store = store
        self.n_levels = tk.StringVar(value='11')
        self.settle_ms = tk.StringVar(value='300')
        self.comment = tk.StringVar()
        self._cancelled = False
        self._patch_window = None
        self._result = None  # set on a completed sweep; consumed by the Export buttons
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        ttk.Label(self, wraplength=650, justify='left', text=(
            'Point the OSpRad at the screen (Radiance mode - a narrow field of view, so '
            'aim it at roughly where stimuli will appear) and press Start. A fullscreen '
            'patch steps through black, then each of Red/Green/Blue at a ladder of '
            'levels, measuring the spectrum at every step - the same workflow as '
            "Psychtoolbox's CalibrateMonSpd. Takes a few minutes; each step needs a moment "
            'to auto-expose, longer for dim levels.')).grid(row=0, column=0, sticky='w')

        settings = ttk.Frame(self)
        settings.grid(row=1, column=0, sticky='w', pady=(PAD, 0))
        settings.columnconfigure(4, weight=1)

        levels_label = ttk.Label(settings, text='Levels per channel')
        levels_label.grid(row=0, column=0, sticky='w')
        levels_entry = ttk.Entry(settings, textvariable=self.n_levels, width=6)
        levels_entry.grid(row=0, column=1, sticky='w', padx=(6, 0))
        levels_tip = ('How many brightness steps to measure per channel (evenly spaced '
                     'from just above black to full). More steps give a better gamma '
                     'fit but take proportionally longer.')
        Tooltip(levels_label, levels_tip)
        Tooltip(levels_entry, levels_tip)

        settle_label = ttk.Label(settings, text='Settle time (ms)')
        settle_label.grid(row=0, column=2, sticky='w', padx=(PAD, 0))
        settle_entry = ttk.Entry(settings, textvariable=self.settle_ms, width=6)
        settle_entry.grid(row=0, column=3, sticky='w', padx=(6, 0))
        settle_tip = ('Pause after each patch colour change, before measuring, so the '
                     'display has finished redrawing. Increase if your monitor or '
                     'window compositor is slow to settle.')
        Tooltip(settle_label, settle_tip)
        Tooltip(settle_entry, settle_tip)

        btn_row = ttk.Frame(self)
        btn_row.grid(row=2, column=0, sticky='w', pady=(PAD, 0))
        self.start_button = ttk.Button(btn_row, text='Start', command=self._start)
        self.start_button.grid(row=0, column=0)
        self.cancel_button = ttk.Button(btn_row, text='Cancel', command=self._cancel,
                                        state='disabled')
        self.cancel_button.grid(row=0, column=1, padx=(6, 0))
        self.export_csv_button = ttk.Button(btn_row, text='Export CSV...',
                                            command=self._export_csv, state='disabled')
        self.export_csv_button.grid(row=0, column=2, padx=(PAD * 2, 0))
        self.export_ptb_button = ttk.Button(btn_row, text='Export for Psychtoolbox...',
                                            command=self._export_ptb, state='disabled')
        self.export_ptb_button.grid(row=0, column=3, padx=(6, 0))
        Tooltip(self.export_ptb_button, (
            'Writes an already-fitted PsychCal-format .mat file - P_device/T_device '
            '(linear device model, ported from PTB\'s own CalibrateFitLinMod) and '
            'gammaTable/gammaInput (a monotone PCHIP fit through the measured points, '
            'not a port of PTB\'s own CalibrateFitGamma - see the module docstring in '
            'monitor_calibration.py). Load directly with cal = LoadCalFile(...) in '
            'Psychtoolbox - no MATLAB-side step needed.'))

        self.status = ttk.Label(self, justify='left', wraplength=650)
        self.status.grid(row=3, column=0, sticky='w', pady=(PAD, 0))

        plot_frame = ttk.Frame(self)
        plot_frame.grid(row=4, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(0, weight=1)
        self.plot = plotting.SpectrumPlot(plot_frame)
        self.plot.widget.grid(row=0, column=0, sticky='nsew')

        self.set_connection(connection)

    def set_connection(self, connection):
        self.connection = connection
        self.start_button['state'] = 'normal' if connection else 'disabled'
        if not connection:
            self.status.config(text='Not connected.')

    # ---------------- sweep ----------------

    def _start(self):
        try:
            n_levels = int(self.n_levels.get())
            settle_ms = max(0, int(self.settle_ms.get()))
            if n_levels < 2:
                raise ValueError
        except ValueError:
            self.status.config(text='Levels per channel must be a whole number >= 2.')
            return

        try:
            config = self.connection.get_config()
        except serial_io.SpecError as exc:
            self.status.config(text=str(exc))
            return

        # Monitor calibration is a downstream USE of an already-calibrated device, not
        # part of calibrating the device itself - it has to be blocked outright, not just
        # warned-and-allowed, until both preconditions below hold. See the wheel-position
        # pitfall diagnosed earlier this session: if Dark and Radiance resolve to the same
        # (or any unsaved) physical angle, every reading is dark-minus-itself noise.
        angles = (config.dark, config.irr, config.rad)
        if any(a < WHEEL_MIN_ANGLE or a > WHEEL_MAX_ANGLE for a in angles):
            messagebox.showerror('OSpRad', (
                "This unit's shutter wheel positions haven't all been saved yet. Finish "
                'Calibration -> Unit & wheel setup first - without a real Dark position, '
                'the dark-frame subtraction has nothing meaningful to subtract, so every '
                'measurement here would be near-zero noise, not a real spectrum.'))
            return

        try:
            self.store.get(config.unit_number)
        except calibration.CalibrationError:
            messagebox.showerror('OSpRad', (
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
        self.start_button['state'] = 'disabled'
        self.cancel_button['state'] = 'normal'
        self.export_csv_button['state'] = 'disabled'
        self.export_ptb_button['state'] = 'disabled'
        self._result = None

        self.connection.set_integration_time(0)  # let each step auto-expose
        self._patch_window = PatchWindow(self.winfo_toplevel(), on_cancel=self._cancel)
        self._advance_sweep()

    def _build_step_list(self, n_levels):
        """('ambient' | channel-index, (r,g,b), status text) per step. Gamma levels
        exclude 0 (linspace(0,1,n+1)[1:]), matching PTB's own rawGammaInput convention -
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
        self.status.config(text='Cancelling - finishing current measurement...')

    def _advance_sweep(self):
        """Runs on the main thread. Only ever waits via self.after() (never blocks),
        so the event loop stays responsive to Escape/Cancel for the whole sweep -
        the worst case for Cancel to take effect is the one measurement already
        in flight (a blocking serial call can't be safely interrupted mid-read),
        not the rest of the sweep."""
        if self._cancelled:
            self._finish_sweep(outcome='cancelled')
            return
        if self._sweep_index >= len(self._sweep_steps):
            self._finish_sweep(outcome='done')
            return
        kind, rgb, text = self._sweep_steps[self._sweep_index]
        self._patch_window.set_color(*rgb)
        self._patch_window.set_status('%s\n%s' % (text, rgb))
        self.status.config(text=text)
        self.after(self._settle_ms, self._start_measurement, kind)

    def _start_measurement(self, kind):
        if self._cancelled:
            self._finish_sweep(outcome='cancelled')
            return
        connection, store = self.connection, self.store
        thread = threading.Thread(target=self._measure_worker, args=(kind, connection, store),
                                  daemon=True)
        thread.start()

    def _measure_worker(self, kind, connection, store):
        """Background thread: hardware I/O and calibration math only - never touches
        any Tk widget directly (Tkinter is not thread-safe). Results are handed back
        to the main thread via self.after()."""
        try:
            measurement = connection.measure('r')
            calib = store.get(measurement.unit_number)
        except (serial_io.SpecError, calibration.CalibrationError) as exc:
            self.after(0, self._measurement_failed, str(exc))
            return
        flux = calib.to_flux(measurement.raw_counts, 'r', measurement.int_time)
        _, resampled = resample_to_ptb_grid(calib.wavelength, flux, DEFAULT_PTB_S)
        self.after(0, self._measurement_done, kind, resampled)

    def _measurement_done(self, kind, spd):
        if kind == 'ambient':
            self._sweep_results['ambient'] = spd
        else:
            self._sweep_results['mon_by_channel'][kind].append(spd)
        self._sweep_index += 1
        self._advance_sweep()

    def _measurement_failed(self, message):
        self.status.config(text=message)
        self._cancelled = True
        self._finish_sweep(outcome='failed')

    def _finish_sweep(self, outcome):
        if self._patch_window is not None:
            self._patch_window.destroy()
            self._patch_window = None
        if self.connection is not None:
            self.connection.set_integration_time(0)
        self.start_button['state'] = 'normal' if self.connection else 'disabled'
        self.cancel_button['state'] = 'disabled'

        if outcome == 'failed':
            return  # status already set by _measurement_failed
        if outcome == 'cancelled':
            self.status.config(text='Cancelled.')
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
        self.status.config(text='Done - %d measurements. Export below, or run another sweep.'
                                % total_steps)
        self.export_csv_button['state'] = 'normal'
        self.export_ptb_button['state'] = 'normal'
        self._show_preview()

    # ---------------- preview ----------------

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

    # ---------------- export ----------------

    def _export_csv(self):
        if self._result is None:
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv',
                                            filetypes=[('CSV', '*.csv')])
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
        self.status.config(text='Saved %s' % path)

    def _export_ptb(self):
        if self._result is None:
            return
        try:
            import scipy.io as sio
        except ImportError:
            messagebox.showerror('OSpRad', 'scipy is required for the .mat export.')
            return
        path = filedialog.asksaveasfilename(defaultextension='.mat',
                                            filetypes=[('MATLAB file', '*.mat')])
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
                'program': 'OSpRad MonitorCalibrationTab (fit: linear device model '
                          'ported from PTB CalibrateFitLinMod; tone curve: monotone '
                          'PCHIP, NOT PTB CalibrateFitGamma - see monitor_calibration.py)',
                'date': time.strftime('%Y-%m-%d %H:%M:%S'),
                'comment': self.comment.get(),
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
        self.status.config(text=('Saved %s - a fitted PsychCal file, load directly with '
                                 'cal = LoadCalFile(...) in Psychtoolbox.' % path))
