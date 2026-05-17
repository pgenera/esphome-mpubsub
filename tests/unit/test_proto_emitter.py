"""Tests for the C++ code emitter in proto_emitter.emit_struct.

We don't compile the emitted code in this file (that's covered by the
host-platform integration test that actually builds a YAML with a
``messages:`` block). Here we string-match the output to assert that
the emitter produced what we expect for every scalar type and edge case.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
COMPONENT = HERE.parents[1] / "components" / "multicast_pubsub"
sys.path.insert(0, str(COMPONENT))

from proto_emitter import (  # noqa: E402
    Field,
    Message,
    TYPE_INFO,
    emit_struct,
    schema_id,
)


def _struct(*fields: tuple[str, str, int], id_: str = "test_msg") -> str:
    msg = Message(id=id_, fields=tuple(Field(*f) for f in fields))
    return emit_struct(msg)


# ---------------------------------------------------------------------------
# Skeleton
# ---------------------------------------------------------------------------


def test_emits_namespace_and_class_name() -> None:
    src = _struct(("v", "int32", 1), id_="room_climate")
    assert "namespace esphome::multicast_pubsub::messages" in src
    assert "struct RoomClimate :" in src
    assert "public esphome::api::ProtoDecodableMessage" in src


def test_emits_schema_id_constant_matching_computation() -> None:
    msg = Message(
        id="m",
        fields=(Field("temperature", "float", 1), Field("humidity", "float", 2)),
    )
    src = emit_struct(msg)
    assert f"static constexpr uint16_t SCHEMA_ID = 0x{schema_id(msg):04x};" in src


def test_emits_schema_name_constant() -> None:
    src = _struct(("v", "int32", 1), id_="my_msg")
    assert 'static constexpr const char *SCHEMA_NAME = "my_msg";' in src


def test_emits_canonical_string_as_comment() -> None:
    src = _struct(("temperature", "float", 1), ("humidity", "float", 2))
    assert "// Canonical schema:" in src
    assert "1:float:temperature\\n2:float:humidity" in src


# ---------------------------------------------------------------------------
# Member declarations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(TYPE_INFO))
def test_emits_member_for_each_type(type_name: str) -> None:
    src = _struct(("field", type_name, 1))
    cpp_type = TYPE_INFO[type_name]["cpp"]
    default = TYPE_INFO[type_name]["default"]
    expected = f"{cpp_type} field{{{default}}};"
    assert expected in src, f"missing '{expected}' in:\n{src}"


# ---------------------------------------------------------------------------
# Encode calls
# ---------------------------------------------------------------------------


def test_emits_encode_to_method_with_expected_calls() -> None:
    src = _struct(
        ("temperature", "float", 1),
        ("humidity", "float", 2),
        ("room_id", "string", 3),
    )
    assert "size_t encode_to(uint8_t *out, size_t max_len) const" in src
    assert "esphome::api::ProtoEncode::encode_float(pos PROTO_ENCODE_DEBUG_ARG, 1, this->temperature);" in src
    assert "esphome::api::ProtoEncode::encode_float(pos PROTO_ENCODE_DEBUG_ARG, 2, this->humidity);" in src
    assert "esphome::api::ProtoEncode::encode_string(pos PROTO_ENCODE_DEBUG_ARG, 3, this->room_id);" in src
    assert "return static_cast<size_t>(pos - out);" in src


def test_emits_correct_encoder_per_type() -> None:
    # bytes has a different signature (no std::vector overload upstream)
    # so it's checked separately below.
    cases = [
        ("bool",   "encode_bool"),
        ("int32",  "encode_int32"),
        ("int64",  "encode_int64"),
        ("uint32", "encode_uint32"),
        ("uint64", "encode_uint64"),
        ("sint32", "encode_sint32"),
        ("sint64", "encode_sint64"),
        ("float",  "encode_float"),
        ("string", "encode_string"),
    ]
    for type_name, encoder in cases:
        src = _struct(("f", type_name, 7))
        expected = f"esphome::api::ProtoEncode::{encoder}(pos PROTO_ENCODE_DEBUG_ARG, 7, this->f);"
        assert expected in src, f"{type_name}: missing '{expected}' in:\n{src}"


def test_emits_expanded_call_for_bytes() -> None:
    src = _struct(("blob", "bytes", 4))
    assert (
        "esphome::api::ProtoEncode::encode_bytes(pos PROTO_ENCODE_DEBUG_ARG, 4, "
        "this->blob.data(), this->blob.size());" in src
    )


# ---------------------------------------------------------------------------
# Decode overrides
# ---------------------------------------------------------------------------


def test_emits_decode_varint_for_varint_field() -> None:
    src = _struct(("v", "int32", 1))
    assert "decode_varint(uint32_t field_id, esphome::api::proto_varint_value_t value)" in src
    assert "case 1: this->v = static_cast<int32_t>(value); return true;" in src


def test_emits_decode_32bit_for_float_field() -> None:
    src = _struct(("temperature", "float", 2))
    assert "decode_32bit(uint32_t field_id, esphome::api::Proto32Bit value)" in src
    assert "case 2: this->temperature = value.as_float(); return true;" in src


def test_emits_decode_length_for_string_field() -> None:
    src = _struct(("name", "string", 3))
    assert "decode_length(uint32_t field_id, esphome::api::ProtoLengthDelimited value)" in src
    assert "this->name = std::string(reinterpret_cast<const char *>(value.data()), value.size());" in src


def test_emits_decode_length_for_bytes_field() -> None:
    src = _struct(("blob", "bytes", 4))
    assert "this->blob = std::vector<uint8_t>(value.data(), value.data() + value.size());" in src


def test_sint32_uses_zigzag_decode() -> None:
    src = _struct(("v", "sint32", 1))
    assert "esphome::api::decode_zigzag32" in src


def test_sint64_uses_zigzag_decode() -> None:
    src = _struct(("v", "sint64", 1))
    assert "esphome::api::decode_zigzag64" in src


def test_bool_decode_compares_to_zero() -> None:
    src = _struct(("flag", "bool", 1))
    assert "this->flag = value != 0;" in src


def test_decode_methods_omitted_for_unused_wire_types() -> None:
    # A message with only varint fields shouldn't emit decode_length / decode_32bit
    src = _struct(("a", "int32", 1), ("b", "int32", 2))
    assert "decode_varint" in src
    assert "decode_length" not in src
    assert "decode_32bit" not in src


# ---------------------------------------------------------------------------
# Mixed-wire-type kitchen sink
# ---------------------------------------------------------------------------


def test_kitchen_sink_message() -> None:
    src = _struct(
        ("flag", "bool", 1),
        ("counter", "uint32", 2),
        ("delta", "sint32", 3),
        ("temperature", "float", 4),
        ("name", "string", 5),
        ("payload", "bytes", 6),
        ("big", "uint64", 7),
        id_="kitchen_sink",
    )
    assert "struct KitchenSink" in src
    # All three decode methods present
    assert "decode_varint" in src
    assert "decode_length" in src
    assert "decode_32bit" in src
    # Members declared with right C++ type
    assert "bool flag{false};" in src
    assert "uint32_t counter{0};" in src
    assert "int32_t delta{0};" in src
    assert "float temperature{0.0f};" in src
    assert 'std::string name{""};' in src
    assert "std::vector<uint8_t> payload{{}};" in src
    assert "uint64_t big{0};" in src
