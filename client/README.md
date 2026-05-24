# client

Desktop app (Mac/Linux/Pi Zero) that bridges a local Reticulum stack to the kiosk browser session via webcam QR decode (RX) and HID keyboard typing (TX), or via vendor-HID fast path (`--mode webhid`).

## Running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python main.py --uplink ble                        # standard path (QR + keyboard)
python main.py --uplink ble --mode webhid          # fast path (vendor-HID, no camera)
python main.py --uplink gadget                     # Pi Zero with /dev/hidg0
python main.py --uplink none                       # development, no hardware
```

| Flag | Default | Description |
|------|---------|-------------|
| `--uplink` | *(required)* | `ble`, `gadget`, or `none` |
| `--mode` | `legacy` | `legacy` (QR + keyboard) or `webhid` (vendor-HID fast path, no camera) |
| `--key-delay-ms` | `5` | Inter-keystroke delay sent to dongle/gadget |
| `--gadget-device` | `/dev/hidg0` | HID gadget device path (gadget uplink only) |
| `--bridge-port` | `4243` | TCP port for the Reticulum TCPClientInterface |
| `--camera` | `0` | OpenCV camera device index (legacy mode only) |

Add to `~/.reticulum/config`:
```
[[KioskInterface]]
  type = TCPClientInterface
  interface_enabled = yes
  target_host = 127.0.0.1
  target_port = 4243
```

## Testing

```bash
pytest                           # 87 tests, ~2s, no hardware needed
pytest tests/test_bridge.py
pytest tests/test_decoder.py
pytest -k test_basic_round_trip
```

## TX uplink backends

### BleUplink (`--uplink ble`)

Connects to the ESP32-S3 dongle (`firmware/`) via Bluetooth. Scans for `"KioskDongle"`, "Just Works" pairing (no PIN). Legacy frames split into 20-byte BLE chunks; fast-path WHID-TX writes use 63-byte chunks (one HID report body per write) after MTU negotiation.

### GadgetUplink (`--uplink gadget`)

Writes 8-byte USB HID boot-protocol reports directly to `/dev/hidg0`. For Pi Zero only.

**One-time Pi Zero OS setup:**
1. Add `dtoverlay=dwc2` to `/boot/config.txt`
2. Add `dwc2` and `libcomposite` to `/etc/modules`
3. Create a HID keyboard gadget via configfs so `/dev/hidg0` appears on USB connect

## macOS notes

- Camera permission dialog must be triggered from the Qt main thread — `gui/app.py` probes and releases `cv2.VideoCapture` before starting the background thread.
- Use zxing-cpp for QR decode. pyzbar/zbar does locale-dependent Big5 conversion on macOS that corrupts binary payloads.
