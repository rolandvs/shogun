#!/usr/bin/env python3
"""
shogun.py - Linux / macOSX host tool for the SHOEMU2/EMU3 EPROM emulator

Protocol:
  UART 115200 baud, 8 data bits, no parity, 2 stop bits (device sends 8N2).
  Device answers ACK 0x06 / NAK 0x15. Numbers are sent as ASCII hex digits;
  every character of a command is answered with one ACK/NAK, except
  the 'W' block transfer (one ACK per block) and the text-output commands.

  '#'+8 hex  start address (EPROM-relative, 16 bit used); emulation OFF, prepare SRAM write
  'S'+8 hex  number of bytes to load
  'W'        block load: repeat { 2 hex len (1..0x85) + len*2 hex data } -> ACK per block
  ':'        slow load: hex pairs, every char ACKed
  '&'+4 hex  compare CRC16 (CRC-16/ARC, poly 0xA001, init 0) of received data -> ACK/NAK
  'V'        read back SRAM and compare CRC -> ACK/NAK
  'E'+1 hex  EPROM type 1=2716 2=2732 3=2764 4=27128 5=27256 6=27512 -> ACK/NAK
  'R'+4 hex  target reset pulse length (1..9 ms, or 10..500 ms in 10 ms steps) -> ACK/NAK
  'J'+8 hex  start emulation (+ reset pulse if 'R' was sent)
  'M'+1 hex  select 64K bank (1 = 0x00000-0x0FFFF, 2 = 0x10000-0x1FFFF, 0 = unchanged)
  'I'+1 hex  same as 'M' plus a target reset pulse
  'X'+2 hex  relay test (emulation off, relays = value & 0x1F)
  'N'+4 hex  no-op
  'D'+1 hex  -> version string  "EMU3" date time version '\\r'
  'Q'        -> clock string    "14.745600 MHz\\r"
  'U'        -> status string   "EMU3: OK!\\r" or an error message
"""

import argparse
import glob
import os
import sys
import time

ACK = 0x06
NAK = 0x15
BAUD = 115200

EPROM_TYPES = {  # name: (type code, size)
    "2716": (1, 0x0800),
    "2732": (2, 0x1000),
    "2764": (3, 0x2000),
    "27128": (4, 0x4000),
    "27256": (5, 0x8000),
    "27512": (6, 0x10000),
}
MAX_BLOCK = 0x85  # firmware buffer is 133 bytes


class EmuError(Exception):
    pass


# --------------------------------------------------------------------------
# Serial port (POSIX termios, no dependencies)
# --------------------------------------------------------------------------
class PosixSerial:
    def __init__(self, port, baud=BAUD):
        import termios
        import fcntl
        self.termios = termios
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self.fd, termios.TIOCEXCL)  # exclusive access
        except Exception:
            pass
        attr = termios.tcgetattr(self.fd)
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attr
        iflag = 0
        oflag = 0
        lflag = 0
        cflag = termios.CS8 | termios.CSTOPB | termios.CREAD | termios.CLOCAL
        speed = getattr(termios, "B%d" % baud)
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        # back to blocking writes
        fl = fcntl.fcntl(self.fd, fcntl.F_GETFL)
        fcntl.fcntl(self.fd, fcntl.F_SETFL, fl & ~os.O_NONBLOCK)
        self.flush_input()

    def write(self, data):
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]
        self.termios.tcdrain(self.fd)

    def read(self, n, timeout):
        import select
        out = bytearray()
        end = time.monotonic() + timeout
        while len(out) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                chunk = os.read(self.fd, n - len(out))
                if chunk:
                    out += chunk
        return bytes(out)

    def flush_input(self):
        self.termios.tcflush(self.fd, self.termios.TCIFLUSH)

    def close(self):
        os.close(self.fd)


class PySerial:
    def __init__(self, port, baud=BAUD):
        import serial
        self.s = serial.Serial(port, baud, bytesize=8, parity="N",
                               stopbits=2, timeout=0)
        self.s.reset_input_buffer()

    def write(self, data):
        self.s.write(data)
        self.s.flush()

    def read(self, n, timeout):
        self.s.timeout = timeout
        return self.s.read(n)

    def flush_input(self):
        self.s.reset_input_buffer()

    def close(self):
        self.s.close()


def open_port(port):
    if os.name == "posix":
        return PosixSerial(port)
    return PySerial(port)


def list_ports():
    pats = ["/dev/ttyUSB*", "/dev/ttyACM*", "/dev/cu.usbserial*",
            "/dev/cu.usbmodem*", "/dev/cu.SLAB*", "/dev/cu.wchusbserial*",
            "/dev/serial/by-id/*"]
    found = []
    for p in pats:
        found += sorted(glob.glob(p))
    return found


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------
class Emu:
    def __init__(self, ser, verbose=False):
        self.ser = ser
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print("  [dbg]", *a)

    def resync(self):
        """Put the device state machine back to idle.
        Any non-hex, non-command character aborts a pending command."""
        self.ser.write(b"Z")
        time.sleep(0.05)
        self.ser.read(64, 0.1)
        self.ser.flush_input()

    def _xchg(self, ch, timeout=2.0):
        self.ser.write(ch)
        r = self.ser.read(1, timeout)
        if not r:
            raise EmuError("no answer from emulator after %r (wrong port / not powered?)" % ch)
        return r[0]

    def cmd(self, letter, value=None, digits=0, timeout=2.0):
        """Send a command letter + hex digits, one char at a time, checking
        the ACK for each. Returns True on final ACK, False on final NAK."""
        chars = letter.encode()
        if value is not None:
            chars += ("%0*X" % (digits, value)).encode()
        self.log("send", chars)
        for i in range(len(chars)):
            r = self._xchg(chars[i:i + 1], timeout)
            last = i == len(chars) - 1
            if r == NAK:
                if last:
                    return False
                raise EmuError("NAK on %r (char %d of %r)" % (chars[i:i + 1], i, chars))
            if r != ACK:
                raise EmuError("unexpected answer 0x%02X to %r" % (r, chars))
        return True

    def read_line(self, timeout=1.0):
        out = bytearray()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            b = self.ser.read(1, max(0.0, end - time.monotonic()))
            if not b:
                break
            if b == b"\r" or b == b"\n":
                if out:
                    break
                continue
            out += b
        return out.decode("latin-1")

    # ---- info -----------------------------------------------------------
    def version(self):
        if not self.cmd("D"):
            raise EmuError("'D' refused")
        self.ser.write(b"0")
        s = self.read_line()
        if not s.startswith("EMU"):
            raise EmuError("unexpected version answer %r" % s)
        return {"type": s[0:4], "date": s[4:15], "time": s[15:23],
                "version": s[23:], "raw": s}

    def clock(self):
        self.ser.write(b"Q")
        return self.read_line()

    def status(self):
        self.ser.write(b"U")
        return self.read_line()

    # ---- load -----------------------------------------------------------
    def set_type(self, code):
        if not self.cmd("E", code, 1):
            raise EmuError("EPROM type %d refused" % code)

    def set_bank(self, bank, with_reset=False):
        if not self.cmd("I" if with_reset else "M", 2 if bank else 1, 1):
            raise EmuError("bank select refused")

    def set_reset_time(self, ms):
        if not (1 <= ms <= 9 or 10 <= ms <= 500):
            raise EmuError("reset time must be 1..500 ms")
        if not self.cmd("R", ms, 4):
            raise EmuError("reset time %d ms refused" % ms)

    def download(self, addr, data, slow=False, progress=None):
        n = len(data)
        if not self.cmd("#", addr, 8):
            raise EmuError("start address refused")
        if not self.cmd("S", n, 8):
            raise EmuError("byte count refused")
        crc = crc16(data)
        if slow:
            if not self.cmd(":"):
                raise EmuError("':' refused")
            for i, b in enumerate(data):
                for c in ("%02X" % b).encode():
                    if self._xchg(bytes([c])) != ACK:
                        raise EmuError("NAK at byte %d" % i)
                if progress and (i & 0xFF) == 0:
                    progress(i, n)
        else:
            if not self.cmd("W"):
                raise EmuError("'W' refused")
            pos = 0
            while pos < n:
                blk = data[pos:pos + 128]
                frame = ("%02X" % len(blk)).encode() + blk.hex().upper().encode()
                self.ser.write(frame)
                r = self.ser.read(1, 3.0)
                if not r:
                    raise EmuError("timeout after block at 0x%X" % pos)
                if r[0] != ACK:
                    raise EmuError("NAK/0x%02X after block at 0x%X" % (r[0], pos))
                pos += len(blk)
                if progress:
                    progress(pos, n)
        if progress and slow:
            progress(n, n)
        if not self.cmd("&", crc, 4):
            raise EmuError("CRC16 mismatch on transfer (host 0x%04X)" % crc)
        return crc

    def verify(self, size):
        self.ser.write(b"V")
        r = self.ser.read(1, 5.0 + size / 20000.0)
        if not r:
            raise EmuError("no answer to verify")
        return r[0] == ACK

    def start(self):
        if not self.cmd("J", 0, 8):
            raise EmuError("start refused")

    def stop(self):
        if not self.cmd("X", 0, 2):
            raise EmuError("stop refused")

    def reset_target(self):
        if not self.cmd("I", 0, 1):
            raise EmuError("reset refused")


def crc16(data, crc=0):
    """CRC-16/ARC as used by the firmware (CalcCRC16)."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


# --------------------------------------------------------------------------
# File loading
# --------------------------------------------------------------------------
def load_file(path):
    """Return dict {address: byte}."""
    raw = open(path, "rb").read()
    text = None
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        pass
    mem = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()] if text else []
    if lines and all(l.startswith(":") for l in lines):
        base = 0
        for l in lines:
            rec = bytes.fromhex(l[1:])
            if sum(rec) & 0xFF:
                raise EmuError("Intel HEX checksum error: %s" % l)
            cnt, a, typ = rec[0], (rec[1] << 8) | rec[2], rec[3]
            d = rec[4:4 + cnt]
            if typ == 0:
                for i, b in enumerate(d):
                    mem[base + a + i] = b
            elif typ == 1:
                break
            elif typ == 2:
                base = ((d[0] << 8) | d[1]) << 4
            elif typ == 4:
                base = ((d[0] << 8) | d[1]) << 16
        return mem, "Intel HEX"
    if lines and all(l.startswith("S") for l in lines):
        for l in lines:
            t = l[1]
            rec = bytes.fromhex(l[2:])
            if (sum(rec) & 0xFF) != 0xFF:
                raise EmuError("S-record checksum error: %s" % l)
            alen = {"1": 2, "2": 3, "3": 4}.get(t)
            if alen:
                a = int.from_bytes(rec[1:1 + alen], "big")
                for i, b in enumerate(rec[1 + alen:-1]):
                    mem[a + i] = b
        return mem, "Motorola S-record"
    return {i: b for i, b in enumerate(raw)}, "binary"


def build_image(mem, offset, size, fill_all):
    """Map file addresses to EPROM addresses (eprom = file - offset).
    Returns (start, bytes)."""
    sel = {a - offset: b for a, b in mem.items() if 0 <= a - offset}
    over = [a for a in sel if a >= size]
    if over:
        raise EmuError("data does not fit the EPROM: highest address 0x%X, EPROM size 0x%X "
                       "(use --offset or a larger --type)" % (max(over), size))
    if not sel:
        raise EmuError("no data in EPROM range (check --offset)")
    if fill_all:
        lo, hi = 0, size
    else:
        lo, hi = min(sel), max(sel) + 1
    img = bytearray(b"\xff" * (hi - lo))
    for a, b in sel.items():
        img[a - lo] = b
    return lo, bytes(img)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def progress_bar(done, total):
    w = 40
    f = int(w * done / total) if total else w
    sys.stdout.write("\r  [%s%s] %d/%d bytes" % ("#" * f, "." * (w - f), done, total))
    if done >= total:
        sys.stdout.write("\n")
    sys.stdout.flush()


def parse_int(s):
    return int(s, 0)


def main():
    ap = argparse.ArgumentParser(
        description="SHOGUN EPROM emulator tool for Linux and OSX",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  shogun.py ports\n"
               "  shogun.py -p /dev/ttyUSB0 info\n"
               "  shogun.py -p /dev/ttyUSB0 load diag.bin -t 27256 --reset-ms 400\n"
               "  shogun.py -p /dev/cu.usbserial-1410 load rom.hex -t 2764 --offset 0xE000\n"
               "  shogun.py -p /dev/ttyUSB0 reset\n")
    ap.add_argument("-p", "--port", default=os.environ.get("SHOGUN_PORT"),
                    help="serial port (or set SHOGUN_PORT)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("ports", help="list likely serial ports")
    sub.add_parser("info", help="show firmware version, clock and status")

    lp = sub.add_parser("load", help="load an image and start emulation")
    lp.add_argument("file", help=".bin, Intel .hex or Motorola .s19/.srec")
    lp.add_argument("-t", "--type", required=True, choices=list(EPROM_TYPES),
                    help="EPROM type")
    lp.add_argument("--offset", type=parse_int, default=0,
                    help="file address that maps to EPROM address 0 (default 0)")
    lp.add_argument("--fill", action="store_true",
                    help="send the whole EPROM size, unused bytes = 0xFF")
    lp.add_argument("--reset-ms", type=int, default=400,
                    help="target reset pulse in ms (1..500, default 400)")
    lp.add_argument("--no-reset", action="store_true", help="do not reset the target")
    lp.add_argument("--bank", type=int, choices=[0, 1],
                    help="64K SRAM bank (0 = 0x00000-0x0FFFF, 1 = 0x10000-0x1FFFF)")
    lp.add_argument("--no-verify", action="store_true", help="skip SRAM read-back")
    lp.add_argument("--no-start", action="store_true",
                    help="load only, leave emulation off")
    lp.add_argument("--slow", action="store_true",
                    help="use the char-by-char ':' transfer instead of 'W' blocks")

    rp = sub.add_parser("reset", help="pulse the target reset line")
    rp.add_argument("--ms", type=int, default=400)

    bp = sub.add_parser("bank", help="switch 64K SRAM bank (for two images)")
    bp.add_argument("bank", type=int, choices=[0, 1])
    bp.add_argument("--reset", action="store_true", help="also reset target")
    bp.add_argument("--ms", type=int, default=400)

    sub.add_parser("start", help="start emulation with the data already in SRAM")
    sub.add_parser("stop", help="stop emulation (disconnect from target)")

    a = ap.parse_args()
    if a.cmd is None:
        ap.print_help()
        return 1
    if a.cmd == "ports":
        ps = list_ports()
        print("\n".join(ps) if ps else "no USB serial ports found")
        return 0
    if not a.port:
        ps = list_ports()
        if len(ps) == 1:
            a.port = ps[0]
            print("using port", a.port)
        else:
            ap.error("give a port with -p (found: %s)" % (", ".join(ps) or "none"))

    ser = open_port(a.port)
    emu = Emu(ser, a.verbose)
    try:
        emu.resync()
        if a.cmd == "info":
            v = emu.version()
            print("Emulator : %s" % v["type"])
            print("Firmware : %s (built %s %s)" % (v["version"], v["date"], v["time"]))
            print("Clock    : %s" % emu.clock())
            print("Status   : %s" % emu.status())

        elif a.cmd == "load":
            code, size = EPROM_TYPES[a.type]
            mem, fmt = load_file(a.file)
            start, img = build_image(mem, a.offset, size, a.fill)
            v = emu.version()
            print("Emulator %s firmware %s on %s" % (v["type"], v["version"], a.port))
            print("File %s (%s), EPROM %s, address 0x%04X-0x%04X, %d bytes"
                  % (os.path.basename(a.file), fmt, a.type, start,
                     start + len(img) - 1, len(img)))
            t0 = time.monotonic()
            emu.set_type(code)
            if a.bank is not None:
                emu.set_bank(a.bank)
            crc = emu.download(start, img, slow=a.slow,
                               progress=None if a.verbose else progress_bar)
            print("CRC16 %04X ok" % crc)
            if not a.no_verify:
                if not emu.verify(len(img)):
                    raise EmuError("verify FAILED (SRAM read-back CRC differs)")
                print("Verify ok")
            emu.set_type(code)  # '#' switched the socket relays off; switch back on
            if not a.no_start:
                if not a.no_reset:
                    emu.set_reset_time(a.reset_ms)
                emu.start()
                print("Emulation running%s" % ("" if a.no_reset else
                                                ", target reset %d ms" % a.reset_ms))
            print("Done in %d ms" % ((time.monotonic() - t0) * 1000))

        elif a.cmd == "reset":
            emu.set_reset_time(a.ms)
            emu.reset_target()
            print("target reset %d ms" % a.ms)

        elif a.cmd == "bank":
            if a.reset:
                emu.set_reset_time(a.ms)
            emu.set_bank(a.bank, with_reset=a.reset)
            print("bank %d selected%s" % (a.bank, " + reset" if a.reset else ""))

        elif a.cmd == "start":
            emu.start()
            print("emulation running")

        elif a.cmd == "stop":
            emu.stop()
            print("emulation stopped")
    except EmuError as e:
        print("ERROR:", e, file=sys.stderr)
        try:
            emu.resync()
        except Exception:
            pass
        return 2
    finally:
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
