"""Cross-implementation check: ``wire_format_test`` (the actual C++
encoder/decoder used by the ESPHome component) must agree with the Python
reference for every encode and every validation rule.

Run ``make wire_format_test`` first.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from reference import (
    FLAG_RETAIN_HINT,
    FLAG_TEXT,
    HEADER_LEN,
    MAX_PAYLOAD,
    WireError,
    decode,
    encode,
    topic_crc32,
)

HERE = Path(__file__).parent
BINARY = HERE / "wire_format_test"


def _run(commands: list[str]) -> list[str]:
    if not BINARY.exists():
        pytest.skip(f"{BINARY} not built; run `make wire_format_test` first")
    proc = subprocess.run(
        [str(BINARY)],
        input="\n".join(commands) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return proc.stdout.strip().splitlines()


@pytest.mark.parametrize(
    "topic,payload,flags",
    [
        ("", b"", 0),
        ("home/temp", b"42.0", FLAG_TEXT),
        ("a", b"\x00" * MAX_PAYLOAD, 0),
        ("retain/hint", b"x", FLAG_RETAIN_HINT),
    ],
)
def test_cpp_encode_matches_python(topic: str, payload: bytes, flags: int) -> None:
    crc = topic_crc32(topic)
    cmd = f"E {crc:08x} {flags:02x} {payload.hex()}"
    [line] = _run([cmd])
    status, encoded_hex = line.split(" ", 1)
    assert status == "OK"
    assert bytes.fromhex(encoded_hex) == encode(topic, payload, flags=flags)


def test_cpp_decode_accepts_valid() -> None:
    pkt = encode("test", b"hello", flags=FLAG_TEXT)
    [line] = _run([f"D {pkt.hex()}"])
    parts = line.split()
    assert parts[0] == "OK"
    assert int(parts[1], 16) == topic_crc32("test")
    assert int(parts[2], 16) == FLAG_TEXT
    assert bytes.fromhex(parts[3]) == b"hello"


@pytest.mark.parametrize(
    "mutation,expected_err",
    [
        # too short
        (lambda p: bytes([0x4D, 0x50, 0x01]), "TOO_SHORT"),
        # bad magic
        (lambda p: b"XX" + p[2:], "BAD_MAGIC"),
        # bad version
        (lambda p: p[:2] + bytes([99]) + p[3:], "BAD_VERSION"),
        # reserved flag set
        (lambda p: p[:3] + bytes([0x40]) + p[4:], "RESERVED_FLAGS"),
        # length mismatch (header claims more payload than provided)
        (lambda p: p[:8] + bytes([99, 0]) + p[10:], "LENGTH_MISMATCH"),
    ],
)
def test_cpp_decode_rejects_invalid(mutation, expected_err: str) -> None:
    good = encode("t", b"hi")
    bad = mutation(good)
    [line] = _run([f"D {bad.hex()}"])
    assert line == f"ERR {expected_err}", line


def test_cpp_ignores_reserved_bytes() -> None:
    pkt = bytearray(encode("t", b"hi"))
    pkt[10] = 0xAB
    pkt[11] = 0xCD
    [line] = _run([f"D {bytes(pkt).hex()}"])
    assert line.startswith("OK")
