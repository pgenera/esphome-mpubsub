# Testing guide

This component ships **two layers** of tests, both runnable on Linux with
no microcontroller in sight.

## Layer 1 — unit tests (~3 seconds)

Tests under `tests/unit/` cover:

* Topic-hash and CRC32 derivation (Python reference + C++ binary
  cross-check).
* Wire format encode/decode and every validation rule (Python +
  C++ binary).
* YAML schema acceptance and rejection via `esphome config`.

```bash
cd tests/unit
make            # builds two tiny C++ test harnesses
pytest -q
```

The C++ harnesses (`topic_hash_test`, `wire_format_test`) link only
`components/multicast_pubsub/*.cpp` plus `topic_hash_main.cpp` /
`wire_format_main.cpp` — no ESPHome dependencies. This means the SHA-256,
topic-to-group, CRC32, header encode, and header decode code paths are
exercised against a Python reference (`tests/unit/reference.py`) for
byte-for-byte agreement.

### Adding new spec vectors

When you change the protocol, add a golden vector in
`tests/unit/test_topic_hash.py` (or `test_wire_format.py`) and re-run
`pytest`. The C++ cross-check tests will automatically extend coverage to
the new vector via the harness.

## Layer 2 — integration tests with `platform: host`

Run actual ESPHome firmware **as native Linux binaries**. The host
platform compiles to a regular `program` executable that uses BSD sockets,
so the C++ component runs end-to-end with real UDP packets over the
loopback interface.

```bash
esphome config tests/publisher.yaml      # validate
esphome config tests/subscriber.yaml
esphome compile tests/publisher.yaml     # build native binary
esphome compile tests/subscriber.yaml
```

### Manual end-to-end run

Three terminals:

```bash
# terminal 1 — subscriber
./tests/.esphome/build/pubsub-subscriber/.pioenvs/pubsub-subscriber/program

# terminal 2 — publisher
./tests/.esphome/build/pubsub-publisher/.pioenvs/pubsub-publisher/program

# terminal 3 — independent probe (uses tests/unit/reference.py for wire decode)
python3 tests/probe.py --topic test/temp --scope link-local --iface lo
```

Expected behavior within a few seconds:

* The subscriber logs `'Subscribed Temperature': Received new state
  NN.000000` once per second.
* The probe prints one captured frame per second, e.g.
  `crc=f7f84fca flags=01 payload(2 B)=3237 '27'`.

The probe is the **third independent implementation** of the wire format
(after C++ and `reference.py`). If all three agree, the protocol is
self-consistent.

### Why `--scope link-local` for local testing

`link-local` (`ff12::/16`) datagrams are delivered to every interface on
the host but never forwarded by a router. That makes them perfect for
two-process testing on a single Linux box: the kernel sees the publish on
the wifi/ethernet link-local address and delivers it to the subscriber
that joined the same group, even when both run on the same machine.

`site-local` may or may not work for inter-host testing depending on your
switch's MLD-snooping configuration. The default scope is `link-local` for
exactly this reason — it Just Works on flat LANs and loopback.

## Compiling for real hardware

Once you've verified your changes pass both test layers, target a real
chip:

```bash
esphome compile examples/01_temperature_sensor.yaml
esphome upload examples/01_temperature_sensor.yaml --device /dev/ttyUSB0
```

The same component sources are used; only the platform-specific socket
backend changes (`bsd_sockets_impl.cpp` on host, LwIP-sockets on ESP-IDF).

## What's NOT covered yet

* No cross-VLAN / multi-host integration test. The `link-local` scope
  short-circuits this nicely on loopback, but verifying MLD-snooping
  forwarding requires a real switch.
* No fuzzing of the decode path. The decoder is small and exhaustively
  validated; fuzzing would still be a worthwhile addition.
* No memory benchmarking. Should be cheap (one socket, one `std::vector`
  per subscription) but unmeasured.
