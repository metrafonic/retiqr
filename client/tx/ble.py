"""
BLE uplink to the ESP32-S3 HID dongle.

The dongle advertises as "KioskDongle" and exposes two GATT characteristics:

  TX_CHAR  (write-without-response): receives HID frame bytes, types them via USB HID
  CFG_CHAR (write): 2-byte big-endian inter-key delay in ms

Frames are split into _BLE_CHUNK-byte pieces before writing so the code works
on any platform regardless of negotiated ATT MTU.  The dongle firmware
reassembles by scanning for the trailing '<' that ends every HID frame.

Pairing: "Just Works" (no PIN).  The HID frame's own XOR checksum catches
any data corruption.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_SERVICE_UUID  = "4b696f73-6b55-0001-0000-000000000000"
_TX_CHAR_UUID  = "4b696f73-6b55-0002-0000-000000000000"
_CFG_CHAR_UUID = "4b696f73-6b55-0003-0000-000000000000"
_DEVICE_NAME   = "KioskDongle"
_SCAN_TIMEOUT  = 10.0   # seconds per scan attempt
_RETRY_DELAY   = 3.0    # seconds between failed attempts
_BLE_CHUNK     = 20     # bytes per write — safe on all platforms (default ATT MTU - 3)


class BleUplink:
    """BLE central that drives the ESP32-S3 HID dongle."""

    kind = "BLE"

    def __init__(self, key_delay_ms: int = 5):
        self.on_connect    = None   # callable()
        self.on_disconnect = None   # callable()
        self._key_delay_ms = key_delay_ms
        self._connected    = False
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def send(self, frame: bytes) -> None:
        """Queue a HID frame. Called from within the event loop."""
        self._queue.put_nowait(frame)

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        while True:
            stop_requested = await self._connect_and_run()
            if stop_requested:
                break
            await asyncio.sleep(_RETRY_DELAY)

    async def stop(self) -> None:
        self._queue.put_nowait(None)

    async def _connect_and_run(self) -> bool:
        """Connect, forward frames until disconnect or stop(). Returns True if stop()."""
        from bleak import BleakClient, BleakScanner

        log.info("BLE: scanning for %r ...", _DEVICE_NAME)
        device = await BleakScanner.find_device_by_name(_DEVICE_NAME, timeout=_SCAN_TIMEOUT)
        if device is None:
            log.debug("BLE: %r not found", _DEVICE_NAME)
            return False

        disconnected = asyncio.Event()

        def _on_disconnect(_client: BleakClient) -> None:
            disconnected.set()

        log.info("BLE: connecting to %s", device.address)
        try:
            async with BleakClient(device, disconnected_callback=_on_disconnect) as client:
                try:
                    await client.write_gatt_char(
                        _CFG_CHAR_UUID, self._key_delay_ms.to_bytes(2, "big")
                    )
                except Exception:
                    pass  # CFG char optional — firmware may not implement it yet

                self._connected = True
                if self.on_connect:
                    self.on_connect()
                log.info("BLE: connected to %s", device.address)

                while not disconnected.is_set():
                    try:
                        frame = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if frame is None:
                        return True  # stop() requested
                    for i in range(0, len(frame), _BLE_CHUNK):
                        await client.write_gatt_char(
                            _TX_CHAR_UUID, frame[i:i + _BLE_CHUNK], response=False
                        )

        except Exception as exc:
            log.warning("BLE: %s", exc)
        finally:
            if self._connected:
                self._connected = False
                if self.on_disconnect:
                    self.on_disconnect()
                log.info("BLE: disconnected")

        return False  # disconnected — outer loop will retry


class NullUplink:
    """No-op uplink for development / testing — logs frames instead of sending them."""

    kind = "none"

    def __init__(self) -> None:
        self.on_connect    = None
        self.on_disconnect = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def send(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    @property
    def connected(self) -> bool:
        return False

    async def run(self) -> None:
        log.warning("NullUplink: frames will be logged, not sent")
        while True:
            item = await self._queue.get()
            if item is None:
                break
            log.debug("TX stub: %r", item)

    async def stop(self) -> None:
        self._queue.put_nowait(None)
