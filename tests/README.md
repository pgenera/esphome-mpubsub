# mpubsub tests

## Unit tests

Pure-Python (and small C++ test binaries). Cover the spec without booting an
ESPHome firmware.

```bash
cd tests/unit
make            # builds topic_hash_test and wire_format_test
pytest -q       # runs all 63 unit tests in ~3s
```

| Test file                    | What it covers                                                  |
|------------------------------|-----------------------------------------------------------------|
| `test_topic_hash.py`         | SHA-256 layout, scope nibbles, CRC32 (Python reference)         |
| `test_topic_hash_cpp.py`     | C++ topic_to_group / topic_crc32 == Python reference (byte-for-byte) |
| `test_wire_format.py`        | Encode/decode round-trip, validation rules (Python)             |
| `test_wire_format_cpp.py`    | C++ encode/decode == Python reference, all reject reasons       |
| `test_config.py`             | YAML schema accepts valid configs, rejects bad scope/port/topic |
| `test_fuzz.py`               | Fuzz the standalone C++ wire-format / topic-hash harnesses under ASan+UBSan with random + adversarial inputs. |
| `test_protobuf_fuzz.py`      | Fuzz the Python protobuf reference so the spec ground-truth doesn't silently accept garbage.                   |
| `test_typed_decode_fuzz.py`  | Fuzz the **full typed-decode path** through a sanitizer-instrumented host binary -- crafted PROTOBUF packets over the actual multicast loopback. |

### Fuzzing

Three layers of coverage:

* **`test_fuzz.py`** -- standalone C++ wire-format/topic-hash via the
  `_san` harnesses (`-fsanitize=address,undefined`). Random bytes,
  mutated valid packets, adversarial size/encoding edge cases, random
  topic strings, valid-envelope + invalid-payload streams.
* **`test_protobuf_fuzz.py`** -- Python protobuf reference. Pure
  Python, fast.
* **`test_typed_decode_fuzz.py`** -- the full codegen-emitted typed
  decoder. Compiles `tests/typed_subscriber_san.yaml` into
  `pubsub-typed-subscriber-san` with
  `platformio_options.build_flags: ["-fsanitize=address,undefined"]`,
  then sends thousands of crafted PROTOBUF packets (valid envelope +
  matching `SCHEMA_ID` + garbage protobuf body) over the real
  multicast loopback. After the fuzz batch, sends one known-good
  packet and asserts the subscriber still decodes correctly --
  catches internal-state corruption that doesn't surface as an
  immediate sanitizer banner.

Any sanitizer report (out-of-bounds read, signed-overflow UB,
alignment violation, exit code 42 from `ASAN_OPTIONS=exitcode=42`,
abort) fails the test.

Run a layer at higher intensity:
```bash
FUZZ_ITERS=50000 pytest test_fuzz.py -v
FUZZ_ITERS=20000 pytest test_typed_decode_fuzz.py -v
```

## Integration tests (host platform)

All three configurations target `platform: host`, so they compile and run as
native Linux binaries — **no ESP hardware required**. End-to-end IPv6
multicast loops over the loopback interface.

```bash
esphome config tests/publisher.yaml
esphome config tests/subscriber.yaml
esphome config tests/bridge_example.yaml   # esp32, config-only
esphome compile tests/subscriber.yaml
esphome compile tests/publisher.yaml
```

To run end-to-end:

```bash
./tests/.esphome/build/pubsub-subscriber/.pioenvs/pubsub-subscriber/program &
./tests/.esphome/build/pubsub-publisher/.pioenvs/pubsub-publisher/program &
# In a third terminal, snoop the wire with the independent Python implementation:
python3 tests/probe.py --topic test/temp --scope link-local --iface lo
```

You should see the subscriber log `Subscribed Temperature: Received new state
NN.000000` once per second, and the probe should print one captured frame per
second with `flags=01` (FLAG_TEXT) and the ASCII float payload.

## What each integration file demonstrates

* **`publisher.yaml`** — one host-platform device that ticks a template
  sensor every second and publishes its state on topic `test/temp` via the
  `mode: publish` sensor platform.
* **`subscriber.yaml`** — a second host-platform device that subscribes via
  `mode: subscribe` and also has an `on_message:` trigger for a control topic.
* **`bridge_example.yaml`** — esp32 device running BOTH `mqtt:` and
  `mpubsub:` simultaneously, bridging messages between them in
  automations. Validates that the two components coexist (the C++ MQTT
  client doesn't have a host port, so this one is `esphome config`-only).

## probe.py — third-implementation cross-check

`tests/probe.py` is a single-file Python tool that joins the multicast
group for a topic, decodes incoming frames using the wire reference
in `tests/unit/reference.py`, and (when `--schema` is given) decodes
protobuf bodies via `tests/unit/protobuf.py` plus the codegen's
`proto_emitter.py` for SCHEMA_ID computation.

It's a **third independent implementation** of the wire protocol after
the C++ component and the codegen-generated typed encoders. When all
three agree the spec is in good shape.

### Listening

```bash
# Schemaless: render every PROTOBUF field by tag + wire type
python3 tests/probe.py --topic test/climate

# Schema-aware: match incoming SCHEMA_ID against a YAML messages: block
# and render fields by their declared names.
python3 tests/probe.py --topic test/climate --schema tests/typed_publisher.yaml
```

### Publishing

```bash
# Raw payload (unchanged from earlier)
python3 tests/probe.py --topic test/temp --publish "42.5"

# Typed protobuf: needs --schema, picks a message id, sets fields by name
python3 tests/probe.py --topic test/climate \
    --schema tests/typed_subscriber.yaml \
    --publish --message room_climate \
    --field temperature=42.5 --field humidity=33.0 --field room_id=garage
```

Repeated fields: pass `--field name=value` multiple times for the same
name. Non-string types coerce from the string form (`int(value)`,
`float(value)`, etc).

### Automated cross-check

`tests/unit/test_probe_cross_check.py` boots the actual host-platform
binaries from `tests/typed_publisher.yaml` and `tests/typed_subscriber.yaml`,
runs probe.py against them in both directions, and asserts the typed
fields show up where expected. Skipped automatically if the binaries
aren't built; build them with `esphome compile tests/typed_*.yaml`.
