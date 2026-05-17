#pragma once

#include "esphome/core/defines.h"
#ifdef USE_NETWORK

#include <array>
#include <functional>
#include <memory>
#include <span>
#include <string>
#include <vector>

#include "esphome/components/socket/socket.h"
#include "esphome/core/component.h"
#include "esphome/core/helpers.h"

#include "topic_hash.h"
#include "wire_format.h"

namespace esphome::multicast_pubsub {

using MessageCallback = std::function<void(std::span<const uint8_t>)>;

struct Subscription {
  std::string topic;
  uint32_t crc;
  GroupAddr group;
  std::vector<MessageCallback> callbacks;
};

class MulticastPubSub : public Component {
 public:
  void set_port(uint16_t port) { this->port_ = port; }
  void set_scope(Scope scope) { this->scope_ = scope; }
  void set_hops(uint8_t hops) { this->hops_ = hops; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // Subscribe `cb` to messages on `topic`. Multiple callbacks per topic are
  // supported; the first subscription to a topic joins the multicast group.
  void subscribe(const std::string &topic, MessageCallback cb);

  // Publish `payload` to `topic`. Defaults to ENCODING=RAW (opaque bytes);
  // typed publishes set Encoding::PROTOBUF and prepend a SCHEMA_ID.
  // Returns false and logs a warning on socket error or oversize payload.
  bool publish(const std::string &topic, std::span<const uint8_t> payload, Encoding encoding = Encoding::RAW);
  bool publish(const std::string &topic, const std::string &payload, Encoding encoding = Encoding::RAW) {
    return this->publish(topic,
                         std::span<const uint8_t>(reinterpret_cast<const uint8_t *>(payload.data()), payload.size()),
                         encoding);
  }

 protected:
  void deliver_(uint32_t crc, std::span<const uint8_t> payload);
  Subscription *find_subscription_(const std::string &topic);

  uint16_t port_{18512};
  Scope scope_{Scope::LINK_LOCAL};
  uint8_t hops_{1};

  std::unique_ptr<socket::Socket> socket_{};
  std::vector<Subscription> subscriptions_{};
};

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
