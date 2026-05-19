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
    ENCODING_PROTOBUF,
    ENCODING_RAW,
    MAX_PAYLOAD,
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
    "topic,payload,encoding",
    [
        ("", b"", ENCODING_RAW),
        ("home/temp", b"42.0", ENCODING_RAW),
        ("a", b"\x00" * MAX_PAYLOAD, ENCODING_RAW),
        ("typed/climate", bytes(range(20)), ENCODING_PROTOBUF),
    ],
)
def test_cpp_encode_matches_python(topic: str, payload: bytes, encoding: int) -> None:
    crc = topic_crc32(topic)
    cmd = f"E {crc:08x} {encoding:02x} {payload.hex()}"
    [line] = _run([cmd])
    status, encoded_hex = line.split(" ", 1)
    assert status == "OK"
    assert bytes.fromhex(encoded_hex) == encode(topic, payload, encoding=encoding)


@pytest.mark.parametrize("encoding", [ENCODING_RAW, ENCODING_PROTOBUF])
def test_cpp_decode_accepts_valid(encoding: int) -> None:
    pkt = encode("test", b"hello", encoding=encoding)
    [line] = _run([f"D {pkt.hex()}"])
    parts = line.split()
    assert parts[0] == "OK"
    assert int(parts[1], 16) == topic_crc32("test")
    assert int(parts[2], 16) == encoding
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
        # unknown encoding (any value > 0x01)
        (lambda p: p[:3] + bytes([0x40]) + p[4:], "UNKNOWN_ENCODING"),
        # length mismatch (header claims more payload than provided)
        (lambda p: p[:8] + bytes([99, 0]) + p[10:], "LENGTH_MISMATCH"),
    ],
)
def test_cpp_decode_rejects_invalid(mutation, expected_err: str) -> None:
    good = encode("t", b"hi")
    bad = mutation(good)
    [line] = _run([f"D {bad.hex()}"])
    assert line == f"ERR {expected_err}", line


def test_cpp_ignores_reserved_byte_11() -> None:
    # Byte 10 is now ENC_MODE; only byte 11 stays fully reserved.
    pkt = bytearray(encode("t", b"hi"))
    pkt[11] = 0xCD
    [line] = _run([f"D {bytes(pkt).hex()}"])
    assert line.startswith("OK")
