"""Unit tests for the LG290P upgrade-protocol logic (no hardware needed).

Several assertions lock in the *exact* bytes that were verified against a real
module (build 2026-07-27, R01A06 -> R02A02), so a regression that changes the
wire format will fail here.
"""
from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lg290p_flash as fw  # noqa: E402


class TestCrc32(unittest.TestCase):
    def test_check_value(self) -> None:
        # Canonical CRC-32/IEEE check value for the ASCII string "123456789".
        self.assertEqual(fw.crc32(b"123456789"), 0xCBF43926)

    def test_empty(self) -> None:
        self.assertEqual(fw.crc32(b""), 0x00000000)


class TestSyncWords(unittest.TestCase):
    def test_wire_byte_order(self) -> None:
        # Guide: SYNC/RSP words are transmitted little-endian. Verified on the wire.
        self.assertEqual(fw.sync_bytes(fw.SYNC_WORD1), bytes.fromhex("09134c51"))
        self.assertEqual(fw.sync_bytes(fw.RSP_WORD1), bytes.fromhex("4d3afcaa"))
        self.assertEqual(fw.sync_bytes(fw.SYNC_WORD2), bytes.fromhex("04a50312"))
        self.assertEqual(fw.sync_bytes(fw.RSP_WORD2), bytes.fromhex("a05bfd55"))


class TestFrame(unittest.TestCase):
    def test_erase_frame_matches_hardware(self) -> None:
        # Exact bytes sent to (and accepted by) the real module for the Erase command.
        self.assertEqual(fw.build_frame(fw.MSG_ERASE, b"").hex(), "aa02030000890ba9ce55")

    def test_frame_envelope(self) -> None:
        frame = fw.build_frame(fw.MSG_RESET, b"")
        self.assertEqual(frame[0], fw.HEADER)
        self.assertEqual(frame[-1], fw.TAIL)
        self.assertEqual(frame[1], fw.CLASS_UPGRADE)
        self.assertEqual(frame[2], fw.MSG_RESET)
        body = frame[1:-5]
        self.assertEqual(struct.unpack(">I", frame[-5:-1])[0], fw.crc32(body))


class TestFirmwareInfo(unittest.TestCase):
    def test_payload_structure(self) -> None:
        blob = b"\xA5" * 100
        payload = fw.firmware_info_payload(blob)
        self.assertEqual(len(payload), 16)
        size, fw_crc, dest, reserved = struct.unpack(">IIII", payload)
        self.assertEqual(size, 100)
        self.assertEqual(dest, 0)
        self.assertEqual(reserved, 0)
        # CRC is over little-endian size prefix ++ the firmware bytes.
        self.assertEqual(fw_crc, fw.crc32(struct.pack("<I", 100) + blob))

    def test_r02a02_frame_matches_hardware(self) -> None:
        # The real LG290P03AANR02A02S.pkg is 2 698 016 B with FW_CRC32 0x42194628.
        # This is the exact Firmware-Information frame the module accepted (status OK).
        payload = struct.pack(">IIII", 2_698_016, 0x42194628, 0, 0)
        frame = fw.build_frame(fw.MSG_FW_INFO, payload)
        self.assertEqual(
            frame.hex(),
            "aa0202001000292b20421946280000000000000000ca05d7c155",
        )


class TestDataPacket(unittest.TestCase):
    def test_sequence_prefix(self) -> None:
        self.assertEqual(fw.data_packet_payload(0, b"\xAA\xBB"), b"\x00\x00\x00\x00\xAA\xBB")
        self.assertEqual(fw.data_packet_payload(1, b"\xCC"), b"\x00\x00\x00\x01\xCC")
        self.assertEqual(fw.data_packet_payload(0x0102_0304, b""), b"\x01\x02\x03\x04")


class TestNmea(unittest.TestCase):
    def test_documented_checksum(self) -> None:
        # $PQTMVERNO*58 -- checksum from the Quectel protocol spec (ground truth).
        self.assertEqual(fw.nmea_command("PQTMVERNO"), b"$PQTMVERNO*58\r\n")

    def test_soft_reset_command(self) -> None:
        cmd = fw.nmea_command("PQTMSRR")
        self.assertTrue(cmd.startswith(b"$PQTMSRR*"))
        self.assertTrue(cmd.endswith(b"\r\n"))


class TestParseResponse(unittest.TestCase):
    def test_bootloader_version_response(self) -> None:
        # Real 0x71 response payload captured from the module: echo 02 71,
        # status 0x0000, bootloader version bytes 01 00 06 (v1.0.6).
        payload = bytes.fromhex("02710000010006")
        frame = fw.build_frame(fw.MSG_RESPONSE, payload)   # module frame, same envelope
        resp = fw.parse_response(frame)
        assert resp is not None
        self.assertEqual(resp.status, fw.STATUS_OK)
        self.assertEqual(resp.msg_id, 0x71)          # echoed command MsgID
        self.assertEqual(resp.payload[4:7], bytes.fromhex("010006"))

    def test_rejects_bad_crc(self) -> None:
        frame = bytearray(fw.build_frame(fw.MSG_RESPONSE, bytes.fromhex("02310000")))
        frame[-2] ^= 0xFF                              # corrupt one CRC byte
        self.assertIsNone(fw.parse_response(bytes(frame)))

    def test_rejects_bad_tail(self) -> None:
        frame = bytearray(fw.build_frame(fw.MSG_RESPONSE, bytes.fromhex("02310000")))
        frame[-1] = 0x00
        self.assertIsNone(fw.parse_response(bytes(frame)))

    def test_rejects_truncated(self) -> None:
        self.assertIsNone(fw.parse_response(b"\xAA\x02"))


class TestStatusText(unittest.TestCase):
    def test_known_and_unknown(self) -> None:
        self.assertEqual(fw.status_text(0x0000), "OK")
        self.assertEqual(fw.status_text(0x0004), "unsupported message")
        self.assertIn("0x00ff", fw.status_text(0x00FF))
        self.assertEqual(fw.status_text(None), "?")


if __name__ == "__main__":
    unittest.main(verbosity=2)
