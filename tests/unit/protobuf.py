"""Minimal protobuf-3 wire-format encoder/decoder.

This is the cross-check reference for the C++ ``DynamicMessage`` /
``DynamicReader`` in ``components/multicast_pubsub/dynamic_message.{h,cpp}``.
Only the subset of wire types ESPHome's encoder supports is implemented:

  * wire type 0: VARINT (bool, int32/64, uint32/64, sint32/64)
  * wire type 2: LENGTH_DELIMITED (string, bytes, embedded messages)
  * wire type 5: FIXED32 (float)

Wire type 1 (FIXED64: double / fixed64 / sfixed64) is intentionally
omitted to match upstream.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

WIRE_VARINT = 0
WIRE_LENGTH = 2
WIRE_FIXED32 = 5


def encode_varint(value: int) -> bytes:
    """Encode an unsigned 64-bit value as a protobuf varint."""
    if value < 0:
        # Negative int32 is encoded as 10-byte uint64 (matches ProtoEncode::encode_int32)
        value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        if value < 0x80:
            out.append(value)
            return bytes(out)
        out.append((value & 0x7F) | 0x80)
        value >>= 7


def encode_tag(field_id: int, wire: int) -> bytes:
    return encode_varint((field_id << 3) | wire)


def encode_zigzag32(value: int) -> int:
    return ((value << 1) ^ (value >> 31)) & 0xFFFFFFFF


def encode_zigzag64(value: int) -> int:
    return ((value << 1) ^ (value >> 63)) & 0xFFFFFFFFFFFFFFFF


def decode_zigzag32(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def decode_zigzag64(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def encode_field(field_id: int, wire: int, payload: bytes) -> bytes:
    return encode_tag(field_id, wire) + payload


def encode_int32(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(value))


def encode_uint32(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(value))


def encode_sint32(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(encode_zigzag32(value)))


def encode_int64(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(value))


def encode_uint64(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(value))


def encode_sint64(field_id: int, value: int) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(encode_zigzag64(value)))


def encode_bool(field_id: int, value: bool) -> bytes:
    return encode_field(field_id, WIRE_VARINT, encode_varint(1 if value else 0))


def encode_float(field_id: int, value: float) -> bytes:
    return encode_field(field_id, WIRE_FIXED32, struct.pack("<f", value))


def encode_string(field_id: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return encode_field(field_id, WIRE_LENGTH, encode_varint(len(raw)) + raw)


def encode_bytes(field_id: int, value: bytes) -> bytes:
    return encode_field(field_id, WIRE_LENGTH, encode_varint(len(value)) + value)


@dataclass
class Field:
    tag: int
    wire: int
    raw: object  # int for varint, bytes for length, int for fixed32


def parse_varint(buf: bytes, pos: int) -> tuple[int, int]:
    value = 0
    shift = 0
    start = pos
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return value, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")
    raise ValueError(f"truncated varint at pos {start}")


def decode(buf: bytes) -> list[Field]:
    """Walk a protobuf byte stream and return the list of fields."""
    out: list[Field] = []
    pos = 0
    while pos < len(buf):
        tag_full, pos = parse_varint(buf, pos)
        wire = tag_full & 0x7
        field_id = tag_full >> 3
        if wire == WIRE_VARINT:
            v, pos = parse_varint(buf, pos)
            out.append(Field(field_id, WIRE_VARINT, v))
        elif wire == WIRE_LENGTH:
            length, pos = parse_varint(buf, pos)
            if pos + length > len(buf):
                raise ValueError("length-delimited overflows buffer")
            out.append(Field(field_id, WIRE_LENGTH, buf[pos:pos + length]))
            pos += length
        elif wire == WIRE_FIXED32:
            if pos + 4 > len(buf):
                raise ValueError("fixed32 overflows buffer")
            v = struct.unpack("<I", buf[pos:pos + 4])[0]
            out.append(Field(field_id, WIRE_FIXED32, v))
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out
