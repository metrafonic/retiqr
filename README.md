# retiqr — An alternative reticulum QR+HID interface

Some places have internet but won't give it to you. Hotel lobbies, library terminals, car dashboards, airport kiosks — a browser is right there, but there's no open WiFi, no ethernet port, no way to get your device online.

retiqr fixes that. Navigate to the retiqr website on the kiosk browser — it renders animated QR codes carrying data down to your device, and accepts typed keystrokes as the uplink. A small Python app on your machine decodes the QR feed via camera, forwards outgoing traffic through a thumb-drive sized Bluetooth dongle plugged into the kiosk USB port, and exposes a local TCP server that Reticulum connects to as a standard TCPClientInterface.

Typical throughput on the standard path: ~3 kB/s down (QR), ~500 B/s up (HID). Enough for messaging and Nomad Network.

**Fast path (new):** When the kiosk browser supports the WebHID API (Chrome/Edge/Opera on desktop, secure context) and the user grants the page access to the dongle, retiqr switches to a vendor-HID transport: full-duplex 64-byte binary reports over the same USB port, no camera, no QR codes. On the current ESP32-S3 build, measured rates are in the tens of KiB/s: roughly ~40 KiB/s uplink over BLE and roughly ~45-58 KiB/s uplink / ~30 KiB/s clean downlink bursts over Wi-Fi AP. Click the **Fast** pill in the kiosk page when the dongle is plugged in. Falls back transparently to QR+keyboard when WebHID is unavailable.

[![Client app in action](docs/demo.png)](docs/demo.png)

*The client app viewfinder pointed at a laptop running the gateway page. The ESP32 BLE dongle is visible bottom-left, plugged into the kiosk USB port. Status bar confirms Reticulum connected with an active BLE link.*

## Components

| Directory | Description |
|-----------|-------------|
| [`gateway/`](gateway/README.md)   | Server-side: aiohttp app that serves the kiosk page and splices WebSocket ↔ Reticulum TCP |
| [`client/`](client/README.md)     | Your-side: desktop app (Mac/Linux/Pi Zero) — webcam QR decode + BLE/HID uplink + Reticulum TCP bridge. Pass `--mode webhid` to skip the camera and use the fast path. |
| [`firmware/`](firmware/README.md) | ESP32-S3 (M5Stack AtomS3 Lite) composite USB-HID dongle: keyboard (standard) + vendor-HID (fast path) + BLE GATT |
| `shared/`                         | `framing.py` — HDLC, HID-keyboard frame, QR-fragment, and vendor-HID-report wire formats shared by gateway and client |

See each component's README for setup and usage details.

## Modes

Two independent mode axes exist:

- **Transport mode**
  - **standard** — QR downlink + keyboard HID uplink
  - **fast** — WebHID binary transport over the vendor HID interface
- **Peer link**
  - **BLE** — current desktop-client path
  - **Wi-Fi AP** — dongle-hosted access point + direct TCP socket for the fast path

## How it works

```
                Reticulum network
                       ↕ TCP/HDLC
                    gateway/                ← public-internet server
                       ↕ WebSocket (binary HDLC stream)
                  kiosk browser             ← navigate here on the kiosk
                  /            \
   Standard path                 Fast path (WebHID)
        ↕                             ↕
   QR animation on canvas        64-byte vendor-HID reports,
   (gateway → laptop, ~3 kB/s)   full duplex, tens of KiB/s
        ↕                             ↕
   webcam captures               ESP32-S3 dongle plugged
   QR codes; OpenCV +            into kiosk USB port —
   zxing-cpp decode              composite HID (keyboard
        ↕                        for standard + vendor for fast)
   client/ (laptop)                    ↕  BLE GATT
                                  client/ (laptop)
                              (--mode webhid: no camera needed)

                       ↕ TCP/HDLC (localhost:4243)
                    Reticulum stack
              (your apps: Sideband, NomadNet, …)
```

The gateway is protocol-blind — it splices bytes without knowing anything about Reticulum. With the fast path active, **the kiosk page becomes protocol-blind too**: WebSocket bytes flow straight to/from 64-byte HID reports with no Reticulum-aware parsing on the page. All Reticulum-level intelligence remains on your device.

## Status

| Path                                              | Announces | Messaging | NomadNet |
| ------------------------------------------------- | --------- | --------- | -------- |
| Laptop + ESP32-S3 dongle (standard QR + keyboard) | yes       | yes       | yes      |
| Laptop + ESP32-S3 dongle (WebHID fast path)       | yes\*     | yes\*     | yes\*    |
| Pi Zero USB HID gadget                            | untested  | untested  | untested |

\* Fast path: hardware-verified on an ESP32-S3 AtomS3 Lite. Current measured ranges are roughly ~40 KiB/s lossless uplink over BLE, plus ~45-58 KiB/s uplink and ~30 KiB/s clean downlink bursts over Wi-Fi AP. BLE downlink remains the weaker, burst-sensitive side.

The Pi Zero gadget path (`--uplink gadget`) is implemented but has not been tested on hardware. Contributions welcome.

## AI disclosure

I am a developer by profession (10+ years).
This repo was built with AI assistance (Claude).
