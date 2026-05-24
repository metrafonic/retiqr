"""Integration tests for bridge.py — TCP server + HDLC routing."""
import asyncio
import threading
import pytest

from bridge import Bridge
from framing import hdlc_encode, hdlc_decode_stream


async def _connect(port: int):
    return await asyncio.open_connection("127.0.0.1", port)


@pytest.fixture
async def bridge():
    b = Bridge(port=0)  # OS picks a free port
    await b.start()
    port = b._server.sockets[0].getsockname()[1]
    b._port = port
    yield b
    await b.stop()


@pytest.mark.asyncio
async def test_bridge_starts(bridge: Bridge):
    assert bridge._server is not None
    assert bridge._server.is_serving()


@pytest.mark.asyncio
async def test_reticulum_connects(bridge: Bridge):
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)
    assert bridge.connection_count == 1
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    assert bridge.connection_count == 0


@pytest.mark.asyncio
async def test_put_rx_delivers_hdlc_to_reticulum(bridge: Bridge):
    """put_rx(packet) should arrive HDLC-encoded at the connected Reticulum client."""
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = b"\x01\x02\x03\x04"
    bridge.put_rx(pkt)

    data = await asyncio.wait_for(reader.read(64), timeout=2)
    assert hdlc_decode_stream(data) == [pkt]

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_put_rx_multiple_packets(bridge: Bridge):
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    packets = [bytes([i, i + 1]) for i in range(5)]
    for p in packets:
        bridge.put_rx(p)

    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 2
    while asyncio.get_event_loop().time() < deadline:
        chunk = await asyncio.wait_for(reader.read(256), timeout=1)
        buf.extend(chunk)
        if len(hdlc_decode_stream(bytes(buf))) >= len(packets):
            break

    assert hdlc_decode_stream(bytes(buf)) == packets
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_on_tx_packet_called_when_reticulum_sends(bridge: Bridge):
    """Bytes written by Reticulum should arrive decoded via on_tx_packet."""
    received: list[bytes] = []
    bridge.on_tx_packet = received.append

    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = b"\xDE\xAD\xBE\xEF"
    writer.write(hdlc_encode(pkt))
    await writer.drain()

    await asyncio.sleep(0.1)
    assert received == [pkt]

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_on_tx_packet_multi_frame_stream(bridge: Bridge):
    received: list[bytes] = []
    bridge.on_tx_packet = received.append

    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkts = [b"\x01", b"\x02\x03", b"\x04\x05\x06"]
    writer.write(b"".join(hdlc_encode(p) for p in pkts))
    await writer.drain()

    await asyncio.sleep(0.1)
    assert received == pkts

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_concurrent_connections_isolated(bridge: Bridge):
    """Each connection has its own rx queue — put_rx delivers to all."""
    r1, w1 = await _connect(bridge._port)
    r2, w2 = await _connect(bridge._port)
    await asyncio.sleep(0.05)
    assert bridge.connection_count == 2

    pkt = b"\xAA\xBB"
    bridge.put_rx(pkt)

    d1 = await asyncio.wait_for(r1.read(64), timeout=2)
    d2 = await asyncio.wait_for(r2.read(64), timeout=2)
    assert hdlc_decode_stream(d1) == [pkt]
    assert hdlc_decode_stream(d2) == [pkt]

    for w in (w1, w2):
        w.close()
        await w.wait_closed()


@pytest.mark.asyncio
async def test_put_rx_no_connections_is_noop(bridge: Bridge):
    # Should not raise even with no active connections.
    bridge.put_rx(b"\x00")


@pytest.mark.asyncio
async def test_put_rx_from_background_thread(bridge: Bridge):
    """put_rx must be safe to call from a non-asyncio thread (camera thread scenario)."""
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    packets = [bytes([i]) * 4 for i in range(1, 6)]

    def _send():
        for p in packets:
            bridge.put_rx(p)

    threading.Thread(target=_send, daemon=True).start()

    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 2
    while asyncio.get_event_loop().time() < deadline:
        chunk = await asyncio.wait_for(reader.read(256), timeout=1)
        buf.extend(chunk)
        if len(hdlc_decode_stream(bytes(buf))) >= len(packets):
            break

    assert hdlc_decode_stream(bytes(buf)) == packets
    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_tx_escapes_special_bytes(bridge: Bridge):
    """Packets containing flag/escape bytes are correctly recovered."""
    received: list[bytes] = []
    bridge.on_tx_packet = received.append

    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = bytes([0x7E, 0x7D, 0xFF])
    writer.write(hdlc_encode(pkt))
    await writer.drain()

    await asyncio.sleep(0.1)
    assert received == [pkt]

    writer.close()
    await writer.wait_closed()
