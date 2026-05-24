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
from pathlib import Path

from aiohttp import web, WSMsgType

log = logging.getLogger(__name__)


async def _pipe_tcp_to_ws(reader: asyncio.StreamReader, ws: web.WebSocketResponse) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            await ws.send_bytes(data)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        await ws.close()


async def _pipe_ws_to_tcp(ws: web.WebSocketResponse, writer: asyncio.StreamWriter) -> None:
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        writer.close()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    host = request.app[_KEY_HOST]
    port = request.app[_KEY_PORT]

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("ws connect from %s", request.remote)

    try:
        reader, writer = await asyncio.open_connection(host, port)
        log.info("tcp connected to %s:%d", host, port)
    except OSError as exc:
        log.error("tcp connect failed: %s", exc)
        await ws.close(code=1011, message=b"tcp connect failed")
        return ws

    tcp_task = asyncio.create_task(_pipe_tcp_to_ws(reader, ws))
    ws_task = asyncio.create_task(_pipe_ws_to_tcp(ws, writer))

    _, pending = await asyncio.wait([tcp_task, ws_task], return_when=asyncio.FIRST_COMPLETED)
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
