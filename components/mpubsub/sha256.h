// Minimal portable SHA-256 implementation. Public-domain reference; used
// unconditionally on every platform so the implementation matches between
// ESP32, ESP8266, host, etc. without #ifdef gymnastics. ~150 lines of C++.

#pragma once

#include <cstddef>
#include <cstdint>

namespace esphome::multicast_pubsub {

class Sha256 {
 public:
  static constexpr size_t DIGEST_SIZE = 32;

  Sha256();
  void update(const uint8_t *data, size_t len);
  // Writes 32 bytes to `out`. Object must not be reused after finalize().
  void finalize(uint8_t out[DIGEST_SIZE]);

  // One-shot helper.
  static void hash(const uint8_t *data, size_t len, uint8_t out[DIGEST_SIZE]);

 private:
  void process_block_(const uint8_t block[64]);

  uint32_t state_[8];
  uint64_t bit_len_;
  uint8_t buffer_[64];
  size_t buffer_len_;
};

}  // namespace esphome::multicast_pubsub
