# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad

from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np

Y_LABELS = {
    'i': "Irradiance  W/(m$^2\\cdot$nm)",
    'r': "Radiance  W/(sr$\\cdot$m$^2\\cdot$nm)",
}

LIGHT = {'bg': '#fafafa', 'fg': '#1c1c1c', 'grid': '#c8c8c8'}
DARK = {'bg': '#1c1c1c', 'fg': '#fafafa', 'grid': '#4a4a4a'}

# The spectral gradient wash only reads sensibly for a single curve, so overlays get a
# plain line cycling through this list.
OVERLAY_COLORS = ['#2e86ab', '#d1495b', '#2a9d8f', '#e9c46a', '#8338ec', '#f4a261']


GREY = 0.42  # stands in for the UV/NIR tails, which have no visible colour


def wavelength_to_rgb(nm):
    """Approximate visible colour of a wavelength, fading to grey in the UV/NIR tails."""
    if nm < 380 or nm > 750:
        return (GREY, GREY, GREY)

    if nm < 440:
        r, g, b = -(nm - 440) / 60.0, 0.0, 1.0
    elif nm < 490:
        r, g, b = 0.0, (nm - 440) / 50.0, 1.0
    elif nm < 510:
        r, g, b = 0.0, 1.0, -(nm - 510) / 20.0
    elif nm < 580:
        r, g, b = (nm - 510) / 70.0, 1.0, 0.0
    elif nm < 645:
        r, g, b = 1.0, -(nm - 645) / 65.0, 0.0
    else:
        r, g, b = 1.0, 0.0, 0.0

    # blend into the grey tails so there is no hard edge at the limits of vision
    if nm < 420:
        weight = (nm - 380) / 40.0
    elif nm > 700:
        weight = (750 - nm) / 50.0
    else:
        weight = 1.0
    tail = (1.0 - weight) * GREY
    return (r * weight + tail, g * weight + tail, b * weight + tail)


class SpectrumPlot:
    def __init__(self, parent, dark=False):
        self.figure = Figure(figsize=(5, 5), tight_layout=True)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self.canvas = FigureCanvasTkAgg(self.figure, parent)
        self.widget = self.canvas.get_tk_widget()
        self._last = None
        self._curves = {}
        # Set by the owner to receive '<wavelength> nm   <value>' hover text.
        self.on_hover = None
        self.canvas.mpl_connect('motion_notify_event', self._on_motion)
        self.apply_theme(dark)

    def apply_theme(self, dark):
        self.colors = DARK if dark else LIGHT
        if self._curves:
            self._redraw()
        elif self._last is not None:
            self.update(*self._last)
        else:
            self._draw_empty()

    def _style_axes(self):
        c = self.colors
        self.figure.set_facecolor(c['bg'])
        self.ax.set_facecolor(c['bg'])
        for spine in self.ax.spines.values():
            spine.set_color(c['grid'])
        self.ax.tick_params(colors=c['fg'], labelsize=9)
        self.ax.xaxis.label.set_color(c['fg'])
        self.ax.yaxis.label.set_color(c['fg'])
        self.ax.title.set_color(c['fg'])
        self.ax.grid(True, color=c['grid'], alpha=0.4, linewidth=0.6)
        self.ax.set_axisbelow(True)

    def _draw_empty(self):
        self.ax.clear()
        self._style_axes()
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel("Spectral flux")
        self.ax.text(0.5, 0.5, "No measurement yet", transform=self.ax.transAxes,
                     ha='center', va='center', color=self.colors['grid'], fontsize=11)
        self.canvas.draw()

    def update(self, wavelength, flux, mode, title=None):
        self._last = (wavelength, flux, mode, title)
        self.ax.clear()
        self._style_axes()

        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel(Y_LABELS.get(mode, "Spectral flux"))
        if title:
            self.ax.set_title(title, fontsize=10)

        w = np.asarray(wavelength)
        y = np.asarray(flux)
        self.ax.set_xlim(w[0], w[-1])
        top = float(y.max()) if y.size and y.max() > 0 else 1.0
        self.ax.set_ylim(0, top * 1.08)

        self._draw_gradient(w, y, top)
        self.ax.plot(w, y, color=self.colors['fg'], linewidth=1.4, zorder=3)
        self.canvas.draw()

    # ---------------- multi-curve overlay API ----------------
    # OSpRad.py's main-window plot uses add_curve() so the live curve and overlaid past
    # readings can share axes + legend. update() above stays unchanged because the
    # calibration wizard calls it directly.

    def add_curve(self, name, wavelength, flux, mode=None, title=None, style='overlay',
                  color=None, label=None):
        """style='live' is the current/just-taken measurement (spectral-gradient wash,
        replaces any previous curve of that name). style='overlay' is a reloaded or
        compared past reading (plain line, cycles through OVERLAY_COLORS)."""
        self._curves[name] = {
            'wavelength': wavelength, 'flux': flux, 'mode': mode, 'title': title,
            'style': style, 'color': color, 'visible': True, 'label': label or title or name,
        }
        self._redraw()

    def remove_curve(self, name):
        if name in self._curves:
            del self._curves[name]
            self._redraw()

    def clear_curves(self):
        if self._curves:
            self._curves.clear()
            self._redraw()

    def set_visible(self, name, visible):
        curve = self._curves.get(name)
        if curve is not None and curve['visible'] != visible:
            curve['visible'] = visible
            self._redraw()

    def _redraw(self):
        visible = [(name, c) for name, c in self._curves.items() if c['visible']]
        if not visible:
            self._draw_empty()
            return

        self.ax.clear()
        self._style_axes()
        self.ax.set_xlabel("Wavelength (nm)")

        live = self._curves.get('live')
        primary = live if (live and live['visible']) else visible[0][1]
        self.ax.set_ylabel(Y_LABELS.get(primary['mode'], "Spectral flux"))
        if primary.get('title'):
            self.ax.set_title(primary['title'], fontsize=10)

        all_w = np.concatenate([np.asarray(c['wavelength']) for _, c in visible])
        all_y = np.concatenate([np.asarray(c['flux']) for _, c in visible])
        self.ax.set_xlim(all_w.min(), all_w.max())
        top = float(all_y.max()) if all_y.size and all_y.max() > 0 else 1.0
        self.ax.set_ylim(0, top * 1.08)

        color_i = 0
        for name, c in visible:
            w = np.asarray(c['wavelength'])
            y = np.asarray(c['flux'])
            if c['style'] == 'live':
                self._draw_gradient(w, y, top)
                self.ax.plot(w, y, color=self.colors['fg'], linewidth=1.4, zorder=3,
                             label=c['label'])
            else:
                color = c['color'] or OVERLAY_COLORS[color_i % len(OVERLAY_COLORS)]
                color_i += 1
                self.ax.plot(w, y, color=color, linewidth=1.2, zorder=2, label=c['label'])

        if len(visible) > 1:
            self.ax.legend(fontsize=8)
        self.canvas.draw_idle()

    def _draw_gradient(self, w, y, top):
        """Spectrum-coloured wash under the curve, clipped to the data outline."""
        gradient = np.array([[wavelength_to_rgb(v) for v in w]])
        image = self.ax.imshow(gradient, extent=(w[0], w[-1], 0, top * 1.08),
                               aspect='auto', origin='lower', alpha=0.55, zorder=1)
        fill = self.ax.fill_between(w, 0, y, facecolor='none', edgecolor='none')
        image.set_clip_path(fill.get_paths()[0], transform=self.ax.transData)

    def _primary_curve(self):
        """Curve the cursor readout tracks: the live scan if visible, else whichever
        visible curve was added most recently."""
        live = self._curves.get('live')
        if live and live['visible']:
            return 'live', live
        for name, c in reversed(list(self._curves.items())):
            if c['visible']:
                return name, c
        return None, None

    def _on_motion(self, event):
        if self.on_hover is None:
            return
        if event.inaxes != self.ax or event.xdata is None:
            self.on_hover(None)
            return
        _, curve = self._primary_curve()
        if curve is None:
            self.on_hover(None)
            return
        w = np.asarray(curve['wavelength'])
        idx = int(np.searchsorted(w, event.xdata))
        idx = max(0, min(idx, len(w) - 1))
        self.on_hover('%.1f nm   %.4g' % (w[idx], np.asarray(curve['flux'])[idx]))

    def save_as_image(self, path):
        self.figure.savefig(path, dpi=150, facecolor=self.figure.get_facecolor())
