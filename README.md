# multicast_pubsub — ESPHome external component

A brokerless publish/subscribe transport for ESPHome based on IPv6 multicast.
Each topic deterministically maps to an IPv6 multicast group; publishers send
UDP datagrams to the group and subscribers join it. No broker is needed, and
the protocol keeps working when the WAN or any cloud MQTT broker is
unreachable.

Based on the disclosure *Self-Organizing Publish/Subscribe on the Network
Edge* (Phil Genera, Technical Disclosure Commons Art. 5601, 2022). See the
PDF in this repository for background.

This component is **complementary to** ESPHome's existing `mqtt:` component —
both can be configured on the same device and bridged together; see
`tests/bridge_example.yaml`.

## Usage

```yaml
external_components:
  - source:
      type: local
      path: ../components
    components: [multicast_pubsub]

multicast_pubsub:
  port: 18512            # default; one above the existing ESPHome udp: default (18511)
  scope: link-local      # default; safest scope, works on any flat LAN
  on_message:
    - topic: "home/vacuum/done"
      then:
        - logger.log: "vacuum finished!"

sensor:
  - platform: multicast_pubsub
    topic: "home/livingroom/temp"
    name: "Living Room Temperature"
    # default mode: subscribe (publishes incoming messages as sensor state)
```

To publish, use the `multicast_pubsub.publish` action from any automation:

```yaml
on_...:
  - multicast_pubsub.publish:
      topic: "home/vacuum/done"
      payload: !lambda 'return "1";'
```

## Wire protocol (v1)

Each publication is a single UDP datagram (≤ 508 bytes) sent to the topic's
multicast group on port 18512 (configurable).

### IPv6 multicast group derivation

```
group_addr = 0xFF || 0x1<scope> || SHA-256(utf8(topic))[0..14]
```

| Field            | Bits  | Value                                              |
|------------------|------:|----------------------------------------------------|
| Multicast prefix |     8 | `0xFF` (IPv6 multicast)                            |
| Flags nibble     |     4 | `0x1` (transient, RFC 4291 §2.7)                   |
| Scope nibble     |     4 | `0x2` link-local / `0x5` site-local / `0x8` org-local |
| Topic hash       |   112 | First 112 bits of `SHA-256(utf8 topic bytes)`      |

This yields exactly 112 bits of topic entropy per the spec.

### Header (12 bytes, little-endian)

```
 byte:  0    1    2    3    4    5    6    7    8    9   10   11
       +----+----+----+----+----+----+----+----+----+----+----+----+
       | 'M'| 'P'| VER| FLG|        TOPIC_CRC32         | PAY_LEN  | RSV
       +----+----+----+----+----+----+----+----+----+----+----+----+
```

| Offset | Size | Field        | Description                                        |
|-------:|-----:|--------------|----------------------------------------------------|
|      0 |    2 | MAGIC        | ASCII `"MP"` (`0x4D 0x50`)                         |
|      2 |    1 | VERSION      | Protocol version, `0x01` for v1                    |
|      3 |    1 | FLAGS        | bit 0 = text payload; bit 1 = retain hint; rest reserved |
|      4 |    4 | TOPIC_CRC32  | CRC-32/IEEE of the UTF-8 topic string, LE          |
|      8 |    2 | PAYLOAD_LEN  | uint16 LE; must equal `len(datagram) - 12`         |
|     10 |    2 | RESERVED     | Senders MUST write `0x00 0x00`; receivers MUST ignore (forward-compat) |
|     12 |  ≤496| PAYLOAD      | Opaque bytes                                       |

Receivers MUST drop the datagram silently if the magic is wrong, the version
is unknown, reserved flag bits are set, the length field is inconsistent, or
the `TOPIC_CRC32` doesn't correspond to any subscribed topic on the node
(handles the rare event of a 112-bit hash collision).

A reference implementation of encode/decode and the address derivation lives
in [`tests/unit/reference.py`](tests/unit/reference.py).

## Testing

```bash
# unit tests (pure Python; no ESPHome needed)
cd tests/unit && pytest -q

# config validation + host-platform compile
esphome config tests/subscriber.yaml
esphome compile tests/subscriber.yaml

# end-to-end smoke (two terminals)
./tests/run_e2e.sh
```

See `tests/README.md` for the full test catalog.

## Limitations (v1)

* IPv6 only. IPv4 / mDNS coordination is described in the spec but not yet
  implemented.
* No encryption, signing, or replay protection. Publishers and subscribers
  trust the local network.
* No MQTT-style wildcards (`+`, `#`) — each subscription is one exact topic.
  A separate bridge can fan out wildcards from MQTT.
* No retain / last-will semantics; multicast UDP is fire-and-forget.
