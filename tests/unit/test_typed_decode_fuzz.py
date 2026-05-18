"""Fuzz the typed-decode path through the actual C++ binary.

The wire-format decoder gets exhaustively fuzzed against ASan + UBSan
via the standalone harness in test_fuzz.py. But that harness stops at
the envelope: it can't reach the codegen-emitted protobuf decoders
(``decode_varint``/``decode_length``/``decode_32bit``) generated for
each typed message, because those depend on the entire ESPHome runtime
to compile.

So we fuzz the typed-decode path *through a running host binary*:

  1. Boot a sanitizer-instrumented typed_subscriber binary (compiled
     from tests/typed_subscriber_san.yaml with -fsanitize=address,undefined).
  2. Fire thousands of crafted PROTOBUF packets at the subscriber's
     multicast group. Every packet has a valid envelope (so it reaches
     the typed decoder) and a matching SCHEMA_ID (so the typed
     callback is invoked).
  3. The bodies are deliberate garbage -- truncated varints, oversize
     length-delim, wire-type confusion, nested length recursion.
  4. After the fuzz batch, send one known-good packet and verify the
     subscriber still decodes it correctly. This proves the binary
     didn't silently corrupt its internal state.
  5. Terminate the binary cleanly and assert no sanitizer banners
     appeared in its log.
"""

from __future__ import annotations

import os
import random
import re
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "components" / "multicast_pubsub"))

from reference import (
    ENCODING_PROTOBUF,
    SCOPE_LINK_LOCAL,
    encode as encode_envelope,
    topic_to_group,
)
from proto_emitter import Field, Message, schema_id
import protobuf as pb

REPO = HERE.parents[1]
TESTS = REPO / "tests"
SAN_BUILD = (
    TESTS / ".esphome" / "build" / "pubsub-typed-subscriber-san"
    / ".pioenvs" / "pubsub-typed-subscriber-san" / "program"
)

FUZZ_ITERS = int(os.environ.get("FUZZ_ITERS", "2000"))

pytestmark = pytest.mark.skipif(
    not SAN_BUILD.exists(),
    reason=(
        "sanitizer-instrumented typed_subscriber not built. Run "
        "`esphome compile tests/typed_subscriber_san.yaml` first."
    ),
)


# The schema that typed_subscriber_san.yaml declares -- mirror it
# byte-for-byte so we compute the same SCHEMA_ID and probe the same
# typed callback.
ROOM_CLIMATE = Message(
    id="room_climate",
    fields=(
        Field("temperature", "float", 1),
        Field("humidity", "float", 2),
        Field("room_id", "string", 3),
    ),
)
ROOM_CLIMATE_SCHEMA_ID = schema_id(ROOM_CLIMATE)


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


def _wrap_protobuf(schema_id_: int, body: bytes) -> bytes:
    """Build a complete wire packet for typed subscribers: envelope +
    SCHEMA_ID (LE) + body."""
    return encode_envelope(
        "test/climate",
        bytes([schema_id_ & 0xFF, (schema_id_ >> 8) & 0xFF]) + body,
        encoding=ENCODING_PROTOBUF,
    )


def _send(sock: socket.socket, group, port: int, packet: bytes) -> None:
    sock.sendto(packet, (str(group), port))


# ---------------------------------------------------------------------------
# Adversarial protobuf body generators
# ---------------------------------------------------------------------------


def _random_garbage_body(rng: random.Random) -> bytes:
    """Random bytes of varying length, no protobuf structure."""
    return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 400)))


def _mutated_valid_body(rng: random.Random) -> bytes:
    """A valid RoomClimate body with random bit-flips / truncations / appends."""
    base = (
        pb.encode_float(1, rng.uniform(-1e6, 1e6))
        + pb.encode_float(2, rng.uniform(-1e6, 1e6))
        + pb.encode_string(3, "garage")
    )
    buf = bytearray(base)
    for _ in range(rng.randint(1, 4)):
        op = rng.choice(["flip", "truncate", "append", "zero", "max"])
        if op == "flip" and buf:
            buf[rng.randint(0, len(buf) - 1)] ^= 1 << rng.randint(0, 7)
        elif op == "truncate" and buf:
            buf = buf[: rng.randint(0, len(buf))]
        elif op == "append":
            buf.extend(rng.randint(0, 255) for _ in range(rng.randint(1, 32)))
        elif op == "zero" and buf:
            buf[rng.randint(0, len(buf) - 1)] = 0
        elif op == "max" and buf:
            buf[rng.randint(0, len(buf) - 1)] = 0xFF
    return bytes(buf)


def _adversarial_bodies() -> list[bytes]:
    """Hand-picked nasty payloads targeting specific decoder paths."""
    cases: list[bytes] = []

    # Truncated varint as the first byte.
    cases.append(b"\x08")
    cases.append(b"\x08\xff")
    cases.append(b"\x08" + b"\xff" * 12)  # never-terminating varint

    # Length-delim header claiming impossible size.
    cases.append(b"\x1a" + pb.encode_varint(2**31 - 1))  # tag 3 (room_id), huge len
    cases.append(b"\x1a" + pb.encode_varint(2**31 - 1) + b"ab")

    # Empty length-delim string.
    cases.append(b"\x1a\x00")

    # Wire-type confusion: tag 1 (temperature, expected fixed32) sent as varint.
    cases.append(b"\x08" + pb.encode_varint(0xDEADBEEF))

    # Wire-type confusion: tag 3 (room_id, expected length-delim) sent as float.
    cases.append(b"\x1d" + struct.pack("<f", 1.5))

    # Unknown high tag with each wire type.
    for wire in (0, 2, 5):
        cases.append(pb.encode_varint((999 << 3) | wire) + b"\x00\x00\x00\x00")

    # Unsupported wire types (1=fixed64, 3/4=group start/end, 6/7=reserved).
    for wire in (1, 3, 4, 6, 7):
        cases.append(pb.encode_varint((1 << 3) | wire) + b"\x00" * 8)

    # Nested length-delim recursion (one repeated field tag containing
    # itself). Decoders that recurse without bound checking blow the
    # stack here.
    inner = b""
    for _ in range(200):
        inner = b"\x1a" + pb.encode_varint(len(inner)) + inner
    cases.append(inner)

    # Maximum-size body (just under the 1218-byte typed-payload cap:
    # 1220 total - 2 schema id = 1218).
    cases.append(b"\x1a" + pb.encode_varint(1200) + b"x" * 1200)

    # Many fields, all of the same tag (exercises a repeated-field
    # push_back loop on a singular subscriber).
    bulk = b""
    for _ in range(50):
        bulk += pb.encode_float(1, 1.0)
    cases.append(bulk)

    return cases


# ---------------------------------------------------------------------------
# Test driver
# ---------------------------------------------------------------------------


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
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


def _spawn_san(log_path: Path) -> subprocess.Popen[bytes]:
    log = open(log_path, "wb")
    return subprocess.Popen(
        [str(SAN_BUILD)],
        stdout=log,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
        env={
            **os.environ,
            # exit=42 makes sanitizer detection a binary signal post-mortem
            "ASAN_OPTIONS": "abort_on_error=0:halt_on_error=0:exitcode=42",
            "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=0",
        },
    )


def _terminate(proc: subprocess.Popen[bytes]) -> int:
    """SIGINT first (lets sanitizers flush), then SIGKILL if needed.
    Returns the process exit code."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except ProcessLookupError:
        return proc.wait(timeout=2)
    try:
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        return proc.wait(timeout=5)


def _assert_no_sanitizer_report(log_path: Path) -> None:
    text = _ANSI.sub("", log_path.read_text(errors="replace"))
    for banner in _SAN_BANNERS:
        if banner in text:
            pytest.fail(
                f"Sanitizer reported '{banner}'. Log tail:\n{text[-4000:]}"
            )


def _build_sender_socket():
    sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
    sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_MULTICAST_HOPS, 1)
    return sock


@pytest.mark.parametrize("seed", [1, 42, 1234])
def test_typed_decoder_survives_garbage_protobuf_under_san(seed: int, tmp_path: Path) -> None:
    """Throw FUZZ_ITERS random + mutated PROTOBUF bodies (envelope valid,
    SCHEMA_ID matching) at the typed subscriber under ASan+UBSan. After
    the fuzz batch, send one known-good packet and verify the subscriber
    decoded it correctly -- proves no internal corruption.
    """
    log_path = tmp_path / "san.log"
    proc = _spawn_san(log_path)
    try:
        time.sleep(1.5)  # let the binary bind + join groups
        rng = random.Random(seed)
        group = topic_to_group("test/climate", SCOPE_LINK_LOCAL)
        sock = _build_sender_socket()

        # Mix the three categories of fuzz inputs.
        iters_per_cat = max(1, FUZZ_ITERS // 3)
        for _ in range(iters_per_cat):
            _send(sock, group, 18512, _wrap_protobuf(ROOM_CLIMATE_SCHEMA_ID, _random_garbage_body(rng)))
        for _ in range(iters_per_cat):
            _send(sock, group, 18512, _wrap_protobuf(ROOM_CLIMATE_SCHEMA_ID, _mutated_valid_body(rng)))
        for body in _adversarial_bodies():
            _send(sock, group, 18512, _wrap_protobuf(ROOM_CLIMATE_SCHEMA_ID, body))

        # Drain time, then a sentinel "known-good" packet that should
        # decode cleanly. Use a unique room_id we can grep for to
        # confirm the subscriber processed it.
        time.sleep(0.5)
        sentinel = (
            pb.encode_float(1, 88.5)
            + pb.encode_float(2, 11.0)
            + pb.encode_string(3, "post-fuzz-canary")
        )
        _send(sock, group, 18512, _wrap_protobuf(ROOM_CLIMATE_SCHEMA_ID, sentinel))
        time.sleep(1.0)
    finally:
        rc = _terminate(proc)

    text = _ANSI.sub("", log_path.read_text(errors="replace"))

    # Sanity: the post-fuzz canary made it through, proving no
    # internal corruption from the garbage stream.
    assert (
        "temperature=88.50 humidity=11.00 room='post-fuzz-canary'" in text
    ), (
        f"post-fuzz sentinel not seen in subscriber log -- decoder may "
        f"have entered a bad state. Tail:\n{text[-2000:]}"
    )

    _assert_no_sanitizer_report(log_path)

    # Exit code 42 means sanitizers caught something even if we missed
    # the banner above; SIGINT exits via 128+2=130 or 0.
    assert rc != 42, f"sanitizer-triggered nonzero exit ({rc}); see {log_path}"
