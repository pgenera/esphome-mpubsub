"""Python reference implementation of the mpubsub wire protocol.

This is the source of truth that the C++ implementation in
``components/mpubsub/`` must match byte-for-byte. It is intentionally
free of any ESPHome dependency so it can also be used by:

  * standalone bridges (e.g. an MQTT <-> multicast pub/sub gateway)
  * the wire-format unit tests (``tests/unit/test_wire_format.py``)
  * the probe / smoke-test tool (``tests/probe.py``)
"""

from __future__ import annotations

import hashlib
import ipaddress
import struct
import zlib
from dataclasses import dataclass

MAGIC = b"MP"
VERSION = 0x01
HEADER_LEN = 12
MAX_DATAGRAM = 1232  # IPv6 min MTU (1280, RFC 8200 §5) - 40 (IPv6) - 8 (UDP)
MAX_PAYLOAD = MAX_DATAGRAM - HEADER_LEN  # = 1220

# Body-encoding enum -- one of these goes in header byte 3.
ENCODING_RAW = 0x00
ENCODING_PROTOBUF = 0x01
KNOWN_ENCODINGS = (ENCODING_RAW, ENCODING_PROTOBUF)
# Values 0x02..0xFF are reserved; receivers MUST drop unknown encodings.

SCOPE_LINK_LOCAL = 0x2
SCOPE_SITE_LOCAL = 0x5
SCOPE_ORG_LOCAL = 0x8
VALID_SCOPES = (SCOPE_LINK_LOCAL, SCOPE_SITE_LOCAL, SCOPE_ORG_LOCAL)

DEFAULT_PORT = 18512


def topic_to_group(topic: str, scope: int = SCOPE_LINK_LOCAL) -> ipaddress.IPv6Address:
    """Map a topic string to an IPv6 multicast address.

    The 128-bit address layout is::

        byte 0      : 0xFF                         (multicast prefix)
        byte 1 hi   : 0x1                          (T=1, transient)
        byte 1 lo   : scope nibble (0x2/0x5/0x8)
        bytes 2..15 : SHA-256(utf8 topic)[0..14]   (112-bit topic hash)
    """
    if scope not in VALID_SCOPES:
        raise ValueError(f"Invalid scope nibble {scope:#x}")
    digest = hashlib.sha256(topic.encode("utf-8")).digest()[:14]
    first = 0xFF
    second = (0x1 << 4) | (scope & 0xF)
    return ipaddress.IPv6Address(bytes((first, second)) + digest)


def topic_crc32(topic: str) -> int:
    """CRC-32/IEEE-802.3 of the UTF-8 topic, identical to ``esphome::crc32``."""
    return zlib.crc32(topic.encode("utf-8")) & 0xFFFFFFFF


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    encoding: int = ENCODING_RAW


def encode(topic: str, payload: bytes, encoding: int = ENCODING_RAW) -> bytes:
    """Serialize a publication to the on-wire byte sequence.

    Raises ``ValueError`` if the payload exceeds :data:`MAX_PAYLOAD` or the
    encoding value is unknown.
    """
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too large ({len(payload)} > {MAX_PAYLOAD})")
    if encoding not in KNOWN_ENCODINGS:
        raise ValueError(f"unknown encoding: {encoding:#04x}")
    crc = topic_crc32(topic)
    # 12-byte header: MAGIC(2) VER(1) ENC(1) CRC(4 LE) PAYLOAD_LEN(2 LE) RESERVED(2)
    header = (
        MAGIC
        + bytes((VERSION, encoding & 0xFF))
        + struct.pack("<IH", crc, len(payload))
        + b"\x00\x00"
    )
    assert len(header) == HEADER_LEN
    return header + payload


class WireError(ValueError):
    """Raised by :func:`decode` when a packet violates the spec."""


def decode(data: bytes) -> tuple[int, int, bytes]:
    """Parse a datagram.

    Returns ``(topic_crc32, encoding, payload)``.

    Raises :class:`WireError` if any validation rule fails. The caller is
    expected to match ``topic_crc32`` against the subscriptions on this node.
    """
    if len(data) < HEADER_LEN:
        raise WireError(f"datagram too short ({len(data)} < {HEADER_LEN})")
    if data[0:2] != MAGIC:
        raise WireError(f"bad magic {data[0:2]!r}")
    version = data[2]
    if version != VERSION:
        raise WireError(f"unsupported version {version}")
    encoding = data[3]
    if encoding not in KNOWN_ENCODINGS:
        raise WireError(f"unknown encoding: {encoding:#04x}")
    crc, payload_len = struct.unpack("<IH", data[4:10])
    # Bytes 10-11 are reserved; receivers ignore their value to allow
    # forward-compatible extensions, matching the C++ decoder.
    if HEADER_LEN + payload_len != len(data):
        raise WireError(
            f"length mismatch: header says {payload_len}, datagram has "
            f"{len(data) - HEADER_LEN}"
        )
    return crc, encoding, data[HEADER_LEN:]
