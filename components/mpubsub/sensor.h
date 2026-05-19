#pragma once

#include "esphome/core/defines.h"
#ifdef USE_NETWORK
#ifdef USE_SENSOR

#include <cstdio>
#include <cstdlib>
#include <span>
#include <string>

#include "esphome/components/sensor/sensor.h"
#include "esphome/core/component.h"

#include "multicast_pubsub.h"

namespace esphome::multicast_pubsub {

class MulticastSensor : public sensor::Sensor, public Component {
 public:
  void set_parent(MulticastPubSub *parent) { this->parent_ = parent; }
  void set_topic(const std::string &topic) { this->topic_ = topic; }
  void set_subscribe(bool v) { this->subscribe_ = v; }
  void set_publish(bool v) { this->publish_ = v; }

  void setup() override {
    if (this->subscribe_) {
      this->parent_->subscribe(this->topic_, [this](std::span<const uint8_t> data) {
        // ASCII float decode. Copy into a small null-terminated buffer first
        // (strtof needs a NUL terminator).
        char buf[32];
        size_t n = data.size() < sizeof(buf) - 1 ? data.size() : sizeof(buf) - 1;
        std::memcpy(buf, data.data(), n);
        buf[n] = 0;
        char *end = nullptr;
        float v = std::strtof(buf, &end);
        if (end == buf)
          return;  // not a number; silently drop
        this->publish_state(v);
      });
    }
    if (this->publish_) {
      this->add_on_state_callback([this](float state) {
        char buf[32];
        int n = std::snprintf(buf, sizeof(buf), "%.6g", state);
        if (n < 0)
          return;
        // Sensor states travel as RAW ASCII bytes; typed-message publishing
        // is a separate code path layered on top later.
        this->parent_->publish(this->topic_,
                               std::span<const uint8_t>(reinterpret_cast<const uint8_t *>(buf),
                                                        static_cast<size_t>(n)),
                               Encoding::RAW);
      });
    }
  }

  float get_setup_priority() const override { return setup_priority::DATA; }

 protected:
  MulticastPubSub *parent_{nullptr};
  std::string topic_;
  bool subscribe_{true};
  bool publish_{false};
};

}  // namespace esphome::multicast_pubsub

#endif  // USE_SENSOR
#endif  // USE_NETWORK
