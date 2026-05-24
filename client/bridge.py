"""
Local TCP server that acts as a Reticulum TCPServerInterface endpoint.

Reticulum connects to this server using a TCPClientInterface entry in
~/.reticulum/config pointing at 127.0.0.1:4243.  Each connection gets
its own Bridge instance that:

  RX path (QR → Reticulum):
    Packets arrive via put_rx(packet) called by the camera thread.
    They are HDLC-encoded and written to the Reticulum TCP socket.

  TX path (Reticulum → HID):
    Bytes stream in from the Reticulum TCP socket.
    hdlc_decode_stream reassembles them into packets.
    Each packet is pushed to on_tx_packet(packet) — set by the caller
    to route to the BLE/HID uplink.

Usage:

    bridge = Bridge(host="127.0.0.1", port=4243)
    bridge.on_tx_packet = lambda pkt: ble.send(hid_encode(pkt))
    asyncio.run(bridge.serve_forever())
"""

import asyncio
import logging

from framing import hdlc_encode, hdlc_decode_stream

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 4243


class _Connection:
    """Handles one active Reticulum TCP connection."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        on_tx_packet,
        on_tx_raw=None,
    ):
        self._reader = reader
        self._writer = writer
        self._on_tx = on_tx_packet
        self._on_tx_raw = on_tx_raw
        self._rx_queue: asyncio.Queue[tuple[str, bytes] | None] = asyncio.Queue()
        self._loop = asyncio.get_running_loop()
        self._closed = False
        self.rx_bytes_sent: int = 0

    async def run(self):
        read_task  = asyncio.create_task(self._read_from_reticulum())
        write_task = asyncio.create_task(self._write_to_reticulum())
        try:
            _done, pending = await asyncio.wait(
                {read_task, write_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            self._closed = True
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass

    async def _read_from_reticulum(self):
        """Read HDLC stream from Reticulum, emit raw chunks AND decoded packets.

        Two consumers can wire in:
          * on_tx_raw    — gets each socket chunk untouched, HDLC and all.
                           Used by the WebHID fast path.
          * on_tx_packet — gets each decoded packet, ready for QR fragmentation.
                           Used by the legacy camera/QR path.

        Both fire if both are wired. The raw callback is cheap (no parse) and
        firing it unconditionally lets a single Reticulum connection feed
        either or both transports.
        """
        buf = bytearray()
        while True:
            chunk = await self._reader.read(4096)
            if not chunk:
                break
            if self._on_tx_raw:
                self._on_tx_raw(bytes(chunk))
            buf.extend(chunk)
            packets = hdlc_decode_stream(bytes(buf))
            # Keep only the tail from the last flag byte onward.
            last_flag = buf.rfind(0x7E)
            buf = buf[last_flag + 1:] if last_flag != -1 else buf
            for pkt in packets:
                log.debug("TX packet %d bytes → HID", len(pkt))
                if self._on_tx:
                    self._on_tx(pkt)

    async def _write_to_reticulum(self):
        """Drain the RX queue and serialize all writes to the Reticulum socket."""
        while True:
            item = await self._rx_queue.get()
            if item is None:
                break
            mode, payload = item
            try:
                if mode == "raw":
                    frame = payload
                else:
                    frame = hdlc_encode(payload)
                self._writer.write(frame)
                await self._writer.drain()
                self.rx_bytes_sent += len(frame)
                if mode == "raw":
                    log.debug("RX raw %d bytes → Reticulum (total %d)", len(frame), self.rx_bytes_sent)
                else:
                    log.info("RX packet %d bytes → Reticulum (total %d)", len(payload), self.rx_bytes_sent)
            except Exception as exc:
                log.warning("write to Reticulum failed: %s", exc)
                break

    def put_rx(self, packet: bytes):
        """Queue a decoded QR packet for delivery to Reticulum (thread-safe).

        Used by the legacy camera/QR path which delivers already-decoded
        packets (no HDLC framing). Packets are HDLC-encoded before being
        written to the Reticulum socket.
        """
        if not self._closed:
            self._loop.call_soon_threadsafe(self._rx_queue.put_nowait, ("packet", packet))

    def put_rx_raw(self, stream: bytes):
        """Write raw HDLC bytes straight to Reticulum (thread-safe).

        Used by the fast-path WHID-RX BLE notification handler. The bytes
        are already HDLC-framed (they were produced by Reticulum on the
        gateway end and spliced through the page+dongle untouched), so we
        skip the encode step and write them directly to the socket.

        We enqueue the write onto the bridge's own loop so this is safe to
        call from any thread (or from the bleak callback thread) and still
        participates in the same ordered drain() path as legacy writes.
        """
        if self._closed or not stream:
            return
        self._loop.call_soon_threadsafe(self._rx_queue.put_nowait, ("raw", stream))

    def close(self):
        self._closed = True
        self._loop.call_soon_threadsafe(self._rx_queue.put_nowait, None)


class Bridge:
    """Asyncio TCP server bridging local Reticulum connections to RX/TX queues."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self.on_tx_packet = None   # set to callable(packet: bytes) — legacy
        self.on_tx_raw    = None   # set to callable(chunk: bytes)  — fast path
        self._connections: list[_Connection] = []
        self._server: asyncio.Server | None = None
        self._rx_bytes_total: int = 0  # cumulative across all connections

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, self.host, self.port
        )
        log.info("Bridge listening on %s:%d", self.host, self.port)

    async def serve_forever(self):
        await self.start()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        for conn in list(self._connections):
            conn.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info("Reticulum connected from %s", peer)
        conn = _Connection(reader, writer, self.on_tx_packet, self.on_tx_raw)
        self._connections.append(conn)
        try:
            await conn.run()
        finally:
            self._rx_bytes_total += conn.rx_bytes_sent
            self._connections.remove(conn)
            log.info("Reticulum disconnected from %s", peer)

    def put_rx(self, packet: bytes):
        """Deliver a decoded QR packet to all active Reticulum connections."""
        for conn in list(self._connections):
            conn.put_rx(packet)

    def put_rx_raw(self, stream: bytes):
        """Deliver raw HDLC stream bytes to all active Reticulum connections.

        Used by the WebHID fast path — the bytes are already HDLC-framed
        because they came through the dongle straight from the gateway's
        TCP stream.
        """
        for conn in list(self._connections):
            conn.put_rx_raw(stream)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    @property
    def rx_bytes_sent(self) -> int:
        active = sum(c.rx_bytes_sent for c in list(self._connections))
        return self._rx_bytes_total + active
