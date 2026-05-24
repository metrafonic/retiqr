"""
Wire-level framing shared between the browser JS and the client app.

Two transport profiles share this file:

Legacy / fallback path (camera + keyboard, ~3 kB/s down, ~500 B/s up):
  Downlink (gateway → client):
    QR payload = [seq_hi, seq_lo, frag_total, frag_idx, *chunk]
    Browser HDLC-decodes the TCP stream first, then packs each packet into
    one or more QR fragments of <= MAX_QR_CHUNK data bytes.
  Uplink (client → gateway):
    HID frame = ">HEXHEX...CC<"
    HEXHEX = packet bytes as uppercase hex, CC = XOR checksum (2 hex chars).
    Browser HDLC-encodes the decoded packet before sending on WebSocket.

WebHID fast path (vendor-HID, full-duplex):
  Both directions tunnel raw HDLC bytes through fixed-size 63-byte HID
  report bodies (1 length byte + <=62 payload bytes). No re-framing — the
  gateway-side bytes are already HDLC because that's what Reticulum's TCP
  interface speaks. See hid_report_pack/unpack.
"""

_FLAG = 0x7E
_ESC  = 0x7D


def _xor_checksum(data: bytes) -> int:
    crc = 0
    for b in data:
        crc ^= b
    return crc


def hdlc_encode(data: bytes) -> bytes:
    out = bytearray([_FLAG])
    for b in data:
        if b in (_FLAG, _ESC):
            out.append(_ESC)
            out.append(b ^ 0x20)
        else:
            out.append(b)
    out.append(_FLAG)
    return bytes(out)


def hdlc_decode_stream(stream: bytes) -> list[bytes]:
    packets: list[bytes] = []
    buf: list[int] = []
    esc = False
    for b in stream:
        if b == _FLAG:
            if buf:
                packets.append(bytes(buf))
            buf = []
            esc = False
        elif b == _ESC:
            esc = True
        else:
            buf.append(b ^ 0x20 if esc else b)
            esc = False
    return packets


def hid_encode(data: bytes) -> str:
    return f">{data.hex().upper()}{_xor_checksum(data):02X}<"


def hid_decode(frame: str) -> bytes | None:
    if not (frame.startswith('>') and frame.endswith('<')):
        return None
    hex_str = ''.join(c for c in frame[1:-1] if c in '0123456789ABCDEFabcdef')
    if len(hex_str) < 4 or len(hex_str) % 2 != 0:
        return None
    data = bytes.fromhex(hex_str[:-2])
    return data if _xor_checksum(data) == int(hex_str[-2:], 16) else None


MAX_QR_CHUNK = 80  # max packet bytes per QR frame


# ─── WebHID fast-path report framing ─────────────────────────────────────────
#
# The vendor-HID interface on the dongle exposes a fixed-size 63-byte report
# body in each direction. We use it as a length-prefixed envelope for raw
# HDLC bytes — chunking a variable-length HDLC byte stream into fixed reports
# without re-framing.
#
#   Report layout (63 bytes):
#     byte 0      payload length n, 0..HID_REPORT_PAYLOAD_MAX
#     bytes 1..n  payload bytes (a slice of the HDLC byte stream)
#     bytes n+1..62  ignored (zero-padded by the sender)
#
# The receiver concatenates payload bytes from successive reports back into
# an HDLC byte stream, which is then framed by the existing HDLC parser
# (gateway side: piped into the WebSocket; client side: written to Reticulum
# TCP socket as-is, since it's already HDLC-framed).

HID_REPORT_SIZE         = 63   # Data bytes per report at the descriptor
                               # level. The TinyUSB HID endpoint buffer
                               # is 64 bytes and includes the prepended
                               # Report ID byte, so the descriptor carries
                               # at most 63 data bytes per report.
HID_REPORT_PAYLOAD_MAX  = HID_REPORT_SIZE - 1   # 1 byte reserved for length


def hid_report_pack(payload: bytes) -> bytes:
    """Pack up to HID_REPORT_PAYLOAD_MAX bytes into a fixed-size HID report.

    Returns exactly HID_REPORT_SIZE bytes, zero-padded.
    """
    if len(payload) > HID_REPORT_PAYLOAD_MAX:
        raise ValueError(
            f"HID report payload too large: {len(payload)} > {HID_REPORT_PAYLOAD_MAX}"
        )
    out = bytearray(HID_REPORT_SIZE)
    out[0] = len(payload)
    out[1:1 + len(payload)] = payload
    return bytes(out)


def hid_report_unpack(report: bytes) -> bytes:
    """Extract the payload bytes from a fixed-size HID report.

    Returns 0..HID_REPORT_PAYLOAD_MAX bytes. Tolerant of oversized inputs
    (some platforms include the report-id byte; some don't).
    """
    if not report:
        return b""
    n = report[0]
    if n > HID_REPORT_PAYLOAD_MAX:
        # Malformed length — clamp to known max to avoid out-of-bounds read,
        # rely on the HDLC parser downstream to discard garbage between flags.
        n = HID_REPORT_PAYLOAD_MAX
    end = min(1 + n, len(report))
    return bytes(report[1:end])


def hid_report_chunks(stream: bytes) -> list[bytes]:
    """Split a byte stream into a list of HID reports, each HID_REPORT_SIZE bytes.

    Empty input produces no reports (an empty stream needs no transmission).
    """
    if not stream:
        return []
    return [
        hid_report_pack(stream[i:i + HID_REPORT_PAYLOAD_MAX])
        for i in range(0, len(stream), HID_REPORT_PAYLOAD_MAX)
    ]


def qr_pack(seq: int, packet: bytes, frag_idx: int = 0, frag_total: int = 1) -> bytes:
    """Pack a packet fragment into a QR payload.

    Format: [seq_hi][seq_lo][frag_total][frag_idx][...data]
    Single-fragment (unfragmented) packets use frag_total=1, frag_idx=0.
    """
    return bytes([(seq >> 8) & 0xFF, seq & 0xFF, frag_total & 0xFF, frag_idx & 0xFF]) + packet


def qr_unpack(payload: bytes) -> tuple[int, bytes, int, int]:
    """Unpack a QR payload. Returns (seq, data, frag_idx, frag_total)."""
    if len(payload) < 4:
        raise ValueError(f"QR payload too short ({len(payload)} bytes)")
    seq = (payload[0] << 8) | payload[1]
    return seq, payload[4:], payload[3], payload[2]


def qr_fragments(seq: int, packet: bytes) -> list[bytes]:
    """Split packet into QR payloads, each carrying at most MAX_QR_CHUNK bytes."""
    if not packet:
        return [qr_pack(seq, b"", 0, 1)]
    chunks = [packet[i:i + MAX_QR_CHUNK] for i in range(0, len(packet), MAX_QR_CHUNK)]
    return [qr_pack(seq, chunk, idx, len(chunks)) for idx, chunk in enumerate(chunks)]
