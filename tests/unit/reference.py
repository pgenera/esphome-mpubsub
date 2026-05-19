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

# Encryption mode enum -- one of these goes in header byte 10.
ENC_MODE_NONE = 0x00
ENC_MODE_XXTEA = 0x01
KNOWN_ENC_MODES = (ENC_MODE_NONE, ENC_MODE_XXTEA)

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


# ----------------------------------------------------------------------------
# XXTEA-256
#
# Block-cipher operating in place on a vector of uint32_t words. Matches the
# reference algorithm used by esphome::xxtea (which packet_transport reuses).
# 256-bit key = 8 uint32 words.
# ----------------------------------------------------------------------------

_DELTA = 0x9E3779B9


def _xxtea_mx(z: int, y: int, sum_: int, p: int, e: int, k: list[int]) -> int:
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((sum_ ^ y) + (k[(p & 3) ^ e] ^ z))) & 0xFFFFFFFF


def xxtea_encrypt(words: list[int], key: list[int]) -> None:
    """In-place XXTEA encrypt of ``words`` (uint32 list) under ``key`` (8 uint32s)."""
    n = len(words)
    if n < 2:
        raise ValueError("XXTEA requires at least 2 words")
    rounds = 6 + 52 // n
    sum_ = 0
    z = words[n - 1]
    for _ in range(rounds):
        sum_ = (sum_ + _DELTA) & 0xFFFFFFFF
        e = (sum_ >> 2) & 3
        for p in range(n - 1):
            y = words[p + 1]
            words[p] = (words[p] + _xxtea_mx(z, y, sum_, p, e, key)) & 0xFFFFFFFF
            z = words[p]
        y = words[0]
        words[n - 1] = (words[n - 1] + _xxtea_mx(z, y, sum_, n - 1, e, key)) & 0xFFFFFFFF
        z = words[n - 1]


def xxtea_decrypt(words: list[int], key: list[int]) -> None:
    """In-place XXTEA decrypt of ``words`` under ``key``."""
    n = len(words)
    if n < 2:
        raise ValueError("XXTEA requires at least 2 words")
    rounds = 6 + 52 // n
    sum_ = (rounds * _DELTA) & 0xFFFFFFFF
    y = words[0]
    for _ in range(rounds):
        e = (sum_ >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = words[p - 1]
            words[p] = (words[p] - _xxtea_mx(z, y, sum_, p, e, key)) & 0xFFFFFFFF
            y = words[p]
        z = words[n - 1]
        words[0] = (words[0] - _xxtea_mx(z, y, sum_, 0, e, key)) & 0xFFFFFFFF
        y = words[0]
        sum_ = (sum_ - _DELTA) & 0xFFFFFFFF


def derive_key(passphrase: str) -> bytes:
    """Hash a user passphrase to the 32-byte XXTEA-256 key.

    Matches ``hashlib.sha256(passphrase).digest()`` -- the same key
    derivation packet_transport uses for its ``encryption.key`` option.
    """
    return hashlib.sha256(passphrase.encode("utf-8")).digest()


def _bytes_to_words(b: bytes) -> list[int]:
    if len(b) % 4 != 0:
        raise ValueError(f"length {len(b)} is not a multiple of 4")
    return list(struct.unpack(f"<{len(b) // 4}I", b))


def _words_to_bytes(words: list[int]) -> bytes:
    return struct.pack(f"<{len(words)}I", *words)


def xxtea_ciphertext_len(plaintext_len: int) -> int:
    """Length of the ciphertext for an mpubsub payload of ``plaintext_len`` bytes.

    The plaintext is ``[topic_crc32 LE (4 bytes)] || payload``, zero-padded
    up to a multiple of 4 bytes (XXTEA word size), with an 8-byte floor.
    """
    needed = plaintext_len + 4
    if needed < 8:
        return 8
    return (needed + 3) & ~3


@dataclass(frozen=True)
class Message:
    topic: str
    payload: bytes
    encoding: int = ENCODING_RAW


def encode(
    topic: str,
    payload: bytes,
    encoding: int = ENCODING_RAW,
    *,
    key: bytes | None = None,
) -> bytes:
    """Serialize a publication to the on-wire byte sequence.

    Raises ``ValueError`` if the payload exceeds :data:`MAX_PAYLOAD`, the
    encoding value is unknown, or (when ``key`` is set) the encrypted
    datagram would exceed :data:`MAX_DATAGRAM`.

    When ``key`` is set, the body is XXTEA-256 ciphertext over
    ``[topic_crc32 LE || payload || zero pad]``. The cleartext header's
    TOPIC_CRC32 field is set to zero; the real CRC32 is the first 4 bytes
    of the decrypted plaintext.
    """
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too large ({len(payload)} > {MAX_PAYLOAD})")
    if encoding not in KNOWN_ENCODINGS:
        raise ValueError(f"unknown encoding: {encoding:#04x}")
    crc = topic_crc32(topic)
    if key is None:
        enc_mode = ENC_MODE_NONE
        header_crc = crc
        body = payload
    else:
        if len(key) != 32:
            raise ValueError(f"key must be 32 bytes, got {len(key)}")
        clen = xxtea_ciphertext_len(len(payload))
        if HEADER_LEN + clen > MAX_DATAGRAM:
            raise ValueError(
                f"encrypted payload too large ({len(payload)} -> {clen}-byte ciphertext)"
            )
        plaintext = struct.pack("<I", crc) + payload + b"\x00" * (clen - 4 - len(payload))
        words = _bytes_to_words(plaintext)
        xxtea_encrypt(words, _bytes_to_words(key))
        body = _words_to_bytes(words)
        enc_mode = ENC_MODE_XXTEA
        header_crc = 0
    # 12-byte header: MAGIC(2) VER(1) ENC(1) CRC(4 LE) PAYLOAD_LEN(2 LE) ENM(1) RSV(1)
    header = (
        MAGIC
        + bytes((VERSION, encoding & 0xFF))
        + struct.pack("<IH", header_crc, len(payload))
        + bytes((enc_mode, 0))
    )
    assert len(header) == HEADER_LEN
    return header + body


class WireError(ValueError):
    """Raised by :func:`decode` when a packet violates the spec."""


def decode(data: bytes, *, key: bytes | None = None) -> tuple[int, int, bytes]:
    """Parse a datagram.

    Returns ``(topic_crc32, encoding, payload)``.

    For encrypted packets the caller MUST supply ``key`` (the 32-byte
    XXTEA-256 key); the returned ``topic_crc32`` is recovered from the
    decrypted plaintext and ``payload`` is the decrypted slice.

    Raises :class:`WireError` if any validation rule fails or if an
    encrypted packet arrives with ``key=None``. The caller is expected to
    match ``topic_crc32`` against the subscriptions on this node.
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
    enc_mode = data[10]
    if enc_mode not in KNOWN_ENC_MODES:
        raise WireError(f"unknown enc_mode: {enc_mode:#04x}")
    header_crc, payload_len = struct.unpack("<IH", data[4:10])
    # byte 11 is reserved; ignored on decode for forward-compatibility.
    if enc_mode == ENC_MODE_XXTEA:
        expected = HEADER_LEN + xxtea_ciphertext_len(payload_len)
        if len(data) != expected:
            raise WireError(
                f"encrypted length mismatch: header says {payload_len} -> "
                f"{expected - HEADER_LEN}-byte ciphertext, datagram has "
                f"{len(data) - HEADER_LEN}"
            )
        if key is None:
            raise WireError("encrypted packet but no key supplied")
        if len(key) != 32:
            raise ValueError(f"key must be 32 bytes, got {len(key)}")
        words = _bytes_to_words(data[HEADER_LEN:])
        xxtea_decrypt(words, _bytes_to_words(key))
        plaintext = _words_to_bytes(words)
        crc = struct.unpack("<I", plaintext[0:4])[0]
        body = plaintext[4 : 4 + payload_len]
        return crc, encoding, body
    # Plaintext path
    if HEADER_LEN + payload_len != len(data):
        raise WireError(
            f"length mismatch: header says {payload_len}, datagram has "
            f"{len(data) - HEADER_LEN}"
        )
    return header_crc, encoding, data[HEADER_LEN:]
