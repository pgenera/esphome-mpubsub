# Typed messages — design plan

Status: design draft. Not yet implemented.

## Context

Today the component carries a single opaque byte payload per
publication. This plan adds **typed protobuf messages** as a first-class
option, with raw byte payloads preserved as a co-equal alternative.
Receivers select the appropriate decoder by inspecting a single
`ENCODING` byte in the header.

Because this component is not yet published, the wire format is changed
**in place** — no version bump, no compatibility shims, no migration
story. Anything currently checked in is fair game to redesign.

## Wire format (revised)

```
 byte:  0    1    2    3    4    5    6    7    8    9   10   11   12 ...
       +----+----+----+----+----+----+----+----+----+----+----+----+----+
       | 'M'| 'P'| 01 |ENC |        TOPIC_CRC32         | PAY_LEN  | RSV | BODY
       +----+----+----+----+----+----+----+----+----+----+----+----+----+
        MAGIC      VER  ENC   uint32 LE                  uint16 LE
```

Header is 12 bytes — same layout as before *except* byte 3 changes
from `FLAGS` (bit-flags) to `ENCODING` (enum).

| Offset | Size | Field         | Description                                                                   |
|-------:|-----:|---------------|-------------------------------------------------------------------------------|
|      0 |    2 | `MAGIC`       | ASCII `"MP"`                                                                  |
|      2 |    1 | `VERSION`     | `0x01`                                                                        |
|      3 |    1 | `ENCODING`    | enum (see below)                                                              |
|      4 |    4 | `TOPIC_CRC32` | LE CRC-32/IEEE of UTF-8 topic                                                 |
|      8 |    2 | `PAYLOAD_LEN` | LE uint16 — number of body bytes after the header                             |
|     10 |    2 | `RESERVED`    | senders MUST write `0x00 0x00`, receivers MUST ignore                         |
|     12 |  ≤1220| `BODY`        | encoding-dependent (see below)                                                |

### ENCODING enum

| Value     | Name        | Body layout                                                       |
|----------:|-------------|-------------------------------------------------------------------|
|     0x00  | `RAW`       | opaque bytes                                                      |
|     0x01  | `PROTOBUF`  | 2-byte `SCHEMA_ID` (LE) followed by protobuf bytes                |
| 0x02..FF  | reserved    | receivers MUST drop the packet                                    |

Design notes:

* Single byte rather than bit-flags because the encoding determines how
  to parse the body — that's not an orthogonal feature, it's a content
  type. Enum makes the parser dispatch obvious.
* `FLAG_TEXT` and `FLAG_RETAIN_HINT` are gone. Neither was acted on by
  any receiver. Text-ness is implied by the schema (for typed messages)
  or by the consumer (for raw). Retain semantics belong in bridge
  configuration, not the wire.
* Future encodings (e.g. compressed flavors) take new enum values.
  Unknown values drop rather than falling back to raw — parsing
  unknown-encoding bytes as raw could deliver garbage to raw
  subscribers.

### Body per encoding

**`RAW` (0x00):**
```
[12-byte header][body bytes (PAYLOAD_LEN total)]
```

**`PROTOBUF` (0x01):**
```
[12-byte header][SCHEMA_ID: uint16 LE][protobuf bytes]
```
where `PAYLOAD_LEN = 2 + len(protobuf bytes)`.

## SCHEMA_ID derivation

A schema's id is the FNV-1a-16 hash of its **canonicalized definition**:

1. Fields sorted by tag number ascending.
2. Each rendered as `"<tag>:<type>:<name>"` (no whitespace).
3. Lines joined with `"\n"`, no trailing newline.
4. Encoded as UTF-8.

Reference Python:
```python
FNV_OFFSET_16 = 0x811C
FNV_PRIME = 0x100000001B3  # standard FNV prime, truncate at 16 bits

def schema_id(fields) -> int:
    canon = "\n".join(
        f"{f.tag}:{f.type}:{f.name}"
        for f in sorted(fields, key=lambda x: x.tag)
    ).encode("utf-8")
    h = FNV_OFFSET_16
    for b in canon:
        h = ((h ^ b) * FNV_PRIME) & 0xFFFF
    return h
```

Two devices declaring an identical schema compute the same `SCHEMA_ID`.
Adding/renaming a field, changing a type, or changing a tag all change
the id — a stale subscriber will drop new publishers' packets at the
schema-id check rather than mis-decode them.

16 bits over a few dozen schemas per device gives a practically-zero
false-match rate; the 112-bit IPv6 group hash already filters by topic.

## YAML surface

### Declaring schemas

```yaml
multicast_pubsub:
  scope: link-local
  messages:
    - id: room_climate
      fields:
        - { name: temperature, type: float,  tag: 1 }
        - { name: humidity,    type: float,  tag: 2 }
        - { name: room_id,     type: string, tag: 3 }
```

Each schema generates a C++ struct, a `constexpr uint16_t schema_id`,
and `encode_to` / `decode_from` methods.

### Publishing

Raw mode (existing behavior):
```yaml
- multicast_pubsub.publish:
    topic: "home/vacuum/done"
    payload: "1"
```

Typed mode (new):
```yaml
- multicast_pubsub.publish:
    topic: "home/livingroom/climate"
    message: room_climate
    values:
      temperature: !lambda 'return id(dht_t).state;'
      humidity:    !lambda 'return id(dht_h).state;'
      room_id:     "livingroom"
```

`payload:` and `message:` are mutually exclusive; `esphome config`
rejects configs that supply both or neither.

### Subscribing

Raw mode (existing):
```yaml
multicast_pubsub:
  on_message:
    - topic: "home/vacuum/done"
      then:
        - logger.log:
            format: "raw: %s"
            args: ['std::string(x.begin(), x.end()).c_str()']
```

Typed mode (new):
```yaml
multicast_pubsub:
  on_message:
    - topic: "home/livingroom/climate"
      message: room_climate
      then:
        - lambda: |-
            // x is the generated RoomClimate struct
            ESP_LOGI("climate", "%.1f C / %.0f%%", x.temperature, x.humidity);
```

A raw trigger fires only on `ENCODING == RAW`. A typed trigger fires
only on `ENCODING == PROTOBUF` **and** matching `SCHEMA_ID`. To accept
both, declare two `on_message:` entries.

## Type catalog

| YAML type | Proto wire type | C++ type                | Encoder           |
|-----------|-----------------|-------------------------|-------------------|
| `bool`    | varint          | `bool`                  | `encode_bool`     |
| `int32`   | varint          | `int32_t`               | `encode_int32`    |
| `int64`   | varint          | `int64_t`               | `encode_int64`    |
| `uint32`  | varint          | `uint32_t`              | `encode_uint32`   |
| `uint64`  | varint          | `uint64_t`              | `encode_uint64`   |
| `sint32`  | zigzag varint   | `int32_t`               | `encode_sint32`   |
| `sint64`  | zigzag varint   | `int64_t`               | `encode_sint64`   |
| `float`   | fixed32         | `float`                 | `encode_float`    |
| `double`  | fixed64         | `double`                | `encode_double`   |
| `string`  | length-delim    | `std::string`           | `encode_string`   |
| `bytes`   | length-delim    | `std::vector<uint8_t>`  | `encode_bytes`    |

Composite:
* `repeated <scalar>` — list of any scalar above; numerics use packed
  encoding. C++ representation `std::vector<T>`.
* Nested messages — **deferred**. Workaround: flatten the schema.
* Enums — **deferred**. Workaround: `int32` with named constants in the
  lambda.

## C++ code generation

Per `messages:` entry we emit a header into the build tree:

```cpp
// Generated from YAML message `room_climate`. Do not edit.
struct RoomClimate {
  static constexpr uint16_t SCHEMA_ID = 0xA37C;

  float temperature{0};
  float humidity{0};
  std::string room_id;

  void encode_to(esphome::api::ProtoWriteBuffer &buf) const {
    buf.encode_float(1, this->temperature);
    buf.encode_float(2, this->humidity);
    buf.encode_string(3, this->room_id);
  }

  static std::optional<RoomClimate> decode_from(std::span<const uint8_t>);
};
```

Codegen template at `components/multicast_pubsub/proto_emitter.py`,
mirroring the shape of `esphome/components/api/api_pb2.{h,cpp}`. Uses
ESPHome's own `proto.h` primitives — no vendored protobuf, no nanopb,
automatically tracks whatever ESPHome version the user is building
with.

### Dependency on `api:`

`ProtoWriteBuffer` lives in `esphome/components/api/proto.h`. Two paths:

1. **`DEPENDENCIES = ["api"]`** — drags in `api_server.cpp` (~10 KB
   unnecessary). Works today.
2. **Upstream `api_proto` leaf sub-component** — factor `proto.{h,cpp}`
   out so we depend only on the encoding primitives. Requires a small
   PR to ESPHome.

Ship with (1), open the upstream PR for (2) in parallel, swap when it
merges.

## Publish / subscribe API (C++)

```cpp
// Typed publish: ENCODING = PROTOBUF, body = SCHEMA_ID || encoded bytes
template<typename T>
bool MulticastPubSub::publish(const std::string &topic, const T &msg);

// Raw publish: ENCODING = RAW
bool MulticastPubSub::publish(const std::string &topic,
                              std::span<const uint8_t> payload);
bool MulticastPubSub::publish(const std::string &topic,
                              const std::string &payload);

// Escape hatch: pre-encoded proto bytes with an explicit schema id
bool MulticastPubSub::publish_dynamic(const std::string &topic,
                                      uint16_t schema_id,
                                      std::span<const uint8_t> proto_bytes);

// Typed subscribe
template<typename T>
void MulticastPubSub::subscribe_typed(const std::string &topic,
                                      std::function<void(const T &)> cb);
```

`subscribe_typed<T>` wraps `subscribe()`: filters by
`ENCODING == PROTOBUF`, validates `SCHEMA_ID == T::SCHEMA_ID`, decodes
into `T`, invokes the callback.

## DynamicMessage / DynamicReader

Lower-level escape hatch for runtime-shaped messages (bridges from
other protocols, variable-shape payloads, debugging). Built on
`esphome::api::ProtoWriteBuffer` and the corresponding reader, so we
don't reimplement varint encoding.

```cpp
namespace multicast_pubsub {

class DynamicMessage {
 public:
  DynamicMessage &add_int32(uint32_t tag, int32_t v);
  DynamicMessage &add_float(uint32_t tag, float v);
  DynamicMessage &add_string(uint32_t tag, std::string_view v);
  DynamicMessage &add_bytes(uint32_t tag, std::span<const uint8_t> v);
  // ... etc, returns *this for chaining
  std::span<const uint8_t> bytes() const;
};

class DynamicReader {
 public:
  explicit DynamicReader(std::span<const uint8_t> bytes);
  struct Field {
    uint32_t tag;
    WireType wire_type;
    bool as_int32(int32_t *out) const;
    bool as_float(float *out) const;
    std::string_view as_string() const;
    std::span<const uint8_t> as_bytes() const;
  };
  std::optional<Field> next();
};

}  // namespace multicast_pubsub
```

## Files

### Modify
* `components/multicast_pubsub/wire_format.h` — replace `FLAGS` with
  `Encoding` enum, drop `FLAG_TEXT`/`FLAG_RETAIN_HINT`.
* `components/multicast_pubsub/wire_format.cpp` — encode/decode with
  encoding enum.
* `components/multicast_pubsub/multicast_pubsub.{h,cpp}` — typed
  publish/subscribe, schema-id dispatch.
* `components/multicast_pubsub/automation.h` — typed trigger templates.
* `components/multicast_pubsub/__init__.py` — `messages:` schema,
  `message:` reference in action/trigger, mutual-exclusion validation,
  codegen for each message type.
* `tests/unit/reference.py` — encoding enum, schema_id helper.
* `tests/unit/test_wire_format.py` + `_cpp.py` — adapt to enum.
* `tests/unit/wire_format_main.cpp` — accept encoding param.
* `tests/unit/test_config.py` — adapt the few flag-related tests.
* `docs/PROTOCOL.md`, `docs/CONFIG.md`, `README.md`, `examples/README.md`,
  every example using flags — update to the new wire layout.

### Add
* `components/multicast_pubsub/proto_emitter.py` — schema-to-C++ codegen.
* `components/multicast_pubsub/dynamic_message.{h,cpp}` — `DynamicMessage`
  / `DynamicReader`.
* `tests/unit/test_schema_id.py` — locked golden vectors.
* `tests/unit/test_protobuf_roundtrip.py` — generated struct encode→decode.
* `tests/unit/test_dynamic_message.py` — builder/reader round-trip.
* `tests/typed_publisher.yaml`, `tests/typed_subscriber.yaml` — host
  integration.
* `examples/07_typed_climate_sensor.yaml`,
  `examples/08_typed_climate_subscriber.yaml`.

### Drop
* All `FLAG_TEXT`, `FLAG_RETAIN_HINT` references.

## Test plan

### Unit (extending the existing 66 tests)

1. **Wire format** — accept valid packets, reject unknown `ENCODING`
   values, reject malformed `PAYLOAD_LEN`.
2. **Schema id** — golden FNV-1a-16 vectors; Python ref vs codegen-
   emitted C++ constant agreement.
3. **Typed encode/decode** — for each scalar type plus a couple of
   `repeated` cases: build struct → encode → decode → assert equal. Cross-
   check against `DynamicReader` parsing the same bytes.
4. **DynamicMessage round-trip** — chain `add_*` calls, parse with
   `DynamicReader`, assert tag/type/value match.
5. **Mutual exclusion** — `esphome config` rejects publishes that
   specify both `payload:` and `message:` or neither.

### Integration (host platform)

6. `tests/typed_publisher.yaml` ticks a `RoomClimate` message every
   second; `tests/typed_subscriber.yaml` declares the same schema and
   logs received fields. Verify schema id matches on the wire and
   subscriber sees correctly-typed values.
7. Extend `tests/probe.py` with `--message <name>` to decode typed
   packets independently (third implementation cross-check).

## Deferred (future work)

* **Compression.** Reserved as future `ENCODING` values
  (`0x02 = COMPRESSED_RAW`, `0x03 = COMPRESSED_PROTOBUF`). Algorithm
  picked then — miniz if `web_server:` is enabled, else heatshrink.
* **Nested messages.** Flatten the schema for now.
* **Enums.** Use `int32` + named constants in lambdas for now.
* **Self-describing schema announcements.** Cute, complicated; not now.
* **Optional fields with explicit `present` bit.** Currently every scalar
  has a zero/empty default; distinguishing "unset" from "zero" costs
  wire bytes. Defer.

## Open questions

* Should `messages:` live under top-level `multicast_pubsub:` (proposed)
  or be its own top-level key? Lean: under `multicast_pubsub:`, users
  who want shared schemas can `!include` the inner list.
* Log schema-id mismatches at `ESP_LOGV` to help debug stale deploys?
  Yes — visible only at verbose log levels, no spam in normal runs.

## Implementation order

Staged so each step is independently testable:

1. **Wire format change + tests.** Replace `FLAGS` with `Encoding`
   enum, update reference.py, all wire-format unit tests, drop
   `FLAG_TEXT`/`FLAG_RETAIN_HINT` references. No new functionality —
   raw path keeps working under the new encoding byte.
2. **Schema codegen + tests.** YAML `messages:` config,
   `proto_emitter.py`, unit tests for generated encode/decode.
3. **Typed publish/subscribe wiring.** `publish<T>`, `subscribe_typed<T>`,
   action/trigger codegen for `message:`, mutual-exclusion validation.
4. **DynamicMessage / DynamicReader.** Lower-level API + tests.
5. **Integration test + examples.** Host-platform typed round-trip,
   probe.py extension, examples 07/08.
6. **Docs.** Update PROTOCOL.md, CONFIG.md, README.md, examples/README.md.
