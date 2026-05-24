# retiqr — An alternative reticulum QR+HID interface

Some places have internet but won't give it to you. Hotel lobbies, library terminals, car dashboards, airport kiosks — a browser is right there, but there's no open WiFi, no ethernet port, no way to get your device online.

retiqr fixes that. Navigate to the retiqr website on the kiosk browser — it renders animated QR codes carrying data down to your device, and accepts typed keystrokes as the uplink. A small Python app on your machine decodes the QR feed via camera, forwards outgoing traffic through a thumb-drive sized Bluetooth dongle plugged into the kiosk USB port, and exposes a local TCP server that Reticulum connects to as a standard TCPClientInterface.

Typical throughput: ~3 kB/s down (QR), ~500 B/s up (HID). Enough for messaging and Nomad Network.

![Client app in action](docs/demo.png)

*The client app viewfinder pointed at a laptop running the gateway page. The ESP32 BLE dongle is visible bottom-left, plugged into the kiosk USB port. Status bar confirms Reticulum connected with an active BLE link.*

## Components

| Directory | Description |
|-----------|-------------|
| [`gateway/`](gateway/README.md) | Server-side: aiohttp app that serves the kiosk page and splices WebSocket ↔ Reticulum TCP |
| [`client/`](client/README.md) | Your-side: desktop app (Mac/Linux/Pi Zero) — webcam QR decode + BLE/HID uplink + Reticulum TCP bridge |
| [`firmware/`](firmware/README.md) | ESP32-S3 (M5Stack AtomS3 Lite) BLE → USB HID keyboard dongle for the laptop uplink path |
| `shared/` | `framing.py` — HDLC, HID, and QR wire format shared by gateway and client |

See each component's README for setup and usage details.

## How it works

```
Reticulum network
  ↕ TCP/HDLC
gateway/            ← your server, reachable from the public internet
  ↕ WebSocket
kiosk browser       ← you navigate here on the kiosk
  ↕ QR codes (downlink) / HID keystrokes (uplink)
client/             ← running on your laptop or Pi Zero
  ↕ TCP/HDLC (localhost:4243)
Reticulum stack     ← your apps: Sideband, NomadNet, etc.
```

The gateway is protocol-blind — it splices bytes without knowing anything about Reticulum. All the intelligence is on your device.

## Status

| Uplink path | Announces | Messaging | NomadNet |
|-------------|-----------|-----------|----------|
| Laptop + ESP32-S3 BLE dongle | yes | yes | yes |
| Pi Zero USB HID gadget | untested | untested | untested |

The Pi Zero gadget path (`--uplink gadget`) is implemented but has not been tested on hardware. Contributions welcome.

## AI disclosure

I am a developer by profession (10+ years).  
This repo was built with AI assistance (Claude).
