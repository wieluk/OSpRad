# OSpRad 3.1.0
# Released under GPL-3.0 license
# https://github.com/troscianko/OSpRad
#
# Requires OSpRad firmware 3.x (newline-terminated commands, framed
# OK/ERR/CFG/DATA/DIAG replies). Every command sent here must end in '\n'
# (see _command/jog_wheel/measure).

import os
import time

PIXELS = 288

# Wire protocol is tied to the firmware's major version.
REQUIRED_FIRMWARE_MAJOR = 3
FIRMWARE_HINT = 'firmware/OSpRad_firmware'

# Sensor self-test threshold: mean absolute difference between adjacent pixels.
#
# Raw ADC swing (max-min) was tried first and proved unreliable on disconnected
# units - it varied ~60-166 across identical conditions. Roughness is a more
# physically grounded signal: a real sensor reads out as a smooth curve (adjacent
# photosites correlate), while a floating pin picks up slow-drifting interference
# that moves the whole reading's amplitude around without making adjacent samples
# jump. Measured on a disconnected unit: roughness 0.7-1.2 across conditions, vs
# 2.5 on a real C12880MA reading real light. 2.0 sits with margin on both sides.
SENSOR_ROUGHNESS_THRESHOLD = 2.0

IS_ANDROID = any(key for key in os.environ if key.startswith('ANDROID_'))

if IS_ANDROID:
    from usb4a import usb
    from usbserial4a import serial4a
else:
    import serial
    import serial.tools.list_ports


class SpecError(Exception):
    pass


class SpecProtocolError(SpecError):
    """Reply was missing, malformed, or failed its checksum."""


class SpecCommandError(SpecError):
    """Firmware replied ERR,<reason>."""


class UnitConfig:
    def __init__(self, unit_number, dark, irr, rad, configured, firmware,
                 sensor_scan_range=None, sensor_roughness=None):
        self.unit_number = unit_number
        self.dark = dark
        self.irr = irr
        self.rad = rad
        self.configured = configured
        self.firmware = firmware
        # Raw numbers from the 'd' self-test scan, or None if it couldn't run.
        self.sensor_scan_range = sensor_scan_range
        self.sensor_roughness = sensor_roughness

    @property
    def sensor_detected(self):
        """True/False from sensor_roughness, or None if the self-test couldn't run."""
        if self.sensor_roughness is None:
            return None
        return self.sensor_roughness >= SENSOR_ROUGHNESS_THRESHOLD


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


def _checksum(payload):
    total = 0
    for ch in payload:
        total = (total + ord(ch)) & 0xFFFF
    return total


class SerialConnection:
    def __init__(self, port=None, timeout=90):
        ports = list_ports()
        if not ports:
            raise SpecError("No serial devices found - is the OSpRad plugged in?")

        if port is None:
            usb_ports = [p for p in ports if 'USB' in p or 'ACM' in p]
            port = usb_ports[0] if usb_ports else ports[0]
        self.port = port

        if IS_ANDROID:
            device = usb.get_usb_device(port)
            while not usb.has_usb_permission(device):
                usb.request_usb_permission(device)
                time.sleep(1)
            self._ser = serial4a.get_serial_port(port, 115200, 8, 'N', 1, timeout=timeout)
        else:
            self._ser = serial.Serial(port, 115200, timeout=timeout)

        time.sleep(2.5)

    def close(self):
        self._ser.close()

    def _readline(self):
        raw = self._ser.readline()
        if not raw:
            raise SpecProtocolError("No reply from OSpRad (timed out)")
        return raw.decode('ascii', errors='replace').strip()

    def _command(self, cmd, expect="OK"):
        self._ser.write(str.encode(cmd + '\n'))
        line = self._readline()
        if line.startswith("ERR,"):
            raise SpecCommandError(line[4:])
        if not line.startswith(expect + ","):
            raise SpecProtocolError(
                "Expected a %s reply but got: %r. Check the OSpRad is running "
                "%d.x firmware." % (expect, line[:60], REQUIRED_FIRMWARE_MAJOR))
        return line

    def sensor_self_test(self, samples=3):
        """Runs the 'd' diagnostic (a raw scan, no servo movement) samples times and
        returns (median_range, median_roughness) across those samples. There is no
        equivalent test for the filter wheel servo: RC servos have no feedback wire.
        """
        ranges = []
        roughnesses = []
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
            if 'roughness' in fields:
                try:
                    roughnesses.append(float(fields['roughness']))
                except ValueError:
                    pass
        ranges.sort()
        median_range = ranges[len(ranges) // 2]
        median_roughness = None
        if roughnesses:
            roughnesses.sort()
            median_roughness = roughnesses[len(roughnesses) // 2]
        return median_range, median_roughness

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
        """Send the initial 'g' handshake with a few retries.

        The Nano resets when the serial port is opened, and different bootloaders
        (genuine vs. the "old bootloader" some clone/Elegoo units need) take a
        variable ~0.5-2.5s before the sketch is actually running. A command sent
        too early is silently swallowed, so a single attempt risks blocking on the
        full 90s read timeout and being misreported as old/missing firmware. Each
        retry flushes the input buffer first so any late trickle of an earlier
        attempt can't masquerade as a later command's reply.
        """
        original_timeout = self._ser.timeout
        self._ser.timeout = 3
        try:
            attempts = 3
            for attempt in range(attempts):
                self._ser.reset_input_buffer()
                try:
                    return self.get_config()
                except SpecProtocolError:
                    if attempt == attempts - 1:
                        raise
        finally:
            self._ser.timeout = original_timeout
            self._ser.reset_input_buffer()

    def check_firmware(self):
        try:
            config = self._probe_config()
        except SpecProtocolError as exc:
            # 1.x firmware has no config command at all, so it simply stays silent
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
            config.sensor_scan_range, config.sensor_roughness = self.sensor_self_test()
        except SpecProtocolError:
            # Advisory only - older firmware without 'd' support, or a flaky reply,
            # should not block an otherwise good connection.
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
                self._ser.write(str.encode(mode + '\n'))
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
            raise SpecProtocolError("Measurement was corrupted in transit")

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
