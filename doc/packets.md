# Packet Notes

This file summarizes what is known about the Lutron HomeWorks Illumination
protocol as observed on the RS485 keypad bus, and highlights areas that remain
ambiguous.

## System context

The HomeWorks Illumination system includes a central Processor with multiple
serial interfaces. One RS485 interface connects to all keypads in the house.
Keypads have buttons with status LEDs, and the Processor provides continuous
LED state updates over this bus.

## General packet format

- Start byte: `0xB0`
- Type byte: 1 byte (defines the message type)
- Length byte: 1 byte
- Payload: variable length
- Checksum: 1 byte
- End byte: `0x00`

Packet layout (length includes all bytes):

| Start | Type | Length | Payload (1..N) | Checksum | End  |
| ----- | ---- | ------ | -------------- | -------- | ---- |
| 0xB0  | TYPE | LENGTH | PAYLOAD        | CHECKSUM | 0x00 |

Length semantics:
- Length includes **all** bytes in the packet, from the start byte (`0xB0`)
  through the trailing zero (`0x00`).
- Payload is always **at least 1 byte**.
- There is **always one** trailing `0x00` byte.

Checksum:
- 8-bit checksum is the two's complement of the sum of all bytes (unsigned).
- Includes the start byte `0xB0`.
- Equivalent formula: `checksum = (-sum(all_bytes_except_checksum)) & 0xFF`
  so that `(sum(all_bytes_including_checksum) & 0xFF) == 0`.

## Lighting control packets (LED status updates)

Packet layout (partial, known fields only):

| Start | Type | Length | Subtype  | LED payload (4 devices) | Checksum | End  |
| ----- | ---- | ------ | -------- | ----------------------- | -------- | ---- |
| 0xB0  | 0x80 | 0x1E   | 0xC0..C7 | 4 x 6 bytes             | CHECKSUM | 0x00 |

In the idle state, the Processor continuously sends packets of type `0x80`
to update keypad LED states. Each `0x80` packet services four keypads. To
cover up to 32 keypads, a subtype byte (`0xC0`..`0xC7`) selects which group
of four keypads the packet targets.

Observations:
- Each packet appears to cover **four devices**.
- Devices are numbered in Lutron starting at 1, but indexed in packet starting
  at 0.
- Each device uses **6 bytes** (48 bits).
- Within those 6 bytes, two consecutive bits per LED represent its state:
  - `00` = off
  - `01` = on
  - `10` = flash 1 (tentative)
  - `11` = flash 2 (tentative)

## Button press packets

Packet layout (partial, known fields only):

| Start | Type                  | Length | Keypad | Button | Unknown | Checksum | End  |
| ----- | --------------------- | ------ | ------ | ------ | ------- | -------- | ---- |
| 0xB0  | 0x01/0x02/0x03/0x04   | 0x09   | KEYPAD | BUTTON | 2 bytes | CHECKSUM | 0x00 |

Example entries (button presses of keypad 21):

```
b0 01 09 14 05 70 2d 90 00
b0 02 09 14 05 02 5a d0 00
b0 01 09 14 05 9a 86 0d 00
b0 02 09 14 05 9a 77 1b 00
b0 01 09 14 06 21 4d be 00
b0 02 09 14 06 e7 34 10 00
```

Interpretation:
- Type mapping:
  - `0x01` = press
  - `0x02` = release
  - `0x03` = double-tap
  - `0x04` = hold / long press
- Length is `0x09` in these samples.
- Keypad number is `0x14` (20 decimal, indexed with +1).
- Button number follows the keypad number.
- The two bytes before checksum vary and are not yet identified.

Unresolved:
- The two bytes preceding checksum in these examples are not yet decoded.

## Flash response packets (keypad disabled)

Packet layout (partial, known fields only):

| Start | Type | Length | Keypad | Unknown | Checksum | End  |
| ----- | ---- | ------ | ------ | ------- | -------- | ---- |
| 0xB0  | 0x89 | 0x0E   | KEYPAD | 8 bytes | CHECKSUM | 0x00 |

Example (type `0x89`, length `0x0e`):

```
b0 89 0e 00 2c 20 46 38 0f 46 1c 0a 74 00
```

Interpretation:
- Type `0x89` indicates a "flash response" when a keypad is disabled.
- The byte after length appears to be the keypad number.
- Some byte ranges appear constant across samples; other bytes vary.

Unresolved:
- Several bytes are labeled "unknown" or "garbage?" in some samples; these
  may be artifacts or leftover values from other packet types at the same
  byte offsets, rather than meaningful fields.
- The meaning of the variable fields is not confirmed.

## Keypad configuration packets

Keypads request configuration during system startup or when they need to
rejoin the bus (e.g., after reconnecting or after an address change via DIP
switches). Two request types are observed:

- Request type `0x06` -> configuration response packet type `0x8E`.
- Request type `0x0A` -> configuration response packet type `0x9A`.

Request packet layout (types `0x06`/`0x0A`):

| Start | Type       | Length | Keypad | Checksum | End  |
| ----- | ---------- | ------ | ------ | -------- | ---- |
| 0xB0  | 0x06/0x0A  | 0x06   | KEYPAD | CHECKSUM | 0x00 |

Configuration response packet layout (types `0x8E`/`0x9A`):

| Start | Type       | Length | Keypad | Fields  | Checksum | End  |
| ----- | ---------- | ------ | ------ | ------- | -------- | ---- |
| 0xB0  | 0x8E/0x9A  | 0x0E   | KEYPAD | 8 bytes | CHECKSUM | 0x00 |

Examples show types `0x8e` and `0x9a` with length `0x0e` (14):

```
b0 8e 0e 00 07 1e 46 38 0f 46 01 f0 cb 00
b0 8e 0e 15 0c 1e 46 38 0f 46 1c 0a 7c 00
b0 9a 0e 15 0a 0a 00 0a 0a 00 1c 12 3d 00
```

Request/response examples:

- Request `0x06` (keypad 0):
  - Request: `b0 06 06 00 44 00`
  - Response (example): `b0 8e 0e 00 07 1e 46 38 0f 46 01 f0 cb 00`

- Request `0x0A` (keypad 21):
  - Request: `b0 0a 06 15 2b 00`
  - Response (example): `b0 9a 0e 15 0a 0a 00 0a 0a 00 1c 12 3d 00`

These examples show the keypad number is consistent between the request and
the configuration response.

### Known field order for `0x8E` (configuration response):

| Byte | Field                                |
| ---- | ------------------------------------ |
| 0    | 0xB0 (start)                         |
| 1    | 0x8E (type)                          |
| 2    | 0x0E (length)                        |
| 3    | Keypad number                        |
| 4    | LED off brightness                   |
| 5    | Double-tap time                      |
| 6    | Hold time                            |
| 7    | Local ACK time (tentative)           |
| 8    | Flash 1 rate                         |
| 9    | Flash 2 rate                         |
| 10   | Background brightness                |
| 11   | Special byte (keypad-type dependent) |
| 12   | Checksum                             |
| 13   | 0x00 (end)                           |

#### International (square) keypad configuration values:

LED off brightness:
- `0x0c` = Nightlight
- `0x00` = Off

Double-tap time:
- `0x00` = Disabled
- `0x1e` = Fast (1/2 s)
- `0x32` = Medium (3/4 s)
- `0x46` = Slow (1 s)

Hold time:
- `0x32` = Short (0.35 s)
- `0x46` = Medium (0.5 s)
- `0x6e` = Long (0.75 s)
- `0xc8` = Very Long (1.53 s)

LED flash 1:
- `0x0f` = Extra fast
- `0x19` = Fast
- `0x28` = Medium

LED flash 2:
- `0x46` = Medium Slow
- `0x64` = Slow
- `0x8c` = Extra Slow

Background brightness:
- `0x00` = Off
- `0x06` = Low
- `0x1c` = Medium
- `0x1c` = High (observed same as Medium)

Special byte:
- Seems to be constant `0x12`.

#### US (single column) keypad configuration values:

LED off brightness:
- `0x07` = Nightlight
- `0x00` = Off

Background brightness:
- `0x03` = Off
- `0x02` = Low
- `0x01` = Medium
- `0x00` = High

Special byte:
- Seems to be LED mask:
- 1-bits indicate *no* LED present.
- `0xF0` = ST-4S-NI / ST-4SIR-NI
- `0xC0` = ST-6BRL-NI
- `0x00` = ST-7B-NI

### Known field order for `0x9A` (configuration response):
Only exists for international keypads.

| Byte | Field                     |
| ---- | ------------------------- |
| 0    | 0xB0 (start)              |
| 1    | 0x9A (type)               |
| 2    | 0x0E (length)             |
| 3    | Keypad number             |
| 4    | Background light (right)  |
| 5    | Background light (left)   |
| 6    | Background light (unused) |
| 7    | Active buttons (right)    |
| 8    | Active buttons (left)     |
| 9    | Active buttons (unused)   |
| 10   | Background brightness     |
| 11   | 0x12 (observed constant)  |
| 12   | Checksum                  |
| 13   | 0x00 (end)                |

### User preference defaults in the Lutron Software:
- LED flash 1: Extra fast {Extra Fast, Fast, Medium}
- LED flash 2: Medium Slow {Extra Slow, Medium Slow, Slow}
- LED 'off' brightness: Nightlight {Off, Nightlight}
- Background brightness: Medium {Off, Low, Medium, High}
- Hold time: Medium (0.5 s) {Short (0.35s), Medium (0.5s), Long (0.75s), Very Long (1.53s)}
- Double-tap time: Fast (1/2 s) {Disabled, Fast (1/2s), Medium (3/4s), Slow (1s)}
- LED state toggle time (local ACK time): 0.75 s

Unresolved:
- The exact byte offsets for each field are not fully confirmed.
- The meaning of some values (e.g., `0x12` for international keypads) is tentative.

## CCO (Contact Closure Output) boards

Lutron CCO boards provide 8 relays for remote contact closure. They share the
RS485 bus with keypads. Relay states are reported through the LED update
mechanism, and a dedicated pulse packet (`0x8A`) triggers timed pulses.

### Pulse packet (`0x8A`)

Packet layout (partial, known fields only):

| Start | Type | Length | Device | Relay | Pulse time | Zeros   | Checksum | End  |
| ----- | ---- | ------ | ------ | ----- | ---------- | ------- | -------- | ---- |
| 0xB0  | 0x8A | 0x0E   | DEVICE | RELAY | PULSE      | 6 bytes | CHECKSUM | 0x00 |

Example:

```
b0 8a 0e 00 00 0b 00 00 00 00 00 00 ad 00
```

Pulse time values (examples):
- `0x0b` = 0.5 s
- `0x0c` = 1.0 s
- `0x0d` = 1.5 s
- `0x0e` = 2.0 s
- `0x0f` = 2.5 s
- `0x10` = 3.0 s
- `0x11` = 3.5 s
- `0x12` = 4.0 s
- `0x13` = 4.5 s
- `0x14` = 5.0 s

Unresolved:
- Confirm whether other bytes in the payload are reserved or fixed.

### LED payload mapping for CCO relay states

CCO relay states are encoded inside the LED payload bytes used by `0x80` LED
status packets. The mapping differs from keypads:

- First two bytes are always `0x00`.
- Last two bytes encode the relay state using 8 double-bits (2 bits per relay):
  - `01` = relay open
  - `10` = relay close
- The two middle bytes also encode relay states, but shifted by one double-bit:
  - Position 0 is always `00`.
  - Position 1 encodes relay 1 (index 0).
  - Position 2 encodes relay 2 (index 1).
  - ...
  - Position 7 encodes relay 7 (index 6).
  - Relay 8 is not present in this middle section.

Examples:

```
00 00 54 55 55 55  (all open)
00 00 54 59 55 56  (close 5)
00 00 54 65 55 59  (close 6)
00 00 54 95 55 65  (close 7)
00 00 54 55 55 95  (close 8)
```

Telnet/KLS comparison notes:
- "KLS" (KLMON) reporting corresponds to the two middle bytes.
- The mask starts at the 9th position.
- Relay 8 appears in logical order after relay 7, in the first double-bit of
  the last two-byte section.
- The last two bytes (full bus payload) are zero in KLS output.

```
LNET> CCOCLOSE,[01:05:01],6
KLS, [01:05:01], 000000000111112110000000

LNET> CCOOPEN,[01:05:01],6
KLS, [01:05:01], 000000000111111110000000

LNET> CCOCLOSE,[01:05:01],5
KLS, [01:05:01], 000000000111121110000000

LNET> CCOOPEN,[01:05:01],5
KLS, [01:05:01], 000000000111111110000000
```

## CCI (Contact Closure Input) boards

CCI boards provide dry-contact inputs and act like an 8-button keypad.
Closing an input corresponds to a button press. They receive `0x8E`
configuration packets like US keypads, but the LED mask is constant `0x00`
since LEDs are not present.

Observed quirk:
- Hold events (`0x04`) appear to report the wrong input index: holding input 0
  reports button 5, holding input 1 reports 6, and so on.
