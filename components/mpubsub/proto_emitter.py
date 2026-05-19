"""Codegen for typed mpubsub messages.

Given a YAML ``messages:`` entry, this module emits:

* The canonical schema string (used both for ``SCHEMA_ID`` computation and
  as a comment in the generated header so deploys can diff schemas).
* The 16-bit ``SCHEMA_ID`` (low 16 bits of CRC-32/IEEE of the canonical
  string -- same hash family as ``TOPIC_CRC32``).
* The C++ source for a struct, ``encode_to(uint8_t *out, size_t max_len)``,
  and ``decode_from(const uint8_t *data, size_t len)``.

The generated code uses ``esphome::api::ProtoEncode`` (from
``components/api/proto.h``) for encoding and ``esphome::api::ProtoVarInt``
for the decode-side varint parsing.

Pure-Python module: no ESPHome runtime dependency, so the unit tests can
exercise it directly.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass


# Type catalog. Each entry carries:
#   cpp        -- the C++ type for the struct member
#   wire       -- protobuf wire-type (0=varint, 2=length-delim, 5=fixed32)
#   encoder    -- the ProtoEncode::encode_* method name
#   default    -- C++ initializer for the struct member
#   setter_arg -- the C++ type used in the by-value setter on Call
#                 (e.g. `const std::string &` so callers can pass literals,
#                 `std::vector<uint8_t>` for bytes since the Call takes ownership)
#   call_category -- "scalar" | "string" | "bytes" -- decides which setter
#                 overloads the Call emits.
#
# Wire type 1 (64-bit fixed: double / fixed64 / sfixed64) is intentionally
# unsupported -- matches ESPHome's own protobuf encoder; see proto.h.
TYPE_INFO = {
    "bool":   {"cpp": "bool",        "wire": 0, "encoder": "encode_bool",   "default": "false",
               "setter_arg": "bool",     "call_category": "scalar"},
    "int32":  {"cpp": "int32_t",     "wire": 0, "encoder": "encode_int32",  "default": "0",
               "setter_arg": "int32_t",  "call_category": "scalar"},
    "int64":  {"cpp": "int64_t",     "wire": 0, "encoder": "encode_int64",  "default": "0",
               "setter_arg": "int64_t",  "call_category": "scalar"},
    "uint32": {"cpp": "uint32_t",    "wire": 0, "encoder": "encode_uint32", "default": "0",
               "setter_arg": "uint32_t", "call_category": "scalar"},
    "uint64": {"cpp": "uint64_t",    "wire": 0, "encoder": "encode_uint64", "default": "0",
               "setter_arg": "uint64_t", "call_category": "scalar"},
    "sint32": {"cpp": "int32_t",     "wire": 0, "encoder": "encode_sint32", "default": "0",
               "setter_arg": "int32_t",  "call_category": "scalar"},
    "sint64": {"cpp": "int64_t",     "wire": 0, "encoder": "encode_sint64", "default": "0",
               "setter_arg": "int64_t",  "call_category": "scalar"},
    "float":  {"cpp": "float",       "wire": 5, "encoder": "encode_float",  "default": "0.0f",
               "setter_arg": "float",    "call_category": "scalar"},
    "string": {"cpp": "std::string", "wire": 2, "encoder": "encode_string", "default": '""',
               "setter_arg": "const std::string &", "call_category": "string"},
    "bytes":  {"cpp": "std::vector<uint8_t>", "wire": 2, "encoder": "encode_bytes", "default": "{}",
               "setter_arg": "std::vector<uint8_t>", "call_category": "bytes"},
}

VALID_TYPES = frozenset(TYPE_INFO.keys())


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    tag: int
    repeated: bool = False


@dataclass(frozen=True)
class Message:
    id: str
    fields: tuple[Field, ...]


def canonical_schema_string(msg: Message) -> str:
    """Return the canonical form of a schema, used to compute SCHEMA_ID.

    Format: fields sorted by tag, each rendered as ``<tag>:<type>:<name>``
    with a ``repeated `` prefix on the type for list fields, lines joined
    with ``\\n``, no trailing newline.

    The canonical form is independent of YAML key order or field declaration
    order, so two devices declaring the same schema in different orderings
    still compute the same SCHEMA_ID. Changing a field from singular to
    repeated (or vice versa) changes the SCHEMA_ID, since the wire format
    differs.
    """
    def _typestr(f: Field) -> str:
        return f"repeated {f.type}" if f.repeated else f.type

    lines = sorted(
        f"{f.tag}:{_typestr(f)}:{f.name}" for f in msg.fields
    )
    return "\n".join(lines)


def schema_id(msg: Message) -> int:
    """Compute the 16-bit SCHEMA_ID for a message.

    Implementation: low 16 bits of CRC-32/IEEE of the UTF-8 canonical
    schema string. Reuses the same CRC family as ``TOPIC_CRC32`` so the
    component carries only one hash family.
    """
    canonical = canonical_schema_string(msg).encode("utf-8")
    return zlib.crc32(canonical) & 0xFFFF


def validate(msg: Message) -> None:
    """Raise ``ValueError`` if the message is malformed."""
    if not msg.id:
        raise ValueError("message id must be non-empty")
    if not msg.fields:
        raise ValueError(f"message {msg.id!r} must have at least one field")

    seen_tags: set[int] = set()
    seen_names: set[str] = set()
    for f in msg.fields:
        if not (1 <= f.tag <= 536_870_911):  # 2^29 - 1, proto3 reserved range
            raise ValueError(
                f"field {f.name!r} in message {msg.id!r}: tag {f.tag} out of range (1..536870911)"
            )
        if 19000 <= f.tag <= 19999:
            raise ValueError(
                f"field {f.name!r}: tag {f.tag} is in proto3's reserved 19000-19999 range"
            )
        if f.tag in seen_tags:
            raise ValueError(
                f"message {msg.id!r}: duplicate tag {f.tag} (field {f.name!r})"
            )
        seen_tags.add(f.tag)
        if f.name in seen_names:
            raise ValueError(f"message {msg.id!r}: duplicate field name {f.name!r}")
        seen_names.add(f.name)
        if f.type not in VALID_TYPES:
            raise ValueError(
                f"field {f.name!r} in message {msg.id!r}: unknown type {f.type!r} "
                f"(valid: {sorted(VALID_TYPES)})"
            )


def _emit_member(f: Field) -> str:
    info = TYPE_INFO[f.type]
    if f.repeated:
        # Repeated fields are always std::vector<CppType>, default-empty.
        return f"  std::vector<{info['cpp']}> {f.name};"
    return f"  {info['cpp']} {f.name}{{{info['default']}}};"


def _emit_encode_call(f: Field) -> str:
    info = TYPE_INFO[f.type]
    if f.repeated:
        # Unpacked repeated encoding: emit one tag+value pair per element.
        # `force=true` so zero/empty elements are still written -- repeated
        # fields preserve all elements, not just non-default ones.
        if f.type == "bytes":
            return (
                f"    for (const auto &v : this->{f.name}) {{\n"
                f"      esphome::api::ProtoEncode::encode_bytes("
                f"pos PROTO_ENCODE_DEBUG_ARG, {f.tag}, v.data(), v.size(), true);\n"
                f"    }}"
            )
        return (
            f"    for (const auto &v : this->{f.name}) {{\n"
            f"      esphome::api::ProtoEncode::{info['encoder']}("
            f"pos PROTO_ENCODE_DEBUG_ARG, {f.tag}, v, true);\n"
            f"    }}"
        )
    if f.type == "bytes":
        # encode_bytes takes (pos, field_id, const uint8_t*, size_t, force) --
        # no std::vector overload, so expand the vector here.
        return (
            f"    esphome::api::ProtoEncode::encode_bytes("
            f"pos PROTO_ENCODE_DEBUG_ARG, {f.tag}, "
            f"this->{f.name}.data(), this->{f.name}.size());"
        )
    return (
        f"    esphome::api::ProtoEncode::{info['encoder']}("
        f"pos PROTO_ENCODE_DEBUG_ARG, {f.tag}, this->{f.name});"
    )


def _emit_decode_case(f: Field) -> str:
    """Emit a `case <tag>:` block inside one of the decode_* overrides.

    Different wire types feed into different override methods; this returns
    a tuple of (override_name, case_body) wrapped as a string so the caller
    can group them.
    """
    raise NotImplementedError("use _emit_decode_overrides instead")


def _emit_decode_overrides(msg: Message) -> str:
    """Emit the three decode_* override methods (varint / length / 32bit).

    Each override is a switch on field_id. Unknown fields return false,
    which causes ProtoDecodableMessage to skip them gracefully.

    Repeated fields append (push_back); singular fields assign.
    """
    varint_cases: list[str] = []
    length_cases: list[str] = []
    fixed32_cases: list[str] = []
    for f in msg.fields:
        info = TYPE_INFO[f.type]
        # Compute the inner expression that converts the wire value to the
        # field's C++ element type. Common to both singular and repeated.
        if info["wire"] == 0:
            if f.type == "bool":
                expr = "value != 0"
            elif f.type == "sint32":
                expr = "esphome::api::decode_zigzag32(static_cast<uint32_t>(value))"
            elif f.type == "sint64":
                expr = "esphome::api::decode_zigzag64(static_cast<uint64_t>(value))"
            elif f.type in ("int32", "uint32", "int64", "uint64"):
                expr = f"static_cast<{info['cpp']}>(value)"
            else:
                raise AssertionError(f"unhandled varint type {f.type}")
        elif info["wire"] == 5:
            expr = "value.as_float()" if f.type == "float" else "value.as_fixed32()"
        elif info["wire"] == 2:
            if f.type == "string":
                expr = "std::string(reinterpret_cast<const char *>(value.data()), value.size())"
            elif f.type == "bytes":
                expr = "std::vector<uint8_t>(value.data(), value.data() + value.size())"
            else:
                raise AssertionError(f"unhandled length-delim type {f.type}")
        else:
            raise AssertionError(f"unsupported wire type {info['wire']}")

        # Repeated fields push_back; singular fields assign.
        if f.repeated:
            case = f"      case {f.tag}: this->{f.name}.push_back({expr}); return true;"
        else:
            case = f"      case {f.tag}: this->{f.name} = {expr}; return true;"

        if info["wire"] == 0:
            varint_cases.append(case)
        elif info["wire"] == 5:
            fixed32_cases.append(case)
        elif info["wire"] == 2:
            length_cases.append(case)

    def _block(cases: list[str], method: str, arg_type: str) -> str:
        if not cases:
            return ""
        joined = "\n".join(cases)
        return (
            f"  bool {method}(uint32_t field_id, {arg_type} value) override {{\n"
            f"    switch (field_id) {{\n{joined}\n      default: return false;\n    }}\n"
            f"  }}\n"
        )

    parts = [
        _block(varint_cases, "decode_varint", "esphome::api::proto_varint_value_t"),
        _block(length_cases, "decode_length", "esphome::api::ProtoLengthDelimited"),
        _block(fixed32_cases, "decode_32bit", "esphome::api::Proto32Bit"),
    ]
    return "\n".join(p for p in parts if p)


def _emit_call_setters(msg: Message, struct_name: str) -> str:
    """Emit the fluent set_<field>() setter chain for the Call class.

    Mirrors esphome::light::LightCall's API shape: every setter takes the
    value by appropriate const ref / value, also has an optional<T>
    overload for callers that want conditional sets, and returns *this so
    multiple sets can be chained. perform() is what actually publishes.
    """
    lines: list[str] = []
    for f in msg.fields:
        info = TYPE_INFO[f.type]
        arg = info["setter_arg"]
        category = info["call_category"]
        cpp = info["cpp"]
        n = f.name
        if f.repeated:
            # Repeated fields use proto-style add_X (push one) plus the
            # bulk set_X(vector) / clear_X(). No optional<T> overload --
            # "is this list set" doesn't fit the repeated semantic.
            lines.append(
                f"    Call &add_{n}({arg} value) {{ this->msg_.{n}.push_back(value); return *this; }}"
            )
            if category == "string":
                lines.append(
                    f"    Call &add_{n}(const char *value) {{ "
                    f"this->msg_.{n}.emplace_back(value); return *this; }}"
                )
            if category == "bytes":
                lines.append(
                    f"    Call &add_{n}(const uint8_t *data, size_t len) {{ "
                    f"this->msg_.{n}.emplace_back(data, data + len); return *this; }}"
                )
                lines.append(
                    f"    Call &add_{n}(std::span<const uint8_t> data) {{ "
                    f"this->msg_.{n}.emplace_back(data.begin(), data.end()); return *this; }}"
                )
            lines.append(
                f"    Call &set_{n}(std::vector<{cpp}> values) {{ "
                f"this->msg_.{n} = std::move(values); return *this; }}"
            )
            lines.append(
                f"    Call &clear_{n}() {{ this->msg_.{n}.clear(); return *this; }}"
            )
            continue
        # Singular fields ----------------------------------------------------
        if category == "scalar":
            opt = f"esphome::optional<{info['cpp']}>"
            lines.append(
                f"    Call &set_{n}({arg} value) {{ this->msg_.{n} = value; return *this; }}"
            )
            lines.append(
                f"    Call &set_{n}({opt} value) {{ "
                f"if (value.has_value()) this->msg_.{n} = *value; return *this; }}"
            )
        elif category == "string":
            opt = "esphome::optional<std::string>"
            lines.append(
                f"    Call &set_{n}({arg} value) {{ this->msg_.{n} = value; return *this; }}"
            )
            lines.append(
                f"    Call &set_{n}(const char *value) {{ this->msg_.{n} = value; return *this; }}"
            )
            lines.append(
                f"    Call &set_{n}({opt} value) {{ "
                f"if (value.has_value()) this->msg_.{n} = *value; return *this; }}"
            )
        elif category == "bytes":
            lines.append(
                f"    Call &set_{n}({arg} value) {{ this->msg_.{n} = std::move(value); return *this; }}"
            )
            lines.append(
                f"    Call &set_{n}(const uint8_t *data, size_t len) {{ "
                f"this->msg_.{n}.assign(data, data + len); return *this; }}"
            )
            lines.append(
                f"    Call &set_{n}(std::span<const uint8_t> data) {{ "
                f"this->msg_.{n}.assign(data.begin(), data.end()); return *this; }}"
            )
        else:
            raise AssertionError(f"unknown call_category {category!r}")
    return "\n".join(lines)


def _action_value_type(f: Field) -> str:
    """The C++ type a Publish<Msg>Action's TEMPLATABLE_VALUE uses for a field.

    For repeated fields we expose the whole std::vector<T> as one templatable
    so users pass a single lambda returning the full list. This keeps the
    YAML schema simple at the cost of dynamic-length flexibility (which
    the lambda body can still do).
    """
    info = TYPE_INFO[f.type]
    if f.repeated:
        return f"std::vector<{info['cpp']}>"
    return info["cpp"]


def _emit_action_setters(msg: Message) -> str:
    return "\n".join(
        f"  TEMPLATABLE_VALUE({_action_value_type(f)}, {f.name})"
        for f in msg.fields
    )


def _emit_action_assignments(msg: Message) -> str:
    return "\n".join(
        f"    m.{f.name} = this->{f.name}_.value(x...);"
        for f in msg.fields
    )


def emit_struct(msg: Message) -> str:
    """Return the full C++ struct definition for a message, plus the
    matching ``On<Msg>Trigger`` class.

    The emitted code is dropped into ``main.cpp`` at file scope via
    ``cg.add_global(cg.RawExpression(...))`` from the component's
    ``to_code``. It depends on ``esphome/components/api/proto.h`` for the
    encoder/decoder primitives and on ``multicast_pubsub.h`` for the
    typed subscribe API and the ``MulticastPubSub`` parent type used by
    the nested ``Call`` builder.
    """
    validate(msg)
    members = "\n".join(_emit_member(f) for f in msg.fields)
    encode_calls = "\n".join(_emit_encode_call(f) for f in msg.fields)
    decode_overrides = _emit_decode_overrides(msg)
    call_setters = _emit_call_setters(msg, pascal_case(msg.id))
    action_setters = _emit_action_setters(msg)
    action_assignments = _emit_action_assignments(msg)
    canonical = canonical_schema_string(msg).replace("\\", "\\\\").replace("\n", "\\n")
    sid = schema_id(msg)
    struct_name = pascal_case(msg.id)
    return f"""
// ----- Generated from YAML messages: entry '{msg.id}' (do not edit) -----
// Canonical schema: "{canonical}"
namespace esphome::multicast_pubsub::messages {{

struct {struct_name} : public esphome::api::ProtoDecodableMessage {{
  static constexpr uint16_t SCHEMA_ID = 0x{sid:04x};
  static constexpr const char *SCHEMA_NAME = "{msg.id}";

{members}

  // Encode into pre-allocated buffer. Returns number of bytes written.
  // Caller must ensure `out` has space for at least the encoded size.
  size_t encode_to(uint8_t *out, size_t max_len) const {{
    uint8_t *pos = out;
    [[maybe_unused]] uint8_t *proto_debug_end_ = out + max_len;
{encode_calls}
    return static_cast<size_t>(pos - out);
  }}

{decode_overrides}

  // Forward declaration for the fluent builder; defined out-of-class
  // below so its `{struct_name} msg_` member can be a value member
  // (incomplete-type rule forbids that inside the enclosing class body).
  class Call;
}};

// Fluent builder, modeled after esphome::light::LightCall. Bind a
// parent + topic at construction, chain set_<field>() calls (each
// returns *this), then perform() to encode and publish.
//
// Example:
//   {struct_name}::Call(id(pubsub), "home/garage/climate")
//       .set_temperature(22.5f)
//       .set_room_id("garage")
//       .perform();
//
// Equivalent via the templated factory on MulticastPubSub:
//   id(pubsub)->make_call<{struct_name}>("home/garage/climate")
//       .set_temperature(22.5f).set_room_id("garage").perform();
class {struct_name}::Call {{
 public:
  Call(esphome::multicast_pubsub::MulticastPubSub *parent, std::string topic)
      : parent_(parent), topic_(std::move(topic)) {{}}

{call_setters}

  /// Direct access to the underlying message -- escape hatch for
  /// repeated-field push_back, conditional assembly, or anything the
  /// fluent setters don't cover.
  {struct_name} &message() {{ return this->msg_; }}
  const {struct_name} &message() const {{ return this->msg_; }}

  /// Set/replace the destination topic.
  Call &set_topic(std::string topic) {{ this->topic_ = std::move(topic); return *this; }}
  const std::string &topic() const {{ return this->topic_; }}

  /// Encode and publish. Returns false on socket error or oversize payload
  /// (see MulticastPubSub::publish for diagnostics).
  bool perform();

 protected:
  esphome::multicast_pubsub::MulticastPubSub *parent_;
  std::string topic_;
  {struct_name} msg_;
}};

}}  // namespace esphome::multicast_pubsub::messages

namespace esphome::multicast_pubsub {{

// Typed trigger class for `on_message: + message: {msg.id}`. The argument
// passed into the user lambda is the fully-decoded struct, not raw bytes.
class On{struct_name}Trigger : public esphome::Trigger<esphome::multicast_pubsub::messages::{struct_name}> {{
 public:
  On{struct_name}Trigger(MulticastPubSub *parent, const std::string &topic) {{
    parent->subscribe_typed<esphome::multicast_pubsub::messages::{struct_name}>(
        topic, [this](const esphome::multicast_pubsub::messages::{struct_name} &m) {{
          this->trigger(m);
        }});
  }}
}};

}}  // namespace esphome::multicast_pubsub

// perform() is defined here so it can call MulticastPubSub::publish<T>
// (whose definition lives in multicast_pubsub.h and is included before this).
inline bool esphome::multicast_pubsub::messages::{struct_name}::Call::perform() {{
  return this->parent_->publish(this->topic_, this->msg_);
}}

namespace esphome::multicast_pubsub {{

// Codegen-generated YAML action class for `mpubsub.publish:
// + message: {msg.id}`. One TEMPLATABLE_VALUE per declared field
// (std::vector<T> for repeated fields). Python codegen calls
// set_<field>(...) on the templated values it has, leaves the rest at
// default. play() always assigns every field -- proto3 default-omission
// keeps the wire size correct for unset fields.
template<typename... Ts> class Publish{struct_name}Action
    : public esphome::Action<Ts...>, public esphome::Parented<MulticastPubSub> {{
 public:
  TEMPLATABLE_VALUE(std::string, topic)
  TEMPLATABLE_VALUE(int16_t, retransmit_count)
{action_setters}

  void play(const Ts &...x) override {{
    esphome::multicast_pubsub::messages::{struct_name} m;
{action_assignments}
    // See PublishAction::play in automation.h for the save/restore rationale.
    if (this->retransmit_count_.has_value()) {{
      int16_t saved = this->parent_->get_retransmit_count();
      this->parent_->set_retransmit_count(this->retransmit_count_.value(x...));
      this->parent_->publish(this->topic_.value(x...), m);
      this->parent_->set_retransmit_count(saved);
    }} else {{
      this->parent_->publish(this->topic_.value(x...), m);
    }}
  }}
}};

}}  // namespace esphome::multicast_pubsub
"""


def pascal_case(name: str) -> str:
    """Convert a snake_case or kebab-case id to PascalCase for the C++ type."""
    return "".join(part[:1].upper() + part[1:] for part in name.replace("-", "_").split("_") if part)


# Backwards-compatible alias used in some tests.
_to_pascal_case = pascal_case
