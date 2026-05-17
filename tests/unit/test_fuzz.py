"""Fuzz tests targeting the **C++** implementation under ASan + UBSan.

The C++ code in ``components/multicast_pubsub/`` is what actually runs
on user devices; the Python ``reference.py`` exists as a spec checker.
This file's job is to throw garbage at the C++ decoder, encoder, and
topic-hash code -- with AddressSanitizer and UndefinedBehaviorSanitizer
turned on -- and assert that nothing crashes, no out-of-bounds reads
happen, no signed-overflow UB fires, no alignment violations, etc.

Requires the sanitizer-build harnesses:
    make wire_format_test_san topic_hash_test_san

Coverage strategies:
  1. Pure random bytes of varying lengths (including pathological).
  2. Mutations of known-valid packets (bit flips, truncations, appends).
  3. Adversarial crafts -- enormous payload_len, every possible encoding
     byte, header-only / header-minus-one truncation, etc.
  4. Random topic strings fed to topic_to_group / topic_crc32.
  5. Garbage on the harness's own command channel (truncated lines,
     invalid hex, etc.) -- not part of the protocol but part of the
     attack surface during testing.

Iteration count is governed by ``FUZZ_ITERS`` (default 5000). Each
test runs that many inputs through one subprocess invocation of the
sanitizer harness; with this many inputs and AS/UBSan instrumentation,
the C++ code gets meaningful coverage in a few seconds.

If a regression is found, the failing test's seed (in the parametrize
entry) deterministically reproduces it.
"""

from __future__ import annotations

import os
import random
import struct
import subprocess
from pathlib import Path

import pytest

from reference import (
    ENCODING_PROTOBUF,
    ENCODING_RAW,
    HEADER_LEN,
    KNOWN_ENCODINGS,
    MAX_DATAGRAM,
    MAX_PAYLOAD,
    VERSION,
    encode,
)

HERE = Path(__file__).parent
WIRE_BIN_SAN = HERE / "wire_format_test_san"
HASH_BIN_SAN = HERE / "topic_hash_test_san"

FUZZ_ITERS = int(os.environ.get("FUZZ_ITERS", "5000"))

_SAN_ENV = {
    # Don't abort on the first sanitizer hit -- keep going so one run
    # can surface multiple bugs. exitcode=42 lets us reliably detect
    # that the sanitizer reported anything.
    "ASAN_OPTIONS": "abort_on_error=0:halt_on_error=0:exitcode=42",
    "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=0:exitcode=42",
}

_SAN_BANNERS = (
    "AddressSanitizer",
    "UndefinedBehaviorSanitizer",
    "LeakSanitizer",
    "runtime error",
    "SEGV",
    "stack-buffer-overflow",
    "heap-buffer-overflow",
    "global-buffer-overflow",
    "use-after-free",
    "ERROR: ",
)


def _run_san(binary: Path, stdin_data: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(binary)],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={**os.environ, **_SAN_ENV},
    )


def _assert_no_san_report(proc: subprocess.CompletedProcess[str], context: str) -> None:
    combined = proc.stderr + proc.stdout
    for banner in _SAN_BANNERS:
        if banner in combined:
            pytest.fail(
                f"Sanitizer reported '{banner}' on {context}:\n"
                f"--- stderr ({len(proc.stderr)} bytes) ---\n{proc.stderr[:4000]}\n"
                f"--- stdout (tail 1KB) ---\n{proc.stdout[-1024:]}"
            )
    if proc.returncode != 0:
        pytest.fail(
            f"harness exited {proc.returncode} on {context} (expected 0):\n"
            f"{proc.stderr[:4000]}"
        )


# ---------------------------------------------------------------------------
# C++ wire_format decoder fuzzing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def wire_san_available() -> None:
    if not WIRE_BIN_SAN.exists():
        pytest.skip(
            f"{WIRE_BIN_SAN.name} not built; run `make wire_format_test_san` first"
        )


@pytest.mark.parametrize("seed", [101, 202, 303, 4444, 55555])
def test_cpp_decoder_random_bytes(wire_san_available, seed: int) -> None:
    """Throw FUZZ_ITERS random byte sequences (length 0..MAX_DATAGRAM+100)
    at the C++ decoder. Any sanitizer report = test failure."""
    rng = random.Random(seed)
    commands = []
    for _ in range(FUZZ_ITERS):
        n = rng.randint(0, MAX_DATAGRAM + 100)
        buf = bytes(rng.randint(0, 255) for _ in range(n))
        commands.append(f"D {buf.hex()}")
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"random-bytes seed={seed} count={len(commands)}")


@pytest.mark.parametrize("seed", [13, 26, 39, 52])
def test_cpp_decoder_valid_envelope_invalid_payload(wire_san_available, seed: int) -> None:
    """Headers that pass every envelope check (correct magic, version,
    valid encoding, length consistent, plausible CRC) but with random
    payload bytes inside. This category targets bugs downstream of the
    envelope -- protobuf decoders, ASCII parsers, schema-id consumers
    -- that random bytes never reach because they're rejected before
    they make it past the wire format.

    Today the C++ wire-format decoder is encoding-agnostic about
    payload contents, so the relevant invariant is "OK responses come
    back consistent regardless of payload bytes." Once protobuf
    decoding lands, this same harness exercises the proto parser too."""
    rng = random.Random(seed)
    commands = []
    expected_oks = 0
    for _ in range(FUZZ_ITERS):
        # Use a real topic so TOPIC_CRC32 is plausible (decoder doesn't
        # validate it against subscriptions anyway, but exercising real
        # CRCs ensures we'd notice if that ever changed).
        topic = f"fuzz/envelope/{rng.randint(0, 999)}"
        encoding = rng.choice(list(KNOWN_ENCODINGS))
        # Payload sized from 0 up to MAX_PAYLOAD, contents pure random.
        n = rng.randint(0, MAX_PAYLOAD)
        garbage_payload = bytes(rng.randint(0, 255) for _ in range(n))
        # Build a fully-valid envelope around the garbage. Using encode()
        # guarantees the header is well-formed.
        packet = encode(topic, garbage_payload, encoding=encoding)
        commands.append(f"D {packet.hex()}")
        expected_oks += 1

    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"valid-envelope+random-payload seed={seed}")

    # Every input had a valid envelope, so EVERY response should be OK
    # (with whatever garbage the payload contained echoed back).
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == expected_oks
    ok_count = sum(1 for l in lines if l.startswith("OK"))
    assert ok_count == expected_oks, (
        f"valid envelopes were rejected: {expected_oks - ok_count} of "
        f"{expected_oks} got ERR"
    )


def test_cpp_decoder_valid_envelope_adversarial_payloads(wire_san_available) -> None:
    """Hand-picked nasty payloads inside valid envelopes -- specifically
    targeting future protobuf/text consumers with the kinds of bytes
    that have historically broken parsers."""
    topic = "fuzz/envelope/adversarial"
    adversarial_payloads = [
        b"",                                      # empty
        b"\x00",                                  # single NUL
        b"\x00" * 1000,                           # NUL flood
        b"\xff" * 1000,                           # 0xFF flood
        b"\x80\x80\x80\x80\x80\x80\x80\x80",      # protobuf-looking varint bytes
        # protobuf wire-type tags that would confuse a decoder
        bytes([0x08, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x7f]),
        # length-delimited tag claiming a huge string
        bytes([0x0a, 0xff, 0xff, 0xff, 0x0f]) + b"short",
        # nested-message looking
        bytes([0x12, 0x05, 0x08, 0x01, 0x12, 0x01, 0x42]),
        # very long string of ASCII that strtof / atoi might mishandle
        b"1" * 500,
        b"1e9999999999999999",                    # overflow in float parser
        b"-" * 200 + b"1",                        # many leading dashes
        b"NaN" + b"\x00" * 100,                   # NaN followed by NULs
        # high-bit utf-8 sequences
        b"\xc3\xa9" * 200,                        # valid utf-8 "é" repeated
        b"\xed\xa0\x80",                          # invalid utf-8 (lone surrogate)
        b"\xff\xfe\x00\x00",                      # BOM-ish, NUL-terminated
    ]
    commands = []
    for p in adversarial_payloads:
        if len(p) > MAX_PAYLOAD:
            p = p[:MAX_PAYLOAD]
        for encoding in KNOWN_ENCODINGS:
            commands.append(f"D {encode(topic, p, encoding=encoding).hex()}")
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"adversarial payloads in valid envelopes ({len(commands)} cases)")
    # All should decode OK -- the envelope is valid in every case.
    lines = proc.stdout.strip().splitlines()
    assert all(l.startswith("OK") for l in lines), [l for l in lines if not l.startswith("OK")][:5]


@pytest.mark.parametrize("seed", [7, 88, 333, 4242])
def test_cpp_decoder_mutated_valid(wire_san_available, seed: int) -> None:
    """Start from a valid packet, apply 1-5 random mutations, hand it to
    the C++ decoder. Catches the case where the decoder mishandles
    slightly-corrupted-but-not-random input -- the realistic failure mode."""
    rng = random.Random(seed)
    base_payloads = [b"", b"x", b"hello world", b"\xff" * 256, b"\x00" * MAX_PAYLOAD]
    commands = []
    for _ in range(FUZZ_ITERS):
        payload = rng.choice(base_payloads)
        topic = "fuzz/" + "".join(
            chr(rng.randint(0x20, 0x7E)) for _ in range(rng.randint(0, 30))
        )
        enc = rng.choice(list(KNOWN_ENCODINGS))
        good = bytearray(encode(topic, payload, encoding=enc))
        for _ in range(rng.randint(1, 5)):
            op = rng.choice(["flip", "truncate", "append", "zero", "max"])
            if op == "flip" and good:
                good[rng.randint(0, len(good) - 1)] ^= 1 << rng.randint(0, 7)
            elif op == "truncate" and good:
                good = good[: rng.randint(0, len(good))]
            elif op == "append":
                good.extend(rng.randint(0, 255) for _ in range(rng.randint(1, 32)))
            elif op == "zero" and good:
                good[rng.randint(0, len(good) - 1)] = 0
            elif op == "max" and good:
                good[rng.randint(0, len(good) - 1)] = 0xFF
        commands.append(f"D {bytes(good).hex()}")
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"mutated-valid seed={seed} count={len(commands)}")


def test_cpp_decoder_adversarial_lengths(wire_san_available) -> None:
    """Hand-picked edge cases around size and header bounds."""
    cases = [
        b"",
        b"\x00" * (HEADER_LEN - 1),
        # exact-header, zero payload
        bytes([0x4D, 0x50, VERSION, ENCODING_RAW, 0, 0, 0, 0, 0, 0, 0, 0]),
        # claims 65535 bytes of payload, none provided
        bytes([0x4D, 0x50, VERSION, ENCODING_RAW, 0, 0, 0, 0, 0xFF, 0xFF, 0, 0]),
        # claims 0 bytes of payload but trailing data
        bytes([0x4D, 0x50, VERSION, ENCODING_RAW, 0, 0, 0, 0, 0, 0, 0, 0]) + b"trailing",
        # MAX_DATAGRAM with payload_len matching
        bytes([0x4D, 0x50, VERSION, ENCODING_PROTOBUF])
        + bytes(4)
        + struct.pack("<H", MAX_PAYLOAD)
        + bytes(2)
        + b"\x00" * MAX_PAYLOAD,
        # MAX_DATAGRAM + 1 (deliberately over the cap)
        bytes([0x4D, 0x50, VERSION, ENCODING_RAW])
        + bytes(4)
        + struct.pack("<H", MAX_PAYLOAD + 1)
        + bytes(2)
        + b"\x00" * (MAX_PAYLOAD + 1),
        # claimed payload_len wraps a 16-bit unsigned value
        bytes([0x4D, 0x50, VERSION, ENCODING_RAW, 0, 0, 0, 0, 0xFE, 0xFF, 0, 0]) + b"\x00" * 65534,
        # every reserved encoding byte
        *[
            bytes([0x4D, 0x50, VERSION, enc, 0, 0, 0, 0, 0, 0, 0, 0])
            for enc in (0x02, 0x10, 0x40, 0x80, 0xFF)
        ],
        # version field claims something other than 0x01
        *[
            bytes([0x4D, 0x50, ver, ENCODING_RAW, 0, 0, 0, 0, 0, 0, 0, 0])
            for ver in (0x00, 0x02, 0x7F, 0xFF)
        ],
    ]
    commands = [f"D {c.hex()}" for c in cases]
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"adversarial cases (n={len(cases)})")


def test_cpp_encoder_all_encoding_values(wire_san_available) -> None:
    """Drive encode_header with every possible encoding byte (including
    reserved) and several payload sizes. The C++ encoder shouldn't UB on
    any of them -- it's a contract violation but should be a clean
    contract violation (e.g. assertion / logged error), not memory
    corruption."""
    rng = random.Random(0xC0DE)
    commands = []
    for enc in range(256):
        payload_len = rng.choice([0, 1, 16, 512, MAX_PAYLOAD])
        crc = rng.randint(0, 0xFFFFFFFF)
        commands.append(f"E {crc:08x} {enc:02x} {'aa' * payload_len}")
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, "all 256 encoding bytes")


def test_cpp_harness_command_channel(wire_san_available) -> None:
    """Test the test harness itself with malformed commands -- ensures
    no bug in the harness leaks into a false-negative for the fuzzer."""
    nasty = [
        "X bad command",
        "E",
        "D",
        "E zzz nope nope",
        "D zz",                          # odd-length hex
        "E " + "f" * 1000 + " 00 ",       # huge crc field
        "",
        "\x00\x00\x00",
    ]
    proc = _run_san(WIRE_BIN_SAN, "\n".join(nasty) + "\n")
    _assert_no_san_report(proc, "nasty command-channel inputs")


# ---------------------------------------------------------------------------
# C++ topic-hash fuzzing
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def hash_san_available() -> None:
    if not HASH_BIN_SAN.exists():
        pytest.skip(
            f"{HASH_BIN_SAN.name} not built; run `make topic_hash_test_san` first"
        )


@pytest.mark.parametrize("seed", [11, 22, 33])
def test_cpp_topic_hash_random_topics(hash_san_available, seed: int) -> None:
    """topic_crc32 + topic_to_group on a flood of random topic strings.
    Topics can include any byte except \\n / \\r (the harness reads one
    topic per line) -- specifically embedded NULs, control chars,
    high-bit bytes, very long strings."""
    rng = random.Random(seed)
    topics: list[str] = []
    for _ in range(min(FUZZ_ITERS, 1500)):
        n = rng.randint(0, 1000)
        line_safe = list(range(0x01, 0x0A)) + list(range(0x0B, 0x0D)) + list(range(0x0E, 0x100))
        topic = bytes(rng.choice(line_safe) for _ in range(n)).decode("latin-1")
        topics.append(topic)
    proc = _run_san(HASH_BIN_SAN, "\n".join(topics) + "\n")
    _assert_no_san_report(proc, f"random-topics seed={seed} n={len(topics)}")


def test_cpp_topic_hash_long_topics(hash_san_available) -> None:
    """Make sure SHA-256 handles long inputs that cross multiple internal
    64-byte blocks correctly (chains of update() calls inside one input)."""
    sizes = [0, 1, 55, 56, 63, 64, 65, 119, 120, 127, 128, 129, 1000, 10000, 100000]
    topics = ["A" * n for n in sizes]
    proc = _run_san(HASH_BIN_SAN, "\n".join(topics) + "\n")
    _assert_no_san_report(proc, f"long-topic sizes {sizes}")


# ---------------------------------------------------------------------------
# Cross-check: every accept/reject decision is internally consistent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [9001])
def test_cpp_decoder_invariants(wire_san_available, seed: int) -> None:
    """Stronger than 'doesn't crash': for every input the C++ decoder
    says OK, the reported (crc, encoding, payload) must reconstruct
    exactly back to the input via the C++ encoder. Catches decoder
    bugs that produce *plausible* garbage rather than detected
    failure."""
    rng = random.Random(seed)
    inputs: list[bytes] = []
    for _ in range(min(FUZZ_ITERS, 1000)):
        n = rng.randint(0, MAX_DATAGRAM + 50)
        inputs.append(bytes(rng.randint(0, 255) for _ in range(n)))
    commands = [f"D {b.hex()}" for b in inputs]
    proc = _run_san(WIRE_BIN_SAN, "\n".join(commands) + "\n")
    _assert_no_san_report(proc, f"invariant-check seed={seed}")

    lines = proc.stdout.strip().splitlines()
    assert len(lines) == len(inputs)
    for orig, line in zip(inputs, lines):
        if line.startswith("ERR"):
            continue
        # OK <crc> <enc> <payload>
        parts = line.split()
        assert len(parts) == 4, f"malformed OK line: {line!r}"
        crc_hex, enc_hex, payload_hex = parts[1], parts[2], parts[3]
        # Re-encode via the same C++ harness and verify byte-for-byte equality
        # with the original (only meaningful when decode said OK).
        reencode_cmd = f"E {crc_hex} {enc_hex} {payload_hex}"
        check = _run_san(WIRE_BIN_SAN, reencode_cmd + "\n")
        _assert_no_san_report(check, f"re-encode of {orig.hex()}")
        reencoded = bytes.fromhex(check.stdout.strip().split()[1])
        assert reencoded == orig, (
            f"decoder accepted {orig.hex()} but re-encode gave {reencoded.hex()}"
        )
