# Flashes firmware through the Nano's own bootloader (STK500v1, what avrdude's
# "arduino" programmer speaks), in plain Python over the same serial port the app
# uses, so it works identically on every platform including Android. The bootloader
# never writes EEPROM, so the unit number and wheel positions survive a flash.

import logging
import time

import serial_io

log = logging.getLogger('osprad.flasher')

PAGE = 128  # ATmega328P flash page, bytes
FLASH_LIMIT = 30720  # what the Arduino IDE allows on a Nano, leaving room for any bootloader
SIGNATURES = {b'\x1e\x95\x0f': 'ATmega328P', b'\x1e\x95\x14': 'ATmega328'}
BAUDS = (115200, 57600)  # current optiboot Nanos, then the old bootloader

# STK500v1 bytes.
INSYNC, OK, EOP = b'\x14', b'\x10', b' '
GET_SYNC, READ_SIGN, ENTER, LEAVE = b'0', b'u', b'P', b'Q'
LOAD_ADDRESS, PROG_PAGE, READ_PAGE = b'U', b'd', b't'


class FlashError(Exception):
    pass


def parse_hex(text):
    """Intel HEX text to a flash image starting at address 0, padded to whole pages."""
    data = {}
    base = 0
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = bytes.fromhex(line[1:]) if line.startswith(':') else b''
        except ValueError:
            raw = b''
        if len(raw) < 5 or len(raw) != raw[0] + 5 or sum(raw) & 0xFF:
            raise FlashError('Line %d is not valid Intel HEX.' % number)
        count, kind, payload = raw[0], raw[3], raw[4:4 + raw[0]]
        address = base + int.from_bytes(raw[1:3], 'big')
        if kind == 0:
            for i in range(count):
                data[address + i] = payload[i]
        elif kind == 1:
            break
        elif kind == 2:
            base = int.from_bytes(payload, 'big') << 4
        elif kind == 4:
            base = int.from_bytes(payload, 'big') << 16
    if not data:
        raise FlashError('The file contains no program data.')
    end = max(data) + 1
    if end > FLASH_LIMIT:
        raise FlashError('The program is %d bytes; at most %d fit beside the bootloader.'
                         % (end, FLASH_LIMIT))
    image = bytearray(b'\xff' * (-(-end // PAGE) * PAGE))
    for address, value in data.items():
        image[address] = value
    return bytes(image)


class _Bootloader:
    def __init__(self, port):
        self.port = port

    def command(self, body, reply=0):
        self.port.write(body + EOP)
        if self._read(1) != INSYNC:
            raise FlashError('The bootloader lost sync.')
        data = self._read(reply)
        if self._read(1) != OK:
            raise FlashError('The bootloader refused a command.')
        return data

    def _read(self, n):
        data = self.port.read(n) if n else b''
        if len(data) != n:
            raise FlashError('No reply from the bootloader.')
        return data

    def sync(self):
        """Reset the Nano (DTR pulse) and catch its bootloader in the ~1s it listens."""
        self.port.dtr = False
        time.sleep(0.25)
        self.port.dtr = True
        time.sleep(0.05)
        # One sync at a time, waited out: optiboot blinks for ~0.3s before listening,
        # and more than two bytes queued meanwhile overrun its UART, misalign the
        # protocol and make it jump to the sketch.
        self.port.timeout = 0.5
        for _ in range(6):
            self.port.reset_input_buffer()
            self.port.write(GET_SYNC + EOP)
            if self.port.read(2) == INSYNC + OK:
                time.sleep(0.05)
                self.port.reset_input_buffer()
                # A second answer proves it is still the bootloader listening, in step.
                self.port.write(GET_SYNC + EOP)
                if self.port.read(2) == INSYNC + OK:
                    self.port.timeout = 1.0
                    return True
        return False


def flash(port_name, image, progress=None, open_port=serial_io.open_port):
    """Write and verify `image` (from parse_hex). Returns (chip name, baud)."""
    total = 2 * (len(image) // PAGE)
    for baud in BAUDS:
        port = open_port(port_name, baud, 1.0)
        try:
            boot = _Bootloader(port)
            if not boot.sync():
                log.info('No bootloader answered at %d baud', baud)
                continue
            log.info('Bootloader answered at %d baud', baud)
            signature = boot.command(READ_SIGN, 3)
            if signature not in SIGNATURES:
                raise FlashError('Unexpected chip (signature %s); not flashing.' % signature.hex())
            boot.command(ENTER)
            done = 0
            for verify in (False, True):
                log.info('%s %d bytes', 'Verifying' if verify else 'Writing', len(image))
                for address in range(0, len(image), PAGE):
                    page = image[address:address + PAGE]
                    boot.command(LOAD_ADDRESS + (address // 2).to_bytes(2, 'little'))
                    length = len(page).to_bytes(2, 'big')
                    if verify:
                        if boot.command(READ_PAGE + length + b'F', len(page)) != page:
                            raise FlashError('Verification failed at 0x%04x; flash again.'
                                             % address)
                    else:
                        boot.command(PROG_PAGE + length + b'F' + page)
                    done += 1
                    if progress is not None:
                        progress(done, total)
            boot.command(LEAVE)
            log.info('Flashed %d bytes to an %s at %d baud', len(image),
                     SIGNATURES[signature], baud)
            return SIGNATURES[signature], baud
        finally:
            port.close()
    raise FlashError('No Arduino bootloader answered at %s baud. Check the cable and that '
                     'nothing else has the port open.' % ' or '.join(map(str, BAUDS)))
