"""Pure-Python tests of the topic-hash & CRC reference implementation.

These nail down the spec: the SHA-256 + scope nibble layout described in
``README.md`` and ``components/mpubsub/topic_hash.{h,cpp}``.
"""

from __future__ import annotations

import hashlib
import ipaddress
import zlib

import pytest

from reference import (
    SCOPE_LINK_LOCAL,
    SCOPE_ORG_LOCAL,
    SCOPE_SITE_LOCAL,
    VALID_SCOPES,
    topic_crc32,
    topic_to_group,
)


# (topic, expected_first_two_bytes_per_scope)
_TOPICS = [
    "",
    "home/livingroom/temp",
    "a" * 200,
    "家/温度",
    "topic/with spaces and !@#$%^&*()",
]


@pytest.mark.parametrize("topic", _TOPICS)
@pytest.mark.parametrize(
    "scope,prefix",
    [
        (SCOPE_LINK_LOCAL, 0xFF12),
        (SCOPE_SITE_LOCAL, 0xFF15),
        (SCOPE_ORG_LOCAL, 0xFF18),
    ],
)
def test_address_layout(topic: str, scope: int, prefix: int) -> None:
    addr = topic_to_group(topic, scope)
    raw = addr.packed
    assert raw[0] == prefix >> 8
    assert raw[1] == prefix & 0xFF
    digest = hashlib.sha256(topic.encode("utf-8")).digest()[:14]
    assert raw[2:] == digest
    assert len(raw) == 16


def test_default_scope_is_link_local() -> None:
    assert topic_to_group("foo").packed[1] == 0x12


def test_invalid_scope_raises() -> None:
    with pytest.raises(ValueError):
        topic_to_group("x", scope=0x3)


@pytest.mark.parametrize("topic", _TOPICS)
def test_crc_matches_zlib(topic: str) -> None:
    assert topic_crc32(topic) == (zlib.crc32(topic.encode("utf-8")) & 0xFFFFFFFF)


def test_distinct_topics_distinct_groups() -> None:
    a = topic_to_group("home/temp")
    b = topic_to_group("home/humidity")
    assert a != b


def test_address_is_ipv6_multicast() -> None:
    for scope in VALID_SCOPES:
        addr = topic_to_group("any/topic", scope)
        assert isinstance(addr, ipaddress.IPv6Address)
        assert addr.is_multicast


# Golden vectors. If these change, the wire format has been broken.
# Generated from the reference; locked in to guarantee cross-implementation
# byte-for-byte compatibility.
GOLDEN = [
    ("", SCOPE_SITE_LOCAL, "ff15:e3b0:c442:98fc:1c14:9afb:f4c8:996f", 0x00000000),
    (
        "home/livingroom/temp",
        SCOPE_SITE_LOCAL,
        None,  # filled in below
        zlib.crc32(b"home/livingroom/temp") & 0xFFFFFFFF,
    ),
]


def test_golden_empty_topic() -> None:
    topic, scope, expected, expected_crc = GOLDEN[0]
    assert str(topic_to_group(topic, scope)) == expected
    assert topic_crc32(topic) == expected_crc
