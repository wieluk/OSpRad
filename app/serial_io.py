# Serial transport. Requires OSpRad firmware 3.x: newline terminated commands,
# framed OK/ERR/CFG/DATA/DIAG replies.

import contextlib
import logging
import sys
import threading
import time

# Feeds the Debug tab via the Qt bridge OSpRad.py installs on the 'osprad' logger.
# Plain stdlib logging so this module never imports the GUI, and so it stays safe
# to call from the measurement worker thread.
log = logging.getLogger('osprad.serial')

PIXELS = 288

# Replies are logged for diagnosis, but a measurement's DATA line is ~2.3KB.
# Untrimmed it would flush the whole 500 line log widget in one go.
_LOG_REPLY_CHARS = 120

# How long a single measurement may take, from the firmware's own limits: in
# near darkness auto exposure never saturates, so intTime clamps to maxIntTime
# (60s) and nScans floors at nScansMin (3). Above sampleTimeMax the firmware
# interleaves a dark scan with every light scan: 3 x (60s + 60s), plus the ~9s
# exposure ramp and six servo moves. The old 90s timeout could not even cover
# one such scan, so a dark measurement timed out and moved the wheel for
# minutes before failing.
MEASURE_TIMEOUT = 420

# Bounds offered in the UI: below the ~370s worst case a dark scan cannot finish,
# and there is no point waiting beyond what the firmware itself can take.
MEASURE_TIMEOUT_MIN = 30
MEASURE_TIMEOUT_MAX = 1800

# Everything else is a short command and reply exchange.
COMMAND_TIMEOUT = 90

# Wire protocol is tied to the firmware's major version.
REQUIRED_FIRMWARE_MAJOR = 3
FIRMWARE_HINT = 'firmware/OSpRad_firmware'

# Firmware that added the live measure command ('l' + mode). It reuses the dark
# reference from the previous measurement instead of moving the wheel to posDark
# and re scanning it, which is what makes continuous mode refresh in fractions of
# a second. Older firmware just measures normally.
LIVE_MEASURE_FIRMWARE = (3, 3, 0)

# Sensor self test verdict: roughness (spatial, adjacent pixels within one
# scan) divided by repeat (temporal, the same pixel across two scans 150ms apart).
#
# A connected sensor's readout repeats, because its pixel to pixel fixed
# pattern is a physical property, so repeat is read noise only and the ratio
# lands near or above 1. A floating VIDEO pin picks up slow drifting
# interference: uncorrelated scan to scan, so repeat runs to tens of counts
# while roughness stays around 1, giving a ratio near 0.03. Being a ratio, it
# does not care about light level, integration time or wheel position, which
# is why the absolute roughness threshold it replaced read "not detected" on
# working units measuring in the dark.
SENSOR_REPEAT_RATIO_THRESHOLD = 0.5

# Floor on the divisor: repeat can legitimately be 0.00 on a quiet connected unit.
_MIN_REPEAT = 0.5

# CPython only defines this when built for Android, so it holds whatever the p4a
# bootstrap does to the environment. The old ANDROID_* env sweep was true on any
# machine with the Android SDK installed, including CI runners, which sent
# desktop builds down the usb4a path.
IS_ANDROID = hasattr(sys, 'getandroidapilevel')

if IS_ANDROID:
    from usb4a import usb
    from usbserial4a import serial4a
    # usbserial4a raises this on a transient USB hiccup rather than a real
    # disconnect. Folded into SpecProtocolError so the per command retry loop
    # covers it.
    from usbserial4a.utilserial4a import SerialException as _TransportError
else:
    import serial
    import serial.tools.list_ports
    _TransportError = serial.SerialException


def _patch_usbserial4a_ftdi():
    """Replace usbserial4a 0.4.0's FtdiSerial._read, which has two bugs its
    cdcacm/ch34x/cp210x siblings don't:

    1. It derives the last packet's payload length as `(total % maxPacketSize) - 2`,
       which goes negative on a read that is an exact multiple of the packet
       size, failing its own `if count > 0` guard and silently dropping 62 bytes.
       Reads are capped at 1024, an exact multiple of 64, so every full read
       lost 62 bytes. Short replies fit in one packet; a measurement's ~2.3KB
       DATA line did not, which is why only measurements ever failed their checksum.
    2. It raised SerialException when its hardcoded 5s bulkTransfer timeout
       expired, but the firmware is legitimately silent far longer while
       measuring. Returning no data and letting SerialConnection's own timeout
       govern is pyserial's contract and what the other three drivers do.
    """
    from usbserial4a import ftdiserial4a
    from usbserial4a.utilserial4a import PortNotOpenError, SerialException

    cls = ftdiserial4a.FtdiSerial
    header = cls.MODEM_STATUS_HEADER_LENGTH

    def _read(self):
        if not self.is_open:
            raise PortNotOpenError()
        if not self._read_endpoint:
            raise SerialException("Read endpoint does not exist!")

        buf = bytearray(self.DEFAULT_READ_BUFFER_SIZE)
        total = self._connection.bulkTransfer(
            self._read_endpoint, buf, self.DEFAULT_READ_BUFFER_SIZE,
            self.USB_READ_TIMEOUT_MILLIS)
        if total < header:
            return b''

        out = bytearray()
        max_packet = self._read_endpoint.getMaxPacketSize()
        offset = 0
        while offset < total:
            chunk = min(max_packet, total - offset)
            if chunk > header:
                out += buf[offset + header:offset + chunk]
            offset += chunk
        return bytes(out)

    cls._read = _read


if IS_ANDROID:
    _patch_usbserial4a_ftdi()


class SpecError(Exception):
    pass


class SpecProtocolError(SpecError):
    """Reply was missing, malformed, or failed its checksum."""


class SpecTimeoutError(SpecProtocolError):
    """The unit sent nothing before the read timeout.

    Its own class because a timeout means something different from a corrupt reply.
    The unit is most likely still working (a dark scene takes minutes), so it must
    not be retried and must not mark the connection dead.
    """


class SpecTransportError(SpecProtocolError):
    """The USB link itself failed: the port went away, or a read returned nothing.

    Distinct from a corrupt but delivered reply: only this kind means the
    connection is dead rather than one message being unlucky. Subclasses
    SpecProtocolError so every existing retry loop and `except SpecError` site
    behaves exactly as before.
    """


class SpecCommandError(SpecError):
    """Firmware replied ERR,<reason>."""


class UnitConfig:
    def __init__(self, unit_number, dark, irr, rad, configured, firmware,
                 sensor_scan_range=None, sensor_roughness=None, sensor_repeat=None):
        self.unit_number = unit_number
        self.dark = dark
        self.irr = irr
        self.rad = rad
        self.configured = configured
        self.firmware = firmware
        # Raw numbers from the 'd' self test, or None if it couldn't run.
        self.sensor_scan_range = sensor_scan_range
        self.sensor_roughness = sensor_roughness
        self.sensor_repeat = sensor_repeat

    @property
    def sensor_repeat_ratio(self):
        """roughness/repeat, or None if the self test didn't report both."""
        if self.sensor_roughness is None or self.sensor_repeat is None:
            return None
        return self.sensor_roughness / max(self.sensor_repeat, _MIN_REPEAT)

    @property
    def sensor_detected(self):
        """True/False, or None if the check couldn't run (firmware < 3.2.0 has no
        'repeat' field). No verdict at all beats the false negative the old
        absolute roughness threshold gave on working units."""
        ratio = self.sensor_repeat_ratio
        if ratio is None:
            return None
        return ratio >= SENSOR_REPEAT_RATIO_THRESHOLD


class Measurement:
    def __init__(self, unit_number, mode, n_scans, int_time, saturated, raw_counts):
        self.unit_number = unit_number
        self.mode = mode
        self.n_scans = n_scans
        self.int_time = int_time
        self.saturated = saturated
        self.raw_counts = raw_counts


def _version_tuple(text):
    """'3.3.0' -> (3, 3, 0). Unparseable text returns () so an unknown firmware is
    treated as the older one (compares below every real version)."""
    parts = []
    for piece in str(text).split('.'):
        try:
            parts.append(int(piece))
        except ValueError:
            return ()
    return tuple(parts)


def list_ports():
    if IS_ANDROID:
        return [d.getDeviceName() for d in usb.get_usb_device_list()]
    return [p.device for p in serial.tools.list_ports.comports()]


def _likely_usb_ports():
    """USB serial ports, found via hwid rather than device name convention (Linux only)."""
    return [p.device for p in serial.tools.list_ports.comports() if 'VID:PID' in (p.hwid or '')]


def _short(text):
    """Trim a reply for the log, marking that it was trimmed."""
    if len(text) <= _LOG_REPLY_CHARS:
        return text
    return '%s... (%d chars)' % (text[:_LOG_REPLY_CHARS], len(text))


def _checksum(payload):
    total = 0
    for ch in payload:
        total = (total + ord(ch)) & 0xFFFF
    return total


def _median(values):
    """Median of a list (empty -> None); caller may sort in place or not."""
    if not values:
        return None
    values = sorted(values)
    return values[len(values) // 2]


class SerialConnection:
    def __init__(self, port=None, timeout=COMMAND_TIMEOUT):
        ports = list_ports()
        if not ports:
            raise SpecError("No serial devices found; is the OSpRad plugged in?")

        if port is None:
            if IS_ANDROID:
                usb_ports = [p for p in ports if 'USB' in p or 'ACM' in p]
            else:
                usb_ports = _likely_usb_ports()
            log.debug('ports: %s; USB serial: %s', ports, usb_ports)
            port = usb_ports[0] if usb_ports else ports[0]
            if not usb_ports:
                log.warning('No USB serial port identified; falling back to %s', port)
        self.port = port
        # One port, several callers: the main window measures on a worker thread
        # while the calibration tabs still issue commands from the GUI thread.
        # See _busy().
        self._lock = threading.Lock()
        # Per connection so the Settings tab can change it without a reconnect.
        self.measure_timeout = MEASURE_TIMEOUT
        # Filled in by get_config(). Gates the live measure command below.
        self.firmware_version = ()
        log.info('Opening %s at 115200 (timeout %ss)', port, timeout)

        if IS_ANDROID:
            device = usb.get_usb_device(port)
            while not usb.has_usb_permission(device):
                usb.request_usb_permission(device)
                time.sleep(1)
            self._ser = serial4a.get_serial_port(port, 115200, 8, 'N', 1, timeout=timeout)
        else:
            try:
                self._ser = serial.Serial(port, 115200, timeout=timeout)
            except (serial.SerialException, OSError) as exc:
                # Raw pyserial errors aren't actionable; usually another program
                # holds the port.
                raise SpecError(
                    "Could not open %s (%s).\n\nThis usually means another program "
                    "has the port open (close the Arduino IDE's Serial Monitor or any "
                    "other OSpRad window) or that the USB driver isn't installed. "
                    "Unplug and replug the OSpRad, then try again." % (port, exc)) from exc

        # Opening the port resets the Nano. Wait out the bootloader before talking.
        log.debug('Port open, waiting 2.5s for the bootloader')
        time.sleep(2.5)

    def cancel_read(self):
        """Interrupt a blocking read from another thread.

        pyserial's documented way to unblock a read in progress (a self pipe on
        posix, an event on win32), so a measurement that would otherwise hold the
        GUI's worker for minutes can be abandoned. Backends without it (usbserial4a
        on Android) are unblocked by closing the port instead.

        Note this only unblocks us: the unit carries on measuring and will
        eventually send its DATA line. The caller must resync the link
        (reconnecting resets the Nano, which aborts the scan) rather than issue
        another command into it.
        """
        log.info('Cancelling the read in progress on %s', self.port)
        cancel = getattr(self._ser, 'cancel_read', None)
        if cancel is not None:
            try:
                cancel()
                return True
            except Exception as exc:
                log.warning('cancel_read failed (%s); closing the port instead', exc)
        try:
            self._ser.close()
        except Exception as exc:
            log.warning('Could not close the port to cancel: %s', exc)
            return False
        return True

    def close(self):
        log.info('Closing %s', self.port)
        self._ser.close()

    @contextlib.contextmanager
    def _busy(self, what):
        """Hold the port for one exchange, refusing rather than queueing.

        Two interleaved command streams on one serial line return each other's
        replies, so overlap has to be prevented. Refusing beats blocking: waiting
        would park the GUI thread behind a read that can legitimately take 90
        seconds. Every caller already handles SpecError, so this surfaces as a
        readable message.
        """
        if not self._lock.acquire(blocking=False):
            log.warning('Refused %s; the port is busy with another exchange', what)
            raise SpecError('The OSpRad is busy with another measurement; wait for it '
                            'to finish, then try again.')
        try:
            yield
        finally:
            self._lock.release()

    def _write(self, data):
        try:
            self._ser.write(data)
        except _TransportError as exc:
            raise SpecTransportError("USB write failed: %s" % exc) from exc

    def _readline(self):
        try:
            raw = self._ser.readline()
        except _TransportError as exc:
            raise SpecTransportError("USB read failed: %s" % exc) from exc
        if not raw:
            # NOT a transport error: the commonest cause is a measurement that is
            # legitimately still running (a dark scene can take minutes), and
            # marking the connection dead there would disconnect a perfectly
            # healthy unit.
            raise SpecTimeoutError("No reply from OSpRad (timed out)")
        return raw.decode('ascii', errors='replace').strip()

    def _command(self, cmd, expect="OK", retries=2):
        with self._busy('command %r' % cmd):
            return self._command_locked(cmd, expect, retries)

    def _command_locked(self, cmd, expect="OK", retries=2):
        # Retrying is safe: every command sets an absolute value, not an increment.
        for attempt in range(retries + 1):
            try:
                log.debug('-> %s', cmd)
                self._write(str.encode(cmd + '\n'))
                line = self._readline()
                log.debug('<- %s', _short(line))
            except SpecProtocolError as exc:
                if attempt == retries:
                    log.error('Command %r failed after %d attempts: %s',
                              cmd, retries + 1, exc)
                    raise
                # Previously invisible: "it silently retried twice" is exactly the
                # thing worth knowing when a unit is behaving intermittently.
                log.warning('Command %r attempt %d/%d failed (%s); retrying',
                            cmd, attempt + 1, retries + 1, exc)
                continue
            if line.startswith("ERR,"):
                raise SpecCommandError(line[4:])
            if not line.startswith(expect + ","):
                raise SpecProtocolError(
                    "Expected a %s reply but got: %r. Check the OSpRad is running "
                    "%d.x firmware." % (expect, line[:60], REQUIRED_FIRMWARE_MAJOR))
            return line

    def sensor_self_test(self, samples=3):
        """Run the 'd' diagnostic (raw scans, no servo movement) `samples` times and
        return the median (range, roughness, repeat). roughness/repeat come back
        None on firmware that doesn't report them. No equivalent for the servo: RC
        servos are open loop (no feedback wire)."""
        ranges = []
        roughnesses = []
        repeats = []
        for _ in range(samples):
            line = self._command("d", expect="DIAG")
            fields = {}
            for part in line[5:].split(','):
                if ':' in part:
                    key, _, value = part.partition(':')
                    fields[key] = value
            try:
                raw_min = int(fields['min'])
                raw_max = int(fields['max'])
            except (KeyError, ValueError) as exc:
                raise SpecProtocolError("Could not read sensor self test: %s" % line) from exc
            ranges.append(raw_max - raw_min)
            for key, collected in (('roughness', roughnesses), ('repeat', repeats)):
                if key in fields:
                    try:
                        collected.append(float(fields[key]))
                    except ValueError:
                        pass

        return _median(ranges), _median(roughnesses), _median(repeats)

    def get_config(self):
        line = self._command("g", expect="CFG")
        fields = {}
        for part in line[4:].split(','):
            if ':' in part:
                key, _, value = part.partition(':')
                fields[key] = value
        try:
            config = UnitConfig(
                unit_number=int(fields['unit']),
                dark=int(fields['dark']),
                irr=int(fields['irr']),
                rad=int(fields['rad']),
                configured=fields['configured'] == '1',
                firmware=fields.get('fw', 'unknown'),
            )
        except (KeyError, ValueError) as exc:
            raise SpecProtocolError("Could not read unit config: %s" % line) from exc
        self.firmware_version = _version_tuple(config.firmware)
        return config

    def _probe_config(self):
        """Send the initial 'g' handshake with a few short timeout retries.

        Opening the port resets the Nano, and bootloaders vary by ~0.5 to 2.5s
        before the sketch runs. A command sent too early is swallowed, so one
        attempt at the full 90s timeout would look like missing firmware. Each
        retry flushes the input buffer so a late reply can't masquerade as the
        next command's.
        """
        original_timeout = self._ser.timeout
        self._ser.timeout = 3
        try:
            for attempt in range(3):
                self._ser.reset_input_buffer()
                try:
                    config = self.get_config()
                    log.info('Handshake OK on attempt %d: unit #%d, firmware v%s',
                             attempt + 1, config.unit_number, config.firmware)
                    return config
                except SpecProtocolError as exc:
                    if attempt == 2:
                        raise
                    log.debug('Handshake attempt %d/3 got nothing (%s)', attempt + 1, exc)
        finally:
            self._ser.timeout = original_timeout
            self._ser.reset_input_buffer()

    def check_firmware(self):
        try:
            config = self._probe_config()
        except SpecProtocolError as exc:
            # 1.x firmware has no config command at all, so it stays silent.
            raise SpecProtocolError(
                "The OSpRad on %s did not respond to a configuration request (%s).\n\n"
                "This usually means it is still running 1.x firmware. Flash %s onto the "
                "Arduino Nano using the Arduino IDE, then reconnect."
                % (self.port, exc, FIRMWARE_HINT)) from exc

        try:
            major = int(config.firmware.split('.')[0])
        except ValueError:
            major = None
        if major != REQUIRED_FIRMWARE_MAJOR:
            raise SpecProtocolError(
                "OSpRad is running firmware %s but this app needs %d.x. "
                "Please reflash %s via the Arduino IDE."
                % (config.firmware, REQUIRED_FIRMWARE_MAJOR, FIRMWARE_HINT))

        try:
            (config.sensor_scan_range, config.sensor_roughness,
             config.sensor_repeat) = self.sensor_self_test()
        except SpecProtocolError:
            # Advisory only. A flaky reply must not block an otherwise good
            # connection.
            pass

        return config

    def set_unit_number(self, number):
        self._command("u%d" % number)

    def jog_wheel(self, angle):
        self._command("w%d" % angle)

    def park_wheel(self):
        """Close the shutter. Firmware replies before the servo moves, so this
        returns in milliseconds while the servo settles in the background."""
        self._command("p", expect="OK,parked")

    def save_wheel_position(self, role):
        self._command("s%s" % role.upper()[0])

    def set_integration_time(self, ms):
        self._command("t%d" % ms)

    def set_scan_range(self, n_min, n_max):
        self._command("n%d" % n_min)
        self._command("a%d" % n_max)

    @property
    def supports_live_measure(self):
        """Whether the unit understands the live measure command."""
        return self.firmware_version >= LIVE_MEASURE_FIRMWARE

    def measure(self, mode, retries=2, live=False):
        """mode: 'r' (radiance) or 'i' (irradiance).

        live=True asks the firmware to reuse the dark reference it already holds,
        skipping the move to the dark position and its block of scans. Only the
        speed changes; the reply is the same DATA line, and the firmware falls
        back to a full measurement whenever the cached dark does not match this
        exposure. Silently ignored on firmware older than LIVE_MEASURE_FIRMWARE,
        so callers never have to check.
        """
        command = ('l' + mode) if (live and self.supports_live_measure) else mode
        with self._busy('measure %r' % command):
            return self._measure_locked(command, retries)

    def _measure_locked(self, command, retries=2):
        original_timeout = self._ser.timeout
        # A measurement is the one exchange that can legitimately run for minutes.
        timeout = self.measure_timeout
        self._ser.timeout = timeout
        try:
            for attempt in range(retries + 1):
                try:
                    log.debug('-> measure %r (attempt %d/%d, timeout %ds)',
                              command, attempt + 1, retries + 1, timeout)
                    started = time.time()
                    self._write(str.encode(command + '\n'))
                    measurement = self._parse_measurement(self._readline())
                    log.debug('<- measurement in %.1fs', time.time() - started)
                    return measurement
                except SpecTimeoutError:
                    # Never retried: if it timed out it was slow, not unlucky, so
                    # a retry just repeats the same multi minute scan (and moves
                    # the shutter wheel through it all over again).
                    log.error('Measurement timed out after %ds', timeout)
                    raise
                except SpecProtocolError as exc:
                    if attempt == retries:
                        log.error('Measurement failed after %d attempts: %s',
                                  retries + 1, exc)
                        raise
                    log.warning('Measurement attempt %d/%d failed (%s); retrying',
                                attempt + 1, retries + 1, exc)
        finally:
            self._ser.timeout = original_timeout

    def _parse_measurement(self, line):
        if line.startswith("ERR,"):
            raise SpecCommandError(line[4:])
        if not line.startswith("DATA,"):
            raise SpecProtocolError(
                "Expected a measurement but got: %r. Check the OSpRad is running "
                "%d.x firmware." % (line[:60], REQUIRED_FIRMWARE_MAJOR))

        payload, _, checksum_field = line.rpartition(',')
        try:
            expected = int(checksum_field, 16)
        except ValueError as exc:
            raise SpecProtocolError("Measurement is missing its checksum") from exc
        if _checksum(payload) != expected:
            # Value count distinguishes truncation (short count) from corruption
            # (full count, bad checksum). Completely different causes.
            raise SpecProtocolError(
                "Measurement was corrupted in transit (got %d of %d values, "
                "checksum %04X but expected %04X)"
                % (len(payload[5:].split(',')) - 5, PIXELS,
                   _checksum(payload), expected))

        fields = payload[5:].split(',')
        if len(fields) != PIXELS + 5:
            raise SpecProtocolError(
                "Measurement has %d values, expected %d" % (len(fields) - 5, PIXELS))
        try:
            values = [float(f) for f in fields]
        except ValueError as exc:
            raise SpecProtocolError("Measurement contains non numeric data") from exc

        measurement = Measurement(
            unit_number=int(values[0]),
            mode='r' if int(values[1]) == 1 else 'i',
            n_scans=int(values[2]),
            int_time=int(values[3]),
            saturated=values[4],
            raw_counts=values[5:],
        )
        log.debug('parsed unit=%d mode=%s n_scans=%d int_time=%dms saturated=%s',
                  measurement.unit_number, measurement.mode, measurement.n_scans,
                  measurement.int_time, measurement.saturated)
        return measurement
