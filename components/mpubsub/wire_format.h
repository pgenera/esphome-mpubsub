// Encoding and decoding of the on-wire packet format.
//
// Header (12 bytes, little-endian multi-byte fields):
//
//   byte:  0    1    2    3    4    5    6    7    8    9   10   11
//         +----+----+----+----+----+----+----+----+----+----+----+----+
//         | 'M'| 'P'| VER| ENC|        TOPIC_CRC32         | PAY_LEN  | RSV
//         +----+----+----+----+----+----+----+----+----+----+----+----+
//
// Byte 3 (ENCODING) tells the receiver how to parse the body:
//   0x00 = RAW       -- opaque bytes
//   0x01 = PROTOBUF  -- body starts with a 2-byte SCHEMA_ID (LE),
//                       then protobuf-encoded bytes (see docs/TYPED_MESSAGES_PLAN.md)
//   0x02..0xFF       -- reserved, receivers MUST drop
//
// See ../../docs/PROTOCOL.md for the full specification and matching
// Python reference in tests/unit/reference.py.

#pragma once

#include <cstddef>
#include <cstdint>
#include <span>

namespace esphome::multicast_pubsub {

constexpr uint8_t MAGIC0 = 'M';
constexpr uint8_t MAGIC1 = 'P';
constexpr uint8_t VERSION = 0x01;
constexpr size_t HEADER_LEN = 12;
// IPv6 minimum MTU (1280, RFC 8200 §5) minus the 40-byte IPv6 header and
// 8-byte UDP header = 1232 bytes of UDP payload guaranteed deliverable on
// any RFC-compliant IPv6 link without fragmentation.
constexpr size_t MAX_DATAGRAM = 1232;
constexpr size_t MAX_PAYLOAD = MAX_DATAGRAM - HEADER_LEN;  // 1220

enum class Encoding : uint8_t {
  RAW = 0x00,
  PROTOBUF = 0x01,
  // 0x02..0xFF reserved (e.g. future compression flavors).
};

constexpr bool is_known_encoding(uint8_t value) {
  return value == static_cast<uint8_t>(Encoding::RAW) || value == static_cast<uint8_t>(Encoding::PROTOBUF);
}

enum class DecodeError : uint8_t {
  OK = 0,
  TOO_SHORT,
  BAD_MAGIC,
  BAD_VERSION,
  UNKNOWN_ENCODING,
  LENGTH_MISMATCH,
};

struct DecodedPacket {
  uint32_t topic_crc;
  Encoding encoding;
  // View into the caller-provided buffer. Valid as long as the buffer is.
  std::span<const uint8_t> payload;
};

// Write the header for a topic + payload of length `payload_len` to `out`.
// Returns the number of bytes written (always HEADER_LEN). The payload bytes
// must be appended by the caller.
size_t encode_header(uint32_t topic_crc, Encoding encoding, uint16_t payload_len, uint8_t out[HEADER_LEN]);

// Parse `data` (a full datagram). Returns OK and fills `*out` on success,
// otherwise leaves `*out` untouched and returns the specific failure reason.
DecodeError decode(std::span<const uint8_t> data, DecodedPacket *out);

}  // namespace esphome::multicast_pubsub
