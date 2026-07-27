# LG290P bootloader upgrade protocol

Complete wire specification for the Quectel LG290P(03) UART firmware-upgrade
protocol, as implemented by `lg290p_flash.py`. Source: _Quectel LG290P(03) &
LGx80P(03) Firmware Upgrade Guide V1.1_, cross-checked against real traffic.

All multi-byte integers are **big-endian** unless stated otherwise. The four
sync/response words are the exception: they go **little-endian** on the wire.

## Physical layer

| Parameter    | Value                              |
| ------------ | ---------------------------------- |
| Baud         | 460800                             |
| Frame format | 8 data bits, 1 stop bit, no parity |
| Flow control | none                               |

The bootloader only listens for the sync sequence for **~500 ms after a reset**.
If it doesn't sync in that window it boots the existing application instead.

## Entering the bootloader

Two ways to get the ~500 ms window to open:

1. **Hardware:** power-cycle the module (or pulse its RESET pin) while the host
   is already sending `SYNC_WORD1`.
2. **Software (this tool's `--auto-reset`):** send the running application the
   NMEA command `$PQTMSRR*<xor>\r\n`; it reboots and the bootloader comes up.

> On the Waveshare LG290P board, QGNSS's RTS/DTR-based reset does **not** work
> (those lines aren't wired to reset), which is why the software reset matters.

## Sync sequence

Words (little-endian on the wire):

| Name         | Value        | Wire bytes    |
| ------------ | ------------ | ------------- |
| `SYNC_WORD1` | `0x514C1309` | `09 13 4C 51` |
| `RSP_WORD1`  | `0xAAFC3A4D` | `4D 3A FC AA` |
| `SYNC_WORD2` | `0x1203A504` | `04 A5 03 12` |
| `RSP_WORD2`  | `0x55FD5BA0` | `A0 5B FD 55` |

1. Host repeatedly sends `SYNC_WORD1` (~20–95 ms apart) until the module replies
   `RSP_WORD1`.
2. Host sends `SYNC_WORD2` once; module replies `RSP_WORD2` and is now in
   **command mode**.

## Command frame

```
+--------+---------+-------+-------------+-----------------+-------------+--------+
| 0xAA   | ClassID | MsgID | Len (BE,2)  | Payload (<=5KB) | CRC32(BE,4) | 0x55   |
+--------+---------+-------+-------------+-----------------+-------------+--------+
         |<---------------- CRC32 is computed over this range --------->|
```

- `HEADER` = `0xAA`, `TAIL` = `0x55` (fixed).
- `Len` counts payload bytes only.
- `CRC32` = standard IEEE CRC-32 (poly `0xEDB88320`, i.e. `zlib.crc32`), over
  `ClassID | MsgID | Len | Payload`.

## Messages (ClassID = `0x02`)

`ClassID 0x02` is the **only** class the bootloader handles (verified by
scanning all 256 class IDs — every other value returns _unsupported_).

| MsgID  | Direction     | Meaning                  |
| ------ | ------------- | ------------------------ |
| `0x02` | host → module | Firmware Information     |
| `0x03` | host → module | Erase firmware           |
| `0x04` | host → module | Firmware data packet     |
| `0x31` | host → module | Reset module             |
| `0x71` | host → module | Query bootloader version |
| `0x00` | module → host | Response                 |

A scan of all 256 MsgIDs found **no** hidden/debug/memory-dump commands — the set
above is complete.

### Response (`MsgID 0x00`)

Payload: `ClassID_echo(1) | MsgID_echo(1) | Status(BE,2) [ | extra ]`.
The echoed ClassID/MsgID mirror the command being answered.

Status codes:

| Status   | Meaning             |
| -------- | ------------------- |
| `0x0000` | OK                  |
| `0x0001` | unknown error       |
| `0x0002` | CRC32 error         |
| `0x0003` | timeout             |
| `0x0004` | unsupported message |
| `0x0005` | package error       |
| `0x0020` | flash erase error   |
| `0x0021` | flash write error   |

`0x71` additionally returns 3 version bytes after the status (observed
`01 00 06` = bootloader v1.0.6).

### Firmware Information (`MsgID 0x02`) — 16-byte payload

| Field    | Bytes  | Notes                                                                                                  |
| -------- | ------ | ------------------------------------------------------------------------------------------------------ |
| FW_Size  | 4 (BE) | size of the whole `.pkg` in bytes                                                                      |
| FW_CRC32 | 4 (BE) | `crc32( struct.pack('<I', size) ++ firmware )` — size is prefixed **little-endian** into the CRC input |
| DestAddr | 4 (BE) | fixed `0x00000000`                                                                                     |
| Reserved | 4      | `0x00000000`                                                                                           |

### Data packet (`MsgID 0x04`)

Payload: `PacketSeq(BE,4) | chunk`. `PacketSeq` starts at `0` and increments.
Each chunk is `PACKET_SIZE` (4096 B); the last chunk is the remainder (≤ 4096 B).

### Erase (`0x03`) / Reset (`0x31`) / Query version (`0x71`)

Empty payload. Erase may take up to ~15 s before it responds.

## Full flash sequence

```
soft-reset ($PQTMSRR) ─┐
                       ├─ sync (SYNC1→RSP1, SYNC2→RSP2)  → command mode
Firmware Information ──── expect status OK
Erase ──────────────────  expect status OK   (up to ~15 s)
Data packet 0..N-1 ─────  expect status OK per packet
  (last packet)            allow up to ~40 s for the whole-image write
Reset ──────────────────  module reboots into the new application
```

## Reference frames (verified against real hardware)

| What                                                                                    | Bytes (hex)                                                     |
| --------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| Erase command                                                                           | `AA 02 03 0000 890BA9CE 55`                                     |
| Firmware Information for `LG290P03AANR02A02S.pkg` (size `0x00292B20`, CRC `0x42194628`) | `AA 02 02 0010 00292B20 42194628 00000000 00000000 CA05D7C1 55` |
| Bootloader-version response payload                                                     | `02 71 0000 010006`                                             |

## Notes on security (from reverse engineering)

The `.pkg` contains AES-encrypted segments plus a SHA-1 tag; the decryption key
lives in the SoC (not exposed over UART). The bootloader offers no memory-read
primitive, so keys/encrypted images are **not** extractable through this
protocol — it only accepts a signed/encrypted image and verifies it.
