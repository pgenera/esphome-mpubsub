# Examples

Each file is a self-contained ESPHome config that compiles for ESP32 (or the
chip variant called out at the top). Replace `!secret` references with your
own credentials, drop the file on an ESP32, and `esphome run`.

The examples currently reference the component via a relative path
(`external_components: [{ source: { type: local, path: ../components } }]`)
so they validate against this repository directly. Once the project is
published to a public git URL, swap the `source:` block for the
upstream form:

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/pgenera/esphome-mpubsub
      ref: main
    components: [mpubsub]
```

| File                              | What it shows                                                                              |
|-----------------------------------|--------------------------------------------------------------------------------------------|
| `01_temperature_sensor.yaml`      | **Raw publish** of DHT22 temperature/humidity values to two topics on every read.          |
| `02_thermostat_subscriber.yaml`   | **Raw subscribe** (via the sensor platform) and drive a relay; no broker in the loop.      |
| `03_button_doorbell.yaml`         | Press a button → one multicast publication → every listener reacts in parallel.            |
| `04_chime_subscriber.yaml`        | Companion to 03: an RTTTL chime that joins the doorbell topic via `on_message`.            |
| `05_mqtt_bridge.yaml`             | Run `mqtt:` and `mpubsub:` on the same device, bridging raw payloads both ways.   |
| `06_robot_coordination.yaml`      | The spec's motivating story: two devices coordinating with no cloud server.                |
| `07_typed_climate_sensor.yaml`    | **Typed publish** of a `RoomClimate` message via the fluent `Call` builder.                |
| `08_typed_climate_subscriber.yaml`| **Typed subscribe** via `on_message: + message:` — the trigger arg is the decoded struct. |
| `09_dynamic_bridge.yaml`          | Schemaless `DynamicMessage` + `publish_dynamic` re-encoding MQTT data as typed protobuf.   |

For local end-to-end testing on Linux without any ESP hardware, see
`tests/publisher.yaml`, `tests/subscriber.yaml`, and `tests/README.md`. Those
use `platform: host` so they compile to a native binary and exchange
multicast traffic over the loopback / link-local interfaces.

## API variants in one place

The same `RoomClimate` message can be published five different ways,
depending on what you have at hand. Pick whichever is most ergonomic
for your call site:

```yaml
# 1. Raw publish from YAML (just a string)
- mpubsub.publish:
    topic: "home/bedroom/climate"
    payload: !lambda 'return esphome::str_sprintf("%.1f", id(t).state);'

# 2. Typed publish via fluent Call builder (preferred, mirrors LightCall)
- lambda: |-
    using esphome::multicast_pubsub::messages::RoomClimate;
    id(pubsub)->make_call<RoomClimate>("home/bedroom/climate")
        .set_temperature(id(t).state)
        .set_humidity(id(h).state)
        .set_room_id("bedroom")
        .perform();

# 3. Typed publish via direct struct + publish<T>
- lambda: |-
    using esphome::multicast_pubsub::messages::RoomClimate;
    RoomClimate m;
    m.temperature = id(t).state;
    m.humidity    = id(h).state;
    m.room_id     = "bedroom";
    id(pubsub)->publish("home/bedroom/climate", m);

# 4. Schemaless DynamicMessage (when shape is decided at runtime)
- lambda: |-
    using esphome::multicast_pubsub::DynamicMessage;
    using esphome::multicast_pubsub::messages::RoomClimate;
    DynamicMessage m;
    m.add_float(1, id(t).state)
     .add_float(2, id(h).state)
     .add_string(3, "bedroom");
    id(pubsub)->publish_dynamic(
        "home/bedroom/climate", RoomClimate::SCHEMA_ID, m.bytes());

# 5. Raw publish from C++ directly
- lambda: |-
    id(pubsub)->publish("home/raw", std::string("hello"));
```

And the same message can be received two ways:

```yaml
# A. Typed receive (YAML on_message + message:)
mpubsub:
  on_message:
    - topic: "home/bedroom/climate"
      message: room_climate
      then:
        - lambda: 'ESP_LOGI("c", "%.1f", x.temperature);'   # x is RoomClimate

# B. Typed receive (direct C++ subscription, e.g. from a custom component)
- lambda: |-
    using esphome::multicast_pubsub::messages::RoomClimate;
    id(pubsub)->subscribe_typed<RoomClimate>("home/bedroom/climate",
        [](const RoomClimate &m) {
          ESP_LOGI("c", "%.1f", m.temperature);
        });
```

Full C++ reference: [`../docs/CXX_API.md`](../docs/CXX_API.md).

## Patterns to know

### Floats and integers

The sensor platform encodes states as ASCII (`"%.6g"`) on the wire. To
publish a raw IEEE-754 float (4 bytes, faster, no parsing on receive):

```yaml
on_value:
  - mpubsub.publish:
      topic: "home/livingroom/temp"
      payload: !lambda |-
        char buf[4];
        memcpy(buf, &x, 4);
        return std::string(buf, 4);
```

…and subscribers must decode the 4 bytes back into a float.

### Idempotent topics

Multicast UDP is **fire-and-forget**: there's no delivery guarantee, no
ordering, and no retain. Design topics so missing one message is fine:

* "current value" topics (`home/.../temperature`) that publish on every
  read — losing one is harmless.
* Idempotent commands (`vacuum/state=done`) where the receiver is fine
  applying the same value twice.

For "this just happened, exactly once" semantics (a button press, a
finished cycle) re-send the message a few times over a second.

### Topic naming

Topic strings are arbitrary UTF-8 with no NUL bytes, up to 200 bytes. There
are no wildcards (`+`, `#`) — each subscription is one exact topic. We
recommend MQTT-style hierarchies (`area/device/measurement`) for human
readability; the protocol itself doesn't care.

### Scope

Pick the smallest IPv6 multicast scope that lets all your devices hear each
other:

| Scope               | When to use                                                           |
|---------------------|-----------------------------------------------------------------------|
| `link-local`        | **Default.** All devices on the same L2 segment / VLAN. Safest, no routing needed. |
| `site-local`        | A single administrative site, possibly across VLANs with MLD-snooping switches configured. |
| `organization-local`| Multiple sites bridged at L3 with multicast routing.                  |

If a subscriber on a different VLAN never hears a publisher, the most
likely cause is the switch dropping the multicast frame for lack of an MLD
querier. Either configure one upstream, or stay with the default
`link-local` scope and keep participants on the same segment.
