import csv
import io
import logging
import math
import os
import tempfile

import numpy as np
from numpy.polynomial import polynomial as P

import cie_cmf

log = logging.getLogger('osprad.calibration')

PIXELS = 288
CSV_COLUMNS = 290  # calibration_data.csv is a fixed width spreadsheet export

_CMF_NM = cie_cmf.START_NM + np.arange(len(cie_cmf.XYZ))
_CMF = np.asarray(cie_cmf.XYZ)


def cmf(wavelength):
    """CIE 1931 2 degree (xbar, ybar, zbar) at the given wavelengths (nm)."""
    return tuple(np.interp(wavelength, _CMF_NM, _CMF[:, k], left=0.0, right=0.0)
                 for k in range(3))


def _planck_uv(temps):
    """CIE 1960 (u, v) of blackbodies at temps (K)."""
    lam = _CMF_NM * 1e-9
    with np.errstate(over='ignore'):
        spd = 1.0 / (lam ** 5 * np.expm1(1.4388e-2 / (lam * np.asarray(temps)[:, None])))
    X, Y, Z = (spd @ _CMF).T
    d = X + 15 * Y + 3 * Z
    return 4 * X / d, 6 * Y / d


_LOCUS_T = np.geomspace(1000, 100000, 4607)  # 0.1% steps
_LOCUS_UV = None


def cct_from_xy(x, y):
    """(CCT in K, Duv) from CIE xy, or None where CIE 15 leaves CCT undefined (|Duv| > 0.05)."""
    global _LOCUS_UV
    if _LOCUS_UV is None:
        _LOCUS_UV = _planck_uv(_LOCUS_T)
    d = -2 * x + 12 * y + 3
    u, v = 4 * x / d, 6 * y / d
    dist = np.hypot(_LOCUS_UV[0] - u, _LOCUS_UV[1] - v)
    i = int(np.argmin(dist))
    if i == 0 or i == len(_LOCUS_T) - 1:
        return None
    # Parabola through the three nearest grid points, in log temperature.
    d0, d1, d2 = dist[i - 1:i + 2]
    offset = 0.5 * (d0 - d2) / (d0 - 2 * d1 + d2)
    cct = float(_LOCUS_T[i] * (_LOCUS_T[i + 1] / _LOCUS_T[i]) ** offset)
    pu, pv = (c[0] for c in _planck_uv([cct]))
    duv = math.copysign(math.hypot(pu - u, pv - v), v - pv)
    if abs(duv) > 0.05:
        return None
    return cct, duv

ROW_LENGTHS = {
    'wavCoef': 6,
    'radSens': PIXELS,
    'irrSens': PIXELS,
    'linCoefs': 2,
}

# Optional rows: the supply voltage (mV) the radiance and irradiance sensitivities
# were measured at; the sensor's serial number; and which of the rows above are still
# placeholders taken from another unit rather than measured on this one.
VCC_ROW = 'vccRef'
SERIAL_ROW = 'sensorSerial'
PLACEHOLDER_ROW = 'placeholder'
OPTIONAL_ROWS = (VCC_ROW, SERIAL_ROW, PLACEHOLDER_ROW)


class CalibrationError(Exception):
    pass


def linearize(count, lin_coefs):
    """Raw ADC count to linear flux, per the OSpRad linearisation model. Odd in count,
    so dark subtracted noise below zero stays negative instead of biasing upwards."""
    a, b = float(lin_coefs[0]), float(lin_coefs[1])
    if count == 0:
        return 0.0
    return count / (a * math.log((abs(count) + 1) * b))


class CalibrationSet:
    def __init__(self, unit_number, wav_coef, rad_sens, irr_sens, lin_coefs, vcc_ref=None,
                 serial='', placeholders=()):
        self.unit_number = unit_number
        self.wav_coef = wav_coef
        self.rad_sens = rad_sens
        self.irr_sens = irr_sens
        self.lin_coefs = lin_coefs
        self.vcc_ref = dict({'r': None, 'i': None}, **(vcc_ref or {}))
        self.serial = serial
        self.placeholders = set(placeholders)
        self._derived = False

    def _derive(self):
        if self._derived:
            return
        c = self.wav_coef
        self.wavelength = [sum(c[k] * i ** k for k in range(6)) for i in range(PIXELS)]
        self.ciex, self.ciey, self.ciez = (list(c) for c in cmf(self.wavelength))

        # Central differences, so each bin is centred on its photosite.
        self.wavelength_bins = list(np.gradient(self.wavelength))
        self._derived = True

    def set_wavelength(self, wav_coef, serial=''):
        """New wavelength axis. Placeholder sensitivity curves are re-transferred onto it;
        returns the rows that were measured on the old axis and should be redone."""
        self.wav_coef = list(wav_coef)
        self.serial = serial
        self.placeholders.discard('wavCoef')
        self._derived = False
        self._derive()
        stale = []
        template = template_unit()
        for row, attr in (('radSens', 'rad_sens'), ('irrSens', 'irr_sens')):
            if row in self.placeholders and template is not None:
                setattr(self, attr, transfer(getattr(template, attr), template.wavelength,
                                             self.wavelength))
            elif row not in self.placeholders:
                stale.append(row)
        return stale

    def sensitivity(self, mode):
        return self.irr_sens if mode == 'i' else self.rad_sens

    def supply_factor(self, vcc, mode):
        """Rescales counts read at supply vcc (mV) to the calibration's supply, since
        the ADC measures against Vcc; 1.0 if either is unknown."""
        ref = self.vcc_ref.get(mode)
        return vcc / ref if vcc and ref else 1.0

    def to_flux(self, raw_counts, mode, int_time, vcc=None):
        """Raw counts to W/(sqm*nm) (irradiance) or W/(sr*sqm*nm) (radiance)."""
        self._derive()
        sens = self.sensitivity(mode)
        factor = self.supply_factor(vcc, mode)
        flux = [0.0] * PIXELS
        for i in range(0, PIXELS):
            if sens[i] > 0:
                flux[i] = (linearize(raw_counts[i] * factor, self.lin_coefs)
                           / (sens[i] * int_time * self.wavelength_bins[i]))
        return flux

    def luminance(self, flux):
        """Flux to lux (irradiance) or cd/sqm (radiance)."""
        self._derive()
        total = 0.0
        for i in range(0, PIXELS):
            total += flux[i] * self.wavelength_bins[i] * self.ciey[i]
        return total * 683

    def chromaticity(self, flux):
        """Flux to CIE 1931 (x, y) chromaticity, or None for a near zero/dark reading.
        Scale invariant (no 683 lm/W factor needed; that only matters for luminance())."""
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


def _parse_rows(handle):
    """Read a calibration CSV into {unit: {row_type: [raw string values]}}."""
    rows = {}
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
    return rows


def hamamatsu_to_wav_coef(coefficients):
    """Hamamatsu's A0, B1..B5 count pixels from 1; the app's wavCoef from 0."""
    out = [0.0] * 6
    for k, a in enumerate(coefficients):
        for j, c in enumerate(P.polypow([1, 1], k)):
            out[j] += a * c
    return out


def wav_coef_to_hamamatsu(wav_coef):
    out = [0.0] * 6
    for k, a in enumerate(wav_coef):
        for j, c in enumerate(P.polypow([-1, 1], k)):
            out[j] += a * c
    return out


def transfer(sens, from_wavelength, to_wavelength):
    """A sensitivity curve moved to another sensor's axis by wavelength (not pixel),
    zero outside the range the source curve covers."""
    sens = np.asarray(sens, dtype=float)
    valid = sens > 0
    if not valid.any():
        return [0.0] * PIXELS
    lo, hi = np.asarray(from_wavelength)[valid][[0, -1]]
    to = np.asarray(to_wavelength)
    out = np.interp(to, from_wavelength, sens, left=0.0, right=0.0)
    out[(to < lo) | (to > hi)] = 0.0
    return list(out)


def template_unit():
    """The first unit shipped with the app, whose curves stand in for a new unit's
    until they are measured. None if the app ships none."""
    rows = _bundled_rows()
    for unit in sorted(rows):
        try:
            calib = CalibrationSet(unit, *[[float(v) for v in rows[unit][r]] for r in ROW_LENGTHS])
        except (KeyError, ValueError):
            continue
        calib._derive()
        return calib
    return None


def new_unit(unit_number, wav_coef, serial=''):
    """Calibration for a unit that has none: its own wavelengths, the template's
    sensitivity (moved onto them) and linearisation as placeholders."""
    template = template_unit()
    if template is None:
        raise CalibrationError('This build of the app has no template calibration.')
    calib = CalibrationSet(unit_number, list(wav_coef), [0.0] * PIXELS, [0.0] * PIXELS,
                           list(template.lin_coefs), serial=serial,
                           placeholders={'radSens', 'irrSens', 'linCoefs'})
    calib._derive()
    calib.rad_sens = transfer(template.rad_sens, template.wavelength, calib.wavelength)
    calib.irr_sens = transfer(template.irr_sens, template.wavelength, calib.wavelength)
    return calib


_BUNDLED_ROWS = None


def _bundled_rows():
    """The calibration rows shipped inside the app, parsed once."""
    global _BUNDLED_ROWS
    if _BUNDLED_ROWS is None:
        try:
            from _calibration_data_bundled import CSV_TEXT
        except ImportError:
            _BUNDLED_ROWS = {}
        else:
            _BUNDLED_ROWS = _parse_rows(io.StringIO(CSV_TEXT))
    return _BUNDLED_ROWS


class CalibrationStore:
    def __init__(self, path='calibration_data.csv'):
        self.path = path
        self.units = {}

    def load(self):
        if not os.path.exists(self.path):
            raise CalibrationError(
                "Calibration file not found: %s\nEnsure calibration_data.csv is in the "
                "same directory as the app." % self.path)

        with open(self.path, newline='') as handle:
            rows = _parse_rows(handle)

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
                    problems.append("Unit %d: '%s' contains non numeric values." % (unit, row_type))
            try:
                vcc = [float(v or 0) or None for v in by_type.get(VCC_ROW, [])]
            except ValueError:
                vcc = []
                problems.append("Unit %d: '%s' contains non numeric values." % (unit, VCC_ROW))
            if len(values) == len(ROW_LENGTHS):
                units[unit] = CalibrationSet(
                    unit, values['wavCoef'], values['radSens'], values['irrSens'],
                    values['linCoefs'], dict(zip('ri', vcc)),
                    serial=(by_type.get(SERIAL_ROW) or [''])[0],
                    placeholders=[r for r in by_type.get(PLACEHOLDER_ROW, []) if r in ROW_LENGTHS])

        if not units:
            problems.append("No usable calibration data found in %s." % self.path)
        if problems:
            log.warning('calibration_data.csv had %d problem(s): %s',
                        len(problems), '; '.join(problems))
            raise CalibrationError('\n'.join(problems))

        log.info('Loaded calibration for %d unit(s) from %s: %s',
                 len(units), self.path, sorted(units))
        self.units = units
        return self

    def get(self, unit_number):
        if unit_number not in self.units:
            raise CalibrationError(
                "No calibration for unit #%d yet. Load its sensor's inspection sheet in "
                "Calibrate \N{RIGHTWARDS ARROW} Wavelength to start one." % unit_number)
        return self.units[unit_number]

    def save_unit(self, calib):
        """Write (or replace) one unit's rows, preserving the padded CSV format."""
        log.info('Saving calibration for unit #%d to %s', calib.unit_number, self.path)
        self.units[calib.unit_number] = calib

        existing = []
        if os.path.exists(self.path):
            with open(self.path, newline='') as handle:
                existing = list(csv.reader(handle))

        def is_target(row):
            if len(row) < 2:
                return False
            try:
                return (int(row[0]) == calib.unit_number
                        and (row[1] in ROW_LENGTHS or row[1] in OPTIONAL_ROWS))
            except ValueError:
                return False

        kept = [row for row in existing if not is_target(row)]
        new_rows = [
            self._pad([calib.unit_number, 'wavCoef'] + list(calib.wav_coef)),
            self._pad([calib.unit_number, 'radSens'] + list(calib.rad_sens)),
            self._pad([calib.unit_number, 'irrSens'] + list(calib.irr_sens)),
            self._pad([calib.unit_number, 'linCoefs'] + list(calib.lin_coefs)),
        ]
        if any(calib.vcc_ref.values()):
            new_rows.append(self._pad([calib.unit_number, VCC_ROW]
                                      + [calib.vcc_ref[m] or 0 for m in 'ri']))
        if calib.serial:
            new_rows.append(self._pad([calib.unit_number, SERIAL_ROW, calib.serial]))
        if calib.placeholders:
            new_rows.append(self._pad([calib.unit_number, PLACEHOLDER_ROW]
                                      + [r for r in ROW_LENGTHS if r in calib.placeholders]))

        directory = os.path.dirname(os.path.abspath(self.path))
        handle = tempfile.NamedTemporaryFile('w', newline='', dir=directory,
                                             delete=False, suffix='.tmp')
        try:
            with handle:
                csv.writer(handle, lineterminator='\n').writerows(kept + new_rows)
            os.replace(handle.name, self.path)
        except BaseException:
            os.unlink(handle.name)
            raise

    @staticmethod
    def _pad(row):
        return list(row) + [''] * (CSV_COLUMNS - len(row))
