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
COMPONENT = HERE.parents[1] / "components" / "mpubsub"
sys.path.insert(0, str(COMPONENT))

from proto_emitter import (  # noqa: E402
    Field,
    Message,
    TYPE_INFO,
    canonical_schema_string,
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


# ---------------------------------------------------------------------------
# Fluent Call builder (mirrors esphome::light::LightCall)
# ---------------------------------------------------------------------------


def test_emits_nested_call_class() -> None:
    src = _struct(("v", "int32", 1), id_="room_climate")
    # Nested forward declaration inside the struct...
    assert "class Call;" in src
    # ...and the out-of-class definition (qualified name) below it.
    assert "class RoomClimate::Call {" in src
    assert "Call(esphome::multicast_pubsub::MulticastPubSub *parent, std::string topic)" in src
    assert "bool perform();" in src
    assert "RoomClimate &message()" in src
    assert "const RoomClimate &message() const" in src
    assert "Call &set_topic(std::string topic)" in src


def test_call_setters_are_fluent_for_scalars() -> None:
    src = _struct(("temperature", "float", 1))
    # By-value setter returns Call&
    assert "Call &set_temperature(float value)" in src
    assert "this->msg_.temperature = value; return *this;" in src
    # optional<T> overload
    assert "Call &set_temperature(esphome::optional<float> value)" in src
    assert "if (value.has_value()) this->msg_.temperature = *value;" in src


def test_call_setters_for_string_include_const_char_overload() -> None:
    src = _struct(("name", "string", 1))
    assert "Call &set_name(const std::string & value)" in src
    assert "Call &set_name(const char *value)" in src
    assert "Call &set_name(esphome::optional<std::string> value)" in src


def test_call_setters_for_bytes_have_three_overloads() -> None:
    src = _struct(("blob", "bytes", 1))
    assert "Call &set_blob(std::vector<uint8_t> value)" in src
    assert "Call &set_blob(const uint8_t *data, size_t len)" in src
    assert "Call &set_blob(std::span<const uint8_t> data)" in src


def test_call_perform_publishes_via_parent_template() -> None:
    src = _struct(("v", "int32", 1), id_="room_climate")
    # Out-of-line perform definition uses publish<T>(topic, msg)
    assert "::Call::perform()" in src
    assert "this->parent_->publish(this->topic_, this->msg_)" in src


# ---------------------------------------------------------------------------
# Repeated fields
# ---------------------------------------------------------------------------


def _struct_with_repeated(name: str, type_name: str, tag: int = 1) -> str:
    msg = Message(id="m", fields=(Field(name, type_name, tag, repeated=True),))
    return emit_struct(msg)


def test_repeated_member_uses_vector_of_cpp_type() -> None:
    src = _struct_with_repeated("values", "float")
    assert "std::vector<float> values;" in src
    # Default-empty -- no {{default}} initializer.
    assert "std::vector<float> values{" not in src


def test_repeated_encode_emits_loop_with_force_true() -> None:
    src = _struct_with_repeated("values", "float", tag=3)
    # for loop over the field, force=true so zero elements still get written
    assert "for (const auto &v : this->values)" in src
    assert "encode_float(pos PROTO_ENCODE_DEBUG_ARG, 3, v, true);" in src


def test_repeated_encode_for_bytes_calls_data_size() -> None:
    src = _struct_with_repeated("blobs", "bytes", tag=4)
    assert "for (const auto &v : this->blobs)" in src
    assert "encode_bytes(pos PROTO_ENCODE_DEBUG_ARG, 4, v.data(), v.size(), true);" in src


def test_repeated_decode_appends_via_push_back() -> None:
    src = _struct_with_repeated("values", "int32", tag=5)
    assert "case 5: this->values.push_back(" in src


def test_repeated_call_has_add_set_clear() -> None:
    src = _struct_with_repeated("values", "float")
    assert "Call &add_values(float value)" in src
    assert "this->msg_.values.push_back(value);" in src
    assert "Call &set_values(std::vector<float> values)" in src
    assert "this->msg_.values = std::move(values);" in src
    assert "Call &clear_values()" in src
    assert "this->msg_.values.clear();" in src
    # No optional<T> overload for repeated fields.
    assert "esphome::optional<float>" not in src


def test_repeated_string_call_includes_const_char_add() -> None:
    src = _struct_with_repeated("names", "string")
    assert "Call &add_names(const std::string & value)" in src
    assert "Call &add_names(const char *value)" in src
    assert "this->msg_.names.emplace_back(value);" in src


def test_repeated_bytes_call_includes_ptr_and_span_overloads() -> None:
    src = _struct_with_repeated("blobs", "bytes")
    assert "Call &add_blobs(std::vector<uint8_t> value)" in src
    assert "Call &add_blobs(const uint8_t *data, size_t len)" in src
    assert "Call &add_blobs(std::span<const uint8_t> data)" in src


def test_repeated_changes_schema_id() -> None:
    from proto_emitter import schema_id
    a = Message(id="m", fields=(Field("v", "float", 1, repeated=False),))
    b = Message(id="m", fields=(Field("v", "float", 1, repeated=True),))
    assert schema_id(a) != schema_id(b)


def test_repeated_appears_in_canonical_form() -> None:
    msg = Message(id="m", fields=(Field("values", "float", 1, repeated=True),))
    assert canonical_schema_string(msg) == "1:repeated float:values"


# ---------------------------------------------------------------------------
# Publish<Msg>Action codegen
# ---------------------------------------------------------------------------


def test_emits_publish_action_class() -> None:
    src = _struct(("temperature", "float", 1), ("room_id", "string", 2), id_="room_climate")
    # Templated class deriving from Action + Parented
    assert (
        "class PublishRoomClimateAction"
        in src
    )
    assert "public esphome::Action<Ts...>, public esphome::Parented<MulticastPubSub>" in src
    # Topic + per-field TEMPLATABLE_VALUEs
    assert "TEMPLATABLE_VALUE(std::string, topic)" in src
    assert "TEMPLATABLE_VALUE(float, temperature)" in src
    assert "TEMPLATABLE_VALUE(std::string, room_id)" in src
    # play() builds the struct, assigns every field, publishes
    assert "void play(const Ts &...x) override" in src
    assert "m.temperature = this->temperature_.value(x...);" in src
    assert "m.room_id = this->room_id_.value(x...);" in src
    assert "this->parent_->publish(this->topic_.value(x...), m);" in src


def test_publish_action_uses_vector_for_repeated_fields() -> None:
    msg = Message(
        id="door_events",
        fields=(Field(name="open_at", type="uint32", tag=1, repeated=True),),
    )
    src = emit_struct(msg)
    # Repeated -> TEMPLATABLE_VALUE wraps the whole std::vector<T>
    assert "TEMPLATABLE_VALUE(std::vector<uint32_t>, open_at)" in src
    assert "m.open_at = this->open_at_.value(x...);" in src


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
