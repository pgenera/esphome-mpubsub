#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace esphome::multicast_pubsub {

// IPv6 multicast scope nibbles per RFC 4291 §2.7.
enum class Scope : uint8_t {
  LINK_LOCAL = 0x2,
  SITE_LOCAL = 0x5,
  ORG_LOCAL = 0x8,
};

// 128-bit IPv6 address in network byte order (byte 0 first).
using GroupAddr = std::array<uint8_t, 16>;

// Derive the IPv6 multicast group address for a topic. Layout:
//   byte 0      : 0xFF
//   byte 1 high : 0x1 (transient flag)
//   byte 1 low  : scope nibble
//   bytes 2..15 : first 14 bytes of SHA-256(utf8 topic)
GroupAddr topic_to_group(std::string_view topic, Scope scope);

// CRC-32/IEEE of the UTF-8 topic. Independent of ESPHome's crc32() helper so
// this file builds in the host unit-test harness without the full esphome
// build, and so the algorithm is locked to the wire spec.
uint32_t topic_crc32(std::string_view topic);

// Format a GroupAddr into the canonical RFC 5952 textual form. `out` must be
// at least 40 bytes (enough for full address + trailing NUL).
size_t group_to_string(const GroupAddr &addr, char *out, size_t out_len);

}  // namespace esphome::multicast_pubsub
