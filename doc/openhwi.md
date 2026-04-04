# OpenHWI Notes

This file documents custom protocol conventions used by the OpenHWI
reverse-engineering setup (not part of the native HomeWorks protocol).

## USB host escape protocol

On the UART -> USB stream, a host-side escape mechanism is used:

- Escape byte: `0xFF`
- ACK marker: `0xFE` (encoded as `0xFF 0xFE`)
- GAP marker: `0xFD` (encoded as `0xFF 0xFD`)
- Escaped data byte: literal `0xFF` is encoded as `0xFF 0xFF`

The device sends `0xFF 0xFE` to acknowledge that a full packet was received
from USB and transmitted on RS485.

The device sends `0xFF 0xFD` when it observes a gap of more than two UART byte
times between consecutive UART bytes.
