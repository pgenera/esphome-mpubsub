// SHA-256 implementation. Adapted from the public-domain reference at
// https://en.wikipedia.org/wiki/SHA-2#Pseudocode (FIPS 180-4). Verified
// against Python's hashlib via tests/unit/test_topic_hash.py.

#include "sha256.h"

#include <cstring>

namespace esphome::multicast_pubsub {

namespace {

constexpr uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

inline uint32_t rotr(uint32_t x, unsigned n) { return (x >> n) | (x << (32 - n)); }

}  // namespace

Sha256::Sha256() : bit_len_(0), buffer_len_(0) {
  this->state_[0] = 0x6a09e667;
  this->state_[1] = 0xbb67ae85;
  this->state_[2] = 0x3c6ef372;
  this->state_[3] = 0xa54ff53a;
  this->state_[4] = 0x510e527f;
  this->state_[5] = 0x9b05688c;
  this->state_[6] = 0x1f83d9ab;
  this->state_[7] = 0x5be0cd19;
}

void Sha256::process_block_(const uint8_t block[64]) {
  uint32_t w[64];
  for (int i = 0; i < 16; ++i) {
    w[i] = (uint32_t(block[i * 4]) << 24) | (uint32_t(block[i * 4 + 1]) << 16) |
           (uint32_t(block[i * 4 + 2]) << 8) | uint32_t(block[i * 4 + 3]);
  }
  for (int i = 16; i < 64; ++i) {
    uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
    uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
    w[i] = w[i - 16] + s0 + w[i - 7] + s1;
  }
  uint32_t a = this->state_[0], b = this->state_[1], c = this->state_[2], d = this->state_[3];
  uint32_t e = this->state_[4], f = this->state_[5], g = this->state_[6], h = this->state_[7];
  for (int i = 0; i < 64; ++i) {
    uint32_t s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
    uint32_t ch = (e & f) ^ (~e & g);
    uint32_t t1 = h + s1 + ch + K[i] + w[i];
    uint32_t s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
    uint32_t mj = (a & b) ^ (a & c) ^ (b & c);
    uint32_t t2 = s0 + mj;
    h = g;
    g = f;
    f = e;
    e = d + t1;
    d = c;
    c = b;
    b = a;
    a = t1 + t2;
  }
  this->state_[0] += a;
  this->state_[1] += b;
  this->state_[2] += c;
  this->state_[3] += d;
  this->state_[4] += e;
  this->state_[5] += f;
  this->state_[6] += g;
  this->state_[7] += h;
}

void Sha256::update(const uint8_t *data, size_t len) {
  this->bit_len_ += uint64_t(len) * 8;
  while (len > 0) {
    size_t take = 64 - this->buffer_len_;
    if (take > len)
      take = len;
    std::memcpy(this->buffer_ + this->buffer_len_, data, take);
    this->buffer_len_ += take;
    data += take;
    len -= take;
    if (this->buffer_len_ == 64) {
      this->process_block_(this->buffer_);
      this->buffer_len_ = 0;
    }
  }
}

void Sha256::finalize(uint8_t out[DIGEST_SIZE]) {
  // FIPS 180-4 padding: append 0x80, then zeros, then 64-bit BE length.
  uint64_t total_bits = this->bit_len_;
  this->buffer_[this->buffer_len_++] = 0x80;
  if (this->buffer_len_ > 56) {
    while (this->buffer_len_ < 64)
      this->buffer_[this->buffer_len_++] = 0;
    this->process_block_(this->buffer_);
    this->buffer_len_ = 0;
  }
  while (this->buffer_len_ < 56)
    this->buffer_[this->buffer_len_++] = 0;
  for (int i = 7; i >= 0; --i)
    this->buffer_[this->buffer_len_++] = uint8_t((total_bits >> (i * 8)) & 0xff);
  this->process_block_(this->buffer_);
  for (int i = 0; i < 8; ++i) {
    out[i * 4 + 0] = uint8_t(this->state_[i] >> 24);
    out[i * 4 + 1] = uint8_t(this->state_[i] >> 16);
    out[i * 4 + 2] = uint8_t(this->state_[i] >> 8);
    out[i * 4 + 3] = uint8_t(this->state_[i]);
  }
}

void Sha256::hash(const uint8_t *data, size_t len, uint8_t out[DIGEST_SIZE]) {
  Sha256 h;
  h.update(data, len);
  h.finalize(out);
}

}  // namespace esphome::multicast_pubsub
