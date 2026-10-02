# Export/import a unit's calibration as one JSON file: both the CSV data and the
# wheel positions in EEPROM, since either half alone is useless and they are easy
# to lose separately. Every section is optional, so import merges into what the
# unit already has rather than replacing it.

import datetime
import json
import logging

import calibration

log = logging.getLogger('osprad.calibration_io')

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

# Each sensitivity carries the supply voltage it was measured at ('<key>Vcc', mV).
SENS_MODES = {'radSens': 'r', 'irrSens': 'i'}

WHEEL_ROLES = ('dark', 'irr', 'rad')
# Export keys -> the single letter role the firmware uses
# (serial_io.save_wheel_position).
WHEEL_ROLE_LETTERS = {'dark': 'D', 'irr': 'I', 'rad': 'R'}


class CalibrationIOError(Exception):
    pass


class ImportedCalibration:
    """One parsed export file. `values` holds only the CSV sections the file
    actually contains, so `available_fields()` is what the import UI offers."""

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
    config is an optional serial_io.UnitConfig for the Arduino side wheel positions."""
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
    for key, mode in SENS_MODES.items():
        if key in fields and calib.vcc_ref[mode]:
            data[key + 'Vcc'] = calib.vcc_ref[mode]
    if 'wavCoef' in fields and calib.serial:
        data['sensorSerial'] = calib.serial
    placeholders = [key for key in CSV_FIELDS if key in fields and key in calib.placeholders]
    if placeholders:
        data['placeholders'] = placeholders
    if WHEEL_FIELD in fields and config is not None:
        data[WHEEL_FIELD] = {'dark': config.dark, 'irr': config.irr, 'rad': config.rad}
    return data


def dumps(calib, config=None, fields=None):
    """Serialise an export to JSON text; the caller writes it through file_io."""
    data = build_export(calib, config, fields)
    log.debug('Exporting unit #%s: %s', data.get('unit_number'),
              [k for k in data if k not in ('format', 'version', 'exported', 'unit_number')])
    return json.dumps(data, indent=2)


def loads(text, source='the file'):
    """Parse and fully validate export text, returning an ImportedCalibration.

    Validation is strict and happens entirely before anything is written anywhere.
    An import overwrites a real unit's calibration and can push wheel angles to the
    Arduino's EEPROM, so a half applied import from a truncated or hand edited file
    would be worse than a rejected one.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CalibrationIOError('Could not read %s: %s' % (source, exc)) from exc

    if not isinstance(data, dict) or data.get('format') != FORMAT_NAME:
        raise CalibrationIOError(
            'This is not an OSpRad calibration file (expected a "%s" JSON file).' % FORMAT_NAME)
    if data.get('version') != FORMAT_VERSION:
        raise CalibrationIOError(
            'Unsupported calibration file version %r; this app writes and reads version %d.'
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
                'Calibration file\'s "%s" contains non numeric values.' % key) from exc

    for key in SENS_MODES:
        if key in values and isinstance(data.get(key + 'Vcc'), (int, float)):
            values[key + 'Vcc'] = float(data[key + 'Vcc'])
    if 'wavCoef' in values and isinstance(data.get('sensorSerial'), str):
        values['sensorSerial'] = data['sensorSerial']
    values['placeholders'] = [k for k in data.get('placeholders') or [] if k in values]

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

    if not any(key in values for key in CSV_FIELDS) and wheel is None:
        raise CalibrationIOError('Calibration file contains no calibration data at all.')

    log.debug('Imported unit #%d from %s: %s', unit_number, source,
              sorted(values) + ([WHEEL_FIELD] if wheel else []))
    return ImportedCalibration(unit_number, values, wheel, data.get('exported'))


def merge(store, imported, fields):
    """Build the CalibrationSet that importing `fields` would produce, without saving.

    Returns a brand new CalibrationSet rather than mutating the one already in the
    store. CalibrationSet caches wavelengths derived from wavCoef on first use, so
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
        pick('linCoefs', 'lin_coefs'),
        {mode: imported.values.get(key + 'Vcc') if key in selected
         else existing and existing.vcc_ref[mode] for key, mode in SENS_MODES.items()},
        serial=(imported.values.get('sensorSerial', '') if 'wavCoef' in selected
                else existing.serial if existing else ''),
        placeholders=({k for k in existing.placeholders if k not in selected} if existing
                      else set()) | {k for k in imported.values['placeholders'] if k in selected})


def apply_wheel_positions(connection, wheel):
    """Push imported wheel angles to the Arduino's EEPROM.

    The firmware's save commands ('sD'/'sI'/'sR') store whatever angle the wheel
    is currently at rather than taking one as an argument, so each role is jogged
    in place first. The wheel physically moves three times.
    """
    for role in WHEEL_ROLES:
        connection.jog_wheel(wheel[role])
        connection.save_wheel_position(WHEEL_ROLE_LETTERS[role])
