"""Cross-implementation integration tests via tests/probe.py.

These start the actual host-platform binaries produced from
tests/typed_publisher.yaml + tests/typed_subscriber.yaml and verify
that:

1. probe.py (Python decoder + Python protobuf reference) correctly
   decodes packets emitted by the C++ codegen-generated encoder, and
2. probe.py's Python encoder produces packets the C++ codegen-generated
   decoder accepts and surfaces with the expected field values.

This is the third-implementation cross-check: when both directions
agree, the spec is in good shape -- two independent implementations
of the wire format are byte-for-byte compatible.

Skipped unless the host binaries are present (run
``esphome compile tests/typed_publisher.yaml`` and
``esphome compile tests/typed_subscriber.yaml`` first).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"
BUILD = TESTS / ".esphome" / "build"
PUBLISHER = BUILD / "pubsub-typed-publisher" / ".pioenvs" / "pubsub-typed-publisher" / "program"
SUBSCRIBER = BUILD / "pubsub-typed-subscriber" / ".pioenvs" / "pubsub-typed-subscriber" / "program"
PROBE = TESTS / "probe.py"
PUBLISHER_YAML = TESTS / "typed_publisher.yaml"
SUBSCRIBER_YAML = TESTS / "typed_subscriber.yaml"


def _binaries_available() -> bool:
    return PUBLISHER.exists() and SUBSCRIBER.exists()


pytestmark = pytest.mark.skipif(
    not _binaries_available(),
    reason=(
        "host-platform binaries not built. Run "
        "`esphome compile tests/typed_publisher.yaml` and "
        "`esphome compile tests/typed_subscriber.yaml` first."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spawn(binary: Path, log: Path) -> subprocess.Popen[bytes]:
    log_file = open(log, "wb")
    return subprocess.Popen(
        [str(binary)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait(timeout=3)


# Strip ANSI escape sequences from ESPHome's coloured log output so we can
# match on the substantive parts.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _decoded_log(path: Path) -> str:
    return _ANSI.sub("", path.read_text(errors="replace"))


# ---------------------------------------------------------------------------
# Direction 1: C++ encodes -> probe.py decodes
# ---------------------------------------------------------------------------


def test_probe_decodes_cpp_typed_publisher(tmp_path: Path) -> None:
    """Boot the C++ typed publisher, capture three packets via probe.py
    with --schema, and assert the decoded fields look right.
    """
    pub_log = tmp_path / "pub.log"
    probe_log = tmp_path / "probe.log"

    pub = _spawn(PUBLISHER, pub_log)
    try:
        # Give the binary a moment to bind sockets + start its intervals.
        time.sleep(1.0)
        # probe.py listens until it has captured --max-packets or the
        # --timeout fires. The publisher emits a RoomClimate every 1s and
        # a dynamic-publish every 3s, so 4s captures comfortably more
        # than enough.
        result = subprocess.run(
            [
                sys.executable,
                str(PROBE),
                "--topic", "test/climate",
                "--scope", "link-local",
                "--schema", str(PUBLISHER_YAML),
                "--max-packets", "3",
                "--timeout", "6",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        _kill(pub)
    probe_log.write_text(result.stdout + result.stderr)

    out = result.stdout
    # Should have at least 3 captured packets, each tagged room_climate
    # and carrying the expected field names.
    captures = [line for line in out.splitlines() if "enc=PROTOBUF" in line]
    assert len(captures) >= 3, f"expected >=3 captures, got {len(captures)}:\n{out}"
    for line in captures:
        assert "room_climate" in line, line
        assert '"temperature"' in line, line
        assert '"humidity"' in line, line
        assert '"room_id"' in line, line

    # At least one packet should be from the C++ Call path with
    # room_id="test-room"; the DynamicMessage path uses "dynamic-room".
    rooms = {m.group(1) for m in re.finditer(r'"room_id":\s*"([^"]+)"', out)}
    assert "test-room" in rooms or "dynamic-room" in rooms, rooms


# ---------------------------------------------------------------------------
# Direction 2: probe.py encodes -> C++ decodes
# ---------------------------------------------------------------------------


def test_cpp_subscriber_receives_probe_typed_publish(tmp_path: Path) -> None:
    """Boot the C++ typed subscriber, publish a RoomClimate via probe.py,
    and assert the subscriber's on_message logged the typed field values.
    """
    sub_log = tmp_path / "sub.log"
    sub = _spawn(SUBSCRIBER, sub_log)
    try:
        # Let the subscriber bind + join multicast groups.
        time.sleep(1.5)
        # Publish a single typed RoomClimate via probe.py. Distinct
        # field values pinpoint this packet in the subscriber's log
        # (the C++ publisher binary would emit unrelated traffic if
        # it were also running).
        publish_result = subprocess.run(
            [
                sys.executable,
                str(PROBE),
                "--topic", "test/climate",
                "--scope", "link-local",
                "--schema", str(SUBSCRIBER_YAML),
                "--publish",
                "--message", "room_climate",
                "--field", "temperature=42.5",
                "--field", "humidity=33.0",
                "--field", "room_id=probe-origin",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Give the subscriber a moment to process and log the packet.
        time.sleep(1.0)
    finally:
        _kill(sub)

    assert publish_result.returncode == 0, publish_result.stdout + publish_result.stderr
    assert "sent" in publish_result.stdout, publish_result.stdout

    decoded = _decoded_log(sub_log)
    assert "temperature=42.50 humidity=33.00 room='probe-origin'" in decoded, (
        f"subscriber did not see probe-origin packet. Tail of sub log:\n"
        f"{decoded[-2000:]}"
    )
