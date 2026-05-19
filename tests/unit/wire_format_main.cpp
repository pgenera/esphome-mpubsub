// CLI harness for the C++ wire-format encoder/decoder.
//
// Protocol on stdin (one command per line):
//   E <topic_crc_hex> <encoding_hex> <payload_hex>
//   D <packet_hex>
//
// Output for E: "OK <encoded_hex>"
// Output for D: "OK <topic_crc_hex> <encoding_hex> <payload_hex>" or "ERR <code>"

#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <span>
#include <string>
#include <vector>

#include "../../components/mpubsub/wire_format.h"

using namespace esphome::multicast_pubsub;

static std::vector<uint8_t> from_hex(const std::string &s) {
  std::vector<uint8_t> out;
  out.reserve(s.size() / 2);
  for (size_t i = 0; i + 1 < s.size(); i += 2) {
    out.push_back(static_cast<uint8_t>(std::strtoul(s.substr(i, 2).c_str(), nullptr, 16)));
  }
  return out;
}

static void emit_hex(const uint8_t *data, size_t len) {
  for (size_t i = 0; i < len; ++i)
    std::printf("%02x", data[i]);
}

static const char *err_name(DecodeError e) {
  switch (e) {
    case DecodeError::OK:
      return "OK";
    case DecodeError::TOO_SHORT:
      return "TOO_SHORT";
    case DecodeError::BAD_MAGIC:
      return "BAD_MAGIC";
    case DecodeError::BAD_VERSION:
      return "BAD_VERSION";
    case DecodeError::UNKNOWN_ENCODING:
      return "UNKNOWN_ENCODING";
    case DecodeError::LENGTH_MISMATCH:
      return "LENGTH_MISMATCH";
  }
  return "UNKNOWN";
}

int main() {
  std::string line;
  while (std::getline(std::cin, line)) {
    if (line.empty())
      continue;
    char cmd = line[0];
    // Defensive substr -- a one-char line is malformed but must not crash.
    std::string rest = line.size() >= 2 ? line.substr(2) : std::string();
    if (cmd == 'E') {
      // E <crc> <encoding> <payload>
      size_t s1 = rest.find(' ');
      if (s1 == std::string::npos) {
        std::printf("ERR malformed_E\n");
        std::fflush(stdout);
        continue;
      }
      size_t s2 = rest.find(' ', s1 + 1);
      if (s2 == std::string::npos) {
        std::printf("ERR malformed_E\n");
        std::fflush(stdout);
        continue;
      }
      uint32_t crc = static_cast<uint32_t>(std::strtoul(rest.substr(0, s1).c_str(), nullptr, 16));
      uint8_t enc_raw = static_cast<uint8_t>(std::strtoul(rest.substr(s1 + 1, s2 - s1 - 1).c_str(), nullptr, 16));
      auto payload = from_hex(rest.substr(s2 + 1));
      uint8_t header[HEADER_LEN];
      encode_header(crc, static_cast<Encoding>(enc_raw), static_cast<uint16_t>(payload.size()), header);
      std::printf("OK ");
      emit_hex(header, HEADER_LEN);
      emit_hex(payload.data(), payload.size());
      std::printf("\n");
    } else if (cmd == 'D') {
      auto packet = from_hex(rest);
      DecodedPacket pkt;
      DecodeError err = decode(std::span<const uint8_t>(packet.data(), packet.size()), &pkt);
      if (err != DecodeError::OK) {
        std::printf("ERR %s\n", err_name(err));
      } else {
        std::printf("OK %08x %02x ", pkt.topic_crc, static_cast<uint8_t>(pkt.encoding));
        emit_hex(pkt.payload.data(), pkt.payload.size());
        std::printf("\n");
      }
    } else {
      std::printf("ERR unknown_cmd\n");
    }
    std::fflush(stdout);
  }
  return 0;
}
