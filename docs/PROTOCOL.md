# Protocol specification (v1)

This document is the authoritative wire-protocol reference for
`multicast_pubsub`. The Python reference implementation in
[`tests/unit/reference.py`](../tests/unit/reference.py) is the executable
ground truth; the C++ implementation in
[`components/multicast_pubsub/`](../components/multicast_pubsub/) is verified
to match it byte-for-byte by [`tests/unit/test_topic_hash_cpp.py`](../tests/unit/test_topic_hash_cpp.py)
and [`tests/unit/test_wire_format_cpp.py`](../tests/unit/test_wire_format_cpp.py).

If a sentence in this document and a behavior in `reference.py` disagree,
`reference.py` wins and this document is wrong — please open a PR.

## 1. Transport

* **Protocol:** UDP/IPv6.
* **Port:** default `18512`, configurable. Both publisher and subscriber
  must use the same port. Chosen to sit one above the existing ESPHome
  `udp:` / `packet_transport:` default of 18511 (no IANA assignment in
  that neighborhood, no conflict with CoAP / mDNS).
* **Datagram size:** maximum **508 bytes** end-to-end. This matches the
  conservatively-safe IPv4 minimum-MTU figure that ESPHome's existing
  `udp:` component uses; IPv6's true minimum MTU is 1280 but we stay at 508
  for portability across encapsulation layers and to avoid IP fragmentation.

## 2. Topic → multicast group derivation

Each topic deterministically maps to a single IPv6 multicast group address:

```
group = 0xFF || 0x1<scope> || SHA-256(utf8(topic))[0..14]
```

| Bits      | Field                | Value                                       |
|-----------|----------------------|---------------------------------------------|
| 0..7      | Multicast prefix     | `0xFF` (IPv6 multicast)                     |
| 8..11     | Flags nibble (`T=1`) | `0x1` (transient, per RFC 4291 §2.7)        |
| 12..15    | Scope nibble         | `0x2`, `0x5`, or `0x8` (see §2.1)           |
| 16..127   | Topic hash           | `SHA-256(utf8(topic))[0..14]` (112 bits)    |

The 112-bit topic hash matches the original disclosure exactly. Truncating
SHA-256 is safe for hash-table-style mapping: birthday-style collisions
require ~`2^56` distinct topics on a single network, which is far beyond
realistic.

### 2.1 Scopes

| Name                 | Nibble | Address prefix | Travels over                                  |
|----------------------|:------:|----------------|-----------------------------------------------|
| `link-local`         | `0x2`  | `ff12::/16`    | A single L2 segment / VLAN. **Default.** Not forwarded by routers. |
| `site-local`         | `0x5`  | `ff15::/16`    | A single administrative site, requires MLD-snooping for VLAN-crossing. |
| `organization-local` | `0x8`  | `ff18::/16`    | Bridged sites with multicast routing config.  |

Choose the smallest scope that covers all participating devices. A smaller
scope reduces the multicast routing surface area and bandwidth across
uninvolved switches.

### 2.2 Worked example

```
topic = "home/livingroom/temp"
sha256(b"home/livingroom/temp") = e7e87b62 a3d2a7c2 ...    (32 bytes)
take first 14 bytes               e7e87b62 a3d2a7c2 ...    (the hash bits)
prefix                            ff12:                    (link-local)
result                            ff12:e7e8:7b62:a3d2:a7c2:...
```

The canonical fingerprint for this topic on a link-local network is:

```
[ff12:e7e8:7b62:a3d2:a7c2:...]:18512
```

Use `python3 tests/probe.py --topic 'home/livingroom/temp' --scope link-local`
to compute it locally; the probe prints the group address as it starts.

## 3. Packet format

```
 byte:  0    1    2    3    4    5    6    7    8    9   10   11   12 ...
       +----+----+----+----+----+----+----+----+----+----+----+----+----+
       | 'M'| 'P'| VER| FLG|        TOPIC_CRC32         | PAY_LEN  | RSV | PAYLOAD ...
       +----+----+----+----+----+----+----+----+----+----+----+----+----+
```

Total: **12-byte header + up to 496 bytes payload = 508-byte datagram.**

| Offset | Size | Field        | Notes                                                          |
|-------:|-----:|--------------|----------------------------------------------------------------|
|      0 |    2 | `MAGIC`      | ASCII `"MP"` (`0x4D 0x50`)                                     |
|      2 |    1 | `VERSION`    | `0x01` for v1                                                  |
|      3 |    1 | `FLAGS`      | See §3.1                                                       |
|      4 |    4 | `TOPIC_CRC32`| Little-endian CRC-32/IEEE 802.3 of the UTF-8 topic string      |
|      8 |    2 | `PAYLOAD_LEN`| Little-endian uint16; must equal `len(datagram) - 12`          |
|     10 |    2 | `RESERVED`   | Senders MUST write `0x00 0x00`. Receivers MUST ignore.         |
|     12 |  ≤496| `PAYLOAD`    | Application-defined bytes                                      |

### 3.1 FLAGS

| Bit  | Mask  | Name                | Meaning                                                  |
|-----:|:-----:|---------------------|----------------------------------------------------------|
|    0 | 0x01  | `FLAG_TEXT`         | Payload is UTF-8 text. Informational only — receivers may use it as a hint for logging or display. |
|    1 | 0x02  | `FLAG_RETAIN_HINT`  | Sender intends this to be a sticky-state message. **Multicast cannot truly retain** — receivers that miss the packet miss the state — but the flag is preserved for bridges (e.g. an MQTT bridge can set `retain=true`). |
| 2..7 | 0xFC  | reserved            | Senders MUST write `0`. Receivers MUST drop packets where any reserved bit is set. |

### 3.2 TOPIC_CRC32

The CRC is computed over the UTF-8 bytes of the topic string, **not** the
topic hash; it's identical to `zlib.crc32(topic.encode("utf-8"))` and the
ESPHome `esphome::crc32()` helper.

It exists to disambiguate the (vanishingly rare) case where two different
topics map to the same 112-bit IPv6 group: a subscriber that joined group
G because it cares about topic A will receive a datagram for unrelated
topic B if some publisher elsewhere maps B onto the same G. The receiver
compares the incoming `TOPIC_CRC32` to the CRC32 of every topic it has
subscribed to; on mismatch it silently drops the datagram.

A 112-bit address hash plus a 32-bit topic CRC means a false positive
requires colliding 144 bits, well above any conceivable network's
collision floor.

## 4. Validation rules

A receiver MUST silently drop a datagram (and SHOULD `ESP_LOGV` the
reason) if any of the following is true:

1. `len(datagram) < 12` — header truncated.
2. `datagram[0..2] != b"MP"` — bad magic.
3. `datagram[2] != 0x01` — unknown version.
4. `(datagram[3] & 0xFC) != 0` — reserved flag bit set.
5. `12 + PAYLOAD_LEN != len(datagram)` — length mismatch.
6. `TOPIC_CRC32` matches none of the topics this node has subscribed to.

The C++ implementation surfaces (1)–(5) as a `DecodeError` enum
(`TOO_SHORT`, `BAD_MAGIC`, `BAD_VERSION`, `RESERVED_FLAGS`,
`LENGTH_MISMATCH`); see `components/multicast_pubsub/wire_format.h`.

## 5. Sender requirements

A publisher MUST:

* Reject a payload larger than **496 bytes**. The Python codegen for the
  `multicast_pubsub.publish:` action rejects literal payloads above that
  size at config time. The runtime rejects oversize lambda payloads at
  `publish()` time and logs an `ERROR`.
* Reject `FLAGS` values with any bit in `0xFC` set (same rule as receivers
  for forward compatibility).
* Set `RESERVED` bytes (offsets 10–11) to `0x00 0x00`.
* Use the same UDP port the subscribers are listening on (default 18512).
* Set IPv6 multicast hop limit (`IPV6_MULTICAST_HOPS`). The component
  defaults to `1` (one hop, so the datagram never leaves the local link);
  raise it via the `hops:` YAML option if you need cross-subnet delivery
  with a wider scope.

## 6. Receiver requirements

A subscriber MUST:

* Bind a UDP/IPv6 socket to `[::]:port`.
* Set `SO_REUSEADDR` (so multiple subscribers can coexist on one host).
* For each subscribed topic, `setsockopt(IPPROTO_IPV6, IPV6_JOIN_GROUP, ...)`
  with the topic's derived group address.
* On each received datagram, run the validation rules in §4. On success,
  deliver the payload to every callback registered for the matching topic.

## 7. Out of scope (v1)

The following are **not** part of this protocol revision. A v2 may add
them; if so it will bump `VERSION` to `0x02`.

* **Encryption / authentication.** A v1 datagram is unauthenticated. Any
  node on the multicast scope can publish to any topic. If you need
  authenticity, layer it inside the payload (signed messages, MAC) or run
  the protocol on an isolated/encrypted L2 (e.g. WireGuard).
* **MQTT-style wildcards.** Each subscription is one exact topic. Bridges
  (see [`examples/05_mqtt_bridge.yaml`](../examples/05_mqtt_bridge.yaml))
  can fan out wildcards on the broker side.
* **Retain / Last Will.** Multicast UDP is fire-and-forget. The
  `FLAG_RETAIN_HINT` bit is a hint for bridges, nothing more.
* **Acknowledgements.** No `PUBACK`-equivalent.
* **IPv4.** Defining an mDNS-coordinated IPv4 mapping (per the original
  disclosure) is left to a future version. Right now IPv4-only networks
  cannot participate.
* **Fragmentation.** A single publication is a single datagram; if your
  payload won't fit in 496 bytes, chunk it at the application layer.

## 8. Differences from the original disclosure

The protocol implemented here is a concrete refinement of the
self-organizing publish/subscribe disclosure (Genera, Technical Disclosure
Commons Art. 5601, 2022). Specific choices made here that the disclosure
left open:

* **Hash function:** SHA-256 (truncated to 112 bits) — chosen for
  ubiquitous availability and good distribution.
* **Header layout:** the disclosure's Fig. 2 shows generic UDP fields
  (`SOURCE | DESTN | LENGTH | CHKSUM | PAYLOAD`) — the IP/UDP outer header
  — and notes that a topic CRC should be embedded "in the published
  message". We implement that as the 12-byte application-layer header
  documented here.
* **Default port:** 18512, picked to sit adjacent to ESPHome's existing
  `udp:` / `packet_transport:` default of 18511 — no IANA assignment in
  that neighborhood, no clash with CoAP (5683) or mDNS (5353).
* **Default scope:** link-local (`ff12::`), since it Just Works on every
  flat LAN without needing MLD-snooping configuration. Bump to
  `site-local` for multi-VLAN deployments.
