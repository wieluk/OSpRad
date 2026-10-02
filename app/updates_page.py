# More -> Updates: the app and firmware versions next to the latest release, updating
# the app, and flashing firmware (bundled, latest from GitHub, or any .hex file).

import os
import time

from PySide6.QtCore import QProcess, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                               QDialogButtonBox, QFileDialog, QLabel, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)

import file_io
import flasher
import serial_io
import updates
from _version import __version__
from qt_worker import Worker
from shell import Card
from ui import FlowLayout, collapsible_group, set_role, wrapped_label

try:
    import _firmware_bundled as bundled
except ImportError:
    bundled = None

SETTING_CHECK = 'updates/check_on_start'
SETTING_LAST_CHECK = 'updates/last_check'
CHECK_INTERVAL_SECONDS = 20 * 3600


class UpdatesPage(QWidget):
    """`host` is the main window: it lends its log, settings, unit firmware version and
    serial port (see OSpRadApp.release_for_flashing)."""

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.release = None
        self.worker = None
        self.flashing = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(12)

        app_card = Card('App')
        self.app_label = wrapped_label('Installed: %s' % __version__)
        app_card.body.addWidget(self.app_label)
        app_row = FlowLayout()
        self.check_button = QPushButton('Check now')
        self.check_button.clicked.connect(lambda: self.check(manual=True))
        app_row.addWidget(self.check_button)
        self.update_button = QPushButton('Update')
        set_role(self.update_button, 'primary')
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self._update_app)
        app_row.addWidget(self.update_button)
        app_card.body.addLayout(app_row)
        self.auto_check = QCheckBox('Check for updates at startup')
        self.auto_check.setChecked(host.get_setting(SETTING_CHECK, True, bool))
        self.auto_check.toggled.connect(lambda on: host.set_setting(SETTING_CHECK, on))
        app_card.body.addWidget(self.auto_check)
        notes_box, notes_layout = collapsible_group('Release notes')
        self.notes = wrapped_label('')
        self.notes.setTextFormat(Qt.TextFormat.MarkdownText)
        self.notes.setOpenExternalLinks(True)
        notes_layout.addWidget(self.notes)
        app_card.body.addWidget(notes_box)
        layout.addWidget(app_card)

        fw_card = Card('Firmware')
        self.fw_label = wrapped_label('')
        fw_card.body.addWidget(self.fw_label)
        # A unit without working firmware never connects, so the port is chosen here
        # rather than taken from the connection.
        port_row = FlowLayout()
        port_row.addWidget(QLabel('Port'))
        self.port_combo = QComboBox()
        self.port_combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.port_combo.setMinimumContentsLength(14)
        port_row.addWidget(self.port_combo)
        refresh_ports = QPushButton('Refresh')
        refresh_ports.clicked.connect(self.refresh_ports)
        port_row.addWidget(refresh_ports)
        fw_card.body.addLayout(port_row)
        fw_row = FlowLayout()
        self.flash_bundled = QPushButton('Flash %s' % (bundled.VERSION if bundled else '-'))
        set_role(self.flash_bundled, 'primary')
        self.flash_bundled.setEnabled(bundled is not None)
        self.flash_bundled.clicked.connect(
            lambda: self._flash(bundled.HEX_TEXT, 'firmware %s' % bundled.VERSION))
        fw_row.addWidget(self.flash_bundled)
        self.flash_latest = QPushButton('Flash latest from GitHub')
        self.flash_latest.setEnabled(False)
        self.flash_latest.clicked.connect(self._flash_latest)
        fw_row.addWidget(self.flash_latest)
        self.flash_file = QPushButton('Flash a .hex file...')
        self.flash_file.clicked.connect(self._flash_file)
        fw_row.addWidget(self.flash_file)
        self.source_button = QPushButton('View source')
        self.source_button.setEnabled(bundled is not None)
        self.source_button.clicked.connect(self._show_source)
        fw_row.addWidget(self.source_button)
        fw_card.body.addLayout(fw_row)
        hint = wrapped_label('Flashing keeps the unit number and wheel positions. It takes '
                             'about 15 seconds; leave the OSpRad plugged in until it reconnects.')
        set_role(hint, 'muted')
        fw_card.body.addWidget(hint)
        layout.addWidget(fw_card)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.status = wrapped_label('')
        layout.addWidget(self.status)
        layout.addStretch(1)

    # ---- state shown on the page ----

    def summary(self):
        """One line for the More list."""
        notes = []
        if self.release is not None and updates.is_newer(self.release.version, __version__):
            notes.append('App %s available' % self.release.version)
        unit = self.host.unit_firmware()
        if bundled is not None and unit and updates.is_newer(bundled.VERSION, unit):
            notes.append('firmware %s available for the unit' % bundled.VERSION)
        return ', '.join(notes).capitalize() if notes else 'Check for new app and firmware versions'

    def refresh_ports(self):
        """USB serial ports (all ports if none look like USB), with the connected
        unit's port, else the current choice, else the first one selected."""
        try:
            ports = serial_io.usb_serial_ports() or serial_io.list_ports()
        except Exception:  # noqa: BLE001 - enumeration failing just means no list
            ports = []
        wanted = self.host.connected_port() or self.port_combo.currentText()
        self.port_combo.clear()
        self.port_combo.addItems(ports)
        index = self.port_combo.findText(wanted) if wanted else -1
        self.port_combo.setCurrentIndex(index if index >= 0 else 0)

    def refresh(self):
        if self.worker is None:
            self.refresh_ports()
        latest = self.release.version if self.release is not None else 'not checked'
        newer = self.release is not None and updates.is_newer(self.release.version, __version__)
        self.app_label.setText('Installed: %s \N{MIDDLE DOT} Latest on GitHub: %s%s'
                               % (__version__, latest, ' (new)' if newer else ''))
        self.update_button.setEnabled(newer and self.worker is None)
        self.notes.setText(self.release.notes if self.release is not None else '')
        firmware_asset = self.release.firmware_asset() if self.release is not None else None
        self.flash_latest.setEnabled(firmware_asset is not None and self.worker is None)
        self.fw_label.setText('On the unit: %s \N{MIDDLE DOT} Bundled: %s \N{MIDDLE DOT} '
                              'Latest on GitHub: %s'
                              % (self.host.unit_firmware() or 'not connected',
                                 bundled.VERSION if bundled else '-',
                                 self._firmware_version(firmware_asset)
                                 or ('not published' if self.release else 'not checked')))
        idle = self.worker is None
        self.check_button.setEnabled(idle)
        self.flash_file.setEnabled(idle)
        self.flash_bundled.setEnabled(idle and bundled is not None)
        self.host.updates_summary(self.summary())

    @staticmethod
    def _firmware_version(asset):
        return asset[0][len('OSpRad-firmware-'):-len('.hex')] if asset else None

    def _set_status(self, text, role='muted'):
        self.status.setText(text)
        set_role(self.status, role)

    # ---- checking ----

    def check_on_start(self):
        if not self.auto_check.isChecked():
            return
        last = self.host.get_setting(SETTING_LAST_CHECK, 0.0, float)
        if time.time() - last >= CHECK_INTERVAL_SECONDS:
            self.check(manual=False)

    def check(self, manual):
        if self.worker is not None:
            return
        self._set_status('Checking GitHub\N{HORIZONTAL ELLIPSIS}' if manual else '')
        self._run(updates.latest_release, lambda r: self._checked(r, manual),
                  lambda e: self._set_status(e, 'bad') if manual else self.host.log(e, 'debug'))

    def _checked(self, release, manual):
        self.release = release
        self.host.set_setting(SETTING_LAST_CHECK, time.time())
        if updates.is_newer(release.version, __version__):
            self.host.log('OSpRad %s is available (installed: %s). See More > Updates.'
                          % (release.version, __version__))
            self._set_status('Version %s is available.' % release.version, 'good')
        elif manual:
            self._set_status('You have the latest version.')
        self.refresh()

    # ---- updating the app ----

    def _update_app(self):
        asset = self.release.app_asset()
        target = updates.replaceable_file()
        if asset is None:
            QMessageBox.information(self, 'OSpRad', (
                'This copy runs from Python (pip or a source checkout). Update it with\n\n'
                'pip install -U osprad\n\nor git pull in the checkout.'))
            return
        name, url = asset
        if target is None:
            # macOS, Android and the Linux tarball: the browser downloads, the user installs.
            QDesktopServices.openUrl(QUrl(url))
            self._set_status('Opened the download of %s in your browser.' % name)
            return
        if QMessageBox.question(self, 'OSpRad', 'Download %s and replace this copy of '
                                'OSpRad?' % name) != QMessageBox.StandardButton.Yes:
            return
        partial = target + '.download'
        self._set_status('Downloading %s\N{HORIZONTAL ELLIPSIS}' % name)

        def work(progress):
            updates.download(url, partial, progress, self.release, name)
            updates.install(partial, target)
            return target

        self._run(work, self._installed, lambda e: self._set_status(e, 'bad'), progress=True)

    def _installed(self, target):
        self._set_status('Updated. Restart OSpRad to use %s.' % self.release.version, 'good')
        if QMessageBox.question(self, 'OSpRad', 'Updated to %s. Restart now?'
                                % self.release.version) == QMessageBox.StandardButton.Yes:
            QProcess.startDetached(target, [])
            QApplication.quit()

    # ---- firmware ----

    def _flash_latest(self):
        name, url = self.release.firmware_asset()
        self._set_status('Downloading %s\N{HORIZONTAL ELLIPSIS}' % name)
        self._run(updates.download_text, lambda text: self._flash(text, name),
                  lambda e: self._set_status(e, 'bad'), url)

    def _flash_file(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Firmware (.hex)', '',
                                              'Intel HEX (*.hex);;All files (*)')
        if path:
            try:
                text = file_io.read_text(path, encoding='ascii')
            except (OSError, UnicodeDecodeError) as exc:
                self._set_status('Could not read %s: %s' % (path, exc), 'bad')
                return
            self._flash(text, os.path.basename(path))

    def _flash(self, hex_text, what):
        try:
            image = flasher.parse_hex(hex_text)
        except flasher.FlashError as exc:
            self._set_status(str(exc), 'bad')
            return
        if QMessageBox.question(
                self, 'OSpRad', 'Flash %s (%d bytes) to the OSpRad? The unit number and '
                'wheel positions are kept.' % (what, len(image))) != QMessageBox.StandardButton.Yes:
            return
        port = self.port_combo.currentText()
        if not port:
            self._set_status('No serial port found. Plug the OSpRad in and press Refresh.',
                             'bad')
            return
        if not self.host.release_for_flashing(port):
            self._set_status('The OSpRad is busy measuring; stop that first.', 'bad')
            return
        self._set_status('Flashing %s on %s\N{HORIZONTAL ELLIPSIS}' % (what, port))
        self.flashing = True
        self._run(lambda progress: flasher.flash(port, image, progress),
                  lambda result: self._flashed(what, result), self._flash_failed, progress=True)

    def _flashed(self, what, result):
        self.flashing = False
        chip, baud = result
        self._set_status('Flashed %s (%s, %d baud); the OSpRad reconnects by itself.'
                         % (what, chip, baud), 'good')
        self.host.log('Flashed %s.' % what)
        self.host.reconnect()

    def _flash_failed(self, message):
        self.flashing = False
        self._set_status('Flashing failed: %s' % message, 'bad')
        self.host.log('Flashing failed: %s' % message, 'error')
        self.host.reconnect()

    def _show_source(self):
        dialog = QDialog(self.window())
        dialog.setWindowTitle('Firmware %s source' % bundled.VERSION)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(bundled.SOURCE_TEXT)
        text.setReadOnly(True)
        text.setFont(QFont('monospace'))
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        window = self.window()
        dialog.resize(min(900, window.width()), int(window.height() * 0.9))
        dialog.exec()

    # ---- background work ----

    def _run(self, fn, on_success, on_failure, *args, progress=False):
        self.worker = Worker(fn, *args)
        if progress:
            self.worker.kwargs['progress'] = self.worker.progress.emit
            self.worker.progress.connect(self._show_progress)
            self.progress.setValue(0)
            self.progress.setVisible(True)

        def finish(callback, value):
            self.worker = None
            self.progress.setVisible(False)
            callback(value)
            self.refresh()

        self.worker.succeeded.connect(lambda value: finish(on_success, value))
        self.worker.failed.connect(lambda message: finish(on_failure, message))
        self.worker.start()
        self.refresh()

    def _show_progress(self, done, total):
        self.progress.setMaximum(max(total, 1))
        self.progress.setValue(done)

