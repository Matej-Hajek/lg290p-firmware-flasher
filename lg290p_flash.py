#!/usr/bin/env python3
"""Flash firmware onto a Quectel LG290P(03) GNSS module over UART, no Windows/QGNSS.

This is a clean-room implementation of the proprietary Quectel bootloader upgrade
protocol described in *LG290P(03)&LGx80P(03) Firmware Upgrade Guide V1.1*. QGNSS
(the vendor's Windows GUI) is merely a front-end for this same UART protocol.

Why this exists: on some boards (e.g. Waveshare LG290P with a CH34x USB-UART
bridge) QGNSS cannot flash at all, because it relies on RTS/DTR to reset the
module into its bootloader and those lines are not wired to the reset pin. This
tool triggers the reset in software (`$PQTMSRR`) and then synchronises, so it
works where QGNSS does not.

Flash sequence: soft-reset -> sync into bootloader command mode ->
Firmware Information -> Erase -> data in 4 KiB packets -> Reset.

Wire frame:  0xAA | ClassID | MsgID | Len(BE,2) | Payload | CRC32(BE,4) | 0x55
CRC32 is standard IEEE (zlib.crc32) computed over ClassID..Payload.

See PROTOCOL.md for the full wire specification.

WARNING: the module has no backup/dual-bank firmware. An interrupted flash leaves
it without a runnable application until a new flash completes. The bootloader
itself survives (it re-opens the sync window on every reset), so a failed flash
is recoverable by flashing again -- but do not cut power mid-flash.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from zlib import crc32 as _zlib_crc32

try:
    import serial  # type: ignore
except ImportError:  # pragma: no cover - import guard
    sys.exit(
        "Missing pyserial. Install it with: pip install pyserial\n"
        "(or run inside a virtualenv that has it)."
    )

# --- protocol constants (Guide Table 1); SYNC/RSP words go little-endian on the wire ---
SYNC_WORD1 = 0x514C1309
RSP_WORD1 = 0xAAFC3A4D
SYNC_WORD2 = 0x1203A504
RSP_WORD2 = 0x55FD5BA0

HEADER = 0xAA
TAIL = 0x55
CLASS_UPGRADE = 0x02

MSG_FW_INFO = 0x02
MSG_ERASE = 0x03
MSG_DATA = 0x04
MSG_RESET = 0x31
MSG_BOOTVER = 0x71
MSG_RESPONSE = 0x00

PACKET_SIZE = 4096          # data packet size (Guide 2.4.3)
DEFAULT_BAUD = 460800       # factory / upgrade baud rate (Guide 2.1)

STATUS_OK = 0x0000
STATUS: dict[int, str] = {
    0x0000: "OK",
    0x0001: "unknown error",
    0x0002: "CRC32 error",
    0x0003: "timeout",
    0x0004: "unsupported message",
    0x0005: "package error",
    0x0020: "flash erase error",
    0x0021: "flash write error",
}


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) -- fully unit-testable, see tests/test_protocol.py
# --------------------------------------------------------------------------- #
def crc32(data: bytes) -> int:
    """Standard IEEE CRC-32 (poly 0xEDB88320), matching the Guide's Appendix B table."""
    return _zlib_crc32(data) & 0xFFFFFFFF


def build_frame(msg_id: int, payload: bytes) -> bytes:
    """Build a host->module frame: 0xAA | Class | Msg | Len(BE) | Payload | CRC32(BE) | 0x55."""
    body = bytes([CLASS_UPGRADE, msg_id]) + struct.pack(">H", len(payload)) + payload
    return bytes([HEADER]) + body + struct.pack(">I", crc32(body)) + bytes([TAIL])


def nmea_command(body: str) -> bytes:
    r"""Build an NMEA command, e.g. 'PQTMSRR' -> b'$PQTMSRR*CS\r\n' (XOR checksum)."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}\r\n".encode("ascii")


def firmware_info_payload(firmware: bytes, dest_addr: int = 0) -> bytes:
    """16-byte Firmware Information payload (Guide 2.4.1).

    Layout: FW_Size(BE,4) | FW_CRC32(BE,4) | DestAddr(BE,4) | Reserved(4)=0.
    FW_CRC32 is CRC-32 over ``struct.pack('<I', size) ++ firmware`` (size prefixed
    little-endian, per the Guide), stored big-endian in the field.
    """
    fw_crc = crc32(struct.pack("<I", len(firmware)) + firmware)
    return (
        struct.pack(">I", len(firmware))
        + struct.pack(">I", fw_crc)
        + struct.pack(">I", dest_addr)
        + struct.pack(">I", 0)
    )


def data_packet_payload(sequence: int, chunk: bytes) -> bytes:
    """Data packet payload (Guide 2.4.3): PacketSeq(BE,4) starting at 0 | firmware chunk."""
    return struct.pack(">I", sequence) + chunk


def sync_bytes(word: int) -> bytes:
    """Sync/response word as it appears on the wire (little-endian, Guide note)."""
    return struct.pack("<I", word)


@dataclass(frozen=True)
class Response:
    """Parsed module->host response frame."""

    class_id: int
    msg_id: int              # echoes the host command's MsgID
    status: Optional[int]    # 2-byte status, or None if payload too short
    payload: bytes           # full response payload (echo + status + any extra data)


def parse_response(frame: bytes) -> Optional[Response]:
    """Parse and validate a complete response frame. Returns None if malformed.

    Frame: 0xAA | Class | Msg | Len(BE) | Payload | CRC32(BE) | 0x55.
    The response payload is ClassID_echo | MsgID_echo | Status(BE,2) [ | extra ].
    """
    if len(frame) < 10 or frame[0] != HEADER or frame[-1] != TAIL:
        return None
    length = struct.unpack_from(">H", frame, 3)[0]
    if len(frame) != 5 + length + 5:
        return None
    payload = frame[5:5 + length]
    body = frame[1:5 + length]
    crc_rx = struct.unpack_from(">I", frame, 5 + length)[0]
    if crc_rx != crc32(body):
        return None
    status = struct.unpack_from(">H", payload, 2)[0] if length >= 4 else None
    echo_msg = payload[1] if length >= 2 else frame[2]
    return Response(class_id=frame[1], msg_id=echo_msg, status=status, payload=payload)


def status_text(status: Optional[int]) -> str:
    if status is None:
        return "?"
    return STATUS.get(status, f"0x{status:04x} (unknown)")


def find_serial_port() -> Optional[str]:
    """Best-effort auto-detect of the module's serial port."""
    candidates = (
        ["/dev/lg290p"]
        + sorted(glob.glob("/dev/ttyACM*"))
        + sorted(glob.glob("/dev/ttyUSB*"))
    )
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


# --------------------------------------------------------------------------- #
# Serial-facing flasher
# --------------------------------------------------------------------------- #
class FlashError(RuntimeError):
    """Raised when a flash step fails."""


class Flasher:
    """Drives the upgrade protocol over an open serial connection."""

    def __init__(self, serial_port: "serial.Serial", verbose: bool = True) -> None:
        self.ser = serial_port
        self.verbose = verbose

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def _read_exact(self, n: int, deadline: float) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n and time.time() < deadline:
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf += chunk
        return bytes(buf) if len(buf) == n else None

    def read_response(self, timeout: float) -> Optional[Response]:
        """Read one response frame from the wire (or None on timeout/garble)."""
        deadline = time.time() + timeout
        while time.time() < deadline:                       # find header
            if self.ser.read(1) == bytes([HEADER]):
                break
        else:
            return None
        head = self._read_exact(4, deadline)                # Class | Msg | Len(BE)
        if head is None:
            return None
        length = struct.unpack_from(">H", head, 2)[0]
        rest = self._read_exact(length + 5, deadline)       # Payload | CRC32 | Tail
        if rest is None:
            return None
        return parse_response(bytes([HEADER]) + head + rest)

    def command(self, msg_id: int, payload: bytes, timeout: float, name: str) -> Optional[int]:
        """Send a command frame and return the response status (or None)."""
        self.ser.reset_input_buffer()
        self.ser.write(build_frame(msg_id, payload))
        self.ser.flush()
        resp = self.read_response(timeout)
        if resp is None:
            self._log(f"  [{name}] no response (timeout)")
            return None
        self._log(f"  [{name}] status = {status_text(resp.status)}")
        return resp.status

    def synchronize(self, window: float) -> bool:
        """Spam SYNC_WORD1 until RSP_WORD1, then send SYNC_WORD2 -> RSP_WORD2.

        The bootloader only listens for ~500 ms after a reset, so call this while
        the module is (or is about to be) resetting. ``window`` bounds the attempt.
        """
        sync1, rsp1 = sync_bytes(SYNC_WORD1), sync_bytes(RSP_WORD1)
        sync2, rsp2 = sync_bytes(SYNC_WORD2), sync_bytes(RSP_WORD2)

        self._log(f"Synchronising (max {window:.0f} s)...")
        self.ser.reset_input_buffer()
        deadline = time.time() + window
        buf = bytearray()
        while time.time() < deadline:
            self.ser.write(sync1)
            self.ser.flush()
            time.sleep(0.02)
            buf += self.ser.read(64)
            if rsp1 in buf:
                break
        else:
            self._log("  x RSP_WORD1 not received -- bootloader not caught (wrong window/baud/port?)")
            return False

        self.ser.write(sync2)
        self.ser.flush()
        buf = bytearray()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            buf += self.ser.read(64)
            if rsp2 in buf:
                self._log("  + command mode")
                return True
        self._log("  x RSP_WORD2 not received")
        return False

    def flash(self, firmware: bytes) -> bool:
        """Run the full upgrade sequence. Returns True on success.

        Assumes the module is already in bootloader command mode (call
        synchronize() first).
        """
        # 1) Firmware Information
        if self.command(MSG_FW_INFO, firmware_info_payload(firmware), 2.0, "fw-info") != STATUS_OK:
            return False
        # 2) Erase
        self._log("Erasing flash (up to 15 s)...")
        if self.command(MSG_ERASE, b"", 15.0, "erase") != STATUS_OK:
            return False
        # 3) Data packets
        total = (len(firmware) + PACKET_SIZE - 1) // PACKET_SIZE
        self._log(f"Sending {len(firmware)} B in {total} packets of {PACKET_SIZE} B...")
        for seq in range(total):
            chunk = firmware[seq * PACKET_SIZE:(seq + 1) * PACKET_SIZE]
            timeout = 40.0 if seq == total - 1 else 2.0   # last packet: whole-image write
            name = f"data {seq + 1}/{total}"
            if self.command(MSG_DATA, data_packet_payload(seq, chunk), timeout, name) != STATUS_OK:
                self._log("  x transfer failed -- do NOT power off, just flash again")
                return False
        # 4) Reset into the new application
        self._log("Resetting module...")
        self.command(MSG_RESET, b"", 2.0, "reset")
        return True


def trigger_soft_reset(port: str, baud: int) -> None:
    """Send `$PQTMSRR` to the running firmware so the bootloader reboots and its
    ~500 ms sync window opens. Call synchronize() immediately afterwards."""
    print(f"Auto-reset: sending $PQTMSRR at {baud} baud...")
    try:
        with serial.Serial(port, baud, timeout=0.2) as ser:
            ser.write(nmea_command("PQTMSRR"))
            ser.flush()
            time.sleep(0.05)
    except serial.SerialException as exc:   # pragma: no cover - hardware dependent
        print(f"  (soft reset failed: {exc})")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_firmware_summary(path: Path, firmware: bytes) -> None:
    fw_crc = crc32(struct.pack("<I", len(firmware)) + firmware)
    print(f"Firmware: {path}")
    print(f"  size     : {len(firmware)} B ({len(firmware) / 1024:.1f} KiB)")
    print(f"  MD5      : {hashlib.md5(firmware).hexdigest()}")
    print(f"  FW_CRC32 : 0x{fw_crc:08x} (over [size LE] ++ firmware)")
    print(f"  packets  : {(len(firmware) + PACKET_SIZE - 1) // PACKET_SIZE} x {PACKET_SIZE} B")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Flash firmware (.pkg) onto a Quectel LG290P over UART, without QGNSS."
    )
    ap.add_argument("pkg", nargs="?", type=Path, help="path to the firmware .pkg file")
    ap.add_argument("--port", help="serial port (default: auto-detect ttyACM*/ttyUSB*)")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD,
                    help=f"bootloader baud rate (default {DEFAULT_BAUD})")
    ap.add_argument("--sync-window", type=float, default=30.0,
                    help="seconds to keep trying to sync (reset the module meanwhile)")
    ap.add_argument("--auto-reset", action="store_true",
                    help="send $PQTMSRR then sync -- hands-free, no manual power-cycle")
    ap.add_argument("--reset-baud", type=int, default=None,
                    help="baud for $PQTMSRR (default = --baud) if the app runs at another rate")
    ap.add_argument("--dry-run", action="store_true",
                    help="only parse the .pkg and compute CRC; never open the port")
    ap.add_argument("--query-only", action="store_true",
                    help="only sync + query bootloader version; erases/writes nothing")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    firmware: Optional[bytes] = None
    if args.pkg is not None:
        if not args.pkg.is_file():
            print(f"File not found: {args.pkg}", file=sys.stderr)
            return 2
        firmware = args.pkg.read_bytes()
        _print_firmware_summary(args.pkg, firmware)

    if args.dry_run:
        if firmware is None:
            print("--dry-run needs a .pkg file", file=sys.stderr)
            return 2
        print("\n[dry-run] no bytes were sent to any port.")
        return 0

    if not args.query_only and firmware is None:
        print("Provide a .pkg file, or use --query-only.", file=sys.stderr)
        return 2

    port = args.port or find_serial_port()
    if port is None:
        print("No serial port found. Pass --port /dev/ttyACM0.", file=sys.stderr)
        return 2
    print(f"Port: {port} @ {args.baud} 8N1")

    if not args.yes and not args.query_only:
        print("\n!!  This overwrites the firmware. The module has no backup -- an "
              "interrupted flash needs a re-flash.")
        if input("Continue? [yes/NO] ").strip().lower() not in ("yes", "y"):
            print("Aborted.")
            return 1

    if args.auto_reset:
        trigger_soft_reset(port, args.reset_baud or args.baud)
    else:
        print("Reset the module during the sync window (unplug/replug USB or the "
              "RESET pin) -- or use --auto-reset.")

    with serial.Serial(port, args.baud, timeout=0.05) as ser:
        flasher = Flasher(ser)
        if not flasher.synchronize(args.sync_window):
            return 1
        if args.query_only:
            flasher.command(MSG_BOOTVER, b"", 1.0, "bootloader-version")
            return 0
        assert firmware is not None
        ok = flasher.flash(firmware)

    if ok:
        print("\n+ DONE. Verify with your NMEA tool: query $PQTMVERNO at 460800 baud.")
        return 0
    print("\nx Flash INCOMPLETE -- try again (reset + re-run).")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
