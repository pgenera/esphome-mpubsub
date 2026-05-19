#include "topic_hash.h"

#include <cstdio>

#include "sha256.h"

namespace esphome::multicast_pubsub {

GroupAddr topic_to_group(std::string_view topic, Scope scope) {
  uint8_t digest[Sha256::DIGEST_SIZE];
  Sha256::hash(reinterpret_cast<const uint8_t *>(topic.data()), topic.size(), digest);
  GroupAddr addr{};
  addr[0] = 0xFF;
  addr[1] = static_cast<uint8_t>((0x1U << 4) | (static_cast<uint8_t>(scope) & 0x0F));
  for (size_t i = 0; i < 14; ++i)
    addr[2 + i] = digest[i];
  return addr;
}

// CRC-32/IEEE (poly 0xEDB88320, reflected). Computes the same value as
// zlib.crc32 and esphome::crc32.
uint32_t topic_crc32(std::string_view topic) {
  uint32_t crc = 0xFFFFFFFFU;
  for (unsigned char c : topic) {
    crc ^= c;
    for (int i = 0; i < 8; ++i) {
      uint32_t mask = -(crc & 1U);
      crc = (crc >> 1) ^ (0xEDB88320U & mask);
    }
  }
  return ~crc;
}

size_t group_to_string(const GroupAddr &addr, char *out, size_t out_len) {
  // Render as eight uppercase-free colon-separated hextets; this is the
  // RFC 4291 canonical *un*compressed form. RFC 5952 :: compression is not
  // applied here -- callers that need it (logging) can normalize separately.
  uint16_t hextets[8];
  for (size_t i = 0; i < 8; ++i)
    hextets[i] = (uint16_t(addr[i * 2]) << 8) | uint16_t(addr[i * 2 + 1]);
  int n = std::snprintf(out, out_len, "%x:%x:%x:%x:%x:%x:%x:%x", hextets[0], hextets[1], hextets[2], hextets[3],
                        hextets[4], hextets[5], hextets[6], hextets[7]);
  return n < 0 ? 0 : static_cast<size_t>(n);
}

}  // namespace esphome::multicast_pubsub
