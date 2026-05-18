#include "dynamic_message.h"

#ifdef USE_NETWORK

#include <cstring>

#include "esphome/components/api/proto.h"

namespace esphome::multicast_pubsub {

namespace {

// Worst-case sizes used to reserve buffer space before calling ProtoEncode.
// Tag-varint is at most 5 bytes for a 32-bit field id. Value varints are
// at most 10 bytes for a 64-bit value. Fixed32 is 4 bytes. Length-delimited
// payloads need 5 bytes of length-prefix plus the payload bytes.
constexpr size_t TAG_MAX = 5;
constexpr size_t VARINT_MAX = 10;
constexpr size_t LENGTH_PREFIX_MAX = 5;

// Resize the buffer up by `reserve_bytes`, run `encode` with a pos pointer
// into the reserved region, then shrink the buffer back to the actually-
// used size. This lets us reuse ESPHome's ProtoEncode primitives (which
// want a writable uint8_t* with bounds) on top of a growing std::vector.
template<typename Fn> void encode_into(std::vector<uint8_t> &buf, size_t reserve_bytes, Fn &&encode) {
  size_t base = buf.size();
  buf.resize(base + reserve_bytes);
  uint8_t *pos = buf.data() + base;
  [[maybe_unused]] uint8_t *proto_debug_end_ = buf.data() + buf.size();
  encode(pos);
  buf.resize(static_cast<size_t>(pos - buf.data()));
}

}  // namespace

DynamicMessage &DynamicMessage::add_bool(uint32_t tag, bool value) {
  encode_into(this->buf_, TAG_MAX + 1, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_bool(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_int32(uint32_t tag, int32_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_int32(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_int64(uint32_t tag, int64_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_int64(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_uint32(uint32_t tag, uint32_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_uint32(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_uint64(uint32_t tag, uint64_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_uint64(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_sint32(uint32_t tag, int32_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_sint32(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_sint64(uint32_t tag, int64_t value) {
  encode_into(this->buf_, TAG_MAX + VARINT_MAX, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_sint64(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_float(uint32_t tag, float value) {
  encode_into(this->buf_, TAG_MAX + 4, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_float(pos, tag, value, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_string(uint32_t tag, std::string_view value) {
  encode_into(this->buf_, TAG_MAX + LENGTH_PREFIX_MAX + value.size(), [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_string(pos, tag, value.data(), value.size(), /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_string(uint32_t tag, const char *value) {
  return this->add_string(tag, std::string_view(value));
}

DynamicMessage &DynamicMessage::add_bytes(uint32_t tag, const uint8_t *data, size_t len) {
  encode_into(this->buf_, TAG_MAX + LENGTH_PREFIX_MAX + len, [&](uint8_t *&pos) {
    esphome::api::ProtoEncode::encode_bytes(pos, tag, data, len, /*force=*/true);
  });
  return *this;
}

DynamicMessage &DynamicMessage::add_bytes(uint32_t tag, std::span<const uint8_t> data) {
  return this->add_bytes(tag, data.data(), data.size());
}

DynamicMessage &DynamicMessage::add_message(uint32_t tag, const DynamicMessage &nested) {
  // Embed as a length-delimited field whose payload is the nested message's
  // already-encoded bytes. encode_bytes does exactly that (tag + length +
  // raw bytes), so we don't reimplement it.
  return this->add_bytes(tag, nested.buf_.data(), nested.buf_.size());
}

// ---------------------------------------------------------------------------
// DynamicReader
// ---------------------------------------------------------------------------

std::optional<DynamicReader::Field> DynamicReader::next() {
  if (this->error_ || this->cur_ >= this->end_)
    return std::nullopt;

  // Parse the tag-varint (combined field_id and wire_type).
  auto tag_res = esphome::api::ProtoVarInt::parse(this->cur_, static_cast<uint32_t>(this->end_ - this->cur_));
  if (!tag_res.has_value()) {
    this->error_ = true;
    return std::nullopt;
  }
  this->cur_ += tag_res.consumed;
  uint64_t tag_full = tag_res.value;
  uint32_t wire = static_cast<uint32_t>(tag_full & 0x7);
  uint32_t field_id = static_cast<uint32_t>(tag_full >> 3);

  Field f{};
  f.tag = field_id;

  switch (wire) {
    case 0: {  // VARINT
      auto v = esphome::api::ProtoVarInt::parse(this->cur_, static_cast<uint32_t>(this->end_ - this->cur_));
      if (!v.has_value()) {
        this->error_ = true;
        return std::nullopt;
      }
      this->cur_ += v.consumed;
      f.wire_type = WireType::VARINT;
      f.raw_varint = static_cast<uint64_t>(v.value);
      return f;
    }
    case 2: {  // LENGTH_DELIMITED
      auto len = esphome::api::ProtoVarInt::parse(this->cur_, static_cast<uint32_t>(this->end_ - this->cur_));
      if (!len.has_value()) {
        this->error_ = true;
        return std::nullopt;
      }
      this->cur_ += len.consumed;
      uint64_t L = static_cast<uint64_t>(len.value);
      if (L > static_cast<uint64_t>(this->end_ - this->cur_)) {
        this->error_ = true;
        return std::nullopt;
      }
      f.wire_type = WireType::LENGTH_DELIMITED;
      f.raw_length = std::span<const uint8_t>(this->cur_, static_cast<size_t>(L));
      this->cur_ += L;
      return f;
    }
    case 5: {  // FIXED32
      if (this->end_ - this->cur_ < 4) {
        this->error_ = true;
        return std::nullopt;
      }
      f.wire_type = WireType::FIXED32;
      uint32_t v;
      std::memcpy(&v, this->cur_, 4);
      // Wire is little-endian; std::memcpy on an LE host is the identity.
      // Repeat the byte swap manually for portability against BE hosts
      // (ESP32, ESP8266 are LE so this is a no-op there).
#if __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
      v = ((v & 0x000000FFu) << 24) | ((v & 0x0000FF00u) << 8) | ((v & 0x00FF0000u) >> 8) |
          ((v & 0xFF000000u) >> 24);
#endif
      f.raw_fixed32 = v;
      this->cur_ += 4;
      return f;
    }
    default:
      // wire type 1 (FIXED64) and reserved values (3, 4, 6, 7) -- not
      // supported. We mark the stream as errored rather than guess.
      this->error_ = true;
      return std::nullopt;
  }
}

bool DynamicReader::Field::as_bool(bool *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = this->raw_varint != 0;
  return true;
}

bool DynamicReader::Field::as_int32(int32_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = static_cast<int32_t>(static_cast<uint32_t>(this->raw_varint));
  return true;
}

bool DynamicReader::Field::as_int64(int64_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = static_cast<int64_t>(this->raw_varint);
  return true;
}

bool DynamicReader::Field::as_uint32(uint32_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = static_cast<uint32_t>(this->raw_varint);
  return true;
}

bool DynamicReader::Field::as_uint64(uint64_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = this->raw_varint;
  return true;
}

bool DynamicReader::Field::as_sint32(int32_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = esphome::api::decode_zigzag32(static_cast<uint32_t>(this->raw_varint));
  return true;
}

bool DynamicReader::Field::as_sint64(int64_t *out) const {
  if (this->wire_type != WireType::VARINT)
    return false;
  *out = esphome::api::decode_zigzag64(this->raw_varint);
  return true;
}

bool DynamicReader::Field::as_float(float *out) const {
  if (this->wire_type != WireType::FIXED32)
    return false;
  std::memcpy(out, &this->raw_fixed32, 4);
  return true;
}

bool DynamicReader::Field::as_string(std::string_view *out) const {
  if (this->wire_type != WireType::LENGTH_DELIMITED)
    return false;
  *out = std::string_view(reinterpret_cast<const char *>(this->raw_length.data()), this->raw_length.size());
  return true;
}

bool DynamicReader::Field::as_bytes(std::span<const uint8_t> *out) const {
  if (this->wire_type != WireType::LENGTH_DELIMITED)
    return false;
  *out = this->raw_length;
  return true;
}

bool DynamicReader::Field::as_message(DynamicReader *out) const {
  if (this->wire_type != WireType::LENGTH_DELIMITED)
    return false;
  *out = DynamicReader(this->raw_length);
  return true;
}

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
