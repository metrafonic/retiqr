# retiqr — An alternative reticulum QR+HID interface

Some places have internet but won't give it to you. Hotel lobbies, library terminals, car dashboards, airport kiosks — a browser is right there, but there's no open WiFi, no ethernet port, no way to get your device online.

retiqr fixes that. Navigate to the retiqr website on the kiosk browser — it renders animated QR codes carrying data down to your device, and accepts typed keystrokes as the uplink. A small Python app on your machine decodes the QR feed via camera, forwards outgoing traffic through a thumb-drive sized Bluetooth dongle plugged into the kiosk USB port, and exposes a local TCP server that Reticulum connects to as a standard TCPClientInterface.

Typical throughput: ~3 kB/s down (QR), ~500 B/s up (HID). Enough for messaging and Nomad Network.

> **Experimental:** A WebHID fast path (thanks [@KenAKAFrosty](https://github.com/KenAKAFrosty)) is available on Chrome/Edge when the dongle is plugged in, lifting throughput to tens of KiB/s. See each component's README for details.

[![Client app in action](docs/demo.png)](docs/demo.png)

*The client app viewfinder pointed at a laptop running the gateway page. The ESP32 BLE dongle is visible bottom-left, plugged into the kiosk USB port. Status bar confirms Reticulum connected with an active BLE link.*

## Components

| Directory | Description |
|-----------|-------------|
| [`gateway/`](gateway/README.md)   | Server-side: aiohttp app that serves the kiosk page and splices WebSocket ↔ Reticulum TCP |
| [`client/`](client/README.md)     | Your-side: desktop app (Mac/Linux/Pi Zero) — webcam QR decode + BLE/HID uplink + Reticulum TCP bridge |
| [`firmware/`](firmware/README.md) | ESP32-S3 (M5Stack AtomS3 Lite) composite USB-HID dongle |
| `shared/`                         | `framing.py` — wire formats shared by gateway and client |

## How it works

```
                Reticulum network
                       ↕ TCP/HDLC
                    gateway/                ← public-internet server
                       ↕ WebSocket (binary HDLC stream)
                  kiosk browser             ← navigate here on the kiosk
                       ↕
              QR animation / WebHID
                       ↕
              ESP32-S3 dongle (USB)
                       ↕ BLE / Wi-Fi
                  client/ (laptop)
                       ↕ TCP/HDLC (localhost:4243)
                    Reticulum stack
              (your apps: Sideband, NomadNet, …)
```

## Status

| Path                                              | Announces | Messaging | NomadNet |
| ------------------------------------------------- | --------- | --------- | -------- |
| Laptop + ESP32-S3 dongle (standard QR + keyboard) | yes       | yes       | yes      |
| Laptop + ESP32-S3 dongle (WebHID fast path)       | yes\*     | yes\*     | yes\*    |
| Pi Zero USB HID gadget                            | untested  | untested  | untested |

\* Experimental. Hardware-verified on an ESP32-S3 AtomS3 Lite.

The Pi Zero gadget path (`--uplink gadget`) is implemented but has not been tested on hardware. Contributions welcome.

## Thanks

[@KenAKAFrosty](https://github.com/KenAKAFrosty) — WebHID fast-path implementation.

## AI disclosure

I am a developer by profession (10+ years).
This repo was built with AI assistance (Claude).
