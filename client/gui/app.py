"""
PySide6 main window.

Layout:
  ┌─────────────────────────────────┐
  │  status bar (Reticulum / RX/TX) │
  ├─────────────────────────────────┤
  │                                 │
  │        camera viewfinder        │
  │                                 │
  └─────────────────────────────────┘

Camera runs in a background thread.  Frames are written to
self._latest_frame (GIL-safe single-reference swap); the QTimer on the
main thread picks them up and updates the viewfinder.
"""

from __future__ import annotations

# Ensure project root is on sys.path so sibling packages are importable
# regardless of the working directory when the script is run directly.
import sys as _sys, pathlib as _pathlib
_root = str(_pathlib.Path(__file__).resolve().parent.parent)
if _root not in _sys.path:
    _sys.path.insert(0, _root)
del _sys, _pathlib, _root

import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class KioskApp:
    """Thin wrapper — instantiate, call run()."""

    def __init__(self, bridge_port: int = 4243, camera_device: int = 0, uplink=None):
        self._bridge_port = bridge_port
        self._camera_device = camera_device
        self._uplink = uplink

    def run(self):
        import sys
        from PySide6.QtWidgets import QApplication

        app = QApplication(sys.argv)
        window = _MainWindow(self._bridge_port, self._camera_device, self._uplink)
        window.show()
        sys.exit(app.exec())


class _MainWindow:
    def __init__(self, bridge_port: int, camera_device: int, uplink=None):
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QImage, QPixmap
        from PySide6.QtWidgets import (
            QHBoxLayout, QLabel, QMainWindow, QStatusBar, QVBoxLayout, QWidget,
        )

        self._QImage = QImage
        self._QPixmap = QPixmap
        self._Qt = Qt

        self._win = QMainWindow()
        self._win.setWindowTitle("Reticulum Kiosk Bridge")
        self._win.resize(800, 600)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        self._win.setCentralWidget(central)

        self._view = QLabel("no camera")
        self._view.setAlignment(Qt.AlignCenter)
        self._view.setStyleSheet("background: #111; color: #555;")
        from PySide6.QtGui import QFontDatabase
        self._view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))

        # ── connection bar (above viewfinder) ────────────────────────────────
        self._ret_dot  = QLabel()
        self._ret_dot.setFixedSize(10, 10)
        self._ret_info = QLabel("Reticulum: disconnected")
        self._ret_info.setStyleSheet("color: #888;")

        self._up_dot   = QLabel()
        self._up_dot.setFixedSize(10, 10)
        self._up_info  = QLabel("TX: —")
        self._up_info.setStyleSheet("color: #888;")

        conn_bar = QWidget()
        conn_bar.setFixedHeight(28)
        conn_bar.setStyleSheet("background: #1a1a1a;")
        bar_layout = QHBoxLayout(conn_bar)
        bar_layout.setContentsMargins(10, 0, 10, 0)
        bar_layout.setSpacing(6)
        bar_layout.addWidget(self._ret_dot)
        bar_layout.addWidget(self._ret_info)
        bar_layout.addSpacing(20)
        bar_layout.addWidget(self._up_dot)
        bar_layout.addWidget(self._up_info)
        bar_layout.addStretch()
        layout.addWidget(conn_bar)
        layout.addWidget(self._view)

        # ── status bar (counters only) ────────────────────────────────────────
        self._rx_label     = QLabel("RX: 0")
        self._rx_b_label   = QLabel("→TCP: 0 B")
        self._tx_label     = QLabel("TX: 0")
        self._qr_label     = QLabel("QR: 0")
        self._seq_label    = QLabel("seq: —")
        self._frame_label  = QLabel("frames: 0")
        status = QStatusBar()
        status.addWidget(self._rx_label)
        status.addWidget(self._rx_b_label)
        status.addWidget(self._tx_label)
        status.addWidget(self._qr_label)
        status.addWidget(self._seq_label)
        status.addWidget(self._frame_label)
        self._win.setStatusBar(status)

        self._ret_dot.setStyleSheet(self._DOT_GRAY)
        self._up_dot.setStyleSheet(self._DOT_GRAY)

        self._rx_count     = 0
        self._tx_count     = 0
        self._qr_raw_count = 0
        self._frame_count  = 0
        self._last_frag: tuple[int, int, int] | None = None  # (seq, frag_idx, frag_total)
        self._bridge      = None
        self._latest_frame = None   # written by camera thread, read by Qt timer

        self._timer = QTimer()
        self._timer.setInterval(50)   # 20 fps display refresh
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        self._start_bridge(bridge_port, uplink)
        self._start_camera(camera_device)

    def show(self):
        self._win.show()

    # ── bridge ────────────────────────────────────────────────────────────────

    def _start_bridge(self, port: int, uplink=None):
        from bridge import Bridge
        from framing import hid_encode
        from tx.ble import NullUplink

        self._ble = uplink if uplink is not None else NullUplink()
        self._bridge = Bridge(port=port)

        def on_tx(pkt: bytes):
            self._tx_count += 1
            self._ble.send(hid_encode(pkt).encode())

        self._bridge.on_tx_packet = on_tx

        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(
                    asyncio.gather(self._bridge.serve_forever(), self._ble.run())
                )
            except Exception as exc:
                log.error("Bridge failed: %s", exc)
                self._bridge_error = str(exc)

        self._bridge_error = None
        threading.Thread(target=_run, daemon=True, name="bridge").start()

    # ── camera ────────────────────────────────────────────────────────────────

    def _start_camera(self, device: int):
        import cv2
        from rx.camera import Camera
        from rx.decoder import decode_frame

        # On macOS, the camera permission dialog must be triggered from the
        # main thread.  Opening and immediately releasing a VideoCapture here
        # causes macOS to show the dialog (or silently succeeds if already
        # granted).  The background camera thread then opens it properly.
        _probe = cv2.VideoCapture(device)
        _probe.release()

        self._camera = Camera(device=device)
        from rx.reassembler import Reassembler
        reassembler = Reassembler()

        def on_frame(img):   # called from camera thread (already RGB)
            self._frame_count += 1
            fragments, n_raw = decode_frame(img)
            self._qr_raw_count += n_raw
            for seq, chunk, frag_idx, frag_total in fragments:
                self._last_frag = (seq, frag_idx, frag_total)
                pkt = reassembler.feed(seq, chunk, frag_idx, frag_total)
                if pkt is not None:
                    self._rx_count += 1
                    if self._bridge:
                        self._bridge.put_rx(pkt)
            self._latest_frame = img   # GIL-safe; Qt timer reads on main thread

        self._camera.on_frame = on_frame
        self._camera.start()

    # ── Qt timer tick (main thread) ───────────────────────────────────────────

    def _tick(self):
        frame = self._latest_frame
        if frame is not None:
            self._latest_frame = None
            self._render_frame(frame)
        self._update_status()

    def _render_frame(self, img):
        from rx.decoder import _to_rgb
        img = _to_rgb(img)
        h, w = img.shape[:2]
        qi = self._QImage(img.data, w, h, 3 * w, self._QImage.Format_RGB888)
        px = self._QPixmap.fromImage(qi)
        scaled = px.scaled(
            self._view.width(), self._view.height(),
            self._Qt.KeepAspectRatio, self._Qt.SmoothTransformation,
        )
        self._view.setPixmap(scaled)

    # ── dot colours ──────────────────────────────────────────────────────────
    _DOT_GREEN  = "background:#2ecc71; border-radius:5px;"
    _DOT_ORANGE = "background:#e67e22; border-radius:5px;"
    _DOT_GRAY   = "background:#555;    border-radius:5px;"
    _DOT_RED    = "background:#e74c3c; border-radius:5px;"

    _TEXT_OK   = "color:#ccc;"
    _TEXT_WARN = "color:#e67e22;"
    _TEXT_DIM  = "color:#666;"
    _TEXT_ERR  = "color:#e74c3c;"

    def _update_status(self):
        # ── Reticulum indicator ───────────────────────────────────────────────
        if self._bridge_error:
            self._ret_dot.setStyleSheet(self._DOT_RED)
            self._ret_info.setStyleSheet(self._TEXT_ERR)
            self._ret_info.setText("Bridge error: %s" % self._bridge_error)
        else:
            conn = self._bridge.connection_count if self._bridge else 0
            if conn:
                self._ret_dot.setStyleSheet(self._DOT_GREEN)
                self._ret_info.setStyleSheet(self._TEXT_OK)
                self._ret_info.setText("Reticulum: connected (%d)" % conn)
            else:
                self._ret_dot.setStyleSheet(self._DOT_GRAY)
                self._ret_info.setStyleSheet(self._TEXT_DIM)
                self._ret_info.setText("Reticulum: disconnected")

        # ── uplink indicator ──────────────────────────────────────────────────
        kind = getattr(self._ble, "kind", "?")
        if kind == "none":
            self._up_dot.setStyleSheet(self._DOT_GRAY)
            self._up_info.setStyleSheet(self._TEXT_DIM)
            self._up_info.setText("TX: none")
        elif self._ble.connected:
            self._up_dot.setStyleSheet(self._DOT_GREEN)
            self._up_info.setStyleSheet(self._TEXT_OK)
            self._up_info.setText("%s: connected" % kind)
        else:
            self._up_dot.setStyleSheet(self._DOT_ORANGE)
            self._up_info.setStyleSheet(self._TEXT_WARN)
            self._up_info.setText("%s: scanning" % kind if kind != "HID" else "HID: waiting for gadget")

        # ── counters (status bar) ─────────────────────────────────────────────
        self._rx_label.setText("RX: %d" % self._rx_count)
        rx_b = self._bridge.rx_bytes_sent if self._bridge else 0
        self._rx_b_label.setText("→TCP: %d B" % rx_b)
        self._tx_label.setText("TX: %d" % self._tx_count)
        self._qr_label.setText("QR: %d" % self._qr_raw_count)
        frag = self._last_frag
        if frag is None:
            self._seq_label.setText("seq: —")
        else:
            seq, frag_idx, frag_total = frag
            if frag_total == 1:
                self._seq_label.setText("seq: %d" % seq)
            else:
                self._seq_label.setText("seq: %d  %d/%d" % (seq, frag_idx + 1, frag_total))
        self._frame_label.setText("frames: %d" % self._frame_count)


if __name__ == "__main__":
    from main import main
    main()
