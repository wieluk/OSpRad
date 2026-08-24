# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad

import csv
import math
import os
import tempfile

PIXELS = 288
CSV_COLUMNS = 290  # calibration_data.csv is a fixed-width spreadsheet export

# CIE 1931 2-degree color-matching functions (Wyman, Sloan & Shirley, 2013), as a
# closed-form piecewise-Gaussian fit. Each entry is a lobe:
# (amplitude, mu, sigma_below, sigma_above).
CIE_X_COEFS = [(1.056, 599.8, 37.9, 31.0), (0.362, 442.0, 16.0, 26.7), (-0.065, 501.1, 20.4, 26.2)]
CIE_Y_COEFS = [(0.821, 568.8, 46.9, 40.5), (0.286, 530.9, 16.3, 31.1)]
CIE_Z_COEFS = [(1.217, 437.0, 11.8, 36.0), (0.681, 459.0, 26.0, 13.8)]


def _piecewise_gaussian(w, lobes):
    total = 0.0
    for amp, mu, sigma_lo, sigma_hi in lobes:
        sigma = sigma_lo if w < mu else sigma_hi
        total += amp * math.exp(-0.5 * (w - mu) ** 2 / sigma ** 2)
    return total


def cct_from_xy(x, y):
    """Correlated color temperature (Kelvin) from CIE xy via McCamy's cubic
    approximation. Only meaningful near the Planckian locus (roughly 2000-20000K) -
    for strongly saturated/narrowband spectra (e.g. a single-color LED) the result is
    numerically valid but physically meaningless, so the UI labels it "CCT (approx.)".
    Returns None if the chromaticity is degenerate (y == 0.1858, division by zero)."""
    denom = 0.1858 - y
    if abs(denom) < 1e-9:
        return None
    n = (x - 0.3320) / denom
    return -449.0 * n ** 3 + 3525.0 * n ** 2 - 6823.3 * n + 5520.33

ROW_LENGTHS = {
    'wavCoef': 6,
    'radSens': PIXELS,
    'irrSens': PIXELS,
    'linCoefs': 2,
}


class CalibrationError(Exception):
    pass


def linearize(count, lin_coefs):
    """Raw ADC count -> linear flux, per the OSpRad linearisation model."""
    a, b = float(lin_coefs[0]), float(lin_coefs[1])
    if count > 0:
        multiplier = a * math.log((count + 1) * b)
    else:
        multiplier = -1 * a * math.log((-count + 1) * b)
    return count / multiplier


class CalibrationSet:
    def __init__(self, unit_number, wav_coef, rad_sens, irr_sens, lin_coefs):
        self.unit_number = unit_number
        self.wav_coef = wav_coef
        self.rad_sens = rad_sens
        self.irr_sens = irr_sens
        self.lin_coefs = lin_coefs
        self._derived = False

    def _derive(self):
        if self._derived:
            return
        self.wavelength = []
        self.ciex = []
        self.ciey = []
        self.ciez = []
        c = self.wav_coef
        for i in range(0, PIXELS):
            self.wavelength.append(
                c[0] + c[1] * i + c[2] * i ** 2 + c[3] * i ** 3 + c[4] * i ** 4 + c[5] * i ** 5)
            w = self.wavelength[i]
            self.ciex.append(_piecewise_gaussian(w, CIE_X_COEFS))
            self.ciey.append(_piecewise_gaussian(w, CIE_Y_COEFS))
            self.ciez.append(_piecewise_gaussian(w, CIE_Z_COEFS))

        self.wavelength_bins = []
        for i in range(0, PIXELS - 1):
            self.wavelength_bins.append(self.wavelength[i + 1] - self.wavelength[i])
        self.wavelength_bins.append(self.wavelength[PIXELS - 1] - self.wavelength[PIXELS - 2])
        self._derived = True

    def sensitivity(self, mode):
        return self.irr_sens if mode == 'i' else self.rad_sens

    def to_flux(self, raw_counts, mode, int_time):
        """Raw counts -> W/(sqm*nm) (irradiance) or W/(sr*sqm*nm) (radiance)."""
        self._derive()
        sens = self.sensitivity(mode)
        flux = [0.0] * PIXELS
        for i in range(0, PIXELS):
            if sens[i] > 0:
                flux[i] = (linearize(raw_counts[i], self.lin_coefs)
                           / (sens[i] * int_time * self.wavelength_bins[i]))
        return flux

    def luminance(self, flux):
        """Flux -> lux (irradiance) or cd/sqm (radiance)."""
        self._derive()
        total = 0.0
        for i in range(0, PIXELS):
            total += flux[i] * self.wavelength_bins[i] * self.ciey[i]
        return total * 683

    def chromaticity(self, flux):
        """Flux -> CIE 1931 (x, y) chromaticity, or None for a near-zero/dark reading.
        Scale-invariant (no photometric 683 lm/W factor needed - that only matters for
        luminance(), which stays absolute)."""
        self._derive()
        X = Y = Z = 0.0
        for i in range(0, PIXELS):
            b = self.wavelength_bins[i]
            X += flux[i] * b * self.ciex[i]
            Y += flux[i] * b * self.ciey[i]
            Z += flux[i] * b * self.ciez[i]
        total = X + Y + Z
        if total <= 0:
            return None
        return X / total, Y / total


class CalibrationStore:
    def __init__(self, path='calibration_data.csv'):
        self.path = path
        self.units = {}

    def load(self):
        if not os.path.exists(self.path):
            raise CalibrationError(
                "Calibration file not found: %s\nEnsure calibration_data.csv is in the "
                "same directory as the app." % self.path)

        rows = {}
        with open(self.path, newline='') as handle:
            for row in csv.reader(handle):
                while row and row[-1] == '':
                    row.pop()
                if len(row) < 3:
                    continue
                try:
                    unit = int(row[0])
                except ValueError:
                    continue  # header row
                rows.setdefault(unit, {})[row[1]] = row[2:]

        problems = []
        units = {}
        for unit, by_type in sorted(rows.items()):
            values = {}
            for row_type, expected in ROW_LENGTHS.items():
                if row_type not in by_type:
                    problems.append("Unit %d: missing '%s' row." % (unit, row_type))
                    continue
                raw = by_type[row_type]
                if len(raw) != expected:
                    problems.append("Unit %d: '%s' has %d values, expected %d."
                                    % (unit, row_type, len(raw), expected))
                    continue
                try:
                    values[row_type] = [float(v) for v in raw]
                except ValueError:
                    problems.append("Unit %d: '%s' contains non-numeric values." % (unit, row_type))
            if len(values) == len(ROW_LENGTHS):
                units[unit] = CalibrationSet(unit, values['wavCoef'], values['radSens'],
                                             values['irrSens'], values['linCoefs'])

        if not units:
            problems.append("No usable calibration data found in %s." % self.path)
        if problems:
            raise CalibrationError('\n'.join(problems))

        self.units = units
        return self

    def get(self, unit_number):
        if unit_number not in self.units:
            raise CalibrationError(
                "No calibration data for unit #%d.\nEnsure %s has data for this unit, "
                "or run the calibration wizard." % (unit_number, self.path))
        return self.units[unit_number]

    def save_unit(self, calib):
        """Write (or replace) one unit's four rows, preserving the padded CSV format."""
        self.units[calib.unit_number] = calib

        existing = []
        if os.path.exists(self.path):
            with open(self.path, newline='') as handle:
                existing = list(csv.reader(handle))

        def is_target(row):
            if len(row) < 2:
                return False
            try:
                return int(row[0]) == calib.unit_number and row[1] in ROW_LENGTHS
            except ValueError:
                return False

        kept = [row for row in existing if not is_target(row)]
        new_rows = [
            self._pad([calib.unit_number, 'wavCoef'] + list(calib.wav_coef)),
            self._pad([calib.unit_number, 'radSens'] + list(calib.rad_sens)),
            self._pad([calib.unit_number, 'irrSens'] + list(calib.irr_sens)),
            self._pad([calib.unit_number, 'linCoefs'] + list(calib.lin_coefs)),
        ]

        directory = os.path.dirname(os.path.abspath(self.path))
        handle = tempfile.NamedTemporaryFile('w', newline='', dir=directory,
                                             delete=False, suffix='.tmp')
        try:
            with handle:
                csv.writer(handle).writerows(kept + new_rows)
            os.replace(handle.name, self.path)
        except BaseException:
            os.unlink(handle.name)
            raise

    @staticmethod
    def _pad(row):
        return list(row) + [''] * (CSV_COLUMNS - len(row))
