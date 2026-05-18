# C++ API reference

This is the API the component exposes to lambdas, automations, and any
custom C++ in the same firmware. For YAML-only users, see `CONFIG.md`.

Every example here assumes you have an ESPHome config with a
`multicast_pubsub:` block declared with `id: pubsub` (or some other id
you reference via `id(pubsub)`).

```yaml
multicast_pubsub:
  id: pubsub
  # ... port, scope, hops, messages, on_message as needed
```

## Headers

```cpp
#include "esphome/components/multicast_pubsub/multicast_pubsub.h"

// Only if you also want the schemaless runtime API:
#include "esphome/components/multicast_pubsub/dynamic_message.h"
```

Lambdas in `!lambda` blocks already see the component types through
the auto-generated includes — these explicit headers are only for
hand-written `.cpp` files.

## Raw payloads (encoding-agnostic byte slices)

### Publish

```cpp
// std::string convenience
id(pubsub)->publish("home/vacuum/done", "1");

// Arbitrary bytes via std::span<const uint8_t>
std::array<uint8_t, 4> body = {0xde, 0xad, 0xbe, 0xef};
id(pubsub)->publish("home/raw", std::span<const uint8_t>(body.data(), body.size()));
```

Returns `false` and logs at `ERROR` level if the payload exceeds the
1220-byte cap or the socket isn't ready.

### Subscribe

```cpp
id(pubsub)->subscribe("home/vacuum/done",
    [](std::span<const uint8_t> payload) {
      std::string s(payload.begin(), payload.end());
      ESP_LOGI("vac", "received: %s", s.c_str());
    });
```

Callbacks fire only on packets with `ENCODING == RAW`. Multiple
callbacks per topic are supported.

## Typed (protobuf) messages

Each YAML `messages:` entry generates a C++ struct in
`esphome::multicast_pubsub::messages::`. There are **three** ways to
publish a typed message and **two** ways to subscribe.

### Publish — fluent `Call` builder (preferred)

Modeled after `esphome::light::LightCall`. Bind a parent + topic at
construction, chain setters, finish with `.perform()`:

```cpp
using esphome::multicast_pubsub::messages::RoomClimate;

id(pubsub)->make_call<RoomClimate>("home/garage/climate")
    .set_temperature(22.5f)
    .set_humidity(50.0f)
    .set_room_id("garage")
    .perform();
```

Equivalent forms (pick whichever reads best at the call site):

```cpp
// Direct constructor:
RoomClimate::Call(id(pubsub), "home/garage/climate")
    .set_temperature(22.5f)
    .perform();
```

**Setters per type** (every setter returns `Call &` for chaining):

| YAML type | Setters |
|-----------|---------|
| `bool` / `int*` / `uint*` / `sint*` / `float` | `set_X(T)`, `set_X(esphome::optional<T>)` |
| `string` | `set_X(const std::string &)`, `set_X(const char *)`, `set_X(esphome::optional<std::string>)` |
| `bytes` | `set_X(std::vector<uint8_t>)`, `set_X(const uint8_t *, size_t)`, `set_X(std::span<const uint8_t>)` |

For **repeated** fields (`repeated: true` in YAML):

| YAML type | Setters |
|-----------|---------|
| repeated scalar | `add_X(T)`, `set_X(std::vector<T>)`, `clear_X()` |
| repeated string | `add_X(const std::string &)`, `add_X(const char *)`, `set_X(...)`, `clear_X()` |
| repeated bytes | `add_X(std::vector<uint8_t>)`, `add_X(const uint8_t *, size_t)`, `add_X(std::span<const uint8_t>)`, `set_X(...)`, `clear_X()` |

**Escape hatches** matching `LightCall` conventions:

```cpp
auto call = id(pubsub)->make_call<RoomClimate>("home/garage/climate");
call.set_temperature(22.5f);

// Direct access to the underlying struct for repeated-field push_back
// or conditional assembly that the fluent setters don't cover:
call.message().room_id = std::string("garage_") + std::to_string(unit);

// Retarget mid-chain:
call.set_topic("home/upstairs/climate").perform();
```

### Publish — `publish<T>(topic, msg)` (direct)

Bypass the builder if you already have a populated struct:

```cpp
using esphome::multicast_pubsub::messages::RoomClimate;

RoomClimate m;
m.temperature = id(dht_t).state;
m.humidity = id(dht_h).state;
m.room_id = "garage";

id(pubsub)->publish("home/garage/climate", m);
```

### Publish — schemaless via `DynamicMessage` + `publish_dynamic`

When the message shape is decided at runtime — bridges from other
protocols, variable-shape payloads, debugging — use the
schemaless API. The wire bytes are bit-for-bit compatible with the
typed encoders above, so typed subscribers can still decode them as
long as you pass a known `SCHEMA_ID`:

```cpp
using esphome::multicast_pubsub::DynamicMessage;
using esphome::multicast_pubsub::messages::RoomClimate;

DynamicMessage m;
m.add_float(1, 22.5f)         // temperature
 .add_float(2, 50.0f)         // humidity
 .add_string(3, "garage");    // room_id

id(pubsub)->publish_dynamic("home/garage/climate", RoomClimate::SCHEMA_ID, m.bytes());
```

Pass `0` as the `schema_id` for truly schemaless messages (only
`DynamicReader`-style consumers will see them — typed subscribers will
drop them via the schema-id check).

`DynamicMessage` supports every encoder ESPHome's protobuf library
implements, plus nesting:

```cpp
DynamicMessage location;
location.add_float(1, 37.4f).add_float(2, -122.1f);

DynamicMessage event;
event.add_uint32(1, 42)
     .add_string(2, "motion")
     .add_message(3, location);   // embedded as a length-delimited field
```

### Subscribe — `subscribe_typed<T>(topic, cb)`

Mirror of `publish<T>` on the receive side. Filters by encoding +
schema id automatically:

```cpp
using esphome::multicast_pubsub::messages::RoomClimate;

id(pubsub)->subscribe_typed<RoomClimate>("home/garage/climate",
    [](const RoomClimate &m) {
      ESP_LOGI("climate", "%.1fC, %.0f%% in %s",
               m.temperature, m.humidity, m.room_id.c_str());
    });
```

Fires only when both `ENCODING == PROTOBUF` and `SCHEMA_ID ==
T::SCHEMA_ID`. Stale-deploy packets (mismatched schema id) are dropped
silently and logged at `ESP_LOGV`.

### Subscribe — schemaless via `DynamicReader`

Inspect any incoming packet's fields without a schema. Useful for
debugging or for a forwarder that re-encodes onto another protocol.
Currently you'd subscribe to the raw byte stream and parse manually:

```cpp
using esphome::multicast_pubsub::DynamicReader;

id(pubsub)->subscribe("debug/topic", [](std::span<const uint8_t> body) {
  // For RAW packets, body is the user bytes directly. For PROTOBUF
  // packets the first 2 bytes are the SCHEMA_ID (LE) followed by the
  // protobuf body -- skip those if the packet is known typed.
  DynamicReader r(body);
  while (auto f = r.next()) {
    ESP_LOGD("dbg", "tag=%u wire=%u", f->tag, (unsigned)f->wire_type);
    if (f->wire_type == DynamicReader::WireType::VARINT) {
      ESP_LOGD("dbg", "  varint = %llu", (unsigned long long)f->raw_varint);
    } else if (f->wire_type == DynamicReader::WireType::FIXED32) {
      float fv;
      if (f->as_float(&fv)) ESP_LOGD("dbg", "  float = %f", fv);
    } else if (f->wire_type == DynamicReader::WireType::LENGTH_DELIMITED) {
      std::string_view s;
      if (f->as_string(&s)) ESP_LOGD("dbg", "  string = '%.*s'", (int)s.size(), s.data());
    }
  }
  if (r.error()) ESP_LOGW("dbg", "malformed packet");
});
```

`DynamicReader::Field` typed accessors (each returns `false` on
wire-type mismatch without touching `*out`):

```cpp
bool          as_bool(bool *);
int32_t       as_int32(int32_t *);
int64_t       as_int64(int64_t *);
uint32_t      as_uint32(uint32_t *);
uint64_t      as_uint64(uint64_t *);
int32_t       as_sint32(int32_t *);   // zigzag
int64_t       as_sint64(int64_t *);   // zigzag
float         as_float(float *);
std::string_view as_string(std::string_view *);
std::span<const uint8_t> as_bytes(std::span<const uint8_t> *);
DynamicReader as_message(DynamicReader *);   // embedded message
```

## YAML ↔ C++ correspondence (cheat sheet)

| Operation                  | YAML                                                                              | C++ (in a `!lambda`)                                                |
|----------------------------|-----------------------------------------------------------------------------------|---------------------------------------------------------------------|
| Publish raw                | `multicast_pubsub.publish: { topic, payload }`                                    | `id(pubsub)->publish(topic, payload)`                               |
| Publish typed              | `multicast_pubsub.publish: { topic, message: <id>, values: { ... } }`             | `id(pubsub)->make_call<T>(topic).set_X(v).perform()`                |
| Publish dynamic            | *(not exposed in YAML — schemaless by nature)*                                    | `id(pubsub)->publish_dynamic(topic, schema_id, bytes)`              |
| Subscribe raw              | `on_message: { topic, then: ... }` (`x` is `std::vector<uint8_t>`)                | `id(pubsub)->subscribe(topic, [](span) { ... })`                    |
| Subscribe typed            | `on_message: { topic, message: <id>, then: ... }` (`x` is the struct)             | `id(pubsub)->subscribe_typed<T>(topic, [](const T &m) { ... })`     |
| Sensor publish/subscribe   | `sensor: [{ platform: multicast_pubsub, topic, mode }]`                           | n/a (use the sensor platform from YAML)                             |

## Error handling

Every publish returns `bool` — `true` on success, `false` on oversize
payload, socket error, or component not yet set up. The component sets
a status warning visible via `ESP_LOGE` and the standard ESPHome
status reporting. Subscribers don't have a return path — they're
fire-and-forget callbacks invoked from the main loop's recv batch.

`DynamicReader` exposes `error()` after `next()` returns
`std::nullopt`, distinguishing a clean end-of-stream from a malformed
varint or oversize length-delim. Always check `error()` if the input
came from untrusted bytes.
