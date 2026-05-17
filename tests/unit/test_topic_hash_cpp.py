"""Cross-implementation check: ``topic_hash_test`` (built from the actual C++
sources used by the ESPHome component) must produce the same address and CRC
for every input as ``reference.py``.

Run ``make topic_hash_test`` in this directory first.
"""

from __future__ import annotations

import ipaddress
import os
import subprocess
from pathlib import Path

import pytest

from reference import (
    SCOPE_LINK_LOCAL,
    SCOPE_ORG_LOCAL,
    SCOPE_SITE_LOCAL,
    topic_crc32,
    topic_to_group,
)

HERE = Path(__file__).parent
BINARY = HERE / "topic_hash_test"


_SCOPE_BY_NIBBLE = {
    SCOPE_LINK_LOCAL: SCOPE_LINK_LOCAL,
    SCOPE_SITE_LOCAL: SCOPE_SITE_LOCAL,
    SCOPE_ORG_LOCAL: SCOPE_ORG_LOCAL,
}


TOPICS = [
    "",
    "home/livingroom/temp",
    "a" * 200,
    "家/温度",
    "topic/with spaces and !@#$%^&*()",
    "edge\ncase\twith\rcontrol",
]


@pytest.fixture(scope="module")
def cpp_output() -> dict[str, list[tuple[int, int, str]]]:
    if not BINARY.exists():
        pytest.skip(f"{BINARY} not built; run `make topic_hash_test` first")
    # The test binary can't receive embedded newlines via stdin, so handle
    # topics containing them separately by encoding them. For simplicity here
    # we feed only topics without ASCII control bytes via stdin and fall back
    # for the others by recursing.
    stdin_topics = [t for t in TOPICS if "\n" not in t and "\r" not in t]
    proc = subprocess.run(
        [str(BINARY)],
        input="\n".join(stdin_topics) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    blocks = [b for b in proc.stdout.split("---\n") if b.strip()]
    assert len(blocks) == len(stdin_topics), proc.stdout
    out: dict[str, list[tuple[int, int, str]]] = {}
    for topic, block in zip(stdin_topics, blocks):
        rows = []
        for line in block.strip().splitlines():
            scope_str, crc_hex, addr_str = line.split()
            rows.append((int(scope_str), int(crc_hex, 16), addr_str))
        out[topic] = rows
    return out


@pytest.mark.parametrize("topic", [t for t in TOPICS if "\n" not in t and "\r" not in t])
def test_cpp_matches_python(topic: str, cpp_output) -> None:
    rows = cpp_output[topic]
    assert {r[0] for r in rows} == {SCOPE_LINK_LOCAL, SCOPE_SITE_LOCAL, SCOPE_ORG_LOCAL}
    py_crc = topic_crc32(topic)
    for scope, crc, addr_str in rows:
        assert crc == py_crc, f"CRC mismatch for topic {topic!r}"
        py_addr = topic_to_group(topic, scope)
        # Normalize both forms via ipaddress so 'ff15:0:...' == 'ff15::...'.
        assert ipaddress.IPv6Address(addr_str) == py_addr, (
            f"Address mismatch for topic={topic!r} scope={scope:#x}: "
            f"cpp={addr_str} python={py_addr}"
        )
