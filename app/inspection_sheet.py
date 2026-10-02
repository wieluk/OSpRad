# Reads Hamamatsu's C12880MA "Final Inspection Sheet" (.xlsx, on the CD-ROM that comes
# with the sensor): serial number, wavelength coefficients A0, B1..B5 and resolution.
# Standard library only, since there is no spreadsheet package on Android.

import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
COEFFICIENTS = ('A0', 'B1', 'B2', 'B3', 'B4', 'B5')


class SheetError(Exception):
    pass


def _cells(data):
    """{(row, column): text} for every sheet in the workbook."""
    try:
        book = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SheetError('Not an .xlsx file. An older .xls sheet has to be typed in by '
                         'hand.') from exc
    shared = []
    if 'xl/sharedStrings.xml' in book.namelist():
        for item in ET.fromstring(book.read('xl/sharedStrings.xml')).findall('m:si', NS):
            shared.append(''.join(t.text or '' for t in item.iter('{%s}t' % NS['m'])))
    sheets = sorted(n for n in book.namelist() if re.match(r'xl/worksheets/sheet\d+\.xml$', n))
    cells = {}
    for index, name in enumerate(sheets):
        for cell in ET.fromstring(book.read(name)).iter('{%s}c' % NS['m']):
            match = re.match(r'([A-Z]+)(\d+)$', cell.get('r', ''))
            if not match:
                continue
            column = 0
            for letter in match.group(1):
                column = column * 26 + ord(letter) - 64
            kind = cell.get('t')
            if kind == 'inlineStr':
                text = ''.join(t.text or '' for t in cell.iter('{%s}t' % NS['m']))
            else:
                value = cell.find('m:v', NS)
                if value is None or value.text is None:
                    continue
                text = shared[int(value.text)] if kind == 's' else value.text
            cells[(index, int(match.group(2)), column)] = text.strip()
    return cells


def _number(text):
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def read(data):
    """Every sensor on the sheet as {'serial', 'coefficients' [A0..B5], 'resolution'}."""
    cells = _cells(data)
    sensors = []
    for (sheet, row, column), text in cells.items():
        if text != 'A0':
            continue
        columns = [column + i for i in range(6)]
        if [cells.get((sheet, row, c)) for c in columns] != list(COEFFICIENTS):
            continue
        header = {t: c for (s, r, c), t in cells.items() if s == sheet and r == row}
        serial_col = next((c for t, c in header.items() if t.lower().startswith('serial')), None)
        result_col = header.get('Result')
        rows = sorted({r for (s, r, c) in cells if s == sheet and r > row})
        for data_row in rows:
            values = [_number(cells.get((sheet, data_row, c))) for c in columns]
            if None in values:
                continue
            serial = cells.get((sheet, data_row, serial_col), '') if serial_col else ''
            sensors.append({
                'serial': serial if re.search(r'[0-9A-Za-z]', serial) else '',
                'coefficients': values,
                'resolution': _number(cells.get((sheet, data_row, result_col))),
            })
    if not sensors:
        raise SheetError('No wavelength coefficients (A0, B1 to B5) found in this file.')
    return sensors
