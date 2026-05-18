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
    this->deliver_(pkt.topic_crc, pkt.encoding, pkt.payload);
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
    ESP_LOGCONFIG(TAG, "    '%s' -> [%s]:%u (crc=%08x, raw=%zu typed=%zu)", sub.topic.c_str(), addr_buf, this->port_,
                  sub.crc, sub.raw_callbacks.size(), sub.typed_callbacks.size());
  }
}

Subscription *MulticastPubSub::find_subscription_(const std::string &topic) {
  for (auto &sub : this->subscriptions_) {
    if (sub.topic == topic)
      return &sub;
  }
  return nullptr;
}

Subscription *MulticastPubSub::find_or_create_subscription_(const std::string &topic) {
  if (Subscription *existing = this->find_subscription_(topic))
    return existing;
  Subscription sub;
  sub.topic = topic;
  sub.crc = topic_crc32(topic);
  sub.group = topic_to_group(topic, this->scope_);
  this->subscriptions_.push_back(std::move(sub));
  // Group join happens in setup() for subscriptions registered before then;
  // for late subscriptions, join immediately so we start receiving.
  if (this->socket_) {
    auto &just_added = this->subscriptions_.back();
    struct ipv6_mreq mreq {};
    std::memcpy(&mreq.ipv6mr_multiaddr, just_added.group.data(), just_added.group.size());
    mreq.ipv6mr_interface = 0;
    if (this->socket_->setsockopt(IPPROTO_IPV6, IPV6_JOIN_GROUP, &mreq, sizeof(mreq)) != 0) {
      ESP_LOGW(TAG, "Late IPV6_JOIN_GROUP failed for topic '%s': errno %d", just_added.topic.c_str(), errno);
    }
  }
  return &this->subscriptions_.back();
}

void MulticastPubSub::subscribe(const std::string &topic, MessageCallback cb) {
  Subscription *sub = this->find_or_create_subscription_(topic);
  sub->raw_callbacks.push_back(std::move(cb));
}

bool MulticastPubSub::publish(const std::string &topic, std::span<const uint8_t> payload, Encoding encoding) {
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
  uint32_t crc = topic_crc32(topic);
  GroupAddr group = topic_to_group(topic, this->scope_);

  std::array<uint8_t, MAX_DATAGRAM> buf;
  encode_header(crc, encoding, static_cast<uint16_t>(payload.size()), buf.data());
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

bool MulticastPubSub::publish_dynamic(const std::string &topic, uint16_t schema_id,
                                      std::span<const uint8_t> proto_bytes) {
  // Same wire layout as the typed publish<T>: ENCODING=PROTOBUF, body =
  // SCHEMA_ID (2 LE bytes) || proto bytes. publish() enforces the
  // 1220-byte payload cap for us.
  std::array<uint8_t, MAX_PAYLOAD> buf;
  if (proto_bytes.size() + 2 > MAX_PAYLOAD) {
    ESP_LOGE(TAG, "publish_dynamic('%s'): payload %zu bytes exceeds max %zu", topic.c_str(),
             proto_bytes.size() + 2, MAX_PAYLOAD);
    return false;
  }
  buf[0] = static_cast<uint8_t>(schema_id);
  buf[1] = static_cast<uint8_t>(schema_id >> 8);
  std::memcpy(buf.data() + 2, proto_bytes.data(), proto_bytes.size());
  return this->publish(topic, std::span<const uint8_t>(buf.data(), 2 + proto_bytes.size()), Encoding::PROTOBUF);
}

void MulticastPubSub::deliver_(uint32_t crc, Encoding encoding, std::span<const uint8_t> payload) {
  for (auto &sub : this->subscriptions_) {
    if (sub.crc != crc)
      continue;
    if (encoding == Encoding::RAW) {
      for (auto &cb : sub.raw_callbacks)
        cb(payload);
    } else if (encoding == Encoding::PROTOBUF) {
      if (payload.size() < 2) {
        ESP_LOGV(TAG, "drop PROTOBUF packet: body too short for SCHEMA_ID (%zu)", payload.size());
        continue;
      }
      uint16_t schema_id = static_cast<uint16_t>(payload[0]) | (static_cast<uint16_t>(payload[1]) << 8);
      auto body = payload.subspan(2);
      bool matched_any = false;
      for (auto &tc : sub.typed_callbacks) {
        if (tc.schema_id == schema_id) {
          tc.callback(body);
          matched_any = true;
        }
      }
      if (!matched_any) {
        // Helps diagnose stale-deploy issues where publisher and subscriber
        // have drifted on schema definitions.
        ESP_LOGV(TAG, "no typed callback for topic '%s' schema_id=%04x", sub.topic.c_str(), schema_id);
      }
    }
  }
}

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
