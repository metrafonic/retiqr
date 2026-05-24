"""Tests for tx/ble.py — BleUplink and NullUplink."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tx.ble import (
    BleUplink,
    NullUplink,
    _BLE_CHUNK_FAST,
    _BLE_CHUNK_LEGACY,
    _TX_CHAR_UUID,
    _WHID_TX_CHAR_UUID,
)


# ─── NullUplink ───────────────────────────────────────────────────────────────

def test_null_send_queues_tx_tuple():
    ul = NullUplink()
    ul.send(b"hello")
    item = ul._queue.get_nowait()
    assert item == ("tx", b"hello")


def test_null_send_raw_queues_whid_tx_tuple():
    ul = NullUplink()
    ul.send_raw(b"\xaa\xbb")
    item = ul._queue.get_nowait()
    assert item == ("whid_tx", b"\xaa\xbb")


async def test_null_run_drains_both_kinds():
    ul = NullUplink()
    ul.send(b"legacy")
    ul.send_raw(b"fast")
    await ul.stop()
    await asyncio.wait_for(ul.run(), timeout=2)


def test_null_uplink_not_connected():
    assert NullUplink().connected is False


# ─── BleUplink queue shape ────────────────────────────────────────────────────

def test_ble_send_queues_tx_tuple():
    ul = BleUplink()
    ul.send(b"frame")
    item = ul._queue.get_nowait()
    assert item == ("tx", b"frame")


def test_ble_send_raw_queues_whid_tx_tuple():
    ul = BleUplink()
    ul.send_raw(b"\x01\x02\x03")
    item = ul._queue.get_nowait()
    assert item == ("whid_tx", b"\x01\x02\x03")


def test_ble_stop_queues_none():
    ul = BleUplink()
    # stop() is async but put_nowait is synchronous — call it via the loop
    loop = asyncio.new_event_loop()
    loop.run_until_complete(ul.stop())
    loop.close()
    assert ul._queue.get_nowait() is None


# ─── BleUplink chunk-size dispatch (mocked bleak) ────────────────────────────

class _FakeBleakClient:
    """Minimal BleakClient stand-in that records write_gatt_char calls."""

    mtu_size = 247

    def __init__(self, device, disconnected_callback=None):
        self._disconnected_cb = disconnected_callback
        self.writes: list[tuple[str, bytes]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def write_gatt_char(self, uuid, data, response=False):
        self.writes.append((uuid, bytes(data)))

    async def start_notify(self, uuid, handler):
        pass


async def _run_uplink_with_mock(ul: BleUplink, fake_client: _FakeBleakClient):
    """Connect the uplink against a mock device, drain the queue, return."""
    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"

    with patch("bleak.BleakScanner.find_device_by_name", new=AsyncMock(return_value=device)), \
         patch("bleak.BleakClient", return_value=fake_client):
        task = asyncio.create_task(ul.run())
        # Let the connect loop spin up.
        await asyncio.sleep(0.05)
        # Queue items then stop.
        await asyncio.wait_for(task, timeout=2)
    return fake_client.writes


async def test_legacy_send_uses_legacy_chunk_size():
    fake = _FakeBleakClient(None)
    ul = BleUplink()

    # A legacy frame larger than _BLE_CHUNK_LEGACY to verify chunking.
    frame = b"A" * (_BLE_CHUNK_LEGACY * 2 + 5)
    ul.send(frame)
    await ul.stop()

    writes = await _run_uplink_with_mock(ul, fake)
    legacy_writes = [(uuid, data) for uuid, data in writes if uuid == _TX_CHAR_UUID]

    assert legacy_writes, "expected at least one write to TX char"
    for _, data in legacy_writes:
        assert len(data) <= _BLE_CHUNK_LEGACY
    assert b"".join(d for _, d in legacy_writes) == frame


async def test_fast_send_uses_fast_chunk_size():
    fake = _FakeBleakClient(None)
    ul = BleUplink()

    # A fast payload larger than _BLE_CHUNK_FAST to verify chunking.
    payload = b"B" * (_BLE_CHUNK_FAST + 10)
    ul.send_raw(payload)
    await ul.stop()

    writes = await _run_uplink_with_mock(ul, fake)
    fast_writes = [(uuid, data) for uuid, data in writes if uuid == _WHID_TX_CHAR_UUID]

    assert fast_writes, "expected at least one write to WHID-TX char"
    for _, data in fast_writes:
        assert len(data) <= _BLE_CHUNK_FAST
    assert b"".join(d for _, d in fast_writes) == payload


async def test_legacy_and_fast_use_separate_chars():
    """Legacy and fast frames go to different GATT characteristics."""
    fake = _FakeBleakClient(None)
    ul = BleUplink()

    ul.send(b"legacy")
    ul.send_raw(b"fast")
    await ul.stop()

    writes = await _run_uplink_with_mock(ul, fake)
    uuids = [uuid for uuid, _ in writes]
    assert _TX_CHAR_UUID in uuids
    assert _WHID_TX_CHAR_UUID in uuids


async def test_on_rx_raw_fires_on_whid_rx_notify():
    """on_rx_raw is called when the mock notifies the WHID-RX characteristic."""
    received: list[bytes] = []
    fake = _FakeBleakClient(None)
    notify_handlers: dict[str, object] = {}

    async def _start_notify(uuid, handler):
        notify_handlers[uuid] = handler

    fake.start_notify = _start_notify  # type: ignore[method-assign]

    ul = BleUplink()
    ul.on_rx_raw = received.append

    from tx.ble import _WHID_RX_CHAR_UUID
    ul.send(b"keepalive")  # keep the loop alive briefly
    await ul.stop()

    device = MagicMock()
    device.address = "AA:BB:CC:DD:EE:FF"

    with patch("bleak.BleakScanner.find_device_by_name", new=AsyncMock(return_value=device)), \
         patch("bleak.BleakClient", return_value=fake):
        task = asyncio.create_task(ul.run())
        await asyncio.sleep(0.05)
        # Simulate a WHID-RX notification arriving from the dongle.
        if _WHID_RX_CHAR_UUID in notify_handlers:
            notify_handlers[_WHID_RX_CHAR_UUID](None, bytearray(b"\xde\xad"))
        await asyncio.wait_for(task, timeout=2)

    assert received == [b"\xde\xad"]
