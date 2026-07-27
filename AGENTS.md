# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

A single-purpose CLI (`lg290p_flash.py`) that flashes firmware onto a Quectel
LG290P(03) GNSS module over UART, reimplementing the Quectel bootloader upgrade
protocol. It exists mainly because the vendor GUI (QGNSS) can't flash certain
boards (RTS/DTR reset not wired); this tool resets in software instead.

## Repository map

| Path | Purpose |
| --- | --- |
| `lg290p_flash.py` | The whole tool: pure protocol helpers + a `Flasher` class + a CLI (`main`). |
| `tests/test_protocol.py` | `unittest` tests for the pure logic; no hardware needed. |
| `PROTOCOL.md` | Authoritative wire specification. **Read this before changing framing/CRC/sequence.** |
| `README.md` | User-facing docs. |

## Design rules

- **The wire protocol is verified against real hardware.** Do not change frame
  layout, CRC computation, endianness, sync words, message IDs, or the flash
  sequence unless `PROTOCOL.md` and a hardware retest back the change. Several
  tests hard-code the exact bytes a real module accepted — if you change the
  wire format you must update both the code and those tests deliberately.
- **Keep I/O and logic separate.** Pure, testable functions (`crc32`,
  `build_frame`, `nmea_command`, `firmware_info_payload`, `data_packet_payload`,
  `parse_response`, `sync_bytes`) do no I/O. Serial work lives in `Flasher` and
  `trigger_soft_reset`. New logic should be added as pure functions with tests.
- **Style:** Python 3.9+, `from __future__ import annotations`, full type
  annotations, `pathlib.Path` for filesystem paths. Standard library only for
  runtime except `pyserial`; tests use stdlib `unittest` (no pytest).
- **Safety first.** The module has no dual-bank backup. Never remove the
  confirmation prompt, the `--dry-run` path, or the `--query-only` (non-
  destructive) path. New destructive behavior must be opt-in and clearly warned.

## How to validate changes

```bash
python -m unittest discover -s tests -v      # must pass
python lg290p_flash.py <some.pkg> --dry-run  # must print size/CRC/packet count
```

Hardware-in-the-loop changes should first be exercised with `--query-only`
(sync + version read, writes nothing) before any real flash.

## Known limitations / good first tasks

- Only tested on one board/firmware transition (see README "Hardware tested").
- No optional hardware DTR/RTS reset path (only software `$PQTMSRR`).
- No resume/retry of individual data packets on transient error.
- No progress bar for the 600+ packet transfer.
