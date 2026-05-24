# firmware

ESP32-S3 firmware for the M5Stack AtomS3 Lite. Composite USB HID device with two user-side peer links: BLE GATT and Wi-Fi AP.

Two USB HID interfaces are exposed simultaneously:

1. **Keyboard** (report ID 1) — types ASCII frames into a focused textarea on the kiosk page. The original "type the data as keystrokes" path. Works on any kiosk that allows USB keyboards (essentially all of them). Slow but maximally compatible.
2. **Vendor HID** (report ID 6, usage page 0xFF00) — bidirectional 64-byte binary reports. Used by the kiosk page's WebHID code path when available. On current builds this delivers practical rates in the tens of KiB/s and is limited mostly by full-speed HID cadence, not the raw USB PHY. The OS sees this as a generic HID device, not a keyboard — it passes the same USB-class filter that allows the keyboard interface, but no keystroke events leak out.

The dongle supports two user-side peer links:

1. **BLE GATT** — the current desktop-client path.
2. **Wi-Fi AP** — a dongle-hosted access point with a direct TCP socket for the fast path.

## Build & flash

```bash
pio run               # build only
pio run -t upload     # build and flash
```

To enter ROM bootloader before flashing: hold the side button, tap the recessed RST button on the bottom, then release both. The device appears as a serial port and PlatformIO uploads automatically.

## Pinout

| GPIO | Function |
|------|----------|
| 35 | SK6812 RGB LED |
| 41 | Button (active-low, internal pull-up) |
| 5 | Serial1 TX — debug UART (pad G5) |
| 6 | Serial1 RX — debug UART (pad G6) |
| USB-C | Native ESP32-S3 USB OTG — composite HID (keyboard + vendor) |

Connect a 3.3 V UART adapter to G5/G6 at 115200 baud to see debug output.

## LED states

| Colour | State |
|--------|-------|
| Yellow pulse | Scanning (no BLE connection) |
| Blue | Connected, idle |
| Green | Typing (standard path) |

## Button

Long-press (3 s): clears BLE bonds and reboots. Bonding is not currently used — this is a no-op in practice, reserved for a future bonding implementation.

## GATT service

UUIDs must match `client/tx/ble.py` exactly.

| | UUID | Properties | Use |
|-|------|------------|-----|
| Service | `4b696f73-6b55-0001-0000-000000000000` | | — |
| TX char | `4b696f73-6b55-0002-0000-000000000000` | WRITE_NR | Standard: ASCII bytes typed as keystrokes |
| CFG char | `4b696f73-6b55-0003-0000-000000000000` | WRITE | 2-byte big-endian inter-keystroke delay (clamped to [1, 100] ms) |
| WHID-RX | `4b696f73-6b55-0004-0000-000000000000` | NOTIFY | Fast: vendor-HID Output Report payloads forwarded to laptop |
| WHID-TX | `4b696f73-6b55-0005-0000-000000000000` | WRITE_NR | Fast: vendor-HID Input Reports (1 length byte + ≤62 payload), chunked by the client to fit the negotiated BLE MTU |

## Vendor HID descriptor

Usage page `0xFF00`, single application collection, report ID 6 (`HID_REPORT_ID_VENDOR`). Descriptor-level report body size is 63 bytes so the on-wire HID packet stays within the framework's 64-byte endpoint buffer once the report ID byte is added. `shared/framing.py` therefore uses `HID_REPORT_SIZE = 63` with a 1-byte payload length prefix and up to 62 payload bytes per report.

## ESP32-C6 note

The dongle is targeted at ESP32-S3. The C6 also has a native USB OTG peripheral and TinyUSB support, but is not the default board and the included `platformio.ini` builds for S3 specifically. To try on a C6 you would change `board = m5stack-atoms3` to a C6 board and re-test — the USB-class buffer sizes and BLE stack should work unchanged, but expect descriptor-aggregation quirks until verified.
