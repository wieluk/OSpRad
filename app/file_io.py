# Every file the app writes goes through here, because "a path" means two different
# things depending on where the app is running.
#
# On desktop a QFileDialog returns a filesystem path that open() understands. On
# Android it returns a Storage Access Framework URI ("content://..."): the picker
# has already created an empty document and handed back a token, not a path.
# Python's open() cannot write to that string, so every export silently produced
# a 0 byte file. Qt's own file engine does understand content:// URIs, so writes
# are routed through QFile there.
#
# Producers therefore hand this module bytes or text rather than writing to a path
# themselves. See plotting.save_as_bytes, datalog.export_text, calibration_io.dumps.

import logging
import os
import sys
import time

from PySide6.QtCore import QFile, QIODevice
from PySide6.QtWidgets import QFileDialog

log = logging.getLogger('osprad.file_io')

IS_ANDROID = hasattr(sys, 'getandroidapilevel')

# Lets the QFile path be exercised on desktop, where content:// URIs don't exist. It
# is the only part of the Android fix that is testable without building an APK.
FORCE_QFILE = os.environ.get('OSPRAD_FORCE_QFILE') == '1'

# QSettings key, shared with OSpRad.py, remembering where the user last saved.
SETTING_LAST_SAVE_DIR = 'paths/last_save_dir'


def is_uri(path):
    """True for the URI forms a platform file dialog can hand back instead of a path."""
    return path.startswith('content://') or path.startswith('file://')


def _use_qfile(path):
    return is_uri(path) or FORCE_QFILE


def _write_android_content_uri(path, data):
    """Write to a Storage Access Framework document through Android's ContentResolver.

    Qt's QFile does not reliably open a content:// URI for writing, which left every
    export on Android as the empty document the picker had already created. Going
    through the resolver is what the platform actually supports. The context is
    resolved exactly as usb4a does it, which already works in this build.
    """
    from jnius import autoclass
    activity = autoclass('org.kivy.android.PythonActivity').mActivity
    if activity is None:
        raise OSError('No Android activity available to write %s' % path)
    uri = autoclass('android.net.Uri').parse(path)
    stream = activity.getContentResolver().openOutputStream(uri)
    if stream is None:
        raise OSError('Android refused to open %s for writing' % path)
    try:
        stream.write(data)
        stream.flush()
    finally:
        stream.close()
    log.debug('wrote %d bytes to %s via ContentResolver', len(data), path)


def write_bytes(path, data):
    """Write bytes to a filesystem path or an Android content:// URI.

    Raises OSError on failure, so callers can catch one exception type regardless of
    which backend actually did the writing.
    """
    if IS_ANDROID and is_uri(path):
        try:
            _write_android_content_uri(path, data)
            return
        except OSError:
            raise
        except Exception as exc:
            log.warning('ContentResolver write failed (%s); trying QFile', exc)

    if not _use_qfile(path):
        with open(path, 'wb') as handle:
            handle.write(data)
        log.debug('wrote %d bytes to %s', len(data), path)
        return

    handle = QFile(path)
    mode = QIODevice.OpenModeFlag.WriteOnly | QIODevice.OpenModeFlag.Truncate
    if not handle.open(mode):
        raise OSError('Could not open %s for writing: %s' % (path, handle.errorString()))
    try:
        written = handle.write(data)
        if written != len(data):
            raise OSError('Only wrote %d of %d bytes to %s: %s'
                          % (written, len(data), path, handle.errorString()))
    finally:
        handle.close()
    log.debug('wrote %d bytes to %s (uri=%s)', len(data), path, is_uri(path))


def write_text(path, text, encoding='utf-8'):
    write_bytes(path, text.encode(encoding))


def read_bytes(path):
    """Read bytes from a filesystem path or an Android content:// URI."""
    if not _use_qfile(path):
        with open(path, 'rb') as handle:
            return handle.read()

    handle = QFile(path)
    if not handle.open(QIODevice.OpenModeFlag.ReadOnly):
        raise OSError('Could not open %s for reading: %s' % (path, handle.errorString()))
    try:
        return bytes(handle.readAll())
    finally:
        handle.close()


def read_text(path, encoding='utf-8'):
    return read_bytes(path).decode(encoding, errors='replace')


def default_name(stem, extension):
    """'osprad-plot', 'png' -> 'osprad-plot-20260826-143512.png'.

    A timestamped default means the save dialog never opens with an empty filename
    box, and repeated saves don't silently overwrite each other.
    """
    return '%s-%s.%s' % (stem, time.strftime('%Y%m%d-%H%M%S'), extension)


def ask_save_path(parent, suggested_name, filters, title='OSpRad'):
    """Save dialog pre filled with a real filename, returning (path, selected_filter).

    Returns (None, None) if cancelled. The selected filter is returned because a
    content:// URI carries no usable extension, so it is the only reliable way to
    know which format the user actually picked.
    """
    start = suggested_name
    # A remembered directory is meaningless under SAF, which has no filesystem paths.
    if not is_uri(suggested_name):
        last_dir = _last_save_dir()
        if last_dir:
            start = os.path.join(last_dir, suggested_name)

    path, selected = QFileDialog.getSaveFileName(parent, title, start, filters)
    if not path:
        return None, None
    if not is_uri(path):
        _remember_save_dir(os.path.dirname(path))
    return path, selected


def _settings():
    # Imported lazily: QSettings needs the QApplication org/app name set first, which
    # main() does at startup.
    from PySide6.QtCore import QSettings
    return QSettings()


def _last_save_dir():
    try:
        return _settings().value(SETTING_LAST_SAVE_DIR, '', type=str)
    except Exception:
        return ''


def _remember_save_dir(directory):
    if not directory:
        return
    try:
        _settings().setValue(SETTING_LAST_SAVE_DIR, directory)
    except Exception:
        pass  # a remembered directory is a convenience, never worth failing a save over


def extension_for(selected_filter, path, default='png'):
    """Work out the format to write, preferring the dialog's selected filter.

    On Android `path` is a content:// URI with no meaningful suffix, so the filter
    is the only signal; on desktop a suffix the user typed wins over the filter.
    """
    suffix = '' if is_uri(path) else os.path.splitext(path)[1].lstrip('.').lower()
    if suffix:
        return suffix
    if selected_filter:
        # Filters look like 'PNG image (*.png)'.
        start = selected_filter.rfind('*.')
        if start != -1:
            return selected_filter[start + 2:].rstrip(') ').lower()
    return default
