# Export/import a unit's calibration as one JSON file - both the CSV data and the
# wheel positions in EEPROM, since either half alone is useless and they're easy to
# lose separately. Every section is optional, so import merges into what the unit
# already has rather than replacing it.

import datetime
import json

import calibration

FORMAT_NAME = 'osprad-calibration'
FORMAT_VERSION = 1

# CSV row types; keys match calibration.ROW_LENGTHS so validation can reuse them.
CSV_FIELDS = ('wavCoef', 'radSens', 'irrSens', 'linCoefs')
WHEEL_FIELD = 'wheel'
ALL_FIELDS = CSV_FIELDS + (WHEEL_FIELD,)

FIELD_LABELS = {
    'wavCoef': 'Wavelength coefficients',
    'radSens': 'Radiance sensitivity',
    'irrSens': 'Irradiance sensitivity',
    'linCoefs': 'Linearisation coefficients',
    WHEEL_FIELD: 'Shutter wheel positions',
}

WHEEL_ROLES = ('dark', 'irr', 'rad')
# Export keys -> the single-letter role the firmware uses (serial_io.save_wheel_position).
WHEEL_ROLE_LETTERS = {'dark': 'D', 'irr': 'I', 'rad': 'R'}


class CalibrationIOError(Exception):
    pass


class ImportedCalibration:
    """One parsed export file. `values` holds only the CSV sections the file actually
    contains, so `available_fields()` is what the import UI offers to apply."""

    def __init__(self, unit_number, values, wheel, exported):
        self.unit_number = unit_number
        self.values = values
        self.wheel = wheel
        self.exported = exported

    def available_fields(self):
        fields = [key for key in CSV_FIELDS if key in self.values]
        if self.wheel is not None:
            fields.append(WHEEL_FIELD)
        return fields


def build_export(calib, config=None, fields=None):
    """Serialise a unit's calibration, including only `fields` (default: everything).
    config is an optional serial_io.UnitConfig for the Arduino-side wheel positions."""
    if fields is None:
        fields = ALL_FIELDS
    fields = set(fields)

    data = {
        'format': FORMAT_NAME,
        'version': FORMAT_VERSION,
        'exported': datetime.datetime.now().isoformat(timespec='seconds'),
        'unit_number': calib.unit_number,
    }
    source = {
        'wavCoef': calib.wav_coef,
        'radSens': calib.rad_sens,
        'irrSens': calib.irr_sens,
        'linCoefs': calib.lin_coefs,
    }
    for key in CSV_FIELDS:
        if key in fields:
            data[key] = [float(v) for v in source[key]]
    if WHEEL_FIELD in fields and config is not None:
        data[WHEEL_FIELD] = {'dark': config.dark, 'irr': config.irr, 'rad': config.rad}
    return data


def write_file(path, calib, config=None, fields=None):
    with open(path, 'w') as handle:
        json.dump(build_export(calib, config, fields), handle, indent=2)


def read_file(path):
    """Parse and fully validate an export file, returning an ImportedCalibration.

    Validation is strict and happens entirely before anything is written anywhere: an
    import overwrites a real unit's calibration and can push wheel angles to the
    Arduino's EEPROM, so a half-applied import from a truncated or hand-edited file
    would be worse than a rejected one.
    """
    try:
        with open(path) as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CalibrationIOError('Could not read %s: %s' % (path, exc)) from exc

    if not isinstance(data, dict) or data.get('format') != FORMAT_NAME:
        raise CalibrationIOError(
            'This is not an OSpRad calibration file (expected a "%s" JSON file).' % FORMAT_NAME)
    if data.get('version') != FORMAT_VERSION:
        raise CalibrationIOError(
            'Unsupported calibration file version %r - this app writes and reads version %d.'
            % (data.get('version'), FORMAT_VERSION))

    try:
        unit_number = int(data['unit_number'])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationIOError('Calibration file has no valid "unit_number".') from exc

    values = {}
    for key in CSV_FIELDS:
        if key not in data:
            continue  # every section is optional (see module docstring)
        expected = calibration.ROW_LENGTHS[key]
        raw = data[key]
        if not isinstance(raw, list) or len(raw) != expected:
            raise CalibrationIOError(
                'Calibration file\'s "%s" has %s values, expected %d.'
                % (key, len(raw) if isinstance(raw, list) else 'non-list', expected))
        try:
            values[key] = [float(v) for v in raw]
        except (TypeError, ValueError) as exc:
            raise CalibrationIOError(
                'Calibration file\'s "%s" contains non-numeric values.' % key) from exc

    wheel = data.get(WHEEL_FIELD)
    if wheel is not None:
        if not isinstance(wheel, dict):
            raise CalibrationIOError('Calibration file\'s "wheel" section is malformed.')
        parsed = {}
        for role in WHEEL_ROLES:
            if role not in wheel:
                raise CalibrationIOError(
                    'Calibration file\'s "wheel" section is missing "%s".' % role)
            try:
                parsed[role] = int(wheel[role])
            except (TypeError, ValueError) as exc:
                raise CalibrationIOError(
                    'Calibration file\'s wheel angle for "%s" is not a number.' % role) from exc
        wheel = parsed

    if not values and wheel is None:
        raise CalibrationIOError('Calibration file contains no calibration data at all.')

    return ImportedCalibration(unit_number, values, wheel, data.get('exported'))


def merge(store, imported, fields):
    """Build the CalibrationSet that importing `fields` would produce, without saving it.

    Returns a brand-new CalibrationSet rather than mutating the one already in the
    store: CalibrationSet caches wavelengths derived from wavCoef on first use, so
    overwriting wavCoef in place would leave that cache stale.
    """
    selected = [key for key in CSV_FIELDS if key in fields and key in imported.values]
    if not selected:
        return None

    try:
        existing = store.get(imported.unit_number)
    except calibration.CalibrationError:
        missing = [key for key in CSV_FIELDS if key not in selected]
        if missing:
            raise CalibrationIOError(
                'Unit #%d has no calibration data yet, so a partial import can\'t be '
                'merged into it. Also import: %s.'
                % (imported.unit_number,
                   ', '.join(FIELD_LABELS[key].lower() for key in missing))) from None
        existing = None

    def pick(imported_key, attr):
        if imported_key in selected:
            return imported.values[imported_key]
        return list(getattr(existing, attr)) if existing else []

    return calibration.CalibrationSet(
        imported.unit_number,
        pick('wavCoef', 'wav_coef'),
        pick('radSens', 'rad_sens'),
        pick('irrSens', 'irr_sens'),
        pick('linCoefs', 'lin_coefs'))


def apply_wheel_positions(connection, wheel):
    """Push imported wheel angles to the Arduino's EEPROM.

    The firmware's save commands ('sD'/'sI'/'sR') store whatever angle the wheel is
    *currently* at rather than taking one as an argument, so each role is jogged in
    place first - the wheel physically moves three times.
    """
    for role in WHEEL_ROLES:
        connection.jog_wheel(wheel[role])
        connection.save_wheel_position(WHEEL_ROLE_LETTERS[role])
