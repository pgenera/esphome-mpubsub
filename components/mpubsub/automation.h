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
  // Per-call override of the component-level retransmit_count. If not
  // set the parent's configured default is used. -1 means indefinite
  // (subsequent publish() to the same topic supersedes it).
  TEMPLATABLE_VALUE(int16_t, retransmit_count)

  void play(Ts... x) override {
    auto t = this->topic_.value(x...);
    auto p = this->payload_.value(x...);
    // Save/restore the component-level count around the publish call.
    // ESPHome's loop is single-threaded; the (count-1) retransmits get
    // scheduled inside publish() reading retransmit_count_ synchronously,
    // and the set_timeout lambdas only re-send the captured datagram --
    // they don't re-read the count -- so restoring afterwards is safe.
    if (this->retransmit_count_.has_value()) {
      int16_t saved = this->parent_->get_retransmit_count();
      this->parent_->set_retransmit_count(this->retransmit_count_.value(x...));
      this->parent_->publish(t, p);
      this->parent_->set_retransmit_count(saved);
    } else {
      this->parent_->publish(t, p);
    }
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
