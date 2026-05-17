// Encoding and decoding of the v1 on-wire packet format.
//
// Header (12 bytes, little-endian multi-byte fields):
//
//   byte:  0    1    2    3    4    5    6    7    8    9   10   11
//         +----+----+----+----+----+----+----+----+----+----+----+----+
//         | 'M'| 'P'| VER| FLG|        TOPIC_CRC32         | PAY_LEN  |
//         +----+----+----+----+----+----+----+----+----+----+----+----+
//
// See ../../README.md for the full specification and matching Python
// reference in tests/unit/reference.py.

#pragma once

#include <cstddef>
#include <cstdint>
#include <span>

namespace esphome::multicast_pubsub {

constexpr uint8_t MAGIC0 = 'M';
constexpr uint8_t MAGIC1 = 'P';
constexpr uint8_t VERSION = 0x01;
constexpr size_t HEADER_LEN = 12;
constexpr size_t MAX_DATAGRAM = 508;
constexpr size_t MAX_PAYLOAD = MAX_DATAGRAM - HEADER_LEN;

enum WireFlag : uint8_t {
  FLAG_TEXT = 0x01,
  FLAG_RETAIN_HINT = 0x02,
  // 0xFC reserved.
};
constexpr uint8_t RESERVED_FLAG_MASK = 0xFC;

enum class DecodeError : uint8_t {
  OK = 0,
  TOO_SHORT,
  BAD_MAGIC,
  BAD_VERSION,
  RESERVED_FLAGS,
  LENGTH_MISMATCH,
};

struct DecodedPacket {
  uint32_t topic_crc;
  uint8_t flags;
  // View into the caller-provided buffer. Valid as long as the buffer is.
  std::span<const uint8_t> payload;
};

// Write the header for a topic + payload of length `payload_len` to `out`.
// Returns the number of bytes written (always HEADER_LEN). The payload bytes
// must be appended by the caller. `flags` must not set any reserved bits.
size_t encode_header(uint32_t topic_crc, uint8_t flags, uint16_t payload_len, uint8_t out[HEADER_LEN]);

// Parse `data` (a full datagram). Returns OK and fills `*out` on success,
// otherwise leaves `*out` untouched and returns the specific failure reason.
DecodeError decode(std::span<const uint8_t> data, DecodedPacket *out);

}  // namespace esphome::multicast_pubsub
