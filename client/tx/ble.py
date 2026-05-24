"""
BLE uplink to the ESP32-S3 KioskDongle.

The dongle advertises as "KioskDongle" and exposes four GATT characteristics:

  TX_CHAR       (write-without-response): legacy path — receives ">HEX<" HID
                  frame bytes; dongle types each character as a USB HID
                  keystroke. Slow but works on any browser.
  CFG_CHAR      (write): 2-byte big-endian inter-key delay in ms
  WHID_TX_CHAR  (write-without-response): fast path — receives 63-byte vendor
                  HID report bodies (1 length byte + <=62 payload bytes,
                  per shared.framing.hid_report_pack). Dongle forwards each
                  completed report as a USB HID Input Report with report ID 6.
  WHID_RX_CHAR  (notify): fast path — receives vendor HID Output Reports
                  payloads from the kiosk page (already HDLC-framed bytes).

Both paths share BLE chunking: writes are split into _BLE_CHUNK-byte pieces
so the wire format is agnostic to negotiated ATT MTU.

Pairing: "Just Works" (no PIN). HDLC's own checksum (legacy: ">HEX<CC<") and
USB's CRC (fast) catch corruption.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_SERVICE_UUID      = "4b696f73-6b55-0001-0000-000000000000"
_TX_CHAR_UUID      = "4b696f73-6b55-0002-0000-000000000000"
_CFG_CHAR_UUID     = "4b696f73-6b55-0003-0000-000000000000"
_WHID_RX_CHAR_UUID = "4b696f73-6b55-0004-0000-000000000000"
_WHID_TX_CHAR_UUID = "4b696f73-6b55-0005-0000-000000000000"
_DEVICE_NAME       = "KioskDongle"
_SCAN_TIMEOUT      = 10.0   # seconds per scan attempt
_RETRY_DELAY       = 3.0    # seconds between failed attempts
_BLE_CHUNK         = 240    # bytes per write — fits in MTU 247 - 3 ATT header.
                            # Firmware advertises a preferred MTU of 247; on
                            # platforms where the negotiation succeeds, a
                            # whole 63-byte vendor HID report goes in one
                            # write. Falls back gracefully on platforms that
                            # only negotiate the 23-byte default (the write
                            # just errors and bleak surfaces it).


class BleUplink:
    """BLE central that drives the ESP32-S3 KioskDongle.

    Two send modes share the BLE link:
      * send(frame)  -> legacy TX char (typed by dongle as keystrokes)
      * send_raw(b)  -> fast-path WHID-TX char (USB HID Input Report)

    Only one direction of inbound: on_rx_raw fires for WHID-RX notifications
    (bytes received from the kiosk page through the dongle).
    """

    kind = "BLE"

    def __init__(self, key_delay_ms: int = 5):
        self.on_connect    = None   # callable()
        self.on_disconnect = None   # callable()
        self.on_rx_raw     = None   # callable(bytes) — WHID-RX notify payload
        self._key_delay_ms = key_delay_ms
        self._connected    = False
        self._queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()

    def send(self, frame: bytes) -> None:
        """Queue a legacy HID-encoded frame for the typed-keystroke path."""
        self._queue.put_nowait(("tx", frame))

    def send_raw(self, payload: bytes) -> None:
        """Queue raw bytes for the vendor-HID fast path.

        `payload` is a sequence of pre-built 63-byte HID report bodies
        concatenated (see shared.framing.hid_report_chunks). The dongle
        accumulates exactly HID_REPORT_SIZE bytes per report body and
        forwards each completed report as a USB HID Input Report.
        """
        self._queue.put_nowait(("whid_tx", payload))

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
                # BlueZ doesn't negotiate ATT MTU on its own for WRITE_NR
                # characteristics; we have to explicitly ask. Without this
                # we're stuck at the 23-byte default and every report takes
                # 4 BLE packets instead of 1.
                try:
                    backend = getattr(client, "_backend", None)
                    acquire = getattr(backend, "_acquire_mtu", None) if backend else None
                    if callable(acquire):
                        await acquire()
                    log.info("BLE: negotiated MTU = %d", client.mtu_size)
                except Exception as exc:
                    log.debug("BLE: MTU negotiation skipped (%s)", exc)
                try:
                    await client.write_gatt_char(
                        _CFG_CHAR_UUID, self._key_delay_ms.to_bytes(2, "big")
                    )
                except Exception:
                    pass  # CFG char optional — firmware may not implement it yet

                # WHID-RX subscription — silently skipped on older firmware that
                # lacks the characteristic (legacy mode still works).
                try:
                    def _on_notify(_handle, data: bytearray) -> None:
                        if self.on_rx_raw is not None:
                            self.on_rx_raw(bytes(data))
                    await client.start_notify(_WHID_RX_CHAR_UUID, _on_notify)
                    log.info("BLE: subscribed to WHID-RX notifications")
                except Exception as exc:
                    log.debug("BLE: WHID-RX subscribe skipped (%s)", exc)

                self._connected = True
                if self.on_connect:
                    self.on_connect()
                log.info("BLE: connected to %s", device.address)

                while not disconnected.is_set():
                    try:
                        item = await asyncio.wait_for(self._queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if item is None:
                        return True  # stop() requested
                    kind_str, frame = item
                    char_uuid = _WHID_TX_CHAR_UUID if kind_str == "whid_tx" else _TX_CHAR_UUID
                    for i in range(0, len(frame), _BLE_CHUNK):
                        await client.write_gatt_char(
                            char_uuid, frame[i:i + _BLE_CHUNK], response=False
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
        self.on_rx_raw     = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def send(self, frame: bytes) -> None:
        self._queue.put_nowait(frame)

    def send_raw(self, payload: bytes) -> None:
        self._queue.put_nowait(payload)

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
