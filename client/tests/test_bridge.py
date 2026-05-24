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


# ─── Fast-path (WebHID) bridge behaviour ─────────────────────────────────────


@pytest.mark.asyncio
async def test_put_rx_raw_writes_unmodified(bridge: Bridge):
    """put_rx_raw writes the bytes to Reticulum verbatim (no HDLC re-encode)."""
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    # Already-HDLC-framed bytes — must be delivered as-is.
    raw = hdlc_encode(b"\x01\x02\x03")
    bridge.put_rx_raw(raw)

    data = await asyncio.wait_for(reader.read(64), timeout=2)
    assert data == raw       # byte-for-byte, no double-encoding

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_mixed_packet_and_raw_writes_preserve_order(bridge: Bridge):
    """Legacy and raw writes share one ordered drain() path on the socket."""
    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = b"\x01\x02"
    raw = hdlc_encode(b"\xaa")
    expected = hdlc_encode(pkt) + raw

    bridge.put_rx(pkt)
    bridge.put_rx_raw(raw)

    buf = bytearray()
    deadline = asyncio.get_event_loop().time() + 2
    while asyncio.get_event_loop().time() < deadline and len(buf) < len(expected):
        buf.extend(await asyncio.wait_for(reader.read(64), timeout=1))

    assert bytes(buf) == expected

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_on_tx_raw_fires_with_socket_chunks(bridge: Bridge):
    """on_tx_raw fires with raw socket chunks BEFORE HDLC decode."""
    raw_chunks: list[bytes] = []
    bridge.on_tx_raw = raw_chunks.append

    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = b"\xaa\xbb\xcc"
    framed = hdlc_encode(pkt)
    writer.write(framed)
    await writer.drain()

    await asyncio.sleep(0.1)
    # The socket may have coalesced or split the write, so check by content.
    assert b"".join(raw_chunks) == framed

    writer.close()
    await writer.wait_closed()


@pytest.mark.asyncio
async def test_on_tx_raw_and_packet_fire_together(bridge: Bridge):
    """Both callbacks fire for the same connection — caller chooses which to use."""
    raw_chunks: list[bytes] = []
    packets: list[bytes] = []
    bridge.on_tx_raw = raw_chunks.append
    bridge.on_tx_packet = packets.append

    reader, writer = await _connect(bridge._port)
    await asyncio.sleep(0.05)

    pkt = b"\x10\x20\x30"
    writer.write(hdlc_encode(pkt))
    await writer.drain()

    await asyncio.sleep(0.1)
    assert packets == [pkt]
    assert b"".join(raw_chunks) == hdlc_encode(pkt)

    writer.close()
    await writer.wait_closed()
