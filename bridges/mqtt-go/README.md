# mqtt-pubsub-bridge

A standalone Go daemon that mirrors messages between an MQTT broker and the
`multicast_pubsub` IPv6 multicast fabric. Useful when:

- you have an existing MQTT-based deployment and want to expose those topics
  to multicast-only ESPHome devices, or
- you want a Linux host (Home Assistant, a logger, an analytics box) to
  ingest multicast publications without having to speak the wire format
  itself.

## Build

```
cd bridges/mqtt-go
go build -o mqtt-pubsub-bridge .
```

## Run

```
./mqtt-pubsub-bridge -config bridge.yaml
./mqtt-pubsub-bridge -config bridge.yaml -log-level debug
```

See [`bridge.example.yaml`](bridge.example.yaml) for the config shape.

## Config

| Section | Key | Default | Meaning |
|---------|-----|---------|---------|
| `mqtt` | `broker` | — | Required. `tcp://host:1883`, `ssl://host:8883`, `ws://...`, etc. |
| `mqtt` | `client_id` | `multicast-pubsub-bridge` | Stable id helps reconnect logic on the broker. |
| `mqtt` | `username` / `password` | — | Optional credentials. |
| `mqtt` | `qos` | `0` | QoS for both subscribed and published MQTT topics. |
| `mqtt` | `retain` | `false` | Retain flag for messages bridged into MQTT. |
| `multicast_pubsub` | `port` | `18512` | Must match the ESPHome devices. |
| `multicast_pubsub` | `scope` | `link-local` | `link-local` / `site-local` / `organization-local`. |
| `multicast_pubsub` | `hops` | `1` | Outgoing `IPV6_MULTICAST_HOPS`. |
| `multicast_pubsub` | `retransmit_count` | `1` | Number of UDP datagrams emitted per logical publish. `1` = no retransmission. First send is synchronous; the rest run on a per-publish goroutine. |
| `multicast_pubsub` | `retransmit_delay` | `100ms` | Spacing between successive sends. Go duration string (`"100ms"`, `"1s"`, `"0s"`). `0` supported. |
| `multicast_pubsub` | `interface` | (kernel default) | Egress interface name (`eth0`, `br-lan`, …). |
| `bridges[]` | `direction` | — | `mqtt_to_mpubsub` or `mpubsub_to_mqtt`. One-directional. |
| `bridges[]` | `mqtt_topic` | — | The MQTT topic to subscribe to (mqtt→mpubsub) or publish to (mpubsub→mqtt). |
| `bridges[]` | `mpubsub_topic` | — | The multicast_pubsub topic. |

## Notes

- **Wire format**: this binary embeds a Go reimplementation of the same wire
  format used by the C++ component (`components/multicast_pubsub/`) and the
  Python reference (`tests/unit/reference.py`). If you change one, change all
  three.
- **Encoding**: outgoing packets are sent as `ENCODING=RAW` (opaque MQTT
  payload bytes). Incoming `ENCODING=PROTOBUF` packets are dropped on the
  multicast→MQTT side because the bridge has no way to know which protobuf
  schema the bytes belong to. Use raw publishes if you need MQTT bridging.
- **Loops**: each bridge entry is one-directional by design. If you bridge
  the same topic both ways through a broker that re-delivers to its
  publisher you can create a feedback loop -- use distinct `mqtt_topic`
  names on the two sides, or be very careful with retained/QoS settings.
- **Loopback**: the multicast socket has `IPV6_MULTICAST_LOOP=1`, so a
  second bridge or a probe co-located on the same host can receive the
  bridge's outbound publications. Because each bridge entry is
  one-directional, the bridge re-receiving its own packet does not feed
  back into the inbound MQTT subscription -- it would only matter if you
  configured a third route looping the destination MQTT topic back into
  the same multicast topic.
