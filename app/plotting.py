import io

from matplotlib.figure import Figure
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
import numpy as np

import file_io

# Plain Unicode rather than mathtext: mathtext renders in its own font, which sat
# noticeably apart from the rest of the label and made the units look pasted on,
# and it ran the multiplier straight into the W. These are the same units either
# way: spectral radiance is W·sr⁻¹·m⁻²·nm⁻¹, written here in the solidus form,
# which is easier to read at label size.
# %s takes the display multiplier (e.g. "×10⁻⁹ ") when the values need one; see _y_scale.
Y_LABELS = {
    'i': "Irradiance  %sW/(m²·nm)",
    'r': "Radiance  %sW/(sr·m²·nm)",
}
Y_LABEL_DEFAULT = "Spectral flux %s"

_SUPERSCRIPT = {'-': '⁻', '0': '⁰', '1': '¹', '2': '²', '3': '³',
                '4': '⁴', '5': '⁵', '6': '⁶', '7': '⁷',
                '8': '⁸', '9': '⁹'}


def _superscript(value):
    return ''.join(_SUPERSCRIPT.get(ch, ch) for ch in str(value))

# 'muted' matches the app's muted label role: readable, but clearly secondary.
LIGHT = {'bg': '#fafafa', 'fg': '#1c1c1c', 'grid': '#c8c8c8', 'muted': '#6a6a6a'}
DARK = {'bg': '#1c1c1c', 'fg': '#fafafa', 'grid': '#4a4a4a', 'muted': '#9a9a9a'}

# The spectral gradient only reads sensibly for a single curve, so overlays get a
# plain line cycling through this list.
OVERLAY_COLORS = ['#2e86ab', '#d1495b', '#2a9d8f', '#e9c46a', '#8338ec', '#f4a261']


GREY = 0.42  # stands in for the UV/NIR tails, which have no visible colour

HEADER_MARGIN_PX = 78


def _y_scale(top):
    """Choose a display multiplier for the y axis, returning (divisor, label_factor).

    Spectral flux runs down around 1e-7, and matplotlib's answer is a small "1e-7"
    floated above the axes, detached from the label it actually qualifies, so it reads
    as a stray number rather than part of the units. Dividing the plotted values and
    folding the exponent into the axis label keeps the magnitude next to the units
    it belongs to.

    Only the plotted copy is scaled; the stored data (and so the cursor readout) keeps
    its true values.
    """
    if not np.isfinite(top) or top <= 0:
        return 1.0, ''
    exponent = int(np.floor(np.log10(top)))
    # Engineering steps read better than arbitrary powers: 10^-6, 10^-9, ...
    exponent -= exponent % 3
    # Anything that already prints as a plain number is left alone.
    if -2 <= exponent <= 2:
        return 1.0, ''
    return 10.0 ** exponent, '×10%s ' % _superscript(exponent)


def _y_label(mode, factor):
    return Y_LABELS.get(mode, Y_LABEL_DEFAULT) % factor


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
    """Matplotlib Figure wrapped in a Qt canvas (self.canvas: add it to a layout to
    display), with hover/multi curve/theme logic shared by the main window's live
    and history plots and every calibration wizard tab's preview plot."""

    def __init__(self, dark=False):
        self.figure = Figure(figsize=(5, 5), tight_layout=True)
        self.ax = self.figure.add_subplot(1, 1, 1)
        self.canvas = FigureCanvasQTAgg(self.figure)
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

    def _wrap_header(self, text, fontsize, bold=False):
        """Fold header text onto as many lines as the current figure width needs."""
        width_px = self.figure.get_size_inches()[0] * self.figure.dpi
        char_px = fontsize * self.figure.dpi / 72.0 * (0.62 if bold else 0.58)
        available = max(60.0, width_px - HEADER_MARGIN_PX)
        max_chars = max(8, int(available / char_px))
        lines, current = [], ''
        for part in [p for p in text.split('   ') if p]:
            candidate = part if not current else current + '   ' + part
            if not current or len(candidate) <= max_chars:
                current = candidate
            else:
                lines.append(current)
                current = part
        if current:
            lines.append(current)
        return lines

    def _set_header(self, title, subtitle):
        """Headline above, settings beneath it, both left aligned."""
        title = title or ''
        subtitle = subtitle or ''
        self.ax.set_title('', loc='right')
        sub_lines = self._wrap_header(subtitle, 8) if subtitle else []
        title_lines = self._wrap_header(title, 11, bold=True) if title else []
        self.ax.set_title('\n'.join(title_lines), loc='left', fontsize=11,
                          fontweight='bold', color=self.colors['fg'],
                          pad=6 + 11 * len(sub_lines))
        if sub_lines:
            self.ax.text(0.0, 1.015, '\n'.join(sub_lines), transform=self.ax.transAxes,
                         ha='left', va='bottom', fontsize=8,
                         color=self.colors['muted'], clip_on=False)

    def _plain_y_ticks(self):
        """Plain tick numbers: the magnitude now lives in the axis label."""
        self.ax.ticklabel_format(axis='y', style='plain', useOffset=False)

    def _draw_empty(self):
        self.ax.clear()
        self._style_axes()
        self.ax.set_xlabel("Wavelength (nm)")
        self.ax.set_ylabel(_y_label(None, ''))
        self.ax.text(0.5, 0.5, "No measurement yet", transform=self.ax.transAxes,
                     ha='center', va='center', color=self.colors['grid'], fontsize=11)
        self.canvas.draw()

    def update(self, wavelength, flux, mode, title=None, subtitle=None):
        self._last = (wavelength, flux, mode, title, subtitle)
        self.ax.clear()
        self._style_axes()

        self.ax.set_xlabel("Wavelength (nm)")
        self._set_header(title, subtitle)

        w = np.asarray(wavelength)
        y = np.asarray(flux)
        self.ax.set_xlim(w[0], w[-1])
        top = float(y.max()) if y.size and y.max() > 0 else 1.0

        divisor, factor = _y_scale(top)
        self.ax.set_ylabel(_y_label(mode, factor))
        y = y / divisor
        top = top / divisor
        self.ax.set_ylim(0, top * 1.08)
        self._plain_y_ticks()

        self._draw_gradient(w, y, top)
        self.ax.plot(w, y, color=self.colors['fg'], linewidth=1.4, zorder=3)
        self.canvas.draw()

    # multi curve overlay API used by OSpRad.py's main window so the live curve and
    # overlaid past readings can share axes + legend. update() stays unchanged because
    # the calibration wizard calls it directly.

    def add_curve(self, name, wavelength, flux, mode=None, title=None, style='overlay',
                  color=None, label=None, subtitle=None):
        """style='live' = current or just taken measurement (with gradient wash,
        replaces any previous curve of that name). style='overlay' = compared past
        reading (plain line cycling through OVERLAY_COLORS)."""
        self._curves[name] = {
            'wavelength': wavelength, 'flux': flux, 'mode': mode, 'title': title,
            'subtitle': subtitle, 'style': style, 'color': color, 'visible': True,
            'label': label or title or name,
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
        self._set_header(primary.get('title'), primary.get('subtitle'))

        all_w = np.concatenate([np.asarray(c['wavelength']) for _, c in visible])
        all_y = np.concatenate([np.asarray(c['flux']) for _, c in visible])
        self.ax.set_xlim(all_w.min(), all_w.max())
        top = float(all_y.max()) if all_y.size and all_y.max() > 0 else 1.0

        # One scale for every overlaid curve, or they could not be compared.
        divisor, factor = _y_scale(top)
        self.ax.set_ylabel(_y_label(primary['mode'], factor))
        top = top / divisor
        self.ax.set_ylim(0, top * 1.08)
        self._plain_y_ticks()

        color_i = 0
        for _, c in visible:
            w = np.asarray(c['wavelength'])
            y = np.asarray(c['flux']) / divisor
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
        """Spectrum coloured wash under the curve, clipped to the data outline."""
        gradient = np.array([[wavelength_to_rgb(v) for v in w]])
        image = self.ax.imshow(gradient, extent=(w[0], w[-1], 0, top * 1.08),
                               aspect='auto', origin='lower', alpha=0.55, zorder=1)
        fill = self.ax.fill_between(w, 0, y, facecolor='none', edgecolor='none')
        image.set_clip_path(fill.get_paths()[0], transform=self.ax.transData)

    def _primary_curve(self):
        """Curve the cursor readout tracks: the live scan if visible, else the most
        recently added visible curve."""
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

    def save_as_bytes(self, fmt='png'):
        """Render the figure to bytes.

        Going via bytes rather than savefig(path) is what lets file_io write to an
        Android content:// URI, which matplotlib cannot open itself.
        """
        buf = io.BytesIO()
        self.figure.savefig(buf, format=fmt, dpi=150,
                            facecolor=self.figure.get_facecolor())
        return buf.getvalue()

    def save_as_image(self, path, fmt='png'):
        file_io.write_bytes(path, self.save_as_bytes(fmt))
