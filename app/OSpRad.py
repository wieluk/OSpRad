# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad
#
# Run this file to launch the app. Keep it in the same folder as serial_io.py,
# calibration.py, plotting.py, datalog.py, analysis.py, calibration_wizard.py and
# calibration_data.csv - measurements are written to data.csv beside them.
# Requires OSpRad 3.x firmware on the Arduino Nano.

import os
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from matplotlib.backends.backend_tkagg import NavigationToolbar2Tk

import analysis
import calibration
import datalog
import plotting
import serial_io
from calibration_wizard import (CosineResponseTab, LinearisationTab, SensitivityTab,
                                Tooltip, UnitSetupTab)
from monitor_calibration import MonitorCalibrationTab

try:
    import sv_ttk
except ImportError:
    sv_ttk = None

# Resolved against this folder rather than CWD so the app behaves the same however
# it is launched (including from Pydroid 3 on Android).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, 'data.csv')
CALIBRATION_FILE = os.path.join(BASE_DIR, 'calibration_data.csv')

__version__ = '3.1.0'

PAD = 8

# Cap on the scrolling log widget, so an unattended long "Repeat every (s)" session
# doesn't grow its memory/redraw cost unboundedly.
LOG_MAX_LINES = 500

# 'debug' is extra-verbose wire-level detail - hidden by default but there for
# diagnosing "what is the app actually sending" questions.
LOG_LEVELS = ('debug', 'info', 'warning', 'error')
LOG_LEVEL_RANK = {level: i for i, level in enumerate(LOG_LEVELS)}


class OSpRadApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('OSpRad %s' % __version__)
        # Conservative small-phone-portrait floor; each tab also scrolls if its
        # content is still taller than this (see _make_scrollable).
        self.minsize(320, 480)

        self.dark_mode = tk.BooleanVar(value=False)
        if sv_ttk:
            sv_ttk.set_theme('light')

        self.connection = None
        self.store = calibration.CalibrationStore(CALIBRATION_FILE)
        self.measurement = None
        self.reading = None
        self._last_luminance = None

        self.save_label = tk.StringVar()
        self.int_time = tk.StringVar(value='0')
        self.min_scans = tk.StringVar(value='3')
        self.max_scans = tk.StringVar(value='50')
        self.repeat_time = tk.StringVar(value='300')
        self.repeat_irr = tk.IntVar(value=1)
        self.repeat_rad = tk.IntVar()
        self.log_level = tk.StringVar(value='info')
        self._motor_test_angle = 30

        self._repeat_running = False
        self._repeat_after_id = None
        self._countdown_after_id = None
        self._repeat_next_time = None

        self._prev_int_time = None
        self._prev_scans = None
        self._compared_offsets = set()
        self._scroll_canvases = []
        self._active_scroll_canvas = None

        self._build_styles()
        self._build_ui()
        self._load_saved_readings()
        self._paint_background()
        self.after(100, self._connect)

    # ---------------- setup ----------------

    def _paint_background(self):
        """Match the window, scroll canvases, plot canvas and log widget to the theme.

        Tk paints newly exposed areas with the toplevel's own background before the
        layout catches up, so leaving it unset shows a bare rectangle during resize.
        """
        style = ttk.Style(self)
        background = style.lookup('TFrame', 'background') or '#fafafa'
        foreground = style.lookup('TLabel', 'foreground') or '#1c1c1c'
        self.configure(background=background)
        for canvas in self._scroll_canvases:
            canvas.configure(background=background, highlightthickness=0)
        if getattr(self, 'plot', None):
            self.plot.widget.configure(background=background, highlightthickness=0, bd=0)
        if getattr(self, 'history_plot', None):
            self.history_plot.widget.configure(background=background, highlightthickness=0, bd=0)
        if getattr(self, 'log_text', None):
            self.log_text.configure(background=background, foreground=foreground,
                                    insertbackground=foreground)

    def _build_styles(self):
        style = ttk.Style(self)
        style.configure('Muted.TLabel', foreground='#7a7a7a')
        style.configure('Good.TLabel', foreground='#2a9d8f')
        style.configure('Bad.TLabel', foreground='#d1495b')
        style.configure('Warn.TLabel', foreground='#e9a23a')

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        notebook = ttk.Notebook(self)
        notebook.grid(row=0, column=0, sticky='nsew')
        notebook.add(self._build_main_tab(notebook), text='Main')
        notebook.add(self._build_history_tab(notebook), text='History')
        notebook.add(self._build_monitor_cal_tab(notebook), text='Monitor calibration')
        notebook.add(self._build_calibration_tab(notebook), text='Calibration')
        notebook.add(self._build_debug_tab(notebook), text='Debug')

    def _make_scrollable(self, parent, expand_row=None):
        """Wrap a canvas+scrollbar around a content frame so a tab taller than the
        window scrolls instead of clipping (common on a short Android screen). If
        expand_row is given, that row is stretched to fill the canvas height when the
        canvas is taller than the content's natural size - so the Main tab's plot still
        grows on a big desktop window, while the whole tab scrolls on a small one.
        Returns (outer_frame, content_frame).
        """
        outer = ttk.Frame(parent)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(0, weight=1)
        canvas = tk.Canvas(outer, highlightthickness=0)
        canvas.grid(row=0, column=0, sticky='nsew')
        vbar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        vbar.grid(row=0, column=1, sticky='ns')
        canvas.configure(yscrollcommand=vbar.set)

        content = ttk.Frame(canvas, padding=PAD)
        content.columnconfigure(0, weight=1)
        if expand_row is not None:
            content.rowconfigure(expand_row, weight=1)
        window_id = canvas.create_window((0, 0), window=content, anchor='nw')

        def on_content_configure(event):
            canvas.configure(scrollregion=canvas.bbox('all'))

        def on_canvas_configure(event):
            height = max(event.height, content.winfo_reqheight())
            canvas.itemconfig(window_id, width=event.width, height=height)

        content.bind('<Configure>', on_content_configure)
        canvas.bind('<Configure>', on_canvas_configure)
        canvas.bind('<Enter>', lambda e: self._bind_mousewheel(canvas))
        canvas.bind('<Leave>', lambda e: self._unbind_mousewheel())
        self._scroll_canvases.append(canvas)
        return outer, content

    # ---------------- Main tab ----------------

    def _build_main_tab(self, parent):
        outer, content = self._make_scrollable(parent, expand_row=10)

        conn_row = ttk.Frame(content)
        conn_row.grid(row=0, column=0, sticky='ew')
        conn_row.columnconfigure(0, weight=1)
        self.conn_status_label = ttk.Label(conn_row, text='Not connected.',
                                           style='Muted.TLabel', wraplength=300)
        self.conn_status_label.grid(row=0, column=0, sticky='w')
        self.bt_connect = ttk.Button(conn_row, text='Reconnect', command=self._connect, width=10)
        self.bt_connect.grid(row=0, column=1, sticky='e')

        ttk.Separator(content).grid(row=1, column=0, sticky='ew', pady=PAD)

        self.bt_rad = ttk.Button(content, text='Radiance', command=lambda: self._measure('r'))
        self.bt_rad.grid(row=2, column=0, sticky='ew', pady=(0, PAD // 2), ipady=6)

        self.bt_irr = ttk.Button(content, text='Irradiance', command=lambda: self._measure('i'))
        self.bt_irr.grid(row=3, column=0, sticky='ew', pady=PAD // 2, ipady=6)

        self.bt_save = ttk.Button(content, text='Save reading', command=self._save, state='disabled')
        self.bt_save.grid(row=4, column=0, sticky='ew', pady=PAD // 2, ipady=6)

        ttk.Label(content, text='Label').grid(row=5, column=0, sticky='w', pady=(PAD, 2))
        ttk.Entry(content, textvariable=self.save_label).grid(row=6, column=0, sticky='ew')

        # Stretch weight on column 2 (past the input) rather than column 1 (between
        # label and input) so the label+input pair stays packed on the left.
        settings = ttk.Frame(content)
        settings.grid(row=7, column=0, sticky='ew', pady=(PAD, 0))
        settings.columnconfigure(2, weight=1)

        int_time_label = ttk.Label(settings, text='Integration time (ms), 0 = auto')
        int_time_label.grid(row=0, column=0, sticky='w')
        int_time_entry = ttk.Entry(settings, textvariable=self.int_time, width=8)
        int_time_entry.grid(row=0, column=1, sticky='w', padx=(PAD, 0))
        int_time_tip = ('How long each scan collects light for. Leave at 0 to let the '
                        'firmware pick automatically by ramping up exposure until just '
                        'below saturation (recommended). Set a fixed value only if you '
                        'need identical exposure across repeated measurements.')
        Tooltip(int_time_label, int_time_tip)
        Tooltip(int_time_entry, int_time_tip)

        scans_label = ttk.Label(settings, text='Scans, min / max')
        scans_label.grid(row=1, column=0, sticky='w', pady=(6, 0))
        scans = ttk.Frame(settings)
        scans.grid(row=1, column=1, sticky='w', padx=(PAD, 0), pady=(6, 0))
        ttk.Entry(scans, textvariable=self.min_scans, width=5).grid(row=0, column=0)
        ttk.Label(scans, text='/').grid(row=0, column=1, padx=4)
        ttk.Entry(scans, textvariable=self.max_scans, width=5).grid(row=0, column=2)
        scans_tip = ('How many repeat scans to average into one measurement. More scans '
                    'reduce noise but take longer. The firmware picks a value in this '
                    'range on its own (short exposures need more repeats to fill about '
                    'a second of total sampling time).')
        Tooltip(scans_label, scans_tip)
        Tooltip(scans, scans_tip)

        repeat_box = ttk.LabelFrame(content, text='Automatic repeat', padding=PAD)
        repeat_box.grid(row=8, column=0, sticky='ew', pady=(PAD, 0))
        repeat_box.columnconfigure(3, weight=1)

        ttk.Label(repeat_box, text='Repeat every (s)').grid(row=0, column=0, sticky='w')
        repeat_entry = ttk.Entry(repeat_box, textvariable=self.repeat_time, width=6)
        repeat_entry.grid(row=0, column=1, sticky='w', padx=(PAD, 0))
        repeat_tip = ('How many seconds to wait between automatic measurements once '
                     'started. Which mode(s) get measured each time is set by the '
                     'checkboxes below.')
        Tooltip(repeat_entry, repeat_tip)

        btn_row = ttk.Frame(repeat_box)
        btn_row.grid(row=0, column=2, sticky='w', padx=(PAD, 0))
        self.bt_repeat_start = ttk.Button(btn_row, text='Start', width=7,
                                          command=self._start_repeat, state='disabled')
        self.bt_repeat_start.grid(row=0, column=0)
        self.bt_repeat_stop = ttk.Button(btn_row, text='Stop', width=7,
                                         command=self._stop_repeat, state='disabled')
        self.bt_repeat_stop.grid(row=0, column=1, padx=(6, 0))
        Tooltip(self.bt_repeat_start, 'Start automatically taking and saving measurements '
                                      'on the interval set above.')
        Tooltip(self.bt_repeat_stop, 'Stop automatic repeat.')

        self.repeat_status_label = ttk.Label(repeat_box, text='Not running.', style='Muted.TLabel')
        self.repeat_status_label.grid(row=1, column=0, columnspan=4, sticky='w', pady=(6, 0))

        # Indented under the row above with a "Measure:" prefix, so it reads as part of
        # the repeat feature (these checkboxes have no effect unless repeat is running).
        repeat_modes = ttk.Frame(repeat_box)
        repeat_modes.grid(row=2, column=0, columnspan=4, sticky='w', pady=(6, 0), padx=(20, 0))
        ttk.Label(repeat_modes, text='Measure:', style='Muted.TLabel').grid(row=0, column=0)
        cb_repeat_irr = ttk.Checkbutton(repeat_modes, text='Irradiance', variable=self.repeat_irr)
        cb_repeat_irr.grid(row=0, column=1, padx=(6, 0))
        cb_repeat_rad = ttk.Checkbutton(repeat_modes, text='Radiance', variable=self.repeat_rad)
        cb_repeat_rad.grid(row=0, column=2, padx=(10, 0))
        Tooltip(cb_repeat_irr, 'Take an Irradiance reading on each automatic repeat.')
        Tooltip(cb_repeat_rad, 'Take a Radiance reading on each automatic repeat.')

        self._build_analysis(content).grid(row=9, column=0, sticky='ew', pady=(PAD, 0))

        plot_frame = ttk.Frame(content)
        plot_frame.grid(row=10, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(2, weight=1)

        self.plot = plotting.SpectrumPlot(plot_frame, dark=self.dark_mode.get())
        self.plot.on_hover = lambda text: self.cursor_label.config(text=text or '')

        toolbar = NavigationToolbar2Tk(self.plot.canvas, plot_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.grid(row=0, column=0, sticky='ew')

        self.cursor_label = ttk.Label(plot_frame, text='', style='Muted.TLabel')
        self.cursor_label.grid(row=1, column=0, sticky='w')

        self.plot.widget.grid(row=2, column=0, sticky='nsew')

        actions = ttk.Frame(content)
        actions.grid(row=11, column=0, sticky='ew', pady=(PAD, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text='Save figure...', command=self._save_figure).grid(
            row=0, column=1, sticky='e')
        ttk.Checkbutton(actions, text='Dark mode', variable=self.dark_mode,
                        command=self._toggle_theme).grid(row=0, column=0, sticky='w')

        return outer

    def _build_analysis(self, parent):
        frame = ttk.LabelFrame(parent, text='Analysis', padding=PAD)
        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

        tips = {
            'peak': 'Wavelength with the highest measured intensity. Daylight and white '
                   'LEDs usually peak around 450-550nm; incandescent bulbs peak further '
                   'into the red, often above 600nm.',
            'fwhm': 'Width of the main peak at half its height. Narrow (a few nm) means a '
                   'single LED or laser line; broad, or "n/a", means a continuous/broadband '
                   'source like daylight or an incandescent bulb.',
            'cie_x': 'CIE 1931 chromaticity x - perceived colour, independent of '
                    'brightness, paired with CIE y below. Daylight is around (0.31, 0.33); '
                    'warm incandescent light is around (0.45, 0.41).',
            'cie_y': 'CIE 1931 chromaticity y - perceived colour, independent of '
                    'brightness, paired with CIE x above. Daylight is around (0.31, 0.33); '
                    'warm incandescent light is around (0.45, 0.41).',
            'cct': 'Correlated Colour Temperature - approximate "warmth" in Kelvin. Lower '
                  '(~2700K) reads warm/orange, like incandescent; higher (~5000-6500K) '
                  'reads cool/blue, like daylight. Shows "-" for narrow-band/coloured '
                  'light, where CCT isn\'t a meaningful concept.',
        }

        self._analysis_labels = {}
        fields = (('peak', 'Peak λ'), ('fwhm', 'FWHM'),
                  ('cie_x', 'CIE x'), ('cie_y', 'CIE y'), ('cct', 'CCT (approx.)'))
        for i, (key, caption) in enumerate(fields):
            r, c = divmod(i, 2)
            caption_label = ttk.Label(frame, text=caption + ':', style='Muted.TLabel')
            caption_label.grid(row=r, column=c * 2, sticky='w',
                               padx=(0 if c == 0 else PAD, 4), pady=(2, 0))
            value = ttk.Label(frame, text='-')
            value.grid(row=r, column=c * 2 + 1, sticky='w', pady=(2, 0))
            self._analysis_labels[key] = value
            Tooltip(caption_label, tips[key])
            Tooltip(value, tips[key])

        return frame

    # ---------------- History tab ----------------

    def _build_history_tab(self, parent):
        outer, content = self._make_scrollable(parent, expand_row=3)

        header = ttk.Frame(content)
        header.grid(row=0, column=0, sticky='ew')
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text='Saved readings', style='Muted.TLabel').grid(
            row=0, column=0, sticky='w')
        ttk.Button(header, text='Clear comparison', command=self._clear_history_plot).grid(
            row=0, column=1, sticky='e')

        tree_frame = ttk.Frame(content)
        tree_frame.grid(row=1, column=0, sticky='ew', pady=(4, 0))
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        self.saved_tree = ttk.Treeview(
            tree_frame, columns=('time', 'label', 'mode', 'luminance'),
            show='headings', height=8, selectmode='extended')
        for col, text, width in (('time', 'Time', 65), ('label', 'Label', 130),
                                  ('mode', 'Mode', 50), ('luminance', 'Lux/cd·m²', 90)):
            self.saved_tree.heading(col, text=text)
            self.saved_tree.column(col, width=width, anchor='w')
        self.saved_tree.grid(row=0, column=0, sticky='nsew')
        tree_scroll = ttk.Scrollbar(tree_frame, orient='vertical', command=self.saved_tree.yview)
        tree_scroll.grid(row=0, column=1, sticky='ns')
        self.saved_tree.configure(yscrollcommand=tree_scroll.set)
        self.saved_tree.bind('<Double-1>', self._on_saved_double_click)
        self.saved_tree.bind('<Button-3>', self._on_saved_right_click)

        ttk.Label(content, text='Double-click a reading to add it to the comparison plot '
                                'below; double-click it again to remove it. Double-click '
                                'more than one to compare them. Right-click for more '
                                'options, including on a multi-selection.',
                 style='Muted.TLabel', wraplength=500, justify='left').grid(
            row=2, column=0, sticky='w', pady=(6, 0))

        plot_frame = ttk.Frame(content)
        plot_frame.grid(row=3, column=0, sticky='nsew', pady=(PAD, 0))
        plot_frame.columnconfigure(0, weight=1)
        plot_frame.rowconfigure(1, weight=1)

        self.history_plot = plotting.SpectrumPlot(plot_frame, dark=self.dark_mode.get())
        toolbar = NavigationToolbar2Tk(self.history_plot.canvas, plot_frame, pack_toolbar=False)
        toolbar.update()
        toolbar.grid(row=0, column=0, sticky='ew')
        self.history_plot.widget.grid(row=1, column=0, sticky='nsew')

        return outer

    # ---------------- Calibration tab ----------------

    def _build_calibration_tab(self, parent):
        outer, content = self._make_scrollable(parent, expand_row=1)

        ttk.Label(content, style='Muted.TLabel', wraplength=560, justify='left', text=(
            'One-time setup per unit. Unit number and wheel positions live on the Arduino '
            'itself; linearisation and spectral sensitivity are stored in '
            'calibration_data.csv.')).grid(row=0, column=0, sticky='w')

        cal_notebook = ttk.Notebook(content)
        cal_notebook.grid(row=1, column=0, sticky='nsew', pady=(PAD, 0))
        self.unit_setup_tab = UnitSetupTab(cal_notebook, self.connection)
        self.linearisation_tab = LinearisationTab(cal_notebook, self.connection, self.store)
        self.sensitivity_tab = SensitivityTab(cal_notebook, self.connection, self.store)
        self.cosine_tab = CosineResponseTab(cal_notebook, self.connection, self.store)
        cal_notebook.add(self.unit_setup_tab, text='Unit & wheel setup')
        cal_notebook.add(self.linearisation_tab, text='Linearisation')
        cal_notebook.add(self.sensitivity_tab, text='Spectral sensitivity')
        cal_notebook.add(self.cosine_tab, text='Cosine response')

        return outer

    # ---------------- Monitor calibration tab ----------------
    # Separate top-level tab, not nested under Calibration: this is a downstream USE of
    # an already-calibrated device (monitor measurement), and is blocked outright by
    # MonitorCalibrationTab._start() if the device isn't calibrated.

    def _build_monitor_cal_tab(self, parent):
        outer, content = self._make_scrollable(parent, expand_row=0)
        self.monitor_cal_tab = MonitorCalibrationTab(content, self.connection, self.store)
        self.monitor_cal_tab.grid(row=0, column=0, sticky='nsew')
        return outer

    # ---------------- Debug tab ----------------

    def _build_debug_tab(self, parent):
        outer, content = self._make_scrollable(parent, expand_row=2)

        components = ttk.LabelFrame(content, text='Components', padding=PAD)
        components.grid(row=0, column=0, sticky='ew')
        components.columnconfigure(0, weight=1)

        self.debug_status_label = ttk.Label(components, text='Not connected.',
                                            justify='left', wraplength=540)
        self.debug_status_label.grid(row=0, column=0, sticky='w')

        ttk.Separator(components).grid(row=1, column=0, sticky='ew', pady=(PAD, PAD // 2))

        self.sensor_verdict_label = ttk.Label(components, text='? Optical sensor: unknown',
                                              style='Muted.TLabel')
        self.sensor_verdict_label.grid(row=2, column=0, sticky='w')
        self.sensor_detail_label = ttk.Label(components, text='', style='Muted.TLabel')
        self.sensor_detail_label.grid(row=3, column=0, sticky='w', pady=(0, PAD // 2))

        motor_row = ttk.Frame(components)
        motor_row.grid(row=4, column=0, sticky='ew')
        motor_row.columnconfigure(0, weight=1)
        ttk.Label(motor_row, text='Filter wheel motor').grid(row=0, column=0, sticky='w')
        self.bt_motor_test = ttk.Button(motor_row, text='Test', command=self._test_motor,
                                        state='disabled', width=8)
        self.bt_motor_test.grid(row=0, column=1, sticky='e')

        log_header = ttk.Frame(content)
        log_header.grid(row=1, column=0, sticky='ew', pady=(PAD, 0))
        log_header.columnconfigure(0, weight=1)
        ttk.Label(log_header, text='Log', style='Muted.TLabel').grid(row=0, column=0, sticky='w')
        ttk.Label(log_header, text='Level', style='Muted.TLabel').grid(row=0, column=1, sticky='e')
        level_box = ttk.Combobox(log_header, textvariable=self.log_level, values=LOG_LEVELS,
                                 state='readonly', width=8)
        level_box.grid(row=0, column=2, sticky='e', padx=(4, 0))

        self._build_log(content).grid(row=2, column=0, sticky='nsew', pady=(2, 0))

        return outer

    def _build_log(self, parent):
        log_frame = ttk.Frame(parent)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=10, wrap='word', state='disabled',
                                borderwidth=0, highlightthickness=0)
        self.log_text.grid(row=0, column=0, sticky='nsew')
        log_scroll = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky='ns')
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure('error', foreground='#d1495b')
        self.log_text.tag_configure('warning', foreground='#e9a23a')
        self.log_text.tag_configure('debug', foreground='#7a7a7a')
        return log_frame

    # ---------------- scrolling ----------------

    def _bind_mousewheel(self, canvas):
        self._active_scroll_canvas = canvas
        canvas.bind_all('<MouseWheel>', self._on_mousewheel)
        canvas.bind_all('<Button-4>', self._on_mousewheel)
        canvas.bind_all('<Button-5>', self._on_mousewheel)

    def _unbind_mousewheel(self):
        if self._active_scroll_canvas is not None:
            self._active_scroll_canvas.unbind_all('<MouseWheel>')
            self._active_scroll_canvas.unbind_all('<Button-4>')
            self._active_scroll_canvas.unbind_all('<Button-5>')
        self._active_scroll_canvas = None

    def _on_mousewheel(self, event):
        canvas = self._active_scroll_canvas
        if canvas is None:
            return
        if event.num == 4:
            canvas.yview_scroll(-1, 'units')
        elif event.num == 5:
            canvas.yview_scroll(1, 'units')
        else:
            canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')

    # ---------------- connection ----------------

    def _connect(self):
        # Runs off the main thread; opening the port and waiting for the firmware
        # reply can take several seconds, and blocking the main thread would freeze
        # the UI including the log that's supposed to show progress.
        self.connection = None
        self._set_connected(False)
        self._propagate_connection()
        self.bt_connect['state'] = 'disabled'
        self.bt_connect['text'] = 'Connecting...'
        self.title('OSpRad %s' % __version__)
        self._log('Connecting...')
        self._update_conn_labels('Connecting...')
        self.sensor_verdict_label.config(text='? Optical sensor: unknown', style='Muted.TLabel')
        self.sensor_detail_label.config(text='')
        self.update_idletasks()

        threading.Thread(target=self._connect_worker, daemon=True).start()

    def _connect_worker(self):
        try:
            self.store.load()
        except calibration.CalibrationError as exc:
            self.after(0, self._connect_failed, str(exc))
            return
        try:
            connection = serial_io.SerialConnection()
            config = connection.check_firmware()
        except serial_io.SpecError as exc:
            self.after(0, self._connect_failed, str(exc))
            return
        self.after(0, self._connect_succeeded, connection, config)

    def _connect_failed(self, message):
        self.bt_connect['state'] = 'normal'
        self.bt_connect['text'] = 'Reconnect'
        self._log(message, level='error')
        self._update_conn_labels(message)

    def _connect_succeeded(self, connection, config):
        self.connection = connection
        self._propagate_connection()
        self._set_connected(True)
        self.bt_connect['state'] = 'normal'
        self.bt_connect['text'] = 'Reconnect'
        # Title (not just the log) so unit/firmware stay visible after later
        # log messages scroll past them.
        self.title('OSpRad %s - unit #%d, firmware v%s'
                  % (__version__, config.unit_number, config.firmware))
        status = ('Connected to unit #%d on %s (firmware v%s)'
                  % (config.unit_number, connection.port, config.firmware))
        self._log(status)
        self._update_conn_labels(status)
        self._update_sensor_status(config)

    def _update_sensor_status(self, config):
        detected = config.sensor_detected
        if detected is None:
            self.sensor_verdict_label.config(text='? Optical sensor: unknown (older firmware?)',
                                             style='Muted.TLabel')
            self.sensor_detail_label.config(text='')
            return
        if detected:
            self.sensor_verdict_label.config(text='✓ Optical sensor: detected', style='Good.TLabel')
        else:
            self.sensor_verdict_label.config(text='✗ Optical sensor: not detected', style='Bad.TLabel')
        # Evidence-based, not certain - see SENSOR_ROUGHNESS_THRESHOLD's caveats.
        self.sensor_detail_label.config(
            text='roughness %.1f (threshold %.1f), raw ADC swing %d'
            % (config.sensor_roughness, serial_io.SENSOR_ROUGHNESS_THRESHOLD,
               config.sensor_scan_range))

    def _test_motor(self):
        # RC servos have no feedback wire, so there is no electrical self-check -
        # the test just jogs to two clearly different angles and the visible swing
        # is the confirmation.
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
        self.conn_status_label.config(text=text)
        self.debug_status_label.config(text=text)

    def _propagate_connection(self):
        for tab in (self.unit_setup_tab, self.linearisation_tab, self.sensitivity_tab,
                   self.cosine_tab, self.monitor_cal_tab):
            tab.set_connection(self.connection)

    def _set_connected(self, connected):
        """Grey out hardware-dependent controls while disconnected."""
        state = 'normal' if connected else 'disabled'
        for button in (self.bt_rad, self.bt_irr, self.bt_motor_test):
            button['state'] = state
        # Start also needs repeat to not already be running; Stop stays enabled so a
        # repeat in progress can still be cancelled if the connection drops mid-run.
        self.bt_repeat_start['state'] = 'normal' if (connected and not self._repeat_running) else 'disabled'
        if not connected:
            self.bt_save['state'] = 'disabled'

    # ---------------- actions ----------------

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
        self.bt_save['state'] = 'normal'

    def _update_analysis(self, wavelength, flux, calib):
        peak = analysis.peak_wavelength(wavelength, flux)
        fw = analysis.fwhm(wavelength, flux)
        self._analysis_labels['peak'].config(text='%.1f nm' % peak)
        self._analysis_labels['fwhm'].config(
            text=('%.1f nm' % fw) if fw is not None else 'n/a (broadband)')

        chroma = calib.chromaticity(flux)
        if chroma is not None:
            x, y = chroma
            cct = calibration.cct_from_xy(x, y)
            self._analysis_labels['cie_x'].config(text='%.4f' % x)
            self._analysis_labels['cie_y'].config(text='%.4f' % y)
            self._analysis_labels['cct'].config(text=('%.0f K' % cct) if cct is not None else '-')
        else:
            self._analysis_labels['cie_x'].config(text='-')
            self._analysis_labels['cie_y'].config(text='-')
            self._analysis_labels['cct'].config(text='-')

    def _push_settings(self):
        int_time = int(self.int_time.get())
        if int_time != self._prev_int_time:
            self.connection.set_integration_time(int_time)
            self._prev_int_time = int_time
            self._log('-> integration time %dms' % int_time, level='debug')

        n_min = max(1, int(self.min_scans.get()))
        n_max = min(50, int(self.max_scans.get()))
        if n_max < n_min:
            n_max = n_min
        if (n_min, n_max) != self._prev_scans:
            self.connection.set_scan_range(n_min, n_max)
            self._prev_scans = (n_min, n_max)
            self._log('-> scan range %d-%d' % (n_min, n_max), level='debug')

    def _save(self):
        if self.reading is None:
            return
        settings, data, wavelength = self.reading
        label = self.save_label.get()
        offset = datalog.append_reading(DATA_FILE, label, self.measurement.unit_number,
                                        settings, data, wavelength)
        self.bt_save['state'] = 'disabled'
        stamp = time.strftime('%H:%M:%S')
        luminance_text = f'{self._last_luminance:.3g}' if self._last_luminance is not None else ''
        self.saved_tree.insert('', 0, iid=str(offset), values=(
            stamp, label or '(unlabelled)', self.measurement.mode, luminance_text))
        self._log('Saved reading "%s"' % (label or '(unlabelled)'))

    def _load_saved_readings(self):
        self.saved_tree.delete(*self.saved_tree.get_children())
        for entry in datalog.iter_index(DATA_FILE):
            self.saved_tree.insert('', 'end', iid=str(entry.offset), values=(
                entry.time, entry.label or '(unlabelled)', entry.mode, f'{entry.luminance:.3g}'))

    def _on_saved_double_click(self, event):
        row_id = self.saved_tree.identify_row(event.y)
        if not row_id:
            return
        offset = int(row_id)
        if offset in self._compared_offsets:
            self._remove_from_comparison(offset)
        else:
            self._add_to_comparison(offset)

    def _add_to_comparison(self, offset):
        try:
            reading = datalog.load_reading(DATA_FILE, offset)
            calib = self.store.get(reading.unit_number)
            calib._derive()  # populates .wavelength - normally a side effect of
                              # to_flux()/luminance(), neither of which runs on this
                              # reload-from-disk path
        except (OSError, ValueError, calibration.CalibrationError) as exc:
            self._log(str(exc), level='error')
            return
        label_text = reading.label or '%s %s' % (reading.date, reading.time)
        # The plot's y-axis unit label reflects only the first curve added (see
        # SpectrumPlot._redraw), so tag each curve's mode in its legend - radiance
        # and irradiance are different physical units and a mixed comparison would
        # otherwise silently imply they're all in whichever unit was added first.
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

    # ---------------- History tab: right-click menu ----------------

    def _on_saved_right_click(self, event):
        row_id = self.saved_tree.identify_row(event.y)
        if not row_id:
            return
        if row_id not in self.saved_tree.selection():
            self.saved_tree.selection_set(row_id)

        selection = self.saved_tree.selection()
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label='Compare', command=self._compare_selected_readings)
        menu.add_command(label='Rename...', command=self._rename_selected_reading,
                         state='normal' if len(selection) == 1 else 'disabled')
        menu.add_command(label='Export...', command=self._export_selected_readings)
        menu.add_separator()
        menu.add_command(label='Delete...', command=self._delete_selected_readings)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _compare_selected_readings(self):
        for row_id in self.saved_tree.selection():
            offset = int(row_id)
            if offset not in self._compared_offsets:
                self._add_to_comparison(offset)

    def _rename_selected_reading(self):
        selection = self.saved_tree.selection()
        if len(selection) != 1:
            return
        offset = int(selection[0])
        current_label = self.saved_tree.item(selection[0], 'values')[1]
        if current_label == '(unlabelled)':
            current_label = ''
        new_label = simpledialog.askstring('OSpRad', 'Label:', initialvalue=current_label,
                                           parent=self)
        if new_label is None:
            return
        try:
            datalog.rename_reading(DATA_FILE, offset, new_label)
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        # A label of different length shifts every later row's byte offset (see
        # datalog.rename_reading), so any compared curve may now point at the wrong
        # row - clear rather than risk a silently wrong plot.
        self._clear_history_plot()
        self._load_saved_readings()
        self._log('Renamed reading to "%s"' % (new_label or '(unlabelled)'))

    def _delete_selected_readings(self):
        selection = self.saved_tree.selection()
        if not selection:
            return
        offsets = [int(row_id) for row_id in selection]
        count = len(offsets)
        if not messagebox.askyesno('OSpRad', 'Delete %d selected reading%s? This cannot '
                                   'be undone.' % (count, '' if count == 1 else 's'),
                                   parent=self):
            return
        try:
            datalog.delete_readings(DATA_FILE, offsets)
        except OSError as exc:
            self._log(str(exc), level='error')
            return
        # Deleting rows shifts every later row's byte offset - same reasoning as in
        # _rename_selected_reading.
        self._clear_history_plot()
        self._load_saved_readings()
        self._log('Deleted %d reading%s' % (count, '' if count == 1 else 's'))

    def _export_selected_readings(self):
        selection = self.saved_tree.selection()
        if not selection:
            return
        offsets = [int(row_id) for row_id in selection]
        path = filedialog.asksaveasfilename(
            defaultextension='.csv', initialdir=os.path.dirname(os.path.abspath(DATA_FILE)),
            filetypes=[('CSV file', '*.csv')])
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
        path = filedialog.asksaveasfilename(
            defaultextension='.png', initialdir=os.path.dirname(os.path.abspath(DATA_FILE)),
            filetypes=[('PNG image', '*.png'), ('PDF document', '*.pdf')])
        if path:
            self.plot.save_as_image(path)

    def _toggle_theme(self):
        dark = self.dark_mode.get()
        if sv_ttk:
            sv_ttk.set_theme('dark' if dark else 'light')
        self.plot.apply_theme(dark)
        self.history_plot.apply_theme(dark)
        self._paint_background()

    def _start_repeat(self):
        if self._repeat_running or self.connection is None:
            return
        if not self.repeat_irr.get() and not self.repeat_rad.get():
            self._log('Tick Irradiance and/or Radiance under "Measure" before starting '
                      'automatic repeat.', level='error')
            return
        try:
            if int(self.repeat_time.get()) < 1:
                raise ValueError
        except ValueError:
            self._log('Repeat interval must be a whole number of seconds.', level='error')
            return
        self._repeat_running = True
        self.bt_repeat_start['state'] = 'disabled'
        self.bt_repeat_stop['state'] = 'normal'
        self._log('Automatic repeat started.')
        self._repeat_after_id = self.after(50, self._repeat_tick)

    def _stop_repeat(self):
        if not self._repeat_running:
            return
        self._repeat_running = False
        if self._repeat_after_id is not None:
            self.after_cancel(self._repeat_after_id)
            self._repeat_after_id = None
        if self._countdown_after_id is not None:
            self.after_cancel(self._countdown_after_id)
            self._countdown_after_id = None
        self._repeat_next_time = None
        self.bt_repeat_start['state'] = 'normal' if self.connection is not None else 'disabled'
        self.bt_repeat_stop['state'] = 'disabled'
        self.repeat_status_label.config(text='Not running.')
        self._log('Automatic repeat stopped.')

    def _repeat_tick(self):
        if not self._repeat_running:
            return
        if self.repeat_irr.get():
            self._measure('i')
            self._save()
        if self.repeat_rad.get():
            self._measure('r')
            self._save()
        if not self._repeat_running:
            return  # Stop may have fired during the _measure()/_save() calls above
        interval = max(1, int(self.repeat_time.get()))
        self._repeat_next_time = time.time() + interval
        self._repeat_after_id = self.after(interval * 1000, self._repeat_tick)
        self._update_repeat_countdown()

    def _update_repeat_countdown(self):
        if not self._repeat_running or self._repeat_next_time is None:
            return
        remaining = max(0, round(self._repeat_next_time - time.time()))
        mins, secs = divmod(int(remaining), 60)
        self.repeat_status_label.config(text='Next measurement in %d:%02d' % (mins, secs))
        self._countdown_after_id = self.after(1000, self._update_repeat_countdown)

    def _log(self, text, level='info'):
        if LOG_LEVEL_RANK.get(level, 1) < LOG_LEVEL_RANK.get(self.log_level.get(), 1):
            return  # below the Debug tab's Level selector threshold
        stamp = time.strftime('%H:%M:%S')
        self.log_text.configure(state='normal')
        tags = (level,) if level in ('error', 'warning', 'debug') else ()
        self.log_text.insert('end', '[%s] %s\n' % (stamp, text), tags)
        line_count = int(self.log_text.index('end-1c').split('.')[0])
        if line_count > LOG_MAX_LINES:
            self.log_text.delete('1.0', '%d.0' % (line_count - LOG_MAX_LINES))
        self.log_text.see('end')
        self.log_text.configure(state='disabled')


if __name__ == '__main__':
    OSpRadApp().mainloop()
