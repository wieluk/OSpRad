# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad

import collections
import csv
import io
import os
import time

from calibration import PIXELS

# data.csv column layout - fixed, 588 fields per row (0-indexed):
#   0:label 1:unit# 2:date 3:time 4:mode 5:intTime 6:nScans 7:saturated
#   8:luminance-label 9:luminance 10:flux-unit-label 11..298:flux (288 values)
#   299:"rawCounts:" 300..587:rawCounts (288 values)
IDX_LABEL = 0
IDX_UNIT = 1
IDX_DATE = 2
IDX_TIME = 3
IDX_MODE = 4
IDX_INT_TIME = 5
IDX_N_SCANS = 6
IDX_SATURATED = 7
IDX_LUMINANCE = 9
IDX_FLUX_START = 11
IDX_FLUX_END = IDX_FLUX_START + PIXELS        # 299
IDX_COUNTS_START = IDX_FLUX_END + 1           # 300
IDX_COUNTS_END = IDX_COUNTS_START + PIXELS    # 588

ReadingIndex = collections.namedtuple(
    'ReadingIndex', 'offset label unit_number date time mode int_time n_scans saturated luminance')

SavedReading = collections.namedtuple(
    'SavedReading', 'label unit_number date time mode int_time n_scans saturated '
                     'luminance flux raw_counts')


def format_measurement(mode, measurement, flux, luminance, wavelength):
    """Bundle everything needed to write one reading: settings row, data row, and
    the wavelength axis used for the CSV header (round-tripped into append_reading)."""
    counts = [f'{c:.4f}' for c in measurement.raw_counts]
    flux_fields = [f'{flux[0]:.4f}'] + [f'{f:.4e}' for f in flux[1:]]

    if mode == 'i':
        data = ['lux:', f'{luminance:.4e}', 'W/(sqm*nm):'] + flux_fields
    else:
        data = ['cd/sqm:', f'{luminance:.4e}', 'W/(sr*sqm*nm):'] + flux_fields
    data += ['rawCounts:'] + counts

    settings = [mode, str(measurement.int_time), str(measurement.n_scans),
                str(measurement.saturated)]
    return settings, data, wavelength


def _header_row(wavelength):
    spectrum = [f'{w:.2f}' for w in wavelength]
    return (['label', 'unit#', 'date', 'time', 'measurement', 'integrationTime',
              'nScans', 'nSaturated', '', 'luminance', ''] + spectrum + ['rawCounts'] + spectrum)


def append_reading(path, label, unit_number, settings, data, wavelength):
    """Appends one reading row to path (writing a header first if the file is new/empty).

    Returns the byte offset of the row just written, so callers can index straight back
    to it (via load_reading) without rescanning the file.
    """
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0

    with open(path, 'a', newline='') as handle:
        writer = csv.writer(handle)
        if needs_header:
            writer.writerow(_header_row(wavelength))
        t = time.localtime()
        row = ([label, str(unit_number), time.strftime('%Y-%m-%d', t),
                time.strftime('%H:%M:%S', t)] + settings + data)
        offset = handle.tell()
        writer.writerow(row)
    return offset


def iter_index(path):
    """Yields a ReadingIndex per saved reading in path, cheapest-possible (only parses
    the first ~10 fields of each row, never the 576 flux/raw-count fields). Safe to call
    when path doesn't exist yet (yields nothing)."""
    if not os.path.exists(path):
        return
    with open(path, 'r', newline='') as handle:
        while True:
            offset = handle.tell()
            line = handle.readline()
            if not line:
                break
            if not line.strip():
                continue
            row = next(csv.reader([line]))
            if len(row) <= IDX_LUMINANCE:
                continue
            try:
                unit_number = int(row[IDX_UNIT])
            except ValueError:
                continue  # header row
            try:
                yield ReadingIndex(
                    offset=offset, label=row[IDX_LABEL], unit_number=unit_number,
                    date=row[IDX_DATE], time=row[IDX_TIME], mode=row[IDX_MODE],
                    int_time=int(row[IDX_INT_TIME]), n_scans=int(row[IDX_N_SCANS]),
                    saturated=float(row[IDX_SATURATED]), luminance=float(row[IDX_LUMINANCE]))
            except ValueError:
                continue  # corrupted/truncated row - skip rather than crash the browser


def _rewrite_rows(path, transform):
    """Rewrite path line-by-line via a temp file (atomic rename at the end), calling
    transform(offset, raw_line) -> new_line_or_None on every line. None drops the line."""
    tmp_path = path + '.tmp'
    with open(path, 'r', newline='') as src, open(tmp_path, 'w', newline='') as dst:
        while True:
            offset = src.tell()
            line = src.readline()
            if not line:
                break
            new_line = transform(offset, line)
            if new_line is not None:
                dst.write(new_line)
    os.replace(tmp_path, path)


def delete_readings(path, offsets):
    """Remove the reading rows at the given byte offsets (as yielded by iter_index()).
    Rows after a deleted one shift to new offsets - callers must treat every previously
    known offset as invalid once this returns."""
    offsets = set(offsets)
    _rewrite_rows(path, lambda offset, line: None if offset in offsets else line)


def rename_reading(path, offset, new_label):
    """Change the label of the reading at offset. A label of different length shifts the
    byte offsets of every later row, same caveat as delete_readings above."""
    def transform(line_offset, line):
        if line_offset != offset:
            return line
        row = next(csv.reader([line]))
        row[IDX_LABEL] = new_label
        buf = io.StringIO()
        csv.writer(buf).writerow(row)
        return buf.getvalue()
    _rewrite_rows(path, transform)


def export_readings(path, offsets, dest_path):
    """Copy the header plus the reading rows at the given offsets to a new CSV file,
    preserving full precision (flux and raw counts included)."""
    offsets = set(offsets)
    with open(path, 'r', newline='') as src, open(dest_path, 'w', newline='') as dst:
        while True:
            offset = src.tell()
            line = src.readline()
            if not line:
                break
            if offset == 0 or offset in offsets:
                dst.write(line)


def load_reading(path, offset):
    """Loads one full reading (flux + raw counts included) from its byte offset,
    as previously yielded by iter_index() or returned by append_reading()."""
    with open(path, 'r', newline='') as handle:
        handle.seek(offset)
        line = handle.readline()
    row = next(csv.reader([line]))
    if len(row) < IDX_COUNTS_END:
        raise ValueError('Reading at offset %d is truncated or corrupted' % offset)
    return SavedReading(
        label=row[IDX_LABEL], unit_number=int(row[IDX_UNIT]), date=row[IDX_DATE],
        time=row[IDX_TIME], mode=row[IDX_MODE], int_time=int(row[IDX_INT_TIME]),
        n_scans=int(row[IDX_N_SCANS]), saturated=float(row[IDX_SATURATED]),
        luminance=float(row[IDX_LUMINANCE]),
        flux=[float(v) for v in row[IDX_FLUX_START:IDX_FLUX_END]],
        raw_counts=[float(v) for v in row[IDX_COUNTS_START:IDX_COUNTS_END]])
