"""
Wire-level framing shared between the browser JS and the Pi Zero KioskInterface.

Downlink (gateway → Pi Zero):
  QR payload = [seq_hi, seq_lo, *raw_packet]
  The browser HDLC-decodes the TCP stream to extract raw packets before
  packing them into QR codes.

Uplink (Pi Zero → gateway):
  HID frame = ">HEXHEX...CC<"
  HEXHEX = packet bytes as uppercase hex, CC = XOR checksum (2 hex chars).
  The browser HDLC-encodes the decoded packet before sending to WebSocket.
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
