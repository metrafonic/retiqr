# firmware

ESP32-S3 firmware for the M5Stack AtomS3 Lite. Acts as a BLE central → USB HID keyboard bridge: receives HID frames from the client app over BLE and types them as keystrokes into the kiosk USB port.

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
| USB-C | Native ESP32-S3 USB OTG — HID keyboard output |

Connect a 3.3 V UART adapter to G5/G6 at 115200 baud to see debug output.

## LED states

| Colour | State |
|--------|-------|
| Yellow pulse | Scanning (no BLE connection) |
| Blue | Connected, idle |
| Green | Typing |

## Button

Long-press (3 s): clears BLE bonds and reboots. Bonding is not currently used — this is a no-op in practice, reserved for a future bonding implementation.

## GATT service

UUIDs must match `client/tx/ble.py` exactly.

| | UUID | Properties |
|-|------|------------|
| Service | `4b696f73-6b55-0001-0000-000000000000` | |
| TX char | `4b696f73-6b55-0002-0000-000000000000` | WRITE_NR |
| CFG char | `4b696f73-6b55-0003-0000-000000000000` | WRITE |

CFG char accepts a 2-byte big-endian inter-keystroke delay in milliseconds (clamped to [1, 100] by the firmware).
