> ⚠️ **This is vibecoded. I take no responsibility for anything it does** — including bricking your module. Use at your own risk.

# lg290p-firmware-flasher

Flash firmware onto a **Quectel LG290P(03)** GNSS module over plain **UART** — from Linux, macOS, or anywhere Python + a serial port runs. **No Windows and no QGNSS required.**

QGNSS (the vendor's Windows GUI) is just a front-end for a small, documented UART protocol. This tool re-implements that protocol directly. On top of that it fixes a real-world blocker: on boards like the **Waveshare LG290P** (CH34x USB-UART bridge) QGNSS can't flash at all, because it resets the module via RTS/DTR and those lines aren't wired to the reset pin. This tool triggers the reset **in software** (`$PQTMSRR`), so it works where QGNSS doesn't.

## Status

✅ **Verified end-to-end on real hardware** (2026-07-27): flashed a Waveshare LG290P from `LG290P03AANR01A06S` → `LG290P03AANR02A02S`. The module rebooted into the new firmware and streamed NMEA normally. QGNSS could not flash the same board.

## ⚠️ Safety

The module has **no backup / dual-bank firmware**. An interrupted flash leaves it without a runnable application until a new flash finishes. The **bootloader survives** (it re-opens its sync window on every reset), so a failed flash is recoverable by simply flashing again — but **do not cut power mid-flash**.

## Requirements

- Python 3.9+
- [`pyserial`](https://pypi.org/project/pyserial/)

```bash
pip install pyserial
```

## Usage

```bash
# 1) Dry run — parse the .pkg, print size/MD5/CRC/packet count. Never opens the port.
python lg290p_flash.py firmware.pkg --dry-run

# 2) Safe handshake test — sync + query bootloader version. Erases/writes NOTHING.
python lg290p_flash.py --query-only --auto-reset --port /dev/ttyACM0 --baud 460800

# 3) Real flash (hands-free: software reset + sync + erase + data + reset)
python lg290p_flash.py firmware.pkg --auto-reset --port /dev/ttyACM0 --baud 460800
```

Key options:

| Option | Meaning |
| --- | --- |
| `--auto-reset` | Send `$PQTMSRR` then sync — no manual USB power-cycle, no 500 ms timing to hit by hand. |
| `--reset-baud N` | Baud for the `$PQTMSRR` reset if the running app uses a different rate than the bootloader. |
| `--query-only` | Enter the bootloader and read its version; make no changes. **Recommended first step.** |
| `--dry-run` | Parse the firmware only; never touch the serial port. |
| `--port` / `--baud` | Serial port (auto-detected if omitted) and baud (default `460800`). |
| `--yes` | Skip the interactive confirmation. |

If you don't use `--auto-reset`, the tool asks you to power-cycle the module during the sync window instead.

**Recommended flow:** run `--query-only` first. If it reaches `+ command mode` and returns a bootloader version, the whole protocol path is validated against your hardware and a real flash is low-risk.

## How it works

The bootloader speaks a tiny framed protocol at **460800 8N1**:

```
0xAA | ClassID | MsgID | Len(BE,2) | Payload | CRC32(BE,4) | 0x55
```

Flash sequence: **soft-reset → sync → Firmware Information → Erase → 4 KiB data packets → Reset**. CRC32 is standard IEEE (`zlib.crc32`).

The full wire specification — sync words, message IDs, status codes, payload layouts — is in [**PROTOCOL.md**](PROTOCOL.md). Guidance for AI agents working on this repo is in [**AGENTS.md**](AGENTS.md).

## Tests

Pure protocol logic (framing, CRC, payload construction, response parsing) is covered by stdlib `unittest` — no hardware, no third-party test deps. Several tests assert the **exact bytes** that a real module accepted, so a wire-format regression fails loudly.

```bash
python -m unittest discover -s tests -v
```

## Hardware tested

| Board | Bridge | From → To | Result |
| --- | --- | --- | --- |
| Waveshare LG290P GNSS RTK Module | CH34x (1a86:55d3) | R01A06 → R02A02 | ✅ success |

Reports for other boards/firmware welcome via [issues](../../issues).

## License

[MIT](LICENSE) © Matěj Hájek
