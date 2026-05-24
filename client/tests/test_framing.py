"""Unit tests for framing.py — no I/O, no async."""
import pytest
from framing import (
    hdlc_encode, hdlc_decode_stream,
    hid_encode, hid_decode,
    qr_pack, qr_unpack, qr_fragments, MAX_QR_CHUNK,
)


class TestHdlc:
    def test_round_trip_basic(self):
        data = b"\x01\x02\x03\x04"
        assert hdlc_decode_stream(hdlc_encode(data)) == [data]

    def test_escapes_flag_byte(self):
        data = bytes([0x7E])
        encoded = hdlc_encode(data)
        assert 0x7E not in encoded[1:-1]
        assert hdlc_decode_stream(encoded) == [data]

    def test_escapes_escape_byte(self):
        data = bytes([0x7D])
        assert hdlc_decode_stream(hdlc_encode(data)) == [data]

    def test_mixed_escape_bytes(self):
        data = bytes([0x7E, 0x7D, 0x7E, 0x7D])
        assert hdlc_decode_stream(hdlc_encode(data)) == [data]

    def test_multi_frame_stream(self):
        a, b = b"\xAA\xBB", b"\xCC\xDD\xEE"
        stream = hdlc_encode(a) + hdlc_encode(b)
        assert hdlc_decode_stream(stream) == [a, b]

    def test_consecutive_flags_produce_no_frame(self):
        assert hdlc_decode_stream(bytes([0x7E, 0x7E, 0x7E])) == []

    def test_all_byte_values(self):
        data = bytes(range(256))
        assert hdlc_decode_stream(hdlc_encode(data)) == [data]

    def test_partial_stream_no_closing_flag_yields_nothing(self):
        partial = bytes([0x7E, 0x01, 0x02])
        assert hdlc_decode_stream(partial) == []


class TestHid:
    def test_round_trip(self):
        data = b"\x01\x02\x03"
        assert hid_decode(hid_encode(data)) == data

    def test_crc_tamper_rejected(self):
        frame = hid_encode(b"\x01\x02\x03")
        tampered = frame[:-3] + "FF<"
        assert hid_decode(tampered) is None

    def test_odd_length_hex_rejected(self):
        assert hid_decode(">ABC<") is None

    def test_missing_delimiters_rejected(self):
        assert hid_decode("010200") is None

    def test_only_crc_no_data_rejected(self):
        assert hid_decode(">00<") is None

    def test_case_insensitive(self):
        data = b"\xDE\xAD"
        assert hid_decode(hid_encode(data).lower()) == data

    def test_non_hex_chars_stripped(self):
        assert hid_decode(">01 02 03<") == b"\x01\x02"

    def test_all_byte_values(self):
        data = bytes(range(256))
        assert hid_decode(hid_encode(data)) == data

    def test_single_zero_byte(self):
        assert hid_decode(hid_encode(b"\x00")) == b"\x00"

    def test_crc_is_xor_of_bytes(self):
        data = b"\x01\x02\x03"
        frame = hid_encode(data)
        expected_crc = 0x01 ^ 0x02 ^ 0x03
        assert frame == f">{data.hex().upper()}{expected_crc:02X}<"


class TestQrPack:
    def test_round_trip(self):
        seq, pkt = 0x1234, b"\xAB\xCD\xEF"
        assert qr_unpack(qr_pack(seq, pkt)) == (seq, pkt, 0, 1)

    def test_seq_zero(self):
        assert qr_unpack(qr_pack(0, b"\xFF")) == (0, b"\xFF", 0, 1)

    def test_seq_max(self):
        assert qr_unpack(qr_pack(0xFFFF, b"\x00")) == (0xFFFF, b"\x00", 0, 1)

    def test_empty_packet(self):
        assert qr_unpack(qr_pack(1, b"")) == (1, b"", 0, 1)

    def test_too_short_raises(self):
        with pytest.raises(ValueError):
            qr_unpack(b"\x00\x01\x02")  # only 3 bytes, need at least 4

    def test_payload_is_four_bytes_plus_packet(self):
        packed = qr_pack(0xABCD, b"\x01\x02")
        assert packed == bytes([0xAB, 0xCD, 0x01, 0x00, 0x01, 0x02])

    def test_fragment_fields_round_trip(self):
        packed = qr_pack(0x0001, b"hi", frag_idx=2, frag_total=5)
        assert qr_unpack(packed) == (0x0001, b"hi", 2, 5)


class TestQrFragments:
    def test_single_fragment_for_small_packet(self):
        pkt = b"x" * 10
        frags = qr_fragments(7, pkt)
        assert len(frags) == 1
        assert qr_unpack(frags[0]) == (7, pkt, 0, 1)

    def test_splits_at_chunk_boundary(self):
        pkt = b"a" * (MAX_QR_CHUNK + 1)
        frags = qr_fragments(1, pkt)
        assert len(frags) == 2
        _, chunk0, idx0, total0 = qr_unpack(frags[0])
        _, chunk1, idx1, total1 = qr_unpack(frags[1])
        assert total0 == total1 == 2
        assert idx0 == 0 and idx1 == 1
        assert chunk0 + chunk1 == pkt

    def test_reassembly_round_trip(self):
        pkt = bytes(range(200))
        frags = qr_fragments(42, pkt)
        reassembled = b"".join(qr_unpack(f)[1] for f in sorted(frags, key=lambda f: qr_unpack(f)[2]))
        assert reassembled == pkt

    def test_empty_packet_yields_one_fragment(self):
        frags = qr_fragments(0, b"")
        assert len(frags) == 1
        assert qr_unpack(frags[0]) == (0, b"", 0, 1)


class TestHidReportFraming:
    """Fast-path: 64-byte vendor HID reports carrying HDLC byte slices."""

    def test_pack_unpack_round_trip(self):
        from framing import hid_report_pack, hid_report_unpack, HID_REPORT_SIZE
        payload = b"hello world"
        report = hid_report_pack(payload)
        assert len(report) == HID_REPORT_SIZE
        assert report[0] == len(payload)
        assert hid_report_unpack(report) == payload

    def test_pack_zero_length(self):
        from framing import hid_report_pack, hid_report_unpack
        report = hid_report_pack(b"")
        assert report[0] == 0
        assert hid_report_unpack(report) == b""

    def test_pack_max_length(self):
        from framing import hid_report_pack, hid_report_unpack, HID_REPORT_PAYLOAD_MAX
        payload = bytes(range(HID_REPORT_PAYLOAD_MAX))
        report = hid_report_pack(payload)
        assert hid_report_unpack(report) == payload

    def test_pack_overflow_rejected(self):
        from framing import hid_report_pack, HID_REPORT_PAYLOAD_MAX
        import pytest
        with pytest.raises(ValueError):
            hid_report_pack(bytes(HID_REPORT_PAYLOAD_MAX + 1))

    def test_unpack_tolerates_malformed_length(self):
        from framing import hid_report_unpack, HID_REPORT_PAYLOAD_MAX
        # Length byte larger than payload area — should clamp, not crash.
        bad = bytes([255]) + bytes(63)
        out = hid_report_unpack(bad)
        assert len(out) == HID_REPORT_PAYLOAD_MAX

    def test_chunks_split_evenly(self):
        from framing import hid_report_chunks, HID_REPORT_SIZE, HID_REPORT_PAYLOAD_MAX
        # 200 bytes / 63 per report = 4 reports (63 + 63 + 63 + 11)
        chunks = hid_report_chunks(b"x" * 200)
        assert len(chunks) == 4
        for c in chunks:
            assert len(c) == HID_REPORT_SIZE
        assert chunks[0][0] == HID_REPORT_PAYLOAD_MAX
        assert chunks[-1][0] == 200 - 3 * HID_REPORT_PAYLOAD_MAX

    def test_chunks_empty_input(self):
        from framing import hid_report_chunks
        assert hid_report_chunks(b"") == []

    def test_stream_round_trip_via_reports(self):
        """A byte stream sliced into reports and concatenated back must match."""
        from framing import hid_report_chunks, hid_report_unpack
        original = bytes(range(256)) * 3   # 768 bytes spanning many reports
        reports = hid_report_chunks(original)
        rejoined = b"".join(hid_report_unpack(r) for r in reports)
        assert rejoined == original
