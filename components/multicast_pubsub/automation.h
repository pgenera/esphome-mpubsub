#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/helpers.h"

#include "multicast_pubsub.h"

#ifdef USE_NETWORK

#include <string>
#include <vector>

namespace esphome::multicast_pubsub {

template<typename... Ts> class PublishAction : public Action<Ts...> {
 public:
  explicit PublishAction(MulticastPubSub *parent) : parent_(parent) {}
  TEMPLATABLE_VALUE(std::string, topic)
  TEMPLATABLE_VALUE(std::string, payload)

  void play(Ts... x) override {
    auto t = this->topic_.value(x...);
    auto p = this->payload_.value(x...);
    this->parent_->publish(t, p);
  }

 protected:
  MulticastPubSub *parent_;
};

class OnMessageTrigger : public Trigger<std::vector<uint8_t>> {
 public:
  OnMessageTrigger(MulticastPubSub *parent, const std::string &topic) {
    parent->subscribe(topic, [this](std::span<const uint8_t> payload) {
      this->trigger(std::vector<uint8_t>(payload.begin(), payload.end()));
    });
  }
};

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
