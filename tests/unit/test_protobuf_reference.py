"""Unit tests for the Python protobuf reference (tests/unit/protobuf.py).

The reference is what cross-checks DynamicMessage round-trips against
hand-known wire-format bytes -- if these golden vectors drift, the
C++ DynamicMessage tests downstream will surface mismatches.
"""

from __future__ import annotations

import struct

import pytest

from protobuf import (
    WIRE_FIXED32,
    WIRE_LENGTH,
    WIRE_VARINT,
    decode,
    encode_bool,
    encode_bytes,
    encode_float,
    encode_int32,
    encode_sint32,
    encode_string,
    encode_uint32,
    encode_varint,
    parse_varint,
)


# ---------------------------------------------------------------------------
# Varint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, b"\x00"),
        (1, b"\x01"),
        (127, b"\x7f"),
        (128, b"\x80\x01"),
        (300, b"\xac\x02"),
        (16384, b"\x80\x80\x01"),
    ],
)
def test_encode_varint_golden(value: int, expected: bytes) -> None:
    assert encode_varint(value) == expected


def test_varint_round_trips() -> None:
    for v in [0, 1, 127, 128, 300, 16384, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF]:
        encoded = encode_varint(v)
        decoded, consumed = parse_varint(encoded, 0)
        assert decoded == v
        assert consumed == len(encoded)


def test_negative_int32_encodes_as_10_byte_uint64() -> None:
    # Matches ESPHome's encode_int32 behavior for negatives.
    encoded = encode_int32(1, -1)
    # tag (1 byte) + 10-byte varint of 0xFFFFFFFFFFFFFFFF
    assert len(encoded) == 11
    assert encoded[0] == (1 << 3) | WIRE_VARINT


# ---------------------------------------------------------------------------
# Single-field encode produces correct wire format (golden vectors)
# ---------------------------------------------------------------------------


def test_uint32_encoding_golden() -> None:
    # field=1, value=300 -> tag 0x08, varint 300 = 0xac 0x02
    assert encode_uint32(1, 300) == b"\x08\xac\x02"


def test_string_encoding_golden() -> None:
    # field=2, value="abc" -> tag 0x12, length 3, "abc"
    assert encode_string(2, "abc") == b"\x12\x03abc"


def test_float_encoding_golden() -> None:
    # field=3, value=1.0 -> tag 0x1d, IEEE754 1.0 LE
    assert encode_float(3, 1.0) == b"\x1d" + struct.pack("<f", 1.0)


def test_bool_encoding_golden() -> None:
    assert encode_bool(4, True) == b"\x20\x01"
    assert encode_bool(4, False) == b"\x20\x00"


def test_bytes_encoding_golden() -> None:
    assert encode_bytes(5, b"\xde\xad\xbe\xef") == b"\x2a\x04\xde\xad\xbe\xef"


def test_sint32_uses_zigzag() -> None:
    # field=1, -1 -> zigzag(−1) = 1 -> tag 0x08, varint 1
    assert encode_sint32(1, -1) == b"\x08\x01"
    # field=1, -2 -> zigzag(−2) = 3
    assert encode_sint32(1, -2) == b"\x08\x03"


# ---------------------------------------------------------------------------
# Multi-field + decode
# ---------------------------------------------------------------------------


def test_decode_walks_known_message() -> None:
    msg = encode_uint32(1, 42) + encode_string(2, "hi") + encode_float(3, 2.5)
    fields = decode(msg)
    assert len(fields) == 3
    assert fields[0].tag == 1 and fields[0].wire == WIRE_VARINT and fields[0].raw == 42
    assert fields[1].tag == 2 and fields[1].wire == WIRE_LENGTH and fields[1].raw == b"hi"
    assert fields[2].tag == 3 and fields[2].wire == WIRE_FIXED32
    assert struct.unpack("<f", struct.pack("<I", fields[2].raw))[0] == 2.5


def test_decode_rejects_truncated_varint() -> None:
    with pytest.raises(ValueError, match="truncated"):
        decode(b"\x80\x80")


def test_decode_rejects_length_overflow() -> None:
    with pytest.raises(ValueError, match="overflows"):
        decode(b"\x0a\x05ab")  # length-delim claims 5 bytes, only 2 present


def test_decode_rejects_unsupported_wire_type() -> None:
    # tag 1, wire type 1 (fixed64) - explicitly unsupported
    with pytest.raises(ValueError, match="wire type"):
        decode(b"\x09\x00\x00\x00\x00\x00\x00\x00\x00")
