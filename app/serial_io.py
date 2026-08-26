# Serial transport. Requires OSpRad firmware 3.x: newline-terminated commands, framed
# OK/ERR/CFG/DATA/DIAG replies.

import sys
import time

PIXELS = 288

# Wire protocol is tied to the firmware's major version.
REQUIRED_FIRMWARE_MAJOR = 3
FIRMWARE_HINT = 'firmware/OSpRad_firmware'

# Sensor self-test verdict: roughness (spatial, adjacent pixels within one scan) divided
# by repeat (temporal, the same pixel across two scans 150ms apart).
#
# A connected sensor's readout repeats, because its pixel-to-pixel fixed pattern is a
# physical property, so repeat is read noise only and the ratio lands near or above 1.
# A floating VIDEO pin picks up slow drifting interference: uncorrelated scan to scan,
# so repeat runs to tens of counts while roughness stays around 1, giving a ratio near
# 0.03. Being a ratio, it doesn't care about light level, integration time or wheel
# position - which is why the absolute roughness threshold it replaced read "not
# detected" on working units measuring in the dark.
SENSOR_REPEAT_RATIO_THRESHOLD = 0.5

# Floor on the divisor: repeat can legitimately be 0.00 on a quiet connected unit.
_MIN_REPEAT = 0.5

# CPython only defines this when built for Android, so it holds whatever the p4a
# bootstrap does to the environment. The old ANDROID_* env sweep was true on any
# machine with the Android SDK installed - including CI runners - which sent
# desktop builds down the usb4a path.
IS_ANDROID = hasattr(sys, 'getandroidapilevel')

if IS_ANDROID:
    from usb4a import usb
    from usbserial4a import serial4a
    # usbserial4a raises this on a transient USB hiccup rather than a real disconnect;
    # folded into SpecProtocolError so the per-command retry loop covers it.
    from usbserial4a.utilserial4a import SerialException as _TransportError
else:
    import serial
    import serial.tools.list_ports
    _TransportError = serial.SerialException


def _patch_usbserial4a_ftdi():
    """Replace usbserial4a 0.4.0's FtdiSerial._read, which has two bugs its
    cdcacm/ch34x/cp210x siblings don't:

    1. It derives the last packet's payload length as `(total % maxPacketSize) - 2`,
       which goes negative on a read that is an exact multiple of the packet size -
       failing its own `if count > 0` guard and silently dropping 62 bytes. Reads are
       capped at 1024, an exact multiple of 64, so every full read lost 62 bytes. Short
       replies fit in one packet; a measurement's ~2.3KB DATA line did not, which is
       why only measurements ever failed their checksum.
    2. It raised SerialException when its hardcoded 5s bulkTransfer timeout expired,
       but the firmware is legitimately silent far longer while measuring. Returning
       no data and letting SerialConnection's own 90s timeout govern is pyserial's
       contract and what the other three drivers do.
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
        # Raw numbers from the 'd' self-test, or None if it couldn't run.
        self.sensor_scan_range = sensor_scan_range
        self.sensor_roughness = sensor_roughness
        self.sensor_repeat = sensor_repeat

    @property
    def sensor_repeat_ratio(self):
        """roughness/repeat, or None if the self-test didn't report both."""
        if self.sensor_roughness is None or self.sensor_repeat is None:
            return None
        return self.sensor_roughness / max(self.sensor_repeat, _MIN_REPEAT)

    @property
    def sensor_detected(self):
        """True/False, or None if the check couldn't run (firmware < 3.2.0 has no
        'repeat' field). No verdict at all beats the false negative the old absolute
        roughness threshold gave on working units."""
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


def list_ports():
    if IS_ANDROID:
        return [d.getDeviceName() for d in usb.get_usb_device_list()]
    return [p.device for p in serial.tools.list_ports.comports()]


def _likely_usb_ports():
    """USB-serial ports, found via hwid rather than device-name convention (Linux-only)."""
    return [p.device for p in serial.tools.list_ports.comports() if 'VID:PID' in (p.hwid or '')]


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
    def __init__(self, port=None, timeout=90):
        ports = list_ports()
        if not ports:
            raise SpecError("No serial devices found - is the OSpRad plugged in?")

        if port is None:
            if IS_ANDROID:
                usb_ports = [p for p in ports if 'USB' in p or 'ACM' in p]
            else:
                usb_ports = _likely_usb_ports()
            port = usb_ports[0] if usb_ports else ports[0]
        self.port = port

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
                # Raw pyserial errors aren't actionable; usually another program holds the port.
                raise SpecError(
                    "Could not open %s (%s).\n\nThis usually means another program has "
                    "the port open - close the Arduino IDE's Serial Monitor or any other "
                    "OSpRad window - or that the USB driver isn't installed. Unplug and "
                    "replug the OSpRad, then try again." % (port, exc)) from exc

        time.sleep(2.5)

    def close(self):
        self._ser.close()

    def _write(self, data):
        try:
            self._ser.write(data)
        except _TransportError as exc:
            raise SpecProtocolError("USB write failed: %s" % exc) from exc

    def _readline(self):
        try:
            raw = self._ser.readline()
        except _TransportError as exc:
            raise SpecProtocolError("USB read failed: %s" % exc) from exc
        if not raw:
            raise SpecProtocolError("No reply from OSpRad (timed out)")
        return raw.decode('ascii', errors='replace').strip()

    def _command(self, cmd, expect="OK", retries=2):
        # Retrying is safe: every command sets an absolute value, not an increment.
        for attempt in range(retries + 1):
            try:
                self._write(str.encode(cmd + '\n'))
                line = self._readline()
            except SpecProtocolError:
                if attempt == retries:
                    raise
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
        return the median (range, roughness, repeat). roughness/repeat come back None
        on firmware that doesn't report them. No equivalent for the servo: RC servos
        are open-loop (no feedback wire)."""
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
                raise SpecProtocolError("Could not read sensor self-test: %s" % line) from exc
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
            return UnitConfig(
                unit_number=int(fields['unit']),
                dark=int(fields['dark']),
                irr=int(fields['irr']),
                rad=int(fields['rad']),
                configured=fields['configured'] == '1',
                firmware=fields.get('fw', 'unknown'),
            )
        except (KeyError, ValueError) as exc:
            raise SpecProtocolError("Could not read unit config: %s" % line) from exc

    def _probe_config(self):
        """Send the initial 'g' handshake with a few short-timeout retries.

        Opening the port resets the Nano, and bootloaders vary by ~0.5-2.5s before the
        sketch runs. A command sent too early is swallowed, so one attempt at the full
        90s timeout would look like missing firmware. Each retry flushes the input
        buffer so a late reply can't masquerade as the next command's.
        """
        original_timeout = self._ser.timeout
        self._ser.timeout = 3
        try:
            for attempt in range(3):
                self._ser.reset_input_buffer()
                try:
                    return self.get_config()
                except SpecProtocolError:
                    if attempt == 2:
                        raise
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
            # Advisory only - a flaky reply must not block an otherwise good connection.
            pass

        return config

    def set_unit_number(self, number):
        self._command("u%d" % number)

    def jog_wheel(self, angle):
        self._command("w%d" % angle)

    def save_wheel_position(self, role):
        self._command("s%s" % role.upper()[0])

    def set_integration_time(self, ms):
        self._command("t%d" % ms)

    def set_scan_range(self, n_min, n_max):
        self._command("n%d" % n_min)
        self._command("a%d" % n_max)

    def measure(self, mode, retries=2):
        """mode: 'r' (radiance) or 'i' (irradiance)."""
        for attempt in range(retries + 1):
            try:
                self._write(str.encode(mode + '\n'))
                return self._parse_measurement(self._readline())
            except SpecProtocolError:
                if attempt == retries:
                    raise

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
            # (full count, bad checksum) - completely different causes.
            raise SpecProtocolError(
                "Measurement was corrupted in transit (got %d of %d values, checksum "
                "%04X but expected %04X)"
                % (len(payload[5:].split(',')) - 5, PIXELS, _checksum(payload), expected))

        fields = payload[5:].split(',')
        if len(fields) != PIXELS + 5:
            raise SpecProtocolError(
                "Measurement has %d values, expected %d" % (len(fields) - 5, PIXELS))
        try:
            values = [float(f) for f in fields]
        except ValueError as exc:
            raise SpecProtocolError("Measurement contains non-numeric data") from exc

        return Measurement(
            unit_number=int(values[0]),
            mode='r' if int(values[1]) == 1 else 'i',
            n_scans=int(values[2]),
            int_time=int(values[3]),
            saturated=values[4],
            raw_counts=values[5:],
        )
