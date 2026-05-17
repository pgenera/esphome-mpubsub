# Examples

Each file is a self-contained ESPHome config that compiles for ESP32 (or the
chip variant called out at the top). Replace `!secret` references with your
own credentials, drop the file on an ESP32, and `esphome run`.

| File                            | What it shows                                                                    |
|---------------------------------|----------------------------------------------------------------------------------|
| `01_temperature_sensor.yaml`    | Publish DHT22 temperature/humidity values to two topics on every read.           |
| `02_thermostat_subscriber.yaml` | Subscribe to the published temperature and drive a relay; no broker in the loop. |
| `03_button_doorbell.yaml`       | Press a button → one multicast publication → every listener reacts in parallel.  |
| `04_chime_subscriber.yaml`      | Companion to 03: an RTTTL chime that joins the doorbell topic.                   |
| `05_mqtt_bridge.yaml`           | Run `mqtt:` and `multicast_pubsub:` on the same device, bridging both ways.      |
| `06_robot_coordination.yaml`    | The spec's motivating story: two devices coordinating with no cloud server.      |

For local end-to-end testing on Linux without any ESP hardware, see
`tests/publisher.yaml`, `tests/subscriber.yaml`, and `tests/README.md`. Those
use `platform: host` so they compile to a native binary and exchange
multicast traffic over the loopback / link-local interfaces.

## Patterns to know

### Floats and integers

The sensor platform encodes states as ASCII (`"%.6g"`) on the wire. To
publish a raw IEEE-754 float (4 bytes, faster, no parsing on receive):

```yaml
on_value:
  - multicast_pubsub.publish:
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
