// Tiny CLI that prints `topic_to_group` and `topic_crc32` results so the
// Python unit test can compare them against hashlib/zlib. Reads one topic per
// stdin line; writes `<scope> <topic_crc32_hex> <group_uncompressed>` to
// stdout for each scope nibble. Empty/blank lines are ignored.

#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>

#include "../../components/mpubsub/topic_hash.h"

using namespace esphome::multicast_pubsub;

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    // Strip trailing CR for cross-platform safety.
    if (!line.empty() && line.back() == '\r')
      line.pop_back();
    uint32_t crc = topic_crc32(line);
    for (Scope s : {Scope::LINK_LOCAL, Scope::SITE_LOCAL, Scope::ORG_LOCAL}) {
      auto addr = topic_to_group(line, s);
      char buf[64];
      group_to_string(addr, buf, sizeof(buf));
      std::printf("%u %08" PRIx32 " %s\n", static_cast<unsigned>(s), crc, buf);
    }
    std::printf("---\n");
  }
  return 0;
}
