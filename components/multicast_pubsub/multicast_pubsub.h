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

// A typed subscription callback is keyed on schema_id so the dispatcher
// can drop packets whose schema doesn't match what this callback expects.
struct TypedCallback {
  uint16_t schema_id;
  MessageCallback callback;  // body bytes have SCHEMA_ID already stripped
};

struct Subscription {
  std::string topic;
  uint32_t crc;
  GroupAddr group;
  // RAW-encoding callbacks fire on packets with ENCODING == RAW.
  std::vector<MessageCallback> raw_callbacks;
  // PROTOBUF-encoding callbacks fire on ENCODING == PROTOBUF when the
  // packet's SCHEMA_ID matches. Multiple typed callbacks per topic are
  // supported (e.g. listen for both RoomClimate and a sibling schema on
  // the same topic via a different sensor type).
  std::vector<TypedCallback> typed_callbacks;
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

  // Subscribe `cb` to ENCODING=RAW messages on `topic`. Multiple callbacks
  // per topic are supported; the first subscription joins the multicast group.
  void subscribe(const std::string &topic, MessageCallback cb);

  // Subscribe `cb` to ENCODING=PROTOBUF messages on `topic` whose SCHEMA_ID
  // matches T::SCHEMA_ID. The lambda receives the decoded typed struct.
  // The protobuf body is decoded via T::decode (inherited from
  // esphome::api::ProtoDecodableMessage).
  template<typename T> void subscribe_typed(const std::string &topic, std::function<void(const T &)> cb) {
    Subscription *sub = this->find_or_create_subscription_(topic);
    sub->typed_callbacks.push_back(TypedCallback{
        T::SCHEMA_ID,
        [cb = std::move(cb)](std::span<const uint8_t> body) {
          T msg;
          msg.decode(body.data(), body.size());
          cb(msg);
        },
    });
  }

  // Publish `payload` to `topic`. Defaults to ENCODING=RAW (opaque bytes);
  // typed publishes set Encoding::PROTOBUF and prepend a SCHEMA_ID.
  // Returns false and logs a warning on socket error or oversize payload.
  bool publish(const std::string &topic, std::span<const uint8_t> payload, Encoding encoding = Encoding::RAW);
  bool publish(const std::string &topic, const std::string &payload, Encoding encoding = Encoding::RAW) {
    return this->publish(topic,
                         std::span<const uint8_t>(reinterpret_cast<const uint8_t *>(payload.data()), payload.size()),
                         encoding);
  }

  // Publish a typed protobuf message. Encodes T via T::encode_to, prepends
  // T::SCHEMA_ID as a 2-byte LE prefix, and sends with ENCODING=PROTOBUF.
  template<typename T> bool publish(const std::string &topic, const T &msg) {
    std::array<uint8_t, MAX_PAYLOAD> buf;
    // Reserve 2 bytes at the front for SCHEMA_ID, then encode.
    constexpr size_t SCHEMA_ID_LEN = 2;
    buf[0] = static_cast<uint8_t>(T::SCHEMA_ID);
    buf[1] = static_cast<uint8_t>(T::SCHEMA_ID >> 8);
    size_t encoded = msg.encode_to(buf.data() + SCHEMA_ID_LEN, MAX_PAYLOAD - SCHEMA_ID_LEN);
    return this->publish(topic,
                         std::span<const uint8_t>(buf.data(), SCHEMA_ID_LEN + encoded),
                         Encoding::PROTOBUF);
  }

  /// Construct a fluent builder for a typed message. Mirrors
  /// esphome::light::LightState::make_call() -- chain set_<field>() calls
  /// and finish with perform() to encode and publish.
  ///
  /// Example:
  ///   id(pubsub)->make_call<RoomClimate>("home/garage/climate")
  ///       .set_temperature(22.5f)
  ///       .set_room_id("garage")
  ///       .perform();
  ///
  /// `T` must be a generated message struct (one of the entries under
  /// `multicast_pubsub.messages:` in YAML).
  template<typename T> typename T::Call make_call(const std::string &topic) {
    return typename T::Call(this, topic);
  }

 protected:
  void deliver_(uint32_t crc, Encoding encoding, std::span<const uint8_t> payload);
  Subscription *find_subscription_(const std::string &topic);
  // Look up `topic` -- create + join the multicast group if missing.
  // Used by both raw subscribe() and typed subscribe_typed<T>().
  Subscription *find_or_create_subscription_(const std::string &topic);

  uint16_t port_{18512};
  Scope scope_{Scope::LINK_LOCAL};
  uint8_t hops_{1};

  std::unique_ptr<socket::Socket> socket_{};
  std::vector<Subscription> subscriptions_{};
};

}  // namespace esphome::multicast_pubsub

#endif  // USE_NETWORK
