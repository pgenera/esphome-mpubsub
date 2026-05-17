#include "wire_format.h"

namespace esphome::multicast_pubsub {

size_t encode_header(uint32_t topic_crc, uint8_t flags, uint16_t payload_len, uint8_t out[HEADER_LEN]) {
  out[0] = MAGIC0;
  out[1] = MAGIC1;
  out[2] = VERSION;
  out[3] = flags;
  out[4] = uint8_t(topic_crc);
  out[5] = uint8_t(topic_crc >> 8);
  out[6] = uint8_t(topic_crc >> 16);
  out[7] = uint8_t(topic_crc >> 24);
  out[8] = uint8_t(payload_len);
  out[9] = uint8_t(payload_len >> 8);
  out[10] = 0;  // reserved / future use
  out[11] = 0;
  return HEADER_LEN;
}

DecodeError decode(std::span<const uint8_t> data, DecodedPacket *out) {
  if (data.size() < HEADER_LEN)
    return DecodeError::TOO_SHORT;
  if (data[0] != MAGIC0 || data[1] != MAGIC1)
    return DecodeError::BAD_MAGIC;
  if (data[2] != VERSION)
    return DecodeError::BAD_VERSION;
  uint8_t flags = data[3];
  if (flags & RESERVED_FLAG_MASK)
    return DecodeError::RESERVED_FLAGS;
  uint32_t crc = uint32_t(data[4]) | (uint32_t(data[5]) << 8) | (uint32_t(data[6]) << 16) | (uint32_t(data[7]) << 24);
  uint16_t payload_len = uint16_t(data[8]) | (uint16_t(data[9]) << 8);
  if (HEADER_LEN + payload_len != data.size())
    return DecodeError::LENGTH_MISMATCH;
  out->topic_crc = crc;
  out->flags = flags;
  out->payload = data.subspan(HEADER_LEN);
  return DecodeError::OK;
}

}  // namespace esphome::multicast_pubsub
