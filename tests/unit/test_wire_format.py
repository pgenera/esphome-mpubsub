"""Wire-format unit tests against the Python reference implementation."""

from __future__ import annotations

import pytest

from reference import (
    FLAG_RETAIN_HINT,
    FLAG_TEXT,
    HEADER_LEN,
    MAGIC,
    MAX_PAYLOAD,
    RESERVED_FLAG_MASK,
    VERSION,
    WireError,
    decode,
    encode,
    topic_crc32,
)


def test_header_is_exactly_twelve_bytes() -> None:
    pkt = encode("foo", b"")
    assert len(pkt) == HEADER_LEN


def test_header_layout() -> None:
    pkt = encode("hello", b"world")
    assert pkt[0:2] == MAGIC
    assert pkt[2] == VERSION
    assert pkt[3] == 0  # no flags
    # CRC is little-endian
    crc = int.from_bytes(pkt[4:8], "little")
    assert crc == topic_crc32("hello")
    payload_len = int.from_bytes(pkt[8:10], "little")
    assert payload_len == 5
    assert pkt[10:12] == b"\x00\x00"
    assert pkt[12:] == b"world"


def test_roundtrip() -> None:
    payload = b"\x01\x02\x03\xff\xfe"
    pkt = encode("topic/x", payload, flags=FLAG_TEXT)
    crc, flags, body = decode(pkt)
    assert crc == topic_crc32("topic/x")
    assert flags == FLAG_TEXT
    assert body == payload


def test_max_payload() -> None:
    pkt = encode("t", b"x" * MAX_PAYLOAD)
    assert len(pkt) == HEADER_LEN + MAX_PAYLOAD
    decode(pkt)  # must not raise


def test_oversize_payload_rejected() -> None:
    with pytest.raises(ValueError, match="payload too large"):
        encode("t", b"x" * (MAX_PAYLOAD + 1))


def test_oversize_payload_rejected_far_above_limit() -> None:
    with pytest.raises(ValueError, match="payload too large"):
        encode("t", b"x" * 65535)


def test_retain_hint_flag() -> None:
    pkt = encode("t", b"", flags=FLAG_RETAIN_HINT)
    _, flags, _ = decode(pkt)
    assert flags & FLAG_RETAIN_HINT


def test_reserved_flag_rejected_in_encode() -> None:
    with pytest.raises(ValueError):
        encode("t", b"", flags=0x04)


def test_reserved_flag_rejected_in_decode() -> None:
    pkt = bytearray(encode("t", b""))
    pkt[3] = 0x80  # set a reserved bit
    with pytest.raises(WireError):
        decode(bytes(pkt))


def test_short_datagram_rejected() -> None:
    with pytest.raises(WireError):
        decode(b"MP\x01\x00")


def test_bad_magic_rejected() -> None:
    pkt = bytearray(encode("t", b""))
    pkt[0] = ord("X")
    with pytest.raises(WireError, match="bad magic"):
        decode(bytes(pkt))


def test_bad_version_rejected() -> None:
    pkt = bytearray(encode("t", b""))
    pkt[2] = 99
    with pytest.raises(WireError, match="unsupported version"):
        decode(bytes(pkt))


def test_length_mismatch_rejected() -> None:
    pkt = bytearray(encode("t", b"hello"))
    # corrupt the payload length so the recorded value disagrees with reality
    pkt[8] = 99
    with pytest.raises(WireError, match="length mismatch"):
        decode(bytes(pkt))


def test_reserved_bytes_ignored_on_decode() -> None:
    pkt = bytearray(encode("t", b"hi"))
    pkt[10] = 0xAB
    pkt[11] = 0xCD
    crc, flags, body = decode(bytes(pkt))
    assert body == b"hi"
