import numpy as np


def peak_wavelength(wavelength, flux):
    """Peak wavelength (nm), with 3-point parabolic sub-pixel refinement around the
    maximum sample (the 288-pixel sensor is coarse enough that this helps)."""
    w = np.asarray(wavelength, dtype=float)
    y = np.asarray(flux, dtype=float)
    i = int(np.argmax(y))
    if i == 0 or i == len(y) - 1:
        return float(w[i])

    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    denom = y0 - 2 * y1 + y2
    if denom == 0:
        return float(w[i])
    offset = max(-1.0, min(1.0, 0.5 * (y0 - y2) / denom))
    if offset >= 0:
        return float(w[i] + offset * (w[i + 1] - w[i]))
    return float(w[i] + offset * (w[i] - w[i - 1]))


def fwhm(wavelength, flux):
    """Full width at half maximum (nm), via linear interpolation of the half-max
    crossings nearest the peak. Returns None if neither side drops below half-max
    inside the recorded window (broadband sources); the UI renders that as "n/a"."""
    w = np.asarray(wavelength, dtype=float)
    y = np.asarray(flux, dtype=float)
    peak = int(np.argmax(y))
    half = y[peak] / 2.0
    if half <= 0:
        return None

    left = None
    for i in range(peak, 0, -1):
        if y[i - 1] < half <= y[i]:
            frac = (half - y[i - 1]) / (y[i] - y[i - 1])
            left = w[i - 1] + frac * (w[i] - w[i - 1])
            break

    right = None
    for i in range(peak, len(y) - 1):
        if y[i] >= half > y[i + 1]:
            frac = (y[i] - half) / (y[i] - y[i + 1])
            right = w[i] + frac * (w[i + 1] - w[i])
            break

    if left is None or right is None:
        return None
    return right - left
