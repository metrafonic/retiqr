#!/usr/bin/env python3
"""
WebSocket ↔ TCP splice gateway for the Reticulum kiosk interface.

Serves the static kiosk website and relays binary frames between the browser
WebSocket and a Reticulum TCPClientInterface endpoint. Protocol-agnostic —
all Reticulum framing and encryption passes through untouched.

Usage:
    python gateway.py --target-host mynode.example.com --target-port 4242
"""

import asyncio
import argparse
import logging
from dataclasses import dataclass
from pathlib import Path

from aiohttp import web, WSMsgType

log = logging.getLogger(__name__)

_WS_TO_TCP_DRAIN_BATCH = 4096
_STATS_INTERVAL_SECS = 1.0


@dataclass
class SpliceStats:
    up_total: int = 0
    down_total: int = 0
    up_window: int = 0
    down_window: int = 0

    def note_up(self, n: int) -> None:
        self.up_total += n
        self.up_window += n

    def note_down(self, n: int) -> None:
        self.down_total += n
        self.down_window += n

    def snapshot(self, interval_secs: float) -> dict[str, float | int | str]:
        up_kibps = self.up_window / 1024.0 / max(interval_secs, 1e-6)
        down_kibps = self.down_window / 1024.0 / max(interval_secs, 1e-6)
        self.up_window = 0
        self.down_window = 0
        return {
            "type": "splice_stats",
            "up_kibps": up_kibps,
            "down_kibps": down_kibps,
            "up_total": self.up_total,
            "down_total": self.down_total,
        }


async def _pipe_tcp_to_ws(
    reader: asyncio.StreamReader,
    ws: web.WebSocketResponse,
    send_lock: asyncio.Lock,
    stats: SpliceStats,
) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            async with send_lock:
                await ws.send_bytes(data)
            stats.note_down(len(data))
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        async with send_lock:
            await ws.close()


async def _pipe_ws_to_tcp(
    ws: web.WebSocketResponse,
    writer: asyncio.StreamWriter,
    stats: SpliceStats,
) -> None:
    pending = 0
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                pending += len(msg.data)
                if pending >= _WS_TO_TCP_DRAIN_BATCH:
                    await writer.drain()
                    stats.note_up(pending)
                    pending = 0
            elif msg.type == WSMsgType.TEXT:
                continue
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        if pending:
            try:
                await writer.drain()
                stats.note_up(pending)
            except Exception:
                pass
        writer.close()


async def _push_ws_stats(
    ws: web.WebSocketResponse,
    send_lock: asyncio.Lock,
    stats: SpliceStats,
) -> None:
    try:
        while not ws.closed:
            await asyncio.sleep(_STATS_INTERVAL_SECS)
            payload = stats.snapshot(_STATS_INTERVAL_SECS)
            async with send_lock:
                await ws.send_json(payload)
    except (asyncio.CancelledError, ConnectionResetError):
        pass


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    host = request.app[_KEY_HOST]
    port = request.app[_KEY_PORT]

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("ws connect from %s", request.remote)
    send_lock = asyncio.Lock()
    stats = SpliceStats()

    try:
        reader, writer = await asyncio.open_connection(host, port)
        log.info("tcp connected to %s:%d", host, port)
    except OSError as exc:
        log.error("tcp connect failed: %s", exc)
        await ws.close(code=1011, message=b"tcp connect failed")
        return ws

    tcp_task = asyncio.create_task(_pipe_tcp_to_ws(reader, ws, send_lock, stats))
    ws_task = asyncio.create_task(_pipe_ws_to_tcp(ws, writer, stats))
    stats_task = asyncio.create_task(_push_ws_stats(ws, send_lock, stats))

    _, pending = await asyncio.wait(
        [tcp_task, ws_task, stats_task], return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        await writer.wait_closed()
    except Exception:
        pass

    log.info("ws closed for %s", request.remote)
    return ws


async def index_handler(request: web.Request) -> web.FileResponse:
    return web.FileResponse(request.app[_KEY_STATIC] / "index.html")


_KEY_HOST   = web.AppKey("target_host", str)
_KEY_PORT   = web.AppKey("target_port", int)
_KEY_STATIC = web.AppKey("static_dir",  Path)


def build_app(target_host: str, target_port: int, static_dir: Path) -> web.Application:
    app = web.Application()
    app[_KEY_HOST]   = target_host
    app[_KEY_PORT]   = target_port
    app[_KEY_STATIC] = static_dir
    app.router.add_get("/ws", ws_handler)
    if static_dir.is_dir():
        app.router.add_get("/", index_handler)
        app.router.add_static("/static", static_dir)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Reticulum kiosk WebSocket gateway")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--target-host", required=True, help="Reticulum TCP interface host")
    parser.add_argument("--target-port", type=int, required=True, help="Reticulum TCP interface port")
    parser.add_argument("--static", default="static", help="Static files directory")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(message)s")

    app = build_app(args.target_host, args.target_port, Path(args.static))
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
