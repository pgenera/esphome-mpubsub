"""Fuzz tests for the Python protobuf reference (tests/unit/protobuf.py).

These are spec-hygiene checks: the Python reference is the executable
ground truth that ``DynamicReader``, the codegen-emitted
``decode_varint``/``decode_length``/``decode_32bit`` overrides, and
probe.py all must agree with. If the reference itself accepts garbage
or crashes on adversarial input, the cross-checks downstream are
worthless.

Categories (mirrors the wire-format fuzzer's structure):

1. Random bytes -- pure noise, must never crash, must mostly reject.
2. Mutated valid -- bit-flips / truncations / appends to known-good
   protobuf streams.
3. Adversarial -- hand-picked nasty cases (oversize length, deeply
   nested length-delim, malformed varints at byte boundaries, etc.).
"""

from __future__ import annotations

import os
import random
import struct

import pytest

from protobuf import (
    decode,
    encode_bool,
    encode_bytes,
    encode_float,
    encode_int32,
    encode_string,
    encode_uint32,
    encode_uint64,
    encode_varint,
)


FUZZ_ITERS = int(os.environ.get("FUZZ_ITERS", "2000"))


def _decode_classify(buf: bytes) -> str:
    """Returns 'ok', 'value_error', or 'other_exception:<type>'. Only
    the last category is a regression."""
    try:
        fields = decode(buf)
        # Consistency: returned fields must look sane.
        for f in fields:
            assert f.wire in (0, 2, 5), f"unknown wire type {f.wire}"
            if f.wire == 0:
                assert isinstance(f.raw, int), type(f.raw)
            elif f.wire == 2:
                assert isinstance(f.raw, (bytes, bytearray)), type(f.raw)
            elif f.wire == 5:
                assert isinstance(f.raw, int) and 0 <= f.raw <= 0xFFFFFFFF
        return "ok"
    except ValueError:
        return "value_error"
    except Exception as e:  # pragma: no cover -- regression we hunt
        return f"other_exception:{type(e).__name__}:{e}"


# ---------------------------------------------------------------------------
# Random bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", [1, 42, 1234, 999999])
def test_decode_random_bytes_never_crashes(seed: int) -> None:
    rng = random.Random(seed)
    outcomes: dict[str, int] = {"ok": 0, "value_error": 0}
    for _ in range(FUZZ_ITERS):
        n = rng.randint(0, 1024)
        buf = bytes(rng.randint(0, 255) for _ in range(n))
        outcome = _decode_classify(buf)
        if outcome.startswith("other_exception"):
            pytest.fail(f"crash on seed={seed} input={buf.hex()}: {outcome}")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
    # Random bytes shouldn't pass anywhere near 100% of the time.
    assert outcomes["value_error"] > 0, outcomes


# ---------------------------------------------------------------------------
# Mutated valid
# ---------------------------------------------------------------------------


def _build_valid_message(rng: random.Random) -> bytes:
    """Assemble a random valid protobuf body from the encoders we have."""
    parts: list[bytes] = []
    for _ in range(rng.randint(1, 8)):
        tag = rng.randint(1, 100)
        kind = rng.choice(["int32", "uint32", "uint64", "bool", "float", "string", "bytes"])
        if kind == "int32":
            parts.append(encode_int32(tag, rng.randint(-(2**31), 2**31 - 1)))
        elif kind == "uint32":
            parts.append(encode_uint32(tag, rng.randint(0, 2**32 - 1)))
        elif kind == "uint64":
            parts.append(encode_uint64(tag, rng.randint(0, 2**64 - 1)))
        elif kind == "bool":
            parts.append(encode_bool(tag, rng.randint(0, 1) == 1))
        elif kind == "float":
            parts.append(encode_float(tag, rng.uniform(-1e9, 1e9)))
        elif kind == "string":
            n = rng.randint(0, 32)
            parts.append(encode_string(tag, "".join(chr(rng.randint(0x20, 0x7E)) for _ in range(n))))
        elif kind == "bytes":
            n = rng.randint(0, 32)
            parts.append(encode_bytes(tag, bytes(rng.randint(0, 255) for _ in range(n))))
    return b"".join(parts)


@pytest.mark.parametrize("seed", [7, 88, 333, 4242])
def test_decode_mutated_valid_never_crashes(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(FUZZ_ITERS):
        good = bytearray(_build_valid_message(rng))
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
        outcome = _decode_classify(bytes(good))
        if outcome.startswith("other_exception"):
            pytest.fail(f"crash on mutated input {bytes(good).hex()}: {outcome}")


# ---------------------------------------------------------------------------
# Adversarial
# ---------------------------------------------------------------------------


def test_adversarial_oversize_length_rejected() -> None:
    # tag 1, length-delim, claimed length = 2^32 - 1, but only 2 bytes follow.
    buf = b"\x0a" + encode_varint(2**32 - 1) + b"ab"
    assert _decode_classify(buf) == "value_error"


def test_adversarial_truncated_at_every_offset() -> None:
    # Truncate a valid message at every offset; none should crash.
    good = encode_uint32(1, 300) + encode_string(2, "hello") + encode_float(3, 1.5)
    for n in range(len(good)):
        outcome = _decode_classify(good[:n])
        assert not outcome.startswith("other_exception"), (n, outcome)


def test_adversarial_runaway_varint() -> None:
    # A varint with all continuation bits set and no terminator.
    buf = b"\x08" + b"\xff" * 12
    assert _decode_classify(buf) == "value_error"


def test_adversarial_zero_length_string() -> None:
    # Should decode cleanly to an empty bytes payload.
    buf = b"\x12\x00"
    fields = decode(buf)
    assert len(fields) == 1
    assert fields[0].raw == b""


def test_adversarial_huge_repeated_packed_run() -> None:
    # 10000 small uint32 fields back-to-back.
    parts = [encode_uint32(1, i) for i in range(10_000)]
    buf = b"".join(parts)
    fields = decode(buf)
    assert len(fields) == 10_000


def test_adversarial_unknown_wire_types() -> None:
    # Every unsupported wire type tag in turn -- 1, 3, 4, 6, 7.
    for wire in (1, 3, 4, 6, 7):
        buf = encode_varint((1 << 3) | wire)
        outcome = _decode_classify(buf)
        # 1 = fixed64, 3/4 = group start/end (proto2 legacy), 6/7 = reserved.
        # All should be rejected by our reference (we don't support any of them).
        assert outcome == "value_error", (wire, outcome)


def test_adversarial_length_then_eof() -> None:
    # length-delim header with no body bytes at all.
    buf = b"\x0a\x05"  # tag 1, length-delim, claimed length 5 -- but no follow-up
    assert _decode_classify(buf) == "value_error"


def test_adversarial_nested_length_recursion() -> None:
    # A length-delim field whose body is itself a length-delim field,
    # nested 100 deep. Each layer is well-formed; decode() doesn't recurse
    # but a careless DynamicReader::as_message walker could blow the stack.
    inner = b""
    for _ in range(100):
        inner = b"\x0a" + encode_varint(len(inner)) + inner
    fields = decode(inner)
    assert len(fields) == 1
    assert fields[0].wire == 2
    # We don't recurse here, but if someone does, they need to track depth.
