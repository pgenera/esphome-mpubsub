#include "multicast_pubsub.h"

#ifdef USE_NETWORK

#include <array>
#include <cerrno>
#include <cstring>

#include "esphome/core/log.h"

// Pull in the platform's `sockaddr_in6` / `ipv6_mreq` definitions. The
// ESPHome socket abstraction takes care of selecting BSD vs LwIP sockets, but
// the IPv6 multicast group-membership struct is the same shape on both:
// 16-byte multicast address + 4-byte interface index.
#if defined(USE_SOCKET_IMPL_BSD_SOCKETS)
#include <netinet/in.h>
#include <sys/socket.h>
#elif defined(USE_SOCKET_IMPL_LWIP_SOCKETS)
#include "lwip/sockets.h"
#endif

namespace esphome::multicast_pubsub {

static const char *const TAG = "multicast_pubsub";

void MulticastPubSub::setup() {
  this->socket_ = socket::socket(AF_INET6, SOCK_DGRAM, IPPROTO_UDP);
  if (!this->socket_) {
    this->status_set_error(LOG_STR("Failed to create IPv6 UDP socket"));
    this->mark_failed();
    return;
  }
  int enable = 1;
  this->socket_->setsockopt(SOL_SOCKET, SO_REUSEADDR, &enable, sizeof(enable));
  this->socket_->setblocking(false);

  // Set the outgoing multicast hop limit. Default 1 = link-local-only travel;
  // users can raise it via the `hops:` YAML option.
  int hops = this->hops_;
  this->socket_->setsockopt(IPPROTO_IPV6, IPV6_MULTICAST_HOPS, &hops, sizeof(hops));

  // Bind to [::]:port so we can receive from any joined group.
  struct sockaddr_in6 server {};
  server.sin6_family = AF_INET6;
  server.sin6_port = htons(this->port_);
  // sin6_addr defaults to in6addr_any (all zeros).
  if (this->socket_->bind(reinterpret_cast<struct sockaddr *>(&server), sizeof(server)) != 0) {
    ESP_LOGE(TAG, "bind() failed: errno %d", errno);
    this->status_set_error(LOG_STR("bind failed"));
    this->mark_failed();
    return;
  }

  // Join all the multicast groups configured at startup.
  for (auto &sub : this->subscriptions_) {
    struct ipv6_mreq mreq {};
    std::memcpy(&mreq.ipv6mr_multiaddr, sub.group.data(), sub.group.size());
    mreq.ipv6mr_interface = 0;  // default interface
    if (this->socket_->setsockopt(IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq, sizeof(mreq)) != 0) {
      ESP_LOGW(TAG, "IPV6_JOIN_GROUP failed for topic '%s': errno %d", sub.topic.c_str(), errno);
      this->status_set_warning(LOG_STR("Failed to join multicast group"));
      // Continue — bad topic shouldn't kill the whole component.
    }
  }
}

void MulticastPubSub::loop() {
  if (!this->socket_)
    return;
  std::array<uint8_t, MAX_DATAGRAM> buf;
  for (;;) {
    auto n = this->socket_->read(buf.data(), buf.size());
    if (n <= 0)
      break;
    DecodedPacket pkt;
    auto err = decode(std::span<const uint8_t>(buf.data(), static_cast<size_t>(n)), &pkt);
    if (err != DecodeError::OK) {
      ESP_LOGV(TAG, "drop packet: decode err %u", static_cast<unsigned>(err));
      continue;
    }
    this->deliver_(pkt.topic_crc, pkt.payload);
  }
}

void MulticastPubSub::dump_config() {
  const char *scope_name = "link-local";
  switch (this->scope_) {
    case Scope::LINK_LOCAL:
      scope_name = "link-local";
      break;
    case Scope::SITE_LOCAL:
      scope_name = "site-local";
      break;
    case Scope::ORG_LOCAL:
      scope_name = "organization-local";
      break;
  }
  ESP_LOGCONFIG(TAG,
                "Multicast Pub/Sub:\n"
                "  Port: %u\n"
                "  Scope: %s\n"
                "  Hops: %u\n"
                "  Subscriptions: %u",
                this->port_, scope_name, this->hops_, static_cast<unsigned>(this->subscriptions_.size()));
  for (const auto &sub : this->subscriptions_) {
    char addr_buf[64];
    group_to_string(sub.group, addr_buf, sizeof(addr_buf));
    ESP_LOGCONFIG(TAG, "    '%s' -> [%s]:%u (crc=%08x)", sub.topic.c_str(), addr_buf, this->port_, sub.crc);
  }
}

Subscription *MulticastPubSub::find_subscription_(const std::string &topic) {
  for (auto &sub : this->subscriptions_) {
    if (sub.topic == topic)
      return &sub;
  }
  return nullptr;
}

void MulticastPubSub::subscribe(const std::string &topic, MessageCallback cb) {
  Subscription *existing = this->find_subscription_(topic);
  if (existing) {
    existing->callbacks.push_back(std::move(cb));
    return;
  }
  Subscription sub;
  sub.topic = topic;
  sub.crc = topic_crc32(topic);
  sub.group = topic_to_group(topic, this->scope_);
  sub.callbacks.push_back(std::move(cb));
  this->subscriptions_.push_back(std::move(sub));
  // Group join happens in setup(); if subscribe() is called after setup()
  // (e.g. dynamic subscription), join now.
  if (this->socket_) {
    auto &just_added = this->subscriptions_.back();
    struct ipv6_mreq mreq {};
    std::memcpy(&mreq.ipv6mr_multiaddr, just_added.group.data(), just_added.group.size());
    mreq.ipv6mr_interface = 0;
    if (this->socket_->setsockopt(IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq, sizeof(mreq)) != 0) {
      ESP_LOGW(TAG, "Late IPV6_JOIN_GROUP failed for topic '%s': errno %d", just_added.topic.c_str(), errno);
    }
  }
}

bool MulticastPubSub::publish(const std::string &topic, std::span<const uint8_t> payload, uint8_t flags) {
  if (!this->socket_) {
    ESP_LOGW(TAG, "publish(%s): socket not ready", topic.c_str());
    return false;
  }
  if (payload.size() > MAX_PAYLOAD) {
    ESP_LOGE(TAG, "publish('%s') rejected: payload %zu bytes exceeds max %zu (datagram limit %zu - 12-byte header)",
             topic.c_str(), payload.size(), MAX_PAYLOAD, MAX_DATAGRAM);
    this->status_set_warning(LOG_STR("oversize publish payload"));
    return false;
  }
  if (flags & RESERVED_FLAG_MASK) {
    ESP_LOGW(TAG, "publish(%s): reserved flag bits set (%02x)", topic.c_str(), flags);
    return false;
  }
  uint32_t crc = topic_crc32(topic);
  GroupAddr group = topic_to_group(topic, this->scope_);

  std::array<uint8_t, MAX_DATAGRAM> buf;
  encode_header(crc, flags, static_cast<uint16_t>(payload.size()), buf.data());
  std::memcpy(buf.data() + HEADER_LEN, payload.data(), payload.size());

  struct sockaddr_in6 dest {};
  dest.sin6_family = AF_INET6;
  dest.sin6_port = htons(this->port_);
  std::memcpy(&dest.sin6_addr, group.data(), group.size());

  auto sent = this->socket_->sendto(buf.data(), HEADER_LEN + payload.size(), 0,
                                    reinterpret_cast<struct sockaddr *>(&dest), sizeof(dest));
  if (sent < 0) {
    ESP_LOGW(TAG, "publish(%s): sendto errno %d", topic.c_str(), errno);
    return false;
  }
  ESP_LOGV(TAG, "published %zu bytes to '%s'", payload.size(), topic.c_str());
  return true;
}

void MulticastPubSub::deliver_(uint32_t crc, std::span<const uint8_t> payload) {
  for (auto &sub : this->subscriptions_) {
    if (sub.crc != crc)
      continue;
    // CRC match -- almost certainly the right topic. (False positives across
    // 32 bits of CRC are vanishingly rare on the small per-device topic set;
    // worst case the subscriber sees a bogus payload.)
    for (auto &cb : sub.callbacks)
      cb(payload);
  }
}

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
