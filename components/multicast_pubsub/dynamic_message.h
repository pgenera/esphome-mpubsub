// Schemaless runtime protobuf builder and reader.
//
// These exist for cases where the message shape is decided at runtime
// rather than compile time -- bridges from other protocols, variable-shape
// payloads, debugging, and forwarders. For statically-known schemas the
// codegen-generated `T::Call` builder is far more ergonomic.
//
// Both classes are built on the protobuf primitives in
// `esphome/components/api/proto.h` (the same library the codegen-emitted
// structs use), so wire output is bit-for-bit compatible.

#pragma once

#include "esphome/core/defines.h"
#ifdef USE_NETWORK

#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace esphome::multicast_pubsub {

class DynamicMessage {
 public:
  DynamicMessage() = default;

  // Singular adds. Each call appends one `tag-varint || value` pair to the
  // running buffer. Returns *this for chaining. `force=true` is used
  // internally so zero/empty values still get written; repeated invocations
  // with the same tag produce a proto3 "repeated" field automatically.
  DynamicMessage &add_bool(uint32_t tag, bool value);
  DynamicMessage &add_int32(uint32_t tag, int32_t value);
  DynamicMessage &add_int64(uint32_t tag, int64_t value);
  DynamicMessage &add_uint32(uint32_t tag, uint32_t value);
  DynamicMessage &add_uint64(uint32_t tag, uint64_t value);
  DynamicMessage &add_sint32(uint32_t tag, int32_t value);
  DynamicMessage &add_sint64(uint32_t tag, int64_t value);
  DynamicMessage &add_float(uint32_t tag, float value);
  DynamicMessage &add_string(uint32_t tag, std::string_view value);
  DynamicMessage &add_string(uint32_t tag, const char *value);
  DynamicMessage &add_bytes(uint32_t tag, const uint8_t *data, size_t len);
  DynamicMessage &add_bytes(uint32_t tag, std::span<const uint8_t> data);

  /// Embed a nested message at `tag`. The nested message is encoded as a
  /// length-delimited field whose payload is its own protobuf bytes.
  DynamicMessage &add_message(uint32_t tag, const DynamicMessage &nested);

  /// Read-only view of the encoded protobuf body.
  std::span<const uint8_t> bytes() const { return {this->buf_.data(), this->buf_.size()}; }
  size_t size() const { return this->buf_.size(); }

  /// Reset to empty so the same instance can be reused for the next message
  /// (avoids reallocation overhead on hot paths).
  void clear() { this->buf_.clear(); }

 protected:
  // Reserve worst-case space at the tail, encode, then resize down to the
  // actual bytes written by ProtoEncode. Shared helper that powers every
  // add_*() method.
  void encode_into_(size_t worst_case, void (*encode_fn)(uint8_t *&pos, void *ctx), void *ctx);

  std::vector<uint8_t> buf_;
};

/// Walks tag/wire-type/value triples in a protobuf byte stream. The reader
/// is single-pass; call ``next()`` until it returns ``std::nullopt``.
///
/// Unknown wire types and truncated varints set ``error()`` and stop the
/// stream. Callers that care about strict validity should check ``error()``
/// after the loop.
class DynamicReader {
 public:
  enum class WireType : uint8_t {
    VARINT = 0,
    LENGTH_DELIMITED = 2,
    FIXED32 = 5,
    // Wire type 1 (64-bit fixed) not supported -- matches ESPHome's encoder.
  };

  struct Field {
    uint32_t tag{0};
    WireType wire_type{WireType::VARINT};

    // Raw values populated by the reader; use the typed accessors below
    // rather than reaching in here directly. Storage for the three
    // supported wire types is unioned semantically (one of these is valid
    // depending on wire_type).
    uint64_t raw_varint{0};
    uint32_t raw_fixed32{0};
    std::span<const uint8_t> raw_length;

    // Typed accessors. Each returns false (without modifying *out) when
    // the wire type doesn't match the requested interpretation. Same set
    // of types as the encoder side, mapped through the same conversions
    // (zigzag for sint32/64, fixed32 → float, length-delim → string/bytes/
    // nested message).
    bool as_bool(bool *out) const;
    bool as_int32(int32_t *out) const;
    bool as_int64(int64_t *out) const;
    bool as_uint32(uint32_t *out) const;
    bool as_uint64(uint64_t *out) const;
    bool as_sint32(int32_t *out) const;
    bool as_sint64(int64_t *out) const;
    bool as_float(float *out) const;
    bool as_string(std::string_view *out) const;
    bool as_bytes(std::span<const uint8_t> *out) const;
    /// Returns a reader that walks the nested message's fields.
    bool as_message(DynamicReader *out) const;
  };

  explicit DynamicReader(std::span<const uint8_t> data)
      : cur_(data.data()), end_(data.data() + data.size()) {}

  /// Returns the next field, or std::nullopt at end-of-stream or on error.
  /// Check ``error()`` after a nullopt to distinguish clean EOF from a
  /// malformed input.
  std::optional<Field> next();

  bool error() const { return this->error_; }
  bool at_end() const { return this->cur_ >= this->end_; }

 protected:
  const uint8_t *cur_;
  const uint8_t *end_;
  bool error_{false};
};

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
